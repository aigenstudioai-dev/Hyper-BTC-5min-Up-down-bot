"""
strategy.py – Signal generation and position sizing engine.

Core responsibilities:
  • Track the BTC price at each 5-minute window open.
  • Compute the Window Delta to detect directional momentum.
  • Generate a STRONG_BUY signal when conditions are met.
  • Size positions using fractional Kelly Criterion.
  • Enforce the maximum drawdown circuit-breaker.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Signal types ──────────────────────────────────────────────────────────────


class SignalType(Enum):
    NONE = "NONE"
    STRONG_BUY_UP = "STRONG_BUY_UP"      # Buy YES token (price will finish higher)
    STRONG_BUY_DOWN = "STRONG_BUY_DOWN"  # Buy NO  token (price will finish lower)


@dataclass
class Signal:
    signal_type: SignalType
    window_delta_pct: float       # (current − open) / open × 100
    current_price: float          # BTC/USD at signal time
    window_open_price: float      # BTC/USD at window open
    timestamp: float              # time.time() at signal generation
    confidence: float             # 0–1, derived from |delta| magnitude
    recommended_token: str = ""   # token_id to buy
    recommended_price: float = 0.0  # limit-order price (USDC per share)


@dataclass
class TradeRecord:
    trade_id: str
    signal: Signal
    entry_usdc: float           # capital deployed
    entry_price: float          # limit order price
    outcome: Optional[str] = None  # "WIN" | "LOSS" | "PUSH"
    pnl_usdc: float = 0.0
    closed_at: Optional[float] = None


# ─── Window price tracker ──────────────────────────────────────────────────────


class WindowTracker:
    """
    Records the BTC opening price for each 5-minute window.

    A 'window' is identified by its end-time (Unix timestamp divisible by 300).
    The opening price is the first tick received after the window starts.
    """

    WINDOW_SECONDS: int = 300

    def __init__(self) -> None:
        self._opens: Dict[int, float] = {}   # window_end_ts → open price
        self._current_window: Optional[int] = None

    def record_tick(self, price: float, ts: float = None) -> None:
        ts = ts or time.time()
        window_end = self._window_end(int(ts))
        if window_end not in self._opens:
            self._opens[window_end] = price
            logger.debug(
                "New window %d → open price %.2f",
                window_end,
                price,
            )
        self._current_window = window_end

    def get_open_price(self, window_end: int) -> Optional[float]:
        return self._opens.get(window_end)

    def compute_delta_pct(self, current_price: float, window_end: int) -> Optional[float]:
        """
        Window Delta (%) = (current − open) / open × 100.
        Returns None if the window open is not yet recorded.
        """
        open_price = self.get_open_price(window_end)
        if open_price is None or open_price == 0:
            return None
        return (current_price - open_price) / open_price * 100.0

    @staticmethod
    def _window_end(ts: int) -> int:
        return ((ts // WindowTracker.WINDOW_SECONDS) + 1) * WindowTracker.WINDOW_SECONDS

    def current_window_end(self) -> int:
        return self._window_end(int(time.time()))


# ─── Signal engine ─────────────────────────────────────────────────────────────


class SignalEngine:
    """
    Evaluates the Window Delta and emits a trading signal.

    Entry rule (STRONG_BUY):
      |delta| > threshold (default 0.08%) at T-45s or closer.
      Positive delta → BUY YES (UP token).
      Negative delta → BUY NO  (DOWN token).

    The confidence score scales linearly up to 2× the threshold,
    then clamps at 1.0 so the Kelly sizer receives a meaningful prior.
    """

    def __init__(
        self,
        threshold_pct: float = 0.08,
        entry_window_seconds: int = 45,
    ) -> None:
        self.threshold_pct = threshold_pct
        self.entry_window_seconds = entry_window_seconds

    def evaluate(
        self,
        current_price: float,
        window_open_price: float,
        seconds_until_close: float,
        yes_mid: float,
        no_mid: float,
        yes_token_id: str,
        no_token_id: str,
    ) -> Signal:
        """
        Returns a Signal.  signal_type == NONE if conditions are not met.
        """
        if seconds_until_close > self.entry_window_seconds:
            return Signal(
                signal_type=SignalType.NONE,
                window_delta_pct=0.0,
                current_price=current_price,
                window_open_price=window_open_price,
                timestamp=time.time(),
                confidence=0.0,
            )

        if window_open_price == 0:
            return Signal(
                signal_type=SignalType.NONE,
                window_delta_pct=0.0,
                current_price=current_price,
                window_open_price=window_open_price,
                timestamp=time.time(),
                confidence=0.0,
            )

        delta = (current_price - window_open_price) / window_open_price * 100.0
        abs_delta = abs(delta)

        if abs_delta < self.threshold_pct:
            return Signal(
                signal_type=SignalType.NONE,
                window_delta_pct=delta,
                current_price=current_price,
                window_open_price=window_open_price,
                timestamp=time.time(),
                confidence=abs_delta / self.threshold_pct,
            )

        # Signal is triggered
        confidence = min(1.0, abs_delta / (2.0 * self.threshold_pct))

        if delta > 0:
            sig_type = SignalType.STRONG_BUY_UP
            rec_token = yes_token_id
            rec_price = yes_mid
        else:
            sig_type = SignalType.STRONG_BUY_DOWN
            rec_token = no_token_id
            rec_price = no_mid

        signal = Signal(
            signal_type=sig_type,
            window_delta_pct=delta,
            current_price=current_price,
            window_open_price=window_open_price,
            timestamp=time.time(),
            confidence=confidence,
            recommended_token=rec_token,
            recommended_price=rec_price,
        )

        logger.info(
            "SIGNAL %s | delta=%.4f%% | price=%.2f | conf=%.2f | token=%s @ %.4f",
            sig_type.value,
            delta,
            current_price,
            confidence,
            rec_token[:8],
            rec_price,
        )
        return signal


# ─── Kelly Criterion position sizer ────────────────────────────────────────────


class KellySizer:
    """
    Fractional Kelly Criterion for binary prediction markets.

    For a binary bet:
        b = (1 / price) - 1   ← net odds (1.00 USDC returned per 'price' staked)
        f* = (b·p − q) / b    ← full Kelly fraction
        f  = kelly_fraction × f*   ← fractional Kelly

    'price' is the market mid (USDC per share).  For the YES token at mid 0.60,
    b = 1/0.60 − 1 = 0.667, meaning you risk 0.60 to win 1.00.

    The strategy's 'confidence' is used as the estimated win probability p.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_position_usdc: float = 500.0,
        min_position_usdc: float = 10.0,
    ) -> None:
        if not 0 < kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        self.kelly_fraction = kelly_fraction
        self.max_position_usdc = max_position_usdc
        self.min_position_usdc = min_position_usdc

    def size(
        self,
        bankroll: float,
        win_prob: float,    # estimated probability of winning (0–1)
        price: float,       # market price USDC per share (0–1)
    ) -> float:
        """
        Returns the recommended USDC amount to stake, clamped to
        [min_position_usdc, max_position_usdc].
        """
        if price <= 0 or price >= 1 or win_prob <= 0 or win_prob >= 1:
            logger.debug(
                "Kelly: degenerate inputs price=%.4f p=%.4f → skip",
                price, win_prob,
            )
            return 0.0

        b = (1.0 / price) - 1.0   # net decimal odds
        q = 1.0 - win_prob

        full_kelly = (b * win_prob - q) / b

        if full_kelly <= 0:
            logger.debug(
                "Kelly: negative edge (p=%.4f, b=%.4f) → no bet", win_prob, b
            )
            return 0.0

        fractional_kelly = self.kelly_fraction * full_kelly
        stake = bankroll * fractional_kelly

        clamped = max(self.min_position_usdc, min(self.max_position_usdc, stake))
        logger.debug(
            "Kelly: p=%.3f b=%.3f f*=%.4f f=%.4f stake=%.2f USDC (clamped=%.2f)",
            win_prob, b, full_kelly, fractional_kelly, stake, clamped,
        )
        return clamped


