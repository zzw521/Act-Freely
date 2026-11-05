"""最小 SDK 的实现体：一个方法一条稳定操作，重试与协商口径写死在这里。

设计口径（客户端只有行为跟这些话一致，SDK 才算有用）：

1. **不猜响应**。每条方法的返回就是契约声明的那份 JSON（`Result.data`），
   顶层键不做增删；`contract()` 拿到什么就返回什么。
2. **重试只在证明安全之后**。GET 天然可重复；POST/PUT 只在①调用方给了
   `idempotency_key`，或②请求根本没发出去（连接层失败）时才自动重试。
   服务端只支持 5 条写操作带键（见契约 `idempotency.supportedOperations`），
   给没接键的端点塞键会直接拿 400 `IDEMPOTENCY_SCOPE_UNSUPPORTED`——
   所以这里按 op 白名单决定「能不能替你重试」，而不是无脑带键。
3. **429 一定看 Retry-After**。数字是服务端给的，SDK 不自造退避表。
4. **access token 过期自己刷一次**。刷完仍 401 就把错误抛给调用方，不循环刷。
5. **契约版本不认识就停手**。协商失败抛 `ContractMismatch`，而不是
   「先按 v1 的代码跑跑看」——第三方最贵的 bug 是拿着错版本的假设跑通了半数请求。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

SDK_NAME = "tt-server-sdk"
SDK_VERSION = "1"

#: 本 SDK 认的对外契约版本（大版本白名单；不在表里 ⇒ 协商阶段就停手）。
#: 「契约版本号」与「SDK 版本号」不是一件事：契约可以 bump 而 SDK 不动，
#: 反过来 SDK 修 bug 也不该改契约，所以这里只登记契约版本。
SDK_CONTRACT_VERSIONS = frozenset({"tt-server-api.v1"})

#: 响应头名（与 `app/api_contract.py:CONTRACT_HEADER` 同一口径，自测交叉比对）。
CONTRACT_HEADER = "X-TT-Api-Contract"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-TT-Idempotent-Replay"

#: 服务端真的接了幂等键的写操作（唯一权威是 `app/idempotency.py:SUPPORTED_SCOPES`；
#: 这里是 SDK 侧的镜像，自测逐键比对，两边漂了就红）。
IDEMPOTENT_OPS = frozenset(
    {
        "market.publishSkill",
        "market.publishSuite",
        "usage.report",
        "orgs.create",
        "orgs.quota.put",
    }
)

#: 市场条目的三种 plural（与 `app/catalog.py` 同源，自测比对）。
MARKET_PLURALS = frozenset({"mcp", "skills", "suites"})

#: op id → 声明路径模板（SDK 只做「这条走哪个路径」的查表，不另抄一份 URL 常量）。
OP_PATHS: dict[str, str] = {
    "health.check": "/health",
    "auth.jwks": "/.well-known/jwks.json",
    "api.contract": "/api/v1/contract",
    "api.contract.verify": "/api/v1/contract/verify",
    "auth.register": "/api/v1/auth/register",
    "auth.login": "/api/v1/auth/login",
    "auth.refresh": "/api/v1/auth/refresh",
    "auth.logout": "/api/v1/auth/logout",
    "auth.me": "/api/v1/auth/me",
    "tokens.revoked": "/api/v1/revoked",
    "usage.report": "/api/v1/usage/report",
    "usage.me": "/api/v1/usage/me",
    "usage.user": "/api/v1/usage/users/{sub}",
    "usage.departments": "/api/v1/admin/usage/departments",
    "orgs.list": "/api/v1/admin/orgs",
    "orgs.create": "/api/v1/admin/orgs",
    "orgs.quota.get": "/api/v1/admin/orgs/{org}/quota",
    "orgs.quota.put": "/api/v1/admin/orgs/{org}/quota",
    "orgs.budget": "/api/v1/admin/orgs/{org}/budget",
    "orgs.members": "/api/v1/admin/orgs/{org}/members",
    "tiers.effective": "/api/v1/model-tiers/effective",
    "tiers.at": "/api/v1/model-tiers/at",
    "market.categories": "/api/v1/categories",
    "market.list": "/api/v1/{plural}",
    "market.publishSkill": "/api/v1/skills",
    "market.publishSuite": "/api/v1/suites",
    "market.detail": "/api/v1/{plural}/{item_id}",
    "market.download": "/api/v1/{plural}/{item_id}/download",
}

# --------------------------------------------------------------------------
# 异常：按「调用方要不要写不同的 except 分支」分类，不是按状态码穷举
# --------------------------------------------------------------------------


class SdkError(Exception):
    """SDK 侧的基类：所有本包抛的错都能被它接住。"""


class TransportError(SdkError):
    """连不上、超时、DNS 失败——**服务端一句话没说**，所以没有 code/status 可依据。

    单独成类是为了让调用方别把「网络没通」和「服务端拒绝」写成同一个分支。
    """

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.url = url


class ContractMismatch(SdkError):
    """协商失败：服务端说的契约版本不在本 SDK 的大版本白名单里。"""

    def __init__(self, server_version: str, supported: Iterable[str]) -> None:
        super().__init__(
            f"服务端契约版本 {server_version!r} 不在 SDK 认的 {sorted(supported)} 里；"
            "请先升级 SDK，不要按旧假设硬跑"
        )
        self.server_version = server_version
        self.supported = tuple(sorted(supported))


class UnknownMarketPlural(SdkError):
    """传了契约里没有的市场类型（路径直接拼错，比让服务端回 404 更早停）。"""


class ApiError(SdkError):
    """服务端给出的错误（有 JSON body、有 `code`）。"""

    def __init__(self, *, status: int, code: str, message: str,
                 headers: Mapping[str, str] | None = None, body: bytes = b"") -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.body = body
        self.retry_after = _retry_after_seconds(self.headers)

    @property
    def retriable(self) -> bool:
        """这一类错**值不值得**换个键/换个时刻再打一次（不代表 SDK 会自动打）。"""
        return self.status in (409, 429, 500, 502, 503, 504)


class AuthError(ApiError):
    """401：没带令牌、令牌无效或已过期。带 `refresh_token` 时 SDK 会先自己刷一次。"""


class ForbiddenError(ApiError):
    """403：权限不够，或组织预算已经收敛到拒发令牌（`ORG_BUDGET_EXCEEDED`）。"""


class NotFoundError(ApiError):
    """404：条目/路由不存在。"""


class ConflictError(ApiError):
    """409：与既有状态冲突（邮箱已占用、同名的包已存在……）。"""


class RateLimited(ApiError):
    """429：速率限制。`retry_after` 是服务端给的秒数。"""


class ServerError(ApiError):
    """5xx：服务端自己的问题。"""


class IdempotencyConflict(ApiError):
    """409 `IDEMPOTENCY_CONFLICT`：同一把键配了另一件事（体不一样）——必须换键。"""


class IdempotencyInProgress(ApiError):
    """409 `IDEMPOTENCY_IN_PROGRESS`：上一次还在跑，按 `retry_after` 再取回执。"""


#: 机器码 → 异常类。只列**调用方会分开处理**的那些，其余落回基类 ApiError。
#: 每一条都必须在契约的 ERROR_CODES 里登记过，且 classify(声明状态, 码) 真给出这个类
#: （`tests/test_sdk.py` 逐条回比：映射私藏了一个没声明的码、或把 400 的码映射成 409 的类，
#: 都会红）。`PACKAGE_ID_MISMATCH` 故意不在这里——它是 400，不是冲突。
#: `NOT_FOUND` 也不在这里：404 由 STATUS_CLASSES 兜底，SDK 不认一个没登记的码。
CODE_CLASSES: dict[str, type[ApiError]] = {
    "AUTH_REQUIRED": AuthError,
    "TOKEN_EXPIRED": AuthError,
    "INVALID_CREDENTIALS": AuthError,
    "INVALID_REFRESH": AuthError,
    "FORBIDDEN": ForbiddenError,
    "REGISTRATION_DISABLED": ForbiddenError,
    "ORG_BUDGET_EXCEEDED": ForbiddenError,
    "MARKET_NOT_FOUND": NotFoundError,
    "ITEM_NOT_FOUND": NotFoundError,
    "EMAIL_TAKEN": ConflictError,
    "ITEM_EXISTS": ConflictError,
    "IDEMPOTENCY_CONFLICT": IdempotencyConflict,
    "IDEMPOTENCY_IN_PROGRESS": IdempotencyInProgress,
    "RATE_LIMITED": RateLimited,
}

#: 状态码 → 默认基类（body 里没有可识别的码时兜底，保证抛出的类型和状态一致）。
STATUS_CLASSES: dict[int, type[ApiError]] = {
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    429: RateLimited,
}


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def classify(status: int, code: str, message: str, headers: Mapping[str, str],
             body: bytes) -> ApiError:
    """把一次失败响应映射成异常类（契约声明的码优先，其次按状态兜底）。"""
    cls = CODE_CLASSES.get(code) or STATUS_CLASSES.get(status) or ApiError
    if status >= 500:
        cls = ServerError if cls in (ApiError, AuthError, ForbiddenError, NotFoundError,
                                     ConflictError, RateLimited) else cls
    return cls(status=status, code=code, message=message, headers=headers, body=body)


# --------------------------------------------------------------------------
# 档位文档的摘要口径（标准库能做到的那一半，另一半是验签，明确不做）
# --------------------------------------------------------------------------


def tier_document_bytes(document: Mapping[str, Any]) -> bytes:
    """与服务端 `model_tiers.canonical_bytes` 同一套规范化：排序键 + 紧凑分隔符 + 不转义非 ASCII。"""
    return json.dumps(dict(document), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def tier_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(tier_document_bytes(document)).hexdigest()


def check_tier_digest(payload: Mapping[str, Any]) -> bool:
    """比对 `payload["digest"]` 与文档实算摘要——**这只证明内容没被换过，不证明签名有效**。

    验签要用 JWKS 里的 RS256 公钥，标准库做不了；调用方该拿 `signature` +
    `signingKid` 交给自己的 JOSE 库，别把本函数当成验签结果采信。
    """
    document = payload.get("document")
    if not isinstance(document, dict):
        return False
    return str(payload.get("digest") or "") == tier_digest(document)


# --------------------------------------------------------------------------
# 结果对象与 multipart
# --------------------------------------------------------------------------


@dataclass
class Result:
    """一次成功响应的原样材料。

    `data` 是解析后的 JSON（二进制端点是 `None`），`raw` 是响应体字节，
    `replayed` 直接读 `X-TT-Idempotent-Replay`——重放与真跑对客户端是**必须可分辨**的。
    """

    status: int
    headers: dict[str, str]
    data: Any
    raw: bytes = b""

    contract_version: str = ""
    replayed: bool = False

    def __post_init__(self) -> None:
        lowered = {str(k).lower(): v for k, v in self.headers.items()}
        self.contract_version = str(lowered.get(CONTRACT_HEADER.lower(), ""))
        self.replayed = str(lowered.get(REPLAY_HEADER.lower(), "")) == "true"


@dataclass
class _TokenPair:
    access: str = ""
    refresh: str = ""
    expires_in: int = 0

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "_TokenPair":
        return cls(
            access=str(data.get("accessToken") or ""),
            refresh=str(data.get("refreshToken") or ""),
            expires_in=int(data.get("expiresIn") or 0),
        )


def build_multipart(fields: Mapping[str, Any], *, file_field: str, file_name: str,
                    file_bytes: bytes,
                    content_type: str = "application/zip") -> tuple[bytes, str]:
    """手搓 multipart/form-data：市场发布端点只吃这个形状，而 SDK 不带 requests。"""
    boundary = f"----ttSdkBoundary{secrets.token_hex(8)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
             f"{value}\r\n").encode("utf-8")
        )
    chunks.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
         f"filename=\"{file_name}\"\r\nContent-Type: {content_type}\r\n\r\n").encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def make_idempotency_key(prefix: str = "sdk") -> str:
    """生成一把符合服务端形状要求的键（8~200 位 [A-Za-z0-9._-]）。"""
    return f"{prefix}-{secrets.token_hex(12)}"


#: 稳定 op id → SDK 方法名，由 @stable 装饰器填；自测拿它和契约清单做集合相等比对。
OP_METHODS: dict[str, str] = {}


def stable(op_id: str) -> Callable:
    """把方法与契约里的 op id 钉在一起（漏实现、错命名都在自测里红，不靠人记）。"""

    def deco(fn: Callable) -> Callable:
        if op_id not in OP_PATHS:
            raise RuntimeError(f"{op_id} 没有登记路径，SDK 不能凭空造 URL")
        OP_METHODS[op_id] = fn.__name__
        return fn

    return deco


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class Client:
    """一个客户端 = 一个 base_url + 一份令牌。同步、无状态之外的全部缓存只有令牌。

    `verify=True` 时第一次调用会打 `/api/v1/contract` 做协商；之后每个响应只要
    `X-TT-Api-Contract` 头里的版本不再是白名单里的，就抛 `ContractMismatch`。
    """

    def __init__(self, base_url: str, *, timeout: float = 15.0,
                 access_token: str = "", refresh_token: str = "",
                 verify_contract: bool = True,
                 max_retry_waits: int = 3, max_total_wait: float = 20.0,
                 sleeper: Callable[[float], None] = time.sleep,
                 opener: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._tokens = _TokenPair(access=access_token, refresh=refresh_token)
        self.verify_contract = verify_contract
        self.max_retry_waits = max(0, int(max_retry_waits))
        self.max_total_wait = float(max_total_wait)
        self._sleep = sleeper
        self._opener = opener or urllib.request.build_opener()
        self._negotiated: dict[str, Any] | None = None
        self._contract_versions_seen: list[str] = []
        #: 协商前先用本地常量，协商成功后被契约里的 marketPlurals 覆盖（同源由自测钉）。
        self._market_plurals: set[str] = set(MARKET_PLURALS)

    # -- 令牌 --------------------------------------------------------------

    @property
    def access_token(self) -> str:
        return self._tokens.access

    @property
    def refresh_token(self) -> str:
        return self._tokens.refresh

    def set_tokens(self, *, access: str = "", refresh: str = "") -> None:
        """给外部存储（钥匙串/配置文件）留的口子：拿到旧令牌就直接塞进来。"""
        if access:
            self._tokens.access = access
        if refresh:
            self._tokens.refresh = refresh

    # -- 协商 --------------------------------------------------------------

    def negotiate(self, *, force: bool = False) -> dict[str, Any]:
        """读 `/api/v1/contract` 并按大版本白名单判一次。失败就抛，别硬跑。"""
        if self._negotiated is not None and not force:
            return self._negotiated
        result = self._raw_call("GET", OP_PATHS["api.contract"], auth=False)
        version = result.contract_version or str(result.data.get("contractVersion") or "")
        if version not in SDK_CONTRACT_VERSIONS:
            raise ContractMismatch(version, SDK_CONTRACT_VERSIONS)
        if not bool(result.data.get("contractConsistent")):
            raise ContractMismatch(
                f"{version}（服务端自检 problems={result.data.get('problems')}）",
                SDK_CONTRACT_VERSIONS,
            )
        plurals = {str(item) for item in result.data.get("marketPlurals") or []}
        if plurals:
            # 协商过一次就把清单里的 plural 当真值，后面拼路径按它校验
            self._market_plurals = plurals
        self._negotiated = result.data
        return result.data

    @property
    def contract(self) -> dict[str, Any] | None:
        return self._negotiated

    # -- 传输核心 ----------------------------------------------------------

    def _url(self, path: str, params: Mapping[str, Any] | None) -> str:
        url = self.base_url + path
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""}
            )
            if query:
                url = f"{url}?{query}"
        return url

    def _raw_call(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                  json_body: Any = None, raw_body: bytes | None = None,
                  content_type: str = "", headers: Mapping[str, str] | None = None,
                  auth: bool = True, binary: bool = False) -> Result:
        send: dict[str, str] = {"Accept": "application/json"}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            send["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body
            send["Content-Type"] = content_type or "application/octet-stream"
        if binary:
            send["Accept"] = "application/octet-stream"
        if auth and self._tokens.access:
            send["Authorization"] = f"Bearer {self._tokens.access}"
        for key, value in (headers or {}).items():
            if value is not None:
                send[key] = str(value)
        if method not in ("GET", "HEAD") and data is None:
            send["Content-Length"] = "0"
        request = urllib.request.Request(self._url(path, params), data=data,
                                         headers=send, method=method)
        try:
            response = self._opener.open(request, timeout=self.timeout)
            status = int(getattr(response, "status", 200))
            got_headers = dict(response.headers.items())
            body = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            got_headers = dict(exc.headers.items() or {})
            body = exc.read()
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"请求未送达：{reason}", url=request.full_url) from exc

        if status == 204:
            payload: Any = None
        elif "json" not in str(_lower_header(got_headers, "content-type") or "").lower():
            payload = None
        else:
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise ApiError(status=status, code="INVALID_JSON_RESPONSE",
                               message=f"响应不是合法 JSON：{exc}", headers=got_headers,
                               body=body) from exc
        result = Result(status=status, headers=got_headers, data=payload, raw=body)
        version = result.contract_version
        if version:
            if version not in SDK_CONTRACT_VERSIONS:
                raise ContractMismatch(version, SDK_CONTRACT_VERSIONS)
            if version not in self._contract_versions_seen:
                self._contract_versions_seen.append(version)
        if status >= 400:
            payload = payload if isinstance(payload, dict) else {}
            raise classify(
                status,
                str(payload.get("code") or f"HTTP_{status}"),
                str(payload.get("message") or body[:200].decode("utf-8", "replace")),
                got_headers,
                body,
            )
        return result

    def call(self, method: str, op_id: str, *, params: Mapping[str, Any] | None = None,
             json_body: Any = None, raw_body: bytes | None = None,
             content_type: str = "", idempotency_key: str = "", path_values:
             Mapping[str, str] | None = None, auth: bool = True,
             binary: bool = False, allow_replay_retry: bool = True) -> Result:
        """带重试与自动刷新的一次调用（方法层都走这里，口径只有一份）。"""
        path = OP_PATHS[op_id]
        for name, value in (path_values or {}).items():
            path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        if "{" in path:
            raise SdkError(f"{op_id} 的路径模板 {OP_PATHS[op_id]} 还有未填的槽位")
        if self.verify_contract and self._negotiated is None and op_id != "api.contract":
            self.negotiate()
        extra: dict[str, str] = {}
        if idempotency_key:
            if op_id not in IDEMPOTENT_OPS:
                raise SdkError(
                    f"{op_id} 不在服务端幂等注册表里：带键打过去只会拿 "
                    f"400 IDEMPOTENCY_SCOPE_UNSUPPORTED，别假装重试是安全的"
                )
            extra[IDEMPOTENCY_HEADER] = idempotency_key
        safe_to_retry = method == "GET" or bool(idempotency_key)

        waits = 0.0
        attempts = 0
        refreshed_once = False
        while True:
            attempts += 1
            try:
                return self._raw_call(method, path, params=params, json_body=json_body,
                                      raw_body=raw_body, content_type=content_type,
                                      headers=extra, auth=auth, binary=binary)
            except AuthError as exc:
                # 401 只刷一次：刷成功就带着新令牌重打，刷不动就把原错抛出去
                if exc.code not in ("TOKEN_EXPIRED", "AUTH_REQUIRED") or refreshed_once \
                        or not self._tokens.refresh or not auth:
                    raise
                refreshed_once = True
                self.refresh(self._tokens.refresh)
                continue
            except (RateLimited, ServerError, IdempotencyInProgress) as exc:
                in_progress = isinstance(exc, IdempotencyInProgress)
                if not safe_to_retry and not in_progress:
                    raise
                if attempts > self.max_retry_waits:
                    raise
                delay = exc.retry_after or (1.0 if in_progress else 0.0)
                if not delay:
                    # 服务端没给 Retry-After 就不自造退避表：429 的秒数是它的权威，
                    # SDK 造一套只会让两边的限流口径各说各话。
                    raise
                if waits + delay > self.max_total_wait:
                    raise
                self._sleep(delay)
                waits += delay
            except TransportError:
                # 请求没发出去 ⇒ 服务端一定没跑过 ⇒ 重打不重复副作用
                if attempts > self.max_retry_waits:
                    raise
                self._sleep(0.2)
                waits += 0.2

    # -- auth --------------------------------------------------------------

    @stable("health.check")
    def health(self) -> dict[str, Any]:
        return self.call("GET", "health.check", auth=False).data

    @stable("auth.jwks")
    def jwks(self) -> list[dict[str, Any]]:
        return self.call("GET", "auth.jwks", auth=False).data["keys"]

    @stable("api.contract")
    def contract_document(self) -> dict[str, Any]:
        return self.negotiate(force=True)

    @stable("api.contract.verify")
    def verify(self) -> dict[str, Any]:
        return self.call("GET", "api.contract.verify", auth=False).data

    @stable("auth.register")
    def register(self, email: str, password: str, nickname: str = "") -> dict[str, Any]:
        result = self.call("POST", "auth.register", auth=False,
                           json_body={"email": email, "password": password,
                                      "nickname": nickname})
        self._tokens = _TokenPair.from_payload(result.data)
        return result.data

    @stable("auth.login")
    def login(self, email: str, password: str) -> dict[str, Any]:
        result = self.call("POST", "auth.login", auth=False,
                           json_body={"email": email, "password": password})
        self._tokens = _TokenPair.from_payload(result.data)
        return result.data

    @stable("auth.refresh")
    def refresh(self, refresh_token: str | None = None) -> dict[str, Any]:
        token = refresh_token or self._tokens.refresh
        result = self.call("POST", "auth.refresh", auth=False,
                           json_body={"refreshToken": token})
        self._tokens = _TokenPair.from_payload(result.data)
        return result.data

    @stable("auth.logout")
    def logout(self) -> dict[str, Any]:
        result = self.call("POST", "auth.logout", auth=False,
                           json_body={"refreshToken": self._tokens.refresh})
        # 登出后本地令牌必须一起丢掉，否则调用方以为还登着
        self._tokens = _TokenPair()
        return result.data

    @stable("auth.me")
    def me(self) -> dict[str, Any]:
        return self.call("GET", "auth.me").data

    @stable("tokens.revoked")
    def revoked_tokens(self, *, kind: str | None = None, page: int = 1,
                       page_size: int = 20) -> dict[str, Any]:
        return self.call("GET", "tokens.revoked", auth=False,
                         params={"type": kind, "page": page, "pageSize": page_size}).data

    # -- usage -------------------------------------------------------------

    @stable("usage.report")
    def report_usage(self, rows: list[dict[str, Any]], *, source: str = "desktop",
                     device_id: str = "sdk", idempotency_key: str = "") -> dict[str, Any]:
        body = {"source": source, "deviceId": device_id, "schemaVersion": 1, "rows": rows}
        return self.call("POST", "usage.report", json_body=body,
                         idempotency_key=idempotency_key).data

    @stable("usage.me")
    def usage_me(self, period: str = "total") -> dict[str, Any]:
        return self.call("GET", "usage.me", params={"period": period}).data

    @stable("usage.user")
    def usage_user(self, sub: str, period: str = "total") -> dict[str, Any]:
        return self.call("GET", "usage.user", path_values={"sub": sub},
                         params={"period": period}).data

    @stable("usage.departments")
    def usage_departments(self, period: str = "total",
                          org: str | None = None) -> dict[str, Any]:
        return self.call("GET", "usage.departments", params={"period": period, "org": org}).data

    # -- orgs（管理员）------------------------------------------------------

    @stable("orgs.list")
    def orgs(self, period: str = "total") -> dict[str, Any]:
        return self.call("GET", "orgs.list", params={"period": period}).data

    @stable("orgs.create")
    def create_org(self, org: str, name: str = "", *,
                   idempotency_key: str = "") -> dict[str, Any]:
        return self.call("POST", "orgs.create",
                         json_body={"org": org, "name": name or org},
                         idempotency_key=idempotency_key).data

    @stable("orgs.quota.get")
    def org_quota(self, org: str) -> dict[str, Any]:
        return self.call("GET", "orgs.quota.get", path_values={"org": org}).data

    @stable("orgs.quota.put")
    def set_org_quota(self, org: str, *, monthly_tokens: int | None = None,
                      concurrent_tasks: int | None = None,
                      member_monthly_tokens: int | None = None,
                      enforce: bool | None = None, idempotency_key: str = "") -> dict[str, Any]:
        # 只发显式给了的字段：契约里写了「未提交的字段保持原值」，全量提交会把没传的清零
        body = {
            "monthlyTokens": monthly_tokens,
            "concurrentTasks": concurrent_tasks,
            "memberMonthlyTokens": member_monthly_tokens,
            "enforce": enforce,
        }
        return self.call("PUT", "orgs.quota.put", path_values={"org": org},
                         json_body={k: v for k, v in body.items() if v is not None},
                         idempotency_key=idempotency_key).data

    @stable("orgs.budget")
    def org_budget(self, org: str, period: str = "total") -> dict[str, Any]:
        return self.call("GET", "orgs.budget", path_values={"org": org},
                         params={"period": period}).data

    @stable("orgs.members")
    def org_members(self, org: str) -> dict[str, Any]:
        return self.call("GET", "orgs.members", path_values={"org": org}).data

    # -- model tiers -------------------------------------------------------

    @stable("tiers.effective")
    def tiers_effective(self) -> dict[str, Any]:
        result = self.call("GET", "tiers.effective")
        if not check_tier_digest(result.data):
            raise SdkError("档位文档的 digest 与内容不一致（中间被换过？），不要采信")
        return result.data

    @stable("tiers.at")
    def tier_revision(self, revision: int) -> dict[str, Any]:
        data = self.call("GET", "tiers.at", params={"revision": revision}).data
        # `/at` 的顶层**没有** contractVersion（实测），别按 effective 的形状硬读
        if isinstance(data.get("document"), dict) and not check_tier_digest(data):
            raise SdkError(f"第 {revision} 版档位文档的 digest 与内容不一致")
        return data

    # -- market ------------------------------------------------------------

    def _plural(self, plural: str) -> str:
        allowed = set(getattr(self, "_market_plurals", ()) or MARKET_PLURALS)
        if plural not in allowed:
            raise UnknownMarketPlural(
                f"市场类型 {plural!r} 不在契约的 marketPlurals {sorted(allowed)} 里")
        return plural

    @stable("market.categories")
    def market_categories(self, item_type: str) -> dict[str, Any]:
        return self.call("GET", "market.categories", params={"type": item_type},
                         auth=False).data

    @stable("market.list")
    def market_list(self, plural: str, *, q: str = "", category: str = "", sort: str = "",
                    page: int | None = None, page_size: int | None = None,
                    featured: bool | None = None) -> dict[str, Any]:
        return self.call("GET", "market.list",
                         path_values={"plural": self._plural(plural)}, auth=False,
                         params={"q": q, "category": category, "sort": sort, "page": page,
                                 "pageSize": page_size, "featured": _tri(featured)}).data

    @stable("market.publishSkill")
    def publish_skill(self, package: bytes, *, file_name: str = "skill.zip",
                      summary: str = "", description: str = "", category: str = "",
                      tags: str = "", readme: str = "", changelog: str = "", icon: str = "",
                      version: str = "", idempotency_key: str = "") -> dict[str, Any]:
        return self._publish("market.publishSkill", package, file_name=file_name,
                             summary=summary, description=description, category=category,
                             tags=tags, readme=readme, changelog=changelog, icon=icon,
                             version=version, idempotency_key=idempotency_key)

    @stable("market.publishSuite")
    def publish_suite(self, package: bytes, *, file_name: str = "suite.zip",
                      summary: str = "", description: str = "", category: str = "",
                      tags: str = "", readme: str = "", changelog: str = "", icon: str = "",
                      version: str = "", idempotency_key: str = "") -> dict[str, Any]:
        return self._publish("market.publishSuite", package, file_name=file_name,
                             summary=summary, description=description, category=category,
                             tags=tags, readme=readme, changelog=changelog, icon=icon,
                             version=version, idempotency_key=idempotency_key)

    def _publish(self, op_id: str, package: bytes, **kwargs: Any) -> dict[str, Any]:
        file_name = kwargs.pop("file_name")
        fields = {k: v for k, v in kwargs.items() if k != "idempotency_key" and v not in ("", None)}
        key = str(kwargs.get("idempotency_key") or "")
        form, ctype = build_multipart(fields, file_field="package", file_name=file_name,
                                      file_bytes=package)
        return self.call("POST", op_id, raw_body=form, content_type=ctype,
                         idempotency_key=key).data

    @stable("market.detail")
    def market_detail(self, plural: str, item_id: str) -> dict[str, Any]:
        return self.call("GET", "market.detail",
                         path_values={"plural": self._plural(plural), "item_id": item_id},
                         auth=False).data

    @stable("market.download")
    def download_package(self, plural: str, item_id: str, *, version: str | None = None,
                         expect_sha256: str = "") -> bytes:
        result = self.call("GET", "market.download",
                           path_values={"plural": self._plural(plural), "item_id": item_id},
                           params={"version": version}, auth=False, binary=True)
        blob = result.raw
        if expect_sha256 and hashlib.sha256(blob).hexdigest() != expect_sha256:
            raise SdkError("下载包与声明的 sha256 不符（客户端必须校验后再装）")
        return blob

    # -- 自省 --------------------------------------------------------------

    @staticmethod
    def op_coverage() -> dict[str, str]:
        """契约 op id → SDK 方法名。自测拿它和清单做集合相等比对。"""
        return dict(OP_METHODS)

    @property
    def seen_contract_versions(self) -> list[str]:
        return list(self._contract_versions_seen)


def _tri(value: bool | None) -> Any:
    """FastAPI 的布尔查询参数吃 true/false 字符串；None 表示「不带这个参数」。"""
    if value is None:
        return None
    return "true" if value else "false"


def _lower_header(headers: Mapping[str, Any], name: str) -> Any:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    return lowered.get(name.lower())
