#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 D-23 服务端半边：组织与两级配额（组织总额 + 成员级三档来源 + 拦停裁决）。

覆盖六件「只做个人配额」时做不到的事：

1) **两级落点**：令牌里的 `quota` 走「个人显式覆盖 > 组织成员默认 > 角色缺省」，
   且响应如实交代每个数字从哪一档来（`quotaResolution.source`）；
2) **组织预算参与签发**：`enforce` 打开后成员月度上限收敛到组织剩余，用尽即**拒发令牌**；
   关掉后与升级前逐字节相同（旧卷升级不改行为的断言）；
3) **0 的陷阱**：本仓库 0 = 不限制。剩余为 1 时收敛出 1（不是 0），剩余为 0 时**不发令牌**，
   绝不用 0 表达「花完了」——那等于把限制取消；
4) **同源**：组织已用与部门报表、个人明细用同一套行过滤（engine 重复桌面的行、
   经网关的副本行都不双计），自测直接拿 `/api/v1/admin/usage/departments` 对数；
5) **口径缺口如实交出**：预算判定只看得见**已上报**的用量；声明是**签发时刻**快照；
   并发收敛的是单人声明不是组织同时任务总数；管理员豁免拦停——四条都在响应里；
6) **管理面与 CLI**：五个端点的角色边界、入参校验、审计入链、会话吊销范围，
   以及 `python -m app.cli orgs …` 的信封、`--monthly-tokens 0` 真能把 0 发出去。

