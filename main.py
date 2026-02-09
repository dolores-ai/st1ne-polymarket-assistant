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


class PriceData:
    """All price data in one place"""
    def __init__(self):
        self.binance = {"price": 0, "bid": 0, "ask": 0}
        self.chainlink = {"price": 0, "change_pct": 0}
        self.polymarket = {"up_price": 0, "down_price": 0, "up_id": "", "down_id": ""}
        self.timestamp = 0
    
    def update(self):
        self.timestamp = time.time()


prices = PriceData()


async def fetch_chainlink():
    """Fetch Chainlink BTC/USD price"""
    # Chainlink BTC/USD feed on mainnet
    # Using direct contract call or public API
    
    # Method: Use Coingecko as proxy for Chainlink (they use similar feeds)
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true",
            timeout=5
        )
        data = resp.json()
        prices.chainlink["price"] = data.get("bitcoin", {}).get("usd", 0)
        prices.chainlink["change_pct"] = data.get("bitcoin", {}).get("usd_24h_change", 0)
    except:
        # Fallback to Binance
        try:
            resp = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5)
            data = resp.json()
            prices.chainlink["price"] = float(data.get("lastPrice", 0))
            prices.chainlink["change_pct"] = float(data.get("priceChangePercent", 0))
        except:
            pass


async def fetch_binance(symbol: str):
    """Fetch Binance price"""
    try:
        resp = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=5)
        prices.binance["price"] = float(resp.json().get("price", 0))
        
        # Get order book for bid/ask
        ob = requests.get(f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=5", timeout=5).json()
        prices.binance["bid"] = float(ob["bids"][0][0]) if ob.get("bids") else 0
        prices.binance["ask"] = float(ob["asks"][0][0]) if ob.get("asks") else 0
    except Exception as e:
        print(f"Binance error: {e}")


class TradingBot:
    """Integrated trading bot"""
    def __init__(self):
        self.client = None
        self.last_trade = 0
        
    def init(self):
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
        if not self.client:
            print("❌ CLOB not initialized")
            return
        
        if time.time() - self.last_trade < 300:
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


async def price_poller(symbol: str):
    """Poll all prices"""
    while True:
        await fetch_binance(symbol)
        await fetch_chainlink()
        prices.update()
        await asyncio.sleep(5)


async def trading_loop(state: feeds.State):
    """Background trading signals"""
    min_spread = 0.015  # 1.5%
    last_signal = None
    
    while True:
        if state.pm_up and state.pm_dn:
            # Spread from 50% baseline
            spread = state.pm_up - 0.5
            
            # Also compare to Chainlink 24h change as sentiment
            cl_change = prices.chainlink.get("change_pct", 0) / 100
            
            # Adjusted spread considering sentiment
            adjusted_spread = spread - (cl_change * 0.1)  # Small adjustment
            
            if adjusted_spread < -min_spread:
                signal = "BUY_UP"
            elif adjusted_spread > min_spread:
                signal = "BUY_DOWN"
            else:
                signal = "WAIT"
            
            if signal != last_signal and signal != "WAIT":
                last_signal = signal
                print(f"\n🎯 SIGNAL: {signal}")
                print(f"   PM UP: {state.pm_up*100:.1f}% | CL 24h: {cl_change*100:.1f}%")
                print(f"   Spread: {spread*100:+.1f}%\n")
        
        await asyncio.sleep(10)


async def print_prices():
    """Print price summary"""
    while True:
        try:
            print(f"\n{'='*50}")
            print(f"[PRICES @ {datetime.now().strftime('%H:%M:%S')}]")
            print(f"{'='*50}")
            print(f"  Binance: ${prices.binance['price']:,.2f}")
            print(f"           Bid: ${prices.binance['bid']:,.2f} | Ask: ${prices.binance['ask']:,.2f}")
            print(f"  Chainlink: ${prices.chainlink['price']:,.2f} ({prices.chainlink['change_pct']:+.1f}% 24h)")
            print(f"{'='*50}\n")
        except:
            pass
        await asyncio.sleep(30)


async def main():
    console.print("\n[bold magenta]═══ CRYPTO PREDICTION DASHBOARD ═══[/bold magenta]\n")

    if POLYMARKET_PRIVATE_KEY and trading_bot.init():
        console.print("  [green]✅ Trading enabled[/green]")
    else:
        console.print("  [yellow]⚠️ Set POLYMARKET_PRIVATE_KEY for trading[/yellow]")

    coin = pick("Select coin:", config.COINS)
    tf = pick("Select timeframe:", config.TIMEFRAMES)

    console.print(f"\n[bold green]Starting {coin} {tf} …[/bold green]\n")

    state = feeds.State()
    state.pm_up_id, state.pm_dn_id = feeds.fetch_pm_tokens(coin, tf)
    
    if state.pm_up_id:
        console.print(f"  [PM] Up   → {state.pm_up_id[:24]}…")
        console.print(f"  [PM] Down → {state.pm_dn_id[:24]}…")
        console.print("  [PM] Waiting for WebSocket data...\n")
    else:
        console.print("  [yellow][PM] no market for this coin/timeframe[/yellow]")

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
        price_poller(binance_sym),
        print_prices(),
    )


if __name__ == "__main__":
    asyncio.run(main())
