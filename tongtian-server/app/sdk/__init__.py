"""通天办公服务端 · 最小对外 SDK（D-19 / #154 刀 C）。

**只用标准库**（urllib + json + hashlib + dataclasses）：第三方装 SDK 不该被拖进
FastAPI / pydantic / jwt 的依赖树，本包也不 import 任何服务端模块——SDK 与契约是否
同源，由 `tongtian-server/tests/test_sdk.py` 交叉比对钉住，不靠这里的注释。

覆盖范围 = `app/api_contract.py` 里承诺稳定的 28 条操作，一条一个方法
（`Client.op_coverage()` 给出 op id → 方法名，自测做集合相等，漏一条就红）。

不含：对话与事件流（引擎进程，非本窗口领地）、真实第三方接入验收（外部条件）、
档位文档的**验签**（`signature` 是 RS256 JWT，验证要 JOSE 库，标准库做不了；
本包只做 `digest` 自洽比对，方法名和文档都按「不是验签」写，不许读成已验签）。
"""

from __future__ import annotations

from .client import (
    CODE_CLASSES,
    CONTRACT_HEADER,
    IDEMPOTENT_OPS,
    IDEMPOTENCY_HEADER,
    MARKET_PLURALS,
    OP_METHODS,
    OP_PATHS,
    REPLAY_HEADER,
    SDK_CONTRACT_VERSIONS,
    SDK_NAME,
    SDK_VERSION,
    ApiError,
    AuthError,
    Client,
    ConflictError,
    ContractMismatch,
    ForbiddenError,
    IdempotencyConflict,
    IdempotencyInProgress,
    NotFoundError,
    RateLimited,
    Result,
    SdkError,
    ServerError,
    TransportError,
    UnknownMarketPlural,
    build_multipart,
    classify,
    make_idempotency_key,
    tier_digest,
    tier_document_bytes,
)

__all__ = [
    "ApiError",
    "AuthError",
    "CODE_CLASSES",
    "CONTRACT_HEADER",
    "Client",
    "ConflictError",
    "ContractMismatch",
    "ForbiddenError",
    "IDEMPOTENT_OPS",
    "IDEMPOTENCY_HEADER",
    "MARKET_PLURALS",
    "OP_METHODS",
    "OP_PATHS",
    "REPLAY_HEADER",
    "RateLimited",
    "Result",
    "SDK_CONTRACT_VERSIONS",
    "SDK_NAME",
    "SDK_VERSION",
    "SdkError",
    "ServerError",
    "TransportError",
    "UnknownMarketPlural",
    "build_multipart",
    "classify",
    "make_idempotency_key",
    "tier_digest",
    "tier_document_bytes",
]
