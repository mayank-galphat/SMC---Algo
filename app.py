"""
SMC Terminal — web dashboard for the SMC / ML trading project.

    python app.py            ->  http://127.0.0.1:5000

Everything except /login, /signup and /api/auth/* requires an account (see auth.py).

Serves a live chart UI and a small JSON API:
    /api/auth/*     signup, login, logout, me
    /api/config     symbols, timeframes, whether an ML model is loaded
    /api/candles    OHLCV (Binance public REST, falls back to the bundled CSV)
    /api/smc        swings, Order Blocks, FVGs + AI verdict per bullish OB
    /api/ict        ICT concepts: structure, OB/Breaker, FVG/IFVG, liquidity, kill zones, setups
    /api/backtest   runs the real Backtrader strategy, returns trades/equity/stats
"""
from __future__ import annotations

import os
import time
import warnings

import joblib
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

import auth
import angel
import billing
import ict_engine
import markets
import paper
import smc_engine
from backtest import MODEL_FILE, DATA_FILE, run_backtest

warnings.filterwarnings("ignore", category=UserWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
auth.init_app(app)  # login / signup / sessions; guards the dashboard and every /api route
billing.init_app(app)  # Razorpay payments, Pro plan, feature gating


def _last_price(symbol: str):
    """Latest traded price for the practice-trading desk: (price, bar_time) or None."""
    df, _ = get_candles(symbol, "5m")
    if df is None or not len(df):
        df, _ = get_candles(symbol, "1h")
    return (float(df["close"].iloc[-1]), int(df["time"].iloc[-1])) if df is not None and len(df) else None


paper.init_app(app, _last_price)  # virtual-money buy/sell
angel.init_app(app, _last_price)  # live trading on your own Angel One account (off unless configured)

INTERVALS = ["5m", "15m", "1h", "4h", "1d"]
FEATURES = ["ob_height", "volume_ratio", "impulse_size", "has_fvg"]

_candle_cache: dict = {}
CACHE_SECONDS = 8

try:
    MODEL = joblib.load(MODEL_FILE)
except Exception as exc:  # missing or incompatible pickle -> run without AI
    print(f"[!] ML model not loaded ({exc}); AI verdicts disabled.")
    MODEL = None


# ----------------------------------------------------------------- data ---
def _load_csv() -> pd.DataFrame:
    df = pd.read_csv(DATA_FILE, parse_dates=["datetime"])
    # Unit-safe epoch seconds (pandas 3 datetimes aren't always ns).
    df["time"] = (df["datetime"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)
    return df[["time", "open", "high", "low", "close", "volume"]]


def get_candles(symbol: str, interval: str, limit: int = 1000):
    key = (symbol, interval, limit)
    hit = _candle_cache.get(key)
    ttl = 20 if markets.is_yahoo(symbol) else CACHE_SECONDS
    if hit and time.time() - hit[0] < ttl:
        return hit[1], hit[2]
    if markets.is_yahoo(symbol):
        df, source = markets.fetch_yahoo(symbol, interval, limit), "yahoo"
    else:
        df, source = markets.fetch_binance(symbol, interval, limit), "binance"
    if df is None:
        if symbol == "BTCUSDT" and interval == "1h":
            df, source = _load_csv(), "csv"
        else:
            return None, "unavailable"
    _candle_cache[key] = (time.time(), df, source)
    return df, source


def _params():
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    interval = request.args.get("interval", "1h")
    if not markets.valid_id(symbol) or interval not in INTERVALS:
        return None, None
    return symbol, interval


def _bad_data(source):
    return jsonify(error="No data for this market/timeframe right now. The source may be unreachable, the symbol "
                         "unknown, or the timeframe unsupported (Yahoo limits 5m/15m to ~60 days). Try 1h or 1d."), 503


# ------------------------------------------------------------------ api ---
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config")
def config():
    return jsonify(symbols=[p["symbol"] for p in markets.popular()], popular=markets.popular(),
                   intervals=INTERVALS, ai_available=MODEL is not None,
                   catalog_size=len(markets.catalog()))


@app.route("/api/search")
def search():
    q = request.args.get("q", "")[:40].strip()
    mk = request.args.get("market", "").upper()
    if mk not in ("US", "IN", "CRYPTO", "IDX") + tuple(markets.LISTS) + ("",):
        return jsonify(error="bad market"), 400
    if not q and mk:
        try:
            off = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            off = 0
        return jsonify(**markets.browse(mk, off))
    res = markets.search(q, market=mk)
    return jsonify(results=res, total=len(res), offset=0)


@app.route("/api/candles")
def candles():
    symbol, interval = _params()
    if not symbol:
        return jsonify(error="bad symbol or interval"), 400
    df, source = get_candles(symbol, interval)
    if df is None:
        return _bad_data(source)
    return jsonify(source=source, symbol=symbol, interval=interval,
                   candles=df.to_dict(orient="records"))


def _ai_verdict(ob):
    if MODEL is None:
        return None
    row = pd.DataFrame([[ob["height"], ob["volume_ratio"], ob["impulse"], int(ob["has_fvg"])]], columns=FEATURES)
    try:
        approved = int(MODEL.predict(row)[0]) == 1
        score = float(MODEL.decision_function(row)[0])
    except Exception:
        return None
    return {"approved": approved, "score": round(score, 3)}


@app.route("/api/smc")
def smc():
    symbol, interval = _params()
    if not symbol:
        return jsonify(error="bad symbol or interval"), 400
    df, source = get_candles(symbol, interval)
    if df is None:
        return _bad_data(source)
    # Drop the still-forming bar so signals never repaint (the strategy does the same).
    closed = markets.drop_forming(df, interval, source)
    res = smc_engine.detect(closed)
    t = closed["time"].to_numpy()

    obs = []
    for ob in res["order_blocks"]:
        obs.append({
            "side": ob["side"], "status": ob["status"], "has_fvg": ob["has_fvg"],
            "top": ob["top"], "bottom": ob["bottom"],
            "t0": int(t[ob["candle"]]), "t_confirm": int(t[ob["confirm"]]), "t1": int(t[ob["end"]]),
            "t_touch": int(t[ob["touched"]]) if ob["touched"] is not None else None,
            "volume_ratio": round(ob["volume_ratio"], 2), "impulse": round(ob["impulse"], 2),
            "ai": _ai_verdict(ob),
        })
    fvgs = [{"side": f["side"], "top": f["top"], "bottom": f["bottom"], "filled": f["filled"],
             "t0": int(t[f["mid"] - 1]), "t1": int(t[f["end"]])} for f in res["fvgs"]]
    swings = [{"time": int(t[s["i"]]), "type": s["type"], "price": s["price"]} for s in res["swings"]]
    sd = []
    sd_locked = billing.locked()   # Supply & Demand is a Pro feature
    for z in ([] if sd_locked else smc_engine.detect_zones(closed)):
        sd.append({**{k: v for k, v in z.items() if k not in ("i0", "leg", "end")},
                   "t0": int(t[z["i0"]]), "t_leg": int(t[z["leg"]]), "t1": int(t[z["end"]])})
    last = float(closed["close"].iloc[-1]) if len(closed) else 0.0
    live = [z for z in sd if z["status"] != "broken"]
    dem = [z for z in live if z["kind"] == "demand" and z["proximal"] <= last]
    sup = [z for z in live if z["kind"] == "supply" and z["proximal"] >= last]

    bull = [o for o in obs if o["side"] == "bull"]
    summary = {
        "total_obs": len(obs),
        "active": sum(o["status"] == "active" for o in obs),
        "bull": len(bull), "bear": len(obs) - len(bull),
        "ai_approved": sum(bool(o["ai"] and o["ai"]["approved"]) for o in bull),
        "ai_rejected": sum(bool(o["ai"] and not o["ai"]["approved"]) for o in bull),
        "open_fvgs": sum(not f["filled"] for f in fvgs),
        "fresh_demand": sum(z["kind"] == "demand" and z["status"] == "fresh" for z in sd),
        "fresh_supply": sum(z["kind"] == "supply" and z["status"] == "fresh" for z in sd),
        "near_demand": max((z["proximal"] for z in dem), default=None),
        "near_supply": min((z["proximal"] for z in sup), default=None),
        "price": last,
    }
    return jsonify(source=source, symbol=symbol, interval=interval, order_blocks=obs,
                   fvgs=fvgs, swings=swings, sd_zones=sd, sd_locked=sd_locked, summary=summary)


@app.route("/api/ict")
def ict():
    blocked = billing.gate("ict")
    if blocked:
        return blocked
    symbol, interval = _params()
    if not symbol:
        return jsonify(error="bad symbol or interval"), 400
    df, source = get_candles(symbol, interval)
    if df is None:
        return _bad_data(source)
    closed = markets.drop_forming(df, interval, source)
    res = ict_engine.detect(closed)
    done = [s for s in res["setups"] if s["status"] in ("win", "loss")]
    rs = [s["r"] for s in done]
    res["summary"] = {
        "setups": len(res["setups"]), "closed": len(done), "wins": sum(s["status"] == "win" for s in done),
        "win_rate": (sum(s["status"] == "win" for s in done) / len(done) * 100) if done else 0,
        "total_r": round(sum(rs), 2), "avg_r": round(sum(rs) / len(rs), 2) if rs else 0,
        "open_fvgs": sum(f["kind"] == "FVG" and f["status"] in ("open", "partial") for f in res["fvgs"]),
        "active_obs": sum(o["status"] in ("active", "mitigated") and o["kind"] == "OB" for o in res["order_blocks"]),
        "unswept": sum(not p["swept"] for p in res["liquidity"]),
    }
    return jsonify(source=source, symbol=symbol, interval=interval, **res)


def _stats(bt, trades):
    start, final = bt["start_cash"], bt["final_value"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    peak, max_dd = start, 0.0
    for _, v in bt["equity"]:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak else 0)
    return {
        "start": start, "final": final, "return_pct": (final - start) / start * 100,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (None if not gross_win else float("inf")),
        "avg_win": gross_win / len(wins) if wins else 0,
        "avg_loss": -gross_loss / len(losses) if losses else 0,
        "max_drawdown_pct": max_dd * 100,
    }


def _ts(x):
    return int((pd.Timestamp(x) - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1))


@app.route("/api/backtest")
def backtest():
    blocked = billing.gate("backtest")
    if blocked:
        return blocked
    symbol, interval = _params()
    if not symbol:
        return jsonify(error="bad symbol or interval"), 400
    use_ai = request.args.get("ai", "1") == "1" and MODEL is not None
    sides = request.args.get("sides", "both")
    if sides not in ("both", "long", "short"):
        return jsonify(error="bad sides"), 400
    df, source = get_candles(symbol, interval)
    if df is None:
        return _bad_data(source)
    d = markets.drop_forming(df, interval, source).copy()
    d["datetime"] = pd.to_datetime(d["time"], unit="s")
    bt = run_backtest(d[["datetime", "open", "high", "low", "close", "volume"]], use_ai=use_ai, sides=sides)

    trades = []
    for t in bt["trades"]:
        trades.append({
            "entry_time": _ts(t["entry_time"]), "exit_time": _ts(t["exit_time"]),
            "entry_price": t["entry_price"], "exit_price": t["exit_price"], "pnl": t["pnl"],
            "side": t["side"],
            "ret_pct": ((t["exit_price"] / t["entry_price"] - 1) * 100 * (1 if t["side"] == "long" else -1))
            if t["entry_price"] and t["exit_price"] else 0,
        })
    equity = [{"time": _ts(ts), "value": v} for ts, v in bt["equity"]]
    by_side = {}
    for sd in ("long", "short"):
        ts = [t for t in bt["trades"] if t["side"] == sd]
        by_side[sd] = {"trades": len(ts), "wins": sum(t["pnl"] > 0 for t in ts), "pnl": sum(t["pnl"] for t in ts),
                       "win_rate": (sum(t["pnl"] > 0 for t in ts) / len(ts) * 100) if ts else 0}
    return jsonify(source=source, ai=use_ai, ai_available=MODEL is not None, sides=sides, by_side=by_side,
                   stats=_stats(bt, bt["trades"]), trades=trades, equity=equity,
                   signals={"total": len(bt["signals"]),
                            "approved": sum(s["approved"] for s in bt["signals"]),
                            "long": sum(s["side"] == "long" for s in bt["signals"]),
                            "short": sum(s["side"] == "short" for s in bt["signals"])})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"SMC Terminal -> http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
