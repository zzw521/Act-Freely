"""JWT、密码哈希、限流与标识归一。

标识规则与桌面端保持一致：
- 技能/套件 ASCII 名 → 小写 slug（上限 64）；
- 含非 ASCII 字符时，套件用 `suite-<crc32:08x>`，技能用 `skill-<crc32:08x>`，
  MCP 用 `mcp-<crc32:08x>`（与 Rust `suite_id_of` 的 crc32 口径一致）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import zlib
from collections import deque

from .config import settings

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

PASSWORD_ITERATIONS = 260_000
MAX_ID_LEN = 64


# ── base64url / JWT HS256（标准库实现） ─────────────────────


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def jwt_encode(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    body = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claims = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{body}.{claims}".encode("ascii")
    sig = hmac.new(settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{body}.{claims}.{_b64url_encode(sig)}"


def jwt_decode(token: str) -> dict | None:
    try:
        body, claims, sig = token.split(".")
        signing_input = f"{body}.{claims}".encode("ascii")
        expected = hmac.new(
            settings.jwt_secret.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            return None
        payload = json.loads(_b64url_decode(claims))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ── 密码哈希 ─────────────────────────────────────────────────


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except Exception:
        return False


_DUMMY_HASH: str | None = None


def dummy_verify(password: str) -> None:
    """邮箱不存在时也跑一次 PBKDF2，避免通过响应耗时枚举账号。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("tongtian-dummy-password")
    verify_password(password, _DUMMY_HASH)


# ── 随机标识与摘要 ───────────────────────────────────────────


def new_uid() -> str:
    return f"tt-{secrets.token_hex(6)}"


def new_session_id() -> str:
    return f"sess-{secrets.token_hex(8)}"


def new_upload_token() -> str:
    return secrets.token_hex(8)


def new_code() -> str:
    return secrets.token_urlsafe(24)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── 标识归一 ─────────────────────────────────────────────────


def slugify(raw: str) -> str:
    out: list[str] = []
    pending_dash = False
    for ch in raw.strip():
        if ch.isascii() and (ch.islower() or ch.isdigit() or ch == "_"):
            if pending_dash:
                out.append("-")
                pending_dash = False
            out.append(ch)
        elif ch.isascii() and ch.isupper():
            if pending_dash:
                out.append("-")
                pending_dash = False
            out.append(ch.lower())
        else:
            pending_dash = bool(out)
    value = "".join(out).strip("-")
    if not value or len(value) > MAX_ID_LEN:
        return ""
    return value


def suite_id_of(name: str) -> str:
    value = name.strip()
    if value.isascii():
        slug = slugify(value)
        if slug:
            return slug
    return f"suite-{zlib.crc32(value.encode('utf-8')) & 0xFFFFFFFF:08x}"


def skill_id_of(name: str) -> str:
    value = name.strip()
    if value.isascii():
        slug = slugify(value)
        if slug:
            return slug
    return f"skill-{zlib.crc32(value.encode('utf-8')) & 0xFFFFFFFF:08x}"


def mcp_id_of(name: str) -> str:
    value = name.strip()
    if value.isascii():
        slug = slugify(value)
        if slug:
            return slug
    return f"mcp-{zlib.crc32(value.encode('utf-8')) & 0xFFFFFFFF:08x}"


def item_id_of(item_type: str, name: str) -> str:
    if item_type == "suite":
        return suite_id_of(name)
    if item_type == "skill":
        return skill_id_of(name)
    return mcp_id_of(name)


def normalize_version(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    if not value or not VERSION_RE.match(value):
        return None
    return value


# ── 限流（内存滑动窗口，单进程） ────────────────────────────


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 4096:
                stale = [
                    k for k, dq in self._hits.items()
                    if k != key and (not dq or now - dq[-1] > window_seconds)
                ]
                for k in stale:
                    self._hits.pop(k, None)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                retry = max(1, int(window_seconds - (now - hits[0])) + 1)
                return False, retry
            hits.append(now)
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