# ─── Drawdown guard ────────────────────────────────────────────────────────────


class DrawdownGuard:
    """
    Tracks peak bankroll and halts the bot if drawdown exceeds the limit.

    Drawdown = (peak − current) / peak
    """

    def __init__(self, max_drawdown: float = 0.15, starting_bankroll: float = 300.0) -> None:
        self.max_drawdown = max_drawdown
        self.peak = starting_bankroll
        self.current = starting_bankroll

    def update(self, current_bankroll: float) -> None:
        self.current = current_bankroll
        if current_bankroll > self.peak:
            self.peak = current_bankroll

    @property
    def drawdown(self) -> float:
        if self.peak == 0:
            return 0.0
        return (self.peak - self.current) / self.peak

    @property
    def is_halted(self) -> bool:
        halted = self.drawdown >= self.max_drawdown
        if halted:
            logger.warning(
                "CIRCUIT BREAKER: drawdown %.2f%% ≥ limit %.2f%%",
                self.drawdown * 100,
                self.max_drawdown * 100,
            )
        return halted

    def status_line(self) -> str:
        return (
            f"Bankroll: ${self.current:.2f} | "
            f"Peak: ${self.peak:.2f} | "
            f"Drawdown: {self.drawdown*100:.2f}%"
        )


# ─── Simulation ledger ─────────────────────────────────────────────────────────


