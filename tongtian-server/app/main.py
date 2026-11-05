"""FastAPI 应用装配：路由、异常格式、CORS、安全响应头与启动检查。"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api_contract import API_CONTRACT_VERSION, CONTRACT_HEADER as API_CONTRACT_HEADER
from .api_contract import router as api_contract_router
from .audit_api import router as audit_router
from .auth import ensure_admin, router as auth_router
from .config import DEV_JWT_SECRET, settings
from .db import init_db
from .desktop_index import router as desktop_index_router
from .errors import ApiError
from .gateway_admin import router as gateway_admin_router
from .gateway_policy import router as gateway_policy_router
from .gateway_usage import router as gateway_usage_router
from .keys import init_keys
from .market import router as market_router
from .model_tiers import router as model_tiers_router
from .org_quota import router as org_quota_router
from .projects import router as projects_router
from .tickets import router as tickets_router
from .usage import router as usage_router
from .workflows import router as workflows_router

logger = logging.getLogger("tongtian")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_DRAIN_BYTES = 8 * 1024 * 1024
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def _configure_logging() -> None:
    if hasattr(sys.stderr, "reconfigure"):
        # basicConfig 的 encoding 只对文件处理器生效，重定向的 stderr 会把中文转义成 \uXXXX
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _configure_logging()
    if settings.production and settings.jwt_secret == DEV_JWT_SECRET:
        raise RuntimeError("生产环境必须配置 TT_SERVER_JWT_SECRET，禁止使用开发默认密钥")
    if settings.production and len(settings.jwt_secret) < 16:
        raise RuntimeError("生产环境 TT_SERVER_JWT_SECRET 至少 16 个字符")
    if settings.jwt_secret == DEV_JWT_SECRET:
        logger.warning("当前使用开发默认 JWT 密钥，部署前请设置 TT_SERVER_JWT_SECRET")
    if settings.production and settings.cors_origins == ("*",):
        logger.warning("生产环境 CORS 仍为 *，建议通过 TT_CORS_ORIGINS 收窄来源")

    init_db()
    store = init_keys()
    logger.info("RS256 签名密钥已就绪：kid=%s", store.current_kid)
    if not settings.gateway_service_token:
        logger.warning("未配置 TT_GATEWAY_SERVICE_TOKEN：网关无法拉取策略")
    if ensure_admin():
        logger.info("管理员账号已就绪：%s", settings.admin_email or "(已存在账号提升)")
    if settings.seed_demo:
        from .seed import seed_demo

        seed_demo()
    yield


# 生产关闭交互文档与 OpenAPI：接口清单不该对公网暴露（排障走 /health 与日志）
_public_docs = not settings.production
app = FastAPI(
    title="通天办公服务端",
    description="账号登录、在线技能市场、专家套件市场与 MCP 插件市场",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if _public_docs else None,
    redoc_url="/redoc" if _public_docs else None,
    openapi_url="/openapi.json" if _public_docs else None,
)

if settings.cors_origins == ("*",):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    content_type = request.headers.get("content-type", "")
    length = request.headers.get("content-length", "")
    if (
        request.method in ("POST", "PUT", "PATCH")
        and not content_type.startswith("multipart/")
        and length.isdigit()
        and int(length) > MAX_JSON_BYTES
    ):
        # multipart 上传由包读取逻辑单独限制；这里挡住超大 JSON/表单请求
        if int(length) <= MAX_DRAIN_BYTES:
            # 未读请求体随连接关闭会在 Windows 上触发 RST，客户端反而收不到 413
            await request.body()
        response = JSONResponse(
            status_code=413,
            content={"ok": False, "code": "PAYLOAD_TOO_LARGE", "message": "请求体超过 2MB 上限"},
            headers={"Connection": "close"},
        )
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
        response.headers.setdefault("Content-Security-Policy", WEB_CSP)
    if request.url.path.startswith(("/api/v1/auth", "/api/auth")):
        response.headers.setdefault("Cache-Control", "no-store")
    if request.url.path.startswith("/api/v1"):
        # 第三方不用先打一次 /api/v1/contract 就知道服务端说的是哪一版对外契约
        response.headers.setdefault(API_CONTRACT_HEADER, API_CONTRACT_VERSION)
    return response


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "code": exc.code, "message": exc.message},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first = errors[0] if errors else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    if first.get("type") == "extra_forbidden":
        message = f"包含未允许的字段：{location}"
    else:
        message = f"参数校验失败：{location} {first.get('msg', '')}".strip()
    return JSONResponse(
        status_code=422,
        content={"ok": False, "code": "VALIDATION_ERROR", "message": message},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException):
    codes = {
        401: "AUTH_REQUIRED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        413: "PAYLOAD_TOO_LARGE",
    }
    detail = exc.detail
    message = detail.get("message") if isinstance(detail, dict) else str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "code": codes.get(exc.status_code, "HTTP_ERROR"),
            "message": message or "请求失败",
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def _unhandled_error(_request: Request, exc: Exception):
    logger.exception("未处理异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "code": "INTERNAL_ERROR", "message": "服务内部错误"},
    )


app.include_router(auth_router)
# 审计查询是控制面自己的只读接口，必须先于网关管理通配代理注册
app.include_router(audit_router)
# 用量上报/查询与项目协作都必须先于市场的 /api/v1/{plural} 通配路由注册，
# 否则会被市场当成未知类型
app.include_router(usage_router)
app.include_router(projects_router)
# 工单层（D-47）的 /api/v1/agent/tickets 同样是控制面自己的路由，
# 必须先于市场的 /api/v1/{plural} 通配注册，否则 "agent" 会被当成未知条目类型
app.include_router(tickets_router)
app.include_router(gateway_policy_router)
# 管理代理必须在策略路由之后：/policies、/policy/{tenant} 是控制面自己的路由，
# 不能被通配代理抢走
app.include_router(gateway_admin_router)
app.include_router(gateway_usage_router)
# 模型档位下发（D-44）也必须先于市场通配路由：/api/v1/model-tiers/effective 是两段路径，
# 会被 /api/v1/{plural}/{item_id} 抢走并把 "effective" 当成条目 ID
app.include_router(model_tiers_router)
# 组织两级配额（D-23）必须先于市场通配路由：/api/v1/admin/orgs 是控制面自己的路由，
# 不能被 /api/v1/{plural}/{item_id} 抢走并把 "orgs" 当成未知条目类型
app.include_router(org_quota_router)
# 桌面端索引适配层（D-07.4）必须先于市场通配路由注册：/api/v1/desktop/revoked.json
# 是两段路径，会被 /api/v1/{plural}/{item_id} 抢走并把 "revoked.json" 当成条目 ID
app.include_router(desktop_index_router)
# 工作流层（D-10）同样必须先于市场通配路由：/api/v1/agent/workflow-runs 是两段路径，
# 会被 /api/v1/{plural}/{item_id} 抢走并把 "workflow-runs" 当成未知条目 ID
app.include_router(workflows_router)
# 对外 API 契约（D-19 / #154）：/api/v1/contract 与 /api/v1/contract/verify 是两段路径，
# 必须先于市场通配注册，否则 "contract" 会被当成未知条目类型（与 #142 那次
# /api/v1/desktop/revoked.json 被吞同类，mock 测不出来，只有真路由表能发现）
app.include_router(api_contract_router)
app.include_router(market_router)

# 轻量 Web 控制台：纯静态资源，直接调用上面的 API
app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="web")


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "tongtian-server",
        "version": __version__,
        "timeMs": int(time.time() * 1000),
    }


@app.get("/")
def root():
    body = {
        "ok": True,
        "service": "通天办公服务端",
        "version": __version__,
        "ui": "/ui/",
        "health": "/health",
    }
    if _public_docs:
        body["docs"] = "/docs"
        body["openapi"] = "/openapi.json"
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
