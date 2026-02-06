import asyncio
import json
import time

import requests
import websockets
from datetime import datetime, timezone, timedelta

import config


class State:
    def __init__(self):
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.mid: float = 0.0

        self.trades: list[dict] = []

        self.klines: list[dict] = []
        self.cur_kline: dict | None = None

        self.pm_up_id:  str | None = None
        self.pm_dn_id:  str | None = None
        self.pm_up:     float | None = None
        self.pm_dn:     float | None = None


OB_POLL_INTERVAL = 2


async def ob_poller(symbol: str, state: State):
    url = f"{config.BINANCE_REST}/depth"
    print(f"  [Binance OB] polling {symbol} every {OB_POLL_INTERVAL}s")
    while True:
        try:
            resp = requests.get(url, params={"symbol": symbol, "limit": 20}, timeout=3).json()
            state.bids = [(float(p), float(q)) for p, q in resp["bids"]]
            state.asks = [(float(p), float(q)) for p, q in resp["asks"]]
            if state.bids and state.asks:
                state.mid = (state.bids[0][0] + state.asks[0][0]) / 2
        except Exception:
            pass
        await asyncio.sleep(OB_POLL_INTERVAL)


async def binance_feed(symbol: str, kline_iv: str, state: State):
    sym = symbol.lower()
    streams = "/".join([
        f"{sym}@trade",
        f"{sym}@kline_{kline_iv}",
    ])
    url = f"{config.BINANCE_WS}?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                print(f"  [Binance WS] connected – {symbol}")
                while True:
                    data   = json.loads(await ws.recv())
                    stream = data.get("stream", "")
                    pay    = data["data"]

                    if "@trade" in stream:
                        state.trades.append({
                            "t":      pay["T"] / 1000.0,
                            "price":  float(pay["p"]),
                            "qty":    float(pay["q"]),
                            "is_buy": not pay["m"],
                        })
                        if len(state.trades) > 5000:
                            cut = time.time() - config.TRADE_TTL
                            state.trades = [t for t in state.trades if t["t"] >= cut]

                    elif "@kline" in stream:
                        k = pay["k"]
                        candle = {
                            "t": k["t"] / 1000.0,
                            "o": float(k["o"]), "h": float(k["h"]),
                            "l": float(k["l"]), "c": float(k["c"]),
                            "v": float(k["v"]),
                        }
                        state.cur_kline = candle
                        if k["x"]:
                            state.klines.append(candle)
                            state.klines = state.klines[-config.KLINE_MAX:]
        except Exception as e:
            print(f"  [Binance WS] error/reconnecting: {e}")
            await asyncio.sleep(5)


async def bootstrap(symbol: str, interval: str, state: State):
    resp = requests.get(
        f"{config.BINANCE_REST}/klines",
        params={"symbol": symbol, "interval": interval, "limit": config.KLINE_BOOT},
    ).json()
    state.klines = [
        {
            "t": r[0] / 1e3,
            "o": float(r[1]), "h": float(r[2]),
            "l": float(r[3]), "c": float(r[4]),
            "v": float(r[5]),
        }
        for r in resp
    ]
    print(f"  [Binance] loaded {len(state.klines)} historical candles")


