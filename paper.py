"""
Practice trading (paper trading): buy and sell with VIRTUAL money at live prices.

No real money, no broker, no real orders. Each user has a virtual wallet per currency
(INR for NSE/BSE stocks, USD for US stocks and crypto) and a portfolio of positions.
The server picks the fill price itself (latest market price) and checks cash / holdings,
so the browser cannot choose its own price or quantity limits.

  GET  /api/paper/portfolio   wallets, positions with live P&L, recent orders
  POST /api/paper/order       {symbol, side: buy|sell, qty}
  POST /api/paper/reset       start over with fresh virtual cash

Config (env): SMC_PAPER_INR (default 1000000), SMC_PAPER_USD (default 10000).
Fees are simulated at 0.05% per order. Long-only: you can sell only what you hold.
"""
from __future__ import annotations

import os
import time

from flask import Blueprint, jsonify, request

import auth
import markets

bp = Blueprint("paper", __name__)
FEE_RATE = 0.0005
MAX_NOTIONAL_X = 1.0          # an order may not exceed available cash (buys) or holdings (sells)
_price_fn = lambda symbol: None   # set by init_app: symbol -> (price, bar_time) | None


def start_cash() -> dict:
    def env(k, d):
        try:
            return max(1.0, float(os.environ.get(k, d)))
        except ValueError:
            return float(d)
    return {"INR": env("SMC_PAPER_INR", 1_000_000), "USD": env("SMC_PAPER_USD", 10_000)}


def currency_of(symbol: str) -> str | None:
    """Wallet currency for a symbol, or None if it cannot be traded here (indices, forex, futures)."""
    if not markets.is_yahoo(symbol):
        return "USD"                                   # Binance USDT pairs
    t = symbol[3:]
    if t.startswith("^") or t.endswith("=X") or t.endswith("=F"):
        return None
    return "INR" if t.endswith((".NS", ".BO")) else "USD"


