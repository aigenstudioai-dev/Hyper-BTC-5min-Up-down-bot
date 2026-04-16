"""
tests/test_strategy.py – Unit tests for strategy.py

Covers every class and every meaningful branch with no network calls.
Run with:  pytest -v tests/
"""

from __future__ import annotations

import time
import pytest

from strategy import (
    DrawdownGuard,
    KellySizer,
    SignalEngine,
    SignalType,
    Signal,
    SimulationLedger,
    TradeRecord,
    WindowTracker,
)

# ─── Shared helpers ────────────────────────────────────────────────────────────

# A stable timestamp well away from epoch-0 so `ts or time.time()` never
# falls back to the real clock in WindowTracker.record_tick.
BASE_TS = 1_700_000_000   # Nov 2023 – arbitrary but fixed


def make_signal(
    signal_type: SignalType = SignalType.STRONG_BUY_UP,
    delta: float = 0.10,
    current: float = 65_065.0,
    window_open: float = 65_000.0,
    confidence: float = 0.625,
    token: str = "yes_token_abc123",
    price: float = 0.60,
) -> Signal:
    return Signal(
        signal_type=signal_type,
        window_delta_pct=delta,
        current_price=current,
        window_open_price=window_open,
        timestamp=time.time(),
        confidence=confidence,
        recommended_token=token,
        recommended_price=price,
    )


def make_trade(
    trade_id: str = "ORDER-001",
    signal_type: SignalType = SignalType.STRONG_BUY_UP,
    entry_usdc: float = 50.0,
    entry_price: float = 0.60,
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        signal=make_signal(signal_type=signal_type, price=entry_price),
        entry_usdc=entry_usdc,
        entry_price=entry_price,
    )


# ─── WindowTracker ─────────────────────────────────────────────────────────────

class TestWindowTracker:

    # ── _window_end boundary arithmetic ───────────────────────────────────────

    def test_window_end_is_always_divisible_by_300(self):
        for ts in [1, 149, 299, 300, 301, 599, 600, BASE_TS]:
            assert WindowTracker._window_end(ts) % 300 == 0

    def test_window_end_snaps_to_next_boundary(self):
        assert WindowTracker._window_end(0) == 300
        assert WindowTracker._window_end(1) == 300
        assert WindowTracker._window_end(299) == 300
        assert WindowTracker._window_end(300) == 600   # 300 itself starts a new window
        assert WindowTracker._window_end(301) == 600

    # ── record_tick ───────────────────────────────────────────────────────────

    def test_first_tick_sets_open_price(self):
        wt = WindowTracker()
        wt.record_tick(65_000.0, ts=BASE_TS)
        window_end = WindowTracker._window_end(BASE_TS)
        assert wt.get_open_price(window_end) == 65_000.0

    def test_subsequent_ticks_do_not_overwrite_open(self):
        wt = WindowTracker()
        wt.record_tick(65_000.0, ts=BASE_TS)
        wt.record_tick(66_000.0, ts=BASE_TS + 10)   # same window
        wt.record_tick(67_000.0, ts=BASE_TS + 200)  # same window
        window_end = WindowTracker._window_end(BASE_TS)
        assert wt.get_open_price(window_end) == 65_000.0

    def test_new_window_records_separately(self):
        wt = WindowTracker()
        ts1 = BASE_TS
        ts2 = BASE_TS + 300   # guaranteed next 300-s window
        wt.record_tick(65_000.0, ts=ts1)
        wt.record_tick(66_000.0, ts=ts2)
        w1 = WindowTracker._window_end(ts1)
        w2 = WindowTracker._window_end(ts2)
        assert w1 != w2
        assert wt.get_open_price(w1) == 65_000.0
        assert wt.get_open_price(w2) == 66_000.0

    def test_get_open_price_returns_none_for_unknown_window(self):
        wt = WindowTracker()
        assert wt.get_open_price(999_999_900) is None

    # ── compute_delta_pct ─────────────────────────────────────────────────────

    def test_delta_pct_positive_move(self):
        wt = WindowTracker()
        wt.record_tick(65_000.0, ts=BASE_TS)
        window_end = WindowTracker._window_end(BASE_TS)
        # 65_065 is 0.10% above 65_000
        delta = wt.compute_delta_pct(65_065.0, window_end)
        assert delta == pytest.approx(0.10, rel=1e-4)

    def test_delta_pct_negative_move(self):
        wt = WindowTracker()
        wt.record_tick(65_000.0, ts=BASE_TS)
        window_end = WindowTracker._window_end(BASE_TS)
        delta = wt.compute_delta_pct(64_935.0, window_end)
        assert delta == pytest.approx(-0.10, rel=1e-4)

    def test_delta_pct_flat(self):
        wt = WindowTracker()
        wt.record_tick(65_000.0, ts=BASE_TS)
        window_end = WindowTracker._window_end(BASE_TS)
        assert wt.compute_delta_pct(65_000.0, window_end) == pytest.approx(0.0)

    def test_delta_pct_none_for_unknown_window(self):
        wt = WindowTracker()
        assert wt.compute_delta_pct(65_000.0, 999_999_900) is None

    def test_delta_pct_none_when_open_is_zero(self):
        wt = WindowTracker()
        wt.record_tick(0.0, ts=BASE_TS)
        window_end = WindowTracker._window_end(BASE_TS)
        # open_price == 0 → division guard → None
        assert wt.compute_delta_pct(65_000.0, window_end) is None


