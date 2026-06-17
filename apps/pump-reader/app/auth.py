"""Simple app login — signed-cookie session, no DB.

Auth turns ON only when APP_PASSWORD is set (so paper/dev runs stay open). On the
Oracle VM set APP_USERNAME / APP_PASSWORD / APP_SECRET_KEY in .env and the whole
dashboard + API require login.
"""

from __future__ import annotations

import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET_KEY = os.getenv("APP_SECRET_KEY", "tradeos-dev-secret-change-me")
COOKIE = "tradeos_session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 days

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="tradeos-auth")


def auth_enabled() -> bool:
    return bool(APP_PASSWORD)


def make_token(user: str) -> str:
    return _serializer.dumps({"u": user})


def valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=MAX_AGE)
        return True
    except (BadSignature, SignatureExpired, Exception):
        return False


def check_credentials(user: str, password: str) -> bool:
    return auth_enabled() and user == APP_USERNAME and password == APP_PASSWORD


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
