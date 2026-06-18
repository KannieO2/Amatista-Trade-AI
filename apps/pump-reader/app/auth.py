"""App login — multi-user, signed-cookie sessions.

Auth turns ON only when APP_PASSWORD is set (so paper/dev runs stay open). The
env APP_USERNAME / APP_PASSWORD is the bootstrap ADMIN: it always works, even if
Supabase is down, so nobody can be locked out of their own box. Additional
accounts live in the Supabase `app_users` table and are created by an admin from
the dashboard. Passwords are stored as pbkdf2_sha256 (stdlib, no extra deps).

Each account is its own bot tenant: the session token carries the user id so
per-user trading state (balance, positions, PnL) can be isolated. The shared
intelligence (scam-pump learning, market scan) stays global — see main.py.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import store

logger = logging.getLogger("pump-reader.auth")

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET_KEY = os.getenv("APP_SECRET_KEY", "tradeos-dev-secret-change-me")
COOKIE = "tradeos_session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 days
OWNER_UID = "owner"  # stable per-user key for the env bootstrap admin
_PBKDF2_ITER = 240_000

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="tradeos-auth")

# username -> {"id","role","active","password_hash"} ; refreshed from Supabase.
_users: dict[str, dict] = {}


def auth_enabled() -> bool:
    return bool(APP_PASSWORD)


# --- password hashing (stdlib pbkdf2) ---------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# --- user cache (synced from Supabase) --------------------------------------

async def load_users() -> None:
    """Refresh the in-memory user cache from Supabase. Best-effort: on failure
    the bootstrap admin still works so nobody gets locked out."""
    try:
        rows = await store.list_users_with_hash()
    except Exception:
        logger.exception("load_users failed")
        return
    cache: dict[str, dict] = {}
    for r in rows:
        u = (r.get("username") or "").strip()
        if u:
            cache[u] = {
                "id": r.get("id"),
                "role": r.get("role") or "operator",
                "active": bool(r.get("active", True)),
                "password_hash": r.get("password_hash") or "",
            }
    _users.clear()
    _users.update(cache)


# --- authentication ----------------------------------------------------------

def authenticate(username: str, password: str) -> dict | None:
    """Return {"id","username","role"} on success, else None."""
    if not auth_enabled():
        return None
    username = (username or "").strip()
    # bootstrap admin from env — always available, even if Supabase is down.
    if username == APP_USERNAME and password == APP_PASSWORD:
        return {"id": OWNER_UID, "username": username, "role": "admin"}
    u = _users.get(username)
    if u and u["active"] and u["password_hash"] and verify_password(password, u["password_hash"]):
        return {"id": u["id"], "username": username, "role": u["role"]}
    return None


# --- tokens ------------------------------------------------------------------

def make_token(user: dict) -> str:
    return _serializer.dumps({"u": user["username"], "uid": user["id"], "r": user["role"]})


def read_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=MAX_AGE)
        return {"username": data.get("u"), "id": data.get("uid"), "role": data.get("r", "operator")}
    except (BadSignature, SignatureExpired, Exception):
        return None


def valid_token(token: str | None) -> bool:
    return read_token(token) is not None


# --- admin user management ---------------------------------------------------

async def create_user(username: str, password: str, role: str = "operator") -> dict:
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("username and password required")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    if role not in ("operator", "admin"):
        role = "operator"
    if username == APP_USERNAME:
        raise ValueError("username reserved by the owner account")
    if username in _users:
        raise ValueError("username already exists")
    if not store.enabled():
        raise ValueError("Supabase no configurado: no se pueden crear cuentas extra")
    row = {"username": username, "password_hash": hash_password(password), "role": role, "active": True}
    saved = await store.insert_user(row)
    await load_users()
    return {"username": username, "role": role, "id": (saved or {}).get("id")}


async def set_active(user_id: str, active: bool) -> None:
    await store.update_user(user_id, {"active": active})
    await load_users()


async def set_password(user_id: str, password: str) -> None:
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    await store.update_user(user_id, {"password_hash": hash_password(password)})
    await load_users()


def list_users() -> list[dict]:
    out = [{"id": OWNER_UID, "username": APP_USERNAME, "role": "admin", "active": True, "owner": True}]
    for name, u in sorted(_users.items()):
        out.append({"id": u["id"], "username": name, "role": u["role"], "active": u["active"], "owner": False})
    return out


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TradeOS AI · Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@500&display=swap" rel="stylesheet"/>
<style>
  *{box-sizing:border-box} html,body{height:100%}
  body{margin:0;font-family:Geist,system-ui,sans-serif;background:#070a0f;color:#e6e9ef;
    display:flex;align-items:center;justify-content:center;
    background-image:radial-gradient(800px 400px at 20% 0%,rgba(255,47,110,.10),transparent),radial-gradient(700px 400px at 90% 100%,rgba(124,108,255,.10),transparent)}
  .card{width:340px;max-width:92vw;background:rgba(16,21,30,.72);border:1px solid rgba(255,255,255,.08);
    border-radius:16px;padding:26px 24px;backdrop-filter:blur(14px);box-shadow:0 40px 90px -20px rgba(0,0,0,.8)}
  .logo{display:flex;align-items:center;gap:10px;margin-bottom:18px}
  .dot{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,#ff2f6e,#7c6cff)}
  h1{font-size:16px;margin:0;font-weight:600} p{margin:2px 0 0;color:#8b95a7;font-size:12px}
  label{display:block;font-size:11px;color:#8b95a7;margin:14px 0 5px}
  input{width:100%;background:#0c1018;border:1px solid #222b3a;border-radius:9px;color:#e6e9ef;
    padding:10px 12px;font-family:inherit;font-size:13px;outline:none}
  input:focus{border-color:#3a4760}
  button{width:100%;margin-top:18px;background:linear-gradient(90deg,#ff2f6e,#ff5a86);border:0;color:#fff;
    padding:11px;border-radius:9px;font-weight:600;font-size:13px;cursor:pointer;font-family:inherit}
  .err{color:#ff6b6b;font-size:12px;margin-top:12px;min-height:14px}
</style></head><body>
  <form class="card" method="post" action="/login">
    <div class="logo"><div class="dot"></div><div><h1>TradeOS AI</h1><p>ScamPump Radar · sign in</p></div></div>
    <label>Username</label><input name="username" autocomplete="username" autofocus/>
    <label>Password</label><input name="password" type="password" autocomplete="current-password"/>
    <button type="submit">Sign in</button>
    <div class="err"><!--ERR--></div>
  </form>
</body></html>"""
