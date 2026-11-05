#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小对外 SDK（D-19 / #154 刀 C）自测。

跑法：`tongtian-server/.venv/Scripts/python.exe -X utf8 tests/test_sdk.py`
判定只看 `^  ✓` 计数与末行「通过 N 项」，别只看退出码。

SDK 最容易骗人的地方是「它自己跑得通」：对着同一个仓库写出来的客户端，
永远不会发现自己把契约读错了。所以这一族测的是**外部视角**——

- SDK 声明的每条 op 是否真的存在、路径与契约清单是否逐条一致（漏实现/多实现都红）；
- SDK 镜像的服务端常量（头名、幂等 op 集、市场 plural）有没有漂；
- 只 import 标准库：SDK 不许把 fastapi/pydantic/uvicorn/jwt 拖进第三方依赖树；
- 契约协商：版本不认识就停手（ContractMismatch），而不是按旧假设硬跑；
- 429 一定读 Retry-After、只重试安全的请求（没带幂等键的 POST 绝不自动重打）；
- access token 过期自动刷一次，刷完仍失败就把原错抛出去（不循环刷）；
- 错误映射：状态码 + 机器码 → 异常类，且抛出的类型与状态一致；
- 档位文档的 digest 自洽比对（**不是验签**，测试里也按这个口径钉）；
- 下载按 sha256 校验后才交字节；
- 真打一轮：注册 → 上报 → 查自己 → 发布 → 列表 → 详情 → 下载 → 登出。

不覆盖：真实第三方按文档集成（外部条件，见 #154 约束 B）、引擎侧对话流（非本窗口领地）。
"""

import atexit
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-sdk-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "sdk-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "SDK 测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import api_contract, idempotency  # noqa: E402
from app.api_contract import ERROR_CODES, STABLE_OPERATIONS  # noqa: E402
from app.main import app  # noqa: E402
from app.sdk import Client, classify, tier_digest  # noqa: E402
from app.sdk import client as sdk_client  # noqa: E402
from app.sdk.client import (  # noqa: E402
    ApiError,
    AuthError,
    ConflictError,
    ContractMismatch,
    ForbiddenError,
    IdempotencyConflict,
    NotFoundError,
    RateLimited,
    SDK_CONTRACT_VERSIONS,
    SdkError,
    TransportError,
    UnknownMarketPlural,
    check_tier_digest,
    make_idempotency_key,
)

_passed = 0
ADMIN_PASSWORD = "admin-secret-123"
ACCOUNT_PASSWORD = "secret123"
OPS = {str(op["id"]): op for op in STABLE_OPERATIONS}


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener()


def start_server() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(150):
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端没起来")


def skill_zip(name: str) -> bytes:
    text = (
        f"---\nname: {name}\nversion: 1.0.0\ncategory: 效率\ntags: 自测,SDK\n---\n\n"
        f"# {name}\n\nSDK 自测造的假技能。\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", text)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# 假传输：只用来测 SDK 自己的重试/刷新/映射逻辑（真打的部分在下面单独一轮）
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def json_response(status: int, data: dict, headers: dict | None = None) -> FakeResponse:
    head = {"content-type": "application/json"}
    head.update(headers or {})
    return FakeResponse(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), head)


class ScriptedOpener:
    """按脚本回放响应；顺带记录每次请求，用来证明「SDK 到底打了几次」。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[dict] = []

    def open(self, request, timeout=None):  # noqa: ANN001, ANN002
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": request.data,
            }
        )
        item = self.script.pop(0) if self.script else None
        if item is None:
            raise AssertionError("脚本用完了（SDK 打多了不该打的请求）")
        if isinstance(item, Exception):
            raise item
        return item


def fake_client(script: list, **kwargs) -> tuple[Client, ScriptedOpener, list]:
    """关掉协商与真实网络；sleeper 只记账不真睡（否则 Retry-After=120 会把自测拖死）。"""
    sleeps: list[float] = []
    opener = ScriptedOpener(script)
    client = Client(
        BASE,
        opener=opener,
        verify_contract=False,
        sleeper=sleeps.append,
        max_retry_waits=kwargs.pop("max_retry_waits", 3),
        max_total_wait=kwargs.pop("max_total_wait", 300.0),
        **kwargs,
    )
    return client, opener, sleeps


