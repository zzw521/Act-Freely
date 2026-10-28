#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通天办公 - 本地 Auth 服务（网页验证登录）

云端 Phase A 尚未立项，本服务作为本地过渡实现：
- 网页端登录/注册/退出页面（浏览器打开，登录成功回跳桌面端 loopback 回调）
- REST API：register / login / exchange（code 换 token）/ refresh / logout
- JWT HS256 手写实现（标准库 hmac/hashlib/base64，不引 PyJWT）：
  access 15min + refresh 30d 轮换（对齐 docs/PRD-任我行.md §4.1）
- SQLite 持久化 users / sessions（云端切换时仅改客户端 TT_AUTH_SERVER_URL）

用法:
    python auth-server/server.py [--port 18080]
环境变量:
    TT_AUTH_PORT         监听端口（缺省 18080，仅绑 127.0.0.1）
    TT_AUTH_DB           SQLite 路径（缺省脚本同目录 auth.db）
    TT_AUTH_JWT_SECRET   JWT 签名密钥（缺省开发密钥，生产必须覆盖）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

# ── .env 加载（与 engine/server.py 同款，无第三方依赖） ────────


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gbk", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

AUTH_VERSION = "0.1.0"
DB_PATH = Path(os.environ.get("TT_AUTH_DB", str(Path(__file__).resolve().parent / "auth.db")))
JWT_SECRET = os.environ.get("TT_AUTH_JWT_SECRET", "tongtian-dev-secret-change-me")
ACCESS_TTL_SECONDS = 15 * 60          # access token 15 分钟
REFRESH_TTL_SECONDS = 30 * 24 * 3600  # refresh token 30 天
CODE_TTL_SECONDS = 5 * 60             # 一次性授权码 5 分钟过期

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# ── JWT HS256（标准库实现） ───────────────────────────────────


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def jwt_encode(payload: dict, secret: str = JWT_SECRET) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    body = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claims = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{body}.{claims}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{body}.{claims}.{_b64url_encode(sig)}"


def jwt_decode(token: str, secret: str = JWT_SECRET) -> dict | None:
    """验签 + 过期校验；失败返回 None"""
    try:
        body, claims, sig = token.split(".")
        signing_input = f"{body}.{claims}".encode("ascii")
        expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            return None
        payload = json.loads(_b64url_decode(claims))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# ── 密码哈希（pbkdf2 + salt） ────────────────────────────────


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000).hex()


# ── SQLite ───────────────────────────────────────────────────

