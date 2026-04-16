"""
probe_markets.py – One-shot diagnostic tool for Polymarket market discovery.

Run this with real network access (before going live) to:
  • Inspect the exact JSON field names returned by the Gamma API.
  • Verify that _is_btc_5min_market, _parse_end_time, and _extract_tokens
    work correctly against live data.
  • Identify how many BTC 5-min markets are active right now.

Usage:
    python probe_markets.py                  # inspect + filter
    python probe_markets.py --raw            # dump raw JSON of first 3 markets
    python probe_markets.py --limit 50       # fetch only 50 markets
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

from api_client import (
    GAMMA_BASE_URL,
    WINDOW_SECONDS,
    _extract_tokens,
    _is_btc_5min_market,
    _next_window_close,
    _parse_end_time,
)


def probe(limit: int = 200, dump_raw: bool = False) -> None:
    session = requests.Session()
    now = int(time.time())

    # ── Fetch ─────────────────────────────────────────────────────────────────
    print(f"\nFetching up to {limit} active markets from Gamma API…")
    try:
        resp = session.get(
            f"{GAMMA_BASE_URL}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    raw = data if isinstance(data, list) else data.get("markets", [])
    print(f"Received {len(raw)} markets.\n")

    # ── Raw dump ──────────────────────────────────────────────────────────────
    if dump_raw and raw:
        print("── First 3 markets (raw JSON) ──────────────────────────────────")
        for m in raw[:3]:
            print(json.dumps(m, indent=2, default=str))
            print()

    # ── Field inventory ───────────────────────────────────────────────────────
    if raw:
        all_keys = set()
        for m in raw:
            all_keys.update(m.keys())
        print(f"Field names seen across all {len(raw)} markets:")
        for k in sorted(all_keys):
            sample = raw[0].get(k, "—")
            sample_str = str(sample)[:80]
            print(f"  {k:<28}  {sample_str}")
        print()

    # ── Filter pass ───────────────────────────────────────────────────────────
    target_windows = [_next_window_close(now, offset=i) for i in range(4)]
    print(f"Target windows (next 4 × 5-min closes):")
    for w in target_windows:
        print(f"  {w}  →  {datetime.fromtimestamp(w, tz=timezone.utc).isoformat()}")
    print()

    btc_pass, time_pass, token_pass = 0, 0, 0
    accepted = []

    for m in raw:
        if not _is_btc_5min_market(m):
            continue
        btc_pass += 1

        end_ts = _parse_end_time(m)
        if end_ts is None:
            print(f"  [NO END TIME]  {m.get('question', '')[:70]}")
            continue
        time_pass += 1

        tokens = _extract_tokens(m)
        if tokens is None:
            print(f"  [NO TOKENS]    {m.get('question', '')[:70]}")
            print(f"                 clobTokenIds={str(m.get('clobTokenIds','—'))[:60]}")
            print(f"                 tokens={str(m.get('tokens','—'))[:60]}")
            continue
        token_pass += 1
        accepted.append((m, end_ts, tokens))

    # ── Summary ───────────────────────────────────────────────────────────────
    print("── Filter results ──────────────────────────────────────────────")
    print(f"  Passed _is_btc_5min_market : {btc_pass}/{len(raw)}")
    print(f"  Passed _parse_end_time     : {time_pass}/{btc_pass}")
    print(f"  Passed _extract_tokens     : {token_pass}/{time_pass}")
    print(f"  In target windows          : ", end="")

    in_window = [(m, et, tk) for m, et, tk in accepted if et in target_windows]
    print(len(in_window))
    print()

    if in_window:
        print("── Tradeable markets ────────────────────────────────────────────")
        for m, end_ts, tokens in in_window:
            ttc = end_ts - now
            print(f"  Q:   {m.get('question', '')[:70]}")
            print(f"  End: {datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()}  (T-{ttc}s)")
            print(f"  YES: {tokens.yes_token_id[:20]}…")
            print(f"  NO:  {tokens.no_token_id[:20]}…")
            print()
    else:
        print("No tradeable BTC 5-min markets found right now.")
        print("This is normal outside of market-open windows; try again in a few seconds.")


def main() -> None:
    p = argparse.ArgumentParser(description="Probe Polymarket Gamma API market discovery")
    p.add_argument("--limit", type=int, default=200, help="Max markets to fetch (default 200)")
    p.add_argument("--raw", action="store_true", help="Dump raw JSON of first 3 markets")
    args = p.parse_args()
    probe(limit=args.limit, dump_raw=args.raw)


if __name__ == "__main__":
    main()