class SimulationLedger:
    """
    Paper-trades without interacting with the blockchain.

    Tracks virtual fills, P&L, and win-rate for strategy validation.
    At window close, the bot calls `settle_trade` with the final BTC price
    to compute whether the outcome token expired in-the-money.
    """

    def __init__(self, starting_usdc: float = 300.0) -> None:
        self.balance = starting_usdc
        self.trades: List[TradeRecord] = []
        self._open: Dict[str, TradeRecord] = {}   # order_id → record

    def open_trade(self, record: TradeRecord) -> None:
        self.balance -= record.entry_usdc
        self._open[record.trade_id] = record
        self.trades.append(record)
        logger.info(
            "[SIM] Open trade %s | size=%.2f | balance=%.2f",
            record.trade_id, record.entry_usdc, self.balance,
        )

    def settle_trade(
        self,
        trade_id: str,
        window_open_price: float,
        window_close_price: float,
    ) -> Optional[TradeRecord]:
        """
        Settle a paper-trade at window expiry.
        Returns the settled record, or None if trade_id not found.
        """
        record = self._open.pop(trade_id, None)
        if record is None:
            return None

        sig = record.signal
        price_moved_up = window_close_price > window_open_price

        if sig.signal_type == SignalType.STRONG_BUY_UP:
            won = price_moved_up
        elif sig.signal_type == SignalType.STRONG_BUY_DOWN:
            won = not price_moved_up
        else:
            won = False

        if won:
            # Pay-out: stake / entry_price (shares × $1 per share)
            payout = record.entry_usdc / record.entry_price
            pnl = payout - record.entry_usdc
            record.outcome = "WIN"
        else:
            payout = 0.0
            pnl = -record.entry_usdc
            record.outcome = "LOSS"

        record.pnl_usdc = pnl
        record.closed_at = time.time()
        self.balance += payout

        logger.info(
            "[SIM] Settled %s | %s | open=%.2f close=%.2f | pnl=%.2f | balance=%.2f",
            trade_id,
            record.outcome,
            window_open_price,
            window_close_price,
            pnl,
            self.balance,
        )
        return record

    def summary(self) -> str:
        closed = [t for t in self.trades if t.outcome is not None]
        if not closed:
            return f"[SIM] No settled trades yet | balance=${self.balance:.2f}"
        wins = sum(1 for t in closed if t.outcome == "WIN")
        total_pnl = sum(t.pnl_usdc for t in closed)
        win_rate = wins / len(closed) * 100
        return (
            f"[SIM] Trades={len(closed)} | Wins={wins} ({win_rate:.1f}%) | "
            f"PnL=${total_pnl:+.2f} | Balance=${self.balance:.2f}"
        )

    @property
    def open_trade_ids(self) -> List[str]:
        return list(self._open.keys())