# ─── SignalEngine ──────────────────────────────────────────────────────────────

class TestSignalEngine:

    THRESHOLD = 0.08   # %
    ENTRY_WIN = 45     # seconds

    def _engine(self) -> SignalEngine:
        return SignalEngine(
            threshold_pct=self.THRESHOLD,
            entry_window_seconds=self.ENTRY_WIN,
        )

    def _eval(
        self,
        engine: SignalEngine,
        current: float = 65_000.0,
        open_: float = 65_000.0,
        ttc: float = 30.0,
        yes_mid: float = 0.55,
        no_mid: float = 0.45,
    ) -> Signal:
        return engine.evaluate(
            current_price=current,
            window_open_price=open_,
            seconds_until_close=ttc,
            yes_mid=yes_mid,
            no_mid=no_mid,
            yes_token_id="YES_TOKEN",
            no_token_id="NO_TOKEN",
        )

    # ── Time gate ─────────────────────────────────────────────────────────────

    def test_no_signal_when_too_early(self):
        eng = self._engine()
        sig = self._eval(eng, current=65_100.0, open_=65_000.0, ttc=46.0)
        assert sig.signal_type == SignalType.NONE
        assert sig.confidence == 0.0

    def test_signal_allowed_at_exactly_entry_window(self):
        eng = self._engine()
        # delta well above threshold, ttc == ENTRY_WIN
        sig = self._eval(eng, current=65_100.0, open_=65_000.0, ttc=float(self.ENTRY_WIN))
        assert sig.signal_type == SignalType.STRONG_BUY_UP

    def test_signal_allowed_inside_entry_window(self):
        eng = self._engine()
        sig = self._eval(eng, current=65_100.0, open_=65_000.0, ttc=10.0)
        assert sig.signal_type == SignalType.STRONG_BUY_UP

    # ── Zero open price guard ─────────────────────────────────────────────────

    def test_no_signal_when_open_price_is_zero(self):
        eng = self._engine()
        sig = self._eval(eng, current=65_000.0, open_=0.0, ttc=10.0)
        assert sig.signal_type == SignalType.NONE

    # ── Delta threshold ───────────────────────────────────────────────────────

    def test_no_signal_below_threshold(self):
        eng = self._engine()
        # 0.05% < 0.08%
        sig = self._eval(eng, current=65_032.5, open_=65_000.0, ttc=30.0)
        assert sig.signal_type == SignalType.NONE
        assert 0 < sig.confidence < 1.0

    def test_no_signal_below_threshold_confidence_scales(self):
        eng = self._engine()
        # delta = 0.04% → confidence = 0.04 / 0.08 = 0.5
        sig = self._eval(eng, current=65_026.0, open_=65_000.0, ttc=30.0)
        assert sig.signal_type == SignalType.NONE
        assert sig.confidence == pytest.approx(0.04 / 0.08, rel=1e-3)

    def test_strong_buy_up_on_positive_delta(self):
        eng = self._engine()
        # delta ≈ +0.10% > 0.08%
        sig = self._eval(eng, current=65_065.0, open_=65_000.0, ttc=30.0)
        assert sig.signal_type == SignalType.STRONG_BUY_UP
        assert sig.recommended_token == "YES_TOKEN"
        assert sig.recommended_price == 0.55

    def test_strong_buy_down_on_negative_delta(self):
        eng = self._engine()
        # delta ≈ -0.10%
        sig = self._eval(eng, current=64_935.0, open_=65_000.0, ttc=30.0)
        assert sig.signal_type == SignalType.STRONG_BUY_DOWN
        assert sig.recommended_token == "NO_TOKEN"
        assert sig.recommended_price == 0.45

    # ── Confidence calculation ────────────────────────────────────────────────

    def test_confidence_at_threshold_is_half(self):
        """
        delta just above threshold → confidence = delta / (2 * threshold) ≈ 0.5.
        We use open_ * threshold directly to avoid floating-point rounding that
        puts the computed delta just below the strict `<` comparison in the code.
        """
        eng = self._engine()
        open_ = 65_000.0
        # Add one cent above the exact threshold to stay strictly > threshold
        current = open_ + (open_ * self.THRESHOLD / 100) + 0.01
        sig = self._eval(eng, current=current, open_=open_, ttc=30.0)
        assert sig.signal_type == SignalType.STRONG_BUY_UP
        # confidence ≈ 0.5 (delta is only infinitesimally above threshold)
        assert sig.confidence == pytest.approx(0.5, abs=0.01)

    def test_confidence_clamps_at_one(self):
        """delta >= 2 * threshold → confidence == 1.0"""
        eng = self._engine()
        open_ = 65_000.0
        # delta = 0.16% == 2 * 0.08%
        current = open_ * (1 + 2 * self.THRESHOLD / 100)
        sig = self._eval(eng, current=current, open_=open_, ttc=30.0)
        assert sig.confidence == pytest.approx(1.0)

    def test_confidence_above_double_threshold_stays_at_one(self):
        eng = self._engine()
        open_ = 65_000.0
        current = open_ * (1 + 0.50 / 100)   # 0.50% >> 2 * 0.08%
        sig = self._eval(eng, current=current, open_=open_, ttc=30.0)
        assert sig.confidence == 1.0

    def test_delta_stored_correctly_in_signal(self):
        eng = self._engine()
        open_ = 65_000.0
        current = 65_065.0
        expected_delta = (current - open_) / open_ * 100
        sig = self._eval(eng, current=current, open_=open_, ttc=30.0)
        assert sig.window_delta_pct == pytest.approx(expected_delta, rel=1e-6)


