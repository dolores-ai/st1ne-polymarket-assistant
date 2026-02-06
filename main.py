import sys
import os
import asyncio
import time
from datetime import datetime
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.live   import Live

import config
import feeds
import dashboard

# --- CONFIGURATION & ENV ---
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
# Default to True for safety
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true" 

# Risk Controls
MAX_POSITION_USD = 5.0
DAILY_LOSS_LIMIT = -15.0

# Execution Filters
CONVICTION_THRESHOLD = 8  # Score must be >= 8 (out of 10) for auto-trade
MIN_IMBALANCE = 0.65      # OBI must be > 0.65 (strong book pressure)
YES_PRICE_MIN = 0.20
YES_PRICE_MAX = 0.58      # Avoid buying tops

console = Console(force_terminal=True)


class RiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.active_positions = 0
        self.last_trade_time = 0

    def can_trade(self) -> bool:
        if self.daily_pnl <= DAILY_LOSS_LIMIT:
            return False
        return True

    def record_trade(self, pnl=0.0):
        self.daily_pnl += pnl
        self.last_trade_time = time.time()


def log_trade(action: str, market: str, price: float, size: float, details: str):
    """Log trade to file for audit trail"""
    timestamp = datetime.now().isoformat()
    mode = "[DRY-RUN]" if DRY_RUN else "[LIVE]"
    entry = f"{timestamp} {mode} {action} {market} @ {price} (Size: {size}) | {details}\n"
    
    with open("trade_log.txt", "a", encoding="utf-8") as f:
        f.write(entry)
    
    # Also print to console (will be overwritten by Live display but visible in logs)
    # We use a separate print to ensure it might be seen if Live is transient
    # print(entry.strip()) 


async def execute_strategy(client: ClobClient, state: feeds.State, coin: str, tf: str, risk: RiskManager):
    """
    Evaluates bias score and executes trade if filters are met.
    This is the core execution logic.
    """
    if not client:
        return

    # 1. Get Bias Score
    score, label, _ = dashboard._score_trend(state)
    
    # 2. Check risk limits
    if not risk.can_trade():
        return

    # 3. Apply Filters
    # We need a valid Polymarket ID to trade
    if not state.pm_up_id or not state.pm_dn_id:
        return
    
    # Check OBI (Order Book Imbalace)
    obi_v = 0.0
    if state.mid:
         import indicators as ind # Lazy import to avoid circular dep if any
         obi_v = ind.obi(state.bids, state.asks, state.mid)

    # Determine functionality based on direction
    # If Bullish -> Buy YES on Up token? Or Buy YES on long-market?
    # The dashboard calls 'pm_up' the price of the 'Up' token.
    # We assume 'Up' token roughly correlates with the crypto price going UP.
    
    target_token_id = None
    target_price = None
    
    is_bullish = score >= CONVICTION_THRESHOLD
    is_bearish = score <= -CONVICTION_THRESHOLD

    # NOTE: This dashboard maps "Up" token to pm_up_id.
    # We buy "Up" if Bullish, "Down" if Bearish.
    
    explanation = []
    
    if is_bullish:
        if obi_v < MIN_IMBALANCE: # Want positive imbalance for bullish
            return 
        price = state.pm_up
        if price is None: return
        
        if YES_PRICE_MIN <= price <= YES_PRICE_MAX:
             target_token_id = state.pm_up_id
             target_price = price
             explanation = [f"Score {score}>={CONVICTION_THRESHOLD}", f"OBI {obi_v:.2f}", f"Price {price:.2f}"]
    
    elif is_bearish:
        if obi_v > -MIN_IMBALANCE: # Want negative imbalance for bearish
            return
        price = state.pm_dn
        if price is None: return
        
        if YES_PRICE_MIN <= price <= YES_PRICE_MAX:
            target_token_id = state.pm_dn_id
            target_price = price
            explanation = [f"Score {score}<={-CONVICTION_THRESHOLD}", f"OBI {obi_v:.2f}", f"Price {price:.2f}"]

    # 4. Execute
    if target_token_id:
        # Debounce: simplistic check to avoid spamming same signal every second
        # Real system needs state.active_orders or position check.
        # For this 'thin' layer, we rely on the high threshold rarity and 
        # maybe a cooldown.
        if time.time() - risk.last_trade_time < 300: # 5 min cooldown
            return

        size_size = MAX_POSITION_USD
        
        # Log intent
        details = ", ".join(explanation)
        log_trade("TRIGGER_BUY", coin, target_price, size_size, details)
        
        if not DRY_RUN:
            try:
                # Market Buy
                # ClobClient market order requires approving the token first (USDC)
                # We use FOK or IOC usually.
                order = client.create_order(
                    OrderArgs(
                        price=target_price + 0.05, # Aggressive slippage cap for market take
                        size=size_size / target_price, # contracts = USD / price
                        side=BUY,
                        token_id=target_token_id,
                        order_type=OrderType.FOK 
                    )
                )
                resp = client.post_order(order)
                log_trade("EXEC_SUCCESS", coin, target_price, size_size, f"Tx: {resp}")
                risk.record_trade(0) # Update timestamp only, pnl unknown yet
            except Exception as e:
                log_trade("EXEC_FAIL", coin, target_price, size_size, str(e))
                # console.print(f"[red]Trade failed: {e}[/red]")
        else:
             # Just update the timestamp to simulate cooldown
             risk.record_trade(0)
             # console.print(f"[yellow][DRY-RUN] Would buy {coin} {tf} @ {target_price}[/yellow]")


