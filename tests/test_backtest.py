"""
tests/test_backtest.py – Unit tests for backtest.py pure functions.

All tests are offline (no network calls).  The Backtester is tested with
hand-crafted windows so expected P&L can be verified analytically.
"""

from __future__ import annotations

import pytest
from backtest import (
    Bar,
    Backtester,
    Window,
    _window_end,
    group_into_windows,
)
from strategy import SignalType

# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_bar(ts: int, open_: float, close: float, high: float = None, low: float = None) -> Bar:
    return Bar(
        ts=ts,
        open=open_,
        high=high if high is not None else max(open_, close) + 1,
        low=low if low is not None else min(open_, close) - 1,
        close=close,
        volume=0.0,
    )


def make_window(
    end_ts: int,
    open_price: float,
    signal_price: float,   # bar[3].close
    close_price: float,
) -> Window:
    """
    Build a synthetic 5-bar window.
    Bars 0-2 are flat at open_price.
    Bar 3 closes at signal_price (the T-60s evaluation price).
    Bar 4 closes at close_price (the settlement price).
    """
    start = end_ts - 300
    bars = [
        make_bar(start + 0,   open_price,  open_price),
        make_bar(start + 60,  open_price,  open_price),
        make_bar(start + 120, open_price,  open_price),
        make_bar(start + 180, open_price,  signal_price),  # bar[3]
        make_bar(start + 240, signal_price, close_price),  # bar[4]
    ]
    return Window(end_ts=end_ts, bars=bars)


# ─── _window_end ──────────────────────────────────────────────────────────────

class TestWindowEnd:
    def test_snap_to_next_300_boundary(self):
        assert _window_end(0) == 300
        assert _window_end(299) == 300
        assert _window_end(300) == 600
        assert _window_end(599) == 600
        assert _window_end(600) == 900

    def test_result_always_divisible_by_300(self):
        for ts in [1, 150, 299, 300, 301, 1_000_000, 1_700_000_001]:
            assert _window_end(ts) % 300 == 0


# ─── group_into_windows ───────────────────────────────────────────────────────

class TestGroupIntoWindows:

    def _make_bars(self, start_ts: int, count: int, price: float = 65_000.0) -> list[Bar]:
        return [make_bar(start_ts + i * 60, price, price) for i in range(count)]

    def test_complete_window_accepted(self):
        bars = self._make_bars(start_ts=300, count=5)
        windows = group_into_windows(bars)
        assert len(windows) == 1
        assert windows[0].end_ts == 600

    def test_incomplete_window_rejected(self):
        bars = self._make_bars(start_ts=300, count=4)   # only 4 bars
        windows = group_into_windows(bars)
        assert len(windows) == 0

    def test_gap_within_window_rejected(self):
        bars = self._make_bars(start_ts=300, count=5)
        bars[2] = make_bar(ts=bars[2].ts + 30, open_=65_000.0, close=65_000.0)  # shift by 30s
        windows = group_into_windows(bars)
        assert len(windows) == 0

    def test_two_consecutive_windows(self):
        bars = (
            self._make_bars(start_ts=300, count=5) +
            self._make_bars(start_ts=600, count=5)
        )
        windows = group_into_windows(bars)
        assert len(windows) == 2
        assert windows[0].end_ts == 600
        assert windows[1].end_ts == 900

    def test_open_price_is_first_bar_open(self):
        bars = self._make_bars(start_ts=300, count=5, price=65_000.0)
        bars[0] = make_bar(300, 65_123.0, 65_000.0)
        windows = group_into_windows(bars)
        assert windows[0].open_price == 65_123.0

    def test_close_price_is_last_bar_close(self):
        bars = self._make_bars(start_ts=300, count=5, price=65_000.0)
        bars[-1] = make_bar(540, 65_000.0, 66_000.0)
        windows = group_into_windows(bars)
        assert windows[0].close_price == 66_000.0

    def test_signal_price_is_bar3_close(self):
        bars = self._make_bars(start_ts=300, count=5, price=65_000.0)
        bars[3] = make_bar(480, 65_000.0, 65_200.0)   # bar[3] closes at 65_200
        windows = group_into_windows(bars)
        assert windows[0].signal_price == 65_200.0


# ─── Window dataclass properties ──────────────────────────────────────────────

