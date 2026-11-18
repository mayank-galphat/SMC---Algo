"""
Payments and plans for SMC Terminal (Razorpay Orders + Checkout).

Flow (money is never trusted to the browser):
  1. POST /api/billing/order    server prices the plan itself, creates a Razorpay order, stores it as 'created'
  2. Razorpay Checkout runs in the browser (UPI / cards / netbanking / wallets)
  3. POST /api/billing/verify   server checks Razorpay's HMAC-SHA256 signature, then grants Pro
  4. POST /api/billing/webhook  Razorpay -> server, signature checked; grants Pro even if the tab was closed
Grants are idempotent: an order can only extend Pro once, however many times verify/webhook fire.

Config (environment variables)
  SMC_RAZORPAY_KEY_ID / SMC_RAZORPAY_KEY_SECRET   from the Razorpay dashboard (use rzp_test_ keys first)
  SMC_RAZORPAY_WEBHOOK_SECRET                     the secret you set when creating the webhook
  SMC_PRICE_MONTHLY / SMC_PRICE_YEARLY            major units, default 499 / 4999
  SMC_CURRENCY                                    default INR
  SMC_BILLING_MODE                                'razorpay' | 'demo' | 'off'  (default: razorpay if keys are set, else off)
  SMC_ADMIN_EMAILS                                comma-separated emails that always have Pro
With billing 'off' nothing is locked, so existing installs keep working until you turn payments on.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time

import requests
from flask import Blueprint, jsonify, request

import auth

bp = Blueprint("billing", __name__)
ORDERS_URL = "https://api.razorpay.com/v1/orders"
DAY = 86400

# Edit this to change what Pro unlocks (enforced on the server in app.py).
PRO_FEATURES = {"ict": "ICT Chart", "backtest": "Backtesting", "sd": "Supply & Demand zones"}


# ----------------------------------------------------------------- config ---
def _key_id() -> str: return os.environ.get("SMC_RAZORPAY_KEY_ID", "").strip()
def _key_secret() -> str: return os.environ.get("SMC_RAZORPAY_KEY_SECRET", "").strip()
def _webhook_secret() -> str: return os.environ.get("SMC_RAZORPAY_WEBHOOK_SECRET", "").strip()
def currency() -> str: return os.environ.get("SMC_CURRENCY", "INR").upper()


def mode() -> str:
    m = os.environ.get("SMC_BILLING_MODE", "").strip().lower()
    if m == "demo":
        return "demo"
    if m == "off":
        return "off"
    return "razorpay" if _key_id() and _key_secret() else "off"


def plans() -> dict:
    def price(env, default):
        try:
            return max(1.0, float(os.environ.get(env, default)))
        except ValueError:
            return float(default)
    return {
        "monthly": {"label": "Pro · 1 month", "price": price("SMC_PRICE_MONTHLY", 499), "days": 30},
        "yearly": {"label": "Pro · 12 months", "price": price("SMC_PRICE_YEARLY", 4999), "days": 365},
    }


def _minor(plan: dict) -> int:
    return int(round(plan["price"] * 100))


# ---------------------------------------------------------------- storage ---
def init_db() -> None:
    with auth._connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS entitlements (
            user_id INTEGER PRIMARY KEY, pro_until INTEGER NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, provider TEXT NOT NULL,
            order_id TEXT NOT NULL UNIQUE, payment_id TEXT UNIQUE, plan TEXT NOT NULL,
            amount INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
            created_at INTEGER NOT NULL, paid_at INTEGER)""")


def pro_until(uid: int) -> int:
    with auth._connect() as con:
        r = con.execute("SELECT pro_until FROM entitlements WHERE user_id = ?", (uid,)).fetchone()
    return int(r["pro_until"]) if r else 0


def _is_admin(email: str) -> bool:
    return email.lower() in {e.strip().lower() for e in os.environ.get("SMC_ADMIN_EMAILS", "").split(",") if e.strip()}


def is_pro(user) -> bool:
    return bool(user) and (_is_admin(user["email"]) or pro_until(user["id"]) > time.time())


def locked() -> bool:
    """True when billing is on and the signed-in user is on the free plan."""
    return mode() != "off" and not is_pro(auth.current_user())


def gate(feature: str):
    """Return a 402 response if `feature` is Pro-only and the user is free, else None."""
    if locked():
        return jsonify(error=f"{PRO_FEATURES[feature]} is a Pro feature.", upgrade=True, feature=feature), 402
    return None


def _grant(order_id: str, payment_id: str) -> bool:
    """Mark the order paid and extend Pro. Returns True only the first time (idempotent)."""
    now = int(time.time())
    with auth._connect() as con:
        row = con.execute("SELECT user_id, plan FROM payments WHERE order_id = ?", (order_id,)).fetchone()
        if not row:
            return False
        try:
            cur = con.execute("UPDATE payments SET status='paid', payment_id=?, paid_at=? "
                              "WHERE order_id=? AND status!='paid'", (payment_id, now, order_id))
        except sqlite3.IntegrityError:   # this payment id already settled a different order: never grant twice
            return False
        if cur.rowcount != 1:
            return False
        days = plans().get(row["plan"], {"days": 30})["days"]
        have = con.execute("SELECT pro_until FROM entitlements WHERE user_id=?", (row["user_id"],)).fetchone()
        until = max(now, int(have["pro_until"]) if have else 0) + days * DAY
        con.execute("INSERT INTO entitlements (user_id, pro_until) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET pro_until = excluded.pro_until", (row["user_id"], until))
    return True