# ---------------------------------------------------------------- storage ---
def init_db() -> None:
    with auth._connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS paper_wallets (
            user_id INTEGER NOT NULL, currency TEXT NOT NULL, cash REAL NOT NULL, start REAL NOT NULL,
            PRIMARY KEY (user_id, currency));
        CREATE TABLE IF NOT EXISTS paper_positions (
            user_id INTEGER NOT NULL, symbol TEXT NOT NULL, currency TEXT NOT NULL,
            qty REAL NOT NULL, avg_price REAL NOT NULL, PRIMARY KEY (user_id, symbol));
        CREATE TABLE IF NOT EXISTS paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
            currency TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
            fee REAL NOT NULL, pnl REAL, created_at INTEGER NOT NULL);""")


def _ensure_wallets(con, uid: int) -> None:
    for cur, amt in start_cash().items():
        con.execute("INSERT OR IGNORE INTO paper_wallets (user_id, currency, cash, start) VALUES (?, ?, ?, ?)",
                    (uid, cur, amt, amt))


@bp.before_request
def _guard():
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return jsonify(error="Cross-site request blocked."), 403
        if not request.is_json:
            return jsonify(error="Send JSON."), 415


def _body() -> dict:
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


# -------------------------------------------------------------------- api ---
@bp.get("/api/paper/portfolio")
def portfolio():
    uid = auth.current_user()["id"]
    with auth._connect() as con:
        _ensure_wallets(con, uid)
        wallets = [dict(r) for r in con.execute("SELECT currency, cash, start FROM paper_wallets WHERE user_id=?", (uid,))]
        pos = [dict(r) for r in con.execute("SELECT symbol, currency, qty, avg_price FROM paper_positions WHERE user_id=? "
                                            "ORDER BY symbol", (uid,))]
        orders = [dict(r) for r in con.execute("SELECT id, symbol, currency, side, qty, price, fee, pnl, created_at "
                                               "FROM paper_orders WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,))]
    value = {w["currency"]: 0.0 for w in wallets}
    for p in pos:
        q = _price_fn(p["symbol"])
        p["price"], p["stale"] = (q[0], False) if q else (p["avg_price"], True)   # fall back to cost if no quote
        p["tv"] = markets.tv_symbol(p["symbol"])
        p["value"] = p["qty"] * p["price"]
        p["pnl"] = (p["price"] - p["avg_price"]) * p["qty"]
        p["pnl_pct"] = (p["price"] / p["avg_price"] - 1) * 100 if p["avg_price"] else 0
        value[p["currency"]] += p["value"]
    for w in wallets:
        w["positions_value"] = value[w["currency"]]
        w["equity"] = w["cash"] + value[w["currency"]]
        w["pnl"] = w["equity"] - w["start"]
    return jsonify(wallets=wallets, positions=pos, orders=orders, fee_rate=FEE_RATE)


@bp.post("/api/paper/order")
def order():
    uid = auth.current_user()["id"]
    d = _body()
    symbol, side = str(d.get("symbol", "")).upper(), str(d.get("side", "")).lower()
    if not markets.valid_id(symbol) or side not in ("buy", "sell"):
        return jsonify(error="Invalid order."), 400
    cur = currency_of(symbol)
    if cur is None:
        return jsonify(error="Indices, forex and futures can't be traded here. Pick a stock, ETF or crypto."), 400
    try:
        qty = float(d.get("qty"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a quantity."), 400
    crypto = not markets.is_yahoo(symbol)
    if not (qty > 0) or qty != qty or qty > 1e9:
        return jsonify(error="Quantity must be greater than zero."), 400
    if not crypto and qty != int(qty):
        return jsonify(error="Stocks trade in whole shares."), 400
    qty = round(qty, 6) if crypto else float(int(qty))
    if qty <= 0:
        return jsonify(error="Quantity is too small."), 400
    if auth._throttled("paper", str(uid), 60, 60):
        return jsonify(error="Too many orders. Slow down for a moment."), 429
    auth._record("paper", str(uid))

    quote = _price_fn(symbol)
    if not quote:
        return jsonify(error="No live price for this symbol right now, so the order was not placed."), 503
    price = float(quote[0])
    gross = qty * price
    fee = gross * FEE_RATE

    con = auth._connect()
    try:
        con.execute("BEGIN IMMEDIATE")                       # one order at a time per database: no double-spend
        _ensure_wallets(con, uid)
        cash = con.execute("SELECT cash FROM paper_wallets WHERE user_id=? AND currency=?", (uid, cur)).fetchone()["cash"]
        pos = con.execute("SELECT qty, avg_price FROM paper_positions WHERE user_id=? AND symbol=?", (uid, symbol)).fetchone()
        pnl = None
        if side == "buy":
            if gross + fee > cash + 1e-9:
                con.rollback()
                return jsonify(error=f"Not enough virtual cash. Needed {gross + fee:,.2f} {cur}, you have {cash:,.2f}."), 400
            new_qty = (pos["qty"] if pos else 0) + qty
            avg = ((pos["qty"] * pos["avg_price"] + gross) / new_qty) if pos else price
            con.execute("INSERT INTO paper_positions (user_id, symbol, currency, qty, avg_price) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(user_id, symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price",
                        (uid, symbol, cur, new_qty, avg))
            cash -= gross + fee
        else:
            held = pos["qty"] if pos else 0
            if qty > held + 1e-9:
                con.rollback()
                return jsonify(error=f"You only hold {held:g}. Selling more than you own (short selling) isn't supported."), 400
            pnl = (price - pos["avg_price"]) * qty - fee
            left = round(held - qty, 6)
            if left <= 1e-9:
                con.execute("DELETE FROM paper_positions WHERE user_id=? AND symbol=?", (uid, symbol))
            else:
                con.execute("UPDATE paper_positions SET qty=? WHERE user_id=? AND symbol=?", (left, uid, symbol))
            cash += gross - fee
        con.execute("UPDATE paper_wallets SET cash=? WHERE user_id=? AND currency=?", (cash, uid, cur))
        con.execute("INSERT INTO paper_orders (user_id, symbol, currency, side, qty, price, fee, pnl, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (uid, symbol, cur, side, qty, price, fee, pnl, int(time.time())))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return jsonify(ok=True, side=side, symbol=symbol, qty=qty, price=price, fee=fee, pnl=pnl, currency=cur,
                   cash=cash, price_time=quote[1])


@bp.post("/api/paper/reset")
def reset():
    uid = auth.current_user()["id"]
    with auth._connect() as con:
        for t in ("paper_wallets", "paper_positions", "paper_orders"):
            con.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
        _ensure_wallets(con, uid)
    return jsonify(ok=True)


def init_app(app, price_fn) -> None:
    global _price_fn
    _price_fn = price_fn
    init_db()
    app.register_blueprint(bp)