def raw_http(method: str, path: str, *, json_body: dict | None = None,
             token: str = "") -> tuple[int, bytes]:
    """给「不在稳定清单里、SDK 故意没方法」的写入面用的裸 HTTP（造前提，不测客户端）。"""
    data = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=20) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def main() -> None:  # noqa: C901 - 自测按小节线性写，拆开反而丢上下文
    start_server()

    print("\n[SDK 只吃标准库]")
    # 真起一个解释器进程只 import SDK：只要它拖进服务端依赖，这里就会看见。
    # 探针代码写到临时文件再跑——Windows 命令行会把内联脚本里的引号搅成一团（本机踩过）。
    probe_path = os.path.join(_TMP, "sdk_dep_probe.py")
    with open(probe_path, "w", encoding="utf-8") as handle:
        handle.write(
            "import sys\n"
            f"sys.path.insert(0, {ROOT!r})\n"
            "import app.sdk  # noqa: F401\n"
            "server_side = {'fastapi', 'pydantic', 'uvicorn', 'jwt', 'starlette', 'httpx',\n"
            "               'anyio', 'cryptography', 'jose'}\n"
            "print(','.join(sorted({m.split('.')[0] for m in sys.modules\n"
            "                       if m.split('.')[0] in server_side})))\n"
        )
    probe = subprocess.run([sys.executable, "-X", "utf8", probe_path],
                           capture_output=True, text=True, timeout=120)
    check("单独 import app.sdk 不拉服务端依赖（第三方不会被拖进 fastapi/pydantic）",
          probe.returncode == 0 and probe.stdout.strip() == "",
          f"rc={probe.returncode} stdout={probe.stdout!r} stderr={probe.stderr[:300]!r}")

    print("\n[SDK 覆盖的 op 与契约清单逐条一致]")
    coverage = Client.op_coverage()
    check("契约里每条稳定操作都有 SDK 方法（一条不漏）",
          set(coverage) == set(OPS),
          f"少 {sorted(set(OPS) - set(coverage))} / 多 {sorted(set(coverage) - set(OPS))}")
    check("方法名与 op id 对得上（装饰器不是摆设）",
          all(hasattr(Client, name) for name in coverage.values()),
          str([n for n in coverage.values() if not hasattr(Client, n)]))
    path_mismatch = []
    for op_id, method_name in coverage.items():
        declared = str(OPS[op_id]["path"])
        template = sdk_client.OP_PATHS.get(op_id, "")
        # 清单用 {item_id}，SDK 模板用同名槽位；市场两类路径模板一致即可
        if template != declared:
            path_mismatch.append(f"{op_id}: {template} != {declared}")
    check("SDK 拼的路径与契约声明的路径一字不差", not path_mismatch, str(path_mismatch[:4]))

    print("\n[SDK 镜像的服务端常量没漂]")
    check("契约头名两边同名", sdk_client.CONTRACT_HEADER == api_contract.CONTRACT_HEADER,
          sdk_client.CONTRACT_HEADER)
    check("幂等键的请求头/重放头两边同名",
          sdk_client.IDEMPOTENCY_HEADER == idempotency.IDEMPOTENCY_HEADER
          and sdk_client.REPLAY_HEADER == idempotency.REPLAY_HEADER,
          f"{sdk_client.IDEMPOTENCY_HEADER}/{sdk_client.REPLAY_HEADER}")
    check("SDK 认的幂等 op 集 == 服务端注册表（多一个就会拿 400，少一个就白丢重放保护）",
          sdk_client.IDEMPOTENT_OPS == set(idempotency.SUPPORTED_SCOPES),
          f"多 {sorted(sdk_client.IDEMPOTENT_OPS - set(idempotency.SUPPORTED_SCOPES))} / "
          f"少 {sorted(set(idempotency.SUPPORTED_SCOPES) - sdk_client.IDEMPOTENT_OPS)}")
    check("市场 plural 两边同源", sdk_client.MARKET_PLURALS == set(api_contract.PLURAL_TO_TYPE),
          str(sdk_client.MARKET_PLURALS))
    check("SDK 认的契约版本就是服务端在发的那一版",
          api_contract.API_CONTRACT_VERSION in SDK_CONTRACT_VERSIONS,
          f"{sorted(SDK_CONTRACT_VERSIONS)} vs {api_contract.API_CONTRACT_VERSION}")

    print("\n[协商：不认识就停手]")
    client, opener, _sleeps = fake_client([
        json_response(200, {"ok": True, "contractVersion": "tt-server-api.v99",
                           "contractConsistent": True, "operationCount": 0,
                           "marketPlurals": ["mcp", "skills", "suites"],
                           "problems": []},
                      {api_contract.CONTRACT_HEADER: "tt-server-api.v99"}),
    ])
    try:
        client.negotiate()
        check("大版本不认识时 negotiate 抛 ContractMismatch", False, "没抛")
    except ContractMismatch as exc:
        check("大版本不认识时 negotiate 抛 ContractMismatch",
              exc.server_version == "tt-server-api.v99", str(exc))
    # 服务端自检没过 ⇒ 说明线上清单与实现已经漂了，客户端不该继续假设形状
    client, _, _ = fake_client([
        json_response(200, {"ok": True, "contractVersion": api_contract.API_CONTRACT_VERSION,
                           "contractConsistent": False, "problems": ["market.list: 路径不对"],
                           "operationCount": 0},
                     {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION}),
    ])
    try:
        client.negotiate()
        check("服务端自检有问题时也不许协商通过", False, "没抛")
    except ContractMismatch as exc:
        check("服务端自检有问题时也不许协商通过", "problems" in str(exc), str(exc)[:160])

    print("\n[429：读 Retry-After，且只重试安全的请求]")
    limited = json_response(429, {"ok": False, "code": "RATE_LIMITED", "message": "慢一点"},
                            {"Retry-After": "7",
                             api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION})
    ok = json_response(200, {"ok": True},
                       {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION})
    client, opener, sleeps = fake_client([limited, ok])
    result = client.call("GET", "health.check", auth=False)
    check("GET 撞 429 会按 Retry-After 退避后重打",
          result.data == {"ok": True} and sleeps == [7.0], f"sleeps={sleeps}")
    check("退避用的是服务端给的秒数，不是 SDK 自造的表",
          7.0 in sleeps, str(sleeps))
    client, opener, sleeps = fake_client([limited, limited, limited, limited])
    try:
        client.call("GET", "health.check", auth=False)
        check("超过 max_retry_waits 就抛 RateLimited，不无限等", False, "没抛")
    except RateLimited as exc:
        check("超过 max_retry_waits 就抛 RateLimited，不无限等",
              exc.retry_after == 7.0 and len(opener.requests) == 4,
              f"{len(opener.requests)} 次 {exc.retry_after}")

    # 关键安全口径：没带幂等键的 POST 不能自动重打——那会真的多写一笔
    client, opener, sleeps = fake_client([limited])
    try:
        client.call("POST", "auth.login", auth=False,
                    json_body={"email": "a@b.c", "password": "x"})
        check("不带幂等键的写请求撞 429 时**不**自动重打", False,
              f"打了 {len(opener.requests)} 次")
    except RateLimited:
        check("不带幂等键的写请求撞 429 时**不**自动重打",
              len(opener.requests) == 1 and sleeps == [],
              f"{len(opener.requests)} 次 {sleeps}")
    key = make_idempotency_key("sdk")
    client, opener, sleeps = fake_client([limited, ok])
    client.call("POST", "usage.report", json_body={"source": "desktop", "deviceId": "d",
                                                  "schemaVersion": 1, "rows": []},
                idempotency_key=key)
    check("带幂等键的写请求才允许自动重打（同一把键跟着重打走）",
          len(opener.requests) == 2
          and all(r["headers"].get(sdk_client.IDEMPOTENCY_HEADER.lower()) == key
                  for r in opener.requests),
          str([r["headers"].get("idempotency-key") for r in opener.requests])[:200])
    client, _, _ = fake_client([ok])
    try:
        client.call("POST", "auth.login", auth=False, json_body={}, idempotency_key="sdk-x")
        check("给没接幂等键的端点硬塞键 → SDK 本地就拒，不发请求", False, "居然发出去了")
    except SdkError as exc:
        check("给没接幂等键的端点硬塞键 → SDK 本地就拒，不发请求",
              "IDEMPOTENCY_SCOPE_UNSUPPORTED" in str(exc), str(exc)[:160])

    print("\n[401：自动刷一次，不循环刷]")
    expired = json_response(401, {"ok": False, "code": "TOKEN_EXPIRED", "message": "过期了"},
                            {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION})
    refreshed = json_response(200, {"ok": True, "accessToken": "new-access",
                                   "refreshToken": "new-refresh", "expiresIn": 900,
                                   "profile": {}, "quotaResolution": {}})
    me_ok = json_response(200, {"ok": True, "profile": {"uid": "u1"}})
    client, opener, _ = fake_client([expired, refreshed, me_ok], access_token="old-access",
                                    refresh_token="r1")
    data = client.me()
    check("access token 过期时自动刷一次并重打原请求",
          data["profile"]["uid"] == "u1" and len(opener.requests) == 3,
          f"{len(opener.requests)} 次")
    check("刷新后本地令牌跟着换（下一次直接带新的）",
          client.access_token == "new-access" and client.refresh_token == "new-refresh",
          client.access_token)
    check("重打时带的是刷新后的新令牌",
          opener.requests[-1]["headers"].get("authorization") == "Bearer new-access",
          str(opener.requests[-1]["headers"]))
    still_bad = json_response(401, {"ok": False, "code": "TOKEN_EXPIRED", "message": "还是过期"},
                              {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION})
    client, opener, _ = fake_client([still_bad, refreshed, still_bad],
                                    access_token="old", refresh_token="r1")
    try:
        client.me()
        check("刷完还是 401 就把原错抛出去（不许循环刷）", False, "没抛")
    except AuthError as exc:
        check("刷完还是 401 就把原错抛出去（不许循环刷）",
              exc.code == "TOKEN_EXPIRED" and len(opener.requests) == 3,
              f"{len(opener.requests)} 次 {exc.code}")
    client, opener, _ = fake_client([still_bad], access_token="old", refresh_token="r1")
    try:
        client.call("GET", "auth.me", auth=False)
        check("明确要求不带凭据的调用不许替它刷（auth=False 是调用方的决定）", False, "没抛")
    except AuthError:
        check("明确要求不带凭据的调用不许替它刷（auth=False 是调用方的决定）",
              len(opener.requests) == 1, str(len(opener.requests)))

    print("\n[错误映射：类型与状态一致，且契约声明的码都归好类]")
    for status, code, cls in [
        (401, "AUTH_REQUIRED", AuthError),
        (401, "INVALID_CREDENTIALS", AuthError),
        (403, "FORBIDDEN", ForbiddenError),
        (403, "ORG_BUDGET_EXCEEDED", ForbiddenError),
        (404, "MARKET_NOT_FOUND", NotFoundError),
        (409, "EMAIL_TAKEN", ConflictError),
        (409, "IDEMPOTENCY_CONFLICT", IdempotencyConflict),
        (429, "RATE_LIMITED", RateLimited),
        (500, "INTERNAL_ERROR", ApiError),
    ]:
        mapped = classify(status, code, "x", {}, b"")
        check(f"{status} {code} → {cls.__name__}",
              isinstance(mapped, cls) and mapped.status == status and mapped.code == code,
              type(mapped).__name__)
    unknown = classify(418, "TEAPOT", "x", {}, b"")
    check("契约里没有的码落回 ApiError 基类（不假装认识）",
          type(unknown) is ApiError, type(unknown).__name__)
    declared_codes = {str(item["code"]) for item in ERROR_CODES}
    unmapped_special = sorted(set(sdk_client.CODE_CLASSES) - declared_codes)
    check("SDK 特殊映射的每个机器码都在契约里声明过（映射不能私藏码）",
          not unmapped_special, str(unmapped_special))
    wrong_status = []
    for code, cls in sdk_client.CODE_CLASSES.items():
        declared = next((int(item["status"]) for item in ERROR_CODES
                         if str(item["code"]) == code), None)
        if declared is None:
            continue
        if not isinstance(classify(declared, code, "x", {}, b""), cls):
            wrong_status.append(f"{code}: 契约说 {declared}，SDK 映射成 {cls.__name__}")
    check("每个特殊映射在契约声明的状态下都真的成立",
          not wrong_status, str(wrong_status[:4]))
    check("classify 对 5xx 一律给 ServerError 级别的可重试语义",
          classify(503, "INTERNAL_ERROR", "x", {"Retry-After": "3"}, b"").retriable,
          "")

    print("\n[路径槽位与参数：填错要当场说，不要发给服务端拿 404]")
    client, opener, _ = fake_client([ok])
    try:
        client.call("GET", "orgs.quota.get")
        check("路径槽位没填就本地报错，不发一个注定 404 的请求", False, "居然发出去了")
    except SdkError as exc:
        check("路径槽位没填就本地报错，不发一个注定 404 的请求",
              not opener.requests and "槽位" in str(exc), str(exc)[:160])
    client, opener, _ = fake_client([ok])
    try:
        client.market_list("plugins")
        check("市场类型不在契约的三种里 → 本地 UnknownMarketPlural，不发请求", False, "发出去了")
    except UnknownMarketPlural as exc:
        check("市场类型不在契约的三种里 → 本地 UnknownMarketPlural，不发请求",
              not opener.requests, str(exc)[:120])
    client, opener, _ = fake_client([json_response(200, {"ok": True, "items": []},
                                                   {api_contract.CONTRACT_HEADER:
                                                    api_contract.API_CONTRACT_VERSION})])
    client.market_list("skills", q="搜索词", page=2, page_size=5, featured=True)
    query = urllib.parse.urlparse(opener.requests[0]["url"]).query
    check("查询参数按 URL 编码送出且 None 参数不带",
          "q=%E6%90%9C%E7%B4%A2%E8%AF%8D" in query and "page=2" in query
          and "featured=true" in query,
          query[:200])

    print("\n[digest 自洽：只说内容没被换过，不说验过签]")
    document = {"scopes": {"global": {"tiers": []}}, "contractVersion": "tt-tiers.v1"}
    from app.model_tiers import document_digest as server_digest  # noqa: E402
    check("tier_digest 与服务端 document_digest 逐字节同结果（规范化口径同源）",
          tier_digest(document) == server_digest(document),
          f"{tier_digest(document)[:16]} vs {server_digest(document)[:16]}")
    check("SDK 里带中文的文档也能算出同一个摘要（ensure_ascii=False 那句是真的）",
          tier_digest({"名称": "档位", "tiers": []}) == server_digest({"名称": "档位", "tiers": []}),
          "")
    tampered = json_response(200, {"ok": True, "digest": "0" * 64, "document": document,
                                   "signature": "jwt", "signingKid": "k1",
                                   "contractVersion": api_contract.API_CONTRACT_VERSION,
                                   "scope": "global", "revision": 1, "publishedAtMs": 1},
                             {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION})
    client, _, _ = fake_client([tampered])
    try:
        client.tiers_effective()
        check("digest 对不上时 SDK 拒绝采信档位文档", False, "居然返回了")
    except SdkError as exc:
        check("digest 对不上时 SDK 拒绝采信档位文档", "digest" in str(exc), str(exc)[:160])
    honest = json_response(200, {"ok": True, "digest": server_digest(document),
                                 "document": document, "signature": "jwt", "signingKid": "k1",
                                 "contractVersion": api_contract.API_CONTRACT_VERSION,
                                 "scope": "global", "revision": 1, "publishedAtMs": 1},
                           {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION})
    client, _, _ = fake_client([honest])
    check("digest 对得上就放行",
          client.tiers_effective()["revision"] == 1, str(honest.read())[:120])
    # 「不是验签」这句话必须真的写在代码里，否则调用方会以为 SDK 替他验过了
    check("check_tier_digest 的文档直说它不证明签名有效",
          "不证明签名有效" in (check_tier_digest.__doc__ or ""),
          (check_tier_digest.__doc__ or "")[:120])
    package_doc = open(os.path.join(ROOT, "app", "sdk", "__init__.py"),
                       encoding="utf-8").read()
    check("SDK 包文档写明验签不在本包范围（RS256 要 JOSE 库），不许被读成已验签",
          "RS256" in package_doc and "JOSE" in package_doc and "不是验签" in package_doc,
          package_doc[:160])

    print("\n[真打一轮：SDK 对着活服务端把主要流程走通]")
    live = Client(BASE)
    reg = live.register("sdk-user@test.local", ACCOUNT_PASSWORD, "SDK 用户")
    check("注册后 SDK 自己拿到令牌（调用方不用手存 access token）",
          live.access_token and reg["profile"]["email"] == "sdk-user@test.local",
          str(reg.get("profile", {}))[:160])
    top = api_contract.STABLE_OPERATIONS
    declared_register_keys = set(next(o for o in top if o["id"] == "auth.register")["response"])
    check("register 返回的顶层键与契约声明一致",
          set(reg) == declared_register_keys,
          f"多 {sorted(set(reg) - declared_register_keys)} / "
          f"缺 {sorted(declared_register_keys - set(reg))}")
    me = live.me()
    check("me 拿到本人档案", me["profile"]["nickname"] == "SDK 用户", str(me)[:160])
    uid = str(me["profile"]["uid"])
    # 行的字段名沿用引擎/桌面导出契约（UsageRowIn，extra=forbid），SDK 不另造一套
    row = {"sub": uid, "modelName": "sdk-model", "day": time.strftime("%Y-%m-%d", time.gmtime()),
           "origin": "web", "promptTokens": 30, "completionTokens": 20, "tokensUsed": 50,
           "calls": 1, "latencySamples": 1, "latencyMsTotal": 100}
    reported = live.report_usage([row], source="desktop", device_id="sdk-device",
                                 idempotency_key=make_idempotency_key("sdk"))
    check("上报用量走带键通道且回执声明了接受数",
          reported["accepted"] == 1 and reported["source"] == "desktop", str(reported)[:200])
    usage = live.usage_me("total")
    check("查自己的总量：SDK 返回的就是契约声明的那份 JSON",
          usage["totals"]["tokensUsed"] == 50 and usage["totals"]["calls"] == 1,
          str(usage.get("totals"))[:200])
    package = skill_zip("sdk-live-skill")
    published = live.publish_skill(package, summary="SDK 真打", category="效率",
                                   version="1.0.0", idempotency_key=make_idempotency_key("sdk"))
    item_id = str(published["item"]["id"])
    check("发布技能成功（multipart 由 SDK 手搓，不靠第三方库）",
          bool(item_id) and published["item"]["type"] == "skill", str(published)[:200])
    listed = live.market_list("skills")
    check("市场列表能看到刚发布的条目",
          item_id in [str(i.get("id")) for i in listed["items"]],
          str([i.get("id") for i in listed["items"]])[:200])
    detail = live.market_detail("skills", item_id)
    check("详情按清单说的挂在 item 里（不是顶层）",
          set(detail) == {"ok", "item"} and detail["item"]["id"] == item_id, str(detail)[:160])
    sha = (detail["item"]["versions"][0] or {}).get("sha256") or ""
    blob = live.download_package("skills", item_id, expect_sha256=sha)
    check("下载字节与上传的原包一致，并且校验了 sha256",
          blob == package and len(sha) == 64, f"{len(blob)} 字节 sha={sha[:12]}")
    try:
        live.download_package("skills", item_id, expect_sha256="f" * 64)
        check("sha256 不符时 SDK 拒绝交字节（客户端不该拿到没校验过的包）", False, "居然返回了")
    except SdkError as exc:
        check("sha256 不符时 SDK 拒绝交字节（客户端不该拿到没校验过的包）",
              "sha256" in str(exc), str(exc)[:160])
    # 档位表的**写入面**（/api/v1/admin/model-tiers/*）不在稳定清单里，SDK 故意没方法，
    # 所以这里用裸 HTTP 造前提；读回来的那条才是 SDK 的活。
    from app.model_tiers import TIER_CONTRACT_VERSION  # noqa: E402
    admin = Client(BASE)
    admin.login("admin@test.local", ADMIN_PASSWORD)
    admin_token = admin.access_token
    tier_document = {
        "contractVersion": TIER_CONTRACT_VERSION,
        "defaultTier": "basic",
        "byokAllowed": True,
        "models": [{"id": "sdk-model", "provider": "demo", "upstreamModel": "demo-sdk",
                    "displayName": "SDK 主力", "contextWindow": 128000,
                    "maxOutputTokens": 8000}],
        "tiers": {"basic": {"label": "基础", "chain": ["sdk-model"]}},
    }
    put_status, put_body = raw_http("PUT", "/api/v1/admin/model-tiers/global/-",
                                   json_body=tier_document, token=admin_token)
    check("用裸 HTTP 下发一份档位表（SDK 不覆盖管理写入面，这条前提只能这么造）",
          put_status == 200, f"{put_status} {put_body[:160]!r}")
    effective = live.tiers_effective()
    check("读档位文档时 SDK 自己比了 digest（服务端签的那份必须比得过）",
          check_tier_digest(effective) and "document" in effective,
          str(effective)[:160])
    check("SDK 算的摘要与服务端签的 digest 同源（真数据，不是自造 JSON）",
          effective["digest"] == tier_digest(effective["document"]),
          f"{effective['digest'][:16]} vs {tier_digest(effective['document'])[:16]}")
    revision = live.tier_revision(1)
    check("按 revision 取回历史版本也能过 digest 自检（/at 没有 contractVersion 那句是真的）",
          revision["revision"] == 1 and "contractVersion" not in revision
          and check_tier_digest(revision), str(revision)[:160])
    contract_doc = live.contract
    check("第一次调用已经协商过，契约文档留在客户端上",
          contract_doc is not None
          and contract_doc["contractVersion"] == api_contract.API_CONTRACT_VERSION,
          str(contract_doc)[:120])
    check("协商过的 plural 被客户端记住（之后按契约校验，不靠 SDK 里的常量）",
          live._market_plurals == set(api_contract.PLURAL_TO_TYPE), str(live._market_plurals))

    print("\n[真打：错误映射与重放]")
    try:
        Client(BASE).login("sdk-user@test.local", "wrong-password-here")
        check("密码错 → AuthError(INVALID_CREDENTIALS)", False, "居然登录了")
    except AuthError as exc:
        check("密码错 → AuthError(INVALID_CREDENTIALS)",
              exc.status == 401 and exc.code == "INVALID_CREDENTIALS", f"{exc.status} {exc.code}")
    try:
        live.market_detail("skills", "no-such-item")
        check("不存在的条目 → NotFoundError", False, "居然返回了")
    except NotFoundError as exc:
        check("不存在的条目 → NotFoundError", exc.status == 404, f"{exc.status} {exc.code}")
    created = admin.create_org("sdkorg", "SDK 组织", idempotency_key=make_idempotency_key("sdk"))
    check("管理员建组织成功", created["created"] is True and created["org"] == "sdkorg",
          str(created)[:200])
    quota = admin.set_org_quota("sdkorg", monthly_tokens=1000, enforce=True,
                               idempotency_key=make_idempotency_key("sdk"))
    check("改配额成功且吊销数可读出（0 = 不限制那个口径没被 SDK 改写）",
          quota["quota"]["monthlyTokens"] == 1000 and bool(quota["quota"]["enforce"]) is True
          and quota["quota"]["concurrentTasks"] == 0,
          str(quota.get("quota"))[:200])
    budget = admin.org_budget("sdkorg", "total")
    check("预算视图能读到（SDK 不做二次加工）",
          budget["org"] == "sdkorg" and "budget" in budget, str(budget)[:160])
    try:
        live.orgs()
        check("个人令牌打管理员面 → ForbiddenError(403)", False, "居然读到了")
    except ForbiddenError as exc:
        check("个人令牌打管理员面 → ForbiddenError(403)",
              exc.status == 403 and exc.code == "FORBIDDEN", f"{exc.status} {exc.code}")

    print("\n[真打：429 与退避在活服务上同样成立]")
    # 限流是按 (ip, 账号) 计数的，所以先注册一个专用账号，再拿错密码把它打到限流。
    limiter_account = "rate-limit@test.local"
    seeder = Client(BASE)
    seeder.register(limiter_account, ACCOUNT_PASSWORD, "限流专用")
    seeder.logout()
    hammer = Client(BASE, max_retry_waits=2, max_total_wait=30.0)
    hit_limit = 0
    exc = None
    for _ in range(12):
        hit_limit += 1
        try:
            hammer.login(limiter_account, "definitely-wrong-9")
        except AuthError:
            continue  # 错密码的 401 是预期，限流要的是它把计数吃掉
        except RateLimited as caught:
            exc = caught
            break
    check("连打登录真的被 429 挡下，且 retry_after 是服务端给的秒数",
          exc is not None and exc.retry_after > 0 and exc.code == "RATE_LIMITED",
          f"第 {hit_limit} 次 {getattr(exc, 'code', '没撞到限流')}")
    check("限流按 (ip, 账号) 计到第 11 次才挡（清单里 RATE_LIMITED 那句是真的）",
          hit_limit == 11, f"第 {hit_limit} 次")
    check("不带幂等键的写请求撞 429 时 SDK 没有替它重打（安全口径，不是偷懒）",
          exc is not None and exc.status == 429, str(exc))

    print("\n[登出：本地令牌一起清掉]")
    guest = Client(BASE)
    guest.register("sdk-guest@test.local", ACCOUNT_PASSWORD, "SDK 访客")
    had = guest.access_token
    guest.logout()
    check("登出后本地令牌清空（否则调用方以为还登着）",
          guest.access_token == "" and had != "", f"{guest.access_token!r}")

    print("\n[连接层：没送达才敢重打]")
    dead = urllib.error.URLError(ConnectionRefusedError("拒绝连接"))
    client, opener, sleeps = fake_client([dead, dead, ok])
    result = client.call("GET", "health.check", auth=False)
    check("连接失败可安全重打（服务端一定没跑过）",
          result.status == 200 and len(opener.requests) == 3, str(len(opener.requests)))
    client, opener, _ = fake_client([TransportError("连不上")] * 3, max_retry_waits=2)
    try:
        client.call("GET", "health.check", auth=False)
        check("连不上最终抛 TransportError，不伪装成服务端错误", False, "没抛")
    except TransportError:
        check("连不上最终抛 TransportError，不伪装成服务端错误",
              len(opener.requests) == 3, str(len(opener.requests)))
    client, opener, _ = fake_client([TransportError("x"), ok])
    client.call("POST", "auth.login", auth=False, json_body={"email": "a@b.c", "password": "x"})
    check("写请求在「没送达」时同样允许重打（这不是重试业务失败）",
          len(opener.requests) == 2, str(len(opener.requests)))

    print("\n[幂等重放：SDK 把回执与重放头一起交回调用方]")
    dup_key = make_idempotency_key("sdk")
    first = json_response(201, {"ok": True, "item": {"id": "i1"}},
                          {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION,
                           sdk_client.REPLAY_HEADER: "false"})
    replay = json_response(201, {"ok": True, "item": {"id": "i1"}},
                           {api_contract.CONTRACT_HEADER: api_contract.API_CONTRACT_VERSION,
                            sdk_client.REPLAY_HEADER: "true"})
    client, _, _ = fake_client([first])
    r1 = client.call("POST", "usage.report", json_body={"source": "desktop", "deviceId": "d",
                                                       "schemaVersion": 1, "rows": []},
                     idempotency_key=dup_key)
    client, _, _ = fake_client([replay])
    r2 = client.call("POST", "usage.report", json_body={"source": "desktop", "deviceId": "d",
                                                       "schemaVersion": 1, "rows": []},
                     idempotency_key=dup_key)
    check("Result.replayed 直接读重放头，调用方能分辨「真跑了」还是「拿到旧回执」",
          r1.replayed is False and r2.replayed is True, f"{r1.replayed}/{r2.replayed}")

    print("\n[输出泄漏总扫]")
    text = open(os.path.join(ROOT, "app", "sdk", "client.py"), encoding="utf-8").read()
    check("SDK 源码里没有硬编码口令/令牌",
          "admin-secret-123" not in text and "eyJ" not in text, "")
    check("SDK 不读环境变量、不落文件（客户端不该有隐藏状态）",
          "import os" not in text and "environ" not in text and "pathlib" not in text
          and "write_text" not in text and "shutil" not in text, "")
    check("SDK 不 import 服务端模块（第三方装了服务端代码就漏了）",
          "from .." not in text and "from app." not in text and "import app" not in text,
          "")

    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
