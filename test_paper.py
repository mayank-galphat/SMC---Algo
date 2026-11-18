"""Practice-trading tests: python test_paper.py   (temp database, fake prices, no network)"""
import os, tempfile, threading
os.environ["SMC_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update(SMC_SECRET_KEY="test", SMC_PAPER_INR="100000", SMC_PAPER_USD="10000")
import app as appmod, paper  # noqa: E402

PRICES = {"YF:RELIANCE.NS": 1000.0, "YF:AAPL": 200.0, "BTCUSDT": 50000.0}
paper._price_fn = lambda s: (PRICES[s], 1700000000) if s in PRICES else None
ok = fail = 0
def check(name, cond):
    global ok, fail; ok += bool(cond); fail += (not cond); print(("PASS " if cond else "FAIL ") + name)
def client(email):
    c = appmod.app.test_client(); c.post("/api/auth/signup", json={"name": "Test User", "email": email, "password": "hunter2hunter2"}); return c
order = lambda c, s, side, q, **x: c.post("/api/paper/order", json={"symbol": s, "side": side, "qty": q, **x})
wal = lambda c, cur: next(w for w in c.get("/api/paper/portfolio").json["wallets"] if w["currency"] == cur)

a, b = client("a@x.com"), client("b@x.com")
check("anonymous blocked", appmod.app.test_client().post("/api/paper/order", json={}).status_code == 401)
check("new user starts with Rs 1,00,000 and $10,000", wal(a, "INR")["cash"] == 100000 and wal(a, "USD")["cash"] == 10000)

r = order(a, "YF:RELIANCE.NS", "buy", 10, price=1).json
check("buy fills at the SERVER price (client price ignored)", r["price"] == 1000.0 and abs(r["fee"] - 5.0) < 1e-9)
check("cash = 100000 - 10000 - fee 5 (INR wallet only)", abs(wal(a, "INR")["cash"] - 89995) < 1e-6 and wal(a, "USD")["cash"] == 10000)
PRICES["YF:RELIANCE.NS"] = 1100.0
p = a.get("/api/paper/portfolio").json["positions"][0]
check("position shows live unrealised P&L (+1000)", p["qty"] == 10 and abs(p["pnl"] - 1000) < 1e-6)
order(a, "YF:RELIANCE.NS", "buy", 10)
check("average price blends (1000 and 1100 -> 1050)", abs(a.get("/api/paper/portfolio").json["positions"][0]["avg_price"] - 1050) < 1e-6)
r = order(a, "YF:RELIANCE.NS", "sell", 5).json
check("sell realises P&L (50*5 - fee 2.75)", abs(r["pnl"] - (250 - 2.75)) < 1e-6)
check("cannot sell more than held (no shorting)", order(a, "YF:RELIANCE.NS", "sell", 50).status_code == 400)
check("cannot buy beyond cash", order(a, "YF:RELIANCE.NS", "buy", 1000).status_code == 400)
check("selling a stock you never bought is rejected", order(a, "YF:AAPL", "sell", 1).status_code == 400)
check("stocks must be whole shares", order(a, "YF:AAPL", "buy", 1.5).status_code == 400)
check("crypto allows fractions, USD wallet", order(a, "BTCUSDT", "buy", 0.05).status_code == 200 and wal(a, "USD")["cash"] < 7500)
check("indices / forex / futures refused", all(order(a, s, "buy", 1).status_code == 400 for s in ("YF:^NSEI", "YF:EURUSD=X", "YF:GC=F")))
check("no price -> order not placed (503)", order(a, "YF:TCS.NS", "buy", 1).status_code == 503)
check("bad qty / side / negative", all(order(a, "BTCUSDT", sd, q).status_code == 400 for sd, q in (("buy", -1), ("buy", 0), ("hold", 1), ("buy", "abc"), ("buy", None))))
check("other users are isolated", wal(b, "INR")["cash"] == 100000 and b.get("/api/paper/portfolio").json["positions"] == [])

# double-submit race: 2 simultaneous buys that together exceed cash -> only one may succeed
c = client("c@x.com"); res2 = []
def fire(cl): res2.append(order(cl, "YF:AAPL", "buy", 30).status_code)   # 30 * 200 = $6000 each, cash $10,000
ths = [threading.Thread(target=fire, args=(c,)) for _ in range(2)]
[x.start() for x in ths]; [x.join() for x in ths]
check("double-submit cannot overspend (one 200, one 400)", sorted(res2) == [200, 400] and wal(c, "USD")["cash"] > 0)

ords = a.get("/api/paper/portfolio").json["orders"]
check("order history recorded", len(ords) >= 4 and ords[0]["side"] == "buy")
check("cross-site POST blocked", a.post("/api/paper/order", json={"symbol": "BTCUSDT", "side": "buy", "qty": 1}, headers={"Origin": "https://evil.example"}).status_code == 403)
a.post("/api/paper/reset", json={})
pf = a.get("/api/paper/portfolio").json
check("reset restores cash and clears positions", wal(a, "INR")["cash"] == 100000 and pf["positions"] == [] and pf["orders"] == [])
print(f"\n{ok} passed, {fail} failed"); raise SystemExit(1 if fail else 0)
