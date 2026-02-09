"""
Crypto Data Fetcher - Real-time via WebSocket
Fetches: Binance + Polymarket CLOB prices via WebSocket
"""
import asyncio
import json
import time
import requests
import websockets

BINANCE_WS = "wss://stream.binance.com:9443/ws"
BINANCE_REST = "https://api.binance.com/api/v3"
PM_WS = "wss://ws-polymarket.ngrok.dev"


class MarketData:
    def __init__(self):
        self.binance = {"price": 0, "bid": 0, "ask": 0, "volume": 0}
        self.polymarket = {"up_price": 0, "down_price": 0, "up_id": "", "down_id": ""}
        self.spread = {"pct": 0, "signal": "WAIT"}
        self.timestamp = 0


async def binance_stream(symbol: str, callback):
    """Stream Binance price updates"""
    async with websockets.connect(f"{BINANCE_WS}/{symbol.lower()}@ticker") as ws:
        async for msg in ws:
            data = json.loads(msg)
            callback({
                "price": float(data.get("c", 0)),
                "bid": float(data.get("b", 0)),
                "ask": float(data.get("a", 0)),
                "volume": float(data.get("v", 0)),
                "timestamp": time.time()
            })


async def polymarket_stream(token_ids: list, callback):
    """Stream Polymarket prices via WebSocket"""
    if not token_ids:
        return
    
    async with websockets.connect(PM_WS, ping_interval=20) as ws:
        await ws.send(json.dumps({
            "assets_ids": token_ids,
            "type": "market"
        }))
        
        async for msg in ws:
            data = json.loads(msg)
            
            if isinstance(data, list):
                for entry in data:
                    asset = entry.get("asset_id")
                    price = float(entry.get("asks", [{}])[0].get("price", 0))
                    callback({"asset": asset, "price": price, "type": "list"})
            
            elif isinstance(data, dict) and data.get("event_type") == "price_change":
                for change in data.get("price_changes", []):
                    callback({
                        "asset": change.get("asset_id"),
                        "price": float(change.get("best_ask", 0)),
                        "type": "update"
                    })


async def fetch_binance_rest(symbol: str) -> dict:
    """REST fallback for Binance"""
    try:
        ticker = requests.get(f"{BINANCE_REST}/ticker/24hr", params={"symbol": symbol}, timeout=5).json()
        return {
            "price": float(ticker.get("lastPrice", 0)),
            "volume": float(ticker.get("quoteVolume", 0)),
            "timestamp": time.time()
        }
    except:
        return {"price": 0, "volume": 0, "timestamp": time.time()}


def build_slug(coin: str, timeframe: str = "15m") -> str:
    """Build Polymarket slug from coin/timeframe"""
    ts = int(time.time())
    
    if timeframe == "15m":
        return f"{coin}-updown-15m-{ts // 900 * 900}"
    elif timeframe == "4h":
        return f"{coin}-updown-4h-{ts // 14400 * 14400}"
    elif timeframe == "1h":
        return f"{coin}-up-or-down-1h-{ts // 3600 * 3600}"
    
    return f"{coin}-updown-15m-{ts // 900 * 900}"


async def get_polymarket_tokens(slug: str) -> dict:
    """Get UP/DOWN token IDs from web page"""
    try:
        resp = requests.get(f"https://polymarket.com/event/{slug}", timeout=5)
        html = resp.text
        
        # Extract token IDs
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
        print(f"Token fetch error: {e}")
    return {"up_id": "", "down_id": "", "up_price": 0, "down_price": 0}


class DataStream:
    def __init__(self, coin: str = "BTC", timeframe: str = "15m"):
        self.coin = coin
        self.timeframe = timeframe
        self.data = MarketData()
        self.running = False
        
    async def start(self):
        """Start all data streams"""
        self.running = True
        symbol = f"{self.coin}USDT"
        slug = build_slug(self.coin, self.timeframe)
        
        # Get Polymarket tokens
        pm_data = await get_polymarket_tokens(slug)
        token_ids = [pm_data["up_id"], pm_data["down_id"]]
        
        print(f"\n{'='*60}")
        print(f"Streaming {self.coin} | {self.timeframe} | {slug}")
        print(f"{'='*60}\n")
        
        # Run streams concurrently
        await asyncio.gather(
            self._binance_loop(symbol),
            self._polymarket_loop(token_ids)
        )
    
    async def _binance_loop(self, symbol: str):
        """Binance price stream"""
        try:
            async with websockets.connect(f"{BINANCE_WS}/{symbol.lower()}@ticker") as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    self.data.binance = {
                        "price": float(data.get("c", 0)),
                        "bid": float(data.get("b", 0)),
                        "ask": float(data.get("a", 0)),
                        "volume": float(data.get("v", 0)),
                    }
                    self._check_signal()
        except Exception as e:
            print(f"Binance WS error: {e}")
    
    async def _polymarket_loop(self, token_ids: list):
        """Polymarket price stream"""
        if not token_ids or not token_ids[0]:
            print("No Polymarket tokens found")
            return
            
        try:
            async with websockets.connect(PM_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "assets_ids": token_ids,
                    "type": "market"
                }))
                
                async for msg in ws:
                    data = json.loads(msg)
                    
                    if isinstance(data, list):
                        for entry in data:
                            asset = entry.get("asset_id")
                            price = float(entry.get("asks", [{}])[0].get("price", 0))
                            self._update_pm_price(asset, price)
                    
                    elif isinstance(data, dict) and data.get("event_type") == "price_change":
                        for change in data.get("price_changes", []):
                            self._update_pm_price(
                                change.get("asset_id"),
                                float(change.get("best_ask", 0))
                            )
        except Exception as e:
            print(f"Polymarket WS error: {e}")
    
    def _update_pm_price(self, asset: str, price: float):
        if asset == self.data.polymarket.get("up_id"):
            self.data.polymarket["up_price"] = price
        elif asset == self.data.polymarket.get("down_id"):
            self.data.polymarket["down_price"] = price
        self._check_signal()
    
    def _check_signal(self):
        """Calculate spread signal"""
        binance_p = self.data.binance.get("price", 0)
        pm_up = self.data.polymarket.get("up_price", 0)
        
        if binance_p > 0 and pm_up > 0:
            # Simple model: UP price = implied probability
            spread_pct = (pm_up - 0.5) * 100
            
            self.data.spread = {
                "binance": binance_p,
                "pm_up": pm_up,
                "pm_down": self.data.polymarket.get("down_price", 0),
                "pct": round(spread_pct, 2),
                "signal": "BUY" if pm_up < 0.48 else "SELL" if pm_up > 0.52 else "WAIT"
            }
            self.data.timestamp = time.time()
            
            # Print status
            print(f"[{time.strftime('%H:%M:%S')}]")
            print(f"  Binance: ${self.data.spread['binance']:,.2f}")
            print(f"  PM UP:   {self.data.spread['pm_up']*100:.1f}% | DOWN: {self.data.spread['pm_down']*100:.1f}%")
            print(f"  Signal:  {self.data.spread['signal']} ({self.data.spread['pct']:+.1f}%)\n")


async def main():
    stream = DataStream("BTC", "15m")
    await stream.start()

if __name__ == "__main__":
    asyncio.run(main())
