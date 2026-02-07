"""
Momentum Scalp strategy for Polymarket 15m BTC Up/Down binary options.

Strategy: Buy the leading side at minute 5 if trending, exit via TP/SL.
- Entry: minute 5, leading side >= 0.60, price increased from minute 3
- Take profit: +15¢ (sell when price rises 15¢ above entry)
- Stop loss: -5¢ (cut losses when price drops 5¢ from entry)
- Deadline: exit at market price at minute 14 if neither TP/SL hit

Backtest results (96h, 294 periods):
  - 185 trades, 58% win rate, +5.6¢/trade, t-stat = +8.18
  - Avg win: +13.3¢, Avg loss: -4.9¢ (2.7:1 reward/risk)
  - Breakeven win rate: 27%, actual: 58% → huge edge buffer
  - Split-half: +5.9¢ H1, +5.3¢ H2 (stable)
  - P&L asymmetry: FIXED (old bot was +3.1¢ win / -10¢ loss)
"""

import time
from dataclasses import dataclass, field
from enum import Enum


class Signal(Enum):
    NONE = "none"
    MOMENTUM_SCALP = "momentum_scalp"


class ExitType(Enum):
    NONE = "none"
    TAKE_PROFIT = "tp"
    STOP_LOSS = "sl"
    DEADLINE = "deadline"


@dataclass
class TradeSignal:
    signal: Signal
    side: str           # "Up" or "Down"
    entry_price: float  # PM price at entry
    entry_minute: float # minute within the period
    period_ts: int      # period start timestamp
    confidence: str     # description of why


@dataclass
class PriceSnapshot:
    """Stores PM Up price at a specific minute within the period."""
    minute: float
    price_up: float
    timestamp: float


@dataclass
class StrategyState:
    """Per-period state for strategy evaluation."""
    period_ts: int = 0
    snapshots: list = field(default_factory=list)
    signal_fired: bool = False
    last_signal: TradeSignal | None = None

    def reset(self, period_ts: int):
        self.period_ts = period_ts
        self.snapshots = []
        self.signal_fired = False
        self.last_signal = None

    def add_snapshot(self, minute: float, price_up: float):
        self.snapshots.append(PriceSnapshot(
            minute=minute,
            price_up=price_up,
            timestamp=time.time(),
        ))

    def get_price_at(self, target_minute: float, tolerance: float = 1.0) -> float | None:
        """Get price closest to target minute, within tolerance."""
        best = None
        best_dist = float("inf")
        for s in self.snapshots:
            dist = abs(s.minute - target_minute)
            if dist < best_dist and dist <= tolerance:
                best_dist = dist
                best = s.price_up
        return best


# ── Strategy Parameters (from backtest: mom_m5_lb2_60%_tp0.15_sl0.05) ──

ENTRY_MINUTE = 5.0          # evaluate signal at minute 5
LOOKBACK_MINUTE = 3.0       # trend confirmation: price must be trending since minute 3
THRESHOLD = 0.60            # minimum price to enter (leading side >= 60%)
MIN_SNAPSHOT_MINUTE = 2.5   # start collecting snapshots at minute 2.5
MAX_SIGNAL_MINUTE = 6.0     # don't fire signals after minute 6

# Exit parameters
TAKE_PROFIT = 0.15          # +15¢ from entry price
STOP_LOSS = 0.05            # -5¢ from entry price
DEADLINE_MINUTE = 14.0      # exit at market if neither TP/SL hit by minute 14


def evaluate(state: StrategyState) -> TradeSignal | None:
    """
    Evaluate the momentum scalp strategy at the current moment.

    Returns a TradeSignal if conditions are met, None otherwise.
    Only fires ONCE per period.
    """
    if state.signal_fired:
        return None

    # Get prices at key minutes
    p3 = state.get_price_at(LOOKBACK_MINUTE)
    p5 = state.get_price_at(ENTRY_MINUTE)

    if p5 is None:
        return None  # not enough data yet

    # Current minute
    now = time.time()
    current_minute = (now - state.period_ts) / 60.0

    # Only evaluate in the right window
    if current_minute < ENTRY_MINUTE - 0.5 or current_minute > MAX_SIGNAL_MINUTE:
        return None

    # ── MOMENTUM SCALP: buy leading side if trending ──
    if p3 is not None:
        price_dn_5 = 1.0 - p5

        # Buy Up: price_up >= threshold AND price_up increased from minute 3
        if p5 >= THRESHOLD and p5 > p3:
            state.signal_fired = True
            sig = TradeSignal(
                signal=Signal.MOMENTUM_SCALP,
                side="Up",
                entry_price=p5,
                entry_minute=current_minute,
                period_ts=state.period_ts,
                confidence=f"momentum Up, p3={p3:.3f} p5={p5:.3f}, tp={p5+TAKE_PROFIT:.3f} sl={p5-STOP_LOSS:.3f}",
            )
            state.last_signal = sig
            return sig

        # Buy Down: price_dn >= threshold AND price_dn increased (price_up decreased)
        elif price_dn_5 >= THRESHOLD and p5 < p3:
            state.signal_fired = True
            sig = TradeSignal(
                signal=Signal.MOMENTUM_SCALP,
                side="Down",
                entry_price=price_dn_5,
                entry_minute=current_minute,
                period_ts=state.period_ts,
                confidence=f"momentum Down, p3={p3:.3f} p5={p5:.3f}, tp={price_dn_5+TAKE_PROFIT:.3f} sl={price_dn_5-STOP_LOSS:.3f}",
            )
            state.last_signal = sig
            return sig

    return None


def check_exit(state: StrategyState, current_price_up: float) -> ExitType:
    """
    Check if the current position should be exited.

    Returns ExitType if position should be closed, ExitType.NONE otherwise.
    Call this every tick while a position is open.
    """
    if state.last_signal is None:
        return ExitType.NONE

    sig = state.last_signal
    now = time.time()
    current_minute = (now - state.period_ts) / 60.0

    # Calculate current price for our side
    if sig.side == "Up":
        current_side_price = current_price_up
    else:
        current_side_price = 1.0 - current_price_up

    # Check take profit
    if current_side_price >= sig.entry_price + TAKE_PROFIT:
        return ExitType.TAKE_PROFIT

    # Check stop loss
    if current_side_price <= sig.entry_price - STOP_LOSS:
        return ExitType.STOP_LOSS

    # Check deadline (exit at market near period end)
    if current_minute >= DEADLINE_MINUTE:
        return ExitType.DEADLINE

    return ExitType.NONE
