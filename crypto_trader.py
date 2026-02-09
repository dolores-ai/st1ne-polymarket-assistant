#!/usr/bin/env python3
"""
Crypto Trading Bot
Real-time signals + Auto-execution on Polymarket
"""
import os
import sys
import json
import time
import asyncio
import requests
import websockets
from datetime import datetime

# Configuration
CONFIG = {
    "binance": {
        "ws": "wss://stream.binance.com:9443/ws",
        "rest": "https://api.binance.com/api/v3"
    },
    "polymarket": {
        "ws": "wss://ws-polymarket.ngrok.dev",
        "gamma": "https://gamma-api.polymarket.com/markets"
    },
    "trading": {
        "min_spread": 0.015,  # 1.5% spread to trade
        "min_size": 5.0,       # $5 minimum
        "max_size": 50.0,      # $50 maximum
        "proxy_url": os.environ.get("POLYMARKET_PROXY", ""),
        "wallet": os.environ.get("POLYMARKET_WALLET", ""),
    },
    "coins": ["BTC", "ETH", "SOL"],
    "timeframes": ["15m", "1h", "4h"]
}

# Polymarket CLOB Client
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions


class TradingBot:
    def __init__(self, coin: str = "BTC", timeframe: str = "15m"):
        self.coin = coin
        self.timeframe = timeframe
        self.symbol = f"{coin}USDT"
        self.data = {
            "binance": {"price": 0, "bid": 0, "ask": 0},
            "polymarket": {"up_id": "", "down_id": "", "up_price": 0, "down_price": 0},
            "signal": "WAIT",
            "spread_pct": 0,
            "last_trade": 0
        }
        self.client = None
        self.running = False
        
    def init_clob(self):
        """Initialize Polymarket CLOB client"""
        if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
            print("⚠️ POLYMARKET_PRIVATE_KEY not set")
            return
            
        private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
        wallet = CONFIG["trading"]["wallet"]
        
        try:
            # Setup proxy if configured
            proxy_url = CONFIG["trading"]["proxy_url"]
            if proxy_url:
                from httpx_socks import SyncProxyTransport
                import httpx
                transport = SyncProxyTransport.from_url(proxy_url)
                http_client = httpx.Client(transport=transport, http2=True, timeout=30)
                
                import py_clob_client.http_helpers.helpers as clob_helpers
                clob_helpers._http_client = http_client
            
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=private_key
            )
            creds = client.derive_api_key()
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=private_key,
                creds=creds,
                signature_type=1,
                funder=wallet
            )
            print(f"✅ CLOB client initialized")
        except Exception as e:
            print(f"❌ CLOB init error: {e}")
    
    def build_slug(self) -> str:
        """Build Polymarket slug"""
        ts = int(time.time())
        
        if self.timeframe == "15m":
            return f"{self.coin}-updown-15m-{ts // 900 * 900}"
        elif self.timeframe == "4h":
            return f"{self.coin}-updown-4h-{ts // 14400 * 14400}"
        elif self.timeframe == "1h":
            return f"{self.coin}-up-or-down-1h-{ts // 3600 * 3600}"
        
        return f"{self.coin}-updown-15m-{ts // 900 * 900}"
    
    def get_pm_tokens(self, slug: str) -> dict:
        """Get Polymarket token IDs from web"""
        try:
            resp = requests.get(f"https://polymarket.com/event/{slug}", timeout=5)
            html = resp.text
            
            import re
            tokens_match = re.search(r'"clobTokenIds":\[([^\]]+)\]', html)
            prices_match = re.search(r'"outcomePrices":\[([^\]]+)\]', html)
            
            if tokens_match and prices_match:
                tokens = json.loads(f"[{tokens_match.group(1)}]")
                prices = json.loads(f"[{prices_match.group(1)}]")
                return {
                    "up_id": tokens[0],
                    "down_id": tokens[1],
                    "up_price": float(prices[0]),
                    "down_price": float(prices[1])
                }
        except Exception as e:
            print(f"PM token error: {e}")
        return {"up_id": "", "down_id": "", "up_price": 0, "down_price": 0}
    
    async def binance_stream(self):
        """Stream Binance prices"""
        try:
            async with websockets.connect(f"{CONFIG['binance']['ws']}/{self.symbol.lower()}@ticker") as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    self.data["binance"] = {
                        "price": float(data.get("c", 0)),
                        "bid": float(data.get("b", 0)),
                        "ask": float(data.get("a", 0))
                    }
                    self.analyze()
        except Exception as e:
            print(f"Binance WS error: {e}")
    
    async def polymarket_stream(self, token_ids: list):
        """Stream Polymarket prices"""
        if not token_ids or not token_ids[0]:
            return
            
        try:
            async with websockets.connect(CONFIG["polymarket"]["ws"], ping_interval=20) as ws:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                
                async for msg in ws:
                    data = json.loads(msg)
                    
                    if isinstance(data, list):
                        for entry in data:
                            asset = entry.get("asset_id")
                            price = float(entry.get("asks", [{}])[0].get("price", 0))
                            self.update_pm_price(asset, price)
                    
                    elif isinstance(data, dict) and data.get("event_type") == "price_change":
                        for change in data.get("price_changes", []):
                            self.update_pm_price(
                                change.get("asset_id"),
                                float(change.get("best_ask", 0))
                            )
                    self.analyze()
        except Exception as e:
            print(f"Polymarket WS error: {e}")
    
    def update_pm_price(self, asset: str, price: float):
        """Update Polymarket prices"""
        if asset == self.data["polymarket"]["up_id"]:
            self.data["polymarket"]["up_price"] = price
        elif asset == self.data["polymarket"]["down_id"]:
            self.data["polymarket"]["down_price"] = price
    
    def analyze(self):
        """Analyze spread and generate signal"""
        binance_p = self.data["binance"]["price"]
        pm_up = self.data["polymarket"]["up_price"]
        pm_down = self.data["polymarket"]["down_price"]
        
        if binance_p > 0 and pm_up > 0:
            # Calculate spread from 50% baseline
            spread_pct = (pm_up - 0.5) * 100
            self.data["spread_pct"] = spread_pct
            
            # Generate signal
            if spread_pct < -CONFIG["trading"]["min_spread"] * 100:
                signal = "BUY UP"  # Underpriced
            elif spread_pct > CONFIG["trading"]["min_spread"] * 100:
                signal = "BUY DOWN"  # Overpriced
            else:
                signal = "WAIT"
            
            self.data["signal"] = signal
            self.print_status()
    
    def print_status(self):
        """Print current status"""
        ts = datetime.now().strftime("%H:%M:%S")
        b = self.data["binance"]
        p = self.data["polymarket"]
        s = self.data["signal"]
        spread = self.data["spread_pct"]
        
        print(f"[{ts}] {self.coin}/{self.timeframe}")
        print(f"  Binance: ${b['price']:,.2f} | Bid: ${b['bid']:.2f} | Ask: ${b['ask']:.2f}")
        print(f"  PM UP:   {p['up_price']*100:.1f}% | DOWN: {p['down_price']*100:.1f}%")
        print(f"  Signal:  {s} ({spread:+.1f}%)\n")
    
    def execute_trade(self, side: str = "BUY"):
        """Execute trade on Polymarket"""
        if not self.client:
            print("❌ CLOB client not initialized")
            return
        
        if self.data["signal"] == "WAIT":
            return
        
        # Check cooldown
        if time.time() - self.data["last_trade"] < 300:  # 5 min cooldown
            return
        
        # Determine token and price
        if self.data["signal"] == "BUY UP":
            token_id = self.data["polymarket"]["up_id"]
            price = self.data["polymarket"]["up_price"]
        elif self.data["signal"] == "BUY DOWN":
            token_id = self.data["polymarket"]["down_id"]
            price = self.data["polymarket"]["down_price"]
        else:
            return
        
        # Calculate size
        size = round(CONFIG["trading"]["min_size"] / price, 2)
        
        if size * price < CONFIG["trading"]["min_size"]:
            size = round(CONFIG["trading"]["min_size"] / price, 2)
        
        print(f"\n{'='*50}")
        print(f"🚀 EXECUTING TRADE")
        print(f"{'='*50}")
        print(f"  Token: {token_id[:20]}...")
        print(f"  Side:  {side}")
        print(f"  Price: ${price:.4f}")
        print(f"  Size:  {size} shares (${size * price:.2f})")
        print(f"{'='*50}\n")
        
        try:
            result = self.client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=size, side=side),
                PartialCreateOrderOptions(tick_size="0.01")
            )
            
            if result.get("success"):
                print(f"✅ Order placed: {result.get('orderID', 'N/A')}")
                self.data["last_trade"] = time.time()
            else:
                print(f"❌ Order failed: {result}")
        except Exception as e:
            print(f"❌ Trade error: {e}")
    
    async def start(self, auto_trade: bool = False):
        """Start the trading bot"""
        self.init_clob()
        self.running = True
        
        slug = self.build_slug()
        pm_data = self.get_pm_tokens(slug)
        self.data["polymarket"] = pm_data
        
        token_ids = [pm_data["up_id"], pm_data["down_id"]]
        
        print(f"\n{'='*60}")
        print(f"🤖 Crypto Trading Bot")
        print(f"{'='*60}")
        print(f"  Coin:      {self.coin}")
        print(f"  Timeframe: {self.timeframe}")
        print(f"  Slug:      {slug}")
        print(f"  Auto-trade: {'ON' if auto_trade else 'OFF'}")
        print(f"{'='*60}\n")
        
        # Start streams
        await asyncio.gather(
            self.binance_stream(),
            self.polymarket_stream(token_ids)
        )


def main():
    """CLI entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Crypto Trading Bot")
    parser.add_argument("--coin", "-c", default="BTC", help="Coin (BTC, ETH, SOL)")
    parser.add_argument("--timeframe", "-t", default="15m", help="Timeframe (15m, 1h, 4h)")
    parser.add_argument("--auto", "-a", action="store_true", help="Enable auto-trading")
    parser.add_argument("--trade", choices=["BUY_UP", "BUY_DOWN"], help="Manual trade")
    
    args = parser.parse_args()
    
    bot = TradingBot(args.coin, args.timeframe)
    
    if args.trade:
        bot.init_clob()
        bot.get_pm_tokens(bot.build_slug())
        if args.trade == "BUY_UP":
            bot.data["signal"] = "BUY UP"
            bot.execute_trade("BUY")
        else:
            bot.data["signal"] = "BUY DOWN"
            bot.execute_trade("BUY")
    else:
        asyncio.run(bot.start(auto_trade=args.auto))


if __name__ == "__main__":
    main()
