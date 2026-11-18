"""Angel One live-trading tests: python test_angel.py  (fake broker, temp DB, no network, no real orders)"""
import os, tempfile, time
os.environ["SMC_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update(SMC_SECRET_KEY="test", SMC_ANGEL_API_KEY="k", SMC_ANGEL_CLIENT_CODE="A123", SMC_ANGEL_PIN="1234",
                  SMC_ANGEL_TOTP_SECRET="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", SMC_LIVE_EMAILS="owner@x.com",
                  SMC_LIVE_MAX_ORDER_VALUE="10000", SMC_LIVE_MAX_ORDERS_PER_DAY="50")
import angel, app as appmod  # noqa: E402
angel.SCRIP_CACHE = os.path.join(tempfile.mkdtemp(), "s.json")

CALLS, STATE = [], {"expire_once": False, "reject": False, "login_ok": True}
class R:
    status_code = 200
    def __init__(s, j): s._j = j
    def json(s): return s._j
def fake_http(method, url, **kw):
    CALLS.append((method, url.split("angelone.in")[-1], kw.get("json")))
    if "OpenAPIScripMaster" in url:
        return R([{"token": "2885", "symbol": "RELIANCE-EQ", "exch_seg": "NSE", "lotsize": "1"}, {"token": "9", "symbol": "RELIANCE-BE", "exch_seg": "NSE"}])
    if url.endswith("loginByPassword"):
        return R({"status": True, "data": {"jwtToken": "J" + str(len(CALLS))}} if STATE["login_ok"] else {"status": False, "message": "Invalid totp"})
    if STATE["expire_once"] and "getRMS" in url:
        STATE["expire_once"] = False; return R({"status": False, "errorcode": "AG8002", "message": "Token Expired"})
    if url.endswith("getLtpData"): return R({"status": True, "data": {"ltp": 1000.0}})
    if url.endswith("getRMS"): return R({"status": True, "data": {"availablecash": "50000", "net": "50000"}})
    if url.endswith("placeOrder"):
        return R({"status": False, "message": "RMS: insufficient funds"} if STATE["reject"] else {"status": True, "data": {"orderid": "OID1"}})
    if url.endswith("cancelOrder"): return R({"status": True, "data": {"orderid": "OID1"}})
    return R({"status": True, "data": []})
angel._http = fake_http
placed = lambda: [c for c in CALLS if c[1].endswith("placeOrder")]

ok = fail = 0
def check(n, c):
    global ok, fail; ok += bool(c); fail += (not c); print(("PASS " if c else "FAIL ") + n)
def client(email):
    c = appmod.app.test_client(); c.post("/api/auth/signup", json={"name": "Test User", "email": email, "password": "hunter2hunter2"}); return c
prev = lambda c, **kw: c.post("/api/live/preview", json={"symbol": "YF:RELIANCE.NS", "side": "buy", "qty": 3, "ordertype": "MARKET", "product": "DELIVERY", **kw})
conf = lambda c, tok, **kw: c.post("/api/live/confirm", json={"token": tok, **kw})

