"""
Checks signup, login, logout, route protection and the abuse limits.

    python test_auth.py

Uses a throw-away database, so it never touches your real users.db.
"""
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["SMC_DB"] = os.path.join(_tmp, "test_users.db")
os.environ["SMC_SECRET_KEY"] = "test-secret-key"

import app as smc_app  # noqa: E402  (must import after the env vars are set)
import auth  # noqa: E402

OK = {"name": "Mayank Test", "email": "Mayank@Example.com", "password": "hunter2hunter2"}


def client():
    return smc_app.app.test_client()


def post(c, path, body=None, **kw):
    return c.post(path, json=body if body is not None else {}, **kw)


def main():
    # ---- logged out: everything private is locked
    c = client()
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login"), "dashboard must redirect"
    assert c.get("/static/index.html").status_code == 302, "static dashboard page must be guarded"
    for p in ("/api/config", "/api/search", "/api/candles", "/api/smc", "/api/ict", "/api/backtest"):
        assert c.get(p).status_code == 401, f"{p} must require login"
    assert c.get("/login").status_code == 200 and c.get("/signup").status_code == 200
    assert c.get("/static/auth.css").status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    print("ok  logged-out access is blocked, public pages load")

    # ---- signup validation
    for body, field in [
        ({**OK, "name": "A"}, "name"),
        ({**OK, "email": "not-an-email"}, "email"),
        ({**OK, "password": "short1"}, "password"),
        ({**OK, "password": "onlyletters"}, "password"),
        ({**OK, "password": "12345678"}, "password"),
    ]:
        r = post(c, "/api/auth/signup", body)
        assert r.status_code == 400 and r.get_json()["field"] == field, (body, r.get_json())
    print("ok  signup validation (name, email, password rules)")

    # ---- signup + auto login
    r = post(c, "/api/auth/signup", OK)
    assert r.status_code == 201, r.get_json()
    user = r.get_json()["user"]
    assert user["email"] == "mayank@example.com" and "password" not in user
    assert c.get("/api/auth/me").get_json()["user"]["name"] == "Mayank Test"
    assert c.get("/").status_code == 200 and c.get("/api/config").status_code == 200
    assert c.get("/login").status_code == 302, "logged-in users skip the login page"
    print("ok  signup creates the account and signs in")

    # ---- password is hashed
    with sqlite3.connect(os.environ["SMC_DB"]) as con:
        stored = con.execute("SELECT password_hash FROM users").fetchone()[0]
    assert OK["password"] not in stored and stored.count("$") >= 2
    print("ok  password stored as a salted hash")

    # ---- duplicate email (case-insensitive)
    r = post(client(), "/api/auth/signup", {**OK, "email": "MAYANK@example.COM", "name": "Other"})
    assert r.status_code == 409 and r.get_json()["field"] == "email"
    print("ok  duplicate email rejected")

    # ---- logout
    assert post(c, "/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401 and c.get("/").status_code == 302
    print("ok  logout ends the session")

    # ---- login: same message for wrong password and unknown email
    c2 = client()
    bad = post(c2, "/api/auth/login", {"email": OK["email"], "password": "wrongpass1"})
    unk = post(c2, "/api/auth/login", {"email": "nobody@example.com", "password": "wrongpass1"})
    assert bad.status_code == unk.status_code == 401
    assert bad.get_json()["error"] == unk.get_json()["error"]
    print("ok  wrong password and unknown email look identical")

    # ---- remember me controls cookie lifetime
    c3 = client()
    r = post(c3, "/api/auth/login", {"email": OK["email"], "password": OK["password"], "remember": False})
    cookie = r.headers["Set-Cookie"]
    assert r.status_code == 200 and "Expires" not in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    c4 = client()
    r = post(c4, "/api/auth/login", {"email": OK["email"], "password": OK["password"], "remember": True})
    assert "Expires" in r.headers["Set-Cookie"]
    print("ok  session cookie: HttpOnly, SameSite=Lax, persistent only with 'remember me'")

    # ---- brute-force throttle (5 bad attempts, then locked even for the right password)
    c5 = client()
    auth._attempts.clear()
    for _ in range(auth.LOGIN_MAX_FAILS):
        assert post(c5, "/api/auth/login", {"email": OK["email"], "password": "nope12345"}).status_code == 401
    r = post(c5, "/api/auth/login", {"email": OK["email"], "password": OK["password"]})
    assert r.status_code == 429 and "retry_after" in r.get_json()
    print("ok  login throttled after 5 failed attempts")

    # ---- CSRF protections
    auth._attempts.clear()
    c6 = client()
    r = post(c6, "/api/auth/login", {"email": OK["email"], "password": OK["password"]},
             headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = c6.post("/api/auth/login", data="email=a&password=b", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 415
    print("ok  cross-site and non-JSON posts rejected")

    # ---- signup rate limit
    auth._attempts.clear()
    c7 = client()
    for i in range(auth.SIGNUP_MAX):
        r = post(c7, "/api/auth/signup", {"name": "User", "email": f"u{i}@example.com", "password": "password123"})
        assert r.status_code == 201, r.get_json()
        post(c7, "/api/auth/logout")
    r = post(c7, "/api/auth/signup", {"name": "User", "email": "extra@example.com", "password": "password123"})
    assert r.status_code == 429
    print("ok  sign-ups rate limited per IP")

    print("\nALL AUTH TESTS PASSED")


if __name__ == "__main__":
    main()
