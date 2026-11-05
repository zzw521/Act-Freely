"""策略文档：本地文件加载与内存快照。"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import CONTRACT_VERSION
from .config import Settings
from .contract import PolicyDocument
from .db import Database
from .policy_client import PolicyClient

logger = logging.getLogger("tongtian.gateway.policy")

DEFAULT_POLICY: dict = {
    "contractVersion": CONTRACT_VERSION,
    "policyVersion": "dev#1",
    "version": "dev#1",
    "tenant": "acme",
    "gateway": {"url": "http://127.0.0.1:8001"},
    "noLog": True,
    "embeddingRoute": "embedding-default",
    "byok": {"allowed": True, "hardQuota": False},
    "models": {
        "azure-gpt-4o": {
            "provider": "azure_openai",
            "upstreamModel": "gpt-4o",
            "capabilities": ["chat", "tools", "vision"],
            "contextWindow": 128000,
            "maxOutputTokens": 16384,
            "priceVersion": "2026-10-01",
            "pricing": {
                "currency": "USD",
                "inputPer1M": "2.50",
                "outputPer1M": "10.00",
                "cachedInputPer1M": "1.25",
                "reasoningAsOutput": True,
            },
        },
        "bailian-qwen-max": {
            "provider": "bailian",
            "upstreamModel": "qwen-max",
            "capabilities": ["chat", "tools"],
            "contextWindow": 32768,
            "maxOutputTokens": 8192,
            "priceVersion": "2026-10-01",
            "pricing": {
                "currency": "USD",
                "inputPer1M": "0.40",
                "outputPer1M": "1.20",
                "reasoningAsOutput": True,
            },
        },
        "azure-text-embedding-3-small": {
            "provider": "azure_openai",
            "upstreamModel": "text-embedding-3-small",
            "capabilities": ["embeddings"],
            "contextWindow": 8191,
            "priceVersion": "2026-10-01",
            "pricing": {
                "currency": "USD",
                "inputPer1M": "0.02",
                "outputPer1M": "0",
                "reasoningAsOutput": False,
            },
        },
    },
    "routes": {
        "auto": {"chain": ["azure-gpt-4o", "bailian-qwen-max"], "fallbackAllowed": True},
        "advanced": {"chain": ["azure-gpt-4o"], "fallbackAllowed": False},
        "basic": {"chain": ["bailian-qwen-max"], "fallbackAllowed": True},
        "economy": {"chain": ["bailian-qwen-max"], "fallbackAllowed": False},
        "embedding-default": {
            "chain": ["azure-text-embedding-3-small"],
            "fallbackAllowed": False,
        },
    },
    "allowedModels": [
        "auto",
        "advanced",
        "basic",
        "economy",
        "embedding-default",
        "azure-gpt-4o",
        "bailian-qwen-max",
        "azure-text-embedding-3-small",
    ],
    "quotaDefaults": {
        "monthlyTokens": 2000000,
        "concurrency": 4,
        "rpm": 60,
        "tpm": 60000,
        "maxOutputTokens": 8192,
    },
    "departments": {
        "dept-sales": {
            "allowedModels": [
                "auto",
                "basic",
                "embedding-default",
                "bailian-qwen-max",
                "azure-text-embedding-3-small",
            ],
            "quota": {
                "monthlyTokens": 500000,
                "concurrency": 2,
                "maxOutputTokens": 2048,
            },
            "byokAllowed": True,
        },
        "dept-eng": {
            "allowedModels": [
                "auto",
                "advanced",
                "basic",
                "economy",
                "embedding-default",
                "azure-gpt-4o",
                "bailian-qwen-max",
                "azure-text-embedding-3-small",
            ],
            "quota": {"monthlyTokens": 5000000, "concurrency": 8},
        },
    },
    "issuedAt": "2026-10-30T00:00:00Z",
    "expiresAt": "2027-10-30T00:00:00Z",
}


@dataclass(frozen=True)
class PolicySnapshot:
    document: dict
    policy_version: str | None
    stale: bool
    loaded_at: float


class PolicyStore:
    """控制面模式按租户拉取；未配置控制面时读本地文件或内置默认策略。"""

    def __init__(
        self, settings: Settings, db: Database | None = None, shared=None
    ) -> None:
        self._settings = settings
        self._document = self._load(settings.policy_file)
        self._loaded_at = time.time()
        self._client: PolicyClient | None = None
        if settings.control_plane_url and settings.service_token:
            self._client = PolicyClient(
                settings, db or Database(settings.db_path), shared=shared
            )

    def _load(self, path: Path | None) -> dict:
        if path is None:
            return DEFAULT_POLICY
        if not path.is_file():
            raise RuntimeError(f"策略文件不存在：{path}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"策略文件无法读取：{path}（{exc}）") from exc
        return self._validate(document)

    @staticmethod
    def _validate(document: dict) -> dict:
        PolicyDocument.model_validate(document)
        return document

    def snapshot(self, tenant: str | None = None) -> PolicySnapshot:
        if self._client is not None and tenant:
            state = self._client.get(tenant)
            return PolicySnapshot(
                document=state.document,
                policy_version=state.policy_version,
                stale=state.stale,
                loaded_at=state.loaded_at,
            )
        return PolicySnapshot(
            document=self._document,
            policy_version=self._document.get("policyVersion"),
            stale=False,
            loaded_at=self._loaded_at,
        )

    def replace(self, document: dict) -> None:
        self._document = self._validate(document)
        self._loaded_at = time.time()

    def invalidate(self, tenant: str) -> None:
        """共享状态失效通知：清掉本 worker 的内存缓存，下个请求重读共享 SQLite。"""
        if self._client is None:
            return
        self._client.invalidate(None if tenant == "*" else tenant)