def pick(title: str, options: list[str]) -> str:
    console.print(f"\n[bold]{title}[/bold]")
    for i, o in enumerate(options, 1):
        console.print(f"  [{i}] {o}")
    while True:
        raw = input("  → ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        console.print("  [red]invalid – try again[/red]")


async def display_loop(state: feeds.State, coin: str, tf: str, client: ClobClient, risk: RiskManager):
    await asyncio.sleep(2)
    with Live(console=console, refresh_per_second=1, transient=False) as live:
        while True:
            if state.mid > 0 and state.klines:
                live.update(dashboard.render(state, coin, tf))
                
                # Execute Strategy Hook
                await execute_strategy(client, state, coin, tf, risk)
                
            await asyncio.sleep(config.REFRESH)


async def main():
    console.print("\n[bold magenta]═══ CRYPTO PREDICTION DASHBOARD + AUTO-EXEC ═══[/bold magenta]\n")
    
    # Init Execution Client
    client = None
    if PRIVATE_KEY:
        try:
            client = ClobClient(
                host=os.getenv("CLOB_API_URL", "https://clob.polymarket.com"),
                key=PRIVATE_KEY,
                chain_id=137,
                signature_type=0, 
            )
            # Derive API creds (Required for order placement)
            client.set_api_creds(client.create_or_derive_api_creds())
            
            mode = "DRY RUN" if DRY_RUN else "LIVE TRADING"
            color = "yellow" if DRY_RUN else "red"
            console.print(f"[{color}]Execution Client Initialized: {mode}[/{color}]")
        except Exception as e:
             console.print(f"[red]Client Init Failed: {e} (Trading Disabled)[/red]")

    risk = RiskManager()

    # Original Inputs
    coin = pick("Select coin:", config.COINS)
    tf   = pick("Select timeframe:", config.TIMEFRAMES)

    console.print(f"\n[bold green]Starting {coin} {tf} …[/bold green]\n")

    state = feeds.State()

    state.pm_up_id, state.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
    if state.pm_up_id:
        console.print(f"  [PM] Up   → {state.pm_up_id[:24]}…")
        console.print(f"  [PM] Down → {state.pm_dn_id[:24]}…")
    else:
        console.print("  [yellow][PM] no market for this coin/timeframe – prices will not show[/yellow]")

    binance_sym = config.COIN_BINANCE[coin]
    kline_iv    = config.TF_KLINE[tf]
    console.print("  [Binance] bootstrapping candles …")
    await feeds.bootstrap(binance_sym, kline_iv, state)

    await asyncio.gather(
        feeds.ob_poller(binance_sym, state),
        feeds.binance_feed(binance_sym, kline_iv, state),
        feeds.pm_feed(state),
        display_loop(state, coin, tf, client, risk),
    )


if __name__ == "__main__":
    asyncio.run(main())
