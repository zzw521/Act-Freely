"""厂商密钥加解密：AES-GCM + 主密钥版本轮换。

密文带 `keyVersion`；`TT_GATEWAY_MASTER_KEYS` 可同时提供多把历史主密钥，
使轮换后旧密文仍可读。明文只在创建/轮换响应里出现一次。
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings

NONCE_BYTES = 12
KEY_BYTES = 32


@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    key_version: int
    last4: str


def _decode_master_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("主密钥必须是 base64 编码") from exc
    if len(key) != KEY_BYTES:
        raise ValueError(f"主密钥必须是 {KEY_BYTES} 字节（base64 后 44 字符）")
    return key


def _fingerprint(secret: str) -> str:
    return secret[-4:]


class Secrets:
    def __init__(
        self, keys: Mapping[int, str | bytes], *, current_version: int
    ) -> None:
        decoded: dict[int, bytes] = {}
        for version, value in keys.items():
            if isinstance(value, bytes):
                key = value
                if len(key) != KEY_BYTES:
                    raise ValueError(f"主密钥版本 {version} 长度不正确")
            else:
                key = _decode_master_key(value)
            decoded[int(version)] = key
        if decoded and current_version not in decoded:
            raise ValueError(f"当前主密钥版本 {current_version} 不在密钥环中")
        self._keys = decoded
        self._current_version = int(current_version)
        self._aesgcm = AESGCM(decoded[current_version]) if decoded else None

    @classmethod
    def disabled(cls) -> "Secrets":
        return cls({}, current_version=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Secrets":
        keys: dict[int, str] = dict(settings.master_keys)
        if settings.master_key:
            keys[settings.master_key_version] = settings.master_key
        if not keys:
            return cls.disabled()
        current = (
            settings.master_key_version
            if settings.master_key
            else max(keys)
        )
        return cls(keys, current_version=current)

    @property
    def available(self) -> bool:
        return self._aesgcm is not None

    @property
    def current_version(self) -> int:
        return self._current_version

    def with_rotation(self, version: int, master_key: str) -> "Secrets":
        keys: dict[int, str | bytes] = dict(self._keys)
        keys[int(version)] = master_key
        return Secrets(keys, current_version=int(version))

    def encrypt(self, secret: str) -> EncryptedSecret:
        if not self.available:
            raise RuntimeError("未配置 TT_GATEWAY_MASTER_KEY，无法保存密钥")
        if not isinstance(secret, str) or not secret:
            raise ValueError("密钥不能为空")
        version = self._current_version
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(
            nonce, secret.encode("utf-8"), self._aad(version)
        )
        return EncryptedSecret(ciphertext, nonce, version, _fingerprint(secret))

    def decrypt(self, ciphertext: bytes, nonce: bytes, key_version: int) -> str:
        key = self._keys.get(int(key_version))
        if key is None:
            raise ValueError(f"缺少主密钥版本 {key_version}，无法解密")
        try:
            plaintext = AESGCM(key).decrypt(
                nonce, ciphertext, self._aad(int(key_version))
            )
        except InvalidTag as exc:
            raise ValueError("密钥解密失败（密文被篡改或主密钥不匹配）") from exc
        return plaintext.decode("utf-8")

    @staticmethod
    def _aad(version: int) -> bytes:
        return f"ttg-provider-key:v1:{version}".encode("ascii")
