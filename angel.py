"""
Live trading through YOUR OWN Angel One (SmartAPI) account: NSE cash equities only.

Why single-account: Angel One's retail SmartAPI is built for one person trading their own account
(one API profile = one trading account, and orders are only accepted from a registered static IP).
It is not a way for a public app to trade for other people's accounts - that needs a licensed
algo-provider arrangement. So credentials live in environment variables on YOUR server and only
e-mails you list may use it.

Safety layers (all enforced on the server):
  * off unless configured; DRY-RUN by default (goes through every check, never calls placeOrder)
  * only e-mails in SMC_LIVE_EMAILS; NSE '-EQ' stocks only; whole shares; MARKET or LIMIT
  * two steps: /preview shows exact details + warnings and returns a signed, single-use, 90-second token;
    /confirm places it. Live orders also need an explicit `ack`
  * limits: max value per order, max orders per day, limit price within +/-20% of the last price
  * kill switch (/halt or SMC_LIVE_KILL=1); every attempt is written to an audit table

Environment
  SMC_ANGEL_API_KEY  SMC_ANGEL_CLIENT_CODE  SMC_ANGEL_PIN  SMC_ANGEL_TOTP_SECRET   (credentials)
  SMC_ANGEL_PUBLIC_IP  (your registered static IPv4)   SMC_ANGEL_MAC (optional)
  SMC_LIVE_EMAILS       comma-separated e-mails allowed to trade
  SMC_ANGEL_LIVE=1      REAL orders (anything else = dry-run)
  SMC_LIVE_MAX_ORDER_VALUE (default 10000)   SMC_LIVE_MAX_ORDERS_PER_DAY (default 10)   SMC_LIVE_KILL=1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, current_app, jsonify, request

import auth
import markets

bp = Blueprint("live", __name__)
HOST = "https://apiconnect.angelone.in"
SCRIP_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
SCRIP_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "angel_scrips.json")
TOKEN_TTL = 90
IST = timezone(timedelta(hours=5.5))
_price_fn = lambda symbol: None
_lock = threading.Lock()
_sess = {"jwt": None, "at": 0.0}
_scrips: dict = {"t": 0.0, "map": {}}


# ----------------------------------------------------------------- config ---
def _env(k: str, d: str = "") -> str: return os.environ.get(k, d).strip()
def configured() -> bool: return all(_env(k) for k in ("SMC_ANGEL_API_KEY", "SMC_ANGEL_CLIENT_CODE", "SMC_ANGEL_PIN", "SMC_ANGEL_TOTP_SECRET"))
def real_orders() -> bool: return _env("SMC_ANGEL_LIVE") == "1"
def allowed(user) -> bool:
    return bool(user) and user["email"].lower() in {e.strip().lower() for e in _env("SMC_LIVE_EMAILS").split(",") if e.strip()}
def _num(k: str, d: float) -> float:
    try:
        return max(0.0, float(_env(k, str(d))))
    except ValueError:
        return d
def max_value() -> float: return _num("SMC_LIVE_MAX_ORDER_VALUE", 10000)
def max_orders() -> int: return int(_num("SMC_LIVE_MAX_ORDERS_PER_DAY", 10))


def totp(secret: str, now: float | None = None) -> str:
    """RFC 6238 time-based one-time password (SHA-1, 30 s, 6 digits) - no extra dependency."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    h = hmac.new(key, struct.pack(">Q", int((now or time.time()) // 30)), hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7fffffff) % 10 ** 6:06d}"


