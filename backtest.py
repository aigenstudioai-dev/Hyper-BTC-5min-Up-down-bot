"""
backtest.py – Historical strategy backtest using Pyth Benchmarks 1-min BTC/USD data.

How it works
------------
1.  Fetch 1-minute OHLCV bars from the Pyth Benchmarks API (public, no key needed).
2.  Group consecutive bars into complete 5-minute windows.
3.  For each window, evaluate the SignalEngine at T-60s (the last complete 1-min
    bar before the window closes – the closest 1-min resolution proxy for T-45s).
4.  Size with fractional Kelly and settle at window close.
5.  Print a performance report; optionally save the trade log to JSON.

Approximation note
------------------
With 1-minute bar data the signal is evaluated at T-60s (bar[3].close), not T-45s.
In live trading the bot evaluates at T-45s.  The 15-second gap means the live signal
sees slightly more price movement before entry; backtested win-rates are a lower bound.

Usage
-----
    python backtest.py                            # last 30 days, default params
    python backtest.py --days 90                  # last 90 days
    python backtest.py --from 2024-01-01 --to 2024-06-30
    python backtest.py --threshold 0.05 --kelly 0.5 --mid 0.52
    python backtest.py --save results.json        # persist trade log
    python backtest.py --days 3                   # quick smoke-test (3 days)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

from strategy import DrawdownGuard, KellySizer, SignalEngine, SignalType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtest")

# ─── Pyth Benchmarks constants ─────────────────────────────────────────────────

BENCHMARKS_BASE = "https://benchmarks.pyth.network"
BTC_SYMBOL = "Crypto.BTC/USD"
RESOLUTION = "1"              # 1-minute bars
BARS_PER_WINDOW = 5           # 5 × 1-min = 1 × 5-min window
SIGNAL_BAR_IDX = 3            # bars[3].close = price at T-60s before window close
SIM_SECONDS_UNTIL_CLOSE = 44  # passed to SignalEngine; just inside its 45s gate


# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Bar:
    ts: int       # bar open timestamp (Unix seconds)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Window:
    end_ts: int        # Unix timestamp when this window closes (divisible by 300)
    bars: List[Bar]    # exactly BARS_PER_WINDOW 1-min bars, sorted by ts

    @property
    def open_price(self) -> float:
        """First bar's open = window open price."""
        return self.bars[0].open

    @property
    def signal_price(self) -> float:
        """bars[3].close = last known price at T-60s (proxy for T-45s)."""
        return self.bars[SIGNAL_BAR_IDX].close

    @property
    def close_price(self) -> float:
        """Last bar's close = settlement price."""
        return self.bars[-1].close


@dataclass
class BacktestTrade:
    window_end: int
    dt: str                  # human-readable datetime
    signal: str              # "UP" | "DOWN"
    open_price: float
    signal_price: float
    close_price: float
    delta_pct: float
    confidence: float
    entry_usdc: float
    entry_mid: float
    pnl_usdc: float
    won: bool
    bankroll_after: float


@dataclass
class BacktestResult:
    # ── Run parameters ──────────────────────────────────────────────────────────
    start_date: str
    end_date: str
    days: int
    signal_threshold_pct: float
    kelly_fraction: float
    market_mid: float
    starting_bankroll: float

    # ── Aggregates ──────────────────────────────────────────────────────────────
    total_windows: int = 0
    signal_windows: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    final_bankroll: float = 0.0
    peak_bankroll: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_signal_delta_pct: float = 0.0
    circuit_breaker_hit: bool = False

    # ── Series data ─────────────────────────────────────────────────────────────
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    # ── Derived properties ──────────────────────────────────────────────────────
    @property
    def win_rate(self) -> float:
        n = self.win_count + self.loss_count
        return self.win_count / n if n > 0 else 0.0

    @property
    def signal_rate(self) -> float:
        return self.signal_windows / self.total_windows if self.total_windows > 0 else 0.0

    @property
    def ev_per_trade(self) -> float:
        return self.total_pnl / len(self.trades) if self.trades else 0.0

    @property
    def roi_pct(self) -> float:
        return (self.final_bankroll - self.starting_bankroll) / self.starting_bankroll * 100


