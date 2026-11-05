"""RS256 签名密钥：生成、持久化、轮换与 JWKS 公钥。

私钥落盘为 `{data_dir}/keys/jwt-rs256-<kid>.pem`，`index.json` 记录当前密钥。
轮换后旧钥保留在 JWKS 里，直到下一次轮换才清理，保证网关缓存窗口内旧票仍可验。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger("tongtian.keys")

KEY_PREFIX = "jwt-rs256-"
MANIFEST_NAME = "index.json"
MAX_KEYS = 2


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding_length = (-len(raw)) % 4
    return base64.urlsafe_b64decode(raw + "=" * padding_length)


def _int_to_b64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return _b64url(raw)


@dataclass(frozen=True)
class SigningKey:
    kid: str
    private_pem: bytes
    created_at: int
    key: rsa.RSAPrivateKey


class KeyStore:
    """单进程密钥库；进程内缓存私钥对象，JWKS 直接由私钥推导。"""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._lock = threading.RLock()
        self._keys: dict[str, SigningKey] = {}
        self._current: str = ""

    # ── 生命周期 ────────────────────────────────────────────

    def load_or_create(self, max_age_seconds: int = 0) -> None:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._load_existing()
            if not self._keys:
                self._generate()
                logger.info("已生成 RS256 签名密钥 kid=%s", self._current)
            elif max_age_seconds > 0:
                age = int(time.time()) - self._keys[self._current].created_at
                if age > max_age_seconds:
                    self.rotate()
                    logger.info("签名密钥已轮换，新 kid=%s", self._current)
            self._prune()
            self._save_manifest()

    def _load_existing(self) -> None:
        manifest = self._read_manifest()
        candidates: list[SigningKey] = []
        for path in sorted(self._dir.glob(f"{KEY_PREFIX}*.pem")):
            kid = path.name[len(KEY_PREFIX) : -4]
            try:
                pem = path.read_bytes()
            except OSError:
                continue
            try:
                key = serialization.load_pem_private_key(pem, password=None)
            except (ValueError, TypeError):
                logger.warning("签名密钥文件无法解析，已忽略：%s", path.name)
                continue
            if not isinstance(key, rsa.RSAPrivateKey):
                continue
            created = int(path.stat().st_mtime)
            candidates.append(SigningKey(kid, pem, created, key))
        if not candidates:
            return
        by_kid = {item.kid: item for item in candidates}
        current = str(manifest.get("current") or "")
        if current not in by_kid:
            current = max(candidates, key=lambda item: item.created_at).kid
        self._keys = by_kid
        self._current = current

    def _read_manifest(self) -> dict:
        path = self._dir / MANIFEST_NAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_manifest(self) -> None:
        payload = {
            "current": self._current,
            "keys": {
                kid: {"createdAt": item.created_at}
                for kid, item in sorted(
                    self._keys.items(), key=lambda pair: pair[1].created_at, reverse=True
                )
            },
        }
        path = self._dir / MANIFEST_NAME
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)

    def _generate(self) -> SigningKey:
        kid = f"k-{secrets.token_hex(6)}"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path = self._dir / f"{KEY_PREFIX}{kid}.pem"
        path.write_bytes(pem)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        item = SigningKey(kid=kid, private_pem=pem, created_at=int(time.time()), key=key)
        self._keys[kid] = item
        self._current = kid
        return item

    def rotate(self) -> str:
        with self._lock:
            if not self._keys:
                self._generate()
            else:
                self._generate()
            self._prune()
            self._save_manifest()
            return self._current

    def _prune(self) -> None:
        if len(self._keys) <= MAX_KEYS:
            return
        ordered = sorted(
            self._keys.values(), key=lambda item: item.created_at, reverse=True
        )
        keep = {item.kid for item in ordered[:MAX_KEYS]}
        keep.add(self._current)
        for kid in list(self._keys):
            if kid in keep:
                continue
            self._keys.pop(kid, None)
            try:
                (self._dir / f"{KEY_PREFIX}{kid}.pem").unlink()
            except OSError:
                pass

    # ── 使用 ────────────────────────────────────────────────

    @property
    def current_kid(self) -> str:
        return self._current

    def signing(self) -> SigningKey:
        with self._lock:
            if not self._current:
                raise RuntimeError("RS256 签名密钥尚未初始化")
            return self._keys[self._current]

    def sign(self, claims: dict) -> str:
        header = {"alg": "RS256", "typ": "JWT", "kid": self.signing().kid}
        header_part = _b64url(
            json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        claims_part = _b64url(
            json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        signing_input = f"{header_part}.{claims_part}".encode("ascii")
        signature = self.signing().key.sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{header_part}.{claims_part}.{_b64url(signature)}"

    def verify(self, token: str, expected_audience: str = "") -> tuple[dict, str]:
        """用本库公钥验 RS256 签名，返回 `(claims, 原因)`；失败时 claims 为空字典。

        只验签名与受众/过期，不判业务语义——业务文档还要各自比对载荷里的摘要，
        因为签名保护的是摘要，正文随响应体一起传输。
        """
        parts = (token or "").split(".")
        if len(parts) != 3:
            return {}, "签名格式不合法"
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, json.JSONDecodeError):
            return {}, "签名载荷无法解析"
        if not isinstance(header, dict) or header.get("alg") != "RS256":
            return {}, "签名算法不是 RS256"
        kid = str(header.get("kid") or "")
        with self._lock:
            item = self._keys.get(kid)
        if item is None:
            return {}, f"签名密钥不在 JWKS 中：{kid or '(空)'}"
        try:
            item.key.public_key().verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            return {}, "签名校验失败：载荷与签名不匹配"
        if not isinstance(claims, dict):
            return {}, "签名载荷不是对象"
        if expected_audience and str(claims.get("aud") or "") != expected_audience:
            return {}, "签名受众不符"
        expires = claims.get("exp")
        if isinstance(expires, int) and not isinstance(expires, bool):
            if expires < int(time.time()):
                return {}, "签名已过期"
        return claims, "可信"

    def jwks(self) -> dict:
        with self._lock:
            keys = sorted(
                self._keys.values(), key=lambda item: item.created_at, reverse=True
            )
            public = [self._jwk(item) for item in keys]
        return {"keys": public}

    @staticmethod
    def _jwk(item: SigningKey) -> dict:
        numbers = item.key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": item.kid,
            "use": "sig",
            "alg": "RS256",
            "n": _int_to_b64url(numbers.n),
            "e": _int_to_b64url(numbers.e),
        }


_store: KeyStore | None = None
_store_lock = threading.Lock()


def key_store() -> KeyStore:
    """按配置懒加载单例；启动时 lifespan 会显式初始化。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from .config import settings

                store = KeyStore(settings.jwt_key_dir)
                store.load_or_create(settings.jwt_key_max_age_seconds)
                _store = store
    return _store