# ----------------------------------------------------------------- storage ---
def init_db() -> None:
    with auth._connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS live_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, ordertype TEXT NOT NULL,
            price REAL, product TEXT NOT NULL, est_value REAL NOT NULL, mode TEXT NOT NULL,
            status TEXT NOT NULL, broker_order_id TEXT, message TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS live_flags (k TEXT PRIMARY KEY, v TEXT NOT NULL);""")


def halted() -> bool:
    if _env("SMC_LIVE_KILL") == "1":
        return True
    with auth._connect() as con:
        r = con.execute("SELECT v FROM live_flags WHERE k='halted'").fetchone()
    return bool(r and r["v"] == "1")


def _orders_today() -> int:
    start = int(datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with auth._connect() as con:
        return con.execute("SELECT COUNT(*) c FROM live_orders WHERE created_at>=? AND status IN ('placed','dry_run','pending')",
                           (start,)).fetchone()["c"]


# --------------------------------------------------------------- broker I/O ---
def _http(method: str, url: str, **kw):          # single choke point (tests replace it)
    return requests.request(method, url, timeout=15, **kw)


def _headers(jwt: str | None = None) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER", "X-SourceID": "WEB",
         "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": _env("SMC_ANGEL_PUBLIC_IP", "127.0.0.1"),
         "X-MACAddress": _env("SMC_ANGEL_MAC", "00:00:00:00:00:00"), "X-PrivateKey": _env("SMC_ANGEL_API_KEY")}
    if jwt:
        h["Authorization"] = "Bearer " + jwt
    return h


class BrokerError(Exception):
    pass


def _login() -> None:
    r = _http("POST", HOST + "/rest/auth/angelbroking/user/v1/loginByPassword", headers=_headers(), json={
        "clientcode": _env("SMC_ANGEL_CLIENT_CODE"), "password": _env("SMC_ANGEL_PIN"),
        "totp": totp(_env("SMC_ANGEL_TOTP_SECRET"))})
    try:
        j = r.json()
    except ValueError:
        raise BrokerError("Angel One login returned an unreadable response.")
    if not j.get("status") or not (j.get("data") or {}).get("jwtToken"):
        raise BrokerError(f"Angel One login failed: {j.get('message') or r.status_code}")
    _sess.update(jwt=j["data"]["jwtToken"], at=time.time())


def call(method: str, path: str, body: dict | None = None) -> dict:
    """Authenticated SmartAPI call. Logs in lazily and once more if the session expired."""
    for attempt in (1, 2):
        with _lock:
            if not _sess["jwt"] or time.time() - _sess["at"] > 8 * 3600 or attempt == 2:
                _login()
            jwt = _sess["jwt"]
        try:
            r = _http(method, HOST + path, headers=_headers(jwt), **({"json": body} if body is not None else {}))
            j = r.json()
        except (requests.RequestException, ValueError):
            raise BrokerError("Could not reach Angel One.")
        if j.get("errorcode") in ("AG8001", "AG8002", "AG8003") and attempt == 1:
            continue
        if not j.get("status"):
            raise BrokerError(j.get("message") or "Angel One rejected the request.")
        return j.get("data")
    raise BrokerError("Angel One session could not be refreshed.")


def scrip(symbol: str) -> dict | None:
    """Map a dashboard symbol (YF:RELIANCE.NS) to Angel One's NSE '-EQ' instrument, via the official scrip master."""
    if not (markets.is_yahoo(symbol) and symbol.endswith(".NS")):
        return None
    if not _scrips["map"] or time.time() - _scrips["t"] > 86400:
        try:
            if os.path.exists(SCRIP_CACHE) and time.time() - os.path.getmtime(SCRIP_CACHE) < 86400:
                m = json.load(open(SCRIP_CACHE))
            else:
                rows = _http("GET", SCRIP_URL).json()
                m = {r["symbol"][:-3]: {"token": r["token"], "symbol": r["symbol"], "lot": r.get("lotsize", "1")}
                     for r in rows if r.get("exch_seg") == "NSE" and r.get("symbol", "").endswith("-EQ")}
                json.dump(m, open(SCRIP_CACHE, "w"))
            _scrips.update(t=time.time(), map=m)
        except (requests.RequestException, ValueError, OSError):
            return None
    return _scrips["map"].get(symbol[3:-3])


def ltp(sc: dict, symbol: str) -> float | None:
    try:
        d = call("POST", "/rest/secure/angelbroking/order/v1/getLtpData",
                 {"exchange": "NSE", "tradingsymbol": sc["symbol"], "symboltoken": sc["token"]})
        return float(d["ltp"])
    except (BrokerError, KeyError, TypeError, ValueError):
        q = _price_fn(symbol)
        return float(q[0]) if q else None


# ------------------------------------------------------------------- guard ---
@bp.before_request
def _guard():
    user = auth.current_user()
    if not allowed(user):
        return jsonify(error="Live trading is not enabled for this account."), 403
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return jsonify(error="Cross-site request blocked."), 403
        if not request.is_json:
            return jsonify(error="Send JSON."), 415