用法: python tests/test_org_quota.py
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-orgq-")
_OLD = tempfile.mkdtemp(prefix="tt-orgq-old-")
_OUT = tempfile.mkdtemp(prefix="tt-orgq-out-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
atexit.register(shutil.rmtree, _OLD, ignore_errors=True)
atexit.register(shutil.rmtree, _OUT, ignore_errors=True)

os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "orgq-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "配额测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app.auth import USER_QUOTA  # noqa: E402
from app.main import app  # noqa: E402
from app.org_quota import ORG_QUOTA_CONTRACT  # noqa: E402

_passed = 0
ADMIN_PASSWORD = "admin-secret-123"
ACCOUNT_PASSWORD = "secret123"
CAPTURED: list[str] = []


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


def http(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    token: str | None = None,
):
    send = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        send["Content-Type"] = "application/json"
    if token:
        send["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=send, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def login(email: str, password: str = ADMIN_PASSWORD) -> dict:
    """返回整份登录响应（要看 quotaResolution，不只拿令牌）。"""
    status, body = http("POST", "/api/v1/auth/login", json_body={"email": email, "password": password})
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {body[:300]!r}")
    return payload(body)


def login_status(email: str, password: str = ACCOUNT_PASSWORD) -> tuple[int, dict]:
    status, body = http("POST", "/api/v1/auth/login", json_body={"email": email, "password": password})
    return status, payload(body)


def register(email: str, nickname: str) -> dict:
    status, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": ACCOUNT_PASSWORD, "nickname": nickname},
    )
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {body[:200]!r}")
    return payload(body)


def claims(token: str) -> dict:
    """只解 payload 看声明；签名由服务端自己保证，本文件另有验签族。"""
    raw = token.split(".")[1]
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def report(token: str, sub: str, org: str, prompt: int, completion: int, *,
           source: str = "engine", origin: str = "web", model: str = "demo-mid",
           device: str = "dev-1", day: str | None = None) -> int:
    status, body = http(
        "POST",
        "/api/v1/usage/report",
        json_body={
            "source": source,
            "deviceId": device,
            "schemaVersion": 1,
            "rows": [
                {
                    "sub": sub,
                    "modelName": model,
                    "day": day or today(),
                    "origin": origin,
                    "org": org,
                    "promptTokens": prompt,
                    "completionTokens": completion,
                    "tokensUsed": prompt + completion,
                    "calls": 1,
                    "latencySamples": 1,
                    "latencyMsTotal": 100,
                }
            ],
        },
        token=token,
    )
    if status != 200:
        raise AssertionError(f"用量上报失败 {status}: {body[:200]!r}")
    return status


def cli(*argv: str, env: dict | None = None) -> tuple[int, dict, str]:
    run_env = dict(os.environ)
    run_env.pop("TT_SERVER_URL", None)
    run_env["TT_SERVER_URL"] = BASE
    run_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "app.cli", *[str(a) for a in argv]],
        cwd=ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    CAPTURED.append(proc.stdout or "")
    CAPTURED.append(proc.stderr or "")
    envelope = None
    for stream in (proc.stdout, proc.stderr):
        for line in reversed((stream or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict) and "contractVersion" in parsed:
                envelope = parsed
                break
        if envelope:
            break
    return proc.returncode, envelope or {}, (proc.stdout + proc.stderr)


def expect_ok(name: str, argv: tuple, env: dict | None = None) -> dict:
    rc, envelope, text = cli(*argv, env=env)
    check(name, rc == 0 and envelope.get("ok") is True, f"rc={rc} {text[:300]}")
    return envelope.get("data") or {}


def expect_cli_fail(name: str, argv: tuple, rc: int, code: str, env: dict | None = None) -> None:
    got, envelope, text = cli(*argv, env=env)
    problems = envelope.get("problems") or [{}]
    check(
        name,
        got == rc
        and envelope.get("ok") is False
        and envelope.get("exitCode") == rc
        and str(problems[0].get("code")) == code,
        f"rc={got} {text[:300]}",
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


def main() -> None:
    server = start_server()
    try:
        admin = login("admin@test.local")
        admin_token = admin["accessToken"]

        print("\n[新表就位与旧卷升级顺序]")
        conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
        conn.row_factory = sqlite3.Row
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        check("org_quotas 表已随启动就位", "org_quotas" in tables, sorted(tables)[:8])
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(org_quotas)")}
        check(
            "配额表列齐（含 enforce 与 updated_by）",
            {
                "org_id",
                "monthly_tokens",
                "concurrent_tasks",
                "member_monthly_tokens",
                "member_concurrent_tasks",
                "enforce",
                "updated_at_ms",
                "updated_by",
            }
            <= columns,
            sorted(columns),
        )
        check("库仍是 WAL（新表不影响既有模式）",
              str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        check("新表建完库仍自洽", integrity == "ok", integrity)
        conn.close()

        # 旧卷不再手工抄一遍 DDL：抄一份就会和真实 SCHEMA 漂移，测过了其实什么都没测。
        # 改成把在线库整份快照出来、只删掉 org_quotas —— 这才是「升级前一刻的历史卷」。
        old_db = os.path.join(_OLD, "market.db")
        live = sqlite3.connect(os.path.join(_TMP, "market.db"))
        with sqlite3.connect(old_db) as copy:
            live.backup(copy)
        live.close()
        before = sqlite3.connect(old_db)
        before.row_factory = sqlite3.Row
        before.execute("DROP TABLE org_quotas")
        first_uid = str(before.execute("SELECT uid FROM users LIMIT 1").fetchone()["uid"])
        before.execute(
            "INSERT INTO usage_rollup (source, device_id, sub, org_id, model_name, day,"
            " origin, prompt_tokens, completion_tokens, calls, reported_at_ms)"
            " VALUES ('engine', 'd-old', ?1, '', 'm-old', '2026-01-01', 'unknown', 5, 5, 1, 1)",
            (first_uid,),
        )
        legacy_users = int(before.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        legacy_usage = int(before.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()[0])
        legacy_tables = {
            str(row["name"])
            for row in before.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        before.commit()
        before.close()
        check("造出来的旧卷确实没有 org_quotas（不然后面的断言是空的）",
              "org_quotas" not in legacy_tables, sorted(legacy_tables)[:10])
        check("旧卷里有账号也有用量（升级不许凭空造数据）",
              legacy_users >= 1 and legacy_usage == 1, f"{legacy_users} 账号 / {legacy_usage} 行")

        # init_db() 不接路径参数，它读全局 settings —— 只能临时把 settings 指到旧卷。
        # settings 是 frozen dataclass，但各模块是「按对象引用」共享同一个实例，
        # 所以绕过 __setattr__ 改的就是大家看到的那一份；改完必须在 finally 里还原，
        # 这个进程里还跑着真正的在线服务，指错了就把自己的库升坏了。
        from app.config import settings
        from app.db import init_db

        saved = (settings.data_dir, settings.db_path, settings.packages_dir)
        try:
            object.__setattr__(settings, "data_dir", Path(_OLD))
            object.__setattr__(settings, "db_path", Path(old_db))
            object.__setattr__(settings, "packages_dir", Path(_OLD) / "packages")
            init_db()
        finally:
            for name, value in zip(("data_dir", "db_path", "packages_dir"), saved):
                object.__setattr__(settings, name, value)
        check("旧卷升级结束后指针回到在线库（没把自己的库指丢）", settings.db_path == saved[1],
              str(settings.db_path))
        upgraded = sqlite3.connect(old_db)
        upgraded.row_factory = sqlite3.Row
        names = {
            str(row["name"])
            for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        check("旧卷升级后 org_quotas 出现（整表新建，不需要补列）", "org_quotas" in names, sorted(names)[:10])
        check(
            "旧卷既有行一条没动",
            int(upgraded.execute("SELECT COUNT(*) FROM users").fetchone()[0]) == legacy_users
            and int(upgraded.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()[0]) == legacy_usage,
            f"{legacy_users}/{legacy_usage}",
        )
        check(
            "旧卷升级后 org_quotas 是空的（没有「顺手塞一行默认额度」）",
            int(upgraded.execute("SELECT COUNT(*) FROM org_quotas").fetchone()[0]) == 0,
        )
        upgraded_cols = {str(row["name"]) for row in upgraded.execute("PRAGMA table_info(org_quotas)")}
        upgraded.close()
        check("旧卷升级后的列与在线库一字不差（不靠抄 DDL 蒙对）",
              upgraded_cols == columns, sorted(upgraded_cols ^ columns))

        print("\n[默认态：不开拦停与升级前逐字节相同]")
        alice = register("alice@test.local", "爱丽丝")
        bob = register("bob@test.local", "鲍勃")
        carol = register("carol@test.local", "卡罗尔")
        alice_uid = str(alice["profile"]["uid"])
        bob_uid = str(bob["profile"]["uid"])
        carol_uid = str(carol["profile"]["uid"])
        claims_a = claims(alice["accessToken"])
        check("无组织账号的声明仍是角色缺省", claims_a["quota"] == USER_QUOTA, claims_a["quota"])
        check(
            "无组织时 quotaResolution 说「没拦停、没收敛、没归属到任何组织」",
            alice["quotaResolution"]["enforced"] is False
            and alice["quotaResolution"]["clamped"] is False
            and alice["quotaResolution"]["org"] == ""
            and alice["quotaResolution"]["reasons"] == [],
            alice.get("quotaResolution"),
        )
        check("签发成功的响应里没有 blocked 这个字段（挡住了就不会有令牌）",
              "blocked" not in alice["quotaResolution"], sorted(alice["quotaResolution"]))
        check("profile 里的 quota 与令牌声明一致", alice["profile"]["quota"] == claims_a["quota"])

        print("\n[角色边界与入参校验]")
        status, body = http("GET", "/api/v1/admin/orgs")
        check("未登录列组织 401", status == 401 and payload(body)["code"] == "AUTH_REQUIRED", f"{status} {body[:200]}")
        status, body = http("GET", "/api/v1/admin/orgs", token=alice["accessToken"])
        check("普通用户列组织 403", status == 403, f"{status} {body}")
        for path in ("/api/v1/admin/orgs/acme/quota", "/api/v1/admin/orgs/acme/budget",
                     "/api/v1/admin/orgs/acme/members"):
            check(f"{path.rsplit('/', 1)[-1]} 也要管理员", http("GET", path, token=alice["accessToken"])[0] == 403)
        # 大小写口径：与既有 _normalize_org（app/auth.py:163）一致——先转小写再校验，不是拒绝。
        # 不改约定（改了会和「给用户指派组织」那条路径分裂成两套规则），
        # 但必须验「ACME 与 acme 是同一行」，否则预算会被大小写劈成两本账（在下一段验）。
        # 这里真正该拒的是能穿出路径的字符与空标识。
        status, body = http("POST", "/api/v1/admin/orgs", json_body={"org": "../etc"}, token=admin_token)
        check("组织标识带路径分隔被拒", status == 400, f"{status}")
        status, body = http("POST", "/api/v1/admin/orgs", json_body={"org": ""}, token=admin_token)
        check("空标识被拒", status == 400 and payload(body)["code"] == "INVALID_ORG", f"{status}")
        status, body = http(
            "GET", "/api/v1/admin/orgs/nosuchorg/quota", token=admin_token
        )
        check("不存在的组织如实 404", status == 404 and payload(body)["code"] == "ORG_NOT_FOUND", f"{status}")

        print("\n[建立组织与改名，全部入审计链]")
        status, body = http("POST", "/api/v1/admin/orgs", json_body={"org": "acme", "name": "安科"},
                            token=admin_token)
        check("建组织 201", status == 201 and payload(body)["created"] is True, f"{status} {body[:200]}")
        status, body = http("POST", "/api/v1/admin/orgs", json_body={"org": "acme", "name": "安科软件"},
                            token=admin_token)
        check("同名组织再建即改名", status == 201 and payload(body)["created"] is False
              and payload(body)["name"] == "安科软件", f"{status}")
        status, body = http("POST", "/api/v1/admin/orgs", json_body={"org": "bolt"}, token=admin_token)
        check("第二个组织名默认用标识", payload(body)["name"] == "bolt")
        check("建组织的响应也带契约版本（六个端点一个不缺，SDK 才能统一断言）",
              payload(body)["contractVersion"] == ORG_QUOTA_CONTRACT, payload(body).get("contractVersion"))
        # 大小写必须落在同一行：否则「ACME 的预算」和「acme 的已用」是两本账，拦停会算漏
        status, body = http("POST", "/api/v1/admin/orgs", json_body={"org": "ACME", "name": "安科软件"},
                            token=admin_token)
        check("大写标识被规范化成小写（沿用存量 _normalize_org 口径，不新建第二行）",
              status == 201 and payload(body)["created"] is False, f"{status}")
        status, body = http("GET", "/api/v1/admin/orgs", token=admin_token)
        keys = sorted(str(item["org"]) for item in payload(body)["orgs"])
        check("组织表里只有 acme/bolt 两行", keys == ["acme", "bolt"], keys)
        check("清单端点带同一契约版本", payload(body)["contractVersion"] == ORG_QUOTA_CONTRACT)
        status, body = http("GET", "/api/v1/admin/audit/verify", token=admin_token)
        check("建组织之后审计链仍可验", payload(body)["ok"] is True, f"{status} {body[:200]}")
        status, body = http("GET", "/api/v1/admin/audit?limit=50", token=admin_token)
        actions = {str(item["action"]) for item in payload(body).get("entries") or []}
        check("org.create / org.rename 都进了链", {"org.create", "org.rename"} <= actions, sorted(actions))

        print("\n[两级落点：组织成员默认 → 个人覆盖]")
        # 先把爱丽丝放进 acme：PATCH 会顺带建默认部门与成员关系
        status, body = http("PATCH", f"/api/v1/admin/users/{alice_uid}",
                            json_body={"org": "acme"}, token=admin_token)
        check("改组织 200 且如实报吊销数", status == 200 and "sessionsRevoked" in payload(body))
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        check("进组织但没配额度：仍是角色缺省", claims(member["accessToken"])["quota"] == USER_QUOTA)
        status, body = http("PUT", "/api/v1/admin/orgs/acme/quota",
                            json_body={"memberMonthlyTokens": 50000, "memberConcurrentTasks": 1},
                            token=admin_token)
        quota = payload(body)["quota"]
        check("写成员默认档 200", status == 200 and quota["memberMonthlyTokens"] == 50000, f"{status} {body[:200]}")
        check("配额写端点带契约版本", payload(body)["contractVersion"] == ORG_QUOTA_CONTRACT)
        check("拦停没开时 quota.enforce 为假", quota["enforce"] is False)
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        claims_a = claims(member["accessToken"])
        check("爱丽丝落到组织成员默认", claims_a["quota"] == {"monthlyTokens": 50000, "concurrentTasks": 1},
              claims_a["quota"])
        check("来源写的是 orgDefault 而不是个人",
              member["quotaResolution"]["source"] == {"monthlyTokens": "orgDefault",
                                                     "concurrentTasks": "orgDefault"},
              member.get("quotaResolution"))
        check("profile 与声明同步（界面和拦停不各说各话）",
              member["profile"]["quota"] == claims_a["quota"])
        status, body = http("PATCH", f"/api/v1/admin/users/{alice_uid}",
                            json_body={"quota": {"monthlyTokens": 30000}}, token=admin_token)
        check("给爱丽丝设个人覆盖", status == 200)
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        claims_a = claims(member["accessToken"])
        check("个人覆盖压过组织成员默认", int(claims_a["quota"]["monthlyTokens"]) == 30000, claims_a["quota"])
        check("来源改成 personal", member["quotaResolution"]["source"]["monthlyTokens"] == "personal")
        check("并发没设个人值，仍落组织成员默认",
              int(claims_a["quota"]["concurrentTasks"]) == 1
              and member["quotaResolution"]["source"]["concurrentTasks"] == "orgDefault")

        print("\n[enforce 关闭时：预算只报表，不改签发]")
        # 组织总额 100000，爱丽丝当期已用 40000：剩余 60000 > 个人 30000，本来也不会收敛
        http("PUT", "/api/v1/admin/orgs/acme/quota",
             json_body={"monthlyTokens": 100000, "concurrentTasks": 4}, token=admin_token)
        report(admin_token, alice_uid, "acme", 24000, 16000)
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        check("budget 读得到当期已用", int(view["usage"]["tokensUsed"]) == 40000, view.get("usage"))
        check("关着时判定是 reportedOnly 而不是 within", view["budget"]["verdict"] == "reportedOnly", view["budget"])
        check("关着时 enforce 标记为假", view["budget"]["enforce"] is False)
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        check("未开拦停：声明没被动过", int(claims(member["accessToken"])["quota"]["monthlyTokens"]) == 30000)

        print("\n[同源核对：组织已用 = 部门报表合计，重复行不双计]")
        # engine 行标 origin=desktop 是被桌面重复记的那部分，必须被滤掉
        report(admin_token, alice_uid, "acme", 1000, 500, source="engine", origin="desktop",
               model="demo-dup", device="dev-9")
        report(admin_token, alice_uid, "acme", 700, 300, source="engine", origin="gateway",
               model="demo-gw", device="dev-9")
        status, body = http("GET", "/api/v1/admin/usage/departments?org=acme&period=monthly", token=admin_token)
        dept_total = int(payload(body)["totals"]["tokensUsed"])
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        check("重复桌面行没被计入组织已用", int(view["usage"]["tokensUsed"]) == 40000, view.get("usage"))
        check("网关副本行也没双计", int(view["budget"]["used"]) == 40000)
        check("组织已用与部门报表逐字相等（同一套过滤，不是两套算法）",
              dept_total == int(view["budget"]["used"]), f"dept={dept_total} org={view['budget']['used']}")
        check("滤掉的行数被交出来而不是悄悄丢", int(view["usage"]["dedupedRows"]) == 2, view.get("usage"))

        print("\n[打开拦停：收敛、0 陷阱、旧令牌吊销]")
        status, body = http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={"enforce": True},
                            token=admin_token)
        check("开拦停 200", status == 200 and payload(body)["quota"]["enforce"] is True)
        check("开拦停即吊销该组织在用会话，并把吊销数交出来",
              int(payload(body)["sessionsRevoked"]) >= 1, payload(body).get("sessionsRevoked"))
        check("同时给出吊销后仍在用的数（不是拿吊销数冒充在用数）",
              int(payload(body)["activeSessions"]) == 0, payload(body).get("activeSessions"))
        check("一句话说清这次影响到几个人", "吊销了" in str(payload(body)["statement"]),
              payload(body).get("statement"))
        stale = member["accessToken"]
        status, _ = http("GET", "/api/v1/auth/me", token=stale)
        check("旧令牌随拦停变更立刻失效（声明必须重签）", status == 401, status)
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        claims_a = claims(member["accessToken"])
        check("剩余 60000 > 个人 30000：不收敛", int(claims_a["quota"]["monthlyTokens"]) == 30000)
        check("并发 4 > 成员默认 1：单人声明不放大", int(claims_a["quota"]["concurrentTasks"]) == 1)
        # 把总额压到刚好剩 10000：30000 → 10000
        http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={"monthlyTokens": 50000}, token=admin_token)
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        claims_a = claims(member["accessToken"])
        check("成员上限收敛到组织剩余 10000", int(claims_a["quota"]["monthlyTokens"]) == 10000, claims_a["quota"])
        check("收敛这件事被明说（clamped + 中文原因）",
              member["quotaResolution"]["clamped"] is True
              and any("收敛" in str(item) for item in member["quotaResolution"]["reasons"]),
              member.get("quotaResolution"))
        # 0 的陷阱：个人 0 = 「不限制」。它只在「组织压根没设月度预算」时才该原样进声明；
        # 组织一设预算，0 必须被当作 +∞ 来收敛，且收敛结果下限取 1（写 0 = 取消限制）。
        http("PATCH", f"/api/v1/admin/users/{bob_uid}", json_body={"org": "acme"}, token=admin_token)
        http("PATCH", f"/api/v1/admin/users/{bob_uid}", json_body={"quota": {"monthlyTokens": 0}},
             token=admin_token)
        http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={"monthlyTokens": 0}, token=admin_token)
        bob_in = login("bob@test.local", ACCOUNT_PASSWORD)
        check("组织未设月度预算时，个人 0（不限制）原样进声明",
              int(claims(bob_in["accessToken"])["quota"]["monthlyTokens"]) == 0,
              claims(bob_in["accessToken"])["quota"])
        check("这种时候没被收敛也没被拦（0 没被误读成额度为零）",
              bob_in["quotaResolution"]["clamped"] is False
              and bob_in["quotaResolution"]["enforced"] is True,
              bob_in.get("quotaResolution"))
        http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={"monthlyTokens": 50000}, token=admin_token)
        bob_in = login("bob@test.local", ACCOUNT_PASSWORD)
        check("组织一设预算，个人 0 也被当作无限大收敛到剩余 10000（不是原样放行）",
              int(claims(bob_in["accessToken"])["quota"]["monthlyTokens"]) == 10000,
              claims(bob_in["accessToken"])["quota"])
        check("收敛原因里说清「原本不限制」，不留成一条看不懂的数",
              any("原本不限制" in str(item) for item in bob_in["quotaResolution"]["reasons"]),
              bob_in.get("quotaResolution"))
        http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={"monthlyTokens": 40001}, token=admin_token)
        bob_in = login("bob@test.local", ACCOUNT_PASSWORD)
        check("组织只剩 1 时收敛成 1，绝不写 0（写 0 = 取消限制）",
              int(claims(bob_in["accessToken"])["quota"]["monthlyTokens"]) == 1,
              claims(bob_in["accessToken"])["quota"])
        report(admin_token, bob_uid, "acme", 1, 0)
        status, body = login_status("bob@test.local")
        check("组织预算用尽即拒发令牌（403 + 稳定码）",
              status == 403 and body.get("code") == "ORG_BUDGET_EXCEEDED", f"{status} {body}")
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        check("用尽时判定是 exhausted 且剩余不为正", view["budget"]["verdict"] == "exhausted", view["budget"])
        admin_again = login("admin@test.local")
        check("管理员不受组织拦停（否则唯一能改预算的人进不来 = 自锁）",
              "accessToken" in admin_again)
        check("豁免这件事在响应里明写，不藏着",
              any("管理员" in str(item) for item in view["disclosed"]), view.get("disclosed"))
        http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={"monthlyTokens": 200000}, token=admin_token)
        bob_in = login("bob@test.local", ACCOUNT_PASSWORD)
        check("加预算后同一账号立刻能登", "accessToken" in bob_in)

        print("\n[超售与归属：报表要说清这套账的边界]")
        status, body = http("PUT", "/api/v1/admin/orgs/acme/quota",
                            json_body={"monthlyTokens": 35000}, token=admin_token)
        check("总额压到 35000", payload(body)["quota"]["monthlyTokens"] == 35000)
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        caps = view["members"]["caps"]
        sum_caps = int(sum(int(item["monthlyTokens"] or 0) for item in caps))
        check("成员上限之和只加有额的（0 = 不限制，不是可以相加的数）",
              int(view["budget"]["sumMemberCaps"]) == sum_caps,
              f"{view['budget']} vs {sum_caps}")
        check("组里还有人「不限制」而组织预算有限 ⇒ 如实标超售",
              view["budget"]["overCommitted"] is True
              and int(view["budget"]["uncappedMembers"]) >= 1,
              view["budget"])
        check("超售的原因被写出来，不是只甩一个布尔",
              "不限制" in str(view["budget"]["overCommitReason"]),
              view["budget"].get("overCommitReason"))
        # 把「不限制」的那个人收进有额档：超售改由「之和 > 总额」这一支触发
        http("PATCH", f"/api/v1/admin/users/{bob_uid}",
             json_body={"quota": {"monthlyTokens": 99999}}, token=admin_token)
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        check("全员有额后不限数归零，但之和超过总额仍标超售",
              int(view["budget"]["uncappedMembers"]) == 0
              and view["budget"]["overCommitted"] is True
              and "超过" in str(view["budget"]["overCommitReason"]),
              view["budget"])
        check("cap 行把豁免标出来：管理员走豁免路径，他的数字不算承诺",
              all("exempt" in item for item in view["members"]["caps"]),
              view["members"]["caps"][:1])
        # 把卡罗尔放进来再挪去 bolt：她带 acme 盖章的用量留在 acme 账上
        http("PATCH", f"/api/v1/admin/users/{carol_uid}", json_body={"org": "acme"}, token=admin_token)
        report(admin_token, carol_uid, "acme", 200, 100)
        http("PATCH", f"/api/v1/admin/users/{carol_uid}", json_body={"org": "bolt"}, token=admin_token)
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        # 戴夫：进组织但这一期一次也没上报——报表必须数得出这种人，不然「已用」看着像全量
        dave = register("dave@test.local", "戴夫")
        dave_uid = str(dave["profile"]["uid"])
        http("PATCH", f"/api/v1/admin/users/{dave_uid}", json_body={"org": "acme"}, token=admin_token)
        check("人已走但用量仍按上报时刻盖章留在原组织",
              int(view["attribution"]["usageFromNonMembers"]) >= 1, view.get("attribution"))
        check("归属规则被写进响应而不是留给读者猜",
              "盖章" in str(view["attribution"]["rule"]), view.get("attribution"))
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        view = payload(body)
        check("当期没上报的成员也数得出来（戴夫进了组织但一次也没上报）",
              int(view["attribution"]["membersWithNoUsageThisPeriod"]) >= 1, view.get("attribution"))
        check("「服务端只看得见已上报用量」这条滞后被交出来",
              any("滞后" in str(item) for item in view["disclosed"]), view.get("disclosed"))
        check("「声明是签发时刻快照」这条也交了",
              any("签发时刻" in str(item) for item in view["disclosed"]), view.get("disclosed"))
        status, body = http("GET", "/api/v1/admin/orgs/acme/members", token=admin_token)
        listed = payload(body)
        check("成员页给出每人下次签发会拿到的两个数",
              all({"quota", "source", "clamped", "blocked"} <= set(item) for item in listed["members"]),
              listed.get("members"))
        check("成员页声明这是签发前的预测不是现值", "下一次签发" in str(listed.get("note")), listed.get("note"))
        check("成员页也带同一契约版本", listed["contractVersion"] == ORG_QUOTA_CONTRACT,
              listed.get("contractVersion"))

        print("\n[配额写入的校验与审计]")
        status, body = http("PUT", "/api/v1/admin/orgs/acme/quota", json_body={}, token=admin_token)
        check("空入参被拒并说清要哪个", status == 400 and payload(body)["code"] == "NOTHING_TO_UPDATE",
              f"{status} {body[:200]}")
        status, body = http("PUT", "/api/v1/admin/orgs/acme/quota",
                            json_body={"monthlyTokens": -1}, token=admin_token)
        check("负额度被拒", status == 422 and payload(body)["code"] == "VALIDATION_ERROR", f"{status}")
        status, body = http("PUT", "/api/v1/admin/orgs/noneorg/quota",
                            json_body={"monthlyTokens": 10}, token=admin_token)
        check("给不存在的组织写额度如实 404", status == 404, f"{status}")
        status, body = http("PUT", "/api/v1/admin/orgs/acme/quota",
                            json_body={"monthlyTokens": 10, "masterKey": "sk-1"}, token=admin_token)
        check("想塞未声明字段（masterKey）被拒", status == 422 and "未允许" in payload(body)["message"],
              f"{status} {body[:200]}")
        status, body = http("GET", "/api/v1/admin/audit/verify", token=admin_token)
        check("配额写入之后审计链仍可验", payload(body)["ok"] is True)
        status, body = http("GET", "/api/v1/admin/audit?limit=80", token=admin_token)
        entries = payload(body).get("entries") or []
        quota_rows = [item for item in entries if str(item["action"]) == "org_quota.set"]
        check("org_quota.set 入链且记了 before/after",
              len(quota_rows) >= 1
              and any((item.get("beforeJson") or item.get("before")) and (item.get("afterJson") or item.get("after"))
                      for item in quota_rows),
              len(quota_rows))
        check("审计里没有令牌也没有密钥",
              all(ACCOUNT_PASSWORD not in str(item) and "Bearer" not in str(item) for item in entries))

        print("\n[CLI orgs 组：机器面同一条契约]")
        admin_env = {"TT_SERVER_TOKEN": admin["accessToken"]}
        data = expect_ok("cli orgs list", ("orgs", "list", "--json"), admin_env)
        check("cli 列到两个组织", int(data["total"]) == 2, data)
        check("cli 一屏给出配额与当期已用",
              any(item["org"] == "acme" and item["quota"] and "used" in item for item in data["orgs"]),
              data["orgs"][:1])
        expect_cli_fail("cli quota set 不给旗子是用法错误", ("orgs", "quota", "set", "acme", "--json"),
                        2, "USAGE", admin_env)
        data = expect_ok("cli quota set --monthly-tokens 0 真把 0 发出去",
                         ("orgs", "quota", "set", "acme", "--monthly-tokens", "0", "--enforce", "off", "--json"),
                         admin_env)
        check("0 落库了（0 = 不限制，不是没填）", int(data["quota"]["monthlyTokens"]) == 0, data["quota"])
        rc, envelope, _ = cli("orgs", "quota", "set", "acme", "--monthly-tokens", "5", "--json", env=admin_env)
        check("command 写成「orgs quota set」便于脚本对齐",
              envelope.get("command") == "orgs quota set", envelope.get("command"))
        data = expect_ok("cli orgs budget", ("orgs", "budget", "acme", "--json"), admin_env)
        check("cli 的预算页有 statement", "限额" in str(data["statement"]), data.get("statement"))
        data = expect_ok("cli orgs quota get", ("orgs", "quota", "get", "acme", "--json"), admin_env)
        check("cli 读得到刚写的值", int(data["quota"]["monthlyTokens"]) == 5, data)
        data = expect_ok("cli orgs members", ("orgs", "members", "acme", "--json"), admin_env)
        check("cli 成员页有落点来源", all("source" in item for item in data["members"]), data["members"][:1])
        # 六个动作透传同一份服务端契约版本：脚本可以只看一处就断言口径没换
        for label, argv in (
            ("list", ("orgs", "list", "--json")),
            ("budget", ("orgs", "budget", "acme", "--json")),
            ("members", ("orgs", "members", "acme", "--json")),
            ("quota get", ("orgs", "quota", "get", "acme", "--json")),
        ):
            got = expect_ok(f"cli {label} 透传服务端契约版本", argv, admin_env)
            check(f"cli {label} 的 contractVersion 是 {ORG_QUOTA_CONTRACT}",
                  got["contractVersion"] == ORG_QUOTA_CONTRACT, got.get("contractVersion"))
        check("cli quota set 也把吊销数带到机器面",
              "sessionsRevoked" in envelope.get("data", {}), sorted(envelope.get("data", {})))
        expect_cli_fail("cli 对不存在的组织给 ORG_NOT_FOUND",
                        ("orgs", "budget", "noneorg", "--json"), 1, "ORG_NOT_FOUND", admin_env)
        data = expect_ok("cli orgs create", ("orgs", "create", "delta", "--name", "德尔塔", "--json"), admin_env)
        check("cli 建组织如实报 created", data["created"] is True and data["name"] == "德尔塔", data)
        expect_cli_fail("cli 成员令牌管不了组织",
                        ("orgs", "list", "--json"), 1, "FORBIDDEN",
                        {"TT_SERVER_TOKEN": login("alice@test.local", ACCOUNT_PASSWORD)["accessToken"]})

        print("\n[备份包把 org_quotas 当底账]")
        package = os.path.join(_OUT, "pkg")
        data = expect_ok("建包", ("backup", "create", "--out", package, "--json"), admin_env)
        # create 的 `manifest` 是清单文件路径，逐表行数在顶层（不是嵌在 manifest 里）
        counts = data["rowCounts"]
        check("清单逐表记了 org_quotas 行数", int(counts.get("org_quotas") or -1) >= 1, counts)
        check("审计链头也一起进清单（恢复后要能对得上）", bool(data["auditChain"]), data.get("auditChain"))
        expect_ok("验包", ("backup", "verify", package, "--json"), admin_env)
        restored = os.path.join(_OUT, "restored")
        os.makedirs(restored, exist_ok=True)
        data = expect_ok("恢复到新数据根",
                         ("backup", "restore", package, "--to", restored, "--yes", "--json"),
                         {"TT_SERVER_DATA_DIR": restored})
        check("恢复后的行数与清单一致",
              int((data["rowCounts"] or {}).get("org_quotas") or -1) == int(counts.get("org_quotas")),
              f"{data['rowCounts']} vs {counts}")
        probe = sqlite3.connect(os.path.join(restored, "market.db"))
        probe.row_factory = sqlite3.Row
        got = probe.execute("SELECT monthly_tokens, enforce FROM org_quotas WHERE org_id = 'acme'").fetchone()
        probe.close()
        check("恢复后的组织配额值就是备份时那份",
              got is not None and int(got["monthly_tokens"]) == 5, dict(got) if got else None)

        print("\n[关掉拦停即回到报表态]")
        # 此时组织总额只剩 1、爱丽丝个人覆盖 30000：开着拦停必然收敛成 1，
        # 关掉之后必须回到「没被动过」的 30000，而不是停在收敛值或写成 0。
        http("PUT", "/api/v1/admin/orgs/acme/quota",
             json_body={"monthlyTokens": 1, "enforce": False}, token=admin_token)
        member = login("alice@test.local", ACCOUNT_PASSWORD)
        claims_a = claims(member["accessToken"])
        check("enforce 关了以后不再按预算收敛（总额只剩 1 也照发个人额度）",
              int(claims_a["quota"]["monthlyTokens"]) == 30000, claims_a["quota"])
        check("关着时签发回执里 enforced 为假、clamped 为假",
              member["quotaResolution"]["enforced"] is False
              and member["quotaResolution"]["clamped"] is False,
              member.get("quotaResolution"))
        check("profile 也跟着回到同一份数（不出现界面 30000 / 声明 1 的分裂）",
              member["profile"]["quota"] == claims_a["quota"], member["profile"].get("quota"))
        status, body = http("GET", "/api/v1/admin/orgs/acme/budget", token=admin_token)
        check("关着时判定回到 reportedOnly", payload(body)["budget"]["verdict"] == "reportedOnly")
        check("预算页与成员页同用一份契约版本（报表和签发不能各说一套）",
              payload(body)["contractVersion"] == ORG_QUOTA_CONTRACT)

        print("\n[输出泄漏总扫]")
        everything = "\n".join(CAPTURED)
        for secret in (ADMIN_PASSWORD, ACCOUNT_PASSWORD, admin["accessToken"], "orgq-test-secret", "Bearer "):
            check(f"CLI 输出里没有 {secret[:8]}…", secret not in everything)

        print(f"\n通过 {_passed} 项")
    finally:
        server.should_exit = True
        time.sleep(0.3)


if __name__ == "__main__":
    main()
