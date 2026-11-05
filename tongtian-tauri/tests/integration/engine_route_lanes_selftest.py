#!/usr/bin/env python3
"""#164 接缝自测：路由分片与工具车道的挂载、越界与遮蔽判定（W4 领地）。

验证项：
1. server.py 末尾的分片注册面按声明顺序挂上七条车道，且引擎主模块绑定正确
2. 车道前缀越界 / 车道名未登记 / 车道内重名 / 被既有通配吞掉 —— 四种都在挂载时就抛
3. 车道端点真能被请求到，并且只经 routes_lanes.engine() 拿到引擎现场（不 import server）
4. 工具车道的开关判定：开关全假必须空手、不许抢常驻六件、两条车道不许注册同名工具
5. build_toolkit 在改造后仍与今日一致：默认六件常驻，slides/office 各加各的
6. 副作用名单四张视图同源（engine/side_effect_registry.py），三个 Python 消费方按视图派生，
   判定行为与搬表前逐条一致

判定只看 [PASS] 计数；缺 fastapi 时输出 SKIP 并以 0 退出（与其余引擎自测同一口径）。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_route_lanes_selftest.py
"""

import sys
import tempfile
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def expect_raises(name: str, fn, needle: str) -> None:
    """要的就是它抛，而且抛的原因得写在脸上（静默吞掉才是这套接缝的失败模式）。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001  判定「抛没抛 + 文案对不对」是本用例的全部内容
        check(name, needle in str(exc), f"抛了但文案不含 {needle!r}：{exc}")
    else:
        check(name, False, "没抛异常")


class FakeToolkit:
    """只模仿 agentscope Toolkit 用到的两点：tools 字典 + 按函数名注册。"""

    def __init__(self):
        self.tools = {}

    def register_tool_function(self, fn):
        self.tools[fn.__name__] = fn


def make_lane_module(name: str, lane: str, paths):
    """造一条临时车道：路径列表 → 挂在 APIRouter 上的空处理函数。"""
    from fastapi import APIRouter

    module = types.ModuleType(name)
    module.LANE = lane
    module.router = APIRouter()
    for path in paths:
        def handler():
            return {"ok": True}

        module.router.add_api_route(path, handler, methods=["GET"])
    # 临时车道也要能拿到引擎现场：走全局那份绑定，而不是 import server
    import routes_lanes as rl

    module.engine_accessor = rl
    sys.modules[name] = module
    return module


def named_tool(name: str):
    """带名字的假工具：注册表按 __name__ 记账，lambda 会让断言看着像空列表。"""

    def fn():
        return None

    fn.__name__ = name
    return fn


ARTIFACT_TOOLS = {
    "generate_pptx": named_tool("generate_pptx"),
    "generate_xlsx": named_tool("generate_xlsx"),
    "generate_docx": named_tool("generate_docx"),
}


def main() -> None:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routes_lanes as rl
        import toolkit_lanes as tl
        import server
    except Exception as exc:  # 无 fastapi/agentscope 的环境里优雅跳过
        print(f"SKIP: engine route lanes selftest unavailable ({exc})")
        sys.exit(0)

    # ── 1 分片注册面 ────────────────────────────────────────
    lanes = [row["lane"] for row in server.ROUTE_LANE_REPORT]
    check("七条车道按声明顺序挂上", lanes == [
        "knowledge", "artifact", "workspace", "exec", "connect", "orchestrate", "voice",
    ], str(lanes))
    check("每条车道都登记了归属窗口", all(row["owner"] for row in server.ROUTE_LANE_REPORT))
    check("引擎主模块绑定正确（不是 __main__ 之外的第二份）", rl.engine() is server)
    # `@app.*` 里混着两类东西：路由与**非路由**（中间件、异常处理器）。#141 加的
    # `@app.exception_handler(Exception)` 让总数从 61 变 62——如果继续只盯总数，
    # 这一条会被读成「分片把一条路由弄丢了」，而真正该钉的是**路由数 60 不变**，
    # 非路由那几个单独点名（多一个少一个都要说出来是什么）。
    app_lines = [line for line in (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8").splitlines()
                 if line.startswith("@app.")]
    route_lines = [line for line in app_lines
                   if any(line.startswith(f"@app.{verb}(") for verb in ("get", "post", "put", "delete", "patch"))]
    other_lines = sorted(line for line in app_lines if line not in route_lines)
    check(
        "既有路由总数没被分片改动（60 条路由装饰器仍在 server.py）",
        server.__doc__ is not None and len(route_lines) == 60,
        str(len(route_lines)),
    )
    check(
        "非路由的 @app.* 只有中间件与未捕获异常处理器两件（各自点名，不混进路由数）",
        other_lines == ['@app.exception_handler(Exception)', '@app.middleware("http")'],
        str(other_lines),
    )

    # ── 2 挂载判定（临时 app，绝不污染 server.app）─────────────
    fresh = FastAPI()

    @fresh.get("/api/exec/{name}")
    def _wildcard(name: str):
        return {"name": name}

    expect_raises(
        "被既有通配吞掉的字面路径：拒绝挂载",
        lambda: rl.attach(
            fresh, server,
            [make_lane_module("tt_probe_shadow", "exec", ["/api/exec/list"]).__name__],
        ),
        "吞掉",
    )
    expect_raises(
        "占用别家前缀：拒绝挂载",
        lambda: rl.attach(
            fresh, server,
            [make_lane_module("tt_probe_prefix", "exec", ["/api/whatever"]).__name__],
        ),
        "越界",
    )
    expect_raises(
        "只差一段的邻家前缀也算越界（/api/artifact 不许吞 /api/artifacts）",
        lambda: rl.attach(
            fresh, server,
            [make_lane_module("tt_probe_plural", "artifact", ["/api/artifacts/history"]).__name__],
        ),
        "越界",
    )
    check(
        "前缀按段对齐：本家放行、只差一段的邻家不放行",
        rl._prefix_ok("/api/artifact/detail", "/api/artifact")
        and rl._prefix_ok("/api/artifact", "/api/artifact")
        and not rl._prefix_ok("/api/artifacts/history", "/api/artifact"),
    )
    expect_raises(
        "车道名未登记：拒绝挂载",
        lambda: rl.attach(
            fresh, server,
            [make_lane_module("tt_probe_unknown", "nonexistent", ["/api/exec/a"]).__name__],
        ),
        "不在登记的车道里",
    )
    expect_raises(
        "同一条车道里重复挂载：拒绝挂载",
        lambda: rl.attach(
            fresh, server,
            # 两段路径：躲开上面那条 /api/exec/{name} 单段通配，测的才是「车道内重名」而不是遮蔽
            [make_lane_module("tt_probe_dup", "exec", ["/api/exec/a/b", "/api/exec/a/b"]).__name__],
        ),
        "重复挂载",
    )

    # ── 3 端点可达 + 只经 engine() 取现场 ────────────────────
    # 用 /api/staging/*：既在 exec 车道的登记前缀里，又躲开临时 app 上那条 /api/exec/{name} 通配
    probe = make_lane_module("tt_probe_ok", "exec", ["/api/staging/ping"])

    @probe.router.get("/api/staging/version")
    def _version():
        return {"version": probe.engine_accessor.engine().ENGINE_VERSION}

    report = rl.attach(fresh, server, ["tt_probe_ok"])
    check("临时车道挂载报告正确", report and report[0]["endpoints"] == 2, str(report))
    client = TestClient(fresh)
    check("车道端点能被请求到", client.get("/api/staging/ping").status_code == 200)
    body = client.get("/api/staging/version").json()
    check("车道经 engine() 拿到的就是同一份引擎模块", body.get("version") == server.ENGINE_VERSION, str(body))

    # ── 4 工具车道开关判定 ──────────────────────────────────
    def ctx_with(**kw):
        base = dict(
            toolkit=FakeToolkit(), work_dir=Path(tempfile.mkdtemp()), base=Path(tempfile.mkdtemp()),
            sandbox=None, emit=None, task_id="", slides=False, office=False, grants=None,
            pptx_templates=None, tools=dict(ARTIFACT_TOOLS), extra=None,
        )
        base.update(kw)
        return tl.ToolkitContext(**base)

    quiet = ctx_with()
    rows = tl.register_all(quiet)
    check("开关全假时七条车道一件都不注册", all(not r["registered"] for r in rows), str(rows))

    slides_ctx = ctx_with(slides=True)
    rows = tl.register_all(slides_ctx)
    check("slides 开关只点亮 artifact 车道的 generate_pptx",
          sorted(slides_ctx.toolkit.tools) == ["generate_pptx"], str(sorted(slides_ctx.toolkit.tools)))

    office_ctx = ctx_with(office=True)
    rows = tl.register_all(office_ctx)
    check("office 开关点亮 generate_xlsx 与 generate_docx",
          sorted(office_ctx.toolkit.tools) == ["generate_docx", "generate_xlsx"],
          str(sorted(office_ctx.toolkit.tools)))

    class FakeGrants:
        def install(self, toolkit, base, emit=None, task_id=""):
            def run_command():
                return None

            toolkit.register_tool_function(run_command)

    grants_ctx = ctx_with(grants=FakeGrants())
    tl.register_all(grants_ctx)
    check("grants 开关点亮 exec 车道", "run_command" in grants_ctx.toolkit.tools,
          str(sorted(grants_ctx.toolkit.tools)))

    def greedy_lane(mod_name: str, lane: str, tool_name: str):
        module = types.ModuleType(mod_name)
        module.LANE = lane

        def register(ctx):
            def stolen():
                return None

            stolen.__name__ = tool_name
            ctx.toolkit.register_tool_function(stolen)

        module.register = register
        sys.modules[mod_name] = module
        return mod_name

    expect_raises(
        "开关全假却注册：当场拒掉（常驻清单不因为写错车道而变胖）",
        lambda: tl.register_all(ctx_with(), [greedy_lane("tt_greedy_off", "voice", "some_new_tool")]),
        "全为假时",
    )
    expect_raises(
        "车道抢注常驻六件：当场拒掉",
        lambda: tl.register_all(
            ctx_with(extra={"voice": "on"}),
            [greedy_lane("tt_greedy_resident", "voice", "write_text_file")],
        ),
        "常驻工具",
    )
    # 开关名不在表里的那条车道：先补一个真开关，才能测「两条车道注册同名工具」
    expect_raises(
        "未登记的车道名：拒跑",
        lambda: tl.register_all(ctx_with(), [greedy_lane("tt_greedy_unknown", "ghost", "x_tool")]),
        "不在开关表里",
    )

    # ── 5 真件行为与改造前一致 ──────────────────────────────
    tmp = Path(tempfile.mkdtemp())
    resident = sorted(server.build_toolkit(tmp).tools)
    check("build_toolkit 默认仍是常驻六件", resident == sorted(tl.RESIDENT_TOOLS), str(resident))
    check("默认装配里没有模式作用域工具", not [n for n in resident if n.startswith("generate_")])
    slides_names = sorted(server.build_toolkit(tmp, slides=True).tools)
    check("slides=True 多出 generate_pptx", slides_names == sorted(resident + ["generate_pptx"]), str(slides_names))
    office_names = sorted(server.build_toolkit(tmp, office=True).tools)
    check("office=True 多出 generate_docx / generate_xlsx",
          office_names == sorted(resident + ["generate_docx", "generate_xlsx"]), str(office_names))

    # ── 6 副作用名单同源（#164 交付四）────────────────────────
    import checkpoint_runtime as cr
    import sandbox_runtime as sr
    import side_effect_registry as ser
    import tool_approval as tap

    # 成员逐字回钉改造前的四张表：这张表一漂移，审批弹窗、L0 拒绝、台账条目三处运行时行为跟着变
    check("重放视图成员与改造前一致", sorted(ser.view("replay")) == [
        "delete_text_file", "generate_docx", "generate_pptx", "generate_xlsx",
        "insert_text_file", "spawn_subagents", "write_text_file",
    ], str(sorted(ser.view("replay"))))
    check("检查点视图就是重放视图", ser.view("checkpoint") == ser.view("replay"))
    check("沙箱写盘面视图成员与改造前一致", sorted(ser.view("sandbox_mutating")) == [
        "computer_action", "delete_text_file", "generate_docx", "generate_pptx",
        "generate_xlsx", "insert_text_file", "write_text_file",
    ], str(sorted(ser.view("sandbox_mutating"))))
    check("审批敏感视图比前两张宽（命令/shell/联网都在）", sorted(ser.view("approval_sensitive")) == [
        "delete_text_file", "fetch_url", "generate_docx", "generate_pptx", "generate_xlsx",
        "insert_text_file", "run_command", "run_shell", "spawn_subagents", "write_text_file",
    ], str(sorted(ser.view("approval_sensitive"))))
    check("关键词兜底表 17 条且含 shell/command/run", len(ser.keywords()) == 17
          and {"shell", "command", "run"} <= set(ser.keywords()), str(ser.keywords()))

    # 三个消费方必须是从这张表派生的，而不是又抄了一份
    check("checkpoint_runtime 的副作用表就是派生自本表", cr._SIDE_EFFECT_TOOLS == set(ser.view("checkpoint")))
    check("checkpoint_runtime 的关键词就是本表的关键词对象", cr._SIDE_EFFECT_KEYWORDS == ser.keywords())
    check("sandbox_runtime.MUTATING_TOOLS 派生自本表", sr.MUTATING_TOOLS == ser.view("sandbox_mutating"))
    check("tool_approval.SENSITIVE_TOOLS 派生自本表", tap.SENSITIVE_TOOLS == ser.view("approval_sensitive"))
    check("沙箱执行类/删除类分层表仍是自己的（不属于副作用成员表）",
          sr.EXECUTION_TOOLS == frozenset({"run_command", "run_shell"})
          and sr.DELETING_TOOLS == frozenset({"delete_text_file"}))

    # 判定行为不许因为搬表而变：只读排除、表内命中、关键词兜底各测一条
    check("只读工具仍不算副作用", not cr.is_side_effect_tool("view_text_file"))
    check("表内工具算副作用", cr.is_side_effect_tool("generate_docx"))
    check("未登记但命中关键词的 MCP 工具算副作用", cr.is_side_effect_tool("mcp_send_email"))
    check("空名不算副作用", not cr.is_side_effect_tool("   "))
    expect_raises(
        "视图名写错就抛（不做「取不到当空集」的乐观降级）",
        lambda: ser.view("checkpont"),
        "未登记的副作用视图",
    )

    print("-" * 50)
    if FAILURES:
        print(f"engine route lanes selftest: {len(FAILURES)} FAILED — {'，'.join(FAILURES)}")
        sys.exit(1)
    print("engine route lanes selftest: ALL PASS")


if __name__ == "__main__":
    main()
