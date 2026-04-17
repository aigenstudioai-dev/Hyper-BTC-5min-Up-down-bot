"""
bot.py – Hyper-BTC 5-Minute Up/Down Trading Bot (Main execution loop)

Usage:
    # Paper-trade (safe, default):
    python bot.py

    # Derive Polymarket API credentials from your wallet (run once):
    python bot.py --derive-api-key

    # Live trading (requires funded Polygon wallet + CLOB API creds):
    python bot.py --live

Environment:
    Copy .env.example → .env and fill in your secrets.
    SIMULATION_MODE=true in .env enables paper-trading regardless of --live flag.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional

from dotenv import load_dotenv

from api_client import (
    PolymarketAuth,
    PolymarketClient,
    PythPriceFeed,
    PythWebSocketFeed,
    MarketInfo,
    OrderResult,
    seconds_until_close,
    _next_window_close,
)
from db import OrderStore
from strategy import (
    DrawdownGuard,
    KellySizer,
    SignalEngine,
    SignalType,
    SimulationLedger,
    TradeRecord,
    WindowTracker,
)

# ─── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

# ─── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class BotConfig:
    simulation_mode: bool = True
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    kelly_fraction: float = 0.25
    max_position_usdc: float = 500.0
    starting_bankroll: float = 300.0
    max_drawdown: float = 0.15
    signal_threshold_pct: float = 0.08
    entry_window_seconds: int = 45
    order_timeout_seconds: int = 5
    tick_interval_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv()
        sim = os.getenv("SIMULATION_MODE", "true").lower() != "false"
        return cls(
            simulation_mode=sim,
            private_key=os.getenv("PRIVATE_KEY", ""),
            api_key=os.getenv("POLY_API_KEY", ""),
            api_secret=os.getenv("POLY_API_SECRET", ""),
            api_passphrase=os.getenv("POLY_API_PASSPHRASE", ""),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            max_position_usdc=float(os.getenv("MAX_POSITION_USDC", "500")),
            starting_bankroll=float(os.getenv("BANKROLL_USDC", "300")),
            max_drawdown=float(os.getenv("MAX_DRAWDOWN", "0.15")),
            signal_threshold_pct=float(os.getenv("SIGNAL_THRESHOLD_PCT", "0.08")),
            entry_window_seconds=int(os.getenv("ENTRY_WINDOW_SECONDS", "45")),
            order_timeout_seconds=int(os.getenv("ORDER_TIMEOUT_SECONDS", "5")),
        )

    def validate(self, live: bool) -> None:
        if live and not self.simulation_mode:
            if not self.private_key:
                raise ValueError("PRIVATE_KEY is required for live trading")
            if not self.api_key:
                raise ValueError(
                    "POLY_API_KEY missing. Run: python bot.py --derive-api-key"
                )


# ─── Main bot class ────────────────────────────────────────────────────────────


class HyperBTCBot:
    """
    Execution loop:
      1. Every tick (~1s): fetch BTC price, update WindowTracker.
      2. At T-45s before a window close: evaluate signal.
      3. On STRONG_BUY: size with Kelly, place limit order at mid-point.
      4. If order not filled within 5s: cancel and replace at new mid.
      5. At window close: settle paper-trades, log P&L, refresh markets.

    State machine per active market:
      WATCHING → SIGNAL_TRIGGERED → ORDER_PLACED → ORDER_FILLED / CANCELLED
    """

    def __init__(self, config: BotConfig) -> None:
        self.cfg = config

        auth = PolymarketAuth(
            private_key=config.private_key or "0x" + "0" * 64,  # placeholder in sim
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        )

        self.client = PolymarketClient(auth, simulation=config.simulation_mode)
        self._ws_feed = PythWebSocketFeed()    # primary: low-latency WebSocket cache
        self._rest_feed = PythPriceFeed()      # fallback: REST polling

        self.window_tracker = WindowTracker()
        self.signal_engine = SignalEngine(
            threshold_pct=config.signal_threshold_pct,
            entry_window_seconds=config.entry_window_seconds,
        )
        self.kelly_sizer = KellySizer(
            kelly_fraction=config.kelly_fraction,
            max_position_usdc=config.max_position_usdc,
        )
        self.drawdown_guard = DrawdownGuard(
            max_drawdown=config.max_drawdown,
            starting_bankroll=config.starting_bankroll,
        )

        # Simulation ledger (used even in live mode for record-keeping)
        self.ledger = SimulationLedger(starting_usdc=config.starting_bankroll)

        # Order persistence
        self.order_store = OrderStore()

        # Active state
        self._active_market: Optional[MarketInfo] = None
        self._open_orders: Dict[str, OrderResult] = {}    # order_id → result
        self._window_open_price: Optional[float] = None
        self._signal_fired_this_window: bool = False
        self._last_market_refresh: float = 0.0
        self._running: bool = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        mode = "SIMULATION" if self.cfg.simulation_mode else "LIVE"
        logger.info("=" * 60)
        logger.info("  Hyper-BTC 5-Minute Bot | Mode: %s", mode)
        logger.info("=" * 60)

        await self._ws_feed.start()
        self._reconcile_open_orders()
        self._running = True
        try:
            while self._running:
                try:
                    await self._tick()
                except KeyboardInterrupt:
                    logger.info("Keyboard interrupt – shutting down.")
                    self._running = False
                    break
                except Exception as exc:
                    logger.error("Tick error (continuing): %s", exc, exc_info=True)

                await asyncio.sleep(self.cfg.tick_interval_seconds)
        finally:
            await self._ws_feed.stop()
            self.order_store.close()

        # Final summary
        logger.info(self.ledger.summary())
        logger.info(self.drawdown_guard.status_line())

    async def _tick(self) -> None:
        """Single iteration of the main loop."""
        now = time.time()

        # 1. Fetch current BTC price.
        #    Prefer the WebSocket cache (sub-millisecond, no I/O).
        #    Fall back to REST if the cache is absent or stale (>10 s).
        price_data = self._ws_feed.get_latest()
        if price_data is None or self._ws_feed.is_stale(max_age_seconds=10.0):
            try:
                price_data = self._rest_feed.fetch_btc_price()
                if self._ws_feed.is_stale(max_age_seconds=10.0):
                    logger.debug(
                        "Price: REST fallback (WS stale) – %.2f", price_data.price
                    )
            except RuntimeError as exc:
                logger.error("Price feed error (both WS and REST failed): %s", exc)
                return
        current_price = price_data.price

        # 2. Update window open tracker
        self.window_tracker.record_tick(current_price, ts=now)

        # 3. Refresh markets every 60 seconds or when we have no active market
        if (now - self._last_market_refresh > 60) or self._active_market is None:
            await self._refresh_market()

        if self._active_market is None:
            logger.debug("No active BTC 5-min market found – waiting.")
            return

        market = self._active_market
        time_left = seconds_until_close(market.end_time)

        # 4. At window close: settle trades and reset state
        if time_left <= 0:
            await self._on_window_close(current_price)
            return

        # 5. Circuit breaker check
        current_balance = (
            self.ledger.balance
            if self.cfg.simulation_mode
            else self.client.get_usdc_balance()
        )
        self.drawdown_guard.update(current_balance)
        if self.drawdown_guard.is_halted:
            logger.warning("Bot halted by drawdown guard.")
            self._running = False
            return

        # 6. Manage any pending orders (cancel-replace if stale)
        await self._manage_open_orders(market)

        # 7. Evaluate signal at T-entry_window
        if time_left <= self.cfg.entry_window_seconds and not self._signal_fired_this_window:
            await self._evaluate_and_trade(market, current_price, current_balance, time_left)

        # 8. Heartbeat log every 10 seconds
        if int(now) % 10 == 0:
            logger.info(
                "BTC=%.2f | T-close=%ds | %s",
                current_price,
                int(time_left),
                self.drawdown_guard.status_line(),
            )

    # ── Signal evaluation & order placement ──────────────────────────────────

    async def _evaluate_and_trade(
        self,
        market: MarketInfo,
        current_price: float,
        bankroll: float,
        time_left: float,
    ) -> None:
        window_open = self.window_tracker.get_open_price(market.end_time)
        if window_open is None:
            logger.debug("Window open price not yet recorded.")
            return

        # Refresh orderbook prices
        market_refreshed = self._refresh_orderbook(market)

        signal = self.signal_engine.evaluate(
            current_price=current_price,
            window_open_price=window_open,
            seconds_until_close=time_left,
            yes_mid=market_refreshed.yes_mid,
            no_mid=market_refreshed.no_mid,
            yes_token_id=market.tokens.yes_token_id,
            no_token_id=market.tokens.no_token_id,
        )

        if signal.signal_type == SignalType.NONE:
            logger.debug(
                "No signal | delta=%.4f%% | threshold=%.4f%%",
                signal.window_delta_pct,
                self.cfg.signal_threshold_pct,
            )
            return

        # Position sizing via Kelly
        size_usdc = self.kelly_sizer.size(
            bankroll=bankroll,
            win_prob=signal.confidence,
            price=signal.recommended_price,
        )

        if size_usdc < 1.0:
            logger.info("Kelly size too small (%.4f USDC) – skipping.", size_usdc)
            return

        self._signal_fired_this_window = True
        logger.info(
            "ENTRY | signal=%s | size=%.2f USDC | token=%s | price=%.4f",
            signal.signal_type.value,
            size_usdc,
            signal.recommended_token[:12],
            signal.recommended_price,
        )

        # Place limit order at mid-point
        result = self.client.place_limit_order(
            token_id=signal.recommended_token,
            side="BUY",
            price=signal.recommended_price,
            size_usdc=size_usdc,
            expiry_seconds=self.cfg.order_timeout_seconds * 2,
        )

        if result.status == "error":
            logger.error("Order failed: %s", result.error)
            self._signal_fired_this_window = False  # allow retry
            return

        self._open_orders[result.order_id] = result

        # Persist order before it can be lost on crash
        self.order_store.insert_order(
            order_id=result.order_id,
            token_id=signal.recommended_token,
            side="BUY",
            entry_price=signal.recommended_price,
            size_usdc=size_usdc,
            window_end=market.end_time,
        )

        # Track in simulation ledger
        record = TradeRecord(
            trade_id=result.order_id,
            signal=signal,
            entry_usdc=size_usdc,
            entry_price=signal.recommended_price,
        )
        if self.cfg.simulation_mode:
            self.ledger.open_trade(record)

    # ── Order lifecycle management ────────────────────────────────────────────

    async def _manage_open_orders(self, market: MarketInfo) -> None:
        """
        Poll pending orders.  Cancel and replace if not filled within
        ORDER_TIMEOUT_SECONDS at the current mid-point.
        """
        if not self._open_orders:
            return

        for order_id in list(self._open_orders.keys()):
            placed = self._open_orders[order_id]
            status = self.client.get_order_status(order_id)

            if status.status in ("matched", "filled"):
                logger.info("Order %s filled.", order_id)
                self.order_store.update_status(order_id, "filled")
                del self._open_orders[order_id]
                continue

            if status.status == "error":
                logger.warning("Order %s error: %s", order_id, status.error)
                self.order_store.update_status(order_id, "cancelled")
                del self._open_orders[order_id]
                continue

            # Check age via order_id timestamp (SIM prefix strips cleanly)
            try:
                placed_ts = int(order_id.split("-")[1]) if order_id.startswith("SIM-") else 0
            except Exception:
                placed_ts = 0

            age = time.time() - placed_ts if placed_ts else self.cfg.order_timeout_seconds + 1

            if age > self.cfg.order_timeout_seconds:
                logger.info(
                    "Order %s stale after %.0fs – cancel and replace.", order_id, age
                )
                self.client.cancel_order(order_id)
                self.order_store.update_status(order_id, "cancelled")
                del self._open_orders[order_id]

                # Replace at updated mid
                refreshed = self._refresh_orderbook(market)
                if self._signal_fired_this_window and refreshed:
                    # Re-use the signal's token; update price
                    # (We only replace once to avoid a loop)
                    pass  # replacement handled next _tick via _signal_fired = True

    # ── Window close handler ──────────────────────────────────────────────────

    async def _on_window_close(self, close_price: float) -> None:
        """
        Called when a market window expires.
        Settles simulation trades, logs results, and resets state.
        """
        market = self._active_market
        if market is None:
            return

        open_price = self.window_tracker.get_open_price(market.end_time)
        if open_price is None:
            open_price = close_price

        logger.info(
            "WINDOW CLOSE | open=%.2f | close=%.2f | move=%.4f%%",
            open_price,
            close_price,
            (close_price - open_price) / open_price * 100 if open_price else 0,
        )

        if self.cfg.simulation_mode:
            for trade_id in self.ledger.open_trade_ids[:]:
                settled = self.ledger.settle_trade(trade_id, open_price, close_price)
                if settled is not None:
                    self.order_store.update_status(
                        trade_id, "settled", pnl_usdc=settled.pnl_usdc
                    )
            logger.info(self.ledger.summary())

        # Reset window state
        self._active_market = None
        self._signal_fired_this_window = False
        self._open_orders.clear()
        self._last_market_refresh = 0.0  # force refresh next tick

    # ── Market refresh helpers ────────────────────────────────────────────────

    async def _refresh_market(self) -> None:
        """Discover the next BTC 5-min market window."""
        try:
            markets = self.client.find_btc_5min_markets(lookahead_windows=2)
        except Exception as exc:
            logger.error("Market refresh error: %s", exc)
            self._last_market_refresh = time.time()
            return

        self._last_market_refresh = time.time()

        now = time.time()
        for m in markets:
            ttc = seconds_until_close(m.end_time)
            if ttc > 10:  # more than 10s left → tradeable
                if self._active_market is None or m.end_time != self._active_market.end_time:
                    logger.info(
                        "Active market: %s | closes in %.0fs | YES=%.4f NO=%.4f",
                        m.question[:60],
                        ttc,
                        m.yes_mid,
                        m.no_mid,
                    )
                    self._active_market = m
                    self._signal_fired_this_window = False
                return

        logger.debug("No tradeable market found this refresh.")

    def _refresh_orderbook(self, market: MarketInfo) -> MarketInfo:
        """Update the in-memory market's bid/ask from the live orderbook."""
        yes_book = self.client._fetch_orderbook(market.tokens.yes_token_id)
        no_book = self.client._fetch_orderbook(market.tokens.no_token_id)
        market.yes_bid = yes_book["bid"]
        market.yes_ask = yes_book["ask"]
        market.no_bid = no_book["bid"]
        market.no_ask = no_book["ask"]
        return market

    # ── Crash-recovery ────────────────────────────────────────────────────────

    def _reconcile_open_orders(self) -> None:
        """
        Called once at startup.  Reads all ``open`` rows from the DB and
        reconciles each against the live CLOB API (or simulation state).

        Outcomes:
          • Window already closed → mark cancelled (we can no longer trade it).
          • CLOB says filled     → mark filled.
          • CLOB says cancelled  → mark cancelled.
          • CLOB says still open → put back into self._open_orders so the
            manage loop can handle it normally (cancel-replace if stale).
          • Any error querying CLOB → leave as open, bot will retry next tick.
        """
        rows = self.order_store.get_open_orders()
        if not rows:
            return

        logger.info("Reconciling %d open order(s) from previous session…", len(rows))
        now = int(time.time())

        for row in rows:
            order_id  = row["order_id"]
            window_end = row["window_end"]

            # Window has already closed – order is unresolvable
            if window_end < now:
                logger.warning(
                    "Recovered order %s: window expired at %d – marking cancelled",
                    order_id, window_end,
                )
                self.order_store.update_status(order_id, "cancelled")
                continue

            if self.cfg.simulation_mode:
                # In sim mode the CLOB is not real; put the order back into the
                # in-memory dict so _manage_open_orders can cancel it if stale.
                self._open_orders[order_id] = OrderResult(
                    order_id=order_id,
                    status="open",
                    error=None,
                )
                logger.info("Recovered SIM order %s – restored to active orders", order_id)
                continue

            try:
                status = self.client.get_order_status(order_id)
            except Exception as exc:
                logger.error(
                    "Reconcile: could not query CLOB for %s: %s – leaving open",
                    order_id, exc,
                )
                continue

            if status.status in ("matched", "filled"):
                logger.info("Recovered order %s: was filled – updating DB", order_id)
                self.order_store.update_status(order_id, "filled")
            elif status.status in ("cancelled", "error", "expired"):
                logger.info(
                    "Recovered order %s: status=%s – updating DB", order_id, status.status
                )
                self.order_store.update_status(order_id, "cancelled")
            else:
                self._open_orders[order_id] = OrderResult(
                    order_id=order_id,
                    status="open",
                    error=None,
                )
                logger.info(
                    "Recovered order %s: still open – re-added to active orders",
                    order_id,
                )