class TestWindowProperties:

    def test_open_is_first_open(self):
        w = make_window(end_ts=600, open_price=65_000.0, signal_price=65_100.0, close_price=65_200.0)
        assert w.open_price == 65_000.0

    def test_signal_is_bar3_close(self):
        w = make_window(end_ts=600, open_price=65_000.0, signal_price=65_100.0, close_price=65_200.0)
        assert w.signal_price == 65_100.0

    def test_close_is_last_close(self):
        w = make_window(end_ts=600, open_price=65_000.0, signal_price=65_100.0, close_price=65_200.0)
        assert w.close_price == 65_200.0


# ─── Backtester ───────────────────────────────────────────────────────────────

BASE_TS = 1_700_000_000 + 300   # any timestamp divisible by 300


class TestBacktester:

    def _bt(
        self,
        threshold=0.08,
        kelly=0.25,
        max_dd=0.50,    # high so circuit-breaker doesn't interfere
        bankroll=1000.0,
        mid=0.50,
    ) -> Backtester:
        return Backtester(
            signal_threshold_pct=threshold,
            kelly_fraction=kelly,
            max_position_usdc=10_000.0,
            max_drawdown=max_dd,
            starting_bankroll=bankroll,
            market_mid=mid,
        )

    # ── No signal ─────────────────────────────────────────────────────────────

    def test_flat_window_no_signal_no_trade(self):
        """delta=0 → no signal → no trade, bankroll unchanged."""
        w = make_window(BASE_TS, open_price=65_000.0, signal_price=65_000.0, close_price=65_000.0)
        bt = self._bt()
        r = bt.run([w], "2024-01-01", "2024-01-02", 1)
        assert r.signal_windows == 0
        assert len(r.trades) == 0
        assert r.final_bankroll == pytest.approx(1000.0)

    def test_below_threshold_no_signal(self):
        """delta=0.05% < 0.08% → no signal."""
        signal_price = 65_000.0 * (1 + 0.05 / 100)
        w = make_window(BASE_TS, 65_000.0, signal_price, 65_100.0)
        r = self._bt().run([w], "2024-01-01", "2024-01-02", 1)
        assert r.signal_windows == 0

    # ── Signal fires: UP ──────────────────────────────────────────────────────

    def test_up_signal_win_increases_bankroll(self):
        """
        delta=+0.15% (above threshold) + price closes higher → WIN.
        entry_mid=0.50: stake/0.50 payout → pnl = stake
        """
        signal_price = 65_000.0 * (1 + 0.15 / 100)
        close_price = signal_price + 100   # price went up → UP signal wins
        w = make_window(BASE_TS, 65_000.0, signal_price, close_price)
        r = self._bt().run([w], "2024-01-01", "2024-01-02", 1)
        assert r.signal_windows == 1
        assert r.win_count == 1
        assert r.loss_count == 0
        assert r.final_bankroll > 1000.0

    def test_up_signal_loss_decreases_bankroll(self):
        """delta=+0.15% but price closes lower → LOSS."""
        signal_price = 65_000.0 * (1 + 0.15 / 100)
        close_price = 65_000.0 - 100   # price went down → UP signal loses
        w = make_window(BASE_TS, 65_000.0, signal_price, close_price)
        r = self._bt().run([w], "2024-01-01", "2024-01-02", 1)
        assert r.win_count == 0
        assert r.loss_count == 1
        assert r.final_bankroll < 1000.0

    # ── Signal fires: DOWN ────────────────────────────────────────────────────

    def test_down_signal_win_when_price_falls(self):
        """delta=-0.15% + price closes lower → WIN."""
        signal_price = 65_000.0 * (1 - 0.15 / 100)
        close_price = signal_price - 100  # price fell further → DOWN wins
        w = make_window(BASE_TS, 65_000.0, signal_price, close_price)
        r = self._bt().run([w], "2024-01-01", "2024-01-02", 1)
        assert r.win_count == 1

    def test_down_signal_loss_when_price_rises(self):
        signal_price = 65_000.0 * (1 - 0.15 / 100)
        close_price = 65_000.0 + 100   # price reversed → DOWN loses
        w = make_window(BASE_TS, 65_000.0, signal_price, close_price)
        r = self._bt().run([w], "2024-01-01", "2024-01-02", 1)
        assert r.loss_count == 1

    # ── P&L arithmetic ────────────────────────────────────────────────────────

    def test_win_pnl_equals_stake_at_mid_50(self):
        """
        mid=0.50, size=S → payout = S/0.50 = 2S → pnl = 2S - S = S
        So final_bankroll = starting + S
        """
        signal_price = 65_000.0 * (1 + 0.15 / 100)
        close_price = signal_price + 100
        w = make_window(BASE_TS, 65_000.0, signal_price, close_price)

        bt = self._bt(bankroll=1000.0, mid=0.50, kelly=1.0)
        r = bt.run([w], "2024-01-01", "2024-01-02", 1)

        stake = r.trades[0].entry_usdc
        assert r.trades[0].pnl_usdc == pytest.approx(stake, rel=1e-6)
        assert r.final_bankroll == pytest.approx(1000.0 + stake, rel=1e-6)

    def test_loss_pnl_equals_negative_stake(self):
        signal_price = 65_000.0 * (1 + 0.15 / 100)
        close_price = 65_000.0 - 100
        w = make_window(BASE_TS, 65_000.0, signal_price, close_price)

        bt = self._bt(bankroll=1000.0, mid=0.50)
        r = bt.run([w], "2024-01-01", "2024-01-02", 1)

        stake = r.trades[0].entry_usdc
        assert r.trades[0].pnl_usdc == pytest.approx(-stake, rel=1e-6)

    # ── Compounding / multi-window ────────────────────────────────────────────

    def test_two_wins_compound(self):
        """Two consecutive wins should grow the bankroll geometrically."""
        signal_price = 65_000.0 * (1 + 0.15 / 100)
        close_up = signal_price + 100
        w1 = make_window(BASE_TS, 65_000.0, signal_price, close_up)
        w2 = make_window(BASE_TS + 300, 65_000.0, signal_price, close_up)

        r = self._bt(bankroll=1000.0).run([w1, w2], "2024-01-01", "2024-01-02", 1)
        assert r.win_count == 2
        # Second stake is larger than first because bankroll grew
        assert r.trades[1].entry_usdc > r.trades[0].entry_usdc

    def test_equity_curve_length_matches_windows(self):
        """equity_curve should have one entry per window + the initial value."""
        windows = [
            make_window(BASE_TS + i * 300, 65_000.0, 65_000.0, 65_000.0)
            for i in range(5)
        ]
        r = self._bt().run(windows, "2024-01-01", "2024-01-02", 1)
        # One initial entry + one per window
        assert len(r.equity_curve) == len(windows) + 1

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def test_circuit_breaker_halts_after_big_loss(self):
        """
        With max_dd=0.05 (5%) and full Kelly, one loss should trip the guard.

        delta=0.14% → confidence = 0.14/(2*0.08) = 0.875 < 1.0 (KellySizer accepts it).
        b=1.0 (mid=0.50), full_kelly = (1.0*0.875 - 0.125)/1.0 = 0.75
        stake = 1000 * 0.75 = $750 → loss → bankroll = $250 → drawdown=75% >> 5%
        """
        signal_price = 65_000.0 * (1 + 0.14 / 100)   # confidence=0.875 < 1.0
        close_loss = 65_000.0 - 500                    # price falls → UP signal loses
        windows = [
            make_window(BASE_TS + i * 300, 65_000.0, signal_price, close_loss)
            for i in range(20)
        ]
        bt = self._bt(bankroll=1000.0, max_dd=0.05, kelly=1.0)
        r = bt.run(windows, "2024-01-01", "2024-01-02", 1)
        assert r.circuit_breaker_hit
        assert r.total_windows < 20   # stopped early

    # ── Signal rate ───────────────────────────────────────────────────────────

    def test_signal_rate_calculation(self):
        above = make_window(BASE_TS,       65_000.0, 65_000.0 * 1.0015, 65_100.0)
        flat1 = make_window(BASE_TS + 300, 65_000.0, 65_000.0,          65_000.0)
        flat2 = make_window(BASE_TS + 600, 65_000.0, 65_000.0,          65_000.0)
        r = self._bt().run([above, flat1, flat2], "2024-01-01", "2024-01-02", 1)
        assert r.total_windows == 3
        assert r.signal_windows == 1
        assert r.signal_rate == pytest.approx(1 / 3, rel=1e-6)

    # ── ROI helper ────────────────────────────────────────────────────────────

    def test_roi_pct_zero_when_no_trades(self):
        w = make_window(BASE_TS, 65_000.0, 65_000.0, 65_000.0)
        r = self._bt().run([w], "2024-01-01", "2024-01-02", 1)
        assert r.roi_pct == pytest.approx(0.0)