_db_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                uid          TEXT PRIMARY KEY,
                email        TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt         TEXT NOT NULL,
                nickname     TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id            TEXT PRIMARY KEY,
                uid           TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    INTEGER NOT NULL,
                revoked       INTEGER NOT NULL DEFAULT 0,
                created_at    INTEGER NOT NULL
            );
            """
        )


def find_user_by_email(email: str) -> sqlite3.Row | None:
    with _db_lock, get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?1", (email,)).fetchone()


def create_user(email: str, password: str, nickname: str) -> sqlite3.Row:
    uid = f"tt-{secrets.token_hex(5)}"
    salt = secrets.token_bytes(16)
    now = int(time.time())
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO users (uid, email, password_hash, salt, nickname, created_at) VALUES (?1,?2,?3,?4,?5,?6)",
            (uid, email, hash_password(password, salt), salt.hex(), nickname, now),
        )
    return find_user_by_email(email)


def create_session(uid: str, refresh_token: str) -> None:
    now = int(time.time())
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, uid, refresh_token, expires_at, revoked, created_at) VALUES (?1,?2,?3,?4,0,?5)",
            (f"sess-{secrets.token_hex(8)}", uid, refresh_token, now + REFRESH_TTL_SECONDS, now),
        )


def revoke_session_by_token(refresh_token: str) -> bool:
    with _db_lock, get_conn() as conn:
        cur = conn.execute("UPDATE sessions SET revoked = 1 WHERE refresh_token = ?1", (refresh_token,))
        return cur.rowcount > 0


def session_active(refresh_token: str) -> bool:
    now = int(time.time())
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT expires_at, revoked FROM sessions WHERE refresh_token = ?1", (refresh_token,)
        ).fetchone()
    return bool(row) and row["revoked"] == 0 and row["expires_at"] > now


# ── 一次性授权码（内存表，绑定 state，5 分钟过期） ──────────

_codes: dict[str, dict] = {}
_codes_lock = threading.Lock()


def issue_code(uid: str, state: str, redirect_uri: str) -> str:
    code = secrets.token_urlsafe(24)
    with _codes_lock:
        # 惰性清理过期码
        now = time.time()
        for c in [k for k, v in _codes.items() if v["expires_at"] < now]:
            _codes.pop(c, None)
        _codes[code] = {
            "uid": uid,
            "state": state,
            "redirect_uri": redirect_uri,
            "expires_at": now + CODE_TTL_SECONDS,
        }
    return code


def consume_code(code: str, state: str) -> dict | None:
    """state 校验通过后取码并销毁（一次性）；不匹配/过期返回 None 且不销毁（允许重试）"""
    with _codes_lock:
        entry = _codes.get(code)
        if entry is None or entry["expires_at"] < time.time():
            _codes.pop(code, None)
            return None
        if entry["state"] != state:
            return None
        _codes.pop(code, None)
    return entry


# ── token 签发 ───────────────────────────────────────────────


def issue_tokens(user: sqlite3.Row) -> dict:
    now = int(time.time())
    profile = {"uid": user["uid"], "nickname": user["nickname"], "email": user["email"]}
    access = jwt_encode(
        {"sub": user["uid"], "type": "access", "iat": now, "exp": now + ACCESS_TTL_SECONDS}
    )
    refresh = jwt_encode(
        {"sub": user["uid"], "type": "refresh", "jti": secrets.token_hex(8), "iat": now, "exp": now + REFRESH_TTL_SECONDS}
    )
    create_session(user["uid"], refresh)
    return {"accessToken": access, "refreshToken": refresh, "profile": profile}


# ── HTML 页面（内嵌模板） ────────────────────────────────────

_PAGE_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
         background: linear-gradient(135deg, #EEF2FF 0%, #F5F3FF 50%, #ECFEFF 100%);
         min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .card { width: 380px; background: #fff; border-radius: 16px; padding: 36px 32px;
          box-shadow: 0 16px 48px rgba(79, 70, 229, 0.14); }
  .logo { display: flex; align-items: center; gap: 10px; margin-bottom: 22px; }
  .logo-badge { width: 34px; height: 34px; border-radius: 10px; color: #fff; font-weight: 800;
                display: flex; align-items: center; justify-content: center;
                background: linear-gradient(135deg, #6366F1 0%, #8B5CF6 50%, #06B6D4 100%); }
  .logo-text { font-size: 18px; font-weight: 800;
               background: linear-gradient(90deg, #4F46E5, #7C3AED 60%, #0EA5E9);
               -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  h2 { font-size: 19px; color: #111827; margin-bottom: 6px; }
  .sub { font-size: 12.5px; color: #6B7280; margin-bottom: 22px; }
  label { display: block; font-size: 12.5px; font-weight: 600; color: #374151; margin: 14px 0 6px; }
  input { width: 100%; padding: 10px 12px; border: 1px solid #E5E7EB; border-radius: 10px;
          font-size: 13.5px; outline: none; transition: border-color .15s; }
  input:focus { border-color: #6366F1; }
  .btn { width: 100%; margin-top: 22px; padding: 11px 0; border: none; border-radius: 10px;
         font-size: 14px; font-weight: 700; color: #fff; cursor: pointer;
         background: linear-gradient(120deg, #6366F1 0%, #8B5CF6 100%);
         transition: filter .15s; }
  .btn:hover { filter: brightness(1.06); }
  .switch { margin-top: 16px; font-size: 12.5px; color: #6B7280; text-align: center; }
  .switch a { color: #4F46E5; font-weight: 600; text-decoration: none; }
  .error { margin-top: 14px; padding: 9px 12px; border-radius: 8px; font-size: 12.5px;
           color: #B91C1C; background: rgba(239, 68, 68, 0.08); }
  .ok { margin-top: 14px; padding: 12px; border-radius: 10px; font-size: 13px;
        color: #065F46; background: rgba(16, 185, 129, 0.10); line-height: 1.6; }
"""


