"""
ICT (Inner Circle Trader) concept engine.

detect(df) -> dict with:
  structure   BOS / CHoCH (MSS) events
  displacement  strong impulse candles
  order_blocks  ICT OBs + Breaker Blocks (status: active/mitigated/invalidated)
  fvgs        Fair Value Gaps (open / partial / filled) + Inversion FVGs (IFVG)
  liquidity   BSL/SSL swing pools, EQH/EQL equal-level pools, swept or not
  sweeps      liquidity raids (wick through a pool, close back inside)
  levels      PDH/PDL, PWH/PWL, daily/weekly open, Asian range (New York time)
  killzones   Asia / London / NY AM / NY PM sessions and Silver Bullet windows
  dealing_range  premium / discount, equilibrium, OTE (62-79%) zone
  setups      sweep -> MSS -> FVG/OB retest models, simulated to win/loss in R
All times returned as epoch seconds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NY = "America/New_York"
SWING_L = 3
DISP_BODY_X = 1.5      # displacement: body > 1.5x average body ...
DISP_BODY_RATIO = 0.6  # ... and body fills >= 60% of the candle range
EQ_TOL = 0.0008        # equal highs/lows tolerance (0.08%)
SWEEP_WINDOW = 30      # bars between a sweep and the MSS it precedes
ENTRY_WAIT = 60        # bars to wait for the retrace into the entry zone
MAX_HOLD = 120

SESSIONS = [  # (name, start_h, end_h) in New York time
    ("Asia", 20.0, 24.0), ("London", 2.0, 5.0), ("NY AM", 7.0, 10.0), ("NY PM", 13.5, 16.0)]
SILVER = [("SB London", 3.0, 4.0), ("SB NY AM", 10.0, 11.0), ("SB NY PM", 14.0, 15.0)]


def _first(cond: np.ndarray, start: int):
    """First index >= start where cond is True, else None."""
    if start >= len(cond):
        return None
    hit = np.nonzero(cond[start:])[0]
    return int(hit[0]) + start if len(hit) else None


def detect(df: pd.DataFrame) -> dict:
    o, h, l, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    tt = df["time"].to_numpy(np.int64)
    n = len(df)
    T = lambda i: int(tt[min(max(int(i), 0), n - 1)])
    out = {k: [] for k in ("structure", "displacement", "order_blocks", "fvgs", "liquidity",
                           "sweeps", "levels", "killzones", "setups")}
    out["dealing_range"] = None
    if n < 40:
        return out

    rng = h - l
    body = np.abs(c - o)
    avg_body = pd.Series(body).rolling(20, min_periods=5).mean().shift(1).to_numpy()
    disp = (body > DISP_BODY_X * np.nan_to_num(avg_body, nan=1e18)) & (body > DISP_BODY_RATIO * np.where(rng > 0, rng, 1e18))
    for i in np.nonzero(disp)[0]:
        out["displacement"].append({"t": T(i), "dir": "bull" if c[i] > o[i] else "bear"})

    # ---- swings (fractals, confirmed SWING_L bars late)
    sh, sl = [], []
    for i in range(SWING_L, n - SWING_L):
        w = range(i - SWING_L, i + SWING_L + 1)
        if all(h[k] < h[i] for k in w if k != i):
            sh.append(i)
        if all(l[k] > l[i] for k in w if k != i):
            sl.append(i)

    # ---- market structure: BOS / CHoCH (MSS) on candle-close breaks
    events, trend = [], 0
    sh_set, sl_set = set(sh), set(sl)
    last_h = last_l = None  # (idx, price) of most recent unbroken confirmed swing
    for t in range(n):
        ci = t - SWING_L
        if ci in sh_set:
            last_h = (ci, h[ci])
        if ci in sl_set:
            last_l = (ci, l[ci])
        if last_h and c[t] > last_h[1]:
            events.append({"kind": "CHoCH" if trend < 0 else "BOS", "dir": "bull", "level": last_h[1],
                           "i0": last_h[0], "i": t, "leg": last_l[0] if last_l else last_h[0]})
            trend, last_h = 1, None
        elif last_l and c[t] < last_l[1]:
            events.append({"kind": "CHoCH" if trend > 0 else "BOS", "dir": "bear", "level": last_l[1],
                           "i0": last_l[0], "i": t, "leg": last_h[0] if last_h else last_l[0]})
            trend, last_l = -1, None
    out["structure"] = [{"kind": e["kind"], "dir": e["dir"], "level": e["level"], "t0": T(e["i0"]), "t1": T(e["i"])}
                        for e in events]

    # ---- Fair Value Gaps + Inversion FVGs
    fvgs = []
    for m in range(1, n - 1):
        for side, gap in (("bull", l[m + 1] - h[m - 1]), ("bear", l[m - 1] - h[m + 1])):
            if gap <= 0 or gap < 0.0002 * c[m]:
                continue
            top, bot = (l[m + 1], h[m - 1]) if side == "bull" else (l[m - 1], h[m + 1])
            ce = (top + bot) / 2
            if side == "bull":
                touched = _first(l <= top, m + 2); half = _first(l <= ce, m + 2)
                filled = _first(l <= bot, m + 2); inv = _first(c < bot, m + 2)
            else:
                touched = _first(h >= bot, m + 2); half = _first(h >= ce, m + 2)
                filled = _first(h >= top, m + 2); inv = _first(c > top, m + 2)
            status = "filled" if filled is not None else "partial" if half is not None else "open"
            f = {"side": side, "kind": "FVG", "top": top, "bottom": bot, "ce": ce, "status": status,
                 "t0": T(m - 1), "t1": T(filled if filled is not None else n - 1),
                 "displaced": bool(disp[m]), "_m": m}
            fvgs.append(f)
            if inv is not None:  # IFVG: closed fully through the gap -> acts as opposite-side zone
                end = _first(c > top if side == "bear" else c < bot, inv + 1)
                if inv < n - 1 or end is None:
                    fvgs.append({"side": "bear" if side == "bull" else "bull", "kind": "IFVG", "top": top,
                                 "bottom": bot, "ce": ce, "status": "invalidated" if end is not None else "open",
                                 "t0": T(inv), "t1": T(end if end is not None else n - 1),
                                 "displaced": False, "_m": inv})
    out["fvgs"] = [{k: v_ for k, v_ in f.items() if k != "_m"} for f in fvgs]

    # ---- Order Blocks (last opposing candle before the displacement that broke structure) + Breakers
    obs = []
    for e in events:
        lo, hi = max(e["leg"], e["i"] - 25), e["i"]
        want_down = e["dir"] == "bull"
        cand = [k for k in range(lo, hi) if (c[k] < o[k]) == want_down and c[k] != o[k]]
        if not cand:
            continue
        k = cand[-1]
        top, bot = h[k], l[k]
        side = e["dir"]
        status, end, touched = "active", n - 1, None
        for j in range(e["i"] + 1, n):
            if side == "bull":
                if c[j] < bot: status, end = "invalidated", j; break
                if touched is None and l[j] <= top: touched = j
            else:
                if c[j] > top: status, end = "invalidated", j; break
                if touched is None and h[j] >= bot: touched = j
        else:
            status = "mitigated" if touched is not None else "active"
        has_fvg = any(f["side"] == side and f["kind"] == "FVG" and k <= f["_m"] <= e["i"] for f in fvgs)
        obs.append({"side": side, "kind": "OB", "top": top, "bottom": bot, "ce": (top + bot) / 2,
                    "status": status, "has_fvg": has_fvg, "choch": e["kind"] == "CHoCH",
                    "t0": T(k), "t_confirm": T(e["i"]), "t1": T(end), "t_touch": T(touched) if touched is not None else None})
        if status == "invalidated":  # Breaker: failed OB flips polarity
            bside = "bear" if side == "bull" else "bull"
            bend = _first(c > top if bside == "bear" else c < bot, end + 1)
            obs.append({"side": bside, "kind": "Breaker", "top": top, "bottom": bot, "ce": (top + bot) / 2,
                        "status": "invalidated" if bend is not None else "active", "has_fvg": False,
                        "choch": False, "t0": T(end), "t_confirm": T(end), "t1": T(bend if bend is not None else n - 1),
                        "t_touch": None})
    out["order_blocks"] = obs

    # ---- Liquidity pools (BSL / SSL swings, EQH / EQL clusters) + sweeps
    pools = []
    def add_pool(kind, i, price, side):
        if side == "high":
            hit = _first(h > price, i + SWING_L + 1)
        else:
            hit = _first(l < price, i + SWING_L + 1)
        swept = hit is not None
        pools.append({"kind": kind, "side": "BSL" if side == "high" else "SSL", "price": price,
                      "t0": T(i), "t1": T(hit if swept else n - 1), "swept": swept,
                      "_hit": hit, "_i": i})
    for idxs, side, arr in ((sh, "high", h), (sl, "low", l)):
        used = set()
        for a in range(len(idxs)):
            grp = [idxs[a]]
            if a in used:
                continue
            for b in range(a + 1, len(idxs)):
                if abs(arr[idxs[b]] - arr[idxs[a]]) <= EQ_TOL * arr[idxs[a]]:
                    grp.append(idxs[b]); used.add(b)
            if len(grp) >= 2:
                price = max(arr[g] for g in grp) if side == "high" else min(arr[g] for g in grp)
                add_pool("EQH" if side == "high" else "EQL", grp[-1], price, side)
            elif a not in used:
                add_pool("swing", idxs[a], arr[idxs[a]], side)
    for p in pools:
        if p["_hit"] is not None:
            j = p["_hit"]
            closed_back = c[j] < p["price"] if p["side"] == "BSL" else c[j] > p["price"]
            out["sweeps"].append({"t": T(j), "i": j, "side": p["side"], "price": p["price"], "kind": p["kind"],
                                  "reject": bool(closed_back), "extreme": float(h[j] if p["side"] == "BSL" else l[j])})
    out["liquidity"] = [{k: v_ for k, v_ in p.items() if not k.startswith("_")} for p in pools]

    # ---- Time-based: New York sessions, kill zones, silver bullet
    ny = pd.to_datetime(tt, unit="s", utc=True).tz_convert(NY)
    hr = (ny.hour + ny.minute / 60).to_numpy()
    day = ny.normalize().tz_localize(None)
    tf = int(np.median(np.diff(tt))) if n > 2 else 3600
    out["intraday"] = tf < 14400
    if out["intraday"]:
        for name, s0, s1 in SESSIONS + SILVER:
            m = (hr >= s0) & (hr < s1) & (tf <= 3600 if name.startswith("SB") else True)
            if name.startswith("SB") and tf > 900:
                continue
            idx = np.nonzero(m)[0]
            if not len(idx):
                continue
            for run in np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1):
                if len(run) and run[-1] - run[0] + 1 >= 1:
                    out["killzones"].append({"name": name, "t0": T(run[0]), "t1": T(run[-1]) + tf,
                                             "high": float(h[run].max()), "low": float(l[run].min()),
                                             "silver": name.startswith("SB")})
        out["killzones"] = out["killzones"][-60:]
        cur_hr = hr[-1]
        out["now_zone"] = next((nm for nm, s0, s1 in SESSIONS if s0 <= cur_hr < s1), "Off-session")

    # ---- Key levels: daily/weekly open, previous day/week high & low, last Asian range
    d = pd.DataFrame({"day": day, "h": h, "l": l, "o": o, "i": np.arange(n)})
    days = d.groupby("day").agg(h=("h", "max"), l=("l", "min"), o=("o", "first"), i=("i", "first"))
    if len(days) >= 2:
        cur, prev = days.iloc[-1], days.iloc[-2]
        out["levels"] += [{"name": "PDH", "price": float(prev.h), "t0": T(prev.i)},
                          {"name": "PDL", "price": float(prev.l), "t0": T(prev.i)},
                          {"name": "DO", "price": float(cur.o), "t0": T(cur.i)}]
    wk = day.to_period("W-SUN")
    w = pd.DataFrame({"wk": wk, "h": h, "l": l, "o": o, "i": np.arange(n)})
    weeks = w.groupby("wk").agg(h=("h", "max"), l=("l", "min"), o=("o", "first"), i=("i", "first"))
    if len(weeks) >= 2:
        cw, pw = weeks.iloc[-1], weeks.iloc[-2]
        out["levels"] += [{"name": "PWH", "price": float(pw.h), "t0": T(pw.i)},
                          {"name": "PWL", "price": float(pw.l), "t0": T(pw.i)},
                          {"name": "WO", "price": float(cw.o), "t0": T(cw.i)}]
    asia = [z for z in out["killzones"] if z["name"] == "Asia"]
    if asia:
        out["levels"] += [{"name": "Asia H", "price": asia[-1]["high"], "t0": asia[-1]["t0"]},
                          {"name": "Asia L", "price": asia[-1]["low"], "t0": asia[-1]["t0"]}]

    # ---- Dealing range, premium / discount, OTE
    if sh and sl:
        i_h, i_l = sh[-1], sl[-1]
        hi_, lo_ = h[i_h], l[i_l]
        if hi_ > lo_:
            up = i_l < i_h  # low formed first -> bullish leg, retrace down to discount
            r = hi_ - lo_
            ote = (hi_ - 0.79 * r, hi_ - 0.62 * r) if up else (lo_ + 0.62 * r, lo_ + 0.79 * r)
            out["dealing_range"] = {"high": float(hi_), "low": float(lo_), "eq": float((hi_ + lo_) / 2),
                                    "ote_top": float(ote[1]), "ote_bottom": float(ote[0]), "bias": "bull" if up else "bear",
                                    "t0": T(min(i_h, i_l)), "price": float(c[-1]),
                                    "zone": "premium" if c[-1] > (hi_ + lo_) / 2 else "discount"}

    # ---- ICT entry model: liquidity sweep -> MSS/CHoCH + displacement -> FVG/OB retrace
    sweeps = sorted(out["sweeps"], key=lambda s: s["i"])
    seen = set()
    for e in events:
        bull = e["dir"] == "bull"
        want = "SSL" if bull else "BSL"
        cands = [s for s in sweeps if s["side"] == want and s["reject"] and e["i"] - SWEEP_WINDOW <= s["i"] <= e["i"]]
        if not cands or e["i"] in seen:
            continue
        sw = cands[-1]
        seen.add(e["i"])
        zone = next((f for f in sorted(fvgs, key=lambda f: -f["_m"]) if f["kind"] == "FVG" and f["side"] == e["dir"]
                     and sw["i"] <= f["_m"] <= e["i"] + 1), None)
        ob = next((x for x in reversed(obs) if x["kind"] == "OB" and x["side"] == e["dir"] and x["t_confirm"] == T(e["i"])), None)
        if zone is None and ob is None:
            continue
        entry = zone["ce"] if zone else ob["ce"]
        window = slice(sw["i"], e["i"] + 1)
        stop = float(l[window].min() * (1 - 0.0005)) if bull else float(h[window].max() * (1 + 0.0005))
        risk = abs(entry - stop)
        if risk <= 0 or risk > 0.05 * entry:
            continue
        opp = [p for p in pools if p["side"] == ("BSL" if bull else "SSL") and not (p["swept"] and p["_hit"] <= e["i"])
               and (p["price"] > entry if bull else p["price"] < entry)]
        tgt_pool = min(opp, key=lambda p: abs(p["price"] - entry)) if opp else None
        target = tgt_pool["price"] if tgt_pool and abs(tgt_pool["price"] - entry) >= 1.5 * risk else \
            entry + (2 * risk if bull else -2 * risk)
        # simulate
        status, fill, exit_i, r_mult = "pending", None, None, None
        for j in range(e["i"] + 1, min(n, e["i"] + 1 + ENTRY_WAIT)):
            if (l[j] <= entry) if bull else (h[j] >= entry):
                fill = j; status = "open"; break
        if fill is None and e["i"] + ENTRY_WAIT < n:
            status = "missed"
        if fill is not None:
            for j in range(fill, min(n, fill + MAX_HOLD)):
                hit_sl = l[j] <= stop if bull else h[j] >= stop
                hit_tp = h[j] >= target if bull else l[j] <= target
                if hit_sl:  # same-bar ambiguity resolved pessimistically
                    status, exit_i, r_mult = "loss", j, -1.0; break
                if hit_tp:
                    status, exit_i, r_mult = "win", j, round(abs(target - entry) / risk, 2); break
        hrs = hr[e["i"]] if out["intraday"] else None
        in_kz = hrs is not None and any(s0 <= hrs < s1 for _, s0, s1 in SESSIONS if _ != "Asia")
        ph = [h[k] for k in sh if k <= e["i"] - SWING_L]
        pl = [l[k] for k in sl if k <= e["i"] - SWING_L]
        mid = (ph[-1] + pl[-1]) / 2 if ph and pl else None
        in_zone = bool(mid is not None and ((entry < mid) if bull else (entry > mid)))
        conf = [("Liquidity sweep", True), ("MSS / CHoCH" if e["kind"] == "CHoCH" else "BOS", True),
                ("Displacement", bool(disp[max(e["i"] - 2, 0): e["i"] + 1].any())), ("FVG", zone is not None),
                ("Order Block", ob is not None),
                ("Discount" if bull else "Premium", in_zone),
                ("Kill zone", bool(in_kz))]
        out["setups"].append({
            "dir": e["dir"], "kind": e["kind"], "t_sweep": T(sw["i"]), "t_break": T(e["i"]),
            "entry": float(entry), "stop": stop, "target": float(target), "rr": round(abs(target - entry) / risk, 2),
            "zone": "FVG" if zone else "OB", "status": status,
            "t_fill": T(fill) if fill is not None else None, "t_exit": T(exit_i) if exit_i is not None else None,
            "r": r_mult, "score": sum(1 for _, ok in conf if ok), "confluence": [{"name": nm, "ok": ok} for nm, ok in conf],
        })
    return out