def _body() -> dict:
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return raw + "." + hmac.new(current_app.secret_key.encode() if isinstance(current_app.secret_key, str) else current_app.secret_key,
                                raw.encode(), hashlib.sha256).hexdigest()


def _unsign(tok: str) -> dict | None:
    try:
        raw, sig = tok.rsplit(".", 1)
        key = current_app.secret_key.encode() if isinstance(current_app.secret_key, str) else current_app.secret_key
        if hmac.compare_digest(hmac.new(key, raw.encode(), hashlib.sha256).hexdigest(), sig):
            return json.loads(base64.urlsafe_b64decode(raw))
    except (ValueError, TypeError):
        pass
    return None


# --------------------------------------------------------------------- api ---
def _mode() -> str:
    return "live" if real_orders() else "dry_run"


@bp.get("/api/live/status")
def status():
    return jsonify(available=configured(), mode=_mode(), halted=halted(), max_order_value=max_value(),
                   max_orders_per_day=max_orders(), orders_today=_orders_today(),
                   message=None if configured() else "Angel One credentials are not set on the server.")


@bp.get("/api/live/account")
def account():
    if not configured():
        return jsonify(error="Angel One credentials are not set on the server."), 503
    try:
        funds = call("GET", "/rest/secure/angelbroking/user/v1/getRMS") or {}
        hold = call("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding") or []
        book = call("GET", "/rest/secure/angelbroking/order/v1/getOrderBook") or []
    except BrokerError as e:
        return jsonify(error=str(e)), 502
    with auth._connect() as con:
        mine = [dict(r) for r in con.execute("SELECT symbol, side, qty, ordertype, price, product, mode, status, broker_order_id, "
                                             "message, created_at FROM live_orders ORDER BY id DESC LIMIT 20")]
    return jsonify(cash=funds.get("availablecash"), net=funds.get("net"), holdings=hold[:50],
                   broker_orders=book[:20] if isinstance(book, list) else [], app_orders=mine)


