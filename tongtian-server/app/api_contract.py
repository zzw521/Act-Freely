"""面向第三方集成的稳定 API 契约（D-19 / #154 第一刀）。

这块解决的是「服务端有 120 多个路由，但没人能据此集成」：
- `/docs`、`/openapi.json` 在生产里是**关掉**的（`main.py` 的 `_public_docs`），
  OpenAPI 只能当开发态调试面，不能当交付给第三方的契约；
- 网关那边早有 `gateway-contract.v1` 的成文契约（`tongtian-gateway/CONTRACT.md`），
  控制面这边一个对应的东西都没有。

所以本模块把「对外承诺稳定」的那批操作写死成一份**可机器校验的清单**，而不是文档里
一段人写的描述：清单里的每条路径都要能在**活路由表**里找到，每个错误码都要能在
`app/` 源码里以 `ApiError(<状态>, "<码>")` 的形式找到，每条速率限制都要能对着
`app/*.py` 里的真实 `.check(...)` 调用核上。清单与实现一旦漂移，`/api/v1/contract/verify`
直接报问题，自测族也会红——契约说谎比没有契约更糟。

**本契约不覆盖的**（见 `COVERAGE_GAPS`，都是实情，别当成已交付）：
`/api/chat` 与对话事件流在引擎进程里（`tongtian-tauri/engine/server.py`，不在本窗口领地），
本契约只管控制面 HTTP 面；真实第三方按文档集成也**尚未验收**。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request

from .audit_compliance import REPORT_CONTRACT_VERSION
from .catalog import PLURAL_TO_TYPE, TYPE_SPECS
from .cli import BACKUP_CONTRACT_VERSION, CLI_CONTRACT_VERSION
from .config import settings
from .idempotency import (
    DEFAULT_TTL_MS,
    IDEMPOTENCY_HEADER,
    LEASE_MS,
    MAX_STORED_RESPONSE_BYTES,
    REPLAY_HEADER,
    SUPPORTED_SCOPES,
)
from .model_tiers import TIER_CONTRACT_VERSION
from .org_quota import ORG_QUOTA_CONTRACT
from .org_quota import PERIODS as ORG_QUOTA_PERIODS
from .usage import PERIODS as USAGE_PERIODS

API_CONTRACT_VERSION = "tt-server-api.v1"

#: 本契约的 HTTP 头名：任何 /api/v1 响应都带，客户端可据此判断「服务端说的是哪一版契约」，
#: 不用先打一次 /api/v1/contract 才知道。
CONTRACT_HEADER = "X-TT-Api-Contract"

router = APIRouter()

#: 子契约家族：一个版本号只管一片，加法修订不回头改老语义。
CONTRACT_FAMILIES: dict[str, str] = {
    "api": API_CONTRACT_VERSION,
    "cli": CLI_CONTRACT_VERSION,
    "backup": BACKUP_CONTRACT_VERSION,
    "modelTiers": TIER_CONTRACT_VERSION,
    "auditCompliance": REPORT_CONTRACT_VERSION,
    "orgQuota": ORG_QUOTA_CONTRACT,
    # 网关数据面的权威是 tongtian-gateway/CONTRACT.md，这里只登记版本号，别把它当本模块维护
    "gateway": "gateway-contract.v1",
}

#: 响应与请求的统一约定（第三方最先要问的几件事，逐条都能在源码里指到）。
CONVENTIONS: dict[str, Any] = {
    "transport": "HTTPS（本仓库自测跑明文 http://127.0.0.1；对外部署必须 TLS）",
    "encoding": "请求与响应一律 UTF-8 JSON；Content-Type: application/json",
    "authHeader": "Authorization: Bearer <accessToken>（RS256 JWT，公钥走 /.well-known/jwks.json）",
    "errorEnvelope": '错误响应统一 {"ok": false, "code": "<机器码>", "message": "<中文说明>"}',
    "successEnvelope": (
        "控制面的 JSON 响应统一带 ok: true；例外只有下载类（返回二进制包体，没有信封）"
        "与 304 条件命中（无响应体）。即便如此，客户端也应以 HTTP 状态码判定成败。"
    ),
    "requestLimits": {
        "jsonMaxBytes": 2 * 1024 * 1024,
        "drainMaxBytes": 8 * 1024 * 1024,
        "tooLarge": "413 PAYLOAD_TOO_LARGE（超过 2MB 直接拒；2–8MB 先读完再拒，避免 Windows 上 RST 吞掉响应）",
    },
    "securityHeaders": ["X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer"],
    "cachePolicy": "/api/v1/auth* 与 /api/auth* 响应带 Cache-Control: no-store",
    "tokens": {
        "accessTtlSeconds": settings.access_ttl_seconds,
        "refreshTtlSeconds": settings.refresh_ttl_seconds,
        "gatewayTokenTtlSeconds": settings.gateway_token_ttl_seconds,
        "algorithm": "RS256",
        "jwksPath": "/.well-known/jwks.json",
        "revocationList": "/api/v1/revoked",
        "rotation": "refresh 一次性：换新令牌后旧 refresh 立刻失效",
    },
    "pagination": (
        "列表类走 ?page=（1 起）与 ?pageSize=（上限 100，超出按 100 收）；"
        "?sort= 只接受该操作声明过的值，非法值报 INVALID_SORT"
    ),
    "conditionalGet": "档位表与缓存型只读接口支持 ETag / If-None-Match，命中返回 304 空响应体",
    "clockSkew": "令牌 exp 按服务端时钟判定；客户端不要把自己的时间当准绳",
    "periods": {
        "values": list(USAGE_PERIODS),
        "note": "?period= 只接受这三个值；用量报表与组织预算各自有一份 PERIODS，"
        "两处不一致时 /api/v1/contract/verify 会报问题",
    },
}

#: 鉴权模式。authSource 由路由表的 Depends 名称机器判定；判定不到就是 handler，
#: 意味着「这个端点在函数体里手动校验」，清单不能假装它被机器盯着。
AUTH_MODES: dict[str, str] = {
    "none": "无需凭据",
    "bearer": "需访问令牌（require_user）",
    "admin": "需管理员令牌（require_admin）",
    "service": "需服务令牌或管理员令牌（函数体内校验，不在 Depends 里）",
}

#: 错误码：只列第三方必须处理的稳定子集。verify 会逐个回源码找 `ApiError(<状态>, "<码>")`，
#: 列了一个源码里已经不存在的码＝契约在说谎。
ERROR_CODES: tuple[dict[str, Any], ...] = (
    {"code": "INVALID_JSON", "status": 400, "meaning": "请求体不是合法 JSON"},
    {"code": "INVALID_EMAIL", "status": 400, "meaning": "邮箱格式不合法"},
    {"code": "WEAK_PASSWORD", "status": 400, "meaning": "密码强度不足"},
    {"code": "INVALID_ORG", "status": 400, "meaning": "组织标识不合法（先规范化再校验字符集）"},
    {"code": "INVALID_QUOTA", "status": 400, "meaning": "配额值超出允许区间"},
    {"code": "USAGE_INVALID_ROW", "status": 400, "meaning": "用量明细行字段不合法"},
    {"code": "USAGE_INVALID_SOURCE", "status": 400, "meaning": "用量来源不在允许集合内"},
    {"code": "USAGE_TOO_MANY_ROWS", "status": 400, "meaning": "单次上报行数超限"},
    {"code": "MARKET_NOT_FOUND", "status": 404, "meaning": "市场类型/条目不存在（含未知 /api/v1/{plural}）"},
    {"code": "ORG_NOT_FOUND", "status": 404, "meaning": "组织不存在"},
    {"code": "USER_NOT_FOUND", "status": 404, "meaning": "用户不存在"},
    {"code": "TIER_TABLE_NOT_FOUND", "status": 404, "meaning": "该范围没有档位表"},
    {"code": "AUTH_REQUIRED", "status": 401, "meaning": "缺少或无法校验凭据"},
    {"code": "INVALID_CREDENTIALS", "status": 401, "meaning": "账号或密码不对"},
    {"code": "TOKEN_EXPIRED", "status": 401, "meaning": "令牌过期，用 refresh 换新的"},
    {"code": "INVALID_REFRESH", "status": 401, "meaning": "refresh 令牌无效或已轮换掉"},
    {"code": "FORBIDDEN", "status": 403, "meaning": "凭据有效但无权做这件事"},
    {"code": "REGISTRATION_DISABLED", "status": 403, "meaning": "服务端关闭了自助注册"},
    {"code": "ORG_BUDGET_EXCEEDED", "status": 403, "meaning": "组织预算已用满，拒绝签发新令牌"},
    {"code": "USAGE_SCOPE_DENIED", "status": 403, "meaning": "越权查别人的用量"},
    {"code": "EMAIL_TAKEN", "status": 409, "meaning": "邮箱已注册"},
    # 下面三条是市场发布路上真正会撞的码（刀 C 写 SDK 时才发现清单漏了它们：
    # SDK 要按码分异常类，而分错类的根源是清单没登记 ⇒ 补齐，并让 SDK 的映射逐条回比状态码）。
    {"code": "ITEM_EXISTS", "status": 409,
     "meaning": "同名条目已存在；发新版本走 /api/v1/{plural}/{item_id}/versions，"
     "重试同一件事请带 Idempotency-Key（不带键重发必然撞这一条）"},
    {"code": "ITEM_NOT_FOUND", "status": 404, "meaning": "发布新版本时路径里的条目不存在"},
    {"code": "PACKAGE_ID_MISMATCH", "status": 400,
     "meaning": "包里声明的条目标识与路径里的不一致（发新版本时包与条目必须是同一个）"},
    {"code": "RATE_LIMITED", "status": 429, "meaning": "触发速率限制，按 Retry-After 秒数退避"},
    {"code": "IDEMPOTENCY_KEY_INVALID", "status": 400,
     "meaning": "Idempotency-Key 形状不合法（8–200 位字母/数字/点/下划线/短横线）"},
    {"code": "IDEMPOTENCY_SCOPE_UNSUPPORTED", "status": 400,
     "meaning": "该操作未接入幂等键（清单没承诺过重放的就是没做，别指望重试安全）"},
    {"code": "IDEMPOTENCY_CONFLICT", "status": 409,
     "meaning": "同一把键 + 不同请求体：键是「这一件事」的名字，做另一件事请换一把键"},
    {"code": "IDEMPOTENCY_IN_PROGRESS", "status": 409,
     "meaning": "同一把键的上一次还在执行中（带 Retry-After，租约最长 60 秒），不并发跑第二遍"},
    {"code": "PAYLOAD_TOO_LARGE", "status": 413, "meaning": "请求体超限"},
    {"code": "VALIDATION_ERROR", "status": 422, "meaning": "字段校验失败（含多传了不允许的字段）"},
    {"code": "AUTH_UNAVAILABLE", "status": 503, "meaning": "鉴权依赖的密钥/存储不可用"},
    {"code": "INTERNAL_ERROR", "status": 500, "meaning": "未预期错误；message 已脱敏，别指望它稳定"},
)

#: 速率限制：全部从 `app/*.py` 里真实的 `.check()` 调用抄来，verify 反向核对。
#: keyTemplate 写成源码里的字面样子，改前缀或改数字都会让校验失败。
RATE_LIMITS: tuple[dict[str, Any], ...] = (
    {
        "id": "login-ip",
        "module": "auth",
        "keyTemplate": 'f"login:ip:{ip}", 50, 300',
        "appliesTo": "POST /api/v1/auth/login",
        "dimension": "来源 IP",
        "limit": 50,
        "windowSeconds": 300,
    },
    {
        "id": "login-ip-account",
        "module": "auth",
        "keyTemplate": 'f"login:{ip}:{account}", 10, 300',
        "appliesTo": "POST /api/v1/auth/login",
        "dimension": "来源 IP + 账号",
        "limit": 10,
        "windowSeconds": 300,
    },
    {
        "id": "register-ip",
        "module": "auth",
        "keyTemplate": 'f"register:{_client_ip(request)}", 20, 3600',
        "appliesTo": "POST /api/v1/auth/register",
        "dimension": "来源 IP",
        "limit": 20,
        "windowSeconds": 3600,
    },
    {
        "id": "gateway-token-ip",
        "module": "auth",
        "keyTemplate": 'f"gateway-token:ip:{ip}", 120, 60',
        "appliesTo": "POST /api/v1/auth/gateway-token",
        "dimension": "来源 IP",
        "limit": 120,
        "windowSeconds": 60,
    },
    {
        "id": "gateway-token-user",
        "module": "auth",
        "keyTemplate": 'f"gateway-token:{ip}:{uid}", 60, 60',
        "appliesTo": "POST /api/v1/auth/gateway-token",
        "dimension": "来源 IP + 用户",
        "limit": 60,
        "windowSeconds": 60,
    },
)

#: 幂等键（D-19 刀 B）：从「清单里说说」升级为「服务端真挡得住」。
#: 支持集合**不在这里手抄**，直接取自 `idempotency.SUPPORTED_SCOPES`——清单与实现同源，
#: 接了哪个端点清单就自动多哪条，漏改不可能（`find_problems()` 还会双向核对 op id 与
#: method/path 是否一致）。
IDEMPOTENCY: dict[str, Any] = {
    "header": IDEMPOTENCY_HEADER,
    "replayHeader": REPLAY_HEADER,
    "status": "enforced",
    "ttlMs": DEFAULT_TTL_MS,
    "inProgressLeaseMs": LEASE_MS,
    "maxStoredResponseBytes": MAX_STORED_RESPONSE_BYTES,
    "keyShape": "^[A-Za-z0-9._-]{8,200}$",
    "supportedOperations": sorted(SUPPORTED_SCOPES),
    "semantics": (
        "带 " + IDEMPOTENCY_HEADER + " 的写请求：同一把键 + 同一份请求体只执行一次，"
        "重试拿第一次的回执（顶层键与状态码与原件一字不差），响应头 "
        + REPLAY_HEADER + ": true 标明这是重放。"
        "同键不同请求体 = 409 IDEMPOTENCY_CONFLICT；同键且上一次还在跑 = 409 "
        "IDEMPOTENCY_IN_PROGRESS（带 Retry-After，租约 "
        + str(LEASE_MS // 1000) + " 秒，进程被杀也不会永久占键）；"
        "键形状不合法 = 400 IDEMPOTENCY_KEY_INVALID。"
        "**不带这个头的请求逐字节走原路径**，行为与接键之前完全一致。"
        "四条要说在前面的口径：① 业务失败不占键（不落缓存、当场释放预留），重试是真的重跑，"
        "别把幂等键当「上次也报错」的保证；② 回执原文落 SQLite 明文字段并到期删除，"
        "因此**签发令牌的路径（register / login / refresh / logout）有意不接**——"
        "不拿凭据换重放便利；③ 回执超过 "
        + str(MAX_STORED_RESPONSE_BYTES // 1024) + "KB 不缓存，同键重试改为真的重跑"
        "（宁可重跑也不装作重放）；④ 键的作用域是 (操作, 调用者, 键)，"
        "匿名端点上键是全局的，别把「换个账号复用同一把键」当成安全。"
    ),
}

#: 自测怎么打这个端点。写操作只有在临时数据根里跑得安全时才给 *-flow。
#: 这里**没有**「只声明不真打」这一档：清单里每条操作都必须落在某个流程里被打过，
#: 想新增一条没得打的，得先在这里加一个真的有探针兜着的模式（否则自检直接报未知模式）。
PROBE_MODES = frozenset(
    {
        "anon",
        "user",
        "admin",
        "auth-flow",
        "usage-flow",
        "org-flow",
        "market-flow",
        "binary",
    }
)

#: 承诺稳定的操作清单。
#: 判定方式：method+path 必须原样出现在活路由表里；声明的 auth 若路由挂了
#: require_user/require_admin，必须由 Depends 判定一致；声明的 errors 必须能在源码找到；
#: response 列的是**实测顶层键**——自测会真打一次这些端点做集合相等比对，
#: 键名写错或实现改了没同步，测试直接红（清单靠机器盯着，不靠人记性好）。
#: probe 取值：anon（免凭据 GET）/ user（普通令牌 GET）/ admin（管理员 GET）/
#: auth-flow（注册-登录-刷新-登出整条）/ usage-flow（上报后再读回）/
#: org-flow（建组织 + 改配额）/ market-flow（发布 → 详情 → 下载，需要真条目 ID）/
#: binary（响应是包文件，按字节比对不做键集合比对）。
STABLE_OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "health.check",
        "method": "GET",
        "path": "/health",
        "auth": "none",
        "probe": "anon",
        "summary": "存活探针；只说进程活着，不代表业务可用",
        "response": ["ok", "service", "version", "timeMs"],
        "errors": [],
    },
    {
        "id": "auth.jwks",
        "method": "GET",
        "path": "/.well-known/jwks.json",
        "auth": "none",
        "probe": "anon",
        "summary": "RS256 公钥集；网关与第三方本地验签用同一份（Cache-Control: public, max-age=300）",
        "response": ["keys"],
        "errors": [],
    },
    {
        "id": "api.contract",
        "method": "GET",
        "path": "/api/v1/contract",
        "auth": "none",
        "probe": "anon",
        "summary": "本契约全文（生产环境也可用，与已关闭的 /openapi.json 无关）",
        "response": [
            "ok", "contractVersion", "families", "conventions", "authModes", "rateLimits",
            "errorCodes", "idempotency", "operations", "coverageGaps", "marketPlurals",
            "operationCount", "contractConsistent", "problems",
        ],
        "errors": [],
    },
    {
        "id": "api.contract.verify",
        "method": "GET",
        "path": "/api/v1/contract/verify",
        "auth": "none",
        "probe": "anon",
        "summary": "自检：契约与活实现是否还一致，problems 非空即不一致",
        "response": ["ok", "contractVersion", "operationCount", "problems"],
        "errors": [],
    },
    {
        "id": "auth.register",
        "idempotency": "not-supported",
        "method": "POST",
        "path": "/api/v1/auth/register",
        "auth": "none",
        "probe": "auth-flow",
        "successStatus": 201,
        "idempotent": False,
        "summary": "自助注册并直接签发令牌（服务端可用 TT_ALLOW_REGISTRATION 关闭）；重放同一次请求会 409 EMAIL_TAKEN，重试前先确认上一次是否已经建号",
        "request": ["email", "password", "nickname"],
        "response": ["ok", "accessToken", "refreshToken", "expiresIn", "profile", "quotaResolution"],
        "errors": ["INVALID_EMAIL", "WEAK_PASSWORD", "EMAIL_TAKEN", "REGISTRATION_DISABLED", "RATE_LIMITED"],
        "rateLimits": ["register-ip"],
    },
    {
        "id": "auth.login",
        "idempotency": "not-supported",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "auth": "none",
        "probe": "auth-flow",
        "successStatus": 200,
        "idempotent": False,
        "summary": "邮箱密码换访问令牌；组织配额打开拦停时这里可能 403 ORG_BUDGET_EXCEEDED",
        "request": ["email", "password"],
        "response": ["ok", "accessToken", "refreshToken", "expiresIn", "profile", "quotaResolution"],
        "errors": ["INVALID_CREDENTIALS", "ORG_BUDGET_EXCEEDED", "RATE_LIMITED", "AUTH_UNAVAILABLE"],
        "rateLimits": ["login-ip", "login-ip-account"],
    },
    {
        "id": "auth.refresh",
        "idempotency": "not-supported",
        "method": "POST",
        "path": "/api/v1/auth/refresh",
        "auth": "none",
        "probe": "auth-flow",
        "successStatus": 200,
        "idempotent": False,
        "summary": "用 refresh 换一对新令牌；旧 refresh 立刻失效（一次性，重放会 INVALID_REFRESH）",
        "request": ["refreshToken"],
        "response": ["ok", "accessToken", "refreshToken", "expiresIn", "profile", "quotaResolution"],
        "errors": ["INVALID_REFRESH", "TOKEN_EXPIRED"],
    },
    {
        "id": "auth.logout",
        "idempotency": "not-supported",
        "method": "POST",
        "path": "/api/v1/auth/logout",
        "auth": "none",
        "probe": "auth-flow",
        "successStatus": 200,
        "idempotent": True,
        "summary": (
            "吊销请求体里那个 refresh 会话——不读 Authorization 头，所以令牌已过期时也能调；"
            "令牌无效/从没出现过时返回 revoked=false；重放同一把已吊销的令牌仍返回 revoked=true"
            "（实测口径：revoked 报的是「匹配到那个会话」，不是「本次发生了状态变化」）"
        ),
        "request": ["refreshToken"],
        "response": ["ok", "revoked"],
        "errors": ["INVALID_JSON"],
    },
    {
        "id": "auth.me",
        "method": "GET",
        "path": "/api/v1/auth/me",
        "auth": "bearer",
        "probe": "user",
        "summary": "当前身份档案（profile.quota 与令牌声明同源，不会出现两套数）",
        "response": ["ok", "profile"],
        "errors": ["AUTH_REQUIRED", "TOKEN_EXPIRED"],
    },
    {
        "id": "tokens.revoked",
        "method": "GET",
        "path": "/api/v1/revoked",
        "auth": "none",
        "probe": "anon",
        "summary": "已吊销条目的分页清单，客户端据此提前失效本地缓存",
        "request": ["type?", "page", "pageSize"],
        "response": ["ok", "items", "total", "page", "pageSize", "hasMore"],
        "errors": ["INVALID_TYPE"],
    },
    {
        "id": "usage.report",
        "idempotency": "supported",
        "method": "POST",
        "path": "/api/v1/usage/report",
        "auth": "bearer",
        "probe": "usage-flow",
        "successStatus": 200,
        "idempotent": True,
        "summary": "上报用量明细；同 (来源, 期次, 行) 重复上报不会双计（天然幂等）",
        "request": ["source", "deviceId", "schemaVersion", "rows"],
        "response": ["ok", "source", "accepted", "generatedAtMs"],
        "errors": [
            "USAGE_INVALID_ROW", "USAGE_INVALID_SOURCE", "USAGE_TOO_MANY_ROWS",
            "USAGE_TOKENS_MISMATCH", "USAGE_SCOPE_DENIED", "AUTH_REQUIRED",
        ],
    },
    {
        "id": "usage.me",
        "method": "GET",
        "path": "/api/v1/usage/me",
        "auth": "bearer",
        "probe": "user",
        "summary": "自己的用量汇总（个人口径）",
        "request": ["period"],
        "response": ["ok", "period", "sub", "totals", "models", "days", "sources", "generatedAtMs"],
        "errors": ["AUTH_REQUIRED", "USAGE_INVALID_PERIOD"],
    },
    {
        "id": "usage.user",
        "method": "GET",
        "path": "/api/v1/usage/users/{sub}",
        "auth": "admin",
        "probe": "admin",
        "summary": "按 uid 查某人用量（管理员或被授权范围）",
        "request": ["period"],
        "response": ["ok", "period", "sub", "totals", "models", "days", "sources", "generatedAtMs"],
        "errors": ["USER_NOT_FOUND", "USAGE_SCOPE_DENIED", "AUTH_REQUIRED"],
    },
    {
        "id": "usage.departments",
        "method": "GET",
        "path": "/api/v1/admin/usage/departments",
        "auth": "admin",
        "probe": "admin",
        "summary": "部门报表；与组织预算视图同源（重复行不双计）",
        "request": ["period", "org"],
        "response": ["ok", "period", "org", "departments", "totals", "generatedAtMs"],
        "errors": ["FORBIDDEN", "AUTH_REQUIRED"],
    },
    {
        "id": "orgs.list",
        "method": "GET",
        "path": "/api/v1/admin/orgs",
        "auth": "admin",
        "probe": "admin",
        "summary": "列出组织及其配额与当期已用（组织标识一律规范化为小写）",
        "request": ["period"],
        "response": ["ok", "contractVersion", "period", "total", "orgs"],
        "errors": ["FORBIDDEN", "AUTH_REQUIRED"],
    },
    {
        "id": "orgs.create",
        "idempotency": "supported",
        "method": "POST",
        "path": "/api/v1/admin/orgs",
        "auth": "admin",
        "probe": "org-flow",
        "successStatus": 201,
        "idempotent": True,
        "summary": "建组织或改名；同 org+name 重复提交返回 created=false，天然幂等",
        "request": ["org", "name"],
        "response": ["ok", "contractVersion", "created", "org", "name", "createdAtMs"],
        "errors": ["INVALID_ORG", "INVALID_ORG_NAME", "FORBIDDEN"],
    },
    {
        "id": "orgs.quota.get",
        "method": "GET",
        "path": "/api/v1/admin/orgs/{org}/quota",
        "auth": "admin",
        "probe": "admin",
        "summary": "读组织级配额（两级配额里的上级）；statement 直说当前是拦停态还是报表态",
        "response": ["ok", "contractVersion", "org", "quota", "statement"],
        "errors": ["ORG_NOT_FOUND", "FORBIDDEN"],
    },
    {
        "id": "orgs.quota.put",
        "idempotency": "supported",
        "method": "PUT",
        "path": "/api/v1/admin/orgs/{org}/quota",
        "auth": "admin",
        "probe": "org-flow",
        "successStatus": 200,
        "idempotent": True,
        "summary": "写组织级配额；拦停开关翻转会吊销在用会话，回执明写吊销了几个、还剩几个",
        "request": ["monthlyTokens", "concurrentTasks", "memberMonthlyTokens", "enforce"],
        "response": [
            "ok", "contractVersion", "org", "quota", "sessionsRevoked", "activeSessions",
            "statement", "next",
        ],
        "errors": ["INVALID_QUOTA", "ORG_NOT_FOUND", "FORBIDDEN"],
    },
    {
        "id": "orgs.budget",
        "method": "GET",
        "path": "/api/v1/admin/orgs/{org}/budget",
        "auth": "admin",
        "probe": "admin",
        "summary": "组织预算视图：已用/剩余/超售边界，报表口径与签发口径同源",
        "request": ["period"],
        "response": [
            "ok", "contractVersion", "org", "period", "quota", "usage", "members", "budget",
            "attribution", "disclosed", "generatedAtMs",
        ],
        "errors": ["ORG_NOT_FOUND", "FORBIDDEN"],
    },
    {
        "id": "orgs.members",
        "method": "GET",
        "path": "/api/v1/admin/orgs/{org}/members",
        "auth": "admin",
        "probe": "admin",
        "summary": "成员的两级配额落点与豁免逐行明写（管理员对组织预算豁免）",
        "response": ["ok", "contractVersion", "org", "total", "members", "note"],
        "errors": ["ORG_NOT_FOUND", "FORBIDDEN"],
    },
    {
        "id": "tiers.effective",
        "method": "GET",
        "path": "/api/v1/model-tiers/effective",
        "auth": "bearer",
        "probe": "user",
        "conditional": "ETag / If-None-Match 命中返回 304",
        "summary": "当前对本人生效的模型档位表（带 RS256 签名，客户端要验签再采信）；"
                   "响应头另有 X-TT-Tier-Revision 直给修订号。口径提醒：localFallbackSuggested "
                   "目前只有 false 一种取值，「该回退本地配置」的真信号是 404 TIER_TABLE_NOT_FOUND，"
                   "别拿这个字段当判据",
        "response": [
            "ok", "contractVersion", "scope", "revision", "digest", "signature", "signingKid",
            "publishedAtMs", "document", "source", "localFallbackSuggested",
        ],
        "errors": ["TIER_TABLE_NOT_FOUND", "AUTH_REQUIRED"],
    },
    {
        "id": "tiers.at",
        "method": "GET",
        "path": "/api/v1/model-tiers/at",
        "auth": "bearer",
        "probe": "user",
        "summary": "按修订号取档位表（同一 revision 永远同一份，可复现）。口径差异要提前知道："
                   "这条走的是历史版本表（model_tier_revisions），顶层**没有 contractVersion**，"
                   "比 /effective 多 action、少契约版本——客户端不能拿同一个解析器硬套两条路径",
        "request": ["revision"],
        "response": [
            "ok", "scope", "revision", "digest", "signature", "signingKid", "action",
            "publishedAtMs", "document", "source",
        ],
        "errors": ["INVALID_REVISION", "TIER_REVISION_NOT_FOUND", "TIER_TABLE_NOT_FOUND"],
    },
    {
        "id": "market.categories",
        "method": "GET",
        "path": "/api/v1/categories",
        "auth": "none",
        "probe": "anon",
        "summary": "分类口径的事实来源（服务端说了算；type 必填，取值为三种市场类型）",
        "request": ["type"],
        "response": ["ok", "type", "categories"],
        "errors": ["INVALID_TYPE"],
    },
    {
        "id": "market.list",
        "method": "GET",
        "path": "/api/v1/{plural}",
        "auth": "none",
        "probe": "anon",
        "summary": "市场列表；plural 只允许声明过的那三个，其余 404 MARKET_NOT_FOUND",
        "pathEnum": {"plural": sorted(PLURAL_TO_TYPE)},
        "request": ["q", "category", "sort", "page", "pageSize", "featured"],
        "response": ["ok", "items", "total", "page", "pageSize", "hasMore", "type", "typeLabel"],
        "errors": ["MARKET_NOT_FOUND", "INVALID_SORT"],
    },
    {
        "idempotency": "supported",
        "id": "market.publishSkill",
        "method": "POST",
        "path": "/api/v1/skills",
        "auth": "bearer",
        "probe": "market-flow",
        "successStatus": 201,
        "idempotent": False,
        "summary": "发布技能包（multipart/form-data，字段名 package）。这条**最需要**幂等键："
        "响应丢了直接重试会撞上自己上一次造的 409 ITEM_EXISTS；带同一把 Idempotency-Key + "
        "同一份包体重试则拿第一次的回执（键的作用域含包的 SHA-256，换包就是另一件事）",
        "request": ["package", "summary", "description", "category", "tags", "readme",
                    "changelog", "icon", "version"],
        "response": ["ok", "item"],
        "errors": ["INVALID_SKILL_PACKAGE", "PACKAGE_REQUIRED", "PACKAGE_TOO_LARGE",
                   "ITEM_EXISTS", "AUTH_REQUIRED", "IDEMPOTENCY_CONFLICT",
                   "IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_KEY_INVALID"],
    },
    {
        "idempotency": "supported",
        "id": "market.publishSuite",
        "method": "POST",
        "path": "/api/v1/suites",
        "auth": "bearer",
        "probe": "market-flow",
        "successStatus": 201,
        "idempotent": False,
        "summary": "发布专家套件包（同一把键机制；技能与套件是两个不同 op ⇒ 同键不互相重放）",
        "request": ["package", "summary", "description", "category", "tags", "readme",
                    "changelog", "icon", "version"],
        "response": ["ok", "item"],
        "errors": ["INVALID_SUITE_PACKAGE", "PACKAGE_REQUIRED", "PACKAGE_TOO_LARGE",
                   "ITEM_EXISTS", "AUTH_REQUIRED", "IDEMPOTENCY_CONFLICT",
                   "IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_KEY_INVALID"],
    },
    {
        "id": "market.detail",
        "method": "GET",
        "path": "/api/v1/{plural}/{item_id}",
        "auth": "none",
        "probe": "market-flow",
        "summary": "条目详情。顶层**只有 ok 与 item 两个键**（实测钉住）：版本列表在 "
        "item.versions 里，不是在顶层——按顶层找 versions 的客户端会拿到 undefined。"
        "item 里再按类型追加：skill = packageInfo{fileCount,files}；suite 另有 "
        "commands[]；mcp 用 mcp{} 取代 packageInfo",
        "pathEnum": {"plural": sorted(PLURAL_TO_TYPE)},
        "nested": {
            "item": [
                "author", "category", "createdAtMs", "description", "downloadUrl", "downloads",
                "featured", "icon", "id", "install", "latestVersion", "name", "packageInfo",
                "publishedAtMs", "readme", "revoked", "starred", "stars", "summary", "tags",
                "type", "updatedAtMs", "versionCount", "versions",
            ]
        },
        "response": ["ok", "item"],
        "errors": ["MARKET_NOT_FOUND", "ITEM_REVOKED"],
    },
    {
        "id": "market.download",
        "method": "GET",
        "path": "/api/v1/{plural}/{item_id}/download",
        "auth": "none",
        "probe": "binary",
        "summary": "下载包文件（?version= 指定版本）；响应是二进制，客户端必须校验 SHA-256 后再装",
        "pathEnum": {"plural": sorted(PLURAL_TO_TYPE)},
        "request": ["version"],
        "response": ["<二进制流>"],
        "errors": ["PACKAGE_MISSING", "PACKAGE_CORRUPTED", "MARKET_NOT_FOUND"],
    },
)

#: 市场条目类型（从 catalog 派生，别手抄；plural 值变了清单自动跟着变）。
MARKET_TYPE_LABELS = {item_type: str(spec["label"]) for item_type, spec in TYPE_SPECS.items()}

#: 覆盖缺口：写清楚「这份契约不承诺什么」，比只写承诺了什么重要。
COVERAGE_GAPS: tuple[dict[str, str], ...] = (
    {
        "area": "对话与事件流",
        "status": "not-covered",
        "note": "/api/chat 与 SSE 事件契约在引擎进程（tongtian-tauri/engine/server.py），"
        "不在控制面，本窗口无写权，因此本契约**不含**一次完整对话的集成路径。"
        "台账 #154 的验收项「第三方按文档一天内完成对话」本刀**未覆盖**。",
    },
    {
        "area": "协作与工单面",
        "status": "internal",
        "note": "/api/v1/projects/*、/api/v1/agent/tickets、/api/v1/.../workflows 是自家客户端在用的面，"
        "形状还在随功能动，不进稳定清单；要对外承诺得先单独收口。",
    },
    {
        "area": "管理面写操作",
        "status": "partial",
        "note": "档位表与网关管理（/api/v1/admin/model-tiers/*、/api/v1/gateway/*）只有管理员或服务令牌能用，"
        "第三方的正常形态是只读自己那一片，这里不承诺对外可写。",
    },
    {
        "area": "幂等键",
        "status": "partial",
        "note": "机制已强制（清单里每条写操作都标了 idempotency=supported / not-supported，"
        "supported 的集合直接取自服务端注册表，不由清单自说自话）。三处**有意不接**：① 签发令牌的"
        "四条路径（register / login / refresh / logout）——回执原文要落 SQLite 明文，"
        "不拿凭据换重放便利；② 条目的**新版本**发布（/api/v1/{plural}/{item_id}/versions）"
        "还没接，重试会撞 PACKAGE_ID_MISMATCH 之类的既有校验；③ mcp 配置的发布（"
        "/api/v1/mcp 不是压缩包路径，走的是另一套校验）。未接的那些重试前请自己保证安全。",
    },
    {
        "area": "OpenAPI",
        "status": "production-off",
        "note": "/openapi.json、/docs 在生产环境关闭（main.py 的 _public_docs），"
        "所以对外交付物是本契约与 SDK，不是 Swagger 文件。",
    },
    {
        "area": "真实第三方集成",
        "status": "not-accepted",
        "note": "本窗口只交付契约、SDK 骨架与自测；没有外部接入方按文档跑通过，"
        "不能写成「已可被集成」。",
    },
)


def _source(module: str) -> str:
    """读 app/<module>.py 的源码文本，用来反向核对契约里抄来的数字与错误码。"""
    import pathlib

    path = pathlib.Path(__file__).with_name(f"{module}.py")
    return path.read_text(encoding="utf-8")


_API_ERROR_RE = re.compile(r'ApiError\(\s*(\d{3})\s*,\s*"([A-Z0-9_]+)"')
_CODE_FIELD_RE = re.compile(r'"code":\s*"([A-Z0-9_]+)"')
# 宽口径**存在性**：任何被引号包住的全大写短码（`code = "INVALID_SKILL_PACKAGE"` 这类
# 先赋值再抛出的写法）。只用于「这个码还在不在」，不用于状态码断言。
_BARE_CODE_RE = re.compile(r'"([A-Z][A-Z0-9_]{3,})"')


def codes_in_source() -> tuple[set[str], dict[str, set[int]]]:
    """全仓 app/*.py 里真实出现过的机器码。

    返回两份：出现过的码的集合，以及「以 `ApiError(状态, 码)` 形式抛出」的码 → 状态集合。
    后者为空是正常的——异常处理器里的 `VALIDATION_ERROR`、`INTERNAL_ERROR` 不走 ApiError，
    只出现在手写的 JSONResponse 里，所以它们只能证明「码还在用」，证明不了状态码。

    「出现过」为什么用宽口径：`app/packages.py` 抛的是 `_fail(code, ...)`，而
    `code = "INVALID_SKILL_PACKAGE"` 是**先赋给变量**再传进去的，直写形式匹配不到 ⇒
    只用严口径会把真在用的码报成「已不在源码中出现」（本刀实测踩过）。代价是宽口径更容易
    误报「还在」，所以**状态码这一层只信直写形式**，宽口径只用于存在性检查。
    """
    import pathlib

    seen: set[str] = set()
    statuses: dict[str, set[int]] = {}
    for path in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for status, code in _API_ERROR_RE.findall(text):
            seen.add(code)
            statuses.setdefault(code, set()).add(int(status))
        for code in _CODE_FIELD_RE.findall(text):
            seen.add(code)
        for code in _BARE_CODE_RE.findall(text):
            seen.add(code)
    return seen, statuses


def live_routes(app: Any) -> dict[tuple[str, str], list[str]]:
    """活路由表：(method, path) → 该端点挂的 require_* 依赖名。"""
    from fastapi.routing import APIRoute

    table: dict[tuple[str, str], list[str]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            table[(method, route.path)] = _dependency_names(route)
    return table


def _dependency_names(route: Any) -> list[str]:
    """把 Depends 链上的 require_user / require_admin 之类扒出来（含嵌套）。"""

    names: list[str] = []

    def walk(dependant: Any) -> None:
        for sub in getattr(dependant, "dependencies", ()) or ():
            call = getattr(sub, "call", None)
            if call is not None:
                name = getattr(call, "__name__", "")
                if name.startswith("require_"):
                    names.append(name)
            walk(sub)

    walk(route.dependant)
    return sorted(set(names))


_EXPECTED_DEPENDENCY = {"admin": "require_admin", "bearer": "require_user"}


def find_problems(app: Any) -> list[str]:
    """契约与活实现的不一致清单；空列表 = 此刻契约没说谎。"""
    problems: list[str] = []
    routes = live_routes(app)
    seen_codes, code_statuses = codes_in_source()
    seen_ids: set[str] = set()

    for op in STABLE_OPERATIONS:
        op_id = str(op["id"])
        if op_id in seen_ids:
            problems.append(f"{op_id}: 操作 id 重复")
        seen_ids.add(op_id)

        key = (str(op["method"]), str(op["path"]))
        if key not in routes:
            problems.append(f"{op_id}: 活路由表里没有 {key[0]} {key[1]}（契约条目已过期或写错了路径）")
            continue

        declared_auth = str(op["auth"])
        deps = routes[key]
        expected = _EXPECTED_DEPENDENCY.get(declared_auth)
        if expected and expected not in deps:
            problems.append(f"{op_id}: 声明 auth={declared_auth}，但 Depends 里找不到 {expected}（实际 {deps or '无'}）")
        if declared_auth == "none" and deps:
            problems.append(f"{op_id}: 声明免凭据，但挂了依赖 {deps}——第三方按免凭据接会 401")

        for code in op.get("errors", ()):
            if str(code) not in seen_codes:
                problems.append(f"{op_id}: 错误码 {code} 在 app/ 源码里已经找不到了")

        for limit_id in op.get("rateLimits", ()):
            if not any(item["id"] == limit_id for item in RATE_LIMITS):
                problems.append(f"{op_id}: 引用了未定义的速率限制 {limit_id}")

        if op.get("method") != "GET":
            for required in ("idempotent", "successStatus", "request", "response", "idempotency"):
                if required not in op:
                    problems.append(f"{op_id}: 写操作缺 {required} 字段（集成方重试前要能查到）")
            flag = str(op.get("idempotency"))
            if flag not in ("supported", "not-supported"):
                problems.append(
                    f"{op_id}: idempotency={flag} 只能是 supported / not-supported（"
                    "写重试安全与否必须是个二值，不能空着）"
                )
            registered = SUPPORTED_SCOPES.get(op_id)
            if flag == "supported" and registered is None:
                problems.append(
                    f"{op_id}: 清单说支持幂等键，但 idempotency.SUPPORTED_SCOPES 里没接这个 op"
                )
            if registered is not None and flag != "supported":
                problems.append(
                    f"{op_id}: 服务端接了幂等键，清单却标 {flag}——集成方会按「不重放安全」写代码"
                )
            if registered is not None:
                for field, actual, want in (
                    ("method", str(op["method"]), str(registered["method"])),
                    ("path", str(op["path"]), str(registered["path"])),
                    ("successStatus", int(op.get("successStatus") or 0),
                     int(registered["successStatus"])),
                ):
                    if actual != want:
                        problems.append(
                            f"{op_id}: 幂等注册表里 {field}={want}，清单写的是 {actual}"
                        )
        if "probe" not in op:
            problems.append(f"{op_id}: 没标 probe，自测不知道该不该真打这个端点")
        elif str(op["probe"]) not in PROBE_MODES:
            problems.append(f"{op_id}: probe={op['probe']} 不是已知探针模式（可选 {sorted(PROBE_MODES)}）")

    declared_supported = {
        str(op["id"]) for op in STABLE_OPERATIONS if str(op.get("idempotency")) == "supported"
    }
    for scope_id in sorted(set(SUPPORTED_SCOPES) - declared_supported):
        if scope_id not in seen_ids:
            problems.append(
                f"{scope_id}: 幂等注册表里有这条，但清单里连这个 op 都没登记（先登记再承诺）"
            )

    for item in RATE_LIMITS:
        try:
            text = _source(str(item["module"]))
        except OSError as exc:  # pragma: no cover - 只在文件被移走时触发
            problems.append(f"速率限制 {item['id']}: 读不到 app/{item['module']}.py（{exc}）")
            continue
        if str(item["keyTemplate"]) not in text:
            problems.append(
                f"速率限制 {item['id']}: app/{item['module']}.py 里找不到 {item['keyTemplate']}"
                "（数字或键前缀改了，契约没跟着改）"
            )

    for entry in ERROR_CODES:
        code = str(entry["code"])
        if code not in seen_codes:
            problems.append(f"错误码 {code} 已不在源码中出现")
            continue
        statuses = code_statuses.get(code) or set()
        # 只有经 ApiError 抛出的码才谈得上「状态对不对」；处理器里手写的码没有可比的状态来源
        if statuses and int(entry["status"]) not in statuses:
            problems.append(
                f"错误码 {code}: 契约写 {entry['status']}，源码里是 {sorted(statuses)}"
            )

    if tuple(USAGE_PERIODS) != tuple(ORG_QUOTA_PERIODS):
        problems.append(
            f"周期口径分家了：usage.PERIODS={USAGE_PERIODS} 与 org_quota.PERIODS={ORG_QUOTA_PERIODS}，"
            "同一份报表与预算会按不同周期聚合"
        )

    for family, version in CONTRACT_FAMILIES.items():
        if not re.fullmatch(r"[a-z0-9-]+\.v\d+", str(version)):
            problems.append(f"子契约 {family} 的版本号形状不对：{version}")

    return problems


def build_contract(app: Any) -> dict[str, Any]:
    """契约全文（给 /api/v1/contract 与 SDK 协商用）。"""
    problems = find_problems(app)
    operations = [dict(op) for op in STABLE_OPERATIONS]
    for op in operations:
        # GET 天然可重复；写操作必须自己声明——「重放一次会不会多出一个人/一份用量」
        # 是集成方重试前必须知道的事，不能让方法名替他猜。
        op["idempotent"] = True if op.get("method") == "GET" else bool(op.get("idempotent", False))
    return {
        "ok": True,
        "contractVersion": API_CONTRACT_VERSION,
        "families": dict(CONTRACT_FAMILIES),
        "conventions": CONVENTIONS,
        "authModes": AUTH_MODES,
        "rateLimits": [dict(item) for item in RATE_LIMITS],
        "errorCodes": [dict(item) for item in ERROR_CODES],
        "idempotency": dict(IDEMPOTENCY),
        "operations": operations,
        "coverageGaps": [dict(item) for item in COVERAGE_GAPS],
        "marketPlurals": sorted(PLURAL_TO_TYPE),
        "operationCount": len(operations),
        "contractConsistent": not problems,
        "problems": problems,
    }


def render_markdown(contract: dict[str, Any]) -> str:
    """把契约渲染成给人读的 markdown。

    `tongtian-server/CONTRACT.md` 不在本窗口领地（`app/**` 之外），所以这里只提供生成器，
    产物由 W4 决定落到哪里——避免第二个人手写一份与代码漂移的文档。
    """
    lines = [
        f"# 通天办公服务端 · 对外 API 契约（{contract['contractVersion']}）",
        "",
        "> 本文由 `python -m app.api_contract --emit-markdown` 从服务端源码生成，",
        "> **不要手改**；要改契约就改 `app/api_contract.py`，改完 `/api/v1/contract/verify` 必须空 problems。",
        "",
        "## 子契约家族",
        "",
        "| 片 | 版本 |",
        "| --- | --- |",
    ]
    for family, version in contract["families"].items():
        lines.append(f"| {family} | `{version}` |")
    lines += ["", "## 约定", ""]
    for key, value in contract["conventions"].items():
        lines.append(f"- **{key}**：{value}")
    lines += ["", "## 稳定操作", "", "| id | 方法 | 路径 | 鉴权 | 说明 |", "| --- | --- | --- | --- | --- |"]
    for op in contract["operations"]:
        path = op["path"].replace("|", "\\|")
        lines.append(f"| `{op['id']}` | {op['method']} | `{path}` | {op['auth']} | {op['summary']} |")
    lines += ["", "## 速率限制", "", "| id | 适用 | 维度 | 上限 | 窗口 |", "| --- | --- | --- | --- | --- |"]
    for item in contract["rateLimits"]:
        lines.append(
            f"| `{item['id']}` | {item['appliesTo']} | {item['dimension']} | "
            f"{item['limit']} | {item['windowSeconds']}s |"
        )
    lines += ["", "## 错误码", "", "| 码 | HTTP | 含义 |", "| --- | --- | --- |"]
    for entry in contract["errorCodes"]:
        lines.append(f"| `{entry['code']}` | {entry['status']} | {entry['meaning']} |")
    lines += ["", "## 不覆盖什么", ""]
    for gap in contract["coverageGaps"]:
        lines.append(f"- **{gap['area']}**（`{gap['status']}`）：{gap['note']}")
    lines.append("")
    return "\n".join(lines)


@router.get("/api/v1/contract")
def api_contract(request: Request) -> dict[str, Any]:
    """契约全文。生产环境也开——它是交付物，不是调试面。"""
    return build_contract(request.app)


@router.get("/api/v1/contract/verify")
def api_contract_verify(request: Request) -> dict[str, Any]:
    """契约自检：problems 非空说明清单跟实现已经不一致了。"""
    problems = find_problems(request.app)
    return {
        "ok": not problems,
        "contractVersion": API_CONTRACT_VERSION,
        "operationCount": len(STABLE_OPERATIONS),
        "problems": problems,
    }


def _main(argv: list[str] | None = None) -> int:
    import json
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    from .main import app  # 只有生成文档时才需要装配整个应用

    if "--emit-markdown" in argv:
        sys.stdout.write(render_markdown(build_contract(app)))
        return 0
    if "--verify" in argv:
        problems = find_problems(app)
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=2))
        return 0 if not problems else 1
    print(json.dumps(build_contract(app), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口留给文档生成与排障
    raise SystemExit(_main())
