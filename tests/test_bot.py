"""
tests/test_bot.py – Unit tests for HyperBTCBot's order lifecycle
(bot.py::_manage_open_orders).

Covers issue #1: cancel-replace was broken in two ways —
  1. staleness age was only computed for "SIM-" order IDs (live orders
     were always treated as immediately stale), and
  2. a cancelled stale order was never actually replaced.

All tests run in simulation mode with the CLOB client and orderbook
fetches mocked out, so nothing here touches the network. An in-memory
OrderStore is swapped in after construction to avoid writing orders.db.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest

from api_client import MarketInfo, OrderResult, TokenPair
from bot import BotConfig, HyperBTCBot
from db import OrderStore

TOKEN_YES = "yes_token_abc"
TOKEN_NO = "no_token_xyz"

WINDOW_END = 1_800_000_300  # arbitrary 5-min boundary, far in the future


class TestBotConstruction:
    """Issue #2: the default (no PRIVATE_KEY) simulation-mode construction
    path must actually work, since it's the bot's documented safe default."""

    def test_default_config_constructs_without_a_configured_wallet(self):
        cfg = BotConfig()  # private_key="" — exactly `python bot.py` with no .env
        b = HyperBTCBot(cfg)
        try:
            assert b.client.simulation is True
        finally:
            b.order_store.close()
            if os.path.exists("orders.db"):
                os.remove("orders.db")


def make_market(end_time: int = WINDOW_END) -> MarketInfo:
    return MarketInfo(
        condition_id="cond-1",
        question="Will BTC be up?",
        end_time=end_time,
        tokens=TokenPair(yes_token_id=TOKEN_YES, no_token_id=TOKEN_NO),
        yes_bid=0.58,
        yes_ask=0.62,
        no_bid=0.38,
        no_ask=0.42,
        is_active=True,
    )


@pytest.fixture
def bot() -> HyperBTCBot:
    cfg = BotConfig(simulation_mode=True, order_timeout_seconds=5)
    b = HyperBTCBot(cfg)
    b.order_store.close()
    b.order_store = OrderStore(":memory:")

    # Isolate _manage_open_orders from real network I/O.
    b.client.get_order_status = MagicMock(
        return_value=OrderResult(order_id="unused", status="live")
    )
    b.client.cancel_order = MagicMock(return_value=True)
    b.client.place_limit_order = MagicMock(
        return_value=OrderResult(order_id="REPLACED-1", status="live", remaining_usdc=25.0)
    )
    b._refresh_orderbook = MagicMock(side_effect=lambda m: m)

    yield b
    b.order_store.close()
    if os.path.exists("orders.db"):
        os.remove("orders.db")


def track_order(
    bot: HyperBTCBot,
    order_id: str,
    *,
    age_seconds: float,
    token_id: str = TOKEN_YES,
    side: str = "BUY",
    entry_price: float = 0.60,
    size_usdc: float = 25.0,
    window_end: int = WINDOW_END,
) -> None:
    """Insert an order row with a backdated created_at and register it as open."""
    bot.order_store.insert_order(
        order_id=order_id,
        token_id=token_id,
        side=side,
        entry_price=entry_price,
        size_usdc=size_usdc,
        window_end=window_end,
    )
    backdated = int(time.time()) - int(age_seconds)
    bot.order_store._conn.execute(
        "UPDATE orders SET created_at = ? WHERE order_id = ?",
        (backdated, order_id),
    )
    bot.order_store._conn.commit()
    bot._open_orders[order_id] = OrderResult(order_id=order_id, status="live")


class TestStalenessTiming:

    def test_live_order_not_cancelled_before_timeout(self, bot):
        import asyncio

        market = make_market(end_time=int(time.time()) + 120)
        track_order(bot, "0xLIVE_ORDER_ID", age_seconds=1)

        asyncio.run(bot._manage_open_orders(market))

        assert "0xLIVE_ORDER_ID" in bot._open_orders
        bot.client.cancel_order.assert_not_called()
        row = bot.order_store.get_order("0xLIVE_ORDER_ID")
        assert row["status"] == "open"

    def test_order_cancelled_after_timeout_and_replaced(self, bot):
        import asyncio

        market = make_market(end_time=int(time.time()) + 120)
        track_order(bot, "0xLIVE_ORDER_ID", age_seconds=10)

        asyncio.run(bot._manage_open_orders(market))

        bot.client.cancel_order.assert_called_once_with("0xLIVE_ORDER_ID")
        assert "0xLIVE_ORDER_ID" not in bot._open_orders

        cancelled_row = bot.order_store.get_order("0xLIVE_ORDER_ID")
        assert cancelled_row["status"] == "cancelled"

        bot.client.place_limit_order.assert_called_once()
        _, kwargs = bot.client.place_limit_order.call_args
        assert kwargs["token_id"] == TOKEN_YES
        assert kwargs["side"] == "BUY"
        assert kwargs["size_usdc"] == pytest.approx(25.0)

        assert "REPLACED-1" in bot._open_orders
        new_row = bot.order_store.get_order("REPLACED-1")
        assert new_row is not None
        assert new_row["status"] == "open"
        assert new_row["window_end"] == market.end_time

    def test_no_replace_when_insufficient_time_left(self, bot):
        import asyncio

        # Only 3s left in the window, but timeout is 5s -> no safe time to replace.
        market = make_market(end_time=int(time.time()) + 3)
        track_order(bot, "0xLIVE_ORDER_ID", age_seconds=10)

        asyncio.run(bot._manage_open_orders(market))

        bot.client.cancel_order.assert_called_once_with("0xLIVE_ORDER_ID")
        bot.client.place_limit_order.assert_not_called()
        assert "0xLIVE_ORDER_ID" not in bot._open_orders
        assert bot._open_orders == {}

    def test_replacement_is_singular_per_stale_event(self, bot):
        import asyncio

        market = make_market(end_time=int(time.time()) + 120)
        track_order(bot, "0xLIVE_ORDER_ID", age_seconds=10)

        asyncio.run(bot._manage_open_orders(market))

        assert len(bot._open_orders) == 1
        assert bot.client.place_limit_order.call_count == 1

    def test_missing_db_row_defensive_fallback(self, bot):
        import asyncio

        market = make_market(end_time=int(time.time()) + 120)
        # Registered as open in-memory, but never persisted to the store.
        bot._open_orders["0xGHOST_ORDER"] = OrderResult(order_id="0xGHOST_ORDER", status="live")

        asyncio.run(bot._manage_open_orders(market))

        # Missing row must never be treated as immediately stale.
        bot.client.cancel_order.assert_not_called()
        assert "0xGHOST_ORDER" in bot._open_orders

    def test_no_replace_and_no_status_change_when_cancel_fails(self, bot):
        """If cancel_order() reports failure (exchange still has it live),
        we must NOT mark it cancelled or place a duplicate replacement —
        that would risk two live orders for one signal."""
        import asyncio

        bot.client.cancel_order = MagicMock(return_value=False)

        market = make_market(end_time=int(time.time()) + 120)
        track_order(bot, "0xLIVE_ORDER_ID", age_seconds=10)

        asyncio.run(bot._manage_open_orders(market))

        bot.client.cancel_order.assert_called_once_with("0xLIVE_ORDER_ID")
        bot.client.place_limit_order.assert_not_called()

        # Still tracked as open — we don't know it's actually gone.
        assert "0xLIVE_ORDER_ID" in bot._open_orders
        row = bot.order_store.get_order("0xLIVE_ORDER_ID")
        assert row["status"] == "open"
