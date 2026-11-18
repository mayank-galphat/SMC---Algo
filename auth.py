"""
User accounts for SMC Terminal: sign up, log in, log out.

Design notes
  * Users live in a local SQLite file (users.db next to this script, or $SMC_DB).
  * Passwords are never stored; only a salted werkzeug hash (scrypt by default).
  * The login state is a signed, HttpOnly, SameSite=Lax session cookie.
    "Remember me" makes it last 30 days, otherwise it ends with the browser.
  * State-changing endpoints accept JSON only and reject a mismatching Origin,
    which blocks cross-site form posts (CSRF).
  * Failed logins are throttled per client IP + email, and sign-ups per IP.
  * Unknown emails and wrong passwords return the same message (and take about
    the same time), so the form doesn't reveal which emails are registered.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SMC_DB", os.path.join(BASE_DIR, "users.db"))

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
REMEMBER_DAYS = 30
LOGIN_MAX_FAILS, LOGIN_WINDOW = 5, 300        # 5 bad attempts per 5 minutes
SIGNUP_MAX, SIGNUP_WINDOW = 10, 3600          # 10 sign-ups per hour per IP

_attempts: dict[tuple, deque] = defaultdict(deque)
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))  # equalises timing for unknown emails


# ---------------------------------------------------------------- storage ---
def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )""")


def load_secret_key() -> str:
    """Stable signing key: $SMC_SECRET_KEY, else a random one saved to .secret_key."""
    env = os.environ.get("SMC_SECRET_KEY")
    if env:
        return env
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path) as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


# ---------------------------------------------------------------- helpers ---
def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _throttled(bucket: str, key: str, limit: int, window: int) -> int:
    """Seconds until the next attempt is allowed (0 = allowed)."""
    q = _attempts[(bucket, key)]
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    return int(window - (now - q[0])) + 1 if len(q) >= limit else 0


def _record(bucket: str, key: str) -> None:
    _attempts[(bucket, key)].append(time.time())


def _fail(message: str, status: int = 400, field: str | None = None, **extra):
    body = {"error": message, **extra}
    if field:
        body["field"] = field
    return jsonify(body), status


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _public(row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with _connect() as con:
        return con.execute("SELECT id, name, email FROM users WHERE id = ?", (uid,)).fetchone()


def _start_session(row, remember: bool) -> None:
    session.clear()  # new session on every login (prevents fixation)
    session["uid"] = row["id"]
    session.permanent = bool(remember)


def validate_password(pw: str) -> str | None:
    if len(pw) < 8:
        return "Use at least 8 characters."
    if len(pw) > 128:
        return "Use 128 characters or fewer."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Include at least one letter and one number."
    return None


# ----------------------------------------------------------------- guards ---
@bp.before_app_request
def _guard():
    path = request.path
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/auth/"):
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return _fail("Cross-site request blocked.", 403)
        if not request.is_json:
            return _fail("Send JSON.", 415)

    public = path in ("/login", "/signup", "/api/billing/webhook") or path.startswith("/api/auth/") or (
        path.startswith("/static/") and path != "/static/index.html")
    if public:
        return None
    if current_user():
        if path == "/static/index.html":
            return redirect("/")
        return None
    if path.startswith("/api/"):
        return _fail("Please sign in to continue.", 401, login=True)
    return redirect("/login")


@bp.after_app_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path in ("/", "/login", "/signup") or request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ pages ---
@bp.route("/login")
def login_page():
    if current_user():
        return redirect("/")
    return send_from_directory(current_app.static_folder, "login.html")


@bp.route("/signup")
def signup_page():
    if current_user():
        return redirect("/")
    return send_from_directory(current_app.static_folder, "signup.html")


# -------------------------------------------------------------------- api ---
@bp.post("/api/auth/signup")
def signup():
    ip = _client_ip()
    wait = _throttled("signup", ip, SIGNUP_MAX, SIGNUP_WINDOW)
    if wait:
        return _fail("Too many sign-ups from this network. Try again later.", 429, retry_after=wait)

    d = _payload()
    name = re.sub(r"\s+", " ", str(d.get("name", ""))).strip()
    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))

    if not 2 <= len(name) <= 50:
        return _fail("Enter your name (2 to 50 characters).", field="name")
    if len(email) > 254 or not EMAIL_RE.match(email):
        return _fail("Enter a valid email address.", field="email")
    problem = validate_password(password)
    if problem:
        return _fail(problem, field="password")

    _record("signup", ip)
    try:
        with _connect() as con:
            cur = con.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                              (name, email, generate_password_hash(password)))
            row = {"id": cur.lastrowid, "name": name, "email": email}
    except sqlite3.IntegrityError:
        return _fail("An account with this email already exists. Try signing in.", 409, field="email")

    _start_session(row, remember=True)
    return jsonify(user=_public(row)), 201


@bp.post("/api/auth/login")
def login():
    d = _payload()
    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))
    remember = bool(d.get("remember"))
    key = f"{_client_ip()}|{email}"

    wait = _throttled("login", key, LOGIN_MAX_FAILS, LOGIN_WINDOW)
    if wait:
        mins = max(1, round(wait / 60))
        return _fail(f"Too many attempts. Try again in about {mins} minute{'s' if mins != 1 else ''}.",
                     429, retry_after=wait)
    if not email or not password:
        return _fail("Enter your email and password.")

    with _connect() as con:
        row = con.execute("SELECT id, name, email, password_hash FROM users WHERE email = ?",
                          (email,)).fetchone()
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
    if not (row and ok):
        _record("login", key)
        return _fail("Incorrect email or password.", 401)

    _attempts.pop(("login", key), None)
    _start_session(row, remember)
    return jsonify(user=_public(row))


@bp.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@bp.get("/api/auth/me")
def me():
    user = current_user()
    if not user:
        return _fail("Not signed in.", 401)
    return jsonify(user=_public(user))


def init_app(app) -> None:
    """Wire sessions, the database and the routes into the Flask app."""
    app.secret_key = load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SMC_HTTPS") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=REMEMBER_DAYS),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    init_db()
    app.register_blueprint(bp)
