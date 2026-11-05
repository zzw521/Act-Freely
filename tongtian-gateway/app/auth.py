"""鉴权：RS256/HS256 JWT 与网关 API Key（`ttg_`）入口。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from .config import Settings
from .errors import GatewayError
from .jwks import client_for


@dataclass(frozen=True)
class Principal:
    subject: str
    session_id: str
    tenant: str
    department: str
    departments: tuple[str, ...]
    roles: tuple[str, ...]
    auth_kind: str
    api_key_id: str | None = None
    scopes: tuple[str, ...] = ()
    model_allowlist: tuple[str, ...] = ()
    api_key_rate_limit_rpm: int = 0
    api_key_rate_limit_tpm: int = 0
    api_key_quota_monthly_tokens: int = 0

    @property
    def quota_key(self) -> str:
        return f"{self.tenant}:{self.department}:{self.subject}"

    @property
    def is_guest(self) -> bool:
        return "guest" in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError) as exc:
        raise GatewayError.of("AUTH_REQUIRED", "令牌格式无效") from exc


def encode_dev_hs256(claims: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_part = _b64url_encode(
        json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    payload_part = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64url_encode(signature)}"


def decode_dev_hs256(token: str, secret: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise GatewayError.of("AUTH_REQUIRED", "令牌格式无效")
    header_part, payload_part, signature_part = parts
    try:
        header = json.loads(_b64url_decode(header_part))
    except json.JSONDecodeError as exc:
        raise GatewayError.of("AUTH_REQUIRED", "令牌头无效") from exc
    if header.get("alg") != "HS256":
        raise GatewayError.of("AUTH_REQUIRED", "不支持的令牌算法")
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(signature_part)):
        raise GatewayError.of("AUTH_REQUIRED", "令牌签名无效")
    try:
        claims = json.loads(_b64url_decode(payload_part))
    except json.JSONDecodeError as exc:
        raise GatewayError.of("AUTH_REQUIRED", "令牌载荷无效") from exc
    if not isinstance(claims, dict):
        raise GatewayError.of("AUTH_REQUIRED", "令牌载荷无效")
    return claims


MAX_TOKEN_BYTES = 8 * 1024
CLOCK_SKEW_SECONDS = 60
MAX_TOKEN_TTL_SECONDS = 900


def decode_rs256(token: str, settings: Settings) -> dict:
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise GatewayError.of("AUTH_REQUIRED", "令牌过长")
    parts = token.split(".")
    if len(parts) != 3:
        raise GatewayError.of("AUTH_REQUIRED", "令牌格式无效")
    header_part, payload_part, signature_part = parts
    try:
        header = json.loads(_b64url_decode(header_part))
    except json.JSONDecodeError as exc:
        raise GatewayError.of("AUTH_REQUIRED", "令牌头无效") from exc
    if not isinstance(header, dict) or header.get("alg") != "RS256":
        raise GatewayError.of("AUTH_REQUIRED", "不支持的令牌算法")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid.strip():
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 kid")
    key = client_for(settings).get_key(kid.strip())
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    try:
        key.verify(
            _b64url_decode(signature_part),
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, ValueError) as exc:
        raise GatewayError.of("AUTH_REQUIRED", "令牌签名无效") from exc
    try:
        claims = json.loads(_b64url_decode(payload_part))
    except json.JSONDecodeError as exc:
        raise GatewayError.of("AUTH_REQUIRED", "令牌载荷无效") from exc
    if not isinstance(claims, dict):
        raise GatewayError.of("AUTH_REQUIRED", "令牌载荷无效")
    return claims


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _check_rs256_claims(claims: dict, settings: Settings, now: int) -> None:
    if str(claims.get("iss") or "") != settings.jwt_issuer:
        raise GatewayError.of("AUTH_REQUIRED", "令牌签发方不匹配")
    if not _audience_ok(claims.get("aud"), settings.jwt_audience):
        raise GatewayError.of("AUTH_REQUIRED", "令牌受众不匹配")
    if claims.get("type") != "access":
        raise GatewayError.of("AUTH_REQUIRED", "令牌类型不是 access")

    iat = _number(claims.get("iat"))
    if iat is None:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少签发时间")
    if int(iat) > now + CLOCK_SKEW_SECONDS:
        raise GatewayError.of("AUTH_REQUIRED", "令牌签发时间异常")
    nbf = _number(claims.get("nbf"))
    if nbf is not None and int(nbf) > now + CLOCK_SKEW_SECONDS:
        raise GatewayError.of("AUTH_REQUIRED", "令牌尚未生效")
    exp = _number(claims.get("exp"))
    if exp is None:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少过期时间")
    if int(exp) <= now:
        raise GatewayError.of("TOKEN_EXPIRED", "访问令牌已过期，请刷新后重试")
    if int(exp) - int(iat) > MAX_TOKEN_TTL_SECONDS:
        raise GatewayError.of("AUTH_REQUIRED", "网关令牌有效期超过上限")

    if not str(claims.get("sub") or "").strip():
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 sub")
    if not str(claims.get("sid") or "").strip():
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少会话绑定")

    tenant = str(claims.get("tenant") or "").strip()
    org = str(claims.get("org") or "").strip()
    if tenant and org and tenant != org:
        raise GatewayError.of("AUTH_REQUIRED", "令牌 tenant 与 org 冲突")
    tenant = tenant or org
    if not tenant:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 tenant")

    dept = str(claims.get("dept") or "").strip()
    if not dept:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 dept")
    raw_depts = claims.get("depts")
    if raw_depts is None:
        depts = (dept,)
    elif isinstance(raw_depts, list):
        depts = tuple(str(item).strip() for item in raw_depts if str(item).strip())
    else:
        raise GatewayError.of("AUTH_REQUIRED", "令牌 depts 格式无效")
    if not depts or dept not in depts:
        raise GatewayError.of("AUTH_REQUIRED", "令牌 dept 不在 depts 内")

    roles = _roles(claims.get("roles"))
    legacy_role = _roles(claims.get("role"))
    if roles and legacy_role and legacy_role[0] not in roles:
        raise GatewayError.of("AUTH_REQUIRED", "令牌 roles 与 role 冲突")
    roles = roles or legacy_role
    if not roles:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 roles")


def _principal_from_rs256(claims: dict) -> Principal:
    dept = str(claims.get("dept") or "").strip()
    raw_depts = claims.get("depts")
    if isinstance(raw_depts, list):
        departments = tuple(str(item).strip() for item in raw_depts if str(item).strip())
    else:
        departments = (dept,) if dept else ()
    roles = _roles(claims.get("roles")) or _roles(claims.get("role"))
    return Principal(
        subject=str(claims.get("sub") or "").strip(),
        session_id=str(claims.get("sid") or "").strip(),
        tenant=str(claims.get("tenant") or claims.get("org") or "").strip(),
        department=dept,
        departments=departments,
        roles=roles,
        auth_kind="jwt",
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    target = name.lower()
    for key, item in headers.items():
        if key.lower() == target:
            return item
    return None


def _bearer(headers: Mapping[str, str]) -> str | None:
    raw = _header(headers, "authorization") or ""
    scheme, _, token = raw.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _extract_token(headers: Mapping[str, str]) -> str | None:
    return (
        _bearer(headers)
        or _header(headers, "x-api-key")
        or _header(headers, "x-goog-api-key")
    )


def _audience_ok(aud, expected: str) -> bool:
    if isinstance(aud, str):
        return aud == expected
    if isinstance(aud, list):
        return expected in aud
    return False


def _roles(raw) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if item)
    return ()


def _principal_from_claims(claims: dict) -> Principal:
    tenant = str(claims.get("tenant") or claims.get("org") or "").strip()
    if not tenant:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 tenant")
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少 sub")
    department = str(claims.get("dept") or "").strip()
    departments = tuple(
        str(item) for item in (claims.get("depts") or []) if str(item).strip()
    )
    if not departments and department:
        departments = (department,)
    roles = _roles(claims.get("roles")) or _roles(claims.get("role"))
    return Principal(
        subject=subject,
        session_id=str(claims.get("sid") or ""),
        tenant=tenant,
        department=department,
        departments=departments,
        roles=roles,
        auth_kind="jwt",
    )


def _check_claims(claims: dict, settings: Settings, now: int) -> None:
    if str(claims.get("iss") or "") != settings.jwt_issuer:
        raise GatewayError.of("AUTH_REQUIRED", "令牌签发方不匹配")
    if not _audience_ok(claims.get("aud"), settings.jwt_audience):
        raise GatewayError.of("AUTH_REQUIRED", "令牌受众不匹配")
    if claims.get("type") != "access":
        raise GatewayError.of("AUTH_REQUIRED", "令牌类型不是 access")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise GatewayError.of("AUTH_REQUIRED", "令牌缺少过期时间")
    if int(exp) <= now:
        raise GatewayError.of("TOKEN_EXPIRED", "访问令牌已过期，请刷新后重试")


def _with_department_override(
    principal: Principal, headers: Mapping[str, str]
) -> Principal:
    requested = (_header(headers, "x-tt-department") or "").strip()
    if not requested or requested == principal.department:
        return principal
    if requested not in principal.departments:
        raise GatewayError.of("FORBIDDEN", "无权切换到该部门")
    return Principal(
        subject=principal.subject,
        session_id=principal.session_id,
        tenant=principal.tenant,
        department=requested,
        departments=principal.departments,
        roles=principal.roles,
        auth_kind=principal.auth_kind,
        api_key_id=principal.api_key_id,
        scopes=principal.scopes,
        model_allowlist=principal.model_allowlist,
        api_key_rate_limit_rpm=principal.api_key_rate_limit_rpm,
        api_key_rate_limit_tpm=principal.api_key_rate_limit_tpm,
        api_key_quota_monthly_tokens=principal.api_key_quota_monthly_tokens,
    )


def authenticate(
    headers: Mapping[str, str],
    settings: Settings,
    *,
    now: int | None = None,
    api_keys=None,
) -> Principal:
    token = _extract_token(headers)
    if not token:
        raise GatewayError.of("AUTH_REQUIRED", "缺少访问令牌")
    current = int(now if now is not None else time.time())
    if token.startswith("ttg_"):
        if api_keys is None:
            raise GatewayError.of("AUTH_REQUIRED", "网关 API Key 未配置")
        record = api_keys.verify(
            token, now=None if now is None else int(now * 1000)
        )
        return _with_department_override(
            Principal(
                subject=record.sub,
                session_id="",
                tenant=record.tenant,
                department=record.dept,
                departments=(record.dept,),
                roles=("member",),
                auth_kind="api_key",
                api_key_id=record.key_id,
                scopes=record.scopes,
                model_allowlist=record.model_allowlist,
                api_key_rate_limit_rpm=record.rate_limit_rpm,
                api_key_rate_limit_tpm=record.rate_limit_tpm,
                api_key_quota_monthly_tokens=record.quota_monthly_tokens,
            ),
            headers,
        )
    if settings.jwt_mode == "rs256":
        claims = decode_rs256(token, settings)
        _check_rs256_claims(claims, settings, current)
        return _with_department_override(_principal_from_rs256(claims), headers)
    if settings.jwt_mode != "hs256-dev":
        raise GatewayError.of("AUTH_UNAVAILABLE", "当前未配置可用的令牌校验方式")
    claims = decode_dev_hs256(token, settings.dev_hs256_secret)
    _check_claims(claims, settings, current)
    return _with_department_override(_principal_from_claims(claims), headers)