# ─── KellySizer ────────────────────────────────────────────────────────────────

class TestKellySizer:

    # ── Construction ─────────────────────────────────────────────────────────

    def test_invalid_kelly_fraction_zero(self):
        with pytest.raises(ValueError):
            KellySizer(kelly_fraction=0.0)

    def test_invalid_kelly_fraction_negative(self):
        with pytest.raises(ValueError):
            KellySizer(kelly_fraction=-0.1)

    def test_invalid_kelly_fraction_above_one(self):
        with pytest.raises(ValueError):
            KellySizer(kelly_fraction=1.1)

    def test_full_kelly_fraction_one_is_valid(self):
        ks = KellySizer(kelly_fraction=1.0)
        assert ks.kelly_fraction == 1.0

    # ── Degenerate inputs ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.5])
    def test_returns_zero_for_degenerate_price(self, price):
        ks = KellySizer()
        assert ks.size(bankroll=1000.0, win_prob=0.60, price=price) == 0.0

    @pytest.mark.parametrize("win_prob", [0.0, 1.0, -0.1, 1.5])
    def test_returns_zero_for_degenerate_win_prob(self, win_prob):
        ks = KellySizer()
        assert ks.size(bankroll=1000.0, win_prob=win_prob, price=0.60) == 0.0

    # ── Negative edge (no bet) ────────────────────────────────────────────────

    def test_returns_zero_for_negative_edge(self):
        """
        price=0.80 → b=0.25; win_prob=0.10 → q=0.90
        full_kelly = (0.25*0.10 - 0.90) / 0.25 = -3.5 < 0 → no bet
        """
        ks = KellySizer(kelly_fraction=0.25)
        result = ks.size(bankroll=1000.0, win_prob=0.10, price=0.80)
        assert result == 0.0

    # ── Correct Kelly arithmetic ──────────────────────────────────────────────

    def test_kelly_formula_correctness(self):
        """
        price=0.60 → b = 1/0.60 - 1 = 2/3
        win_prob=0.70 → q=0.30
        full_kelly = (2/3 * 0.70 - 0.30) / (2/3) = 0.25
        fractional   = 0.25 * 0.25 = 0.0625
        stake        = 1000 * 0.0625 = 62.50
        """
        ks = KellySizer(kelly_fraction=0.25, min_position_usdc=1.0, max_position_usdc=10_000.0)
        result = ks.size(bankroll=1000.0, win_prob=0.70, price=0.60)
        assert result == pytest.approx(62.50, rel=1e-4)

    def test_full_kelly_fraction(self):
        """
        Same inputs as above but kelly_fraction=1.0 → stake = 1000 * 0.25 = 250
        """
        ks = KellySizer(kelly_fraction=1.0, min_position_usdc=1.0, max_position_usdc=10_000.0)
        result = ks.size(bankroll=1000.0, win_prob=0.70, price=0.60)
        assert result == pytest.approx(250.0, rel=1e-4)

    # ── Clamping ──────────────────────────────────────────────────────────────

    def test_clamps_to_max_position(self):
        ks = KellySizer(kelly_fraction=1.0, max_position_usdc=100.0, min_position_usdc=1.0)
        # Huge bankroll → unclamped stake would exceed max
        result = ks.size(bankroll=1_000_000.0, win_prob=0.70, price=0.60)
        assert result == 100.0

    def test_clamps_to_min_position(self):
        ks = KellySizer(kelly_fraction=0.01, min_position_usdc=10.0, max_position_usdc=500.0)
        # Tiny bankroll → unclamped stake would be below min
        result = ks.size(bankroll=10.0, win_prob=0.55, price=0.50)
        assert result == 10.0

    def test_returns_zero_not_min_when_no_edge(self):
        """min_position_usdc should NOT override a zero-edge signal."""
        ks = KellySizer(min_position_usdc=10.0)
        result = ks.size(bankroll=1000.0, win_prob=0.10, price=0.80)
        assert result == 0.0   # negative edge → 0, not clamped to min


