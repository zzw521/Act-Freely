"""账号与登录：v1 JSON API + 兼容桌面端的网页验证登录（loopback 授权码）。"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import hmac
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from .audit import write_audit
from .config import settings
from .db import DEFAULT_DEPT_ID, now_ms, tx
from .errors import ApiError
from .keys import key_store
from .orgs import (
    department_ids,
    ensure_default_department,
    ensure_department,
    memberships,
    primary_department,
    replace_memberships,
)
from .roles import MANAGEMENT_ROLES, normalize_role, role_label, role_of
from .security import (
    EMAIL_RE,
    RateLimiter,
    dummy_verify,
    hash_password,
    jwt_decode,
    jwt_decode_verbose,
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
# 单账号保留的活跃会话上限：登录次数只增不减会让 sessions 表无限膨胀
MAX_SESSIONS_PER_USER = 20
# PKCE：桌面端登录发起时带 code_challenge，换 token 时必须出示 code_verifier
PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ORG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

USER_QUOTA = {"monthlyTokens": 100_000, "concurrentTasks": 2}
ADMIN_QUOTA = {"monthlyTokens": 2_000_000, "concurrentTasks": 8}
QUOTA_LIMITS = {"monthlyTokens": (0, 1_000_000_000), "concurrentTasks": (1, 100)}

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


class QuotaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthlyTokens: int | None = None
    concurrentTasks: int | None = None


class UserClaimsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: str | None = None
    role: str | None = None
    quota: QuotaIn | None = None
    depts: list[str] | None = None
    primaryDept: str | None = None


class DepartmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: str
    id: str
    name: str = ""


class DepartmentPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


# ── 用户与会话 ───────────────────────────────────────────────


def _user_value(user: sqlite3.Row | dict, key: str, default: Any = None) -> Any:
    if isinstance(user, sqlite3.Row):
        return user[key] if key in user.keys() else default
    return user.get(key, default)


def effective_org(user: sqlite3.Row | dict) -> str:
    org = str(_user_value(user, "org_id", "") or "").strip().lower()
    uid = str(_user_value(user, "uid", "") or "")
    return org or f"personal-{uid}"


def _default_quota(user: sqlite3.Row | dict) -> dict:
    role = _user_value(user, "role", "user") or "user"
    return dict(ADMIN_QUOTA if role == "admin" else USER_QUOTA)


def effective_quota(user: sqlite3.Row | dict) -> dict:
    quota = _default_quota(user)
    try:
        override = json.loads(_user_value(user, "quota_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        return quota
    if not isinstance(override, dict):
        return quota
    for key, (low, high) in QUOTA_LIMITS.items():
        value = override.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            quota[key] = value
    return quota


def _normalize_org(raw: str | None) -> str:
    org = (raw or "").strip().lower()
    if not org:
        return ""
    if not ORG_RE.match(org):
        raise ApiError(400, "INVALID_ORG", "组织标识需为 1~64 位小写字母、数字、下划线或连字符")
    return org


def _validate_quota(model: QuotaIn) -> dict:
    quota: dict[str, int] = {}
    for key, (low, high) in QUOTA_LIMITS.items():
        value = getattr(model, key)
        if value is None:
            continue
        if not low <= value <= high:
            raise ApiError(400, "INVALID_QUOTA", f"{key} 需在 {low}~{high} 之间")
        quota[key] = value
    return quota


def _validate_role(raw: str | None) -> str:
    role = normalize_role(raw)
    if not role:
        raise ApiError(
            400,
            "INVALID_ROLE",
            "角色必须是 admin / operator / auditor / user 之一",
        )
    return role


def profile_of(user: sqlite3.Row | dict, quota: dict | None = None) -> dict:
    def get(key: str) -> Any:
        return user[key] if isinstance(user, sqlite3.Row) else user.get(key)

    return {
        "uid": get("uid"),
        "nickname": get("nickname"),
        "email": get("email"),
        "plan": get("plan") or "个人免费版",
        "role": get("role") or "user",
        "roleLabel": role_label(get("role") or "user"),
        "org": effective_org(user),
        # 签发路径会传进「两级算完」的值；管理面列表没有 conn，沿用个人/角色口径
        "quota": dict(quota) if quota else effective_quota(user),
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
    uid = str(_user_value(user, "uid", "") or "")
    role = _user_value(user, "role", "user") or "user"
    session_id = new_session_id()
    # 组织两级配额（D-23）：声明里的 quota 走「个人覆盖 > 组织成员默认 > 角色缺省」，
    # 组织开了拦停再把月度值收敛到剩余预算。延迟导入——org_quota 反向依赖本模块。
    from .org_quota import resolve_member_quota

    resolved = resolve_member_quota(conn, user)
    if resolved["blocked"]:
        raise ApiError(
            403,
            "ORG_BUDGET_EXCEEDED",
            str((resolved["reasons"] or ["组织预算已用尽"])[0]),
        )
    claims = {
        "sub": uid,
        "sid": session_id,
        "role": role,
        "org": effective_org(user),
        "quota": resolved["quota"],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
    }
    access = jwt_encode(
        {**claims, "type": "access", "exp": now + settings.access_ttl_seconds}
    )
    refresh = jwt_encode(
        {
            **claims,
            "type": "refresh",
            "jti": new_session_id(),
            "exp": now + settings.refresh_ttl_seconds,
        }
    )
    conn.execute(
        "INSERT INTO sessions (id, uid, refresh_hash, expires_at_ms, revoked, created_at_ms) "
        "VALUES (?1, ?2, ?3, ?4, 0, ?5)",
        (
            session_id,
            uid,
            token_hash(refresh),
            (now + settings.refresh_ttl_seconds) * 1000,
            now_ms(),
        ),
    )
    # 清理过期会话，并把同一账号超过上限的旧会话吊销（设备丢失/重复登录不再无限累积）
    conn.execute("DELETE FROM sessions WHERE expires_at_ms <= ?1", (now_ms(),))
    conn.execute(
        "UPDATE sessions SET revoked = 1 WHERE uid = ?1 AND revoked = 0 AND id NOT IN ("
        "SELECT id FROM sessions WHERE uid = ?1 ORDER BY created_at_ms DESC, id DESC LIMIT ?2)",
        (uid, MAX_SESSIONS_PER_USER),
    )
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresIn": settings.access_ttl_seconds,
        # profile 里的 quota 必须与令牌声明一致：两处数字不一样时，桌面显示的限额
        # 和引擎实际拦停用的限额会各说各话
        "profile": profile_of(user, resolved["quota"]),
        "quotaResolution": {
            "org": resolved["org"],
            "enforced": resolved["enforced"],
            "clamped": resolved["clamped"],
            "source": resolved["source"],
            "reasons": resolved["reasons"],
            # 声明是签发时刻的快照：组织预算在此期间被同事故用完不会自动改小本令牌
            "snapshot": True,
        },
    }


def issue_tokens(user: sqlite3.Row | dict) -> dict:
    with tx() as conn:
        return _issue_tokens(conn, user)


def refresh_tokens(refresh_token: str) -> dict:
    payload = jwt_decode(
        refresh_token,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    if payload is None or payload.get("type") != "refresh":
        raise ApiError(401, "INVALID_REFRESH", "refresh token 无效或已过期")
    sid = str(payload.get("sid") or "")
    uid = str(payload.get("sub") or "")
    if not sid or not uid:
        raise ApiError(401, "INVALID_REFRESH", "refresh token 缺少会话绑定，请重新登录")
    with tx() as conn:
        cur = conn.execute(
            "UPDATE sessions SET revoked = 1 "
            "WHERE id = ?1 AND uid = ?2 AND refresh_hash = ?3 AND revoked = 0 AND expires_at_ms > ?4",
            (sid, uid, token_hash(refresh_token), now_ms()),
        )
        if cur.rowcount != 1:
            raise ApiError(401, "INVALID_REFRESH", "refresh token 无效或已吊销")
        user = conn.execute("SELECT * FROM users WHERE uid = ?1", (uid,)).fetchone()
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


def find_user_by_session(session_id: str, uid: str) -> sqlite3.Row | None:
    if not session_id or not uid:
        return None
    with tx() as conn:
        return conn.execute(
            "SELECT u.*, s.revoked AS session_revoked, s.expires_at_ms AS session_expires_at_ms "
            "FROM sessions s JOIN users u ON u.uid = s.uid "
            "WHERE s.id = ?1 AND s.uid = ?2",
            (session_id, uid),
        ).fetchone()


def bearer_user(request: Request) -> dict | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    payload = jwt_decode(
        header[7:].strip(),
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    if payload is None or payload.get("type") != "access":
        return None
    uid = str(payload.get("sub") or "")
    sid = str(payload.get("sid") or "")
    if not uid or not sid:
        # 升级前签发的无 sid 令牌无法按会话吊销，直接要求重新登录
        return None
    user = find_user_by_session(sid, uid)
    if user is None:
        return None
    if int(user["session_revoked"] or 0) or int(user["session_expires_at_ms"] or 0) <= now_ms():
        return None
    if (payload.get("role") or "user") != (user["role"] or "user"):
        return None
    if str(payload.get("org") or "") != effective_org(user):
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


def require_gateway_staff(request: Request) -> dict:
    """网关管理面：平台管理员、网关运维、审计员都可读；写操作由代理按角色拦截。"""
    user = require_user(request)
    if role_of(user) not in MANAGEMENT_ROLES:
        raise ApiError(403, "FORBIDDEN", "需要网关管理或审计权限")
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


def issue_code(uid: str, state: str, redirect_uri: str, code_challenge: str = "") -> str:
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
            "code_challenge": code_challenge,
            "expires_at": now + CODE_TTL_SECONDS,
        }
    return code


def _pkce_challenge(verifier: str) -> str:
    """S256：BASE64URL(SHA256(verifier))，去掉 padding（RFC 7636）"""
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def consume_code(code: str, state: str, code_verifier: str = "") -> dict | None:
    with _codes_lock:
        entry = _codes.get(code)
        if entry is None or entry["expires_at"] < time.time():
            _codes.pop(code, None)
            return None
        if not hmac.compare_digest(str(entry["state"]), state):
            return None
        challenge = str(entry.get("code_challenge") or "")
        if challenge:
            if not code_verifier or not hmac.compare_digest(
                challenge, _pkce_challenge(code_verifier)
            ):
                # 校验失败也作废：避免对同一个 code 反复试 verifier
                _codes.pop(code, None)
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
    profile = profile_of(user)
    with tx() as conn:
        profile["depts"] = memberships(conn, user["uid"], effective_org(user))
    return {"ok": True, "profile": profile}


# ── 公钥与网关短票（M1.3） ─────────────────────────────────


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


@router.get("/.well-known/jwks.json")
def well_known_jwks():
    return JSONResponse(
        key_store().jwks(),
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.post("/api/v1/auth/gateway-token")
async def api_gateway_token(request: Request):
    token = _bearer_token(request)
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "缺少 access token")
    payload, reason = jwt_decode_verbose(
        token,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    if reason == "expired":
        raise ApiError(401, "TOKEN_EXPIRED", "access token 已过期，请先刷新")
    if reason or payload is None:
        raise ApiError(401, "AUTH_REQUIRED", "access token 无效")
    if payload.get("type") != "access":
        raise ApiError(401, "AUTH_REQUIRED", "仅接受 access token")
    uid = str(payload.get("sub") or "")
    sid = str(payload.get("sid") or "")
    if not uid or not sid:
        raise ApiError(401, "AUTH_REQUIRED", "access token 缺少会话绑定")
    user = find_user_by_session(sid, uid)
    if user is None:
        raise ApiError(401, "AUTH_REQUIRED", "会话不存在或已吊销")
    if int(user["session_revoked"] or 0) or int(user["session_expires_at_ms"] or 0) <= now_ms():
        raise ApiError(401, "AUTH_REQUIRED", "会话已失效，请重新登录")
    if (payload.get("role") or "user") != (user["role"] or "user"):
        raise ApiError(401, "AUTH_REQUIRED", "账号权限已变更，请重新登录")
    if str(payload.get("org") or "") != effective_org(user):
        raise ApiError(401, "AUTH_REQUIRED", "账号组织已变更，请重新登录")

    ip = _client_ip(request)
    allowed_ip, retry_ip = _login_limiter.check(f"gateway-token:ip:{ip}", 120, 60)
    allowed_user, retry_user = _login_limiter.check(f"gateway-token:{ip}:{uid}", 60, 60)
    if not allowed_ip or not allowed_user:
        retry = max(retry_ip, retry_user, 1)
        raise ApiError(
            429,
            "RATE_LIMITED",
            f"换票过于频繁，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )

    body: dict[str, Any] = {}
    raw = await request.body()
    if raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "INVALID_REQUEST", "请求体需为 JSON 对象") from None
        if not isinstance(parsed, dict):
            raise ApiError(400, "INVALID_REQUEST", "请求体需为 JSON 对象")
        body = parsed
    requested = ""
    if "dept" in body:
        if not isinstance(body["dept"], str):
            raise ApiError(400, "INVALID_REQUEST", "dept 需为字符串")
        requested = body["dept"].strip()

    tenant = str(user["org_id"] or "").strip()
    if not tenant:
        raise ApiError(403, "FORBIDDEN", "个人账号没有组织，不能换取网关令牌")
    with tx() as conn:
        depts = [item["id"] for item in memberships(conn, uid, tenant)]
    if not depts:
        raise ApiError(403, "FORBIDDEN", "账号未分配部门，不能换取网关令牌")
    if requested:
        if requested not in depts:
            raise ApiError(403, "FORBIDDEN", "无权使用该部门")
        dept = requested
    else:
        with tx() as conn:
            dept = primary_department(conn, uid, tenant) or depts[0]

    now = int(time.time())
    access_exp = int(payload["exp"])
    exp = min(now + settings.gateway_token_ttl_seconds, access_exp)
    if exp <= now:
        raise ApiError(401, "TOKEN_EXPIRED", "access token 已过期，请先刷新")
    role = str(user["role"] or "user")
    claims = {
        "sub": uid,
        "sid": sid,
        "tenant": tenant,
        "dept": dept,
        "depts": depts,
        "roles": [role],
        "org": tenant,
        "role": role,
        "iss": settings.jwt_issuer,
        "aud": settings.gateway_audience,
        "type": "access",
        "iat": now,
        "exp": exp,
        "jti": f"gt-{secrets.token_hex(8)}",
    }
    token = key_store().sign(claims)
    return JSONResponse(
        {
            "ok": True,
            "token": token,
            "tokenType": "Bearer",
            "expiresIn": exp - now,
            "expiresAt": exp,
            "tenant": tenant,
            "dept": dept,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/v1/admin/departments")
def api_admin_departments(
    org: str = Query(""),
    _admin: dict = Depends(require_admin),
):
    org_id = _normalize_org(org)
    with tx() as conn:
        if org_id:
            rows = conn.execute(
                "SELECT d.org_id, d.id, d.name, d.created_at_ms, "
                "(SELECT COUNT(*) FROM user_departments ud "
                " WHERE ud.org_id = d.org_id AND ud.dept_id = d.id) AS members "
                "FROM departments d WHERE d.org_id = ?1 ORDER BY d.name, d.id",
                (org_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT d.org_id, d.id, d.name, d.created_at_ms, "
                "(SELECT COUNT(*) FROM user_departments ud "
                " WHERE ud.org_id = d.org_id AND ud.dept_id = d.id) AS members "
                "FROM departments d ORDER BY d.org_id, d.name, d.id"
            ).fetchall()
    return {
        "ok": True,
        "departments": [
            {
                "org": row["org_id"],
                "id": row["id"],
                "name": row["name"],
                "members": row["members"],
                "createdAtMs": row["created_at_ms"],
            }
            for row in rows
        ],
    }


@router.post("/api/v1/admin/departments", status_code=201)
def api_admin_create_department(
    payload: DepartmentIn,
    _admin: dict = Depends(require_admin),
):
    org_id = _normalize_org(payload.org)
    if not org_id:
        raise ApiError(400, "INVALID_ORG", "缺少组织标识")
    dept_id = payload.id.strip().lower()
    if not ORG_RE.match(dept_id):
        raise ApiError(400, "INVALID_DEPARTMENT", "部门标识需为 1~64 位小写字母、数字、下划线或连字符")
    name = payload.name.strip() or dept_id
    if len(name) > 64 or CONTROL_RE.search(name):
        raise ApiError(400, "INVALID_DEPARTMENT", "部门名称不合法")
    with tx() as conn:
        ensure_department(conn, org_id, dept_id, name)
        row = conn.execute(
            "SELECT org_id, id, name, created_at_ms FROM departments "
            "WHERE org_id = ?1 AND id = ?2",
            (org_id, dept_id),
        ).fetchone()
    return {
        "ok": True,
        "department": {
            "org": row["org_id"],
            "id": row["id"],
            "name": row["name"],
            "createdAtMs": row["created_at_ms"],
        },
    }


@router.patch("/api/v1/admin/departments/{org}/{dept}")
def api_admin_rename_department(
    org: str,
    dept: str,
    payload: DepartmentPatchIn,
    _admin: dict = Depends(require_admin),
):
    org_id = _normalize_org(org)
    dept_id = dept.strip().lower()
    if not org_id or not ORG_RE.match(dept_id):
        raise ApiError(400, "INVALID_DEPARTMENT", "部门标识不合法")
    name = payload.name.strip()
    if not name or len(name) > 64 or CONTROL_RE.search(name):
        raise ApiError(400, "INVALID_DEPARTMENT", "部门名称需为 1~64 个字符且不含控制字符")
    with tx() as conn:
        row = conn.execute(
            "SELECT org_id, id FROM departments WHERE org_id = ?1 AND id = ?2",
            (org_id, dept_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "DEPARTMENT_NOT_FOUND", "部门不存在")
        conn.execute(
            "UPDATE departments SET name = ?1 WHERE org_id = ?2 AND id = ?3",
            (name, org_id, dept_id),
        )
    # 部门名不在 JWT claims 里，改名不需要吊销会话；成员变动才需要重新登录
    return {"ok": True, "department": {"org": org_id, "id": dept_id, "name": name}}


@router.patch("/api/v1/admin/users/{uid}")
def api_admin_patch_user(
    uid: str,
    payload: UserClaimsIn,
    admin: dict = Depends(require_admin),
):
    if not payload.model_fields_set:
        raise ApiError(
            400, "NOTHING_TO_UPDATE", "请至少提供 org、role、quota、depts 或 primaryDept"
        )
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE uid = ?1", (uid,)).fetchone()
        if row is None:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
        old_org = str(row["org_id"] or "")
        org = old_org
        if "org" in payload.model_fields_set:
            org = _normalize_org(payload.org)
        quota_json = row["quota_json"]
        if "quota" in payload.model_fields_set:
            quota_json = (
                "{}"
                if payload.quota is None
                else json.dumps(_validate_quota(payload.quota), separators=(",", ":"))
            )
        old_role = str(row["role"] or "user")
        role = old_role
        if "role" in payload.model_fields_set:
            role = _validate_role(payload.role)
            if uid == str(admin.get("uid") or "") and role != old_role:
                raise ApiError(400, "SELF_ROLE_CHANGE", "不能修改自己的角色，请让其他管理员操作")
        conn.execute(
            "UPDATE users SET org_id = ?1, quota_json = ?2, role = ?3 WHERE uid = ?4",
            (org, quota_json, role, uid),
        )
        if role != old_role:
            write_audit(
                conn,
                actor=str(admin.get("uid") or "admin"),
                action="user.role",
                object=uid,
                payload={"before": old_role, "after": role},
                before={"role": old_role},
                after={"role": role},
            )
        if org != old_org:
            conn.execute(
                "DELETE FROM user_departments WHERE uid = ?1 AND org_id = ?2",
                (uid, old_org),
            )
            if org:
                ensure_default_department(conn, org)
                replace_memberships(conn, uid, org, [DEFAULT_DEPT_ID], DEFAULT_DEPT_ID)

        if "depts" in payload.model_fields_set or "primaryDept" in payload.model_fields_set:
            if not org:
                raise ApiError(400, "INVALID_ORG", "个人账号没有组织，不能分配部门")
            known = department_ids(conn, org)
            current = [item["id"] for item in memberships(conn, uid, org)]
            if "depts" in payload.model_fields_set:
                wanted: list[str] = []
                for item in payload.depts or []:
                    dept_id = str(item or "").strip().lower()
                    if not dept_id:
                        continue
                    if dept_id not in known:
                        raise ApiError(400, "UNKNOWN_DEPARTMENT", f"部门不存在：{dept_id}")
                    if dept_id not in wanted:
                        wanted.append(dept_id)
            else:
                wanted = current
            primary = (
                str(payload.primaryDept or "").strip().lower()
                if "primaryDept" in payload.model_fields_set
                else ""
            )
            if primary and primary not in wanted:
                raise ApiError(400, "INVALID_DEPARTMENT", "主部门必须在成员部门内")
            if not primary:
                existing = primary_department(conn, uid, org)
                primary = existing if existing in wanted else (wanted[0] if wanted else "")
            replace_memberships(conn, uid, org, wanted, primary)

        # 声明是签发时刻的快照：改组织/部门/配额后必须让旧令牌立即失效，否则旧声明继续生效
        cur = conn.execute(
            "UPDATE sessions SET revoked = 1 WHERE uid = ?1 AND revoked = 0 AND expires_at_ms > ?2",
            (uid, now_ms()),
        )
        updated = conn.execute("SELECT * FROM users WHERE uid = ?1", (uid,)).fetchone()
        profile = profile_of(updated)
        profile["depts"] = memberships(conn, uid, effective_org(updated))
    return {
        "ok": True,
        "profile": profile,
        "sessionsRevoked": cur.rowcount,
    }


@router.get("/api/v1/admin/users")
def api_admin_users(
    q: str = Query(""),
    page: int = Query(1),
    pageSize: int = Query(20),
    _admin: dict = Depends(require_admin),
):
    q = (q or "").strip()
    if len(q) > 100:
        raise ApiError(400, "INVALID_QUERY", "搜索词不能超过 100 字")
    page = max(1, page)
    page_size = min(100, max(1, pageSize))
    where_sql = ""
    params: list[Any] = []
    if q:
        where_sql = " WHERE instr(lower(email), ?1) > 0 OR instr(lower(nickname), ?1) > 0"
        params.append(q.lower())
    with tx() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM users" + where_sql, params).fetchone()["n"]
        rows = conn.execute(
            "SELECT uid, email, nickname, role, plan, org_id, created_at_ms FROM users"
            + where_sql
            + " ORDER BY created_at_ms DESC, rowid DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        cards = []
        for row in rows:
            org = row["org_id"] or f"personal-{row['uid']}"
            members = memberships(conn, row["uid"], org) if row["org_id"] else []
            cards.append(
                {
                    "uid": row["uid"],
                    "email": row["email"],
                    "nickname": row["nickname"],
                    "role": row["role"],
                    "plan": row["plan"],
                    "org": org,
                    "depts": members,
                    "primaryDept": next(
                        (item["id"] for item in members if item["primary"]), ""
                    ),
                    "createdAtMs": row["created_at_ms"],
                }
            )
    return {
        "ok": True,
        "users": cards,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "hasMore": page * page_size < total,
    }


@router.post("/api/v1/admin/users/{uid}/revoke-sessions")
def api_admin_revoke_sessions(uid: str, _admin: dict = Depends(require_admin)):
    with tx() as conn:
        row = conn.execute("SELECT uid FROM users WHERE uid = ?1", (uid,)).fetchone()
        if row is None:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
        cur = conn.execute(
            "UPDATE sessions SET revoked = 1 WHERE uid = ?1 AND revoked = 0 AND expires_at_ms > ?2",
            (uid, now_ms()),
        )
        return {"ok": True, "revoked": cur.rowcount}


# ── 引擎令牌吊销内省 ─────────────────────────────────────────
# 引擎默认只按 JWT 的 exp 失效（最长 15 分钟窗口）。开启内省后，引擎带共享令牌
# 来这里查某 sid 是否仍有效，把「删除用户 / 强制下线」的生效窗口压到缓存 TTL 内。
# 共享令牌取自 TT_ENGINE_INTROSPECT_TOKEN；未配置时端点关闭（返回 404），
# 引擎侧对不可达按放行处理，离线与桌面模式行为不变。


@router.get("/api/v1/engine/introspect")
def api_engine_introspect(request: Request, sub: str = "", sid: str = ""):
    expected = os.environ.get("TT_ENGINE_INTROSPECT_TOKEN", "").strip()
    got = (request.headers.get("x-tt-engine-introspect") or "").strip()
    if not expected:
        raise ApiError(404, "INTROSPECT_DISABLED", "未启用引擎令牌内省")
    if not got or not hmac.compare_digest(expected, got):
        raise ApiError(401, "INTROSPECT_UNAUTHORIZED", "内省令牌无效")
    if not sub or not sid:
        raise ApiError(400, "INVALID_INTROSPECT_QUERY", "sub 与 sid 必填")
    user = find_user_by_session(sid, sub)
    if user is None:
        return {"ok": True, "active": False, "reason": "session_not_found"}
    if int(user["session_revoked"] or 0):
        return {"ok": True, "active": False, "reason": "session_revoked"}
    if int(user["session_expires_at_ms"] or 0) <= now_ms():
        return {"ok": True, "active": False, "reason": "session_expired"}
    return {"ok": True, "active": True}


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

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _page(
    title: str,
    body_html: str,
    status: int = 200,
    form_action_origin: str | None = None,
) -> HTMLResponse:
    # Chrome 会把 form-action 套到表单提交后的 302 跳转上；回跳地址是另一个端口的
    # loopback，必须显式放行，否则浏览器直接拦掉跳转，桌面端永远收不到 code。
    form_action = "'self'"
    if form_action_origin:
        form_action = f"'self' {form_action_origin}"
    csp = _CSP.replace("form-action 'self'", f"form-action {form_action}")
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
        headers={"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff"},
    )


def _hidden_fields(state: str, redirect_uri: str, code_challenge: str = "") -> str:
    challenge = (
        f'<input type="hidden" name="code_challenge" value="{html.escape(code_challenge, quote=True)}">'
        if code_challenge
        else ""
    )
    return (
        f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">'
        f'<input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri, quote=True)}">'
        f"{challenge}"
    )


def _switch_link(path: str, state: str, redirect_uri: str, code_challenge: str = "") -> str:
    params = {"state": state, "redirect_uri": redirect_uri}
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    query = urllib.parse.urlencode(params)
    return html.escape(f"{path}?{query}", quote=True)


def _login_page(
    state: str, redirect_uri: str, error: str = "", status: int = 200, code_challenge: str = ""
) -> HTMLResponse:
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    link = _switch_link("/auth/register", state, redirect_uri, code_challenge)
    return _page(
        "登录",
        f"""<h2>登录账号</h2><div class="sub">在浏览器完成验证后，将自动返回通天办公桌面端</div>
