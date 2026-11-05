#!/usr/bin/env python3
"""#157 工作区后端的**端点面**自测（workspace 车道：/api/workspace/*）。

跑的是「引擎端点 → 后端运行时」这条真实接线：请求体怎么变成 SandboxPolicy、
三段判定的结论怎么原样回到界面、准入判定在哪一层生效、审计从哪一层落盘。
后端本体（配置解析、命令行/SQL/签名、凭据口径）由
`engine_workspace_backend_selftest.py` 钉，本文件不重跑那 184 项，
只复用同一批假件（假 DB-API、假对象存储端点、假 docker CLI），不另造一套。

必须用 `./vendor/python/python.exe -X utf8` 跑；缺 fastapi/agentscope 时整族 SKIP 且 rc=0。

**假端点绿不等于真后端能用**：真容器、真库、真桶、真 SSH 主机一律未验收，
本文件里凡是靠注入口跑通的用例都在名字里写了「假件」。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_workspace_backend_routes_selftest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "integration"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-ws-routes-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

ENGINE_MODULES = {path.stem for path in (PROJECT_ROOT / "engine").glob("*.py")}


def import_gate(exc: BaseException, suite: str) -> None:
    """只在**真缺第三方依赖**时优雅 SKIP；我们自己的引擎文件导入断了必须大声红。

    早先这里是 ``except Exception`` 一把吞：``engine/mcp_errors.py`` 一次装配错就把整族
    报成「SKIP + rc=0」，126 项自测瞬间变成 0 项还全绿——那正是「假绿写进真台账」的形状。
    """
    root = exc
    for _ in range(12):
        nxt = root.__cause__ or root.__context__
        if nxt is None:
            break
        root = nxt
    missing = getattr(root, "name", "") or ""
    if isinstance(root, ModuleNotFoundError) and missing not in ENGINE_MODULES:
        print(f"SKIP: {suite} unavailable（缺第三方依赖 {missing or root}）")
        sys.exit(0)
    print(f"[FAIL] {suite} 起不来：引擎侧导入断了，不是依赖缺失 — {type(root).__name__}: {root}")
    print("[FAIL] 整族未运行（拒绝把上面的断裂报成 SKIP）")
    sys.exit(1)


try:
    import engine_workspace_backend_selftest as fixtures  # 同一批假件，不另造
    import routes_workspace as rw
    import routes_lanes as rl
    import sandbox_runtime as sr
    import workspace_backend_runtime as wb
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "workspace routes selftest")

try:
    import server
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "workspace routes selftest")

# fixtures 在 import 时把数据根指到它自己的临时目录，这里再拧回本文件的目录：
# 审计落点必须是临时目录，不能是真的引擎数据目录（假用量写进真台账的代价记在案）。
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

FAILURES = []

LOCAL_TOKEN = "tt-selftest-local-token"
STATIC_TOKEN = "tt-selftest-static-token"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def headers(token: str = LOCAL_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def section(title: str) -> None:
    print(f"--- {title} ---")


# ── 现场装配 ────────────────────────────────────────────────────


def make_home(name: str) -> Path:
    home = _TMP / name
    (home / "work").mkdir(parents=True, exist_ok=True)
    return home


def attach_backend(backend) -> str:
    problem = wb.REGISTRY.add(backend)
    assert not problem, problem
    return backend.spec.backend_id


def assemble(home: Path) -> dict[str, object]:
    """把四类后端装进清单（docker/PG/S3 走假传输件，注入口在生产接线里不存在）。"""
    wb.reset_registry()
    wb._MEMORY_AUDIT.clear()

    calls: list[tuple[list[str], int]] = []

    def fake_run(argv, timeout):
        calls.append((list(argv), timeout))
        return 0, "容器内 stdout\n", ""

    mount = home / "work"
    local = wb.build_backend(
        wb.parse_spec({"backendId": "rt-local", "kind": "local", "options": {"dir": str(mount)}})
    )
    ro = wb.build_backend(
        wb.parse_spec(
            {"backendId": "rt-ro", "kind": "local", "readOnly": True, "options": {"dir": str(mount)}}
        )
    )
    docker = wb.build_backend(
        wb.parse_spec(
            {
                "backendId": "rt-docker",
                "kind": "docker",
                "options": {"dir": str(mount), "image": "python:3.12-slim"},
            }
        ),
        deps={"run_fn": fake_run},
    )
    rows: dict[tuple[str, str], tuple[str, int, str]] = {}
    sql_log: list = []
    conn = fixtures.FakeConnection(rows, sql_log)

    def connect_fn(dsn):
        conn.dsn = dsn
        return conn

    pg = wb.build_backend(
        wb.parse_spec(
            {
                "backendId": "rt-pg",
                "kind": "postgresql",
                "prefix": "docs",
                "options": {
                    "host": "db.invalid",
                    "port": "5432",
                    "dbname": "tt",
                    "user": "tt_ro",
                    "password": "env:TT_SELFTEST_PG_PW",
                    "schema": "app",
                    "table": "ws_object",
                },
            }
        ),
        deps={"connect_fn": connect_fn},
        environ={"TT_SELFTEST_PG_PW": "pg-fake-secret"},
    )
    store = fixtures.FakeStore({"docs/a.md": b"hello object"})
    s3 = wb.build_backend(
        wb.parse_spec(
            {
                "backendId": "rt-s3",
                "kind": "s3",
                "prefix": "docs",
                "options": {
                    "bucket": "tt-fake-bucket",
                    "endpoint": "https://s3.example.invalid",
                    "accessKeyId": "env:TT_SELFTEST_AK",
                    "secretKey": "env:TT_SELFTEST_SK",
                },
            }
        ),
        deps={"request_fn": store},
        environ={"TT_SELFTEST_AK": "AKID", "TT_SELFTEST_SK": "sk-fake-value"},
    )
    for backend in (local, docker, pg, s3, ro):
        attach_backend(backend)
    wb.REGISTRY.switch("rt-local")
    return {"mount": mount, "calls": calls, "rows": rows, "sql": sql_log, "store": store}


def add_session(task_id: str, work_dir: Path, runtime: dict) -> None:
    session = server.TaskSession(task_id)
    session.work_dir = work_dir
    session.runtime = runtime
    server._TASK_SESSIONS[task_id] = session


def drop_session(task_id: str) -> None:
    server._TASK_SESSIONS.pop(task_id, None)


# ── 1. 挂载与路径面 ────────────────────────────────────────────


def mount_checks() -> None:
    section("1. 车道挂载与路径登记（#164 接缝）")
    row = next((item for item in server.ROUTE_LANE_REPORT if item["lane"] == "workspace"), None)
    check("workspace 车道挂上了且端点数与登记一致", row and row["endpoints"] == len(rw.ENDPOINT_PATHS), str(row))
    app_paths = {(m, r.path) for r in server.app.routes for m in (r.methods or set())}
    get_paths = set(rw.READ_ONLY_PATHS)
    registered = {("GET" if p in get_paths else "POST", p) for p in rw.ENDPOINT_PATHS}
    actually_get = {r.path for r in server.app.routes if "GET" in (r.methods or set()) and r.path.startswith("/api/snapshot")} | \
                   {r.path for r in server.app.routes if "GET" in (r.methods or set()) and r.path.startswith("/api/workspace")}
    check("只读面走 GET 的清单与 app 上真实注册的方法一致（写面不靠 GET）",
          actually_get == set(rw.READ_ONLY_PATHS) and len(rw.READ_ONLY_PATHS) == 7,
          f"app={sorted(actually_get)}")
    missing = sorted(registered - app_paths)
    check("登记的每条路径都真在 app 上（没有只写在常量里却没挂上的）", not missing, str(missing))
    check("操作面路径都登记在 ENDPOINT_PATHS 里", set(rw.OP_PATHS) <= set(rw.ENDPOINT_PATHS))
    check("#140 已占用 /api/snapshot，且仍在本车道声明的前缀里（不越界到别家）",
          all(p.startswith(rl.LANE_PREFIXES["workspace"]) for p in rw.ENDPOINT_PATHS),
          str([p for p in rw.ENDPOINT_PATHS if not p.startswith(rl.LANE_PREFIXES["workspace"])])[:200])
    check("常量没有第二份定义（两处 READ_ONLY_PATHS 迟早分叉）",
          (PROJECT_ROOT / "engine" / "routes_workspace.py").read_text(encoding="utf-8").count(
              "\nREAD_ONLY_PATHS = ("
          ) == 1)
    rl.check_lane(rw)
    check("车道自检通过（没有越界占用别家前缀）", True)
    sources = set(rw.DENIAL_SOURCE_LABELS)
    check(
        "三段拒绝来源都有标签且标签互不相同（档位句与沙箱句不许共用文案）",
        sources == {wb.SOURCE_BACKEND_BOUNDARY, wb.SOURCE_BACKEND, wb.SOURCE_SANDBOX, wb.SOURCE_FILE_PERMISSION}
        and len(set(rw.DENIAL_SOURCE_LABELS.values())) == 4,
        str(rw.DENIAL_SOURCE_LABELS),
    )
    lane_src = (PROJECT_ROOT / "engine" / "routes_workspace.py").read_text(encoding="utf-8")
    check("车道不 import server（引擎现场只经 routes_lanes.engine()）", "import server" not in lane_src)
    check(
        "车道不从请求体把 environ 递给装配面（凭据只从引擎进程环境/钥匙串解析）",
        "environ=" not in lane_src and 'payload.get("environ")' not in lane_src,
    )


# ── 2. 准入 ────────────────────────────────────────────────────


def admission_checks(client: TestClient) -> None:
    section("2. 准入：桌面壳/回环放行，远程身份整条车道不开放")
    os.environ.pop("TT_ENGINE_TOKEN", None)
    os.environ.pop("TT_ENGINE_LOCAL_TOKEN", None)
    resp = client.get(rw.ENDPOINT_PATHS[0])
    check("没配任何凭据（本机回环）时清单可读", resp.status_code == 200, str(resp.status_code))

    os.environ["TT_ENGINE_TOKEN"] = STATIC_TOKEN
    os.environ["TT_ENGINE_LOCAL_TOKEN"] = LOCAL_TOKEN
    resp = client.get(rw.ENDPOINT_PATHS[0], headers=headers(LOCAL_TOKEN))
    check("桌面壳本机令牌可读清单", resp.status_code == 200, str(resp.status_code))

    remote = client.get(rw.ENDPOINT_PATHS[0], headers=headers(STATIC_TOKEN))
    check("远程身份读清单被拒（403 + WS_DESKTOP_ONLY）", remote.status_code == 403 and remote.json()["code"] == wb.CODE_DESKTOP_ONLY, str(remote)[:200])
    body = remote.text
    check("拒绝理由点名「按人隔离尚未落地」而不是含糊的「无权限」", "按人隔离" in body, body[:200])
    check("拒绝理由不含「已支持对象存储」这类声称（约束 A）", "已支持对象存储" not in body)

    for path in rw.OP_PATHS:
        resp = client.post(path, headers=headers(STATIC_TOKEN), json={"backendId": "rt-local", "path": "x.md", "content": "x", "command": "ls", "from": "a", "to": "b"})
        check(f"远程身份下 {path} 一律 403", resp.status_code == 403 and resp.json().get("code") == wb.CODE_DESKTOP_ONLY, f"{resp.status_code} {resp.text[:120]}")
    for path in ("/api/workspace/backends/configure", "/api/workspace/switch", "/api/workspace/backends/probe", "/api/workspace/audit"):
        method = "get" if path == "/api/workspace/audit" else "post"
        resp = getattr(client, method)(path, headers=headers(STATIC_TOKEN), json={}) if method == "post" else client.get(path, headers=headers(STATIC_TOKEN))
        check(f"远程身份下 {path} 也一律 403", resp.status_code == 403, f"{resp.status_code} {resp.text[:120]}")

    resp = client.get(rw.ENDPOINT_PATHS[0])
    check("引擎配了令牌而未带令牌时先被守卫拒（本车道没跑）", resp.status_code == 401, str(resp.status_code))
    os.environ.pop("TT_ENGINE_TOKEN", None)
    os.environ.pop("TT_ENGINE_LOCAL_TOKEN", None)


# ── 3. 清单装配 / 探测 / 切换 ──────────────────────────────────


def registry_checks(client: TestClient, home: Path) -> None:
    section("3. 清单装配、探测草稿与切换（设置页那一屏的后端）")
    wb.reset_registry()
    wb._MEMORY_AUDIT.clear()
    good = {"backendId": "cfg-local", "kind": "local", "options": {"dir": str(home / "work")}}
    bad = {"backendId": "cfg-docker", "kind": "docker", "options": {"dir": str(home / "work")}}
    resp = client.post("/api/workspace/backends/configure", json={"backends": [good, bad]})
    payload = resp.json()
    check("HTTP 装配只装进合法条目", resp.status_code == 200 and payload["added"] == ["cfg-local"], str(payload)[:200])
    check("坏配置留在 rejected 里并说清缺的是 options.image", payload["rejected"] and "image" in str(payload["rejected"]), str(payload["rejected"]))
    check("坏配置没进清单（切不过去也动不了）", "cfg-docker" not in wb.REGISTRY.ids(), str(wb.REGISTRY.ids()))

    listing = client.get("/api/workspace/backends").json()
    check("清单里能同时看到活动后端与被拒条目", listing["activeBackendId"] == "cfg-local" and listing["rejected"], str(listing)[:200])
    check("清单回话给了档位模式与凭据口径（界面不自己抄一份）", listing["filePermissionModes"] == list(sr.FILE_PERMISSION_MODES) and "env:" in listing["credentialPolicy"], str(listing)[:160])
    check("清单回话里的 endpoints 与常量同源", listing["endpoints"] == list(rw.ENDPOINT_PATHS), str(listing["endpoints"]))

    switch_bad = client.post("/api/workspace/switch", json={"backendId": "cfg-docker"})
    check("切到被拒后端给 SPEC_INVALID 并带原因", switch_bad.status_code == 400 and switch_bad.json()["code"] == wb.CODE_SPEC_INVALID and switch_bad.json()["rejectedReasons"], str(switch_bad.text[:200]))
    switch_missing = client.post("/api/workspace/switch", json={"backendId": "nope"})
    check("切到没写过的 id 给 404（与被拒是两句话）", switch_missing.status_code == 404 and switch_missing.json()["code"] == wb.CODE_BACKEND_NOT_FOUND, str(switch_missing.text[:200]))

    resp = client.post("/api/workspace/backends/configure", json={"backends": [good], "activeBackendId": "cfg-local"})
    check("重复装配同一条不炸且活动后端可指", resp.json()["ok"] is True and wb.REGISTRY.active_id() == "cfg-local", str(resp.json())[:160])

    draft = {"backendId": "draft-pg", "kind": "postgresql", "options": {"host": "db.invalid", "dbname": "tt", "user": "tt", "password": "env:TT_ABSENT"}}
    probe = client.post("/api/workspace/backends/probe", json={"backend": draft})
    body = probe.json()
    check("草稿探测不装进清单（带病配置进清单就等于切得过去）", body.get("registered") is False and "draft-pg" not in wb.REGISTRY.ids(), str(body)[:200])
    check("草稿探测回话点名真库未验收", any("真 PostgreSQL" in item for item in body.get("unverified") or []), str(body)[:200])
    check("草稿探测状态是未验收（配置合法不等于实机验过）", body.get("status") == wb.STATUS_UNVERIFIED, str(body.get("status")))

    anon = client.post("/api/workspace/backends/probe", json={"backend": {"kind": "postgresql"}})
    check("草稿没名字时拒绝探测（审计说不清探的是哪一个）", anon.status_code == 400 and anon.json()["code"] == wb.CODE_BACKEND_REQUIRED, str(anon.text[:160]))
    both = client.post("/api/workspace/backends/probe", json={"backendId": "cfg-local", "backend": draft})
    check("同时给 backendId 与草稿时不猜以哪份为准", both.status_code == 400 and "要么" in both.json()["message"], str(both.text[:200]))
    bad_draft = client.post("/api/workspace/backends/probe", json={"backend": {"backendId": "d2", "kind": "s3", "options": {"bucket": "x"}}})
    check("非法草稿在探测面就报配置错（不装也不探）", bad_draft.status_code == 400 and bad_draft.json()["code"] == wb.CODE_SPEC_INVALID, str(bad_draft.text[:200]))
    probe_all = client.post("/api/workspace/backends/probe", json={})
    check("不带参数探测 = 探清单里全部", probe_all.status_code == 200 and "cfg-local" in (probe_all.json().get("probes") or {}), str(probe_all.json())[:200])


# ── 4. 三段判定经端点回话 ──────────────────────────────────────


def denial_checks(client: TestClient, home: Path, env: dict) -> None:
    section("4. 三段判定经端点回话：来源、code 与文案都不许合并（约束 B）")
    runtime = {"filePermission": "auto_approve", "sandbox": {"configured": True, "executionLevel": "L1", "fileBlocklist": ["secret/**"]}}
    add_session("t-deny", home / "work", runtime)
    base = {"session": "t-deny", "backendId": "rt-pg", "path": "notes/a.md", "content": "正文"}

    unsafe = client.post("/api/workspace/files/read", json={**base, "path": "../outside.md"}).json()
    drive = client.post("/api/workspace/files/read", json={**base, "path": "C:/Windows/a.md"}).json()
    sandbox_hit = client.post("/api/workspace/files/write", json={**base, "path": "secret/key.pem"}).json()
    tier = client.post("/api/workspace/files/write", json={**base, "path": "notes/a.md"}).json()
    delete_tier = client.post("/api/workspace/files/delete", json={**base, "path": "notes/a.md"}).json()

    check("操作面把 .. 收成非法路径（不交给后端做 traversal）", unsafe["code"] == wb.CODE_PATH_UNSAFE and unsafe["deniedBy"] == wb.SOURCE_BACKEND, str(unsafe)[:200])
    check("本机盘符写法在远程后端前被拒且点名「不承载盘符」", drive["code"] == wb.CODE_PATH_UNSAFE and "盘符" in drive["message"], str(drive)[:200])
    check("沙箱黑名单归沙箱", sandbox_hit["deniedBy"] == wb.SOURCE_SANDBOX and "黑名单" in sandbox_hit["message"], str(sandbox_hit)[:200])
    check("远程写被档位拒（不是被沙箱拒）", tier["deniedBy"] == wb.SOURCE_FILE_PERMISSION and tier["code"] == wb.CODE_TIER_DENIES_REMOTE, str(tier)[:200])
    check("远程删除未到完全访问也是档位句", delete_tier["deniedBy"] == wb.SOURCE_FILE_PERMISSION and delete_tier["code"] == wb.CODE_DELETE_NOT_FULL_ACCESS, str(delete_tier)[:200])
    messages = [row["message"] for row in (unsafe, drive, sandbox_hit, tier, delete_tier)]
    check("五句拒绝各是各的话（不共用文案）", len(set(messages)) == 5, str(messages)[:300])
    check("每条回话都给了来源标签（界面不必自己映射）", all(row.get("deniedByLabel") for row in (unsafe, drive, sandbox_hit, tier, delete_tier)), str([r.get("deniedByLabel") for r in (unsafe, sandbox_hit, tier)][:3]))
    check("档位句点名「切完全访问也管不到根前缀与只读」", "根前缀" in tier["message"] and "只读" in tier["message"], tier["message"][:200])
    check("回话带回档位读数", tier["policy"]["filePermission"] == "auto_approve" and tier["policy"]["policySource"].startswith("会话"), str(tier["policy"]))

    l0 = client.post("/api/workspace/files/write", json={**base, "runtime": {"filePermission": "full_access", "sandbox": {"configured": True, "executionLevel": "L0", "fileBlocklist": [], "fileAllowlist": []}}, "session": ""}).json()
    check("沙箱 L0 只读句归沙箱而不是档位", l0["deniedBy"] == wb.SOURCE_SANDBOX and l0["code"] == wb.CODE_CAPABILITY_DENIED, str(l0)[:200])
    check("L0 句与远程档位句不是同一句", l0["message"] != tier["message"])

    add_session("t-full", home / "work", {"filePermission": "full_access"})
    backend_delete = client.post("/api/workspace/files/delete", json={"session": "t-full", "backendId": "rt-pg", "path": "notes/a.md"}).json()
    check("档位够了但后端没开远程删除 → 来源是后端设置", backend_delete["deniedBy"] == wb.SOURCE_BACKEND and backend_delete["code"] == wb.CODE_DELETE_DENIED and "allowRemoteDelete" in backend_delete["message"], str(backend_delete)[:220])
    check("「后端未开启远程删除」与「档位没到完全访问」是两句不同的话", backend_delete["message"] != delete_tier["message"])
    ro_write = client.post("/api/workspace/files/write", json={"session": "t-full", "backendId": "rt-ro", "path": "x.md", "content": "写不进只读后端"}).json()
    check("只读后端连完全访问也写不了（来源后端）", ro_write["deniedBy"] == wb.SOURCE_BACKEND and ro_write["code"] == wb.CODE_BACKEND_READONLY, str(ro_write)[:200])
    check("只读句与非法路径句、档位句三句互不相同", len({ro_write["message"], unsafe["message"], tier["message"]}) == 3)
    drop_session("t-full")

    nosession = client.post("/api/workspace/files/read", json={"backendId": "rt-local", "session": "t-ghost", "path": "a.md"})
    check("会话不在本进程时回退最保守档位并说清", nosession.json()["policy"]["policySource"].startswith("会话 t-ghost") and nosession.json()["policy"]["filePermission"] == sr.DEFAULT_FILE_PERMISSION, str(nosession.json())[:220])
    default_policy = client.post("/api/workspace/files/list", json={"backendId": "rt-local"}).json()
    check("什么都没带时按缺省档位读数回话（不猜成完全访问）", default_policy["policy"]["filePermission"] == sr.DEFAULT_FILE_PERMISSION, str(default_policy)[:200])

    drop_session("t-deny")


# ── 5. 操作面真跑 ──────────────────────────────────────────────


def op_checks(client: TestClient, home: Path, env: dict) -> None:
    section("5. 操作面：本地真跑 + 假件后端（假 CLI / 假 DB-API / 假端点）")
    mount = env["mount"]
    full = {"filePermission": "full_access"}
    add_session("t-ops", mount, full)
    call = lambda path, payload: client.post(path, json={**payload, "session": "t-ops"})  # noqa: E731

    written = call("/api/workspace/files/write", {"backendId": "rt-local", "path": "notes/a.md", "content": "端点面写入的正文"})
    check("本地后端经端点写成功并落到绑定目录", written.status_code == 200 and written.json()["ok"] and (mount / "notes" / "a.md").read_text(encoding="utf-8") == "端点面写入的正文", str(written.text[:200]))
    read = call("/api/workspace/files/read", {"backendId": "rt-local", "path": "notes/a.md"}).json()
    check("端点读回正文且带后端信息", read["ok"] and read["content"] == "端点面写入的正文" and read["backendKind"] == "local", str(read)[:200])
    empty = call("/api/workspace/files/write", {"backendId": "rt-local", "path": "notes/empty.md", "content": ""}).json()
    check("空文本是合法载荷（写空文件不为「没带内容」）", empty["ok"] and (mount / "notes" / "empty.md").exists(), str(empty)[:160])
    listed = call("/api/workspace/files/list", {"backendId": "rt-local", "path": "notes"}).json()
    check("列目录经端点回条目", listed["ok"] and any(i["name"] == "a.md" for i in listed["items"]), str(listed)[:200])
    moved = call("/api/workspace/files/move", {"backendId": "rt-local", "from": "notes/empty.md", "to": "notes/moved.md"}).json()
    check("改名经端点可用并如实说明是复制", moved["ok"] and "复制" in str(moved.get("note")), str(moved)[:200])
    missing_arg = call("/api/workspace/files/write", {"backendId": "rt-local", "path": "x.md"})
    check("没带 content 键才叫缺参（与空文本分得开）", missing_arg.status_code == 400 and "content" in missing_arg.json()["message"], str(missing_arg.text[:200]))
    missing_to = call("/api/workspace/files/move", {"backendId": "rt-local", "from": "a.md", "to": None})
    check("改名的目标为 None 也算缺参", missing_to.status_code == 400 and missing_to.json()["code"] == wb.CODE_BACKEND_REQUIRED, str(missing_to.text[:160]))
    deleted = call("/api/workspace/files/delete", {"backendId": "rt-local", "path": "notes/a.md"}).json()
    check("完全访问下删除成功且留回收副本", deleted["ok"] and deleted.get("trashPath"), str(deleted)[:200])

    capability = call("/api/workspace/exec", {"backendId": "rt-local", "command": "ls"})
    check("本地后端起命令按能力位拒绝（403 而不是静默空跑）", capability.status_code == 403 and capability.json()["code"] == wb.CODE_CAPABILITY_DENIED, str(capability.text[:200]))
    docker_run = call("/api/workspace/exec", {"backendId": "rt-docker", "command": "pytest -q"}).json()
    check("docker 后端经端点起命令（假 CLI）", docker_run["ok"] and docker_run["returnCode"] == 0, str(docker_run)[:200])
    check("假 CLI 收到的是列表 argv 且带镜像", env["calls"] and "python:3.12-slim" in env["calls"][-1][0], str(env["calls"][-1][0][:6]))
    empty_cmd = call("/api/workspace/exec", {"backendId": "rt-docker", "command": "   "})
    check("空命令不猜成「列目录」而是缺参回话", empty_cmd.status_code == 400 and empty_cmd.json()["code"] == wb.CODE_BACKEND_REQUIRED, str(empty_cmd.text[:160]))

    pg_write = call("/api/workspace/files/write", {"backendId": "rt-pg", "path": "b.md", "content": "表里的正文"}).json()
    check("表后端经端点写入（假 DB-API）", pg_write["ok"] and pg_write["objPath"] == "docs/b.md", str(pg_write)[:200])
    check("落点是后端根 + 相对路径（换后端不换路径）", ("rt-pg", "docs/b.md") in env["rows"], str(sorted(env["rows"])))
    check("正文不落进 SQL 文本（按绑定参数传）", all("表里的正文" not in str(sql) for sql, _ in env["sql"]), str([s for s, _ in env["sql"]][:1]))
    pg_list = call("/api/workspace/files/list", {"backendId": "rt-pg", "path": ""}).json()
    check("表后端列表回的 path 相对后端根", pg_list["ok"] and all(not i["path"].startswith("docs/") for i in pg_list["items"]), str(pg_list["items"])[:200])

    s3_write = call("/api/workspace/files/write", {"backendId": "rt-s3", "path": "c.md", "content": "对象正文"}).json()
    check("对象存储经端点写成功（假端点，真桶未验收）", s3_write["ok"] and "docs/c.md" in env["store"].objects, str(s3_write)[:200])
    s3_list = call("/api/workspace/files/list", {"backendId": "rt-s3", "path": ""}).json()
    check("对象存储列表按后端根过滤并剥前缀", s3_list["ok"] and sorted(i["path"] for i in s3_list["items"]) == ["a.md", "c.md"], str(s3_list["items"])[:200])
    last_put = env["store"].requests[-1]
    check("假端点收到的签名头里没有密钥原值", "sk-fake-value" not in str(last_put["headers"]), str(last_put["headers"])[:160])

    backend_gone = call("/api/workspace/files/read", {"backendId": "rt-unknown", "path": "a.md"})
    check("操作面找不到后端也分「被拒」与「没写过」", backend_gone.status_code == 404, str(backend_gone.text[:160]))
    wb.REGISTRY.add(wb.build_backend(wb.parse_spec({"backendId": "rt-bad", "kind": "local", "options": {}})))
    check("直接装配缺 dir 的空壳后端不会静默可用", "rt-bad" in wb.REGISTRY.ids() and wb.REGISTRY.get("rt-bad").spec.errors, str(wb.REGISTRY.get("rt-bad").spec.errors))
    bad_ops = client.post("/api/workspace/files/list", json={"backendId": "rt-bad", "session": "t-ops"})
    check("带病后端的操作 fail-closed 回 400 而不是 500", bad_ops.status_code == 400 and bad_ops.json()["code"] == wb.CODE_SPEC_INVALID, str(bad_ops.text[:200]))
    wb.reset_registry()

    no_backend = client.post("/api/workspace/files/list", json={"path": ""})
    check("清单为空时说清怎么装而不是回个空表", no_backend.status_code == 400 and "configure" in no_backend.json()["message"], str(no_backend.text[:200]))
    drop_session("t-ops")


# ── 6. 审计面 ──────────────────────────────────────────────────


def audit_checks(client: TestClient, home: Path, env: dict) -> None:
    section("6. 审计经端点读：只落字节数与指纹，不落正文与密钥")
    assemble(home)
    add_session("t-audit", home / "work", {"filePermission": "auto_approve"})
    client.post("/api/workspace/files/write", json={"backendId": "rt-pg", "session": "t-audit", "path": "note.md", "content": "机密正文-不要落账"})
    client.post("/api/workspace/files/write", json={"backendId": "rt-pg", "session": "t-audit", "path": "../nope.md", "content": "x"})
    client.post("/api/workspace/switch", json={"backendId": "rt-pg", "session": "t-audit"})

    page = client.get("/api/workspace/audit?backendId=rt-pg&limit=20").json()
    check("审计端点能按后端过滤", page["ok"] and all(i["backendId"] == "rt-pg" for i in page["items"]), str(page)[:200])
    outcomes = {i["outcome"] for i in page["items"]}
    check("审计同时记成功与被拒（denied 也在账上）", "ok" in outcomes and "denied" in outcomes, str(outcomes))
    text = Path(wb.audit_file()).read_text(encoding="utf-8")
    check("审计文件里没有正文", "机密正文-不要落账" not in text)
    check("审计文件里没有密钥原值", "pg-fake-secret" not in text and "sk-fake-value" not in text)
    denied = next((i for i in page["items"] if i["outcome"] == "denied"), {})
    check("被拒条目带来源与 code", denied.get("deniedBy") and denied.get("code"), str(denied)[:200])
    written_row = next((i for i in page["items"] if i["outcome"] == "ok" and i.get("op") == "write"), {})
    check("写入条目记字节数与内容指纹（而不是正文）", isinstance(written_row.get("contentBytes"), int) and written_row.get("contentFingerprint"), str(written_row)[:200])
    switch_row = next((i for i in page["items"] if i.get("op") == "switch"), {})
    check("切换条目落 agentConfigChanged=false（D-29 第一句）", switch_row.get("agentConfigChanged") is False, str(switch_row)[:200])

    far = client.get("/api/workspace/audit?offset=9999").json()
    check("偏移越界给明确说法而不是空表当没审计", far["ok"] and "offset=0" in str(far.get("note")), str(far)[:200])
    filtered = client.get("/api/workspace/audit?outcome=denied&limit=1").json()
    check("审计分页给 hasMore 与 total", filtered["total"] >= 1 and isinstance(filtered["hasMore"], bool), str(filtered)[:160])
    drop_session("t-audit")


# ── 7. 未验收声明 ──────────────────────────────────────────────


def unverified_checks(client: TestClient, home: Path, env: dict) -> None:
    section("7. 未验收声明：假端点绿不等于真后端能用（约束 A）")
    assemble(home)
    listing = client.get("/api/workspace/backends").json()
    kinds = {b["kind"] for b in listing["backends"]}
    check("四类后端都在清单里（local/docker/postgresql/s3）", kinds == {"local", "docker", "postgresql", "s3"}, str(kinds))
    unverified = " ".join(listing["unverified"])
    check("未验收汇总点名真对象存储（不许声称已支持）", "对象存储" in unverified, unverified[:200])
    check("未验收汇总点名真 PostgreSQL", "PostgreSQL" in unverified, unverified[:200])
    check("未验收汇总点名容器 daemon 未验", "docker" in unverified.lower() or "容器" in unverified, unverified[:200])
    probes = listing["probes"]
    check("每个远程后端的探测状态都是未验收", all(probes[k]["status"] == wb.STATUS_UNVERIFIED for k in ("rt-pg", "rt-s3", "rt-docker")), str({k: v["status"] for k, v in probes.items()})[:200])
    check("本地后端探测给出目录事实", probes["rt-local"]["status"] in (wb.STATUS_VERIFIED, wb.STATUS_UNVERIFIED) and "rootExists" in probes["rt-local"], str(probes["rt-local"])[:200])
    check("清单回话里没有任何凭据原值", "sk-fake-value" not in str(listing) and "pg-fake-secret" not in str(listing))
    check("清单回话里的凭据只有字段名与指纹", all(
        "value" not in ref and "secret" not in str(ref).lower().replace("secretkey", "credkey")
        for b in listing["backends"] for ref in (b.get("credentials") or [])
    ), str([b.get("credentials") for b in listing["backends"]])[:200])


class FakeToolkit:
    """只模仿 agentscope Toolkit 用到的两点：tools 字典 + 按函数名注册。"""

    def __init__(self):
        self.tools = {}

    def register_tool_function(self, fn):
        self.tools[fn.__name__] = fn


def ctx_with(toolkit, **kw):
    import toolkit_lanes as tl

    base = dict(
        toolkit=toolkit, work_dir=Path(tempfile.mkdtemp()), base=Path(tempfile.mkdtemp()),
        sandbox=None, emit=None, task_id="t-toolkit", slides=False, office=False, grants=None,
        pptx_templates=None, tools={}, extra=None,
    )
    base.update(kw)
    return tl.ToolkitContext(**base)


def tool_text(response) -> str:
    block = response.content[0]
    return block["text"] if isinstance(block, dict) else getattr(block, "text", str(block))


def toolkit_checks(home: Path, env: dict) -> None:
    section("8. 工具车道：开关点亮什么、模型看到的那几句话从哪来")
    import toolkit_lanes as tl
    import toolkit_workspace as tw

    quiet = ctx_with(FakeToolkit())
    rows = tl.register_all(quiet)
    ws_row = next(r for r in rows if r["lane"] == "workspace")
    check("开关没接线时本车道一件都不注册（与今日行为一致）", not ws_row["registered"], str(ws_row))
    check(
        "车道声明的 7 件里没有常驻六件的名字（不抢 Rust 已实现的口子）",
        not (set(tw.WORKSPACE_TOOLS) & set(tl.RESIDENT_TOOLS)),
        str(sorted(set(tw.WORKSPACE_TOOLS) & set(tl.RESIDENT_TOOLS))),
    )

    local = wb.REGISTRY.get("rt-local") or wb.build_backend(
        wb.parse_spec({"backendId": "rt-local", "kind": "local", "options": {"dir": str(home / "work")}})
    )
    full = fixtures.policy_for(home / "work", permission="full_access")
    ask = fixtures.policy_for(home / "work", permission="ask_every_time")

    for label, handle in (
        ("实例", local),
        ("runtime 字典", {"workspaceBackend": {"backendId": "rt-local", "kind": "local", "options": {"dir": str(home / "work")}}}),
        ("注册表式句柄", wb.REGISTRY),
    ):
        toolkit = FakeToolkit()
        ctx = ctx_with(toolkit, extra={"workspace_backend": handle}, sandbox=full)
        tl.register_all(ctx)
        check(f"句柄是{label}时七件都挂上", sorted(toolkit.tools) == sorted(tw.WORKSPACE_TOOLS), str(sorted(toolkit.tools)))

    bogus = FakeToolkit()
    ctx = ctx_with(bogus, extra={"workspace_backend": "rt-local"}, sandbox=full)
    tl.register_all(ctx)
    check("认不到的句柄形态照样注册（工具面在，取不到后端时如实报错而不是不注册后静默）", sorted(bogus.tools) == sorted(tw.WORKSPACE_TOOLS))
    text = tool_text(bogus.tools["workspace_backend_list"]())
    check("取不到后端时回 WS_BACKEND_REQUIRED 且明说不会退回本地目录假装", "WS_BACKEND_REQUIRED" in text and "不会退回" in text, text[:200])

    toolkit = FakeToolkit()
    ctx = ctx_with(toolkit, extra={"workspace_backend": local}, sandbox=full)
    tl.register_all(ctx)
    status = tool_text(ctx.toolkit.tools["workspace_backend_status"]())
    check("状态件回清单与未验收条目（模型看得见「哪些还没验」）", "unverified" in status and "activeBackendId" in status, status[:200])
    check("状态件声明切后端不改 Agent 配置", "note" in status and "切换后端不会改" in status, status[-300:])

    wrote = tool_text(ctx.toolkit.tools["workspace_backend_write"]("toolkit/a.md", "工具面写的正文"))
    check("写件真的落到后端目录", (home / "work" / "toolkit" / "a.md").exists(), wrote[:200])
    read_back = tool_text(ctx.toolkit.tools["workspace_backend_read"]("toolkit/a.md"))
    check("读件回正文", "工具面写的正文" in read_back, read_back[:200])
    listed = tool_text(ctx.toolkit.tools["workspace_backend_list"]("toolkit"))
    check("列件回条目", "a.md" in listed, listed[:200])
    exec_text = tool_text(ctx.toolkit.tools["workspace_backend_exec"]("ls"))
    check("本地后端的执行件回「不承载命令执行」而不是空跑", "不承载命令执行" in exec_text, exec_text[:200])
    deleted = tool_text(ctx.toolkit.tools["workspace_backend_delete"]("toolkit/a.md"))
    check("完全访问下删除件可用", '"ok": true' in deleted and not (home / "work" / "toolkit" / "a.md").exists(), deleted[:200])

    ask_toolkit = FakeToolkit()
    ask_ctx = ctx_with(ask_toolkit, extra={"workspace_backend": wb.REGISTRY.get("rt-pg")}, sandbox=ask)
    tl.register_all(ask_ctx)
    tier_text = tool_text(ask_ctx.toolkit.tools["workspace_backend_write"]("b.md", "x"))
    check("工具面的远程写被档位拒时保留来源（deniedBy=file_permission）", "file_permission" in tier_text and "WS_TIER_DENIES_REMOTE" in tier_text, tier_text[:250])
    blocked = fixtures.policy_for(home / "work", permission="full_access", fileBlocklist=["secret/**"])
    block_ctx = ctx_with(FakeToolkit(), extra={"workspace_backend": wb.REGISTRY.get("rt-pg")}, sandbox=blocked)
    tl.register_all(block_ctx)
    block_text = tool_text(block_ctx.toolkit.tools["workspace_backend_read"]("secret/k.md"))
    check("沙箱拒绝是另一句（与档位句不同、来源是 sandbox）", "黑名单" in block_text and "sandbox" in block_text and block_text != tier_text, block_text[:250])

    many = home / "work" / "many"
    many.mkdir(parents=True, exist_ok=True)
    for index in range(120):
        (many / f"entry-{index:04d}-with-a-fairly-descriptive-file-name.md").write_text("x", encoding="utf-8")
    big_list = tool_text(ctx.toolkit.tools["workspace_backend_list"]("many"))
    check("超长回话按预算截断并说明怎么拿全（不把整个目录灌进上下文）", len(big_list) <= tw._MAX_TEXT_CHARS + 200 and "已截断" in big_list, str(len(big_list)))

    switch_text = tool_text(ctx.toolkit.tools["workspace_backend_switch"]("rt-nope"))
    check("切到不存在的后端回 WS_BACKEND_NOT_FOUND", "WS_BACKEND_NOT_FOUND" in switch_text, switch_text[:200])


def main() -> None:
    # 本文件会临时改引擎的准入环境变量（测「远程身份」这一档必须让引擎真配上一份令牌），
    # 所以进出都要按原值恢复：留着一个改过的环境出去，下一份自测看到的是别人的现场。
    saved = {name: os.environ.get(name) for name in ("TT_ENGINE_TOKEN", "TT_ENGINE_LOCAL_TOKEN", "TONGTIANWORK_HOME")}
    client = TestClient(server.app)
    home = make_home("home-routes")
    try:
        mount_checks()
        admission_checks(client)
        registry_checks(client, home)
        env = assemble(home)
        denial_checks(client, home, env)
        op_checks(client, home, env)
        audit_checks(client, home, env)
        unverified_checks(client, home, env)
        toolkit_checks(home, env)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        wb.reset_registry()
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine workspace routes selftest: ALL PASS")


if __name__ == "__main__":
    main()
