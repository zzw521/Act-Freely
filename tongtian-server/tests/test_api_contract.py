#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外 API 契约（D-19 / #154）自测。

跑法：`tongtian-server/.venv/Scripts/python.exe -X utf8 tests/test_api_contract.py`
判定只看 `^  ✓` 计数与末行「通过 N 项」，别只看退出码。

这里测的不是「契约文件写得全不全」，而是**契约说的每句话客户端真打一遍是不是这样**：
- 契约端点匿名可用（生产关掉 `/openapi.json` 之后，它是唯一对外的说明书）；
- 清单里每条稳定操作真打一次，返回的顶层键必须与声明一字不差；
- 声明的鉴权边界真拦得住：免凭据的不该 401，声明 bearer 的没令牌必须 401，
  声明 admin 的用个人令牌必须 403；
- 声明的速率限制数字，就是真会把人挡下的那个数字；
- 反向用例：把契约随手改坏十处，自检必须逐条报出来——不然「自检通过」是空话。

不覆盖：真实第三方按文档集成（外部条件）、引擎侧 `/api/chat` 与对话事件流
（在 `tongtian-tauri/engine/`，不在本窗口领地）。
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

_TMP = tempfile.mkdtemp(prefix="tt-api-contract-")
_SUB = tempfile.mkdtemp(prefix="tt-api-contract-sub-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
atexit.register(shutil.rmtree, _SUB, ignore_errors=True)

os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "api-contract-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "契约测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import api_contract  # noqa: E402
from app.api_contract import (  # noqa: E402
    API_CONTRACT_VERSION,
    CONTRACT_HEADER,
    COVERAGE_GAPS,
    ERROR_CODES,
    PROBE_MODES,
    RATE_LIMITS,
    STABLE_OPERATIONS,
    build_contract,
    find_problems,
    render_markdown,
)
from app.main import app  # noqa: E402
from app.model_tiers import TIER_CONTRACT_VERSION  # noqa: E402

_passed = 0
ADMIN_PASSWORD = "admin-secret-123"
ACCOUNT_PASSWORD = "secret123"
OPS = {str(op["id"]): op for op in STABLE_OPERATIONS}
ALICE_UID = ""  # 注册后填；top_level() 里的 usage.user 探针要用


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


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None,
         headers: dict | None = None, raw_body: bytes | None = None,
         content_type: str = ""):
    send = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        send["Content-Type"] = "application/json"
    elif raw_body is not None:
        # 市场发布只吃 multipart/form-data，urllib 不会手搓这个形状
        data = raw_body
        send["Content-Type"] = content_type
    if token:
        send["Authorization"] = f"Bearer {token}"
    if headers:
        send.update(headers)
    request = urllib.request.Request(BASE + path, data=data, headers=send, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except Exception as exc:  # 服务还没起来时先按 0 处理，由 start_server 重试
        return 0, {}, str(exc).encode("utf-8")


def head(headers: dict, name: str):
    """响应头按大小写不敏感取——uvicorn 不保证原样保留声明时的大小写。"""
    lowered = {str(key).lower(): value for key, value in headers.items()}
    return lowered.get(name.lower())


def payload(body) -> dict:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except Exception:
        return {}


def multipart(fields: dict, *, file_field: str, file_name: str, file_bytes: bytes,
              content_type: str = "application/zip"):
    """手搓 multipart/form-data：市场发布端点只吃这个形状，JSON 打不进去。"""
    boundary = "----w1ContractBoundary7M3kQ"
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


def skill_zip(name: str) -> bytes:
    """合法技能包 = ZIP 根目录一个 SKILL.md（front-matter 里带 name/version）。"""
    text = (
        f"---\nname: {name}\nversion: 1.0.0\ncategory: 效率\ntags: 自测,契约\n---\n\n"
        f"# {name}\n\n这是契约自测造的假技能，只用于把发布→详情→下载这条路真打一遍。\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", text)
    return buffer.getvalue()


def suite_zip(name: str) -> bytes:
    """合法套件包 = ZIP 根目录一个 plugin.json（name 必填，commands 可为字符串数组）。"""
    manifest = json.dumps(
        {"name": name, "description": "契约自测假的套件", "version": "1.0.0",
         "commands": ["contract-probe"], "tags": ["自测"]},
        ensure_ascii=False,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest)
    return buffer.getvalue()


def login(email: str, password: str = ADMIN_PASSWORD):
    return http("POST", "/api/v1/auth/login", json_body={"email": email, "password": password})


def must_login(email: str, password: str = ADMIN_PASSWORD) -> dict:
    status, _, body = login(email, password)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {body[:300]!r}")
    return payload(body)


def register(email: str, nickname: str):
    return http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": ACCOUNT_PASSWORD, "nickname": nickname},
    )


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(150):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