# ------------------------------------------------------------------ guard ---
@bp.before_request
def _guard():
    if request.method == "POST" and request.path != "/api/billing/webhook":
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return jsonify(error="Cross-site request blocked."), 403
        if not request.is_json:
            return jsonify(error="Send JSON."), 415


def _body() -> dict:
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _sig(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


# -------------------------------------------------------------------- api ---
@bp.get("/api/billing/status")
def status():
    user = auth.current_user()
    until = pro_until(user["id"]) if user else 0
    pro = is_pro(user)
    with auth._connect() as con:
        pays = con.execute("SELECT plan, amount, currency, paid_at FROM payments WHERE user_id=? AND status='paid' "
                           "ORDER BY paid_at DESC LIMIT 5", (user["id"],)).fetchall()
    return jsonify(
        enabled=mode() != "off", mode=mode(), pro=pro or mode() == "off", pro_until=until or None,
        admin=_is_admin(user["email"]), currency=currency(), key_id=_key_id() if mode() == "razorpay" else None,
        plans=[{"id": k, "label": v["label"], "price": v["price"], "days": v["days"]} for k, v in plans().items()],
        features=PRO_FEATURES, payments=[dict(p) for p in pays])


@bp.post("/api/billing/order")
def create_order():
    if mode() != "razorpay":
        return jsonify(error="Online payments are not enabled."), 503
    user = auth.current_user()
    plan_id = str(_body().get("plan", ""))
    plan = plans().get(plan_id)
    if not plan:
        return jsonify(error="Unknown plan."), 400
    wait = auth._throttled("order", str(user["id"]), 10, 3600)
    if wait:
        return jsonify(error="Too many payment attempts. Try again later."), 429
    auth._record("order", str(user["id"]))
    amount, cur = _minor(plan), currency()
    try:
        r = requests.post(ORDERS_URL, auth=(_key_id(), _key_secret()), timeout=12, json={
            "amount": amount, "currency": cur, "receipt": f"u{user['id']}-{int(time.time())}",
            "notes": {"user_id": str(user["id"]), "plan": plan_id}})
        order = r.json()
    except (requests.RequestException, ValueError):
        return jsonify(error="Could not reach the payment provider. Please try again."), 502
    if r.status_code != 200 or not order.get("id"):
        return jsonify(error="The payment provider rejected the request. Check the API keys."), 502
    with auth._connect() as con:
        con.execute("INSERT INTO payments (user_id, provider, order_id, plan, amount, currency, status, created_at) "
                    "VALUES (?, 'razorpay', ?, ?, ?, ?, 'created', ?)",
                    (user["id"], order["id"], plan_id, amount, cur, int(time.time())))
    return jsonify(order_id=order["id"], amount=amount, currency=cur, key_id=_key_id(), label=plan["label"])


@bp.post("/api/billing/verify")
def verify():
    if mode() != "razorpay":
        return jsonify(error="Online payments are not enabled."), 503
    user, d = auth.current_user(), _body()
    oid, pid, sig = (str(d.get(k, "")) for k in ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature"))
    with auth._connect() as con:  # the order must belong to the signed-in user
        row = con.execute("SELECT status FROM payments WHERE order_id=? AND user_id=?", (oid, user["id"])).fetchone()
    if not row:
        return jsonify(error="Unknown order."), 404
    if not hmac.compare_digest(_sig(_key_secret(), f"{oid}|{pid}".encode()), sig):
        return jsonify(error="Payment signature did not match. No access was granted."), 400
    _grant(oid, pid)
    return jsonify(ok=True, pro_until=pro_until(user["id"]))


@bp.post("/api/billing/demo-pay")
def demo_pay():
    """Simulated payment for trying the flow without Razorpay. Only exists when SMC_BILLING_MODE=demo."""
    if mode() != "demo":
        return jsonify(error="Demo payments are disabled."), 403
    user, plan_id = auth.current_user(), str(_body().get("plan", ""))
    if plan_id not in plans():
        return jsonify(error="Unknown plan."), 400
    oid = f"demo_{user['id']}_{time.time_ns()}"
    with auth._connect() as con:
        con.execute("INSERT INTO payments (user_id, provider, order_id, plan, amount, currency, status, created_at) "
                    "VALUES (?, 'demo', ?, ?, ?, ?, 'created', ?)",
                    (user["id"], oid, plan_id, _minor(plans()[plan_id]), currency(), int(time.time())))
    _grant(oid, "pay_" + oid)
    return jsonify(ok=True, pro_until=pro_until(user["id"]))


@bp.post("/api/billing/webhook")
def webhook():
    secret = _webhook_secret()
    if mode() != "razorpay" or not secret:
        return jsonify(error="Webhook not configured."), 503
    raw = request.get_data()
    if not hmac.compare_digest(_sig(secret, raw), request.headers.get("X-Razorpay-Signature", "")):
        return jsonify(error="Bad signature."), 400
    try:
        ev = json.loads(raw)
        if ev.get("event") in ("payment.captured", "order.paid"):
            pay = ev["payload"]["payment"]["entity"]
            with auth._connect() as con:
                row = con.execute("SELECT amount, currency FROM payments WHERE order_id=?",
                                  (pay.get("order_id"),)).fetchone()
            if (row and pay.get("status") == "captured" and pay.get("amount") == row["amount"]
                    and pay.get("currency") == row["currency"]):
                _grant(pay["order_id"], pay["id"])
    except (ValueError, KeyError, TypeError):
        pass  # malformed but correctly signed: acknowledge so Razorpay does not retry forever
    return jsonify(ok=True)


def init_app(app) -> None:
    init_db()
    app.register_blueprint(bp)
