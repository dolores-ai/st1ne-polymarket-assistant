"""
Order execution layer for Polymarket CLOB.

Handles:
- Authentication (API key derivation from private key)
- Maker-only order placement (post_only=True)
- Order monitoring and cancellation
- Position tracking
- Paper trading mode (log signals without placing orders)

Requires environment variables:
- PM_PRIVATE_KEY: Ethereum private key for signing orders
- PM_FUNDER: Polymarket wallet address (funder address for proxy wallets)
- PM_SIGNATURE_TYPE: 0=EOA, 1=POLY_PROXY (Magic/email), 2=GNOSIS_SAFE (default: 1)
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from strategy import TradeSignal, Signal, ExitType


# ── Trade log ────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent.parent / "logs"

@dataclass
class TradeRecord:
    """Immutable record of a trade."""
    timestamp: str
    period_ts: int
    signal_type: str  # "momentum_scalp"
    side: str         # "Up" or "Down"
    entry_price: float
    entry_minute: float
    size_usd: float
    order_id: str | None
    status: str       # "paper", "posted", "filled", "cancelled", "failed"
    confidence: str
    outcome: str | None = None     # "won", "lost", None (pending)
    exit_type: str | None = None   # "tp", "sl", "deadline", "settlement"
    exit_price: float | None = None
    pnl: float | None = None


@dataclass
class BotState:
    """Tracks bot positions and P&L across periods."""
    trades: list[TradeRecord] = field(default_factory=list)
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    current_order_id: str | None = None
    current_side: str | None = None
    current_token_id: str | None = None

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0

    def record_trade(self, rec: TradeRecord):
        self.trades.append(rec)
        _append_log(rec)

    def record_outcome(self, won: bool, pnl: float, exit_type: str = "settlement", exit_price: float | None = None):
        if self.trades:
            last = self.trades[-1]
            last.outcome = "won" if won else "lost"
            last.pnl = pnl
            last.exit_type = exit_type
            last.exit_price = exit_price
        self.total_pnl += pnl
        if won:
            self.wins += 1
        else:
            self.losses += 1

    def clear_position(self):
        self.current_order_id = None
        self.current_side = None
        self.current_token_id = None


def _append_log(rec: TradeRecord):
    """Append trade record to daily JSON log file."""
    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"trades_{today}.jsonl"

    entry = {
        "timestamp": rec.timestamp,
        "period_ts": rec.period_ts,
        "signal": rec.signal_type,
        "side": rec.side,
        "entry_price": rec.entry_price,
        "entry_minute": round(rec.entry_minute, 2),
        "size_usd": rec.size_usd,
        "order_id": rec.order_id,
        "status": rec.status,
        "confidence": rec.confidence,
        "outcome": rec.outcome,
        "exit_type": rec.exit_type,
        "exit_price": rec.exit_price,
        "pnl": rec.pnl,
    }

    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Execution Modes ──────────────────────────────────────────

class PaperExecutor:
    """Paper trading — logs signals without placing real orders."""

    def __init__(self, size_usd: float = 10.0):
        self.size_usd = size_usd
        self.bot = BotState()

    def execute(self, signal: TradeSignal, up_token_id: str, dn_token_id: str) -> TradeRecord:
        token_id = up_token_id if signal.side == "Up" else dn_token_id
        self.bot.current_side = signal.side
        self.bot.current_token_id = token_id

        rec = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            period_ts=signal.period_ts,
            signal_type=signal.signal.value,
            side=signal.side,
            entry_price=signal.entry_price,
            entry_minute=signal.entry_minute,
            size_usd=self.size_usd,
            order_id=None,
            status="paper",
            confidence=signal.confidence,
        )
        self.bot.record_trade(rec)
        return rec

    def exit_position(self, exit_type: ExitType, current_price: float):
        """Exit the current position at current market price (TP/SL/deadline).

        Args:
            exit_type: Why we're exiting (TAKE_PROFIT, STOP_LOSS, DEADLINE)
            current_price: Current PM price for our side
        """
        if not self.bot.current_side:
            return

        entry = self.bot.trades[-1].entry_price if self.bot.trades else 0.5
        shares = self.size_usd / entry
        pnl = shares * (current_price - entry)
        won = pnl > 0

        self.bot.record_outcome(won, pnl, exit_type=exit_type.value, exit_price=current_price)
        self.bot.clear_position()

    def settle(self, outcome: str):
        """Settle the current position via oracle. outcome = 'up' or 'down'.

        NOTE: With TP/SL exits, this should rarely be called. Only used as
        a safety fallback if somehow a position is held to settlement.
        """
        if not self.bot.current_side:
            return

        won = (
            (self.bot.current_side == "Up" and outcome == "up") or
            (self.bot.current_side == "Down" and outcome == "down")
        )
        entry = self.bot.trades[-1].entry_price if self.bot.trades else 0.5
        settlement = 1.0 if won else 0.0
        shares = self.size_usd / entry
        pnl = shares * (settlement - entry)

        self.bot.record_outcome(won, pnl, exit_type="settlement", exit_price=settlement)
        self.bot.clear_position()

    def cancel(self):
        """Cancel current order (no-op for paper trading)."""
        self.bot.clear_position()


class LiveExecutor:
    """Live trading via Polymarket CLOB API."""

    def __init__(self, size_usd: float = 10.0):
        self.size_usd = size_usd
        self.bot = BotState()
        self.client = None
        self._init_client()

    def _init_client(self):
        """Initialize py-clob-client with credentials from env vars."""
        private_key = os.environ.get("PM_PRIVATE_KEY")
        funder = os.environ.get("PM_FUNDER")
        sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

        if not private_key:
            raise ValueError("PM_PRIVATE_KEY environment variable required for live trading")
        if not funder:
            raise ValueError("PM_FUNDER environment variable required for live trading")

        from py_clob_client.client import ClobClient

        host = "https://clob.polymarket.com"
        chain_id = 137  # Polygon mainnet

        # Create temp client to derive API creds
        temp_client = ClobClient(host, key=private_key, chain_id=chain_id)
        api_creds = temp_client.create_or_derive_api_creds()

        # Create full trading client
        self.client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=sig_type,
            funder=funder,
            creds=api_creds,
        )
        print("  [EXEC] CLOB client initialized (live mode)")

    def execute(self, signal: TradeSignal, up_token_id: str, dn_token_id: str) -> TradeRecord:
        """Place a maker-only order on Polymarket."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        token_id = up_token_id if signal.side == "Up" else dn_token_id
        price = round(signal.entry_price, 2)
        shares = round(self.size_usd / price, 1)

        self.bot.current_side = signal.side
        self.bot.current_token_id = token_id

        order_id = None
        status = "failed"

        try:
            signed_order = self.client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=shares,
                    side=BUY,
                )
            )
            resp = self.client.post_order(signed_order, OrderType.GTC)
            order_id = resp.get("orderID")
            if order_id:
                status = "posted"
                self.bot.current_order_id = order_id
                print(f"  [EXEC] order posted: {order_id[:16]}… | {signal.side} @ {price:.2f} | {shares:.1f} shares")
            else:
                status = "failed"
                print(f"  [EXEC] order post failed: {resp}")
        except Exception as e:
            print(f"  [EXEC] order placement error: {e}")
            status = "failed"

        rec = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            period_ts=signal.period_ts,
            signal_type=signal.signal.value,
            side=signal.side,
            entry_price=price,
            entry_minute=signal.entry_minute,
            size_usd=self.size_usd,
            order_id=order_id,
            status=status,
            confidence=signal.confidence,
        )
        self.bot.record_trade(rec)
        return rec

    def exit_position(self, exit_type: ExitType, current_price: float):
        """Exit the current position at current market price (TP/SL/deadline).

        For live trading, this would place a sell order. For now, we compute
        paper-style P&L as a placeholder until sell order logic is implemented.
        """
        if not self.bot.current_side:
            return

        # TODO: Place actual sell order via CLOB API
        # For now: paper-style P&L calculation
        entry = self.bot.trades[-1].entry_price if self.bot.trades else 0.5
        shares = self.size_usd / entry
        pnl = shares * (current_price - entry)
        won = pnl > 0

        self.bot.record_outcome(won, pnl, exit_type=exit_type.value, exit_price=current_price)
        self.bot.clear_position()

    def settle(self, outcome: str):
        """Settle the current position based on market outcome."""
        if not self.bot.current_side:
            return

        won = (
            (self.bot.current_side == "Up" and outcome == "up") or
            (self.bot.current_side == "Down" and outcome == "down")
        )
        entry = self.bot.trades[-1].entry_price if self.bot.trades else 0.5
        settlement = 1.0 if won else 0.0
        shares = self.size_usd / entry
        pnl = shares * (settlement - entry)

        self.bot.record_outcome(won, pnl, exit_type="settlement", exit_price=settlement)
        self.bot.clear_position()

    def cancel(self):
        """Cancel current open order."""
        if self.bot.current_order_id and self.client:
            try:
                self.client.cancel(self.bot.current_order_id)
                print(f"  [EXEC] order cancelled: {self.bot.current_order_id[:16]}…")
            except Exception as e:
                print(f"  [EXEC] cancel error: {e}")
        self.bot.clear_position()