check("TOTP matches RFC 6238 test vector (287082)", angel.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", now=59) == "287082")
owner, other = client("owner@x.com"), client("other@x.com")
check("non-listed user: every live endpoint is 403", all(other.get(u).status_code == 403 for u in ("/api/live/status", "/api/live/account")) and prev(other).status_code == 403)
check("anonymous blocked", appmod.app.test_client().get("/api/live/status").status_code == 401)
st = owner.get("/api/live/status").json
check("owner sees status; default mode is dry_run", st["available"] and st["mode"] == "dry_run" and not st["halted"])

# ---- dry run
p = prev(owner).json
check("preview resolves RELIANCE-EQ and prices it", p["summary"]["symbol"] == "RELIANCE-EQ" and p["summary"]["est_value"] == 3000.0 and "token" in p)
r = conf(owner, p["token"]).json
check("DRY RUN: confirm succeeds but sends NO order", r["status"] == "dry_run" and not placed())
check("replaying a token is refused (single use)", conf(owner, p["token"]).status_code == 409)
check("tampered token refused", conf(owner, p["token"][:-3] + "abc").status_code == 400)
check("garbage token refused", conf(owner, "nope").status_code == 400)

# ---- validation / limits
check("fractional qty refused", prev(owner, qty=1.5).status_code == 400)
check("order over value limit refused (11 x 1000 > 10000)", prev(owner, qty=11).status_code == 400)
check("BSE / US / crypto symbols refused", all(prev(owner, symbol=s).status_code == 400 for s in ("YF:RELIANCE.BO", "YF:AAPL", "BTCUSDT")))
check("limit price >20% from last price refused", prev(owner, ordertype="LIMIT", price=1500).status_code == 400)
check("limit order inside band accepted, rounded to tick", prev(owner, ordertype="LIMIT", price=999.97).json["summary"]["price"] == 999.95)
check("bad side/product refused", prev(owner, side="short").status_code == 400 and prev(owner, product="MARGIN").status_code == 400)

# ---- live mode
os.environ["SMC_ANGEL_LIVE"] = "1"
p = prev(owner, qty=3).json
check("live preview says mode=live", p["mode"] == "live")
check("live confirm WITHOUT ack refused, nothing sent", conf(owner, p["token"]).status_code == 400 and not placed())
r = conf(owner, p["token"], ack=True).json
body = placed()[0][2] if placed() else {}
check("live confirm places exactly one order", r["status"] == "placed" and r["order_id"] == "OID1" and len(placed()) == 1)
check("order payload correct (NSE, RELIANCE-EQ, token, BUY, qty, DELIVERY)", body.get("tradingsymbol") == "RELIANCE-EQ" and body.get("symboltoken") == "2885"
      and body.get("transactiontype") == "BUY" and body.get("quantity") == "3" and body.get("producttype") == "DELIVERY" and body.get("exchange") == "NSE")
check("replay after live fill refused, still one order", conf(owner, p["token"], ack=True).status_code == 409 and len(placed()) == 1)
other_tok = prev(owner).json["token"]
check("another user cannot use my token", conf(other, other_tok, ack=True).status_code == 403)

# ---- broker rejection, expiry, session refresh, kill switch, cancel
STATE["reject"] = True
r = conf(owner, prev(owner).json["token"], ack=True)
check("broker rejection reported (502) and recorded", r.status_code == 502 and r.json["status"] == "rejected")
STATE["reject"] = False
orig = angel.TOKEN_TTL; angel.TOKEN_TTL = -1
check("expired token refused", conf(owner, prev(owner).json["token"], ack=True).status_code == 400); angel.TOKEN_TTL = orig
STATE["expire_once"] = True; n0 = len([c for c in CALLS if c[1].endswith("loginByPassword")])
check("expired broker session triggers re-login and still works", prev(owner).status_code == 200 and len([c for c in CALLS if c[1].endswith("loginByPassword")]) > n0)
tok = prev(owner).json["token"]
check("kill switch on", owner.post("/api/live/halt", json={"halt": True}).json["halted"])
check("halted: preview AND confirm refused", prev(owner).status_code == 423 and conf(owner, tok, ack=True).status_code == 423)
owner.post("/api/live/halt", json={"halt": False})
check("cancel works only for app-placed orders", owner.post("/api/live/cancel", json={"order_id": "OID1"}).status_code == 200 and owner.post("/api/live/cancel", json={"order_id": "FOREIGN"}).status_code == 404)
os.environ["SMC_LIVE_MAX_ORDERS_PER_DAY"] = "1"
check("daily order cap enforced", prev(owner).status_code == 429)
os.environ["SMC_LIVE_MAX_ORDERS_PER_DAY"] = "50"
STATE["login_ok"] = False; angel._sess["jwt"] = None
check("login failure -> clear error, no crash", owner.get("/api/live/account").status_code == 502)
check("cross-site POST blocked", owner.post("/api/live/preview", json={}, headers={"Origin": "https://evil.example"}).status_code == 403)
print(f"\n{ok} passed, {fail} failed"); raise SystemExit(1 if fail else 0)