<form method="post" action="/api/auth/login">
  {_hidden_fields(state, redirect_uri, code_challenge)}
  <label>邮箱</label><input type="email" name="email" required placeholder="you@example.com">
  <label>密码</label><input type="password" name="password" required placeholder="请输入密码">
  <button class="btn" type="submit">登 录</button>{err}
</form>
<div class="switch">还没有账号？<a href="{link}">立即注册</a></div>""",
        status,
        form_action_origin=_loopback_origin(redirect_uri),
    )


def _register_page(
    state: str, redirect_uri: str, error: str = "", status: int = 200, code_challenge: str = ""
) -> HTMLResponse:
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    link = _switch_link("/auth/login", state, redirect_uri, code_challenge)
    return _page(
        "注册",
        f"""<h2>注册账号</h2><div class="sub">注册成功后自动登录，并返回通天办公桌面端</div>
<form method="post" action="/api/auth/register">
  {_hidden_fields(state, redirect_uri, code_challenge)}
  <label>昵称</label><input type="text" name="nickname" required maxlength="24" placeholder="怎么称呼你">
  <label>邮箱</label><input type="email" name="email" required placeholder="you@example.com">
  <label>密码</label><input type="password" name="password" required minlength="8" placeholder="至少 8 位">
  <label>确认密码</label><input type="password" name="confirm" required minlength="8" placeholder="再次输入密码">
  <button class="btn" type="submit">注册并登录</button>{err}
