"""统一错误码与三种协议的错误信封。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    message: str
    retryable: bool


ERROR_SPECS: dict[str, ErrorSpec] = {
    "AUTH_REQUIRED": ErrorSpec(401, "缺少或无效的访问凭据", False),
    "MTLS_REQUIRED": ErrorSpec(401, "管理面要求 mTLS 客户端证书", False),
    "TOKEN_EXPIRED": ErrorSpec(401, "访问令牌已过期", False),
    "FORBIDDEN": ErrorSpec(403, "无权访问该资源", False),
    "MODEL_NOT_ALLOWED": ErrorSpec(403, "模型不在当前策略允许范围内", False),
    "INVALID_REQUEST": ErrorSpec(400, "请求格式或参数错误", False),
    "PAYLOAD_TOO_LARGE": ErrorSpec(413, "请求体超过上限", False),
    "QUOTA_TOKENS_EXCEEDED": ErrorSpec(429, "当月 token 配额不足", False),
    "QUOTA_CONCURRENCY_EXCEEDED": ErrorSpec(429, "并发请求数超限", True),
    "RATE_LIMITED": ErrorSpec(429, "请求速率超限", True),
    "POLICY_STALE": ErrorSpec(503, "策略已过期且控制面不可达", True),
    "AUTH_UNAVAILABLE": ErrorSpec(503, "认证组件不可用", True),
    "SHARED_STATE_UNAVAILABLE": ErrorSpec(503, "共享状态不可用，暂时无法安全计数", True),
    "PROVIDER_UNAVAILABLE": ErrorSpec(503, "上游模型服务不可用", True),
    "PROVIDER_AUTH_FAILED": ErrorSpec(502, "上游模型服务鉴权失败", False),
    "UPSTREAM_RATE_LIMITED": ErrorSpec(429, "上游模型服务限流", True),
    "UPSTREAM_TIMEOUT": ErrorSpec(504, "上游模型服务超时", True),
    "UPSTREAM_ERROR": ErrorSpec(502, "上游模型服务返回错误", False),
    "CLIENT_CANCELLED": ErrorSpec(499, "客户端已取消请求", False),
    "NOT_IMPLEMENTED": ErrorSpec(501, "该能力尚未实现", False),
    "INTERNAL_ERROR": ErrorSpec(500, "网关内部错误", True),
}

_OPENAI_TYPES: dict[str, str] = {
    "AUTH_REQUIRED": "authentication_error",
    "MTLS_REQUIRED": "authentication_error",
    "TOKEN_EXPIRED": "authentication_error",
    "FORBIDDEN": "permission_error",
    "MODEL_NOT_ALLOWED": "permission_error",
    "INVALID_REQUEST": "invalid_request_error",
    "PAYLOAD_TOO_LARGE": "invalid_request_error",
    "QUOTA_TOKENS_EXCEEDED": "rate_limit_error",
    "QUOTA_CONCURRENCY_EXCEEDED": "rate_limit_error",
    "RATE_LIMITED": "rate_limit_error",
    "PROVIDER_UNAVAILABLE": "api_error",
    "POLICY_STALE": "api_error",
    "AUTH_UNAVAILABLE": "api_error",
    "SHARED_STATE_UNAVAILABLE": "api_error",
    "PROVIDER_AUTH_FAILED": "api_error",
    "UPSTREAM_RATE_LIMITED": "rate_limit_error",
    "UPSTREAM_TIMEOUT": "api_error",
    "UPSTREAM_ERROR": "api_error",
    "CLIENT_CANCELLED": "api_error",
    "NOT_IMPLEMENTED": "invalid_request_error",
    "INTERNAL_ERROR": "api_error",
}

_ANTHROPIC_TYPES: dict[str, str] = {
    "AUTH_REQUIRED": "authentication_error",
    "MTLS_REQUIRED": "authentication_error",
    "TOKEN_EXPIRED": "authentication_error",
    "FORBIDDEN": "permission_error",
    "MODEL_NOT_ALLOWED": "permission_error",
    "INVALID_REQUEST": "invalid_request_error",
    "PAYLOAD_TOO_LARGE": "invalid_request_error",
    "QUOTA_TOKENS_EXCEEDED": "rate_limit_error",
    "QUOTA_CONCURRENCY_EXCEEDED": "rate_limit_error",
    "RATE_LIMITED": "rate_limit_error",
    "PROVIDER_UNAVAILABLE": "overloaded_error",
    "POLICY_STALE": "overloaded_error",
    "AUTH_UNAVAILABLE": "overloaded_error",
    "SHARED_STATE_UNAVAILABLE": "overloaded_error",
    "PROVIDER_AUTH_FAILED": "api_error",
    "UPSTREAM_RATE_LIMITED": "rate_limit_error",
    "UPSTREAM_TIMEOUT": "api_error",
    "UPSTREAM_ERROR": "api_error",
    "CLIENT_CANCELLED": "api_error",
    "NOT_IMPLEMENTED": "invalid_request_error",
    "INTERNAL_ERROR": "api_error",
}

_GEMINI_STATUS: dict[str, str] = {
    "AUTH_REQUIRED": "UNAUTHENTICATED",
    "MTLS_REQUIRED": "UNAUTHENTICATED",
    "TOKEN_EXPIRED": "UNAUTHENTICATED",
    "FORBIDDEN": "PERMISSION_DENIED",
    "MODEL_NOT_ALLOWED": "PERMISSION_DENIED",
    "INVALID_REQUEST": "INVALID_ARGUMENT",
    "PAYLOAD_TOO_LARGE": "INVALID_ARGUMENT",
    "QUOTA_TOKENS_EXCEEDED": "RESOURCE_EXHAUSTED",
    "QUOTA_CONCURRENCY_EXCEEDED": "RESOURCE_EXHAUSTED",
    "RATE_LIMITED": "RESOURCE_EXHAUSTED",
    "PROVIDER_UNAVAILABLE": "UNAVAILABLE",
    "POLICY_STALE": "UNAVAILABLE",
    "AUTH_UNAVAILABLE": "UNAVAILABLE",
    "SHARED_STATE_UNAVAILABLE": "UNAVAILABLE",
    "PROVIDER_AUTH_FAILED": "INTERNAL",
    "UPSTREAM_RATE_LIMITED": "RESOURCE_EXHAUSTED",
    "UPSTREAM_TIMEOUT": "DEADLINE_EXCEEDED",
    "UPSTREAM_ERROR": "INTERNAL",
    "CLIENT_CANCELLED": "CANCELLED",
    "NOT_IMPLEMENTED": "UNIMPLEMENTED",
    "INTERNAL_ERROR": "INTERNAL",
}


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status: int | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        spec = ERROR_SPECS.get(code)
        if spec is None:
            raise ValueError(f"未知错误码：{code}")
        super().__init__(message or spec.message)
        self.code = code
        self.status = status or spec.status
        self.message = message or spec.message
        self.retryable = spec.retryable
        self.param = param
        self.headers = headers

    @classmethod
    def of(cls, code: str, message: str | None = None, **kwargs) -> "GatewayError":
        return cls(code, message, **kwargs)


def protocol_for_path(path: str) -> str:
    if path.startswith("/gateway/v1/admin"):
        return "admin"
    if path == "/v1beta" or path.startswith("/v1beta/"):
        return "gemini"
    if path == "/v1/messages" or path.startswith("/v1/messages/"):
        return "anthropic"
    return "openai"


def envelope(protocol: str, code: str, message: str, param: str | None = None) -> dict:
    spec = ERROR_SPECS[code]
    if protocol == "admin":
        return {"ok": False, "code": code, "message": message}
    if protocol == "anthropic":
        return {
            "type": "error",
            "error": {
                "type": _ANTHROPIC_TYPES[code],
                "message": message,
                "tt_code": code,
            },
        }
    if protocol == "gemini":
        return {
            "error": {
                "code": spec.status,
                "message": message,
                "status": _GEMINI_STATUS[code],
                "details": [
                    {
                        "@type": "type.googleapis.com/tongtian.GatewayError",
                        "code": code,
                    }
                ],
            }
        }
    return {
        "error": {
            "message": message,
            "type": _OPENAI_TYPES[code],
            "param": param,
            "code": code,
        }
    }


def envelope_for_path(
    path: str, code: str, message: str, param: str | None = None
) -> dict:
    return envelope(protocol_for_path(path), code, message, param)