# ─── DrawdownGuard ─────────────────────────────────────────────────────────────

class TestDrawdownGuard:

    def test_zero_drawdown_at_start(self):
        dg = DrawdownGuard(max_drawdown=0.15, starting_bankroll=300.0)
        assert dg.drawdown == pytest.approx(0.0)
        assert not dg.is_halted

    def test_peak_tracks_growth(self):
        dg = DrawdownGuard(starting_bankroll=300.0)
        dg.update(350.0)
        assert dg.peak == 350.0
        assert dg.current == 350.0

    def test_peak_does_not_shrink(self):
        dg = DrawdownGuard(starting_bankroll=300.0)
        dg.update(350.0)
        dg.update(280.0)
        assert dg.peak == 350.0
        assert dg.current == 280.0

    def test_drawdown_calculation(self):
        """(350 - 280) / 350 ≈ 0.2"""
        dg = DrawdownGuard(starting_bankroll=300.0)
        dg.update(350.0)
        dg.update(280.0)
        assert dg.drawdown == pytest.approx((350 - 280) / 350, rel=1e-6)

    def test_not_halted_below_limit(self):
        dg = DrawdownGuard(max_drawdown=0.15, starting_bankroll=300.0)
        dg.update(300.0)   # peak stays at 300
        dg.update(260.0)   # drawdown = 40/300 ≈ 13.3% < 15%
        assert not dg.is_halted

    def test_halted_at_exact_limit(self):
        dg = DrawdownGuard(max_drawdown=0.15, starting_bankroll=300.0)
        dg.update(300.0)
        dg.update(255.0)   # drawdown = 45/300 = 15.0% == limit
        assert dg.is_halted

    def test_halted_above_limit(self):
        dg = DrawdownGuard(max_drawdown=0.15, starting_bankroll=300.0)
        dg.update(300.0)
        dg.update(200.0)   # drawdown = 100/300 ≈ 33%
        assert dg.is_halted

    def test_peak_zero_edge_case(self):
        """Guard against zero-division when peak is somehow 0."""
        dg = DrawdownGuard.__new__(DrawdownGuard)
        dg.max_drawdown = 0.15
        dg.peak = 0.0
        dg.current = 0.0
        assert dg.drawdown == 0.0

    def test_status_line_contains_key_fields(self):
        dg = DrawdownGuard(starting_bankroll=300.0)
        dg.update(350.0)
        dg.update(300.0)
        line = dg.status_line()
        assert "300.00" in line   # current
        assert "350.00" in line   # peak
        assert "%" in line        # drawdown percentage


