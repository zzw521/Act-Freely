"""账号与登录：v1 JSON API + 兼容桌面端的网页验证登录（loopback 授权码）。"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import threading
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from .config import settings
from .db import now_ms, tx
from .errors import ApiError
from .security import (
    EMAIL_RE,
    RateLimiter,
    dummy_verify,
    hash_password,
    jwt_decode,
    jwt_encode,
    new_code,
    new_session_id,
    new_uid,
    token_hash,
    verify_password,
)

logger = logging.getLogger("tongtian.auth")
router = APIRouter()

CODE_TTL_SECONDS = 5 * 60
MAX_PENDING_CODES = 10_000
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_login_limiter = RateLimiter()

_codes: dict[str, dict[str, Any]] = {}
_codes_lock = threading.Lock()


# ── Pydantic 入参 ────────────────────────────────────────────


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str
    nickname: str | None = None


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refreshToken: str


class LogoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refreshToken: str = ""


# ── 用户与会话 ───────────────────────────────────────────────


def profile_of(user: sqlite3.Row | dict) -> dict:
    def get(key: str) -> Any:
        return user[key] if isinstance(user, sqlite3.Row) else user.get(key)

    return {
        "uid": get("uid"),
        "nickname": get("nickname"),
        "email": get("email"),
        "plan": get("plan") or "个人免费版",
        "role": get("role") or "user",
        "mock": False,
    }


def find_user_by_email(email: str) -> sqlite3.Row | None:
    with tx() as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?1", (email,)).fetchone()


def find_user_by_uid(uid: str) -> sqlite3.Row | None:
    if not uid:
        return None
    with tx() as conn:
        return conn.execute("SELECT * FROM users WHERE uid = ?1", (uid,)).fetchone()


def _normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ApiError(400, "INVALID_EMAIL", "邮箱格式不正确")
    return email


def _validate_password(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) < 8:
        raise ApiError(400, "WEAK_PASSWORD", "密码至少 8 位")
    if len(raw) > 128:
        raise ApiError(400, "WEAK_PASSWORD", "密码不能超过 128 位")
    if not raw.strip() or CONTROL_RE.search(raw):
        raise ApiError(400, "WEAK_PASSWORD", "密码不能为空白或包含控制字符")
    return raw


def _normalize_nickname(raw: str | None, email: str) -> str:
    nickname = (raw or "").strip() or email.split("@")[0]
    if len(nickname) > 24:
        raise ApiError(400, "INVALID_NICKNAME", "昵称不能超过 24 个字符")
    if CONTROL_RE.search(nickname):
        raise ApiError(400, "INVALID_NICKNAME", "昵称含不可见控制字符")
    return nickname


def _create_user(email: str, password: str, nickname: str, role: str = "user") -> sqlite3.Row:
    uid = new_uid()
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO users (uid, email, password_hash, nickname, role, plan, created_at_ms) "
                "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                (uid, email, hash_password(password), nickname, role, "个人免费版", now_ms()),
            )
            row = conn.execute("SELECT * FROM users WHERE uid = ?1", (uid,)).fetchone()
    except sqlite3.IntegrityError:
        raise ApiError(409, "EMAIL_TAKEN", "该邮箱已注册，请直接登录") from None
    return row


def _issue_tokens(conn: sqlite3.Connection, user: sqlite3.Row | dict) -> dict:
    now = int(time.time())
    uid = user["uid"] if isinstance(user, sqlite3.Row) else user["uid"]
    role = (user["role"] if isinstance(user, sqlite3.Row) else user.get("role")) or "user"
    access = jwt_encode(
        {
            "sub": uid,
            "type": "access",
            "role": role,
            "iat": now,
            "exp": now + settings.access_ttl_seconds,
        }
    )
    refresh = jwt_encode(
        {
            "sub": uid,
            "type": "refresh",
            "role": role,
            "jti": new_session_id(),
            "iat": now,
            "exp": now + settings.refresh_ttl_seconds,
        }
    )
    conn.execute(
        "INSERT INTO sessions (id, uid, refresh_hash, expires_at_ms, revoked, created_at_ms) "
        "VALUES (?1, ?2, ?3, ?4, 0, ?5)",
        (
            new_session_id(),
            uid,
            token_hash(refresh),
            (now + settings.refresh_ttl_seconds) * 1000,
            now_ms(),
        ),
    )
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresIn": settings.access_ttl_seconds,
        "profile": profile_of(user),
    }


def issue_tokens(user: sqlite3.Row | dict) -> dict:
    with tx() as conn:
        return _issue_tokens(conn, user)


def refresh_tokens(refresh_token: str) -> dict:
    payload = jwt_decode(refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise ApiError(401, "INVALID_REFRESH", "refresh token 无效或已过期")
    with tx() as conn:
        cur = conn.execute(
            "UPDATE sessions SET revoked = 1 "
            "WHERE refresh_hash = ?1 AND revoked = 0 AND expires_at_ms > ?2",
            (token_hash(refresh_token), now_ms()),
        )
        if cur.rowcount != 1:
            raise ApiError(401, "INVALID_REFRESH", "refresh token 无效或已吊销")
        user = conn.execute("SELECT * FROM users WHERE uid = ?1", (payload.get("sub", ""),)).fetchone()
        if user is None:
            raise ApiError(401, "INVALID_REFRESH", "账号不存在")
        return _issue_tokens(conn, user)


def revoke_refresh(refresh_token: str) -> bool:
    if not refresh_token:
        return False
    with tx() as conn:
        cur = conn.execute(
            "UPDATE sessions SET revoked = 1 WHERE refresh_hash = ?1",
            (token_hash(refresh_token),),
        )
        return cur.rowcount > 0


def bearer_user(request: Request) -> dict | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    payload = jwt_decode(header[7:].strip())
    if payload is None or payload.get("type") != "access":
        return None
    user = find_user_by_uid(str(payload.get("sub", "")))
    if user is None:
        return None
    return dict(user)


def require_user(request: Request) -> dict:
    user = bearer_user(request)
    if user is None:
        raise ApiError(401, "AUTH_REQUIRED", "请先登录")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        raise ApiError(403, "FORBIDDEN", "需要管理员权限")
    return user


def ensure_admin() -> bool:
    """按环境变量引导管理员；已存在的账号只提升角色，不改密码/昵称（保护存量数据）。"""
    if not settings.admin_email and not settings.admin_password:
        return False
    if not settings.admin_email or len(settings.admin_password) < 8:
        raise RuntimeError("TT_ADMIN_EMAIL 与至少 8 位的 TT_ADMIN_PASSWORD 必须同时配置")
    email = _normalize_email(settings.admin_email)
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?1", (email,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (uid, email, password_hash, nickname, role, plan, created_at_ms) "
                "VALUES (?1, ?2, ?3, ?4, 'admin', '平台管理员', ?5)",
                (
                    new_uid(),
                    email,
                    hash_password(settings.admin_password),
                    settings.admin_nickname[:24],
                    now_ms(),
                ),
            )
            return True
        if row["role"] != "admin":
            conn.execute("UPDATE users SET role = 'admin' WHERE uid = ?1", (row["uid"],))
            return True
    return False


# ── 一次性授权码（内存） ────────────────────────────────────


def issue_code(uid: str, state: str, redirect_uri: str) -> str:
    code = new_code()
    now = time.time()
    with _codes_lock:
        for key in [k for k, v in _codes.items() if v["expires_at"] < now]:
            _codes.pop(key, None)
        while len(_codes) >= MAX_PENDING_CODES:
            _codes.pop(next(iter(_codes)), None)
        _codes[code] = {
            "uid": uid,
            "state": state,
            "redirect_uri": redirect_uri,
            "expires_at": now + CODE_TTL_SECONDS,
        }
    return code


def consume_code(code: str, state: str) -> dict | None:
    with _codes_lock:
        entry = _codes.get(code)
        if entry is None or entry["expires_at"] < time.time():
            _codes.pop(code, None)
            return None
        if entry["state"] != state:
            return None
        _codes.pop(code, None)
        return entry


# ── 入口校验 ─────────────────────────────────────────────────


def _client_ip(request: Request) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _limit_login(request: Request, email: str) -> str:
    ip = _client_ip(request)
    account = (email or "")[:254]
    allowed_ip, retry_ip = _login_limiter.check(f"login:ip:{ip}", 50, 300)
    allowed_pair, retry_pair = _login_limiter.check(f"login:{ip}:{account}", 10, 300)
    if not allowed_ip or not allowed_pair:
        retry = max(retry_ip, retry_pair, 1)
        raise ApiError(
            429,
            "RATE_LIMITED",
            f"登录尝试过于频繁，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    return f"login:{ip}:{email}"


def _limit_register(request: Request) -> None:
    allowed, retry = _login_limiter.check(f"register:{_client_ip(request)}", 20, 3600)
    if not allowed:
        raise ApiError(
            429,
            "RATE_LIMITED",
            f"注册过于频繁，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )


def _authenticate(email: str, password: str) -> sqlite3.Row:
    user = find_user_by_email(email)
    if user is None:
        dummy_verify(password)
        raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
    if not verify_password(password, user["password_hash"]):
        raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
    return user


# ── v1 JSON API ─────────────────────────────────────────────


@router.post("/api/v1/auth/register", status_code=201)
def api_v1_register(payload: RegisterIn, request: Request):
    if not settings.allow_registration:
        raise ApiError(403, "REGISTRATION_DISABLED", "当前服务未开放注册")
    _limit_register(request)
    email = _normalize_email(payload.email)
    password = _validate_password(payload.password)
    nickname = _normalize_nickname(payload.nickname, email)
    user = _create_user(email, password, nickname)
    return {"ok": True, **issue_tokens(user)}


@router.post("/api/v1/auth/login")
def api_v1_login(payload: LoginIn, request: Request):
    email = (payload.email or "").strip().lower()
    key = _limit_login(request, email)
    if not EMAIL_RE.match(email):
        raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
    user = _authenticate(email, payload.password)
    _login_limiter.reset(key)
    return {"ok": True, **issue_tokens(user)}


@router.post("/api/v1/auth/refresh")
def api_v1_refresh(payload: RefreshIn):
    return {"ok": True, **refresh_tokens(payload.refreshToken)}


@router.post("/api/v1/auth/logout")
def api_v1_logout(payload: LogoutIn):
    return {"ok": True, "revoked": revoke_refresh(payload.refreshToken)}


@router.get("/api/v1/auth/me")
def api_v1_me(user: dict = Depends(require_user)):
    return {"ok": True, "profile": profile_of(user)}


# ── 网页登录/注册（loopback 回跳） ──────────────────────────


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
         background: linear-gradient(120deg, #6366F1 0%, #8B5CF6 100%); }
  .switch { margin-top: 16px; font-size: 12.5px; color: #6B7280; text-align: center; }
  .switch a { color: #4F46E5; font-weight: 600; text-decoration: none; }
  .error { margin-top: 14px; padding: 9px 12px; border-radius: 8px; font-size: 12.5px;
           color: #B91C1C; background: rgba(239, 68, 68, 0.08); }
  .ok { margin-top: 14px; padding: 12px; border-radius: 10px; font-size: 13px;
        color: #065F46; background: rgba(16, 185, 129, 0.10); line-height: 1.6; }
"""