</form>
<div class="switch">已有账号？<a href="{link}">直接登录</a></div>""",
        status,
        form_action_origin=_loopback_origin(redirect_uri),
    )


def _success_page(nickname: str) -> HTMLResponse:
    return _page(
        "登录成功",
        f"""<h2>登录成功</h2>
<div class="ok">{html.escape(nickname)}，验证通过。正在返回通天办公桌面端，如未自动跳转可关闭此页面。</div>""",
    )


def _loopback_origin(redirect_uri: str) -> str | None:
    """回跳地址的 origin（含端口），用于放行表单提交后的跨端口跳转。"""
    if not _loopback_redirect(redirect_uri):
        return None
    try:
        parts = urllib.parse.urlsplit(redirect_uri)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


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


def _clean_challenge(raw: str) -> str:
    """只接受合法的 S256 challenge；空串表示旧客户端（不带 PKCE）。"""
    challenge = (raw or "").strip()
    return challenge if PKCE_CHALLENGE_RE.match(challenge) else ""


@router.get("/auth/login", response_class=HTMLResponse)
def page_login(state: str = "", redirect_uri: str = "", code_challenge: str = ""):
    return _login_page(state, redirect_uri, code_challenge=_clean_challenge(code_challenge))


@router.get("/auth/register", response_class=HTMLResponse)
def page_register(state: str = "", redirect_uri: str = "", code_challenge: str = ""):
    return _register_page(state, redirect_uri, code_challenge=_clean_challenge(code_challenge))


@router.post("/api/auth/register")
async def legacy_register(request: Request):
    form = await _form(request)
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    confirm = form.get("confirm", "")
    state = form.get("state", "")
    redirect_uri = form.get("redirect_uri", "")
    code_challenge = _clean_challenge(form.get("code_challenge", ""))

    def fail(message: str, status: int = 400) -> HTMLResponse:
        return _register_page(state, redirect_uri, message, status, code_challenge)

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

    code = issue_code(user["uid"], state, redirect_uri, code_challenge)
    return RedirectResponse(_append_code(redirect_uri, code, state), status_code=302)


@router.post("/api/auth/login")
async def legacy_login(request: Request):
    form = await _form(request)
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    state = form.get("state", "")
    redirect_uri = form.get("redirect_uri", "")
    code_challenge = _clean_challenge(form.get("code_challenge", ""))

    def fail(message: str, status: int = 401) -> HTMLResponse:
        return _login_page(state, redirect_uri, message, status, code_challenge)

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

    code = issue_code(user["uid"], state, redirect_uri, code_challenge)
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
    verifier = body.get("codeVerifier", body.get("code_verifier", ""))
    if not isinstance(code, str) or not isinstance(state, str) or not code or not state:
        raise ApiError(400, "INVALID_CODE", "缺少 code/state")
    if verifier is not None and not isinstance(verifier, str):
        raise ApiError(400, "INVALID_CODE", "codeVerifier 需为字符串")
    entry = consume_code(code, state, verifier or "")
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