@bp.post("/api/live/preview")
def preview():
    user, d = auth.current_user(), _body()
    if not configured():
        return jsonify(error="Angel One credentials are not set on the server."), 503
    if halted():
        return jsonify(error="Trading is halted (kill switch is on)."), 423
    symbol, side = str(d.get("symbol", "")).upper(), str(d.get("side", "")).lower()
    otype, product = str(d.get("ordertype", "MARKET")).upper(), str(d.get("product", "DELIVERY")).upper()
    if side not in ("buy", "sell") or otype not in ("MARKET", "LIMIT") or product not in ("DELIVERY", "INTRADAY"):
        return jsonify(error="Invalid order."), 400
    sc = scrip(symbol)
    if not sc:
        return jsonify(error="Only NSE stocks (for example RELIANCE.NS) can be traded here."), 400
    try:
        qty = float(d.get("qty"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a quantity."), 400
    if qty != int(qty) or not (1 <= qty <= 100000):
        return jsonify(error="Quantity must be a whole number of shares."), 400
    qty = int(qty)
    if auth._throttled("live-preview", str(user["id"]), 30, 60):
        return jsonify(error="Slow down for a moment."), 429
    auth._record("live-preview", str(user["id"]))
    last = ltp(sc, symbol)
    if not last:
        return jsonify(error="Could not get a live price, so the order was not prepared."), 503
    price = None
    warnings = []
    if otype == "LIMIT":
        try:
            price = round(round(float(d.get("price")) / 0.05) * 0.05, 2)
        except (TypeError, ValueError):
            return jsonify(error="Enter a limit price."), 400
        if price <= 0 or abs(price / last - 1) > 0.20:
            return jsonify(error=f"Limit price {price} is more than 20% away from the last price {last:.2f}. Check for a typo."), 400
    else:
        warnings.append("Market order: the fill price can differ from the last price shown.")
    est = qty * (max(price, last) if price else last)
    if est > max_value():
        return jsonify(error=f"Order value ≈ ₹{est:,.0f} exceeds your per-order limit of ₹{max_value():,.0f}."), 400
    if _orders_today() >= max_orders():
        return jsonify(error=f"Daily limit of {max_orders()} orders reached."), 429
    if side == "buy":
        try:
            cash = float((call("GET", "/rest/secure/angelbroking/user/v1/getRMS") or {}).get("availablecash", 0))
            if product == "DELIVERY" and cash < est:
                warnings.append(f"Available cash ₹{cash:,.0f} looks lower than the order value; the broker may reject it.")
        except (BrokerError, ValueError):
            warnings.append("Could not check your available funds.")
    nonce = secrets.token_hex(12)
    token = _sign({"u": user["id"], "n": nonce, "exp": int(time.time()) + TOKEN_TTL, "o": {
        "symbol": symbol, "side": side, "qty": qty, "ordertype": otype, "price": price, "product": product, "est": est}})
    return jsonify(token=token, expires_in=TOKEN_TTL, mode=_mode(), warnings=warnings, summary={
        "symbol": sc["symbol"], "side": side.upper(), "qty": qty, "ordertype": otype, "price": price, "product": product,
        "last_price": last, "est_value": est})


@bp.post("/api/live/confirm")
def confirm():
    user, d = auth.current_user(), _body()
    p = _unsign(str(d.get("token", "")))
    if not p or p["u"] != user["id"]:
        return jsonify(error="This confirmation is not valid. Start again."), 400
    if time.time() > p["exp"]:
        return jsonify(error="The confirmation expired. Preview the order again."), 400
    if real_orders() and d.get("ack") is not True:
        return jsonify(error="Tick the box to confirm you are placing a REAL order."), 400
    if halted():
        return jsonify(error="Trading is halted (kill switch is on)."), 423
    if _orders_today() >= max_orders():
        return jsonify(error=f"Daily limit of {max_orders()} orders reached."), 429
    o = p["o"]
    sc = scrip(o["symbol"])
    if not sc:
        return jsonify(error="Instrument lookup failed."), 400
    mode = _mode()
    try:                                       # the unique nonce makes a token single-use
        with auth._connect() as con:
            con.execute("INSERT INTO live_orders (nonce, user_id, symbol, side, qty, ordertype, price, product, est_value, mode, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (p["n"], user["id"], o["symbol"], o["side"], o["qty"], o["ordertype"], o["price"], o["product"], o["est"], mode, int(time.time())))
    except sqlite3.IntegrityError:
        return jsonify(error="This order was already submitted."), 409
    status, oid, msg = "dry_run", None, "Dry run: no order was sent to Angel One."
    if mode == "live":
        try:
            r = call("POST", "/rest/secure/angelbroking/order/v1/placeOrder", {
                "variety": "NORMAL", "tradingsymbol": sc["symbol"], "symboltoken": sc["token"],
                "transactiontype": o["side"].upper(), "exchange": "NSE", "ordertype": o["ordertype"],
                "producttype": o["product"], "duration": "DAY", "price": str(o["price"] or 0),
                "squareoff": "0", "stoploss": "0", "quantity": str(o["qty"])})
            status, oid, msg = "placed", (r or {}).get("orderid"), "Order sent to Angel One."
        except BrokerError as e:
            status, msg = "rejected", str(e)
    with auth._connect() as con:
        con.execute("UPDATE live_orders SET status=?, broker_order_id=?, message=? WHERE nonce=?", (status, oid, msg, p["n"]))
    return jsonify(ok=status != "rejected", status=status, mode=mode, order_id=oid, message=msg), (200 if status != "rejected" else 502)


@bp.post("/api/live/cancel")
def cancel():
    oid = str(_body().get("order_id", ""))
    with auth._connect() as con:
        ok = con.execute("SELECT 1 FROM live_orders WHERE broker_order_id=?", (oid,)).fetchone()
    if not oid or not ok:
        return jsonify(error="Only orders placed from this app can be cancelled here."), 404
    try:
        call("POST", "/rest/secure/angelbroking/order/v1/cancelOrder", {"variety": "NORMAL", "orderid": oid})
    except BrokerError as e:
        return jsonify(error=str(e)), 502
    with auth._connect() as con:
        con.execute("UPDATE live_orders SET status='cancelled' WHERE broker_order_id=?", (oid,))
    return jsonify(ok=True)


@bp.post("/api/live/halt")
def halt():
    on = "1" if _body().get("halt") else "0"
    with auth._connect() as con:
        con.execute("INSERT INTO live_flags (k, v) VALUES ('halted', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (on,))
    return jsonify(halted=halted())


def init_app(app, price_fn) -> None:
    global _price_fn
    _price_fn = price_fn
    init_db()
    app.register_blueprint(bp)