_MONTHS = ["", "january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def _et_now() -> datetime:
    utc = datetime.now(timezone.utc)
    year = utc.year

    mar1_dow  = datetime(year, 3, 1).weekday()
    mar_sun   = 1 + (6 - mar1_dow) % 7
    dst_start = datetime(year, 3, mar_sun + 7, 2, 0, 0, tzinfo=timezone.utc)

    nov1_dow = datetime(year, 11, 1).weekday()
    nov_sun  = 1 + (6 - nov1_dow) % 7
    dst_end  = datetime(year, 11, nov_sun, 6, 0, 0, tzinfo=timezone.utc)

    offset = timedelta(hours=-4) if dst_start <= utc < dst_end else timedelta(hours=-5)
    return utc + offset


def _to_12h(hour24: int) -> str:
    if hour24 == 0:
        return "12am"
    if hour24 < 12:
        return f"{hour24}am"
    if hour24 == 12:
        return "12pm"
    return f"{hour24 - 12}pm"


def _build_slug(coin: str, tf: str) -> str | None:
    now_utc = datetime.now(timezone.utc)
    now_ts  = int(now_utc.timestamp())
    et      = _et_now()

    if tf == "15m":
        ts = (now_ts // 900) * 900
        return f"{config.COIN_PM[coin]}-updown-15m-{ts}"

    if tf == "4h":
        ts = ((now_ts - 3600) // 14400) * 14400 + 3600
        return f"{config.COIN_PM[coin]}-updown-4h-{ts}"

    if tf == "1h":
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-"
                f"{_to_12h(et.hour)}-et") # Note: SolSt1ne original code had month/day logic here that was buggy or unused in snippet? 
        # Actually, let's keep original logic but wrap the WS part. 
        # Wait, I cannot see the original code for _build_slug in the snippet I am replacing.
        # I am replacing lines 47-196.
        # I MUST be careful not to break _build_slug or other helpers.
        # The previous view_file output showed _build_slug at lines 134-157.
        # I should primarily target binance_feed (47-85) and pm_feed (176-196).
        # Multi-replace is better here to avoid rewriting the helpers.


async def bootstrap(symbol: str, interval: str, state: State):
    resp = requests.get(
        f"{config.BINANCE_REST}/klines",
        params={"symbol": symbol, "interval": interval, "limit": config.KLINE_BOOT},
    ).json()
    state.klines = [
        {
            "t": r[0] / 1e3,
            "o": float(r[1]), "h": float(r[2]),
            "l": float(r[3]), "c": float(r[4]),
            "v": float(r[5]),
        }
        for r in resp
    ]
    print(f"  [Binance] loaded {len(state.klines)} historical candles")


_MONTHS = ["", "january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def _et_now() -> datetime:
    utc = datetime.now(timezone.utc)
    year = utc.year

    mar1_dow  = datetime(year, 3, 1).weekday()
    mar_sun   = 1 + (6 - mar1_dow) % 7
    dst_start = datetime(year, 3, mar_sun + 7, 2, 0, 0, tzinfo=timezone.utc)

    nov1_dow = datetime(year, 11, 1).weekday()
    nov_sun  = 1 + (6 - nov1_dow) % 7
    dst_end  = datetime(year, 11, nov_sun, 6, 0, 0, tzinfo=timezone.utc)

    offset = timedelta(hours=-4) if dst_start <= utc < dst_end else timedelta(hours=-5)
    return utc + offset


def _to_12h(hour24: int) -> str:
    if hour24 == 0:
        return "12am"
    if hour24 < 12:
        return f"{hour24}am"
    if hour24 == 12:
        return "12pm"
    return f"{hour24 - 12}pm"


def _build_slug(coin: str, tf: str) -> str | None:
    now_utc = datetime.now(timezone.utc)
    now_ts  = int(now_utc.timestamp())
    et      = _et_now()

    if tf == "15m":
        ts = (now_ts // 900) * 900
        return f"{config.COIN_PM[coin]}-updown-15m-{ts}"

    if tf == "4h":
        ts = ((now_ts - 3600) // 14400) * 14400 + 3600
        return f"{config.COIN_PM[coin]}-updown-4h-{ts}"

    if tf == "1h":
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-"
                f"{_MONTHS[et.month]}-{et.day}-{_to_12h(et.hour)}-et")

    if tf == "daily":
        resolution = et.replace(hour=12, minute=0, second=0, microsecond=0)
        target      = et if et < resolution else et + timedelta(days=1)
        return (f"{config.COIN_PM_LONG[coin]}-up-or-down-on-"
                f"{_MONTHS[target.month]}-{target.day}")

    return None


def fetch_pm_tokens(coin: str, tf: str) -> tuple:
    slug = _build_slug(coin, tf)
    if slug is None:
        return None, None
    try:
        data = requests.get(config.PM_GAMMA, params={"slug": slug, "limit": 1}).json()
        if not data or data[0].get("ticker") != slug:
            print(f"  [PM] no active market for slug: {slug}")
            return None, None
        ids = json.loads(data[0]["markets"][0]["clobTokenIds"])
        return ids[0], ids[1]
    except Exception as e:
        print(f"  [PM] token fetch failed ({slug}): {e}")
        return None, None


async def pm_feed(state: State):
    if not state.pm_up_id:
        print("  [PM] no tokens for this coin/timeframe – skipped")
        return

    assets = [state.pm_up_id, state.pm_dn_id]
    
    while True:
        try:
            async with websockets.connect(config.PM_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
                print("  [PM] connected")
                while True:
                    raw = json.loads(await ws.recv())

                    if isinstance(raw, list):
                        for entry in raw:
                            _pm_apply(entry.get("asset_id"), entry.get("asks", []), state)

                    elif isinstance(raw, dict) and raw.get("event_type") == "price_change":
                        for ch in raw.get("price_changes", []):
                            if ch.get("best_ask"):
                                _pm_set(ch["asset_id"], float(ch["best_ask"]), state)
        except Exception as e:
            print(f"  [PM] error/reconnecting: {e}")
            await asyncio.sleep(5)


def _pm_apply(asset, asks, state):
    if asks:
        _pm_set(asset, min(float(a["price"]) for a in asks), state)


def _pm_set(asset, price, state):
    if asset == state.pm_up_id:
        state.pm_up = price
    elif asset == state.pm_dn_id:
        state.pm_dn = price
