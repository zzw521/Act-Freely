#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-37 服务端管理面 CLI（`python -m app.cli`）自测。

这台机器上没有界面可点，验收只能对着「子进程真的按命令行跑了一遍」来判：

1) 契约面：`--json` 每次调用输出**一个**固定形状的 JSON 对象，退出码 0/1/2 三档分明，
   连 argparse 的用法错误也被翻译成同一个信封；
2) 凭据面：`--token` / `--password` 一律拒绝，URL 里带凭据一律拒绝，环境变量登录成功与否
   都不回显口令与令牌（收尾对全部子进程输出与落盘文件做一次泄漏扫描）；
3) 本地面：`doctor` 只读打开数据根，服务地址指向一个没人听的端口也能出结论；链被改过要
   报 `AUDIT_CHAIN_BROKEN`，改回来要重新变绿；
4) 服务端面：账号、部门、档位、审计与锚点各命令都真的打到对应端点，服务端的稳定错误码
   原样进 `problems[].code`（`SELF_ROLE_CHANGE` / `TIER_INVALID` / `AUTH_REQUIRED` …）。

用法: python tests/test_cli.py
"""

from __future__ import annotations

import atexit
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-cli-")
_EMPTY = tempfile.mkdtemp(prefix="tt-cli-empty-")
_OUT = tempfile.mkdtemp(prefix="tt-cli-out-")
_SERVE = tempfile.mkdtemp(prefix="tt-cli-serve-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
atexit.register(shutil.rmtree, _EMPTY, ignore_errors=True)
atexit.register(shutil.rmtree, _OUT, ignore_errors=True)
atexit.register(shutil.rmtree, _SERVE, ignore_errors=True)

os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "cli-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "CLI测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402
from app.model_tiers import TIER_CONTRACT_VERSION  # noqa: E402

_passed = 0
ADMIN_PASSWORD = "admin-secret-123"
ACCOUNT_PASSWORD = "secret123"

# 捕获到的所有子进程输出，收尾统一做凭据泄漏扫描
CAPTURED: list[str] = []
# 子进程写出来的文件（审计导出、合规报告），同样要扫
WRITTEN_FILES: list[str] = []


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


def _port_open(port: int) -> bool:
    """端口还能不能连上；用来证明 serve 进程真的停了。"""
    with socket.socket() as sock:
        sock.settimeout(1.0)
        try:
            return sock.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


PORT = free_port()
DEAD_PORT = free_port()
SERVE_PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
DEAD = f"http://127.0.0.1:{DEAD_PORT}"
SERVE_URL = f"http://127.0.0.1:{SERVE_PORT}"
DB_PATH = os.path.join(_TMP, "market.db")
_opener = urllib.request.build_opener()


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=30)
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def register(email: str, nickname: str) -> dict:
    status, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": ACCOUNT_PASSWORD, "nickname": nickname},
    )
    data = payload(body)
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {data}")
    return data


def login(email: str, password: str = ACCOUNT_PASSWORD) -> str:
    status, body = http("POST", "/api/v1/auth/login", json_body={"email": email, "password": password})
    data = payload(body)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {data}")
    return data["accessToken"]


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


# ── CLI 调用 ────────────────────────────────────────────────


class Run:
    """一次子进程调用：退出码、两个流、从任一流里解析出的信封。"""

    def __init__(self, argv: list[str], proc: subprocess.CompletedProcess, envelope: dict | None):
        self.argv = argv
        self.rc = proc.returncode
        self.stdout = proc.stdout or ""
        self.stderr = proc.stderr or ""
        self.env = envelope or {}
        self.data = self.env.get("data") if isinstance(self.env.get("data"), dict) else {}
        self.problems = self.env.get("problems") or []

    @property
    def code(self) -> str:
        return str((self.problems[0] or {}).get("code", "")) if self.problems else ""

    @property
    def detail(self) -> str:
        return str((self.problems[0] or {}).get("detail", "")) if self.problems else ""

    def brief(self) -> str:
        return (
            f"argv={self.argv} rc={self.rc} code={self.code} "
            f"stdout={self.stdout[:400]!r} stderr={self.stderr[:400]!r}"
        )

    @property
    def text(self) -> str:
        return self.stdout + self.stderr


def cli(*argv: str, env: dict | None = None, drop: tuple[str, ...] = ()) -> Run:
    run_env = dict(os.environ)
    for key in drop:
        run_env.pop(key, None)
    # 默认给一个可用地址；父进程环境里的 TT_SERVER_URL 不能悄悄影响别的路径
    run_env.pop("TT_SERVER_URL", None)
    run_env["TT_SERVER_URL"] = BASE
    if env:
        run_env.update(env)
    args = [str(item) for item in argv]
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "app.cli", *args],
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
    # 用法错误时 stderr 是「argparse 的 usage + 我们的 JSON 信封」两段，按行取最后一段 JSON
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
    return Run(args, proc, envelope)


def expect_ok(name: str, run: Run) -> dict:
    check(name, run.rc == 0 and run.env.get("ok") is True, run.brief())
    return run.data


def expect_fail(name: str, run: Run, rc: int, code: str = "", contains: str = "") -> dict:
    ok_rc = run.rc == rc
    ok_env = run.env.get("ok") is False and run.env.get("exitCode") == rc
    ok_code = (not code) or run.code == code
    ok_text = (not contains) or contains in run.detail
    check(name, ok_rc and ok_env and ok_code and ok_text, run.brief())
    return run.env


def envelope_shape(name: str, run: Run) -> None:
    check(
        name,
        set(run.env) == {"ok", "contractVersion", "command", "data", "problems", "exitCode"},
        run.brief(),
    )


def patch_audit_cell(row_id: int, column: str, value: str) -> str:
    """改一行审计的某一列，返回原值。只用于制造「有人改过库」的证据，用完必须还原。"""
    assert column in ("after_json", "action")
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        previous = str(conn.execute(f"SELECT {column} FROM admin_audit WHERE id = ?1", (row_id,)).fetchone()[0])
        conn.execute(f"UPDATE admin_audit SET {column} = ?1 WHERE id = ?2", (value, row_id))
        conn.commit()
    finally:
        conn.close()
    return previous


TIER_DOC = {
    "contractVersion": TIER_CONTRACT_VERSION,
    "defaultTier": "auto",
    "byokAllowed": True,
    "models": [
        {
            "id": "small-fast",
            "provider": "demo",
            "upstreamModel": "demo-small",
            "displayName": "经济小模型",
            "contextWindow": 32000,
            "maxOutputTokens": 4000,
        },
        {
            "id": "mid-main",
            "provider": "demo",
            "upstreamModel": "demo-mid",
            "displayName": "基础主力",
            "contextWindow": 128000,
            "maxOutputTokens": 8000,
        },
        {
            "id": "big-long",
            "provider": "demo",
            "upstreamModel": "demo-big",
            "displayName": "高级长上下文",
            "contextWindow": 256000,
            "maxOutputTokens": 16000,
        },
    ],
    "tiers": {
        "economy": {"label": "经济", "chain": ["small-fast"], "byokAllowed": False, "note": ""},
        "basic": {"label": "基础", "chain": ["mid-main", "small-fast"], "note": ""},
        "advanced": {"label": "高级", "chain": ["big-long", "mid-main"], "byokAllowed": True, "note": ""},
    },
    "autoPolicy": {
        "rules": [
            {"taskKind": "chat", "maxContextTokens": 8000, "tier": "economy"},
            {"taskKind": "report", "maxContextTokens": None, "tier": "advanced"},
        ],
        "fallbackTier": "basic",
    },
}


def write_doc(name: str, doc: dict) -> str:
    path = os.path.join(_OUT, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)
    return path


def manifest_of(directory: str) -> dict:
    path = os.path.join(directory, "manifest.json")
    WRITTEN_FILES.append(path)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    server = start_server()
    try:
        admin_token = login("admin@test.local", ADMIN_PASSWORD)
        stamp = int(time.time())
        member = register(f"cli-member-{stamp}@test.local", "CLI成员")
        member_uid = str(member["profile"]["uid"])
        ADMIN = {"TT_SERVER_TOKEN": admin_token}

        print("\n[机器面契约：一次调用一个对象]")
        run = cli("health", "--json", env=ADMIN)
        expect_ok("health 拿到服务名与版本", run)
        envelope_shape("信封六个键固定", run)
        check("health 的 data 只有服务信息", run.data.get("service") == "tongtian-server", run.brief())
        check("command 记的是组名", run.env.get("command") == "health", run.brief())
        check("problems 为空数组", run.env.get("problems") == [], run.brief())

        run = cli("whoami", "--json", env=ADMIN)
        expect_ok("whoami 认出管理员", run)
        check("whoami 带 uid/role", run.data.get("role") == "admin" and run.data.get("uid"), run.brief())
        admin_uid = str(run.data.get("uid"))
        check("whoami 的 auth 只说来源不说内容", run.data["auth"]["authSource"] == "env-token", run.brief())
        check("whoami 的 tokenPresent 为真", run.data["auth"]["tokenPresent"] is True, run.brief())
        check(
            "whoami 输出里没有令牌本体",
            admin_token not in run.text,
            f"令牌出现在输出里：{run.argv}",
        )

        run = cli("--help")
        check("--help 正常退出（不是错误）", run.rc == 0, run.brief())
        check("--help 讲清凭据只走环境变量", "TT_SERVER_TOKEN" in run.stdout, run.brief())

        print("\n[用法错误也走同一个信封]")
        run = cli("bogus", "--json")
        expect_fail("未知命令组给 USAGE/2", run, 2, "USAGE")
        envelope_shape("用法错误的信封同样固定", run)
        check("未知命令组的 command 记为 usage", run.env.get("command") == "usage", run.brief())

        run = cli("users", "--json")
        expect_fail("缺动作给 USAGE/2", run, 2, "USAGE")

        run = cli("audit", "bogus-action", "--json", env=ADMIN)
        expect_fail("未知动作给 USAGE/2", run, 2, "USAGE")

        run = cli("users", "set-quota", member_uid, "--json", env=ADMIN)
        expect_fail("set-quota 一个值都不给时不猜默认", run, 2, "USAGE", "至少")

        run = cli("tiers", "revert", "global", "-", "--json", env=ADMIN)
        expect_fail("revert 不给 --revision 直接拒", run, 2, "USAGE", "--revision")

        print("\n[凭据不许进命令行]")
        for flag in ("--token", "--password", "--bearer", "--secret", "--authorization"):
            run = cli("users", "list", flag, "abc", "--json", env=ADMIN)
            expect_fail(f"{flag} 被拒在解析之前", run, 2, "USAGE", "环境变量")
        run = cli("users", "list", "--token=abc", "--json", env=ADMIN)
        expect_fail("--token=值 的写法同样被拒", run, 2, "USAGE")
        check("拒绝信息里不回显被拒的值", "abc" not in run.text, run.brief())

        print("\n[地址口径]")
        run = cli("health", "--url", f"http://u:p@127.0.0.1:{PORT}", "--json")
        expect_fail("URL 里带凭据直接拒", run, 2, "USAGE", "TT_SERVER_TOKEN")
        run = cli("health", "--url", "ftp://127.0.0.1:21", "--json")
        expect_fail("非 http/https 地址拒", run, 2, "USAGE", "http")
        run = cli("health", "--url", f"{BASE}/?debug=1", "--json")
        expect_fail("地址带查询串拒", run, 2, "USAGE", "查询串")
        run = cli("health", "--url", "not-a-url", "--json")
        expect_fail("没协议裸地址拒", run, 2, "USAGE")

        print("\n[服务不可达时]")
        run = cli("health", "--url", DEAD, "--json")
        expect_fail("连不上给 SERVER_UNREACHABLE/1", run, 1, "SERVER_UNREACHABLE")
        check("不可达信息点名 --url 与环境变量", "TT_SERVER_URL" in run.detail, run.brief())

        print("\n[缺凭据]")
        run = cli("users", "list", "--json", drop=("TT_SERVER_TOKEN", "TT_SERVER_ADMIN_EMAIL", "TT_SERVER_ADMIN_PASSWORD"))
        expect_fail("管理命令没凭据是用法错误", run, 2, "USAGE", "TT_SERVER_TOKEN")
        run = cli("audit", "verify", "--json", env={"TT_SERVER_TOKEN": "not-a-real-token"})
        expect_fail("坏令牌由服务端判 401", run, 1, "AUTH_REQUIRED")

        print("\n[doctor 不依赖服务在跑]")
        run = cli("doctor", "--json", "--url", DEAD)
        data = expect_ok("服务地址是死端口也能出结论", run)
        check("doctor 的 db 存在", data.get("dbExists") is True, run.brief())
        check("doctor 报 integrity ok", data.get("sqliteIntegrity") == "ok", run.brief())
        check("doctor 报 WAL", str(data.get("journalMode", "")).lower() == "wal", run.brief())
        check("doctor 无缺表", data.get("missingTables") == [], run.brief())
        check("doctor 数得到账号", int(data["rowCounts"]["users"]) >= 2, run.brief())
        check("doctor 带链头", len(str(data["auditChain"]["headHash"])) == 64, run.brief())
        check("doctor 带签名钥 kid", bool(data["signingKey"]["currentKid"]), run.brief())
        check("doctor 报密钥把数", int(data["signingKey"]["keyCount"]) >= 1, run.brief())
        check("doctor 报磁盘剩余", int(data.get("diskFreeBytes", -1)) > 0, run.brief())
        check("doctor 只说凭据来源", data["auth"]["url"] == DEAD, run.brief())
        check("doctor 报归档未配置", data.get("archiveConfigured") is False, run.brief())

        run = cli("doctor", "--json", env={"TT_SERVER_DATA_DIR": _EMPTY})
        expect_fail("空数据根给 DB_MISSING/1", run, 1, "DB_MISSING")
        codes = [str(item["code"]) for item in run.problems]
        check("空数据根同时点名没有签名钥", "NO_SIGNING_KEY" in codes, run.brief())
        check(
            "体检不会替数据根生成密钥目录",
            not os.path.exists(os.path.join(_EMPTY, "keys")),
            "doctor 在空目录里写出了 keys/——自检不该有这种副作用",
        )
        check("体检不会替数据根建库", not os.path.exists(os.path.join(_EMPTY, "market.db")), "")

        print("\n[环境变量登录：口令只在进程里]")
        env_login = {
            "TT_SERVER_ADMIN_EMAIL": "admin@test.local",
            "TT_SERVER_ADMIN_PASSWORD": ADMIN_PASSWORD,
        }
        run = cli("whoami", "--json", env=env_login, drop=("TT_SERVER_TOKEN",))
        data = expect_ok("用邮箱+密码环境变量登进去", run)
        check("来源标为 env-login", data["auth"]["authSource"] == "env-login", run.brief())
        check("登录输出不含口令", ADMIN_PASSWORD not in run.text, run.brief())
        check("登录输出不含令牌", "Bearer" not in run.text and "eyJ" not in run.text, run.brief())

        run = cli(
            "whoami",
            "--json",
            env={"TT_SERVER_ADMIN_EMAIL": "admin@test.local", "TT_SERVER_ADMIN_PASSWORD": "wrong-password"},
            drop=("TT_SERVER_TOKEN",),
        )
        expect_fail("错密码由服务端判 401", run, 1, "INVALID_CREDENTIALS")
        check("失败输出不含任何口令", "wrong-password" not in run.text and ADMIN_PASSWORD not in run.text, run.brief())

        print("\n[账号管理面]")
        run = cli("users", "list", "--json", env=ADMIN)
        data = expect_ok("列账号", run)
        check("列表带总数与分页", int(data["total"]) >= 2 and data["page"] == 1, run.brief())
        check("列表项带 uid/role/org", all({"uid", "role", "org"} <= set(item) for item in data["users"]), run.brief())
        check("列表不含口令哈希字段", not any("password" in str(k).lower() for item in data["users"] for k in item), run.brief())

        run = cli("users", "list", "--query", f"cli-member-{stamp}", "--page-size", "1", "--json", env=ADMIN)
        data = expect_ok("搜索 + 单页", run)
        check("命中一个", int(data["total"]) == 1 and len(data["users"]) == 1, run.brief())

        run = cli("users", "set-role", admin_uid, "operator", "--json", env=ADMIN)
        expect_fail("不能改自己的角色（服务端码原样透传）", run, 1, "SELF_ROLE_CHANGE")

        member_token = login(f"cli-member-{stamp}@test.local")
        run = cli("whoami", "--json", env={"TT_SERVER_TOKEN": member_token})
        data = expect_ok("成员令牌改角色前可用", run)
        check("成员当前角色是 user", data.get("role") == "user", run.brief())

        run = cli("users", "set-role", member_uid, "operator", "--json", env=ADMIN)
        data = expect_ok("改成员角色为 operator", run)
        check("返回体是新档案", data["profile"]["role"] == "operator", run.brief())
        check("如实带 sessionsRevoked", isinstance(data.get("sessionsRevoked"), int), run.brief())

        run = cli("whoami", "--json", env={"TT_SERVER_TOKEN": member_token})
        expect_fail("改角色后旧令牌当场失效", run, 1, "AUTH_REQUIRED")

        run = cli("users", "set-role", member_uid, "user", "--json", env=ADMIN)
        expect_ok("改回 user", run)

        member_token = login(f"cli-member-{stamp}@test.local")
        run = cli("users", "set-org", member_uid, "--org", "ops", "--json", env=ADMIN)
        data = expect_ok("把成员挪进 ops 组织", run)
        check("档案里的组织变了", data["profile"]["org"] == "ops", run.brief())

        run = cli("audit", "verify", "--json", env={"TT_SERVER_TOKEN": member_token})
        expect_fail("改组织后旧令牌的会话声明过期", run, 1, "AUTH_REQUIRED")

        run = cli("users", "set-quota", member_uid, "--concurrent-tasks", "3", "--json", env=ADMIN)
        data = expect_ok("只改并发上限", run)
        check("配额落到档案", (data["profile"].get("quota") or {}).get("concurrentTasks") == 3, run.brief())

        run = cli("users", "set-quota", member_uid, "--concurrent-tasks", "0", "--json", env=ADMIN)
        expect_fail("并发给 0 由服务端挡下", run, 1, "INVALID_QUOTA")

        run = cli("users", "revoke-sessions", member_uid, "--json", env=ADMIN)
        data = expect_ok("撤销成员全部会话", run)
        check("返回撤销计数", isinstance(data.get("revoked"), int), run.brief())

        print("\n[部门管理面]")
        run = cli("depts", "create", "rnd", "--org", "ops", "--name", "研发部", "--json", env=ADMIN)
        data = expect_ok("建部门", run)
        check("返回部门体", data["department"]["id"] == "rnd" and data["department"]["org"] == "ops", run.brief())

        run = cli("depts", "list", "--org", "ops", "--json", env=ADMIN)
        data = expect_ok("列 ops 部门", run)
        names = {item["id"]: item["name"] for item in data["departments"]}
        check("新建的部门在列", "rnd" in names, run.brief())
        check("默认部门也在列", len(data["departments"]) >= 2, run.brief())

        run = cli("depts", "rename", "rnd", "--org", "ops", "--name", "平台组", "--json", env=ADMIN)
        data = expect_ok("改部门名", run)
        check("名字确实改了", data["department"]["name"] == "平台组", run.brief())

        run = cli("depts", "rename", "nosuch", "--org", "ops", "--name", "x", "--json", env=ADMIN)
        expect_fail("改不存在的部门给 404 码", run, 1, "DEPARTMENT_NOT_FOUND")

        run = cli("depts", "create", "rnd", "--json", env=ADMIN)
        expect_fail("建部门不给 --org 直接拒", run, 2, "USAGE", "--org")

        print("\n[档位管理面]")
        run = cli("tiers", "effective", "--json", env=ADMIN)
        expect_fail("没表时如实说没有", run, 1, "TIER_TABLE_NOT_FOUND")

        run = cli("tiers", "publish", "--from-file", os.path.join(_OUT, "nope.json"), "--json", env=ADMIN)
        expect_fail("文件不存在给 USAGE", run, 2, "USAGE", "文件不存在")

        broken_json = os.path.join(_OUT, "broken.json")
        with open(broken_json, "w", encoding="utf-8") as handle:
            handle.write("{不是 JSON")
        run = cli("tiers", "publish", "--from-file", broken_json, "--json", env=ADMIN)
        expect_fail("非法 JSON 给 USAGE", run, 2, "USAGE", "不是合法 JSON")

        bad_doc = json.loads(json.dumps(TIER_DOC))
        bad_doc["tiers"]["advanced"]["chain"] = ["ghost-model"]
        run = cli("tiers", "publish", "--from-file", write_doc("bad-tier.json", bad_doc), "--json", env=ADMIN)
        expect_fail("链引用未登记模型由服务端挡下", run, 1, "TIER_INVALID")

        good = write_doc("tier-v1.json", TIER_DOC)
        run = cli("tiers", "publish", "--from-file", good, "--json", env=ADMIN)
        data = expect_ok("发布 v1", run)
        check("v1 是 revision 1", data["revision"] == 1 and data["changed"] is True, run.brief())
        check("v1 带 64 位摘要", len(str(data["digest"])) == 64, run.brief())

        run = cli("tiers", "publish", "--from-file", good, "--json", env=ADMIN)
        data = expect_ok("重复发布不升版", run)
        check("changed=False 且版本仍是 1", data["changed"] is False and data["revision"] == 1, run.brief())

        v2_doc = json.loads(json.dumps(TIER_DOC))
        v2_doc["defaultTier"] = "basic"
        run = cli("tiers", "publish", "--from-file", write_doc("tier-v2.json", v2_doc), "--json", env=ADMIN)
        data = expect_ok("内容变了才升版", run)
        check("v2 revision=2", data["revision"] == 2 and data["changed"] is True, run.brief())
        digest_v2 = str(data["digest"])

        run = cli("tiers", "effective", "--json", env=ADMIN)
        data = expect_ok("effective 拿到当前版", run)
        check("effective 的 revision 是 2", data["revision"] == 2, run.brief())

        run = cli("tiers", "list", "--json", env=ADMIN)
        data = expect_ok("列各作用域", run)
        check("契约版本回显", data["contractVersion"] == TIER_CONTRACT_VERSION, run.brief())
        check("档位名四个", len(data["tiers"]) == 4, run.brief())
        check("表里记着 global 的 revision", any(item["scopeKind"] == "global" and item["revision"] == 2 for item in data["tables"]), run.brief())

        run = cli("tiers", "verify", "--json", env=ADMIN)
        data = expect_ok("逐张验签", run)
        check("验签零不可信", int(data["untrusted"]) == 0 and int(data["tables"]) >= 1, run.brief())

        run = cli("tiers", "get", "global", "-", "--revisions", "5", "--json", env=ADMIN)
        data = expect_ok("取当前版并带历史", run)
        check("默认取的是 revision 2", data["table"]["revision"] == 2, run.brief())
        revs = {item["revision"]: item["digest"] for item in data["table"]["revisions"]}
        check("历史里有 v1/v2 两版", set(revs) == {1, 2}, run.brief())

        run = cli("tiers", "get", "global", "-", "--revision", "1", "--json", env=ADMIN)
        data = expect_ok("按版本号回看 v1", run)
        check("v1 的 defaultTier 是 auto", data["table"]["document"]["defaultTier"] == "auto", run.brief())
        check("v1 摘要与历史一致", data["table"]["digest"] == revs[1], run.brief())

        run = cli("tiers", "revert", "global", "-", "--revision", "1", "--json", env=ADMIN)
        data = expect_ok("回滚到 v1 的内容", run)
        check("回滚产出新版本 3", data["revision"] == 3, run.brief())
        check("点名回滚来源版本", data["revertToRevision"] == 1, run.brief())

        run = cli("tiers", "revert", "global", "-", "--revision", "3", "--json", env=ADMIN)
        expect_fail("回滚到当前或更新的版本被挡", run, 1, "TIER_NOT_AHEAD")

        run = cli("tiers", "revert", "global", "-", "--revision", "99", "--json", env=ADMIN)
        expect_fail("回滚到不存在的版本给 404 码", run, 1, "TIER_REVISION_NOT_FOUND")

        run = cli("tiers", "withdraw", "global", "-", "--json", env=ADMIN)
        data = expect_ok("撤销下发", run)
        check("如实说明客户端回落口径", "models.json" in str(data.get("clientBehavior", "")), run.brief())

        run = cli("tiers", "effective", "--json", env=ADMIN)
        expect_fail("撤销后又回到没有", run, 1, "TIER_TABLE_NOT_FOUND")

        print("\n[审计与合规面]")
        run = cli("audit", "verify", "--json", env=ADMIN)
        data = expect_ok("全链复算", run)
        check("链是好的", data["chain"]["ok"] is True, run.brief())
        check("链头是 64 位", len(str(data["chain"]["headHash"])) == 64, run.brief())

        run = cli("audit", "list", "--limit", "5", "--json", env=ADMIN)
        data = expect_ok("翻页取审计", run)
        check("一次最多 5 条", len(data["entries"]) <= 5 and len(data["entries"]) >= 1, run.brief())
        cursor = int(data["nextAfter"])

        run = cli("audit", "list", "--after", str(cursor), "--limit", "5", "--json", env=ADMIN)
        data = expect_ok("游标续页", run)
        check("续页都是更大的 id", all(int(item["id"]) > cursor for item in data["entries"]), run.brief())

        run = cli("audit", "list", "--limit", "5000", "--json", env=ADMIN)
        expect_fail("超上限由服务端挡下", run, 1, "INVALID_REQUEST")

        run = cli("audit", "export", "--limit", "20", "--json", env=ADMIN)
        data = expect_ok("不给 --out 只报条数与预览", run)
        check("报 bytes 与 lines", int(data["bytes"]) > 0 and int(data["lines"]) > 0, run.brief())
        check("预览最多三行", len(data["preview"]) <= 3, run.brief())

        ndjson_path = os.path.join(_OUT, "audit.ndjson")
        run = cli("audit", "export", "--limit", "50", "--out", ndjson_path, "--json", env=ADMIN)
        data = expect_ok("导出落盘", run)
        WRITTEN_FILES.append(ndjson_path)
        check("写出的就是给的路径", data["written"] == ndjson_path, run.brief())
        with open(ndjson_path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        parsed = [json.loads(line) for line in lines]
        check("每行都是合法 JSON", len(parsed) == len(lines) and len(lines) >= 1, ndjson_path)
        check("导出行带 id 与摘要", all({"id", "entryHash"} <= set(item) for item in parsed), ndjson_path)

        run = cli("audit", "compliance", "--brief", "--json", env=ADMIN)
        data = expect_ok("合规简报", run)
        check("没有锚点前不可信", data["trustworthy"] is False, run.brief())
        warn_codes = {str(item.get("code")) for item in data["warnings"]}
        check("简报把缺锚点点名成警告", "NO_ANCHOR" in warn_codes, run.brief())
        check("简报把缺远端归档点名成警告", "NO_REMOTE_ARCHIVE" in warn_codes, run.brief())
        check("此时没有错误级发现", data["errors"] == [], run.brief())

        run = cli("audit", "compliance", "--json", env=ADMIN)
        data = expect_ok("完整合规报告", run)
        report = data["report"]
        check("报告带契约版本", str(report.get("contractVersion", "")).startswith("audit-compliance"), run.brief())
        check("报告带区间", bool(report.get("period")), run.brief())

        run = cli("audit", "compliance", "--days", "0", "--json", env=ADMIN)
        expect_fail("非法区间由服务端挡下", run, 1, "INVALID_REQUEST")

        run = cli("audit", "anchor-create", "--json", env=ADMIN)
        data = expect_ok("签发第一个锚点", run)
        check("锚点覆盖到当前链头", len(str(data["headHash"])) == 64, run.brief())
        check("默认输出不带签名串", "signature" not in data, run.brief())
        first_signature_absent = "eyJ" not in run.text
        check("默认输出里找不到 JWT 形状的东西", first_signature_absent, run.text[:200])

        run = cli("audit", "anchor-create", "--print-signature", "--json", env=ADMIN)
        data = expect_ok("显式要求才打签名", run)
        check("签名串是 RS256 JWT", str(data.get("signature", "")).startswith("eyJ"), run.brief())
        check("第二个锚点 seq 更大", int(data["seq"]) == 2, run.brief())

        run = cli("audit", "anchor-verify", "--json", env=ADMIN)
        data = expect_ok("锚点全部可信", run)
        check("untrusted=0", int(data["untrusted"]) == 0 and int(data["anchors"]) == 2, run.brief())
        check("口径句说全部可信", data["statement"] == "全部锚点可信", run.brief())

        run = cli("audit", "anchors", "--json", env=ADMIN)
        data = expect_ok("列锚点", run)
        check("列表带归档状态", all("archiveState" in item for item in data["anchors"]), run.brief())
        check("列锚点不回显签名", not any("signature" in item for item in data["anchors"]), run.brief())

        run = cli("audit", "archive", "--json", env=ADMIN)
        data = expect_ok("未配置归档时如实说没推", run)
        check("pushed=False", data["pushed"] is False, run.brief())
        check("原因点名环境变量", "TT_AUDIT_ARCHIVE_URL" in str(data.get("reason", "")), run.brief())

        run = cli("audit", "archive-status", "--json", env=ADMIN)
        data = expect_ok("归档状态", run)
        check("状态说未启用", data["archive"]["enabled"] is False, run.brief())
        check("带按状态计数", isinstance(data.get("byState"), dict), run.brief())

        run = cli("audit", "compliance", "--brief", "--json", env=ADMIN)
        data = expect_ok("有锚点后简报转可信", run)
        check("trustworthy=True", data["trustworthy"] is True, run.brief())

        md_path = os.path.join(_OUT, "compliance.md")
        run = cli("audit", "compliance-export", "--out", md_path, "--json", env=ADMIN)
        data = expect_ok("合规报告 Markdown 落盘", run)
        WRITTEN_FILES.append(md_path)
        with open(md_path, "r", encoding="utf-8") as handle:
            markdown = handle.read()
        check("报告写了「证明不了什么」", "本报告证明不了什么" in markdown, md_path)
        check("报告是 markdown 标题结构", markdown.lstrip().startswith("# "), md_path[:80])

        print("\n[doctor 能抓住坏消息]")
        target_id = int(parsed[max(range(len(parsed)), key=lambda i: parsed[i]["id"])]["id"])
        original = patch_audit_cell(target_id, "after_json", '{"tampered": true}')
        try:
            run = cli("doctor", "--json", "--url", DEAD)
            expect_fail("改过一行审计：本地自检报断链", run, 1, "AUDIT_CHAIN_BROKEN")
            check("doctor 的链判定同步变坏", run.data["auditChain"]["ok"] is False, run.brief())
            check("doctor 点名断在哪一行", str(target_id) in run.detail, run.brief())

            run = cli("audit", "verify", "--json", env=ADMIN)
            data = expect_ok("服务端口径也报断（同一把尺子）", run)
            check("服务端 chain.ok=False", data["chain"]["ok"] is False, run.brief())
            check("服务端点名同一行", any(int(item["id"]) == target_id for item in data["chain"]["breaks"]), run.brief())

            run = cli("audit", "anchor-verify", "--json", env=ADMIN)
            data = expect_ok("锚点巡检报不可信", run)
            check("两个锚点都不再与链一致", int(data["untrusted"]) >= 1, run.brief())

            run = cli("audit", "compliance", "--brief", "--json", env=ADMIN)
            data = expect_ok("简报出现错误级发现", run)
            codes = {str(item.get("code")) for item in data["errors"]}
            check("错误级里有断链与锚点不符", {"CHAIN_BREAK", "ANCHOR_MISMATCH"} <= codes, run.brief())
            check("简报转为不可信", data["trustworthy"] is False, run.brief())
        finally:
            patch_audit_cell(target_id, "after_json", original)

        run = cli("doctor", "--json", "--url", DEAD)
        expect_ok("还原后本地自检重新变绿", run)
        run = cli("audit", "anchor-verify", "--json", env=ADMIN)
        data = expect_ok("还原后锚点重新全可信", run)
        check("untrusted 归零", int(data["untrusted"]) == 0, run.brief())

        print("\n[备份：建包与验包]")
        pkg_a = os.path.join(_OUT, "pkg-a")
        run = cli("backup", "create", "--out", pkg_a, "--json", drop=("TT_SERVER_TOKEN",))
        data = expect_ok("建包是纯本地面：不需要任何凭据", run)
        check("包里落了库快照", os.path.exists(os.path.join(pkg_a, "market.db")), pkg_a)
        check("快照带 sha256 与字节数", len(str(data["sha256"])) == 64 and int(data["bytes"]) > 0, run.brief())
        manifest_a = manifest_of(pkg_a)
        check("清单记了契约版本", manifest_a["contractVersion"] == "tt-server-backup.v1", pkg_a)
        check("清单默认不含私钥", manifest_a["keysIncluded"] is False, pkg_a)
        check("清单里只有库文件一个条目", [item["name"] for item in manifest_a["files"]] == ["market.db"], pkg_a)

        run = cli("audit", "verify", "--json", env=ADMIN)
        live_head = str(run.data["chain"]["headHash"])
        check("备份清单的链头与服务端口径一致", manifest_a["auditChain"]["headHash"] == live_head, pkg_a)
        run = cli("users", "list", "--page-size", "1", "--json", env=ADMIN)
        check("备份清单的行数与账号面一致", manifest_a["rowCounts"]["users"] == int(run.data["total"]), pkg_a)

        run = cli("backup", "verify", pkg_a, "--json")
        data = expect_ok("验包通过", run)
        check("文件摘要逐一对上", all(item["matches"] is True for item in data["files"]), run.brief())
        check("口径句声明「没核验锚点签名」", "未核验锚点签名" in data["statement"], run.brief())
        check("清单原文随结果交出", data["manifest"]["rowCounts"]["users"] == manifest_a["rowCounts"]["users"], run.brief())

        print("\n[备份：包被动过就不放行]")
        conn = sqlite3.connect(os.path.join(pkg_a, "market.db"), timeout=20)
        conn.execute("UPDATE users SET nickname = ?1 WHERE uid = ?2", ("包外改过", member_uid))
        conn.commit()
        conn.close()
        run = cli("backup", "verify", pkg_a, "--json")
        expect_fail("包里改一个昵称也被判与清单不符", run, 1, "BACKUP_VERIFY_FAILED")
        check("findings 点名是 market.db", any("market.db" in str(item) for item in run.data["findings"]), run.brief())
        run = cli("backup", "restore", pkg_a, "--yes", "--allow-overwrite", "--json")
        expect_fail("坏包不放行：restore 先跑验包", run, 1, "BACKUP_VERIFY_FAILED")

        run = cli("backup", "verify", os.path.join(_OUT, "no-such-pkg"), "--json")
        expect_fail("目录里没有清单就不是备份包", run, 1, "BACKUP_PACKAGE_MISSING")

        v0 = os.path.join(_OUT, "pkg-v0")
        os.makedirs(v0, exist_ok=True)
        with open(os.path.join(v0, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"contractVersion": "tt-server-backup.v0", "files": []}, handle)
        run = cli("backup", "verify", v0, "--json")
        expect_fail("本版本读不了的旧包口径直接拒", run, 1, "BACKUP_CONTRACT_UNSUPPORTED")

        print("\n[备份：恢复的三道确认]")
        pkg_b = os.path.join(_OUT, "pkg-roundtrip")
        run = cli("backup", "create", "--out", pkg_b, "--json")
        expect_ok("建恢复演练用的包", run)
        manifest_b = manifest_of(pkg_b)

        run = cli("backup", "restore", pkg_b, "--json")
        expect_fail("不加 --yes 直接拒", run, 2, "USAGE", "--yes")
        run = cli("backup", "restore", pkg_b, "--yes", "--json")
        expect_fail("目标已有非空库时要 --allow-overwrite", run, 1, "BACKUP_TARGET_EXISTS")

        late_email = f"cli-late-{stamp}@test.local"
        register(late_email, "备份之后建的账号")
        run = cli("users", "list", "--query", f"cli-late-{stamp}", "--json", env=ADMIN)
        check("备份之后建的账号先确认在", int(run.data["total"]) == 1, run.brief())

        run = cli("backup", "restore", pkg_b, "--yes", "--allow-overwrite", "--json")
        data = expect_ok("就地恢复到备份点", run)
        check("覆盖前留了 .pre-restore 副本", bool(data["safetyCopy"]) and os.path.exists(data["safetyCopy"]), run.brief())
        check("恢复后的行数回到备份点", data["rowCounts"]["users"] == manifest_b["rowCounts"]["users"], run.brief())

        run = cli("users", "list", "--query", f"cli-late-{stamp}", "--json", env=ADMIN)
        check("备份之后建的账号已经不在库里", int(run.data["total"]) == 0, run.brief())

        run = cli("doctor", "--json", "--url", DEAD)
        data = expect_ok("恢复后本地自检全绿", run)
        check("恢复后链头等于清单记的那一个", data["auditChain"]["headHash"] == manifest_b["auditChain"]["headHash"], run.brief())
        check("恢复后账号行数与清单一致", data["rowCounts"]["users"] == manifest_b["rowCounts"]["users"], run.brief())

        run = cli("audit", "anchor-verify", "--json", env=ADMIN)
        data = expect_ok("回滚后的锚点仍然可信", run)
        check("untrusted=0", int(data["untrusted"]) == 0, run.brief())

        print("\n[备份：带密钥的换机演练]")
        pkg_c = os.path.join(_OUT, "pkg-with-keys")
        run = cli("backup", "create", "--out", pkg_c, "--include-keys", "--json")
        data = expect_ok("显式带密钥建包", run)
        check("清单记了含私钥", data["keysIncluded"] is True, run.brief())
        check("输出点名这是机密", any("机密" in str(item) for item in data["warnings"]), run.brief())
        manifest_c = manifest_of(pkg_c)
        key_files = [item["name"] for item in manifest_c["files"] if str(item["name"]).startswith("keys/")]
        check("密钥文件逐个进了摘要清单", len(key_files) >= 2, pkg_c)

        run = cli("backup", "verify", pkg_c, "--json")
        data = expect_ok("带钥的包同样验过", run)
        check("keys/ 条目也逐个复核了摘要", all(item["matches"] is True for item in data["files"]), run.brief())

        fresh = os.path.join(_OUT, "fresh-root")
        os.makedirs(fresh, exist_ok=True)
        run = cli(
            "backup",
            "restore",
            pkg_c,
            "--yes",
            "--to",
            fresh,
            "--with-keys",
            "--json",
            env={"TT_SERVER_DATA_DIR": fresh},
        )
        data = expect_ok("恢复到另一个数据根", run)
        check("新数据根落了库", os.path.exists(os.path.join(fresh, "market.db")), fresh)
        check("新数据根落了密钥清单", os.path.exists(os.path.join(fresh, "keys", "index.json")), fresh)
        check("新库的链自洽", data["auditChain"]["ok"] is True, run.brief())

        run = cli("doctor", "--json", env={"TT_SERVER_DATA_DIR": fresh})
        data = expect_ok("换机演练：新数据根自检全绿", run)
        check("新根用恢复来的公钥仍能验锚点", int(data["anchors"]["untrusted"]) == 0, run.brief())
        check("新根锚点数量没丢", int(data["anchors"]["anchors"]) >= 2, run.brief())
        check(
            "恢复全程没碰过那个空数据根",
            not os.path.exists(os.path.join(_EMPTY, "keys")),
            "之前只读自检用的空数据根被写过了",
        )

        run = cli("backup", "create", "--out", pkg_c, "--json")
        expect_fail("同一目录重复建包要先问清楚", run, 1, "BACKUP_DIR_TAKEN")
        run = cli("backup", "create", "--out", pkg_c, "--force", "--json")
        expect_ok("显式 --force 才覆盖旧包", run)

        print("\n[启停：一条命令拉起新数据根]")
        run = cli("serve", "--host", "0.0.0.0", "--json")
        expect_fail("绑非回环地址要先加 --allow-public", run, 2, "USAGE", "--allow-public")
        run = cli("serve", "--port", "0", "--json")
        expect_fail("端口 0 不静默退回 8000", run, 2, "USAGE", "1–65535")
        run = cli("serve", "--port", "abc", "--json")
        expect_fail("非数字端口是用法错误", run, 2, "USAGE")
        run = cli("serve", "--log-level", "noisy", "--json")
        expect_fail("日志级别只认 uvicorn 那几档", run, 2, "USAGE")

        serve_log = []
        serve_proc = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "app.cli",
                "serve",
                "--port",
                str(SERVE_PORT),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env={**os.environ, "TT_SERVER_DATA_DIR": _SERVE},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            status = None
            body = ""
            waited = 0.0
            # 有界轮询而不是固定睡眠：机器快就早退出，机器慢也不会有假绿
            while waited < 30.0 and serve_proc.poll() is None:
                try:
                    with urllib.request.urlopen(SERVE_URL + "/health", timeout=2.0) as resp:
                        status, body = int(resp.status), resp.read().decode("utf-8", "replace")
                    break
                except Exception:
                    time.sleep(0.2)
                    waited += 0.2
            check("serve 一条命令就能把服务拉起来并访问到", status == 200, f"body={body[:200]!r} rc={serve_proc.poll()}")
            check("起来的确实是 tongtian-server", json.loads(body).get("service") == "tongtian-server", body[:200])
            check("新数据根被就地初始化了库", os.path.exists(os.path.join(_SERVE, "market.db")), _SERVE)

            run = cli("health", "--json", env={"TT_SERVER_URL": SERVE_URL}, drop=("TT_SERVER_TOKEN",))
            data = expect_ok("对刚拉起的服务跑 health", run)
            check("health 报的是那个新实例", str(data.get("service")) == "tongtian-server", run.brief())

            run = cli(
                "whoami",
                "--json",
                env={
                    "TT_SERVER_URL": SERVE_URL,
                    "TT_SERVER_ADMIN_EMAIL": "admin@test.local",
                    "TT_SERVER_ADMIN_PASSWORD": ADMIN_PASSWORD,
                },
                drop=("TT_SERVER_TOKEN",),
            )
            data = expect_ok("新实例自带管理员且能环境变量登录", run)
            check("登录后的身份是管理员", str(data.get("role")) == "admin", run.brief())
            check("新数据根自己生成了签名钥", os.path.exists(os.path.join(_SERVE, "keys", "index.json")), _SERVE)

            # 主根的令牌在新根里没有对应会话，必须在新根重新用环境变量换一枚
            run = cli(
                "users",
                "list",
                "--json",
                env={
                    "TT_SERVER_URL": SERVE_URL,
                    "TT_SERVER_ADMIN_EMAIL": "admin@test.local",
                    "TT_SERVER_ADMIN_PASSWORD": ADMIN_PASSWORD,
                },
                drop=("TT_SERVER_TOKEN",),
            )
            data = expect_ok("对新实例列账号", run)
            check("新实例只看到自己根里的账号", int(data.get("total")) >= 1, run.brief())
            check(
                "新实例的账号数远少于主根（数据没串门）",
                int(data.get("total"))
                < int(expect_ok("主根账号数作为对照", cli("users", "list", "--json", env=ADMIN)).get("total")),
                run.brief(),
            )

            run = cli("doctor", "--json", env={"TT_SERVER_DATA_DIR": _SERVE})
            codes = {str(item.get("code")) for item in run.problems}
            check(
                "刚拉起的根没有缺库/坏页/缺表",
                codes.isdisjoint({"DB_MISSING", "DB_UNREADABLE", "DB_INTEGRITY", "SCHEMA_INCOMPLETE"}),
                run.brief(),
            )
            check("自检看的就是刚拉起的那个目录", os.path.samefile(str(run.data["dataDir"]), _SERVE), run.brief())
            check(
                "只读自检不替新根生成锚点",
                int(run.data["anchors"]["anchors"]) == 0,
                run.brief(),
            )

            run = cli("doctor", "--json")
            expect_ok("主测试根的自检不受影响", run)
        finally:
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.wait(timeout=20)
            try:
                serve_log.append(serve_proc.stdout.read() or "")
            except Exception:
                pass
            # Windows 上 terminate() 是硬杀，优雅停机是否仍能把信封打出来本机验不了
            check("serve 进程收得回来", serve_proc.returncode is not None, str(serve_proc.returncode))
            check("停掉之后端口不再应答", not _port_open(SERVE_PORT), SERVE_URL)
        for chunk in serve_log:
            CAPTURED.append(chunk)

        print("\n[凭据泄漏总扫]")
        everything = "\n".join(CAPTURED)
        for written in WRITTEN_FILES:
            with open(written, "r", encoding="utf-8") as handle:
                everything += "\n" + handle.read()
        check("全部输出里没有管理员口令", ADMIN_PASSWORD not in everything, "")
        check("全部输出里没有账号口令", ACCOUNT_PASSWORD not in everything, "")
        check("全部输出里没有访问令牌", admin_token not in everything, "")
        check("全部输出里没有成员令牌", member_token not in everything, "")
        check("全部输出里没有 Authorization 头", "Bearer " not in everything, "")
        check("全部输出里没有 JWT 密钥", "cli-test-secret" not in everything, "")
        check("全程没有裸栈外泄", "Traceback (most recent call last)" not in everything, "")

        print("\n[人读面]")
        run = cli("health", env=ADMIN)
        check("不带 --json 时打的是键值行", "service: tongtian-server" in run.stdout, run.brief())
        check("人读面不是 JSON", not run.stdout.strip().startswith("{"), run.stdout[:80])
        run = cli("users", "list", "--page-size", "1", env=ADMIN)
        check("列表命令人读面用短横线分条", "total:" in run.stdout, run.brief())
        run = cli("audit", "bogus", env=ADMIN)
        check("人读面的错误走 stderr 而不是 stdout", run.stdout.strip() == "" and run.stderr.strip() != "", run.brief())
    finally:
        server.should_exit = True
        time.sleep(0.3)

    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
