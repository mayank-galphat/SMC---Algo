"""Billing tests: python test_billing.py   (uses a temp database and a fake Razorpay; no network, no real money)"""
import hashlib, hmac, json, os, tempfile, time

os.environ["SMC_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update(SMC_SECRET_KEY="test", SMC_RAZORPAY_KEY_ID="rzp_test_x", SMC_RAZORPAY_KEY_SECRET="sekret",
                  SMC_RAZORPAY_WEBHOOK_SECRET="whsec", SMC_PRICE_MONTHLY="499", SMC_PRICE_YEARLY="4999")
import app as appmod, billing  # noqa: E402

N = {"orders": 0}
class FakeResp:
    status_code = 200
    def __init__(self, j): self._j = j
    def json(self): return self._j
def fake_post(url, **kw):
    N["orders"] += 1; N["last"] = kw["json"]
    return FakeResp({"id": f"order_T{N['orders']}"})
billing.requests.post = fake_post

ok = fail = 0
def check(name, cond):
    global ok, fail
    ok += cond; fail += (not cond); print(("PASS " if cond else "FAIL ") + name)

def client(email):
    c = appmod.app.test_client()
    c.post("/api/auth/signup", json={"name": "Test User", "email": email, "password": "hunter2hunter2"})
    return c
sign = lambda s, m: hmac.new(s.encode(), m, hashlib.sha256).hexdigest()

a, b = client("a@x.com"), client("b@x.com")
check("free user: ICT blocked (402)", a.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 402)
check("free user: backtest blocked (402)", a.get("/api/backtest?symbol=BTCUSDT&interval=1h").status_code == 402)
smc = a.get("/api/smc?symbol=BTCUSDT&interval=1h").json
check("free user: SMC works, S/D hidden", smc["sd_locked"] and smc["sd_zones"] == [])
check("anonymous cannot create orders", appmod.app.test_client().post("/api/billing/order", json={"plan": "monthly"}).status_code == 401)

check("unknown plan rejected", a.post("/api/billing/order", json={"plan": "free-lunch"}).status_code == 400)
o = a.post("/api/billing/order", json={"plan": "monthly", "amount": 1}).json
check("server sets the price, client cannot (49900 paise)", N["last"]["amount"] == 49900 and o["amount"] == 49900)
oid = o["order_id"]
check("forged signature rejected, no access", a.post("/api/billing/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": "pay_1", "razorpay_signature": "deadbeef"}).status_code == 400
      and a.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 402)
good = sign("sekret", f"{oid}|pay_1".encode())
check("another user cannot claim this order", b.post("/api/billing/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": "pay_1", "razorpay_signature": good}).status_code == 404)
r = a.post("/api/billing/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": "pay_1", "razorpay_signature": good})
check("valid signature grants Pro", r.status_code == 200 and a.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 200)
until1 = billing.pro_until(1)
check("30 days granted", 29.9 * 86400 < until1 - time.time() < 30.1 * 86400)
a.post("/api/billing/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": "pay_1", "razorpay_signature": good})
check("replaying verify does not extend again", billing.pro_until(1) == until1)

ev = lambda oid_, amt=49900, st="captured", pid="pay_1": json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {"id": pid, "order_id": oid_, "amount": amt, "currency": "INR", "status": st}}}}).encode()
anon = appmod.app.test_client()
body = ev(oid)
check("webhook without/with bad signature rejected", anon.post("/api/billing/webhook", data=body, headers={"X-Razorpay-Signature": "x"}).status_code == 400)
anon.post("/api/billing/webhook", data=body, headers={"X-Razorpay-Signature": sign("whsec", body)})
check("webhook after verify does not double-grant", billing.pro_until(1) == until1)

o2 = b.post("/api/billing/order", json={"plan": "yearly"}).json
bad = ev(o2["order_id"], amt=100, pid="pay_2")
anon.post("/api/billing/webhook", data=bad, headers={"X-Razorpay-Signature": sign("whsec", bad)})
check("webhook with wrong amount grants nothing", b.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 402)
good2 = ev(o2["order_id"], amt=499900, pid="pay_3")
check("webhook alone (tab closed) grants Pro", anon.post("/api/billing/webhook", data=good2, headers={"X-Razorpay-Signature": sign("whsec", good2)}).status_code == 200
      and 364 * 86400 < billing.pro_until(2) - time.time() < 366 * 86400)
st = a.get("/api/billing/status").json
o3 = a.post("/api/billing/order", json={"plan": "monthly"}).json
until_a = billing.pro_until(1)
dup = ev(o3["order_id"], amt=49900, pid="pay_1")
check("reused payment id cannot settle a second order (no 500)", anon.post("/api/billing/webhook", data=dup, headers={"X-Razorpay-Signature": sign("whsec", dup)}).status_code == 200 and billing.pro_until(1) == until_a)
check("status reports pro + history", st["pro"] and st["payments"] and st["mode"] == "razorpay")
check("demo-pay disabled in razorpay mode", a.post("/api/billing/demo-pay", json={"plan": "monthly"}).status_code == 403)
check("cross-site POST blocked", a.post("/api/billing/order", json={"plan": "monthly"}, headers={"Origin": "https://evil.example"}).status_code == 403)

os.environ["SMC_ADMIN_EMAILS"] = "c@x.com"; cc = client("c@x.com")
check("admin email always Pro", cc.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 200)
os.environ["SMC_BILLING_MODE"] = "off"; d = client("d@x.com")
check("billing off: nothing locked", d.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 200)
os.environ["SMC_BILLING_MODE"] = "demo"; e = client("e@x.com")
check("demo mode grants Pro", e.post("/api/billing/demo-pay", json={"plan": "monthly"}).status_code == 200
      and e.get("/api/ict?symbol=BTCUSDT&interval=1h").status_code == 200)
print(f"\n{ok} passed, {fail} failed"); raise SystemExit(1 if fail else 0)