def declared_keys(op_id: str) -> set[str]:
    return {str(item) for item in OPS[op_id]["response"]}


def top_level(op_id: str) -> str:
    """把清单里的模板路径换成当前数据根下真能打通的具体路径。"""
    return {
        "health.check": "/health",
        "auth.jwks": "/.well-known/jwks.json",
        "api.contract": "/api/v1/contract",
        "api.contract.verify": "/api/v1/contract/verify",
        "auth.me": "/api/v1/auth/me",
        "tokens.revoked": "/api/v1/revoked",
        "market.categories": "/api/v1/categories?type=skill",
        "market.list": "/api/v1/skills",
        "usage.me": "/api/v1/usage/me?period=total",
        "usage.user": f"/api/v1/usage/users/{ALICE_UID}",
        "usage.departments": "/api/v1/admin/usage/departments?period=total",
        "orgs.list": "/api/v1/admin/orgs?period=total",
        "orgs.quota.get": "/api/v1/admin/orgs/acme/quota",
        "orgs.budget": "/api/v1/admin/orgs/acme/budget?period=total",
        "orgs.members": "/api/v1/admin/orgs/acme/members",
        "tiers.effective": "/api/v1/model-tiers/effective",
        "tiers.at": "/api/v1/model-tiers/at?revision=1",
    }[op_id]


TIER_DOCUMENT = {
    "contractVersion": TIER_CONTRACT_VERSION,
    "defaultTier": "basic",  # 用 auto 得配 autoPolicy；契约探针不关心选档策略
    "byokAllowed": True,
    "models": [
        {
            "id": "mid-main",
            "provider": "demo",
            "upstreamModel": "demo-mid",
            "displayName": "基础主力",
            "contextWindow": 128000,
            "maxOutputTokens": 8000,
        }
    ],
    "tiers": {"basic": {"label": "基础", "chain": ["mid-main"]}},
}


def usage_row(sub: str, org: str = "") -> dict:
    return {
        "sub": sub,
        "modelName": "mid-main",
        "day": time.strftime("%Y-%m-%d", time.gmtime()),
        "origin": "web",
        "org": org,
        "promptTokens": 100,
        "completionTokens": 20,
        "tokensUsed": 120,
        "calls": 1,
        "latencySamples": 1,
        "latencyMsTotal": 100,
    }


def drift(label: str, name: str, mutated, expect: str) -> None:
    """把契约的某一处改坏，自检必须报出对应问题；改完当场还原。"""
    original = getattr(api_contract, name)
    setattr(api_contract, name, mutated)
    try:
        problems = find_problems(app)
    finally:
        setattr(api_contract, name, original)
    check(label, any(expect in item for item in problems), json.dumps(problems, ensure_ascii=False)[:300])