_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


def _page(title: str, body_html: str, status: int = 200) -> HTMLResponse:
    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - 通天办公</title><style>{_PAGE_CSS}</style></head>
<body><div class="card">
  <div class="logo"><div class="logo-badge">通</div><div class="logo-text">通天办公</div></div>
  {body_html}
</div></body></html>"""
    return HTMLResponse(
        body,
        status_code=status,
        headers={"Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff"},
    )


def _hidden_fields(state: str, redirect_uri: str) -> str:
    return (
        f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">'
        f'<input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri, quote=True)}">'
    )


def _switch_link(path: str, state: str, redirect_uri: str) -> str:
    query = urllib.parse.urlencode({"state": state, "redirect_uri": redirect_uri})
    return html.escape(f"{path}?{query}", quote=True)


def _login_page(state: str, redirect_uri: str, error: str = "", status: int = 200) -> HTMLResponse:
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    link = _switch_link("/auth/register", state, redirect_uri)
    return _page(
        "登录",
        f"""<h2>登录账号</h2><div class="sub">在浏览器完成验证后，将自动返回通天办公桌面端</div>
<form method="post" action="/api/auth/login">
  {_hidden_fields(state, redirect_uri)}
  <label>邮箱</label><input type="email" name="email" required placeholder="you@example.com">
  <label>密码</label><input type="password" name="password" required placeholder="请输入密码">
  <button class="btn" type="submit">登 录</button>{err}