def init_keys() -> KeyStore:
    global _store
    from .config import settings

    with _store_lock:
        _store = KeyStore(settings.jwt_key_dir)
        _store.load_or_create(settings.jwt_key_max_age_seconds)
    return _store


def reset_for_tests() -> None:
    global _store
    with _store_lock:
        _store = None


def peek_keys(directory: Any) -> dict[str, Any]:
    """只读看一眼签名密钥目录：不建目录、不生成钥、不轮换。

    自检（`app.cli doctor`）要报「签名钥在不在、有几把、当前 kid」，但不能因为做了一次
    体检就把密钥生成出来——`key_store()` 的懒加载会 `mkdir` + 生成，还会在
    `TT_JWT_KEY_MAX_AGE_SECONDS` 到点时**顺手轮换**；轮换超过 `MAX_KEYS` 次以后，
    旧锚点和旧档位表就再也验不过。诊断面必须比业务面更保守。
    """
    base = Path(directory)
    manifest: dict[str, Any] = {}
    try:
        raw = json.loads((base / MANIFEST_NAME).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            manifest = raw
    except (OSError, json.JSONDecodeError):
        manifest = {}
    kids = sorted(
        path.name[len(KEY_PREFIX) : -4] for path in base.glob(f"{KEY_PREFIX}*.pem")
    )
    listed = manifest.get("keys")
    known = [str(kid) for kid in (listed or {}) if str(kid)]
    # 当前 kid 以清单为准；清单读不出来时按文件名兜底，只说明「有钥」这件事
    current = str(manifest.get("current") or "")
    if current and current not in kids:
        kids.append(current)
    return {
        "keyCount": len(kids),
        "currentKid": current,
        "kids": sorted(set(kids)),
        "manifestReadable": bool(manifest),
        "listedKeys": sorted(set(known)),
    }


def peek_store(directory: Any) -> KeyStore | None:
    """把某个目录里**已有**的钥装成一个只读密钥库；没有可用钥就返回 None。

    与 `key_store()` 的区别：它绝不建目录、绝不生成私钥、也绝不按 max_age 轮换。
    自检（`app.cli doctor`）和「换机恢复演练」需要拿指定目录里的公钥去验旧锚点，
    这种读操作一旦带上写副作用，就会出现「做一次体检把旧证据变成验不过」的事故。
    """
    store = KeyStore(Path(directory))
    store._load_existing()  # noqa: SLF001 - 同模块内部使用，正是为了不经过 load_or_create
    if not store._keys:  # noqa: SLF001
        return None
    return store
