"""
tests/test_integration.py – End-to-end integration tests (require network).

These tests hit live external APIs and are SKIPPED by default.
Run them manually before going live:

    pytest tests/test_integration.py --network -v

What is tested:
  • Gamma API returns parseable markets and our filters work against real data
  • Pyth REST feed returns a sane BTC/USD price
  • Pyth WebSocket feed connects and delivers a price within 15 seconds
  • Full bot tick loop runs without error in simulation mode for 10 seconds
"""

from __future__ import annotations

import asyncio
import time

import pytest
import requests

from api_client import (
    GAMMA_BASE_URL,
    PYTH_HERMES_URL,
    BTC_USD_FEED_ID,
    PythPriceFeed,
    PythWebSocketFeed,
    _extract_tokens,
    _is_btc_5min_market,
    _parse_end_time,
)


# ─── Gamma API ────────────────────────────────────────────────────────────────

@pytest.mark.network
class TestGammaApiIntegration:

    def test_markets_endpoint_returns_list(self):
        resp = requests.get(
            f"{GAMMA_BASE_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 50},
            timeout=15,
        )
        assert resp.status_code == 200
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])
        assert isinstance(markets, list), f"Expected list, got {type(markets)}"
        assert len(markets) > 0, "Gamma API returned 0 markets"

    def test_market_fields_are_present(self):
        resp = requests.get(
            f"{GAMMA_BASE_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 10},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])

        for m in markets[:5]:
            assert "question" in m or "Question" in m, f"No question field in: {list(m.keys())}"

    def test_btc_5min_filter_does_not_crash_on_live_data(self):
        resp = requests.get(
            f"{GAMMA_BASE_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])

        # Must not raise on any real market
        for m in markets:
            _is_btc_5min_market(m)
            _parse_end_time(m)
            _extract_tokens(m)

    def test_end_time_parsing_for_all_live_markets(self):
        """Every active market with an endDateIso field must parse without error."""
        resp = requests.get(
            f"{GAMMA_BASE_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])

        for m in markets:
            if "endDateIso" in m or "end_date_iso" in m:
                ts = _parse_end_time(m)
                if ts is not None:
                    assert isinstance(ts, int), f"Expected int, got {type(ts)}"
                    assert ts > 0, f"Non-positive timestamp: {ts}"


# ─── Pyth REST ────────────────────────────────────────────────────────────────

@pytest.mark.network
class TestPythRestIntegration:

    def test_fetch_btc_price_returns_sane_value(self):
        feed  = PythPriceFeed()
        price = feed.fetch_btc_price()
        assert price.price > 1_000,  f"BTC price suspiciously low: {price.price}"
        assert price.price < 10_000_000, f"BTC price suspiciously high: {price.price}"
        assert price.confidence >= 0
        assert price.feed_id == BTC_USD_FEED_ID

    def test_price_timestamp_is_recent(self):
        feed  = PythPriceFeed()
        price = feed.fetch_btc_price()
        age   = time.time() - price.timestamp
        assert age < 120, f"Pyth REST price is {age:.0f}s old (>120s)"


# ─── Pyth WebSocket ───────────────────────────────────────────────────────────

@pytest.mark.network
class TestPythWebSocketIntegration:

    def test_websocket_delivers_price_within_15s(self):
        async def _run():
            feed = PythWebSocketFeed()
            await feed.start()
            try:
                deadline = time.time() + 15
                while time.time() < deadline:
                    if not feed.is_stale(max_age_seconds=15):
                        price = feed.get_latest()
                        assert price is not None
                        assert price.price > 1_000
                        return
                    await asyncio.sleep(0.5)
                pytest.fail("WebSocket did not deliver a price within 15 seconds")
            finally:
                await feed.stop()

        asyncio.run(_run())

    def test_websocket_is_connected_after_start(self):
        async def _run():
            feed = PythWebSocketFeed()
            await feed.start()
            # Give it a moment to connect
            await asyncio.sleep(3)
            connected = feed.is_connected
            await feed.stop()
            assert connected, "WebSocket not connected after 3 seconds"

        asyncio.run(_run())


# ─── Full bot tick (simulation mode) ─────────────────────────────────────────

@pytest.mark.network
class TestBotSimulationIntegration:

    def test_bot_runs_10_ticks_without_error(self):
        """
        Start the bot in simulation mode, run 10 ticks, then stop.
        No orders are placed; this verifies the full tick pipeline:
        market discovery → price feed → signal evaluation → logging.
        """
        import os
        os.environ.setdefault("SIMULATION_MODE", "true")
        os.environ.setdefault("PRIVATE_KEY",     "0x" + "0" * 64)
        os.environ.setdefault("POLY_API_KEY",    "test")
        os.environ.setdefault("POLY_API_SECRET", "test")
        os.environ.setdefault("POLY_API_PASSPHRASE", "test")

        from bot import BotConfig, HyperBTCBot

        config = BotConfig(
            simulation_mode   = True,
            private_key       = "0x" + "0" * 64,
            kelly_fraction    = 0.25,
            starting_bankroll = 300.0,
            tick_interval_seconds = 0.1,
        )
        bot = HyperBTCBot(config)

        async def _run_ticks():
            await bot._ws_feed.start()
            try:
                for _ in range(10):
                    await bot._tick()
                    await asyncio.sleep(0.1)
            finally:
                await bot._ws_feed.stop()
                bot.order_store.close()

        asyncio.run(_run_ticks())