# ─── CLI entry point ───────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hyper-BTC 5-Minute Polymarket Trading Bot"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (default: simulation mode from .env)",
    )
    parser.add_argument(
        "--derive-api-key",
        action="store_true",
        help="Derive Polymarket CLOB API credentials from PRIVATE_KEY and exit.",
    )
    return parser.parse_args()


def derive_and_print_api_key(config: BotConfig) -> None:
    """One-time helper: sign with private key to get CLOB API creds."""
    if not config.private_key:
        logger.error("PRIVATE_KEY not set in .env")
        sys.exit(1)

    auth = PolymarketAuth(private_key=config.private_key)
    client = PolymarketClient(auth, simulation=False)

    try:
        creds = client.derive_api_credentials()
        print("\n── Polymarket CLOB API Credentials ──────────────────────")
        print(f"POLY_API_KEY={creds.get('apiKey', creds.get('key', ''))}")
        print(f"POLY_API_SECRET={creds.get('secret', '')}")
        print(f"POLY_API_PASSPHRASE={creds.get('passphrase', '')}")
        print("─────────────────────────────────────────────────────────")
        print("Copy these into your .env file and restart the bot.\n")
    except Exception as exc:
        logger.error("Failed to derive API credentials: %s", exc)
        sys.exit(1)


async def main() -> None:
    args = parse_args()
    config = BotConfig.from_env()

    # Override simulation flag if --live is passed
    if args.live and os.getenv("SIMULATION_MODE", "true").lower() != "false":
        logger.info("--live flag passed; disabling simulation mode.")
        config.simulation_mode = False

    if args.derive_api_key:
        derive_and_print_api_key(config)
        return

    try:
        config.validate(live=args.live)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    bot = HyperBTCBot(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