def _page(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - 通天办公</title><style>{_PAGE_CSS}</style></head>
<body><div class="card">
  <div class="logo"><div class="logo-badge">通</div><div class="logo-text">通天办公</div></div>
  {body_html}
</div></body></html>"""


def _hidden_fields(state: str, redirect_uri: str) -> str:
    return (
        f'<input type="hidden" name="state" value="{state}">'
        f'<input type="hidden" name="redirect_uri" value="{redirect_uri}">'
    )


def login_page_html(state: str, redirect_uri: str, error: str = "") -> str:
    err = f'<div class="error">{error}</div>' if error else ""
    switch_link = f"/auth/register?state={state}&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
    return _page(
        "登录",
        f"""<h2>登录账号</h2><div class="sub">在浏览器完成验证后，将自动返回通天办公桌面端</div>
<form method="post" action="/api/auth/login">
  {_hidden_fields(state, redirect_uri)}
  <label>邮箱</label><input type="email" name="email" required placeholder="you@example.com">
  <label>密码</label><input type="password" name="password" required placeholder="请输入密码">
  <button class="btn" type="submit">登 录</button>{err}
</form>
<div class="switch">还没有账号？<a href="{switch_link}">立即注册</a></div>""",
    )


def register_page_html(state: str, redirect_uri: str, error: str = "") -> str:
    err = f'<div class="error">{error}</div>' if error else ""
    switch_link = f"/auth/login?state={state}&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
    return _page(
        "注册",
        f"""<h2>注册账号</h2><div class="sub">注册成功后自动登录，并返回通天办公桌面端</div>
<form method="post" action="/api/auth/register">
  {_hidden_fields(state, redirect_uri)}
  <label>昵称</label><input type="text" name="nickname" required maxlength="24" placeholder="怎么称呼你">
  <label>邮箱</label><input type="email" name="email" required placeholder="you@example.com">
  <label>密码</label><input type="password" name="password" required minlength="6" placeholder="至少 6 位">
  <label>确认密码</label><input type="password" name="confirm" required minlength="6" placeholder="再次输入密码">
  <button class="btn" type="submit">注册并登录</button>{err}
</form>
<div class="switch">已有账号？<a href="{switch_link}">直接登录</a></div>""",
    )


def success_page_html(nickname: str) -> str:
    return _page(
        "登录成功",
        f"""<h2>登录成功</h2>
<div class="ok">{nickname}，验证通过。正在返回通天办公桌面端，如未自动跳转可关闭此页面。</div>""",
    )


# ── FastAPI 应用 ─────────────────────────────────────────────

app = FastAPI(title="tongtian-auth-server", version=AUTH_VERSION)

# CORS：浏览器直连联调（开发/联调阶段）
try:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:  # pragma: no cover - fastapi 版本差异兜底
    pass


@app.on_event("startup")
def _startup() -> None:
    init_db()


async def _form(request: Request) -> dict:
    """解析表单 body（不依赖 python-multipart）"""
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw)
    return {k: v[0] if v else "" for k, v in parsed.items()}


def _redirect_only_loopback(redirect_uri: str) -> bool:
    """回调地址安全校验：仅允许 127.0.0.1 / localhost loopback"""
    try:
        parts = urllib.parse.urlsplit(redirect_uri)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return parts.hostname in ("127.0.0.1", "localhost", "::1")


@app.get("/health")
async def health():
    return {"ok": True, "service": "tongtian-auth-server", "version": AUTH_VERSION}


@app.get("/auth/login", response_class=HTMLResponse)
async def page_login(state: str = "", redirect_uri: str = ""):
    return HTMLResponse(login_page_html(state, redirect_uri))


@app.get("/auth/register", response_class=HTMLResponse)
async def page_register(state: str = "", redirect_uri: str = ""):
    return HTMLResponse(register_page_html(state, redirect_uri))


@app.post("/api/auth/register")
async def api_register(request: Request):
    form = await _form(request)
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    confirm = form.get("confirm", "")
    nickname = form.get("nickname", "").strip() or email.split("@")[0]
    state = form.get("state", "")
    redirect_uri = form.get("redirect_uri", "")

    def fail(msg: str):
        return HTMLResponse(register_page_html(state, redirect_uri, msg), status_code=400)

    if not EMAIL_RE.match(email):
        return fail("邮箱格式不正确")
    if len(password) < 6:
        return fail("密码至少 6 位")
    if password != confirm:
        return fail("两次输入的密码不一致")
    if not _redirect_only_loopback(redirect_uri):
        return fail("回调地址非法（仅允许本机回跳）")
    if find_user_by_email(email) is not None:
        return fail("该邮箱已注册，请直接登录")

    user = create_user(email, password, nickname)
    code = issue_code(user["uid"], state, redirect_uri)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={urllib.parse.quote(code)}&state={urllib.parse.quote(state)}", status_code=302)


@app.post("/api/auth/login")
async def api_login(request: Request):
    form = await _form(request)
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    state = form.get("state", "")
    redirect_uri = form.get("redirect_uri", "")

    def fail(msg: str):
        return HTMLResponse(login_page_html(state, redirect_uri, msg), status_code=401)

    if not _redirect_only_loopback(redirect_uri):
        return fail("回调地址非法（仅允许本机回跳）")
    user = find_user_by_email(email)
    if user is None or not hmac.compare_digest(
        hash_password(password, bytes.fromhex(user["salt"])), user["password_hash"]
    ):
        return fail("邮箱或密码错误")

    code = issue_code(user["uid"], state, redirect_uri)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={urllib.parse.quote(code)}&state={urllib.parse.quote(state)}", status_code=302)


@app.post("/api/auth/exchange")
async def api_exchange(request: Request):
    """授权码换 token（桌面端 loopback 收到 code 后调用）"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体需为 JSON"}, status_code=400)
    code = body.get("code", "")
    state = body.get("state", "")
    if not code or not state:
        return JSONResponse({"ok": False, "message": "缺少 code/state"}, status_code=400)

    entry = consume_code(code, state)
    if entry is None:
        return JSONResponse({"ok": False, "message": "授权码无效、已过期或 state 不匹配"}, status_code=400)

    with _db_lock, get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE uid = ?1", (entry["uid"],)).fetchone()
    if user is None:
        return JSONResponse({"ok": False, "message": "用户不存在"}, status_code=400)

    tokens = issue_tokens(user)
    return JSONResponse({"ok": True, **tokens})