# ─── SimulationLedger ──────────────────────────────────────────────────────────

class TestSimulationLedger:

    # ── open_trade ────────────────────────────────────────────────────────────

    def test_open_trade_deducts_balance(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade(entry_usdc=50.0))
        assert ledger.balance == pytest.approx(250.0)

    def test_open_trade_appends_to_trades(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1"))
        ledger.open_trade(make_trade("T2"))
        assert len(ledger.trades) == 2

    def test_open_trade_ids_tracks_open_trades(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1"))
        ledger.open_trade(make_trade("T2"))
        assert set(ledger.open_trade_ids) == {"T1", "T2"}

    # ── settle_trade WIN ──────────────────────────────────────────────────────

    def test_settle_win_up_signal_price_goes_up(self):
        """STRONG_BUY_UP + price rose = WIN"""
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", signal_type=SignalType.STRONG_BUY_UP,
                                     entry_usdc=60.0, entry_price=0.60))
        rec = ledger.settle_trade("T1", window_open_price=65_000.0, window_close_price=65_100.0)
        assert rec.outcome == "WIN"

    def test_settle_win_down_signal_price_goes_down(self):
        """STRONG_BUY_DOWN + price fell = WIN"""
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", signal_type=SignalType.STRONG_BUY_DOWN,
                                     entry_usdc=60.0, entry_price=0.60))
        rec = ledger.settle_trade("T1", window_open_price=65_000.0, window_close_price=64_900.0)
        assert rec.outcome == "WIN"

    # ── settle_trade LOSS ─────────────────────────────────────────────────────

    def test_settle_loss_up_signal_price_goes_down(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", signal_type=SignalType.STRONG_BUY_UP,
                                     entry_usdc=60.0, entry_price=0.60))
        rec = ledger.settle_trade("T1", window_open_price=65_000.0, window_close_price=64_900.0)
        assert rec.outcome == "LOSS"

    def test_settle_loss_down_signal_price_goes_up(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", signal_type=SignalType.STRONG_BUY_DOWN,
                                     entry_usdc=60.0, entry_price=0.60))
        rec = ledger.settle_trade("T1", window_open_price=65_000.0, window_close_price=65_100.0)
        assert rec.outcome == "LOSS"

    # ── P&L arithmetic ────────────────────────────────────────────────────────

    def test_win_pnl_calculation(self):
        """
        entry_usdc=60, price=0.60 → shares = 60/0.60 = 100
        payout = 100 * 1.00 = 100 USDC
        pnl    = 100 - 60  = +40 USDC
        """
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", entry_usdc=60.0, entry_price=0.60))
        rec = ledger.settle_trade("T1", 65_000.0, 65_100.0)
        assert rec.pnl_usdc == pytest.approx(40.0, rel=1e-6)

    def test_loss_pnl_is_negative_entry(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", entry_usdc=60.0, entry_price=0.60))
        rec = ledger.settle_trade("T1", 65_000.0, 64_900.0)
        assert rec.pnl_usdc == pytest.approx(-60.0, rel=1e-6)

    def test_win_adds_payout_to_balance(self):
        """After WIN: balance = (300 - 60) + 100 = 340"""
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", entry_usdc=60.0, entry_price=0.60))
        ledger.settle_trade("T1", 65_000.0, 65_100.0)
        assert ledger.balance == pytest.approx(340.0, rel=1e-6)

    def test_loss_does_not_add_to_balance(self):
        """After LOSS: balance = 300 - 60 = 240"""
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", entry_usdc=60.0, entry_price=0.60))
        ledger.settle_trade("T1", 65_000.0, 64_900.0)
        assert ledger.balance == pytest.approx(240.0, rel=1e-6)

    def test_settle_removes_from_open_dict(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1"))
        ledger.settle_trade("T1", 65_000.0, 65_100.0)
        assert "T1" not in ledger.open_trade_ids

    def test_settle_unknown_trade_returns_none(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        result = ledger.settle_trade("NONEXISTENT", 65_000.0, 65_100.0)
        assert result is None

    # ── summary ───────────────────────────────────────────────────────────────

    def test_summary_no_settled_trades(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        s = ledger.summary()
        assert "No settled trades" in s
        assert "300.00" in s

    def test_summary_with_one_win(self):
        ledger = SimulationLedger(starting_usdc=300.0)
        ledger.open_trade(make_trade("T1", entry_usdc=60.0, entry_price=0.60))
        ledger.settle_trade("T1", 65_000.0, 65_100.0)
        s = ledger.summary()
        assert "Trades=1" in s
        assert "Wins=1" in s
        assert "100.0%" in s

    def test_summary_win_rate_calculation(self):
        ledger = SimulationLedger(starting_usdc=500.0)
        # 2 trades: 1 win, 1 loss
        ledger.open_trade(make_trade("T1", entry_usdc=50.0, entry_price=0.50))
        ledger.open_trade(make_trade("T2", entry_usdc=50.0, entry_price=0.50))
        ledger.settle_trade("T1", 65_000.0, 65_100.0)   # WIN (UP signal, price up)
        ledger.settle_trade("T2", 65_000.0, 64_900.0)   # LOSS (UP signal, price down)
        s = ledger.summary()
        assert "Trades=2" in s
        assert "Wins=1" in s
        assert "50.0%" in s

    def test_multiple_open_trades_tracked(self):
        ledger = SimulationLedger(starting_usdc=500.0)
        for i in range(5):
            ledger.open_trade(make_trade(f"T{i}", entry_usdc=20.0))
        assert len(ledger.open_trade_ids) == 5
        assert ledger.balance == pytest.approx(400.0)