def main() -> None:  # noqa: C901 - 自测按小节线性写，拆开反而丢上下文
    global ALICE_UID
    start_server()

    print("\n[契约端点匿名可用]")
    status, heads, body = http("GET", "/api/v1/contract")
    contract = payload(body)
    check("生产口径下 /api/v1/contract 免凭据可读", status == 200, f"{status} {body[:200]!r}")
    check("契约版本就是 tt-server-api.v1",
          contract.get("contractVersion") == API_CONTRACT_VERSION, str(contract.get("contractVersion")))
    check("响应顶层键与清单里对自身的声明一字不差",
          set(contract) == declared_keys("api.contract"),
          f"实际多 {sorted(set(contract) - declared_keys('api.contract'))} / "
          f"缺 {sorted(declared_keys('api.contract') - set(contract))}")
    check("响应头带契约版本号，第三方不用额外打一次",
          head(heads, CONTRACT_HEADER) == API_CONTRACT_VERSION, str(heads))
    check("子契约家族登记了网关那一份",
          contract["families"].get("gateway") == "gateway-contract.v1", str(contract["families"]))
    check("六个控制面子契约都在册",
          {key for key in contract["families"] if key != "gateway"}
          == {"api", "cli", "backup", "modelTiers", "auditCompliance", "orgQuota"},
          str(contract["families"]))
    check("清单条数与 operationCount 一致",
          contract["operationCount"] == len(STABLE_OPERATIONS), str(contract["operationCount"]))
    check("此刻契约与实现一致（problems 空）",
          contract["contractConsistent"] is True and contract["problems"] == [],
          json.dumps(contract["problems"], ensure_ascii=False)[:400])
    status, _, body = http("GET", "/api/v1/contract/verify")
    check("/api/v1/contract/verify 也报一致",
          status == 200 and payload(body)["ok"] is True and payload(body)["problems"] == [],
          f"{status} {body[:200]!r}")
    check("探针模式白名单本身是闭合的（清单里没有一个未登记的 probe）",
          {str(op["probe"]) for op in STABLE_OPERATIONS} <= PROBE_MODES,
          str({str(op["probe"]) for op in STABLE_OPERATIONS} - set(PROBE_MODES)))

    print("\n[通配路由没吃掉契约]")
    check("/api/v1/contract 不是被市场通配抢走后吐出的 MARKET_NOT_FOUND",
          status == 200 and contract.get("code") != "MARKET_NOT_FOUND", f"{body[:160]!r}")
    status, _, body = http("GET", "/api/v1/nonsense")
    check("未知市场类型确实 404（清单对 plural 的枚举是真的）",
          status == 404 and payload(body).get("code") == "MARKET_NOT_FOUND", f"{status} {body[:160]!r}")
    for plural in ("skills", "suites", "mcp"):
        status, _, body = http("GET", f"/api/v1/{plural}")
        check(f"{plural} 可匿名列表", status == 200, f"{status} {body[:120]!r}")

    print("\n[先把身份准备好]")
    admin = must_login("admin@test.local")
    admin_token = admin["accessToken"]
    reg_status, reg_heads, reg_body = register("alice@test.local", "爱丽丝")
    alice = payload(reg_body)
    user_token = alice["accessToken"]
    check("注册返回 201 且顶层键与声明一致",
          reg_status == 201 and set(alice) == declared_keys("auth.register"),
          f"{reg_status} 实际多 {sorted(set(alice) - declared_keys('auth.register'))} "
          f"少 {sorted(declared_keys('auth.register') - set(alice))}")
    status, _, body = http("GET", "/api/v1/auth/me", token=user_token)
    ALICE_UID = str(payload(body)["profile"]["uid"])
    check("从 /auth/me 拿到 uid（后面 usage.user 探针要用）", bool(ALICE_UID), ALICE_UID)
    check("profile 里的配额与令牌声明同一份数（不许两套口径）",
          payload(body)["profile"]["quota"] == json.loads(
              base64_json(user_token))["quota"],
          str(payload(body)["profile"].get("quota")))
    check("登录响应也带契约头", head(reg_heads, CONTRACT_HEADER) == API_CONTRACT_VERSION, str(reg_heads))

    print("\n[鉴权边界与声明一致]")
    anon_200: list[str] = []
    bearer_401: list[str] = []
    admin_403: list[str] = []
    for op_id, op in OPS.items():
        if op["method"] != "GET" or op["probe"] not in ("anon", "user", "admin"):
            continue
        path = top_level(op_id)
        if path is None:
            continue
        status, _, body = http("GET", path)
        code = payload(body).get("code")
        if op["auth"] == "none":
            if status == 200:
                anon_200.append(op_id)
            else:
                check(f"{op_id} 免凭据可拿到（没被偷偷加鉴权）", False, f"{status} {code} {body[:120]!r}")
        elif op["auth"] in ("bearer", "admin"):
            if status == 401 and code == "AUTH_REQUIRED":
                bearer_401.append(op_id)
            else:
                check(f"{op_id} 无令牌时 401 AUTH_REQUIRED", False, f"{status} {code} {body[:120]!r}")
    checkable = [
        op_id for op_id, op in OPS.items()
        if op["method"] == "GET" and op["probe"] in ("anon", "user", "admin")
    ]
    check("免凭据的 GET 全部真免凭据",
          len(anon_200) == len([op_id for op_id in checkable if OPS[op_id]["auth"] == "none"]),
          str(anon_200))
    check("声明要令牌的 GET 全部回了 401",
          len(bearer_401) == len([op_id for op_id in checkable if OPS[op_id]["auth"] in ("bearer", "admin")]),
          str(bearer_401))
    personal_403 = []
    for op_id, op in OPS.items():
        if op["method"] != "GET" or op["auth"] != "admin":
            continue
        status, _, body = http("GET", top_level(op_id), token=user_token)
        if status == 403 and payload(body).get("code") == "FORBIDDEN":
            personal_403.append(op_id)
        else:
            check(f"{op_id} 用个人令牌应 403 FORBIDDEN", False,
                  f"{status} {payload(body).get('code')}")
    check("管理员面用个人令牌一律 403（FORBIDDEN 而不是 401，说明令牌有效只是无权）",
          len(personal_403) >= 5, str(personal_403))

    print("\n[令牌流：状态码、键集合与一次性]")
    status, _, body = login("alice@test.local", ACCOUNT_PASSWORD)
    login_data = payload(body)
    check("登录 200 且顶层键与声明一字不差",
          status == 200 and set(login_data) == declared_keys("auth.login"),
          f"{status} 实际多 {sorted(set(login_data) - declared_keys('auth.login'))} "
          f"少 {sorted(declared_keys('auth.login') - set(login_data))}")
    check("登录响应带 quotaResolution（组织配额收敛的落点）",
          "quotaResolution" in login_data, str(login_data)[:200])
    status, _, body = http("POST", "/api/v1/auth/refresh", json_body={"refreshToken": login_data["refreshToken"]})
    refreshed = payload(body)
    check("刷新 200 且键集合与声明一致",
          status == 200 and set(refreshed) == declared_keys("auth.refresh"),
          f"{status} 实际多 {sorted(set(refreshed) - declared_keys('auth.refresh'))} "
          f"少 {sorted(declared_keys('auth.refresh') - set(refreshed))}")
    check("刷新发的是**新的** refresh 令牌", refreshed["refreshToken"] != login_data["refreshToken"])
    status, _, body = http("POST", "/api/v1/auth/refresh", json_body={"refreshToken": login_data["refreshToken"]})
    check("旧 refresh 重放被拒（一次性这件事是真的，清单别写错）",
          status == 401 and payload(body).get("code") == "INVALID_REFRESH", f"{status} {body[:160]!r}")
    status, _, body = http("POST", "/api/v1/auth/logout", json_body={"refreshToken": refreshed["refreshToken"]})
    logout_data = payload(body)
    check("登出 200 且键集合与声明一致（不需要 Authorization 头）",
          status == 200 and set(logout_data) == declared_keys("auth.logout"),
          f"{status} 实际 {sorted(set(logout_data))}")
    check("登出真吊销了那个会话", logout_data.get("revoked") is True, str(logout_data)[:200])
    status, _, body = http("POST", "/api/v1/auth/logout", json_body={"refreshToken": refreshed["refreshToken"]})
    check(
        "登出可重复：再来一次仍 200，revoked 仍为真（实测口径：它报的是「匹配到那个会话」，"
        "不是「本次发生了状态变化」，清单必须这么写）",
        status == 200 and payload(body).get("revoked") is True, f"{status} {body[:160]!r}"
    )
    check("吊销后旧 refresh 换不了新令牌",
          http("POST", "/api/v1/auth/refresh",
              json_body={"refreshToken": refreshed["refreshToken"]})[0] == 401)
    status, _, body = http("POST", "/api/v1/auth/logout", json_body={"nope": 1})
    check("写操作请求体形状错误走统一错误信封（多传字段也不行）",
          status == 422 and payload(body).get("code") == "VALIDATION_ERROR"
          and payload(body).get("ok") is False, f"{status} {body[:160]!r}")

    print("\n[把探针需要的数据准备好，再逐条真打]")
    status, _, body = http("PUT", "/api/v1/admin/model-tiers/global/-", json_body=TIER_DOCUMENT, token=admin_token)
    check("发布一份全局档位表（两条 tiers 探针的前提）",
          status == 200 and payload(body).get("revision") == 1, f"{status} {body[:200]!r}")
    status, _, body = http("POST", "/api/v1/admin/orgs",
                           json_body={"org": "ACME", "name": "阿斯米"}, token=admin_token)
    created = payload(body)
    check("orgs.create 的键集合与声明一致",
          status == 201 and set(created) == declared_keys("orgs.create"),
          f"{status} 实际多 {sorted(set(created) - declared_keys('orgs.create'))} "
          f"少 {sorted(declared_keys('orgs.create') - set(created))}")
    check("组织标识按契约规范化成小写（清单里写了键一律小写）",
          created.get("org") == "acme", str(created.get("org")))
    status, _, body = http("POST", "/api/v1/admin/orgs",
                           json_body={"org": "acme", "name": "阿斯米"}, token=admin_token)
    check("重复提交同组织返回 created=false 且仍是 201（清单声明天然幂等）",
          status == 201 and payload(body).get("created") is False, f"{status} {body[:160]!r}")
    status, _, body = http(
        "PUT",
        "/api/v1/admin/orgs/acme/quota",
        json_body={"monthlyTokens": 100000, "concurrentTasks": 4, "memberMonthlyTokens": 20000, "enforce": 0},
        token=admin_token,
    )
    put_data = payload(body)
    check("orgs.quota.put 的键集合与声明一致",
          status == 200 and set(put_data) == declared_keys("orgs.quota.put"),
          f"{status} 实际多 {sorted(set(put_data) - declared_keys('orgs.quota.put'))} "
          f"少 {sorted(declared_keys('orgs.quota.put') - set(put_data))}")
    check("写配额的回执直说吊销了几个会话",
          isinstance(put_data.get("sessionsRevoked"), int) and isinstance(put_data.get("activeSessions"), int),
          str(put_data)[:200])
    report_body = {"source": "engine", "deviceId": "dev-1", "schemaVersion": 1,
                   "rows": [usage_row(ALICE_UID, "acme")]}
    status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body, token=user_token)
    reported = payload(body)
    check("usage.report 的键集合与声明一致",
          status == 200 and set(reported) == declared_keys("usage.report"),
          f"{status} 实际多 {sorted(set(reported) - declared_keys('usage.report'))} "
          f"少 {sorted(declared_keys('usage.report') - set(reported))}")
    status, _, body = http("POST", "/api/v1/usage/report", json_body=report_body, token=user_token)
    check("同一条明细重报被接收但不双计（清单说的天然幂等）",
          status == 200 and payload(body).get("accepted") == 1, f"{status} {body[:160]!r}")
    status, _, body = http("GET", "/api/v1/usage/me?period=total", token=user_token)
    view = payload(body)
    check("重报后本人视图的总量仍是这一条的量（没双计）",
          status == 200 and view["totals"]["tokensUsed"] == 120 and view["totals"]["calls"] == 1,
          json.dumps(view.get("totals"), ensure_ascii=False)[:200])
    check("重报的是同一条时不去重计数也不虚增（engineDedupedRows 讲的是引擎行被桌面行盖住，"
          "这里只有引擎行，必须保持 0）",
          view["sources"]["engineDedupedRows"] == 0 and view["sources"]["engineRows"] >= 1,
          json.dumps(view.get("sources"), ensure_ascii=False)[:220])

    probed: list[str] = []
    skipped: list[str] = []
    for op_id, op in OPS.items():
        if op["method"] != "GET" or op["probe"] not in ("anon", "user", "admin"):
            skipped.append(op_id)
            continue
        token = {"anon": None, "user": user_token, "admin": admin_token}[op["probe"]]
        status, _, body = http("GET", top_level(op_id), token=token)
        data = payload(body)
        check(f"{op_id} 探针 200", status == 200, f"{status} {data.get('code')} {body[:160]!r}")
        check(f"{op_id} 顶层键与清单声明一字不差",
              set(data) == declared_keys(op_id),
              f"实际多 {sorted(set(data) - declared_keys(op_id))} / "
              f"缺 {sorted(declared_keys(op_id) - set(data))}")
        probed.append(op_id)

    print("\n[市场流程真打：发布 → 详情 → 下载]")
    # 这四条走的是另一条探针模式（要先有条目 ID、请求体不是 JSON），单独打一轮。
    # 打之前它们在清单里只是「声明」，客户端按声明写出来的下载代码其实没人验过。
    flow_probed: list[str] = []
    package = skill_zip("w1-contract-skill")
    form, ctype = multipart({"summary": "契约自测", "category": "效率", "version": "1.0.0"},
                            file_field="package", file_name="w1-contract-skill.zip",
                            file_bytes=package)
    status, _, body = http("POST", str(OPS["market.publishSkill"]["path"]), raw_body=form,
                           content_type=ctype, token=user_token)
    published = payload(body)
    check("market.publishSkill 真打 201 且顶层键与声明一致",
          status == 201 and set(published) == declared_keys("market.publishSkill"),
          f"{status} 实际 {sorted(published)} {body[:160]!r}")
    skill_id = str((published.get("item") or {}).get("id") or "")
    check("发布回执的 item 里能拿到 id（详情/下载两条探针要用）", bool(skill_id),
          str(published)[:200])
    flow_probed.append("market.publishSkill")

    status, _, body = http("GET", f"/api/v1/skills/{skill_id}")
    detail = payload(body)
    check("market.detail 真打 200 且顶层只有 ok 与 item（版本列表在 item 里，不在顶层）",
          status == 200 and set(detail) == declared_keys("market.detail"),
          f"{status} 实际 {sorted(detail)}")
    skill_item = detail.get("item") or {}
    check("技能详情的 item 键集合与清单声明一字不差",
          set(skill_item) == set(OPS["market.detail"]["nested"]["item"]),
          f"多 {sorted(set(skill_item) - set(OPS['market.detail']['nested']['item']))} / "
          f"缺 {sorted(set(OPS['market.detail']['nested']['item']) - set(skill_item))}")
    check("summary 里那句「技能的 packageInfo 只有 fileCount/files」是真的",
          set(skill_item.get("packageInfo") or {}) == {"fileCount", "files"},
          str(skill_item.get("packageInfo"))[:160])

    status, dl_heads, blob = http("GET", f"/api/v1/skills/{skill_id}/download")
    check("market.download 真打 200，回的是原样的 ZIP 字节（客户端要能直接落盘校验）",
          status == 200 and blob[:2] == b"PK" and len(blob) == len(package),
          f"{status} {len(blob)} != {len(package)}")
    check("下载响应不是 JSON（声明的 <二进制流> 不能说谎）",
          "json" not in str(head(dl_heads, "Content-Type") or "").lower(),
          str(head(dl_heads, "Content-Type")))
    flow_probed += ["market.detail", "market.download"]

    suite_package = suite_zip("w1-contract-suite")
    sform, stype = multipart({"summary": "契约自测套件", "version": "1.0.0"},
                             file_field="package", file_name="w1-contract-suite.zip",
                             file_bytes=suite_package)
    status, _, body = http("POST", str(OPS["market.publishSuite"]["path"]), raw_body=sform,
                           content_type=stype, token=user_token)
    suite_published = payload(body)
    check("market.publishSuite 真打 201 且顶层键与声明一致",
          status == 201 and set(suite_published) == declared_keys("market.publishSuite"),
          f"{status} 实际 {sorted(suite_published)} {body[:160]!r}")
    suite_id = str((suite_published.get("item") or {}).get("id") or "")
    status, _, body = http("GET", f"/api/v1/suites/{suite_id}")
    suite_item = (payload(body).get("item") or {})
    check("summary 里那句「套件另有 commands」是真的（item = 声明的基线键 + commands）",
          set(suite_item) == set(OPS["market.detail"]["nested"]["item"]) | {"commands"},
          f"多 {sorted(set(suite_item) - set(OPS['market.detail']['nested']['item']))} / "
          f"缺 {sorted(set(OPS['market.detail']['nested']['item']) - set(suite_item))}")
    check("套件的 packageInfo 比技能多 skillCount/dataLinkCount（两种类型的键集合确实不同源）",
          set(suite_item.get("packageInfo") or {})
          == {"fileCount", "files", "skillCount", "dataLinkCount"},
          str(suite_item.get("packageInfo"))[:200])
    flow_probed.append("market.publishSuite")

    check("真打了绝大多数稳定操作，剩下几条只声明不真打",
          len(probed) >= 15, f"通用探针 {len(probed)} 条；未打 {sorted(skipped)}")
    unprobed = set(skipped) - set(flow_probed)
    check("未真打的那些全是在「准备数据」里真打过的写操作",
          unprobed == {"auth.register", "auth.login", "auth.refresh", "auth.logout",
                       "usage.report", "orgs.create", "orgs.quota.put"},
          f"多 {sorted(unprobed - {'auth.register', 'auth.login', 'auth.refresh', 'auth.logout',
                                   'usage.report', 'orgs.create', 'orgs.quota.put'})} / "
          f"少 {sorted({'auth.register', 'auth.login', 'auth.refresh', 'auth.logout',
                        'usage.report', 'orgs.create', 'orgs.quota.put'} - unprobed)}")
    check("清单里 28 条稳定操作每条都被真打过（通用 GET + 市场流程 + 准备数据的写操作）",
          len(probed) + len(flow_probed) + len(unprobed) == len(OPS),
          f"{len(probed)} + {len(flow_probed)} + {len(unprobed)} != {len(OPS)}")
    check("health 给出版本号", payload(http("GET", "/health")[2]).get("version") is not None)
    check("jwks 给的是非空 keys 数组",
          isinstance(payload(http("GET", "/.well-known/jwks.json")[2]).get("keys"), list)
          and len(payload(http("GET", "/.well-known/jwks.json")[2])["keys"]) >= 1)
    check("orgs.list 里能看到刚建的 acme",
          "acme" in [item.get("org") for item in payload(
              http("GET", top_level("orgs.list"), token=admin_token)[2])["orgs"]])
    check("tiers.effective 给的是签名后的文档（客户端要验签才采信）",
          {"signature", "signingKid", "digest"} <= set(payload(
              http("GET", top_level("tiers.effective"), token=user_token)[2])))
    check("tiers.at 按 revision 取回同一份摘要（可复现）",
          payload(http("GET", top_level("tiers.at"), token=user_token)[2])["digest"]
          == payload(http("GET", top_level("tiers.effective"), token=user_token)[2])["digest"])
    tier_status, tier_heads, tier_body = http(
        "GET", "/api/v1/model-tiers/effective", token=user_token
    )
    check("档位表响应带 ETag 与 X-TT-Tier-Revision 头",
          tier_status == 200 and bool(head(tier_heads, "ETag"))
          and head(tier_heads, "X-TT-Tier-Revision") == str(payload(tier_body)["revision"]),
          f"{tier_status} ETag={head(tier_heads, 'ETag')!r}")
    check("命中 ETag 的条件请求必须回 304（清单 conditional 那句话说得出）",
          http("GET", "/api/v1/model-tiers/effective", token=user_token,
               headers={"If-None-Match": str(head(tier_heads, "ETag"))})[0] == 304,
          str(http("GET", "/api/v1/model-tiers/effective", token=user_token,
                   headers={"If-None-Match": str(head(tier_heads, "ETag"))}))[:160])
    check("换一个不匹配的 ETag 仍回 200（304 不是无条件甩的）",
          http("GET", "/api/v1/model-tiers/effective", token=user_token,
               headers={"If-None-Match": '"tiers-global-999999"'}
               )[0] == 200)

    print("\n[契约声明的枚举与实现同源]")
    fresh = build_contract(app)
    check("清单里市场 plural 就是 catalog 的三种",
          fresh["marketPlurals"] == ["mcp", "skills", "suites"], str(fresh["marketPlurals"]))
    check("period 取值三处一致（契约 / 用量 / 组织预算）",
          fresh["conventions"]["periods"]["values"] == ["daily", "monthly", "total"]
          and api_contract.USAGE_PERIODS == api_contract.ORG_QUOTA_PERIODS,
          str(fresh["conventions"]["periods"]))
    status, _, body = http("GET", "/api/v1/usage/me?period=weekly", token=user_token)
    check("非法 period 报 USAGE_INVALID_PERIOD（契约说了只收三个值）",
          status == 400 and payload(body).get("code") == "USAGE_INVALID_PERIOD", f"{status} {body[:160]!r}")

    print("\n[速率限制：数字就是真挡人的数字]")
    pair = next(item for item in RATE_LIMITS if item["id"] == "login-ip-account")
    blocked_at = 0
    for attempt in range(1, pair["limit"] + 4):
        status, heads, body = login("ghost@test.local", "wrong-password")
        if status == 429:
            blocked_at = attempt
            retry = head(heads, "Retry-After")
            check("挡下来时给 Retry-After 秒数", retry is not None and int(retry) >= 1, str(heads))
            check("429 也走统一错误信封",
                  payload(body).get("ok") is False and payload(body).get("code") == "RATE_LIMITED",
                  f"{body[:160]!r}")
            check("429 响应也带契约版本头", head(heads, CONTRACT_HEADER) == API_CONTRACT_VERSION, str(heads))
            break
    check(f"第 {pair['limit'] + 1} 次才被挡（契约声明每 IP+账号 {pair['limit']}/{pair['windowSeconds']}s）",
          blocked_at == pair["limit"] + 1, f"实际第 {blocked_at} 次")
    check("清单里这条限制挂在 auth.login 上",
          "login-ip-account" in OPS["auth.login"].get("rateLimits", ()),
          str(OPS["auth.login"].get("rateLimits")))

    print("\n[自检抓得到漂移]")
    ops = list(STABLE_OPERATIONS)
    drift("路径写错会被抓", "STABLE_OPERATIONS",
          tuple([dict(ops[0], path="/api/v1/does-not-exist")] + ops[1:]), "活路由表里没有")
    drift("免凭据声明与 Depends 冲突会被抓", "STABLE_OPERATIONS",
          tuple([dict(op, auth="none") if op["id"] == "auth.me" else op for op in ops]),
          "声明免凭据，但挂了依赖")
    drift("编一个源码里不存在的错误码会被抓", "STABLE_OPERATIONS",
          tuple([dict(op, errors=list(op.get("errors", ())) + ["CODE_THAT_NEVER_EXISTED"])
                 if op["id"] == "auth.login" else op for op in ops]), "已经找不到了")
    drift("引用不存在的速率限制会被抓", "STABLE_OPERATIONS",
          tuple([dict(op, rateLimits=["no-such-limit"]) if op["id"] == "auth.login" else op for op in ops]),
          "未定义的速率限制")
    drift("写操作漏标 idempotency 会被抓", "STABLE_OPERATIONS",
          tuple([{key: value for key, value in op.items() if key != "idempotency"}
                 if op["id"] == "orgs.create" else op for op in ops]),
          "写操作缺 idempotency")
    drift("写操作漏声明 idempotent 会被抓", "STABLE_OPERATIONS",
          tuple([{key: value for key, value in op.items() if key != "idempotent"}
                 if op["id"] == "orgs.create" else op for op in ops]), "写操作缺 idempotent")
    drift("probe 写错会被抓", "STABLE_OPERATIONS",
          tuple([dict(op, probe="maybe") if op["id"] == "health.check" else op for op in ops]),
          "不是已知探针模式")
    drift("操作 id 重复会被抓", "STABLE_OPERATIONS", tuple(ops) + (dict(ops[0]),), "操作 id 重复")
    drift("速率数字与源码不一致会被抓", "RATE_LIMITS",
          tuple([dict(item, keyTemplate=str(item["keyTemplate"]).replace(", 10, 300", ", 999, 300"))
                 for item in RATE_LIMITS]), "找不到")
    drift("错误码状态写错会被抓", "ERROR_CODES",
          tuple([dict(item, status=418) if item["code"] == "RATE_LIMITED" else item for item in ERROR_CODES]),
          "契约写 418")
    drift("两处 PERIODS 分家会被抓", "USAGE_PERIODS", ("daily", "monthly"), "周期口径分家了")
    check("十处改坏全部还原后，自检重新为空（上面是真抓不是常绿）",
          find_problems(app) == [] and build_contract(app)["contractConsistent"] is True)

    print("\n[覆盖缺口如实写在契约里]")
    gaps = {str(item["area"]): item for item in COVERAGE_GAPS}
    check("对话与事件流直说不在本契约内",
          gaps["对话与事件流"]["status"] == "not-covered" and "/api/chat" in gaps["对话与事件流"]["note"])
    check("真实第三方集成直说未验收", gaps["真实第三方集成"]["status"] == "not-accepted")
    check("OpenAPI 生产关闭这件事写在契约里", gaps["OpenAPI"]["status"] == "production-off")
    check("协作面标成 internal（不把没收口的形状许成稳定）",
          gaps["协作与工单面"]["status"] == "internal")
    # 刀 B 之后这两件事分开断言：机制**已强制**（status=enforced），但覆盖仍是**partial**
    # （签发令牌的路径有意不接、新版本发布与 mcp 发布还没接）。把它们划等号是刀 A 那版
    # 的写法，当时两边都还是 declared-not-enforced，看着像一致其实是巧合。
    check("幂等机制已强制（清单状态从 declared-not-enforced 翻成 enforced）",
          IDEMPOTENCY_STATUS == "enforced", IDEMPOTENCY_STATUS)
    check("覆盖缺口仍如实标 partial，不因为接了就写成全量",
          gaps["幂等键"]["status"] == "partial", str(gaps["幂等键"]["status"]))
    check("缺口那条 note 点名了哪三处有意不接（读者能据此决定重试安全）",
          all(needle in gaps["幂等键"]["note"]
              for needle in ("register", "versions", "/api/v1/mcp")),
          gaps["幂等键"]["note"][:200])
    check("supportedOperations 与清单里标 supported 的 op 是同一批（不是两处手抄）",
          sorted(str(op["id"]) for op in STABLE_OPERATIONS
                 if str(op.get("idempotency")) == "supported")
          == list(IDEMPOTENCY_SUPPORTED), str(IDEMPOTENCY_SUPPORTED))
    check("每条写操作都标了 supported / not-supported（二值，不许空着）",
          all(str(op.get("idempotency")) in ("supported", "not-supported")
              for op in STABLE_OPERATIONS if op["method"] != "GET"), "")

    print("\n[文档生成器与命令行入口]")
    markdown = render_markdown(fresh)
    check("markdown 里有契约版本标题",
          f"# 通天办公服务端 · 对外 API 契约（{API_CONTRACT_VERSION}）" in markdown)
    check("markdown 每条稳定操作一行", all(f"| `{op_id}` |" in markdown for op_id in OPS),
          str(markdown.count("| `")))
    check("markdown 含「不覆盖什么」清单", "## 不覆盖什么" in markdown and "not-accepted" in markdown)
    check("markdown 声明自己是生成的、别手改", "不要手改" in markdown)
    env = dict(os.environ, TT_SERVER_DATA_DIR=_SUB)
    interpreter = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    done = subprocess.run([interpreter, "-X", "utf8", "-m", "app.api_contract", "--verify"],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    check("--verify 子进程退出码 0", done.returncode == 0,
          f"{done.returncode} {done.stdout[:200]} {done.stderr[:200]}")
    verify_out = payload(done.stdout)
    check("--verify 输出说 ok 且无 problems",
          verify_out.get("ok") is True and verify_out.get("problems") == [], done.stdout[:200])
    emit = subprocess.run([interpreter, "-X", "utf8", "-m", "app.api_contract", "--emit-markdown"],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    check("--emit-markdown 子进程可用且长度像一份文档",
          emit.returncode == 0 and len(emit.stdout) > 3000, f"{emit.returncode} {len(emit.stdout)}")

    print("\n[输出泄漏总扫]")
    dumped = json.dumps(fresh, ensure_ascii=False)
    for secret in (ADMIN_PASSWORD, ACCOUNT_PASSWORD, "api-contract-test-secret", "BEGIN PRIVATE KEY",
                   user_token, admin_token):
        check(f"契约全文不含「{secret[:8]}…」", secret not in dumped, "命中了敏感串")
    check("契约里没有任何 /docs 或 /openapi.json 被许成稳定操作",
          not any(str(op["path"]).lower().endswith(("/docs", "/openapi.json")) for op in STABLE_OPERATIONS))

    print(f"\n通过 {_passed} 项")


# 这几样在文件末尾的常量区里再绑一次，方便「幂等键」那一刀改状态时只动一处
from app.api_contract import IDEMPOTENCY as _IDEMPOTENCY  # noqa: E402

IDEMPOTENCY_STATUS = str(_IDEMPOTENCY["status"])
IDEMPOTENCY_SUPPORTED = list(_IDEMPOTENCY["supportedOperations"])


def base64_json(token: str) -> str:
    """解 JWT 的 payload 段看声明；只读，不做验签（验签在档位表/锚点那两族里测）。"""
    raw = token.split(".")[1]
    raw += "=" * (-len(raw) % 4)
    import base64

    return base64.urlsafe_b64decode(raw).decode("utf-8")


if __name__ == "__main__":
    main()
