"""
Pure pandas/numpy port of the SmartMoneyConcepts Backtrader indicator.

The web dashboard needs the same swing / Order Block / FVG logic, but
Backtrader indicators only run inside a Cerebro instance. This module
reproduces `smc_indicator.py` bar-for-bar (verified by test_engine_parity.py)
and adds zone lifecycle tracking (active / mitigated / invalidated / expired)
so the chart can draw them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LOOKBACK = 2
IMPULSE_THRESHOLD = 1.5
OB_SEARCH_BARS = 10
MIN_PERIOD = max(LOOKBACK * 2 + 1, OB_SEARCH_BARS + 2)
PENDING_EXPIRY = 100  # same constant the strategy uses


def detect(df: pd.DataFrame) -> dict:
    """
    df: columns open, high, low, close, volume (any index, time-ordered).
    Returns dict with 'swings', 'fvgs', 'order_blocks' (lists of dicts that
    reference bars by integer position, `i`).
    """
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    n = len(df)

    swing_high = np.zeros(n, dtype=int)
    swing_low = np.zeros(n, dtype=int)
    bull_fvg = np.zeros(n, dtype=int)
    bear_fvg = np.zeros(n, dtype=int)

    last_sh = None
    last_sl = None
    obs = []

    # Backtrader calls next() once `MIN_PERIOD` bars exist -> first index is MIN_PERIOD-1.
    for t in range(MIN_PERIOD - 1, n):
        ci = t - LOOKBACK

        # --- swings (confirmed LOOKBACK bars late) ---
        window = range(t - LOOKBACK * 2, t + 1)
        if all(h[k] < h[ci] for k in window if k != ci):
            swing_high[ci] = 1
            last_sh = h[ci]
        if all(l[k] > l[ci] for k in window if k != ci):
            swing_low[ci] = 1
            last_sl = l[ci]

        # --- FVGs (flag lands on the middle candle, t-1) ---
        if l[t] > h[t - 2]:
            bull_fvg[t - 1] = 1
        if h[t] < l[t - 2]:
            bear_fvg[t - 1] = 1

        # --- Order blocks ---
        avg_candle = sum(h[t - i] - l[t - i] for i in range(1, 6)) / 5
        bullish_found = False

        if last_sh is not None and c[t] > last_sh:
            move = c[t] - last_sl if last_sl is not None else 0
            if avg_candle > 0 and move > avg_candle * IMPULSE_THRESHOLD:
                for i in range(1, OB_SEARCH_BARS + 1):
                    if c[t - i] < o[t - i]:
                        has_fvg = any(bull_fvg[t - j] == 1 for j in range(1, i + 1))
                        avg_vol = v[max(0, t - 19): t + 1].mean()
                        obs.append({
                            "side": "bull", "confirm": t, "candle": t - i,
                            "top": h[t - i], "bottom": l[t - i], "has_fvg": bool(has_fvg),
                            "height": h[t - i] - l[t - i],
                            "volume_ratio": float(v[t - i] / avg_vol) if avg_vol > 0 else 1.0,
                            "impulse": float(c[t] - h[t - i]),
                        })
                        last_sh = None
                        bullish_found = True
                        break

        if last_sl is not None and c[t] < last_sl:
            move = last_sh - c[t] if last_sh is not None else 0
            if avg_candle > 0 and move > avg_candle * IMPULSE_THRESHOLD:
                for i in range(1, OB_SEARCH_BARS + 1):
                    if c[t - i] > o[t - i]:
                        if not bullish_found:
                            has_fvg = any(bear_fvg[t - j] == 1 for j in range(1, i + 1))
                            avg_vol = v[max(0, t - 19): t + 1].mean()
                            obs.append({
                                "side": "bear", "confirm": t, "candle": t - i,
                                "top": h[t - i], "bottom": l[t - i], "has_fvg": bool(has_fvg),
                                "height": h[t - i] - l[t - i],
                                "volume_ratio": float(v[t - i] / avg_vol) if avg_vol > 0 else 1.0,
                                "impulse": float(l[t - i] - c[t]),
                            })
                        last_sl = None
                        break

    # --- zone lifecycle after confirmation ---
    for ob in obs:
        status, end = "active", n - 1
        touched = None
        for k in range(ob["confirm"] + 1, n):
            if k - ob["confirm"] > PENDING_EXPIRY:
                status, end = ("mitigated" if touched is not None else "expired"), k
                break
            if ob["side"] == "bull":
                if c[k] < ob["bottom"]:
                    status, end = "invalidated", k
                    break
                if touched is None and l[k] <= ob["top"]:
                    touched = k
            else:
                if c[k] > ob["top"]:
                    status, end = "invalidated", k
                    break
                if touched is None and h[k] >= ob["bottom"]:
                    touched = k
        else:
            if touched is not None:
                status = "mitigated"
        ob["status"] = status
        ob["end"] = end
        ob["touched"] = touched

    # --- unfilled FVGs ---
    fvgs = []
    for m in np.nonzero(bull_fvg)[0]:
        top, bottom = l[m + 1], h[m - 1]
        filled = next((k for k in range(m + 2, n) if l[k] <= bottom), None)
        fvgs.append({"side": "bull", "mid": int(m), "top": top, "bottom": bottom,
                     "end": filled if filled is not None else n - 1, "filled": filled is not None})
    for m in np.nonzero(bear_fvg)[0]:
        top, bottom = l[m - 1], h[m + 1]
        filled = next((k for k in range(m + 2, n) if h[k] >= top), None)
        fvgs.append({"side": "bear", "mid": int(m), "top": top, "bottom": bottom,
                     "end": filled if filled is not None else n - 1, "filled": filled is not None})

    swings = (
        [{"i": int(i), "type": "high", "price": h[i]} for i in np.nonzero(swing_high)[0]]
        + [{"i": int(i), "type": "low", "price": l[i]} for i in np.nonzero(swing_low)[0]]
    )
    swings.sort(key=lambda s: s["i"])
    return {"swings": swings, "fvgs": fvgs, "order_blocks": obs}


# ======================================================================
# Supply & Demand zones (classic base -> leg-out method)
# ======================================================================
SD_BASE_BODY_X = 0.5     # base candle body <= 0.5 x average range
SD_BASE_RANGE_X = 1.3    # ... and its range <= 1.3 x average range
SD_LEG_BODY_X = 1.2      # leg-out candle body >= 1.2 x average range ...
SD_LEG_RATIO = 0.55      # ... and body fills >= 55% of its own range
SD_LEG_MOVE_X = 1.8      # price must travel >= 1.8 x avg range away from the base within 3 bars
SD_MAX_BASE = 4          # 1-4 candle bases
SD_MAX_HEIGHT_X = 3.0    # skip zones taller than 3 x average range


def detect_zones(df: pd.DataFrame) -> list[dict]:
    """
    Demand zone = base before a strong move UP; supply zone = base before a strong move DOWN.
      DBR  drop-base-rally  (reversal demand)     RBR  rally-base-rally  (continuation demand)
      RBD  rally-base-drop  (reversal supply)     DBD  drop-base-drop    (continuation supply)
    proximal = the edge price reaches first (entry side), distal = the far edge (stop side).
    status: fresh (never retested) / tested (retested, still holding) / broken (closed beyond distal).
    Returned dicts reference bars by integer position: i0 (first base bar), leg (leg-out bar), end.
    """
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    if n < 30:
        return []
    rng = h - l
    body = np.abs(c - o)
    avg = pd.Series(rng).rolling(14, min_periods=5).mean().shift(1).to_numpy()
    base = (body <= SD_BASE_BODY_X * avg) & (rng <= SD_BASE_RANGE_X * avg)
    strong = (body >= SD_LEG_BODY_X * avg) & (body >= SD_LEG_RATIO * np.where(rng > 0, rng, np.inf))

    zones = []
    for j in range(6, n - 1):
        if not strong[j] or not np.isfinite(avg[j]) or avg[j] <= 0:
            continue
        up = c[j] > o[j]
        e = j - 1
        if not base[e]:
            continue
        s = e
        while s - 1 >= 1 and base[s - 1] and (e - (s - 1) + 1) <= SD_MAX_BASE:
            s -= 1
        a = avg[j]
        win = slice(j, min(j + 3, n))
        move = (h[win].max() - h[s:e + 1].max()) if up else (l[s:e + 1].min() - l[win].min())
        if move < SD_LEG_MOVE_X * a:
            continue
        if up:
            distal, proximal = l[s:e + 1].min(), np.maximum(o[s:e + 1], c[s:e + 1]).max()
        else:
            distal, proximal = h[s:e + 1].max(), np.minimum(o[s:e + 1], c[s:e + 1]).min()
        height = abs(proximal - distal)
        if height <= 0 or height > SD_MAX_HEIGHT_X * a:
            continue
        leg_in_down = c[s - 1] < c[max(s - 4, 0)]  # price was falling into the base
        pattern = ("DBR" if leg_in_down else "RBR") if up else ("RBD" if not leg_in_down else "DBD")
        imbalance = bool(l[min(j + 1, n - 1)] > h[j - 1]) if up else bool(h[min(j + 1, n - 1)] < l[j - 1])

        # --- lifecycle
        status, touches, end, in_zone = "fresh", 0, n - 1, False
        for k in range(j + 1, n):
            if up:
                if c[k] < distal:
                    status, end = "broken", k; break
                hit = l[k] <= proximal
            else:
                if c[k] > distal:
                    status, end = "broken", k; break
                hit = h[k] >= proximal
            if hit and not in_zone:
                touches += 1
            in_zone = hit
        if status != "broken":
            status = "fresh" if touches == 0 else "tested"

        # --- strength 0-100: departure, tight base, freshness, imbalance, reversal pattern
        score = (min(move / (4 * a), 1) * 35 + max(0.0, 1 - height / (2 * a)) * 20
                 + (25 if status == "fresh" else 10 if touches == 1 and status == "tested" else 0)
                 + (12 if imbalance else 0) + (8 if pattern in ("DBR", "RBD") else 0)
                 + (4 if (e - s + 1) <= 2 else 0))
        zones.append({"kind": "demand" if up else "supply", "pattern": pattern, "top": float(max(proximal, distal)),
                      "bottom": float(min(proximal, distal)), "proximal": float(proximal), "distal": float(distal),
                      "i0": int(s), "leg": int(j), "end": int(end), "status": status, "touches": touches,
                      "base_bars": int(e - s + 1), "imbalance": imbalance, "score": int(round(score)),
                      "rating": "strong" if score >= 70 else "medium" if score >= 45 else "weak"})
    return zones