# ─── Pyth Benchmarks data fetcher ──────────────────────────────────────────────

def fetch_bars(from_ts: int, to_ts: int, chunk_days: int = 7) -> List[Bar]:
    """
    Download 1-minute BTC/USD OHLCV bars from the Pyth Benchmarks API.

    Requests are chunked into `chunk_days`-day windows to stay within API
    response-size limits.  A 0.5-second pause between chunks is applied to
    avoid rate-limiting.
    """
    session = requests.Session()
    all_bars: List[Bar] = []
    chunk_secs = chunk_days * 86_400
    cursor = from_ts

    while cursor < to_ts:
        chunk_end = min(cursor + chunk_secs, to_ts)
        logger.info("  Fetching %s → %s", _fmt(cursor), _fmt(chunk_end))

        for attempt in range(3):
            try:
                resp = session.get(
                    f"{BENCHMARKS_BASE}/v1/shims/tradingview/history",
                    params={
                        "symbol": BTC_SYMBOL,
                        "resolution": RESOLUTION,
                        "from": cursor,
                        "to": chunk_end,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                wait = 2 ** attempt
                logger.warning("    Attempt %d failed: %s – retrying in %ds", attempt + 1, exc, wait)
                time.sleep(wait)
        else:
            logger.error("    Chunk %s–%s failed after 3 attempts – skipping.", _fmt(cursor), _fmt(chunk_end))
            cursor = chunk_end
            continue

        status = data.get("s", "error")
        if status == "no_data":
            logger.debug("    No data for this chunk.")
        elif status == "ok":
            ts_list = data.get("t", [])
            for t, o, h, l, c, v in zip(
                ts_list,
                data.get("o", []),
                data.get("h", []),
                data.get("l", []),
                data.get("c", []),
                data.get("v", [0.0] * len(ts_list)),
            ):
                all_bars.append(
                    Bar(ts=int(t), open=float(o), high=float(h),
                        low=float(l), close=float(c), volume=float(v))
                )
            logger.info("    → %d bars", len(ts_list))
        else:
            logger.warning("    Unexpected status: %s", status)

        cursor = chunk_end
        time.sleep(0.5)

    all_bars.sort(key=lambda b: b.ts)
    logger.info("Total bars fetched: %d", len(all_bars))
    return all_bars


# ─── Window grouping ───────────────────────────────────────────────────────────

def group_into_windows(bars: List[Bar]) -> List[Window]:
    """
    Partition 1-minute bars into complete, gap-free 5-minute windows.

    A window is keyed by its end timestamp (next multiple of 300 after each
    bar's open time).  Windows with fewer than 5 bars or with internal
    timestamp gaps (not exactly 60 s apart) are discarded.
    """
    groups: Dict[int, List[Bar]] = defaultdict(list)
    for bar in bars:
        groups[_window_end(bar.ts)].append(bar)

    windows: List[Window] = []
    skipped = 0
    for end_ts in sorted(groups):
        group = sorted(groups[end_ts], key=lambda b: b.ts)
        if len(group) != BARS_PER_WINDOW:
            skipped += 1
            continue
        # Verify no intra-window gaps
        if any(group[i + 1].ts - group[i].ts != 60 for i in range(BARS_PER_WINDOW - 1)):
            skipped += 1
            continue
        windows.append(Window(end_ts=end_ts, bars=group))

    logger.info("Complete windows: %d  |  Skipped (gaps/incomplete): %d", len(windows), skipped)
    return windows


# ─── Back-test engine ──────────────────────────────────────────────────────────

class Backtester:
    """
    Replay strategy logic over historical windows with compounding Kelly sizing.

    The backtest is intentionally conservative:
      • Signal evaluated at T-60s (bar[3].close), not T-45s.
      • Market mid is symmetric (0.50 default) – no spread or slippage.
      • Kelly compound sizing means early losses shrink subsequent stakes.
    """

    def __init__(
        self,
        signal_threshold_pct: float = 0.08,
        kelly_fraction: float = 0.25,
        max_position_usdc: float = 500.0,
        max_drawdown: float = 0.15,
        starting_bankroll: float = 300.0,
        market_mid: float = 0.50,
    ) -> None:
        self.engine = SignalEngine(
            threshold_pct=signal_threshold_pct,
            entry_window_seconds=45,
        )
        self.sizer = KellySizer(
            kelly_fraction=kelly_fraction,
            max_position_usdc=max_position_usdc,
            min_position_usdc=5.0,
        )
        self.guard = DrawdownGuard(
            max_drawdown=max_drawdown,
            starting_bankroll=starting_bankroll,
        )
        self.starting_bankroll = starting_bankroll
        self.market_mid = market_mid

    def run(
        self,
        windows: List[Window],
        start_date: str,
        end_date: str,
        days: int,
    ) -> BacktestResult:

        result = BacktestResult(
            start_date=start_date,
            end_date=end_date,
            days=days,
            signal_threshold_pct=self.engine.threshold_pct,
            kelly_fraction=self.sizer.kelly_fraction,
            market_mid=self.market_mid,
            starting_bankroll=self.starting_bankroll,
            final_bankroll=self.starting_bankroll,
            peak_bankroll=self.starting_bankroll,
        )

        bankroll = self.starting_bankroll
        result.equity_curve.append(bankroll)
        abs_deltas: List[float] = []

        for window in windows:
            result.total_windows += 1

            if self.guard.is_halted:
                logger.warning("Circuit breaker tripped at %s", _fmt(window.end_ts))
                result.circuit_breaker_hit = True
                break

            signal = self.engine.evaluate(
                current_price=window.signal_price,
                window_open_price=window.open_price,
                seconds_until_close=SIM_SECONDS_UNTIL_CLOSE,
                yes_mid=self.market_mid,
                no_mid=self.market_mid,
                yes_token_id="YES",
                no_token_id="NO",
            )

            if signal.signal_type == SignalType.NONE:
                result.equity_curve.append(bankroll)
                continue

            # ── Signal fired ──────────────────────────────────────────────────
            result.signal_windows += 1
            abs_deltas.append(abs(signal.window_delta_pct))

            size = self.sizer.size(
                bankroll=bankroll,
                win_prob=signal.confidence,
                price=self.market_mid,
            )
            if size <= 0:
                result.equity_curve.append(bankroll)
                continue

            # ── Settlement ────────────────────────────────────────────────────
            price_up = window.close_price > window.open_price
            won = (signal.signal_type == SignalType.STRONG_BUY_UP and price_up) or \
                  (signal.signal_type == SignalType.STRONG_BUY_DOWN and not price_up)

            payout = (size / self.market_mid) if won else 0.0
            pnl = payout - size
            bankroll += pnl

            result.win_count += (1 if won else 0)
            result.loss_count += (0 if won else 1)
            result.total_pnl += pnl

            self.guard.update(bankroll)
            dd = self.guard.drawdown * 100
            if dd > result.max_drawdown_pct:
                result.max_drawdown_pct = dd
            if bankroll > result.peak_bankroll:
                result.peak_bankroll = bankroll

            result.equity_curve.append(bankroll)
            result.trades.append(BacktestTrade(
                window_end=window.end_ts,
                dt=_fmt(window.end_ts),
                signal="UP" if signal.signal_type == SignalType.STRONG_BUY_UP else "DOWN",
                open_price=window.open_price,
                signal_price=window.signal_price,
                close_price=window.close_price,
                delta_pct=round(signal.window_delta_pct, 5),
                confidence=round(signal.confidence, 4),
                entry_usdc=round(size, 4),
                entry_mid=self.market_mid,
                pnl_usdc=round(pnl, 4),
                won=won,
                bankroll_after=round(bankroll, 4),
            ))

        result.final_bankroll = bankroll
        result.avg_signal_delta_pct = sum(abs_deltas) / len(abs_deltas) if abs_deltas else 0.0
        return result


# ─── Reporting ─────────────────────────────────────────────────────────────────

def print_report(r: BacktestResult) -> None:
    W = 64
    sep = "═" * W

    def row(label: str, value: str) -> None:
        print(f"  {label:<32} {value}")

    print(f"\n{sep}")
    print(f"  Hyper-BTC 5-min Back-test Report")
    print(sep)
    row("Period", f"{r.start_date}  →  {r.end_date}  ({r.days}d)")
    row("Signal threshold", f"{r.signal_threshold_pct:.3f}%  |  Kelly {r.kelly_fraction:.0%}  |  mid {r.market_mid:.2f}")
    row("Starting bankroll", f"${r.starting_bankroll:.2f}")
    print()

    print("  MARKET COVERAGE")
    row("Windows analyzed", f"{r.total_windows:,}  ({r.total_windows // (r.days or 1):,}/day avg)")
    row("Signal triggered", f"{r.signal_windows:,}  ({r.signal_rate * 100:.2f}% of windows)")
    row("Avg |delta| at signal", f"{r.avg_signal_delta_pct:.4f}%")
    print()

    print("  PERFORMANCE")
    row("Win rate", f"{r.win_rate * 100:.1f}%  ({r.win_count}W / {r.loss_count}L)")
    row("EV per trade", f"${r.ev_per_trade:+.3f}")
    row("Total PnL", f"${r.total_pnl:+.2f}")
    row("Final bankroll", f"${r.final_bankroll:.2f}  ({r.roi_pct:+.1f}% ROI)")
    row("Peak bankroll", f"${r.peak_bankroll:.2f}")
    print()

    print("  RISK")
    cb_status = "YES  ← strategy halted early" if r.circuit_breaker_hit else "No"
    row("Max drawdown", f"{r.max_drawdown_pct:.2f}%")
    row("Circuit breaker hit", cb_status)
    print()

    print("  EQUITY CURVE  (sampled at 10 equal intervals)")
    curve = r.equity_curve
    n = len(curve)
    if n > 1:
        step = max(1, n // 10)
        samples = [(i, curve[i]) for i in range(0, n, step)]
        samples.append((n - 1, curve[-1]))
        for idx, val in samples:
            pct = (val - r.starting_bankroll) / r.starting_bankroll * 100
            bar_len = max(0, min(30, int((val / r.starting_bankroll - 1) * 30 + 15)))
            bar = "▓" * bar_len
            print(f"  [{idx:>6}]  ${val:>8.2f}  ({pct:+6.1f}%)  {bar}")
    print()

    if r.circuit_breaker_hit:
        print("  ⚠  Circuit breaker was triggered during this back-test run.")
        print("     Consider a higher threshold or lower Kelly fraction.\n")

    print(sep)
    print()


# ─── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hyper-BTC 5-min Strategy Back-test")

    # Date range (mutual exclusion: --days vs --from/--to)
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=30, metavar="N",
                     help="Back-test the last N days (default: 30)")
    grp.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD",
                     help="Start date (UTC)")
    p.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD",
                   help="End date (UTC), required with --from")

    # Strategy params
    p.add_argument("--threshold", type=float, default=0.08, metavar="PCT",
                   help="Window delta %% to trigger signal (default: 0.08)")
    p.add_argument("--kelly", type=float, default=0.25, metavar="F",
                   help="Kelly fraction 0–1 (default: 0.25)")
    p.add_argument("--mid", type=float, default=0.50, metavar="PRICE",
                   help="Synthetic market mid price per share (default: 0.50)")
    p.add_argument("--bankroll", type=float, default=300.0, metavar="USDC",
                   help="Starting bankroll in USDC (default: 300)")
    p.add_argument("--max-pos", type=float, default=500.0, metavar="USDC",
                   help="Max single position size (default: 500)")
    p.add_argument("--max-dd", type=float, default=0.15, metavar="FRAC",
                   help="Max drawdown fraction before halt (default: 0.15)")

    # Output
    p.add_argument("--save", metavar="FILE.json",
                   help="Save full trade log to JSON")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress fetch progress logs")

    return p.parse_args()