@app.post("/api/auth/refresh")
async def api_refresh(request: Request):
    """refresh token 轮换：旧 token 立即吊销"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体需为 JSON"}, status_code=400)
    refresh_token = body.get("refreshToken", "")
    payload = jwt_decode(refresh_token) if refresh_token else None
    if payload is None or payload.get("type") != "refresh" or not session_active(refresh_token):
        return JSONResponse({"ok": False, "message": "refresh token 无效或已吊销"}, status_code=401)

    with _db_lock, get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE uid = ?1", (payload["sub"],)).fetchone()
    if user is None:
        return JSONResponse({"ok": False, "message": "用户不存在"}, status_code=401)

    revoke_session_by_token(refresh_token)  # 轮换：旧 token 作废
    return JSONResponse({"ok": True, **issue_tokens(user)})


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    """退出：吊销 refresh token 对应 session（尽力而为，幂等）"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    refresh_token = body.get("refreshToken", "")
    revoked = revoke_session_by_token(refresh_token) if refresh_token else False
    return JSONResponse({"ok": True, "revoked": revoked})


if __name__ == "__main__":
    # Windows GBK 控制台兼容：强制 UTF-8 输出
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="通天办公本地 Auth 服务")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TT_AUTH_PORT", "18080")))
    args = parser.parse_args()

    print("=========================================", flush=True)
    print("  通天办公 - 本地 Auth 服务", flush=True)
    print("=========================================", flush=True)
    print(f"  登录页:   http://127.0.0.1:{args.port}/auth/login", flush=True)
    print(f"  注册页:   http://127.0.0.1:{args.port}/auth/register", flush=True)
    print(f"  数据文件: {DB_PATH}", flush=True)
    print("  模式:     本地过渡实现（云端 Phase A 上线后切换 TT_AUTH_SERVER_URL）", flush=True)
    print("-----------------------------------------", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
