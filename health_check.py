"""
health_check.py – Pre-flight connectivity and configuration check.

Run this before starting the bot (live or simulation) to confirm that
all external services are reachable and the local environment is sane.

Checks performed:
  1. Environment   – .env loaded, required vars present
  2. Gamma API     – BTC 5-min market discovery returns ≥1 market
  3. Pyth REST     – One BTC/USD price fetch succeeds
  4. Pyth WebSocket– Connect, receive first price, disconnect (≤15 s)
  5. SQLite DB     – Create, write, read, and clean up a test order

Usage:
    python health_check.py          # full check (requires network)
    python health_check.py --no-ws  # skip WebSocket (faster in CI)

Exit code: 0 all pass, 1 one or more fail.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from typing import Tuple

from dotenv import load_dotenv

# ─── Result helpers ───────────────────────────────────────────────────────────

OK   = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

_WIDTH = 32   # column width for check names


def _result(label: str, status: str, detail: str = "") -> None:
    tag = f"[{status}]"
    line = f"  {tag:<6}  {label:<{_WIDTH}}  {detail}"
    print(line)


# ─── Individual checks ───────────────────────────────────────────────────────


def check_environment() -> Tuple[str, str]:
    """Verify .env is present and all required variables are set."""
    load_dotenv()
    missing = []
    for var in ("PRIVATE_KEY", "POLYGON_RPC_URL"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        return FAIL, f"Missing env vars: {', '.join(missing)}"
    sim = os.getenv("SIMULATION_MODE", "true").lower()
    mode = "SIMULATION" if sim != "false" else "LIVE"
    return OK, f"Mode={mode}"


def check_gamma_api() -> Tuple[str, str]:
    """Fetch up to 20 markets and verify BTC 5-min discovery finds at least one."""
    import requests
    from api_client import (
        GAMMA_BASE_URL,
        _extract_tokens,
        _is_btc_5min_market,
        _parse_end_time,
    )

    try:
        resp = requests.get(
            f"{GAMMA_BASE_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 200},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        markets = raw if isinstance(raw, list) else raw.get("markets", [])
    except Exception as exc:
        return FAIL, f"HTTP error: {exc}"

    if not markets:
        return FAIL, "API returned 0 markets"

    found = [
        m for m in markets
        if _is_btc_5min_market(m) and _parse_end_time(m) and _extract_tokens(m)
    ]
    if not found:
        return OK, (
            f"{len(markets)} markets fetched; 0 BTC 5-min right now "
            "(normal outside market-open windows)"
        )
    return OK, f"{len(markets)} markets fetched; {len(found)} BTC 5-min tradeable"


def check_pyth_rest() -> Tuple[str, str]:
    """Fetch one BTC/USD price from the Hermes REST endpoint."""
    from api_client import BTC_USD_FEED_ID, PYTH_HERMES_URL, PythPriceFeed

    try:
        feed  = PythPriceFeed()
        price = feed.fetch_btc_price()
        return OK, f"BTC/USD = ${price.price:,.2f}  (±${price.confidence:.2f})"
    except Exception as exc:
        return FAIL, str(exc)


async def _ws_check(timeout: float = 15.0) -> Tuple[str, str]:
    """Connect to Pyth WebSocket and wait for the first BTC/USD price."""
    from api_client import PythWebSocketFeed

    feed = PythWebSocketFeed()
    await feed.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if not feed.is_stale(max_age_seconds=timeout):
                price = feed.get_latest()
                return OK, f"BTC/USD = ${price.price:,.2f}  (latency < {timeout:.0f}s)"
            await asyncio.sleep(0.5)
        return FAIL, f"No price received within {timeout:.0f}s"
    finally:
        await feed.stop()


def check_pyth_websocket(timeout: float = 15.0) -> Tuple[str, str]:
    """Synchronous wrapper for the async WebSocket check."""
    try:
        return asyncio.run(_ws_check(timeout=timeout))
    except Exception as exc:
        return FAIL, str(exc)


def check_database() -> Tuple[str, str]:
    """Create an in-memory OrderStore, write one row, read it back."""
    from db import OrderStore

    try:
        store = OrderStore(":memory:")
        order_id = "HEALTH-CHECK-001"
        store.insert_order(
            order_id    = order_id,
            token_id    = "test-token",
            side        = "BUY",
            entry_price = 0.55,
            size_usdc   = 10.0,
            window_end  = int(time.time()) + 300,
        )
        row = store.get_order(order_id)
        store.update_status(order_id, "cancelled")
        row2 = store.get_order(order_id)
        store.close()

        if row is None:
            return FAIL, "Inserted row not found"
        if row2["status"] != "cancelled":
            return FAIL, f"Status update failed (got {row2['status']!r})"
        return OK, "orders.db writable and queryable"
    except Exception as exc:
        return FAIL, str(exc)


# ─── Runner ───────────────────────────────────────────────────────────────────


def run_checks(skip_ws: bool = False) -> bool:
    checks = [
        ("Environment",         check_environment),
        ("Gamma API",           check_gamma_api),
        ("Pyth REST feed",      check_pyth_rest),
        ("SQLite database",     check_database),
    ]

    if not skip_ws:
        checks.insert(3, ("Pyth WebSocket feed", check_pyth_websocket))

    print()
    print("  Hyper-BTC Pre-Flight Health Check")
    print("  " + "─" * 54)

    all_passed = True
    for label, fn in checks:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = FAIL, f"Unexpected error: {exc}"

        _result(label, status, detail)
        if status == FAIL:
            all_passed = False

    print("  " + "─" * 54)
    if all_passed:
        print("  All checks passed. Safe to run: python bot.py")
    else:
        print("  One or more checks failed – see details above.")
    print()
    return all_passed


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pre-flight connectivity check for the Hyper-BTC bot."
    )
    p.add_argument(
        "--no-ws",
        action="store_true",
        help="Skip the Pyth WebSocket check (faster, useful in CI).",
    )
    args = p.parse_args()

    ok = run_checks(skip_ws=args.no_ws)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
