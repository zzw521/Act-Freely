"""FastAPI 应用装配：路由、异常格式、CORS、安全响应头与启动检查。"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .auth import ensure_admin, router as auth_router
from .config import DEV_JWT_SECRET, settings
from .db import init_db
from .errors import ApiError
from .market import router as market_router
from .projects import router as projects_router

logger = logging.getLogger("tongtian")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_DRAIN_BYTES = 8 * 1024 * 1024


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
    if ensure_admin():
        logger.info("管理员账号已就绪：%s", settings.admin_email or "(已存在账号提升)")
    if settings.seed_demo:
        from .seed import seed_demo

        seed_demo()
    yield


app = FastAPI(
    title="通天办公服务端",
    description="账号登录、在线技能市场、专家套件市场与 MCP 插件市场",
    version=__version__,
    lifespan=lifespan,
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
    if request.url.path.startswith(("/api/v1/auth", "/api/auth")):
        response.headers.setdefault("Cache-Control", "no-store")
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
# 项目协作必须先于市场的 /api/v1/{plural} 通配路由注册，否则会被市场当成未知类型
app.include_router(projects_router)
app.include_router(market_router)


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
    return {
        "ok": True,
        "service": "通天办公服务端",
        "version": __version__,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
