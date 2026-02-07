#!/usr/bin/env python3
"""
Scalping strategy backtest — exits BEFORE settlement using TP/SL.

The key difference from backtest_all_strategies.py: instead of holding to binary
settlement (0 or 1), we exit within the period when price hits take-profit or
stop-loss targets. This fixes the brutal P&L asymmetry that killed the momentum bot.

Strategy families:
  1. Momentum Scalp — existing minute-7 signal but with TP/SL exits
  2. Early Cheap Scalp — buy cheap (0.30-0.50) in minutes 0-3, take profit
  3. Mean Reversion Scalp — buy after overshoot, scalp the bounce
  4. Volatility Spike Scalp — buy very cheap (0.05-0.20), target big swing
  5. Range-Bound Trader — detect oscillation, trade the range
  6. Support/Resistance — trade bounces off within-period levels

Usage:
  python scripts/backtest_scalp.py [--hours 96] [--coin BTC]
"""

import argparse
import json
import math
import requests
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────

PM_PAST_RESULTS = "https://polymarket.com/api/past-results"
PM_GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_PRICES = "https://clob.polymarket.com/prices-history"
HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── Data Collection (reused from backtest_all_strategies.py) ────

def fetch_oracle_periods(coin: str, hours: int) -> list[dict]:
    all_results = []
    seen = set()
    now = datetime.now(timezone.utc)
    total_steps = hours * 4 + 1
    for step in range(total_steps):
        dt = now - timedelta(hours=hours) + timedelta(minutes=step * 15)
        ts_aligned = (int(dt.timestamp()) // 900) * 900
        dt_aligned = datetime.fromtimestamp(ts_aligned, tz=timezone.utc)
        start_iso = dt_aligned.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resp = requests.get(PM_PAST_RESULTS, params={
                "symbol": coin, "variant": "fifteen", "assetType": "crypto",
                "currentEventStartTime": start_iso,
            }, timeout=5, headers=HEADERS)
            if resp.status_code == 200:
                for r in resp.json().get("data", {}).get("results", []):
                    key = r["startTime"]
                    if key not in seen:
                        seen.add(key)
                        all_results.append(r)
        except Exception:
            pass
    all_results.sort(key=lambda x: x["startTime"])
    return all_results


def fetch_pm_token_for_period(coin: str, period_start_ts: int) -> tuple[str | None, str | None]:
    slug = f"{coin.lower()}-updown-15m-{period_start_ts}"
    try:
        resp = requests.get(PM_GAMMA, params={"slug": slug, "limit": 1}, timeout=5)
        data = resp.json()
        if data and data[0].get("markets"):
            m = data[0]["markets"][0]
            ids = json.loads(m["clobTokenIds"])
            return ids[0], ids[1]
    except Exception:
        pass
    return None, None


def fetch_prices_history(token_id: str, start_ts: int, end_ts: int) -> list[dict]:
    try:
        resp = requests.get(CLOB_PRICES, params={
            "market": token_id, "startTs": start_ts,
            "endTs": end_ts, "fidelity": 1,
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "history" in data:
                return [{"t": int(h["t"]), "p": float(h["p"])} for h in data["history"]]
    except Exception:
        pass
    return []


# ── Core: Within-Period Exit Simulator ───────────────────────────

def simulate_exit(
    prices_up: list[dict],
    period_start: int,
    side: str,
    entry_price: float,
    entry_minute: float,
    tp_delta: float,
    sl_delta: float,
    exit_deadline: float = 14.0,
    hold_to_settlement: bool = False,
    outcome: str = "",
) -> dict:
    """
    Walk forward through price data from entry point.
    Exit when TP or SL is hit, or at deadline.

    Returns dict with: exit_type, exit_price, exit_minute, pnl
    """
    tp_target = entry_price + tp_delta
    sl_target = entry_price - sl_delta

    # Walk through prices after entry
    for p in prices_up:
        minute = (p["t"] - period_start) / 60.0
        if minute <= entry_minute + 0.3:  # skip prices at or before entry
            continue
        if minute > 15.0:
            break

        # Compute position price
        if side == "Up":
            pos_price = p["p"]
        else:
            pos_price = 1.0 - p["p"]

        # Check take-profit
        if pos_price >= tp_target:
            return {
                "exit_type": "tp",
                "exit_price": tp_target,  # assume fill at target
                "exit_minute": minute,
                "pnl": tp_delta,
            }

        # Check stop-loss
        if pos_price <= sl_target:
            return {
                "exit_type": "sl",
                "exit_price": sl_target,
                "exit_minute": minute,
                "pnl": -sl_delta,
            }

        # Check deadline
        if minute >= exit_deadline:
            pnl = pos_price - entry_price
            return {
                "exit_type": "deadline",
                "exit_price": pos_price,
                "exit_minute": minute,
                "pnl": pnl,
            }

    # Fell through — hold to settlement as last resort
    if hold_to_settlement and outcome:
        won = (side == "Up" and outcome == "up") or (side == "Down" and outcome == "down")
        settlement = 1.0 if won else 0.0
        return {
            "exit_type": "settlement",
            "exit_price": settlement,
            "exit_minute": 15.0,
            "pnl": settlement - entry_price,
        }

    # No data after entry — use last available price
    if prices_up:
        last_p = prices_up[-1]["p"]
        pos_price = last_p if side == "Up" else 1.0 - last_p
        return {
            "exit_type": "eod",
            "exit_price": pos_price,
            "exit_minute": 14.5,
            "pnl": pos_price - entry_price,
        }

    return {"exit_type": "no_data", "exit_price": entry_price, "exit_minute": entry_minute, "pnl": 0}


# ── Helper Functions ──────────────────────────────────────────────

def get_price_at_minute(prices, period_start, minute):
    target_ts = period_start + minute * 60
    best, best_dist = None, float("inf")
    for p in prices:
        dist = abs(p["t"] - target_ts)
        if dist < best_dist:
            best_dist = dist
            best = p["p"]
    return best if best_dist <= 90 else None


def get_prices_in_window(prices, period_start, min_start, min_end):
    ws = period_start + min_start * 60
    we = period_start + min_end * 60
    return [(((p["t"] - period_start) / 60.0), p["p"]) for p in prices if ws <= p["t"] <= we]


# ── Strategy Families ─────────────────────────────────────────────

def strat_momentum_scalp(prices_up, period_start, outcome, params):
    """Strategy 1: Same momentum signal but with TP/SL exits."""
    entry_min = params["entry_minute"]
    threshold = params["threshold"]
    lookback = params["lookback"]
    tp = params["tp_delta"]
    sl = params["sl_delta"]
    deadline = params.get("deadline", 14.0)
    hold_settle = params.get("hold_settlement", False)

    p_now = get_price_at_minute(prices_up, period_start, entry_min)
    p_before = get_price_at_minute(prices_up, period_start, entry_min - lookback)
    if p_now is None or p_before is None:
        return None

    # Trend confirm: leading side above threshold and trending
    side = None
    entry_price = None
    if p_now >= threshold and p_now > p_before:
        side, entry_price = "Up", p_now
    elif (1.0 - p_now) >= threshold and p_now < p_before:
        side, entry_price = "Down", 1.0 - p_now

    if side is None:
        return None

    exit_info = simulate_exit(prices_up, period_start, side, entry_price,
                              entry_min, tp, sl, deadline, hold_settle, outcome)
    return {
        "side": side, "entry_price": entry_price, "entry_minute": entry_min,
        "outcome": outcome, **exit_info,
    }


def strat_early_cheap_scalp(prices_up, period_start, outcome, params):
    """Strategy 2: Buy cheap side early (minutes 0-3), scalp for profit."""
    window_end = params["window_end"]
    max_entry = params["max_entry"]  # only buy if below this (e.g. 0.45)
    min_entry = params.get("min_entry", 0.10)
    tp = params["tp_delta"]
    sl = params["sl_delta"]
    deadline = params.get("deadline", 10.0)

    pts = get_prices_in_window(prices_up, period_start, 0, window_end)
    if len(pts) < 2:
        return None

    # Use latest price in window for entry
    pts.sort(key=lambda x: x[0])
    entry_min, price_up = pts[-1]
    price_dn = 1.0 - price_up

    # Buy whichever side is cheaper and within range
    side = None
    entry_price = None
    if price_up <= max_entry and price_up >= min_entry and price_up < price_dn:
        side, entry_price = "Up", price_up
    elif price_dn <= max_entry and price_dn >= min_entry and price_dn <= price_up:
        side, entry_price = "Down", price_dn

    if side is None:
        return None

    exit_info = simulate_exit(prices_up, period_start, side, entry_price,
                              entry_min, tp, sl, deadline, False, outcome)
    return {
        "side": side, "entry_price": entry_price, "entry_minute": entry_min,
        "outcome": outcome, **exit_info,
    }


def strat_mean_reversion_scalp(prices_up, period_start, outcome, params):
    """Strategy 3: After a fast move, buy the cheap side and scalp the bounce."""
    obs_end = params["obs_end"]
    lookback = params["lookback"]
    min_move = params["min_move"]  # minimum price move to trigger
    max_entry = params["max_entry"]
    tp = params["tp_delta"]
    sl = params["sl_delta"]
    deadline = params.get("deadline", 12.0)

    p_now = get_price_at_minute(prices_up, period_start, obs_end)
    p_before = get_price_at_minute(prices_up, period_start, obs_end - lookback)
    if p_now is None or p_before is None:
        return None

    move_up = p_now - p_before  # positive = Up side got more expensive

    side = None
    entry_price = None

    # If Up surged (move_up > min_move), buy Down (mean reversion)
    if move_up >= min_move:
        dn_price = 1.0 - p_now
        if dn_price <= max_entry and dn_price >= 0.05:
            side, entry_price = "Down", dn_price

    # If Down surged (move_up < -min_move), buy Up
    elif move_up <= -min_move:
        if p_now <= max_entry and p_now >= 0.05:
            side, entry_price = "Up", p_now

    if side is None:
        return None

    exit_info = simulate_exit(prices_up, period_start, side, entry_price,
                              obs_end, tp, sl, deadline, False, outcome)
    return {
        "side": side, "entry_price": entry_price, "entry_minute": obs_end,
        "outcome": outcome, **exit_info,
    }


def strat_vol_spike_scalp(prices_up, period_start, outcome, params):
    """Strategy 4: Buy very cheap in volatile periods, target big swing."""
    entry_window = params["entry_window"]
    max_entry = params["max_entry"]  # buy only if very cheap (e.g. 0.15)
    min_range = params["min_range"]  # minimum observed range to confirm volatility
    tp = params["tp_delta"]  # large TP target
    deadline = params.get("deadline", 14.0)
    hold_settle = params.get("hold_settlement", True)

    pts = get_prices_in_window(prices_up, period_start, 0, entry_window)
    if len(pts) < 3:
        return None

    # Check volatility: range of prices seen so far
    prices_only = [p for _, p in pts]
    observed_range = max(prices_only) - min(prices_only)
    if observed_range < min_range:
        return None

    # Entry: use latest price, buy cheapest side if below max_entry
    pts.sort(key=lambda x: x[0])
    entry_min, price_up = pts[-1]
    price_dn = 1.0 - price_up

    side = None
    entry_price = None
    if price_up <= max_entry and price_up <= price_dn:
        side, entry_price = "Up", price_up
    elif price_dn <= max_entry and price_dn < price_up:
        side, entry_price = "Down", price_dn

    if side is None:
        return None

    # No stop-loss for this strategy — cost is already low
    exit_info = simulate_exit(prices_up, period_start, side, entry_price,
                              entry_min, tp, entry_price * 0.9,  # SL at 90% loss
                              deadline, hold_settle, outcome)
    return {
        "side": side, "entry_price": entry_price, "entry_minute": entry_min,
        "outcome": outcome, **exit_info,
    }


def strat_range_trader(prices_up, period_start, outcome, params):
    """Strategy 5: Detect range-bound periods, trade the range."""
    obs_end = params["obs_end"]
    min_range = params["min_range"]
    max_range = params["max_range"]
    tp = params["tp_delta"]
    sl = params["sl_delta"]
    deadline = params.get("deadline", 12.0)

    pts = get_prices_in_window(prices_up, period_start, 0, obs_end)
    if len(pts) < 4:
        return None

    prices_only = [p for _, p in pts]
    p_range = max(prices_only) - min(prices_only)
    if p_range < min_range or p_range > max_range:
        return None

    # Check for oscillation: did price cross the midpoint at least once?
    midpoint = (max(prices_only) + min(prices_only)) / 2
    crosses = 0
    for i in range(1, len(prices_only)):
        if (prices_only[i-1] < midpoint and prices_only[i] >= midpoint) or \
           (prices_only[i-1] >= midpoint and prices_only[i] < midpoint):
            crosses += 1
    if crosses < 1:
        return None

    # Entry: buy the cheap side at the bottom of the range
    pts.sort(key=lambda x: x[0])
    entry_min, price_up = pts[-1]
    price_dn = 1.0 - price_up

    range_low_up = min(prices_only)
    range_low_dn = 1.0 - max(prices_only)

    side = None
    entry_price = None

    # Buy whichever side is closer to its range bottom
    up_from_bottom = price_up - range_low_up
    dn_from_bottom = price_dn - range_low_dn

    if up_from_bottom < dn_from_bottom and price_up < 0.50:
        side, entry_price = "Up", price_up
    elif price_dn < 0.50:
        side, entry_price = "Down", price_dn

    if side is None:
        return None

    exit_info = simulate_exit(prices_up, period_start, side, entry_price,
                              entry_min, tp, sl, deadline, False, outcome)
    return {
        "side": side, "entry_price": entry_price, "entry_minute": entry_min,
        "outcome": outcome, **exit_info,
    }


def strat_sr_bounce(prices_up, period_start, outcome, params):
    """Strategy 6: Support/resistance bounce trading."""
    obs_start = params["obs_start"]
    obs_end = params["obs_end"]
    proximity = params["proximity"]  # how close to S/R to trigger
    tp = params["tp_delta"]
    sl = params["sl_delta"]
    deadline = params.get("deadline", 12.0)

    # Build S/R from early prices
    early_pts = get_prices_in_window(prices_up, period_start, 0, obs_start)
    if len(early_pts) < 3:
        return None

    early_prices = [p for _, p in early_pts]
    support_up = min(early_prices)
    resist_up = max(early_prices)

    if resist_up - support_up < 0.05:
        return None  # not enough range

    # Look for entry in observation window: price near support
    entry_pts = get_prices_in_window(prices_up, period_start, obs_start, obs_end)
    if not entry_pts:
        return None

    entry_pts.sort(key=lambda x: x[0])

    for entry_min, price_up in entry_pts:
        price_dn = 1.0 - price_up

        # Up near support (buying Up at its low)
        if abs(price_up - support_up) <= proximity and price_up < 0.50:
            exit_info = simulate_exit(prices_up, period_start, "Up", price_up,
                                      entry_min, tp, sl, deadline, False, outcome)
            return {
                "side": "Up", "entry_price": price_up, "entry_minute": entry_min,
                "outcome": outcome, **exit_info,
            }

        # Down near its support (= Up near resistance)
        support_dn = 1.0 - resist_up
        if abs(price_dn - support_dn) <= proximity and price_dn < 0.50:
            exit_info = simulate_exit(prices_up, period_start, "Down", price_dn,
                                      entry_min, tp, sl, deadline, False, outcome)
            return {
                "side": "Down", "entry_price": price_dn, "entry_minute": entry_min,
                "outcome": outcome, **exit_info,
            }

    return None


# ── Strategy Registry ─────────────────────────────────────────────

def build_configs():
    configs = []

    # 1. MOMENTUM SCALP — existing signal + TP/SL
    for entry in [5, 7]:
        for lookback in [2, 3]:
            for th in [0.55, 0.60, 0.65]:
                for tp in [0.05, 0.08, 0.10, 0.15]:
                    for sl in [0.05, 0.08, 0.10]:
                        configs.append({
                            "name": f"mom_m{entry}_lb{lookback}_{th:.0%}_tp{tp:.2f}_sl{sl:.2f}",
                            "family": "momentum_scalp",
                            "fn": strat_momentum_scalp,
                            "params": {"entry_minute": entry, "lookback": lookback,
                                       "threshold": th, "tp_delta": tp, "sl_delta": sl},
                        })

    # 2. EARLY CHEAP SCALP
    for wend in [2, 3, 4]:
        for max_e in [0.35, 0.40, 0.45]:
            for tp in [0.05, 0.08, 0.10, 0.15]:
                for sl in [0.03, 0.05, 0.08]:
                    configs.append({
                        "name": f"early_w{wend}_{max_e:.0%}_tp{tp:.2f}_sl{sl:.2f}",
                        "family": "early_cheap",
                        "fn": strat_early_cheap_scalp,
                        "params": {"window_end": wend, "max_entry": max_e,
                                   "tp_delta": tp, "sl_delta": sl},
                    })

    # 3. MEAN REVERSION SCALP
    for obs in [5, 7, 9]:
        for lb in [2, 3, 4]:
            for move in [0.08, 0.10, 0.15]:
                for max_e in [0.25, 0.30, 0.35, 0.40]:
                    for tp in [0.05, 0.08, 0.10]:
                        for sl in [0.03, 0.05, 0.08]:
                            configs.append({
                                "name": f"meanrev_o{obs}_lb{lb}_mv{move:.2f}_{max_e:.0%}_tp{tp:.2f}_sl{sl:.2f}",
                                "family": "mean_reversion",
                                "fn": strat_mean_reversion_scalp,
                                "params": {"obs_end": obs, "lookback": lb,
                                           "min_move": move, "max_entry": max_e,
                                           "tp_delta": tp, "sl_delta": sl},
                            })

    # 4. VOLATILITY SPIKE SCALP
    for ew in [3, 5, 7]:
        for max_e in [0.10, 0.15, 0.20]:
            for min_r in [0.05, 0.10, 0.15]:
                for tp in [0.15, 0.25, 0.40, 0.60]:
                    for hold in [True, False]:
                        configs.append({
                            "name": f"volspike_w{ew}_{max_e:.0%}_r{min_r:.2f}_tp{tp:.2f}_h{int(hold)}",
                            "family": "vol_spike",
                            "fn": strat_vol_spike_scalp,
                            "params": {"entry_window": ew, "max_entry": max_e,
                                       "min_range": min_r, "tp_delta": tp,
                                       "hold_settlement": hold},
                        })

    # 5. RANGE-BOUND TRADER
    for obs in [4, 5, 6]:
        for min_r in [0.05, 0.08, 0.10]:
            for max_r in [0.15, 0.20, 0.30]:
                for tp in [0.05, 0.08, 0.10]:
                    for sl in [0.03, 0.05]:
                        if max_r <= min_r:
                            continue
                        configs.append({
                            "name": f"range_o{obs}_r{min_r:.2f}-{max_r:.2f}_tp{tp:.2f}_sl{sl:.2f}",
                            "family": "range_bound",
                            "fn": strat_range_trader,
                            "params": {"obs_end": obs, "min_range": min_r,
                                       "max_range": max_r, "tp_delta": tp, "sl_delta": sl},
                        })

    # 6. SUPPORT/RESISTANCE BOUNCE
    for obs_s in [3, 4, 5]:
        for obs_e in [6, 7, 8, 10]:
            if obs_e <= obs_s:
                continue
            for prox in [0.02, 0.03, 0.05]:
                for tp in [0.05, 0.08, 0.10]:
                    for sl in [0.03, 0.05]:
                        configs.append({
                            "name": f"sr_s{obs_s}_e{obs_e}_p{prox:.2f}_tp{tp:.2f}_sl{sl:.2f}",
                            "family": "support_resistance",
                            "fn": strat_sr_bounce,
                            "params": {"obs_start": obs_s, "obs_end": obs_e,
                                       "proximity": prox, "tp_delta": tp, "sl_delta": sl},
                        })

    return configs


# ── Reporting ─────────────────────────────────────────────────────

def compute_stats(trades):
    if not trades:
        return {"n": 0}

    n = len(trades)
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n
    mean_pnl = statistics.mean(pnls)
    std_pnl = statistics.stdev(pnls) if n > 1 else 0
    t_stat = (mean_pnl / (std_pnl / math.sqrt(n))) if std_pnl > 0 else 0

    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    avg_entry = statistics.mean([t["entry_price"] for t in trades])
    avg_win = statistics.mean([p for p in pnls if p > 0]) if wins > 0 else 0
    avg_loss = statistics.mean([p for p in pnls if p <= 0]) if (n - wins) > 0 else 0

    # Exit type breakdown
    exit_types = defaultdict(int)
    for t in trades:
        exit_types[t.get("exit_type", "?")] += 1

    return {
        "n": n, "wins": wins, "wr": wr,
        "mean_pnl": mean_pnl, "std": std_pnl,
        "t_stat": t_stat, "pf": pf,
        "avg_entry": avg_entry,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "total_pnl": sum(pnls),
        "exit_types": dict(exit_types),
    }


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scalping strategy backtest")
    parser.add_argument("--hours", type=int, default=96, help="Hours of history")
    parser.add_argument("--coin", type=str, default="BTC", help="Coin symbol")
    parser.add_argument("--max-periods", type=int, default=700)
    parser.add_argument("--json-out", type=str, default=None)
    args = parser.parse_args()

    print(f"\n{'='*90}")
    print(f"  SCALPING STRATEGY BACKTEST — {args.coin} 15m — {args.hours}h — TP/SL EXITS")
    print(f"{'='*90}\n")

    # 1. Collect periods
    print("Collecting oracle periods...")
    periods = fetch_oracle_periods(args.coin, args.hours)
    periods = [p for p in periods if p.get("outcome") in ("up", "down")]
    print(f"  Found {len(periods)} resolved periods")

    if not periods:
        sys.exit(1)
    periods = periods[:args.max_periods]

    # 2. Fetch price data
    print(f"\nFetching price histories...")
    period_data = []
    for i, period in enumerate(periods):
        st = datetime.fromisoformat(period["startTime"].replace("Z", "+00:00"))
        period_start_ts = (int(st.timestamp()) // 900) * 900
        period_end_ts = period_start_ts + 900

        up_token, dn_token = fetch_pm_token_for_period(args.coin, period_start_ts)
        if not up_token:
            continue

        prices_up = fetch_prices_history(up_token, period_start_ts, period_end_ts)
        if not prices_up:
            continue

        period_data.append({
            "period": period,
            "period_start_ts": period_start_ts,
            "prices_up": prices_up,
            "outcome": period["outcome"],
        })

        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(periods)} ({len(period_data)} with data)")
        time.sleep(0.3)

    total_periods = len(period_data)
    print(f"  Total: {total_periods} periods with data")

    # 3. Build configs
    configs = build_configs()
    print(f"\nTesting {len(configs)} strategy configurations...")

    # 4. Run all strategies
    all_results = []
    for ci, config in enumerate(configs):
        trades = []
        for pd in period_data:
            trade = config["fn"](
                pd["prices_up"], pd["period_start_ts"],
                pd["outcome"], config["params"],
            )
            if trade:
                trades.append(trade)

        stats = compute_stats(trades)
        all_results.append({
            "name": config["name"],
            "family": config["family"],
            "params": config["params"],
            "stats": stats,
            "trades": trades,
        })

        if (ci + 1) % 200 == 0:
            print(f"  ... {ci+1}/{len(configs)} strategies evaluated")

    # 5. Report by family
    print(f"\n{'='*110}")
    print(f"  RESULTS BY FAMILY (top 5 per family by t-stat, n>=15)")
    print(f"{'='*110}\n")

    families = defaultdict(list)
    for r in all_results:
        if r["stats"]["n"] >= 15:
            families[r["family"]].append(r)

    for family in sorted(families.keys()):
        members = families[family]
        members.sort(key=lambda x: -x["stats"]["t_stat"])

        print(f"\n  ── {family.upper()} ({len(members)} qualifying configs) ──\n")
        print(f"    {'Name':>55}  {'N':>4}  {'Win%':>5}  {'AvgE':>5}  "
              f"{'AvgW':>7}  {'AvgL':>7}  {'PnL/tr':>8}  {'t':>6}  {'PF':>5}  {'Tot$':>8}  {'Exits':>20}")
        print(f"    {'─'*55}  {'─'*4}  {'─'*5}  {'─'*5}  {'─'*7}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*5}  {'─'*8}  {'─'*20}")

        for r in members[:5]:
            s = r["stats"]
            exits = s.get("exit_types", {})
            exit_str = " ".join(f"{k}:{v}" for k, v in sorted(exits.items()))
            print(f"    {r['name']:>55}  {s['n']:>4}  {s['wr']:>4.0%}  "
                  f"{s['avg_entry']:>5.3f}  {s['avg_win']:>+7.4f}  {s['avg_loss']:>+7.4f}  "
                  f"{s['mean_pnl']:>+8.4f}  {s['t_stat']:>+6.2f}  {s['pf']:>5.2f}  "
                  f"{s['total_pnl']:>+8.2f}  {exit_str:>20}")

    # 6. Overall top 25
    qualified = [r for r in all_results if r["stats"]["n"] >= 20]
    qualified.sort(key=lambda x: -x["stats"]["t_stat"])

    print(f"\n{'='*110}")
    print(f"  TOP 25 STRATEGIES (n>=20, by t-stat)")
    print(f"{'='*110}\n")
    print(f"  {'#':>3}  {'Name':>55}  {'Fam':>12}  {'N':>4}  {'Win%':>5}  "
          f"{'AvgW':>7}  {'AvgL':>7}  {'PnL/tr':>8}  {'t':>6}  {'PF':>5}  {'Tot$':>8}")
    print(f"  {'─'*3}  {'─'*55}  {'─'*12}  {'─'*4}  {'─'*5}  "
          f"{'─'*7}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*5}  {'─'*8}")

    for i, r in enumerate(qualified[:25]):
        s = r["stats"]
        print(f"  {i+1:>3}  {r['name']:>55}  {r['family']:>12}  {s['n']:>4}  "
              f"{s['wr']:>4.0%}  {s['avg_win']:>+7.4f}  {s['avg_loss']:>+7.4f}  "
              f"{s['mean_pnl']:>+8.4f}  {s['t_stat']:>+6.2f}  {s['pf']:>5.2f}  "
              f"{s['total_pnl']:>+8.2f}")

    # 7. Split-half for top 15
    print(f"\n{'='*110}")
    print(f"  SPLIT-HALF STABILITY (top 15)")
    print(f"{'='*110}\n")
    print(f"  {'Name':>55}  {'H1 N':>5}  {'H1 PnL':>8}  {'H2 N':>5}  {'H2 PnL':>8}  {'OK':>3}")
    print(f"  {'─'*55}  {'─'*5}  {'─'*8}  {'─'*5}  {'─'*8}  {'─'*3}")

    for r in qualified[:15]:
        trades = r["trades"]
        mid = len(trades) // 2
        h1 = [t["pnl"] for t in trades[:mid]]
        h2 = [t["pnl"] for t in trades[mid:]]
        h1m = statistics.mean(h1) if h1 else 0
        h2m = statistics.mean(h2) if h2 else 0
        ok = "✓" if h1m > 0 and h2m > 0 else "✗"
        print(f"  {r['name']:>55}  {len(h1):>5}  {h1m:>+8.4f}  {len(h2):>5}  {h2m:>+8.4f}  {ok:>3}")

    # 8. Family summary
    print(f"\n{'='*110}")
    print(f"  FAMILY COMPARISON (best per family, n>=20)")
    print(f"{'='*110}\n")

    for family in sorted(families.keys()):
        members = [r for r in families[family] if r["stats"]["n"] >= 20]
        if not members:
            print(f"  {family:>20}: no qualifying configs")
            continue
        best = max(members, key=lambda x: x["stats"]["t_stat"])
        s = best["stats"]
        trades = best["trades"]
        mid = len(trades) // 2
        h1 = statistics.mean([t["pnl"] for t in trades[:mid]]) if mid > 0 else 0
        h2 = statistics.mean([t["pnl"] for t in trades[mid:]]) if mid > 0 else 0
        stable = "✓" if h1 > 0 and h2 > 0 else "✗"
        exits = s.get("exit_types", {})
        tp_pct = exits.get("tp", 0) / s["n"] * 100 if s["n"] > 0 else 0
        sl_pct = exits.get("sl", 0) / s["n"] * 100 if s["n"] > 0 else 0

        print(f"  {family:>20}: n={s['n']:>3}  wr={s['wr']:.0%}  t={s['t_stat']:+.2f}  "
              f"pnl={s['mean_pnl']:+.4f}  PF={s['pf']:.2f}  "
              f"tp:{tp_pct:.0f}% sl:{sl_pct:.0f}%  {stable}")

    # 9. Key insight: P&L asymmetry comparison
    print(f"\n{'='*110}")
    print(f"  P&L ASYMMETRY CHECK (top 10): avg_win vs avg_loss, breakeven win rate")
    print(f"{'='*110}\n")
    for i, r in enumerate(qualified[:10]):
        s = r["stats"]
        aw = abs(s["avg_win"]) if s["avg_win"] else 0.001
        al = abs(s["avg_loss"]) if s["avg_loss"] else 0.001
        be_wr = al / (aw + al) * 100
        print(f"  {i+1:>3}. {r['name']:>50}  avgW={s['avg_win']:+.4f}  avgL={s['avg_loss']:+.4f}  "
              f"BE_wr={be_wr:.0f}%  actual_wr={s['wr']:.0%}  "
              f"{'✓ EDGE' if s['wr'] > be_wr/100 else '✗ NO EDGE'}")

    # 10. Save
    if args.json_out:
        output = {
            "meta": {"coin": args.coin, "hours": args.hours,
                     "total_periods": total_periods, "n_strategies": len(configs)},
            "results": [{"name": r["name"], "family": r["family"], "stats": r["stats"]}
                        for r in qualified[:50]],
        }
        with open(args.json_out, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\nSaved to {args.json_out}")


if __name__ == "__main__":
    main()
