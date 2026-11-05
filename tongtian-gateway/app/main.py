"""FastAPI 应用装配：健康、能力发现、协议面、管理 API 与错误信封。"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import CONTRACT_VERSION, __version__
from .adapters import warm_litellm
from .admin import router as admin_router
from .config import settings
from .contract import (
    Capabilities,
    GatewayFeatures,
    GatewayLimits,
    ProtocolSurface,
)
from .errors import GatewayError, envelope_for_path, protocol_for_path
from .protocols import anthropic_router, gemini_router, openai_router
from .providers import provider_ids
from .quota import reset_at
from .startup import enforce_ledger, enforce_startup
from .shared_state import SharedStateError
from .state import (
    audit_webhook,
    db,
    policy_store,
    secrets,
    shared_state,
    usage_uploader,
)

logger = logging.getLogger("tongtian.gateway")

PROTOCOL_PREFIXES = ("/v1/", "/v1beta/")


def _configure_logging() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        # 重定向的 stderr 默认会把中文转义，和日志格式对不上
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    report = enforce_startup(settings, secrets)
    for warning in report.warnings:
        logger.warning(warning)
    ledger = enforce_ledger(db, settings)
    logger.info(
        "账本已校验：backend=%s schemaVersion=%s",
        ledger["backend"],
        ledger["schemaVersion"],
    )
    if shared_state.enabled:
        try:
            shared_state.ping()
        except SharedStateError as exc:
            raise RuntimeError(
                "无法连接 Redis/Sentinel，共享状态不可用，拒绝启动"
            ) from exc
        shared_state.start_policy_listener(policy_store.invalidate)
        logger.info(
            "共享状态已启用：mode=%s tls=%s prefix=%s workers=%s",
            shared_state.mode,
            settings.redis_tls,
            settings.redis_prefix,
            settings.workers,
        )
    else:
        logger.info("共享状态：memory（单 worker）")
    warmed_ms = await warm_litellm()
    logger.info("LiteLLM 预热完成：%.0fms", warmed_ms)
    snapshot = policy_store.snapshot()
    logger.info(
        "网关已就绪：id=%s tenant=%s policy=%s db=%s",
        settings.gateway_id,
        snapshot.document.get("tenant"),
        snapshot.policy_version,
        db.path if db.dialect == "sqlite" else db.dialect,
    )
    if usage_uploader.enabled:
        usage_uploader.start()
    else:
        logger.warning("未配置控制面地址或服务令牌：用量只在本地账本累计，不上报")
    if audit_webhook.enabled:
        audit_webhook.start()
        logger.info("审计外送已启用：%s", settings.audit_webhook_url)
    yield
    shared_state.stop()
    audit_webhook.stop()
    usage_uploader.stop()
    db.close()


app = FastAPI(
    title="通天办公模型网关",
    description="OpenAI / Anthropic / Gemini 协议面与组织级模型策略",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)


def _is_protocol_path(path: str) -> bool:
    return path.startswith(PROTOCOL_PREFIXES)


@app.middleware("http")
async def _request_context(request: Request, call_next):
    request_id = request.headers.get("x-tt-request-id") or f"req_{uuid.uuid4().hex}"
    request.state.request_id = request_id
    path = request.url.path
    length = request.headers.get("content-length", "")
    write_method = request.method in ("POST", "PUT", "PATCH")
    if write_method and request.headers.get("transfer-encoding", "").strip():
        # 分块传输没有长度上限，无法按 Content-Length 预判体积，直接拒绝
        response = JSONResponse(
            status_code=413,
            content=envelope_for_path(
                path, "PAYLOAD_TOO_LARGE", "请求必须带 Content-Length，分块传输不被接受"
            ),
            headers={"Connection": "close"},
        )
    elif write_method and length.isdigit() and int(length) > settings.max_request_bytes:
        if int(length) <= settings.max_request_bytes * 4:
            # 未读完的请求体在 Windows 上会触发 RST，客户端反而收不到 413
            await request.body()
        response = JSONResponse(
            status_code=413,
            content=envelope_for_path(
                path,
                "PAYLOAD_TOO_LARGE",
                f"请求体超过 {settings.max_request_bytes} 字节上限",
            ),
            headers={"Connection": "close"},
        )
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if getattr(request.state, "policy_stale", False):
        response.headers.setdefault("X-TT-Policy-Stale", "1")
    if _is_protocol_path(path):
        response.headers.setdefault("X-TT-Request-Id", request_id)
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(GatewayError)
async def _gateway_error(request: Request, exc: GatewayError):
    path = request.url.path
    headers = dict(exc.headers or {})
    if exc.code in ("QUOTA_TOKENS_EXCEEDED", "QUOTA_CONCURRENCY_EXCEEDED"):
        headers.setdefault("X-TT-Quota-Reset", reset_at())
    if _is_protocol_path(path):
        headers.setdefault(
            "X-TT-Request-Id", getattr(request.state, "request_id", "")
        )
    return JSONResponse(
        status_code=exc.status,
        content=envelope_for_path(path, exc.code, exc.message, exc.param),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first = errors[0] if errors else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = f"参数校验失败：{location} {first.get('msg', '')}".strip()
    return JSONResponse(
        status_code=400,
        content=envelope_for_path(request.url.path, "INVALID_REQUEST", message),
    )


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    logger.exception("未处理异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content=envelope_for_path(request.url.path, "INTERNAL_ERROR", "网关内部错误"),
    )


def build_capabilities() -> Capabilities:
    snapshot = policy_store.snapshot()
    return Capabilities(
        contractVersion=CONTRACT_VERSION,
        gatewayVersion=__version__,
        gatewayId=settings.gateway_id,
        protocols=[
            ProtocolSurface(
                name="openai",
                endpoints=["/v1/chat/completions", "/v1/embeddings", "/v1/models"],
                stream=True,
                tools=True,
                vision=True,
                embeddings=True,
                testedAt="2026-10-30",
                note="M1.1：OpenAI 兼容上游、流式、工具、视觉与嵌入",
            ),
            ProtocolSurface(
                name="anthropic",
                endpoints=["/v1/messages", "/v1/messages/count_tokens"],
                stream=True,
                tools=True,
                vision=True,
                embeddings=False,
                testedAt="2026-10-30",
                note="M1.2：Anthropic Messages 翻译、工具调用与流式事件",
            ),
            ProtocolSurface(
                name="gemini",
                endpoints=[
                    "/v1beta/models/{model}:generateContent",
                    "/v1beta/models/{model}:streamGenerateContent",
                    "/v1beta/models/{model}:countTokens",
                    "/v1beta/models",
                ],
                stream=True,
                tools=True,
                vision=True,
                embeddings=False,
                testedAt="2026-10-30",
                note="M1.2：Gemini contents/parts 翻译、函数调用与流式帧",
            ),
        ],
        features=GatewayFeatures(
            stream=True,
            tools=True,
            vision=True,
            embeddings=True,
            countTokens=True,
            promptCaching=False,
        ),
        providers=provider_ids(),
        limits=GatewayLimits(
            maxRequestBytes=settings.max_request_bytes,
            maxOutputTokens=settings.max_output_tokens,
            upstreamTimeoutSeconds=settings.upstream_timeout_seconds,
            streamIdleTimeoutSeconds=settings.stream_idle_timeout_seconds,
        ),
        policyVersion=snapshot.policy_version,
    )


@app.get("/gateway/v1/capabilities")
def capabilities() -> dict:
    return build_capabilities().model_dump()


@app.get("/health")
def health() -> dict:
    snapshot = policy_store.snapshot()
    return {
        "ok": True,
        "service": "tongtian-gateway",
        "version": __version__,
        "gatewayId": settings.gateway_id,
        "contractVersion": CONTRACT_VERSION,
        "policyVersion": snapshot.policy_version,
        "timeMs": int(time.time() * 1000),
    }


@app.get("/")
def root() -> dict:
    body = {
        "ok": True,
        "service": "通天办公模型网关",
        "version": __version__,
        "contractVersion": CONTRACT_VERSION,
        "health": "/health",
        "capabilities": "/gateway/v1/capabilities",
    }
    if settings.docs_enabled:
        body["docs"] = "/docs"
    return body


app.include_router(admin_router)
app.include_router(openai_router)
app.include_router(anthropic_router)
app.include_router(gemini_router)


if __name__ == "__main__":
    import uvicorn

    if settings.workers > 1:
        # 多 worker 必须用 import string，uvicorn 才能 fork/spawn 子进程
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            log_level="info",
            workers=settings.workers,
            proxy_headers=False,
        )
    else:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level="info",
            # mTLS 信任边界依赖对端地址：不能让客户端用 X-Forwarded-For 改写 request.client
            proxy_headers=False,
        )
