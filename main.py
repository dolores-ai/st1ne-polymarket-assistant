import sys
import os
import asyncio
import json
import time
import requests
import websockets
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.live import Live

import config
import feeds
import dashboard

# Trading imports
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

console = Console(force_terminal=True)

# Environment for trading
POLYMARKET_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
PROXY_WALLET = os.environ.get("PROXY_WALLET", "")
PROXY_URL = os.environ.get("PROXY_URL", "")


class TradingBot:
    """Integrated trading bot"""
    def __init__(self):
        self.client = None
        self.last_trade = 0
        
    def init(self):
        """Initialize CLOB client"""
        if not POLYMARKET_PRIVATE_KEY:
            return False
        try:
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=POLYMARKET_PRIVATE_KEY
            )
            creds = client.derive_api_key()
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=POLYMARKET_PRIVATE_KEY,
                creds=creds,
                signature_type=1,
                funder=PROXY_WALLET
            )
            return True
        except Exception as e:
            print(f"CLOB init error: {e}")
            return False
    
    def execute(self, token_id: str, side: str, price: float, amount: float = 5.0):
        """Execute trade"""
        if not self.client:
            print("❌ CLOB not initialized")
            return
        
        if time.time() - self.last_trade < 300:  # 5 min cooldown
            print("⏳ Cooldown active")
            return
        
        size = round(amount / price, 2)
        try:
            result = self.client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=size, side=side),
                PartialCreateOrderOptions(tick_size="0.01")
            )
            if result.get("success"):
                print(f"✅ Order placed: {result.get('orderID', 'N/A')[:16]}...")
                self.last_trade = time.time()
            else:
                print(f"❌ Failed: {result}")
        except Exception as e:
            print(f"❌ Trade error: {e}")


trading_bot = TradingBot()


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


async def display_loop(state: feeds.State, coin: str, tf: str):
    await asyncio.sleep(2)
    with Live(console=console, refresh_per_second=1, transient=False) as live:
        while True:
            if state.mid > 0 and state.klines:
                live.update(dashboard.render(state, coin, tf))
            await asyncio.sleep(config.REFRESH)


async def trading_loop(state: feeds.State):
    """Background trading signals"""
    min_spread = 0.015  # 1.5%
    last_signal = None
    
    while True:
        if state.pm_up and state.pm_dn:
            spread = state.pm_up - 0.5
            
            if spread < -min_spread and last_signal != "BUY_UP":
                signal = "BUY_UP"
            elif spread > min_spread and last_signal != "BUY_DOWN":
                signal = "BUY_DOWN"
            else:
                signal = "WAIT"
            
            if signal != last_signal and signal != "WAIT":
                last_signal = signal
                print(f"\n🎯 SIGNAL: {signal} | Spread: {spread*100:+.1f}%\n")
        
        await asyncio.sleep(10)


async def main():
    console.print("\n[bold magenta]═══ CRYPTO PREDICTION DASHBOARD ═══[/bold magenta]\n")

    # Initialize trading if keys present
    if POLYMARKET_PRIVATE_KEY:
        if trading_bot.init():
            console.print("  [green]✅ Trading enabled[/green]")
        else:
            console.print("  [yellow]⚠️ Trading disabled (no API key)[/yellow]")
    else:
        console.print("  [yellow]⚠️ Set POLYMARKET_PRIVATE_KEY to enable trading[/yellow]")

    coin = pick("Select coin:", config.COINS)
    tf = pick("Select timeframe:", config.TIMEFRAMES)

    console.print(f"\n[bold green]Starting {coin} {tf} …[/bold green]\n")

    state = feeds.State()

    state.pm_up_id, state.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
    if state.pm_up_id:
        console.print(f"  [PM] Up   → {state.pm_up_id[:24]}…")
        console.print(f"  [PM] Down → {state.pm_dn_id[:24]}…")
        pm_up_price = f"${state.pm_up:.4f}" if state.pm_up else "N/A"
        pm_dn_price = f"${state.pm_dn:.4f}" if state.pm_dn else "N/A"
        console.print(f"  [PM] UP Price: {pm_up_price} | DOWN: {pm_dn_price}\n")
    else:
        console.print("  [yellow][PM] no market for this coin/timeframe – prices will not show[/yellow]")

    binance_sym = config.COIN_BINANCE[coin]
    kline_iv = config.TF_KLINE[tf]
    console.print("  [Binance] bootstrapping candles …")
    await feeds.bootstrap(binance_sym, kline_iv, state)

    console.print("\n  Commands: [UP] buy UP | [DN] buy DOWN | [Q] quit\n")

    await asyncio.gather(
        feeds.ob_poller(binance_sym, state),
        feeds.binance_feed(binance_sym, kline_iv, state),
        feeds.pm_feed(state),
        display_loop(state, coin, tf),
        trading_loop(state),
    )


if __name__ == "__main__":
    asyncio.run(main())