</form>
<div class="switch">还没有账号？<a href="{link}">立即注册</a></div>""",
        status,
    )


def _register_page(state: str, redirect_uri: str, error: str = "", status: int = 200) -> HTMLResponse:
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    link = _switch_link("/auth/login", state, redirect_uri)
    return _page(
        "注册",
        f"""<h2>注册账号</h2><div class="sub">注册成功后自动登录，并返回通天办公桌面端</div>
<form method="post" action="/api/auth/register">
  {_hidden_fields(state, redirect_uri)}
  <label>昵称</label><input type="text" name="nickname" required maxlength="24" placeholder="怎么称呼你">
  <label>邮箱</label><input type="email" name="email" required placeholder="you@example.com">
  <label>密码</label><input type="password" name="password" required minlength="8" placeholder="至少 8 位">
  <label>确认密码</label><input type="password" name="confirm" required minlength="8" placeholder="再次输入密码">
  <button class="btn" type="submit">注册并登录</button>{err}
</form>
<div class="switch">已有账号？<a href="{link}">直接登录</a></div>""",
        status,
    )


def _success_page(nickname: str) -> HTMLResponse:
    return _page(
        "登录成功",
        f"""<h2>登录成功</h2>
<div class="ok">{html.escape(nickname)}，验证通过。正在返回通天办公桌面端，如未自动跳转可关闭此页面。</div>""",
    )


def _loopback_redirect(redirect_uri: str) -> bool:
    if len(redirect_uri) > 2048:
        return False
    try:
        parts = urllib.parse.urlsplit(redirect_uri)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return parts.hostname in ("127.0.0.1", "localhost", "::1")


def _valid_state(state: str) -> bool:
    return 1 <= len(state) <= 256 and not CONTROL_RE.search(state)


def _append_code(redirect_uri: str, code: str, state: str) -> str:
    parts = urllib.parse.urlsplit(redirect_uri)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend([("code", code), ("state", state)])
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


@router.get("/auth/login", response_class=HTMLResponse)
def page_login(state: str = "", redirect_uri: str = ""):
    return _login_page(state, redirect_uri)


@router.get("/auth/register", response_class=HTMLResponse)
def page_register(state: str = "", redirect_uri: str = ""):
    return _register_page(state, redirect_uri)


@router.post("/api/auth/register")
async def legacy_register(request: Request):
    form = await _form(request)
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    confirm = form.get("confirm", "")
    state = form.get("state", "")
    redirect_uri = form.get("redirect_uri", "")

    def fail(message: str, status: int = 400) -> HTMLResponse:
        return _register_page(state, redirect_uri, message, status)

    if not settings.allow_registration:
        return fail("当前服务未开放注册", 403)
    try:
        _limit_register(request)
    except ApiError as exc:
        return fail(exc.message, exc.status)
    if not _loopback_redirect(redirect_uri):
        return fail("回调地址非法（仅允许本机回跳）")
    if not _valid_state(state):
        return fail("登录状态码非法")
    try:
        email = _normalize_email(email)
        _validate_password(password)
    except ApiError as exc:
        return fail(exc.message)
    if password != confirm:
        return fail("两次输入的密码不一致")
    if find_user_by_email(email) is not None:
        return fail("该邮箱已注册，请直接登录")
    try:
        nickname = _normalize_nickname(form.get("nickname", ""), email)
        user = _create_user(email, password, nickname)
    except ApiError as exc:
        return fail(exc.message, exc.status)

    code = issue_code(user["uid"], state, redirect_uri)
    return RedirectResponse(_append_code(redirect_uri, code, state), status_code=302)


@router.post("/api/auth/login")
async def legacy_login(request: Request):
    form = await _form(request)
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    state = form.get("state", "")
    redirect_uri = form.get("redirect_uri", "")

    def fail(message: str, status: int = 401) -> HTMLResponse:
        return _login_page(state, redirect_uri, message, status)

    if not _loopback_redirect(redirect_uri):
        return fail("回调地址非法（仅允许本机回跳）", 400)
    if not _valid_state(state):
        return fail("登录状态码非法", 400)
    try:
        key = _limit_login(request, email)
        if not EMAIL_RE.match(email):
            raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
        user = _authenticate(email, password)
    except ApiError as exc:
        return fail(exc.message, exc.status)
    _login_limiter.reset(key)

    code = issue_code(user["uid"], state, redirect_uri)
    return RedirectResponse(_append_code(redirect_uri, code, state), status_code=302)


@router.post("/api/auth/exchange")
async def legacy_exchange(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "code": "INVALID_JSON", "message": "请求体需为 JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        raise ApiError(400, "INVALID_JSON", "请求体需为 JSON 对象")
    code = body.get("code", "")
    state = body.get("state", "")
    if not isinstance(code, str) or not isinstance(state, str) or not code or not state:
        raise ApiError(400, "INVALID_CODE", "缺少 code/state")
    entry = consume_code(code, state)
    if entry is None:
        raise ApiError(400, "INVALID_CODE", "授权码无效、已过期或 state 不匹配")
    user = find_user_by_uid(entry["uid"])
    if user is None:
        raise ApiError(400, "INVALID_CODE", "账号不存在")
    return {"ok": True, **issue_tokens(user)}


@router.post("/api/auth/refresh")
async def legacy_refresh(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_JSON", "请求体需为 JSON") from None
    token = body.get("refreshToken", "") if isinstance(body, dict) else ""
    if not isinstance(token, str) or not token:
        raise ApiError(400, "INVALID_REFRESH", "缺少 refreshToken")
    return {"ok": True, **refresh_tokens(token)}


@router.post("/api/auth/logout")
async def legacy_logout(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = body.get("refreshToken", "") if isinstance(body, dict) else ""
    return {"ok": True, "revoked": revoke_refresh(token if isinstance(token, str) else "")}