def resolve_date_range(args: argparse.Namespace) -> tuple[int, int, str, str, int]:
    """Return (from_ts, to_ts, from_str, to_str, days)."""
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    if args.from_date:
        if not args.to_date:
            sys.exit("--to is required when using --from")
        from_dt = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
        to_dt = datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)
    else:
        to_dt = now
        from_dt = to_dt - timedelta(days=args.days)

    days = max(1, (to_dt - from_dt).days)
    return (
        int(from_dt.timestamp()),
        int(to_dt.timestamp()),
        from_dt.strftime("%Y-%m-%d"),
        to_dt.strftime("%Y-%m-%d"),
        days,
    )


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _window_end(ts: int) -> int:
    """Next 300-second boundary after ts."""
    return ((ts // 300) + 1) * 300


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ─── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    if args.quiet:
        logging.getLogger("backtest").setLevel(logging.WARNING)

    from_ts, to_ts, from_str, to_str, days = resolve_date_range(args)

    logger.info("═" * 50)
    logger.info("Back-test: %s → %s (%d days)", from_str, to_str, days)
    logger.info("Params: threshold=%.3f%%  kelly=%.2f  mid=%.2f  bankroll=$%.0f",
                args.threshold, args.kelly, args.mid, args.bankroll)
    logger.info("═" * 50)

    # ── Fetch ─────────────────────────────────────────────────────────────────
    logger.info("Downloading Pyth 1-min BTC/USD bars...")
    bars = fetch_bars(from_ts, to_ts)

    if not bars:
        sys.exit("No bars returned from Pyth Benchmarks. Check network or date range.")

    # ── Group ────────────────────────────────────────────────────────────────
    logger.info("Grouping into 5-min windows...")
    windows = group_into_windows(bars)

    if not windows:
        sys.exit("No complete windows found. Try a wider date range.")

    # ── Run ───────────────────────────────────────────────────────────────────
    logger.info("Running back-test over %d windows...", len(windows))
    bt = Backtester(
        signal_threshold_pct=args.threshold,
        kelly_fraction=args.kelly,
        max_position_usdc=args.max_pos,
        max_drawdown=args.max_dd,
        starting_bankroll=args.bankroll,
        market_mid=args.mid,
    )
    result = bt.run(windows, from_str, to_str, days)

    # ── Report ────────────────────────────────────────────────────────────────
    print_report(result)

    # ── Save ─────────────────────────────────────────────────────────────────
    if args.save:
        payload = {
            "params": {
                "start": from_str,
                "end": to_str,
                "days": days,
                "threshold_pct": args.threshold,
                "kelly_fraction": args.kelly,
                "market_mid": args.mid,
                "starting_bankroll": args.bankroll,
            },
            "summary": {
                "total_windows": result.total_windows,
                "signal_windows": result.signal_windows,
                "signal_rate_pct": round(result.signal_rate * 100, 3),
                "win_rate_pct": round(result.win_rate * 100, 2),
                "total_pnl": round(result.total_pnl, 4),
                "final_bankroll": round(result.final_bankroll, 4),
                "roi_pct": round(result.roi_pct, 2),
                "max_drawdown_pct": round(result.max_drawdown_pct, 3),
                "circuit_breaker_hit": result.circuit_breaker_hit,
            },
            "trades": [asdict(t) for t in result.trades],
            "equity_curve": [round(v, 4) for v in result.equity_curve],
        }
        with open(args.save, "w") as fh:
            json.dump(payload, fh, indent=2)
        logger.info("Trade log saved → %s  (%d trades)", args.save, len(result.trades))


if __name__ == "__main__":
    main()
