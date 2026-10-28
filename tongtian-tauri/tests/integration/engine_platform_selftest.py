#!/usr/bin/env python3
"""Agent 平台层自测（消息总线 / 技能包 / MCP Catalog / 通道网关 / 调度 / CLI）

锁住新增的四层能力仍是「薄封装」，并且与存量引擎一起可用：
1. 统一消息结构体与总线：校验、路由隔离、按会话回收回复；
2. 技能注册表：内置 + 用户目录、非法技能逐条报错、关键词/优先级匹配、
   动态热加载、内置工具与 MCP 工具白名单；
3. MCP Catalog：stdio/HTTP 条目校验、按技能绑定筛选、客户端池发现/代理调用/收尾；
4. 通道网关与调度：注入 runner 验证匹配与路由；真实 `server.build_agent`
   验证技能提示词、工具白名单、MCP 工具接入与调用回填（离线假模型）。

离线运行：MCP 用仓库内夹具；引擎用假模型，不联网、无需 API Key。
agentscope/mcp/PyYAML 缺失时对应分组输出 SKIP，不影响其余分组。

用法: python tests/integration/engine_platform_selftest.py
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
FIXTURE = str(PROJECT_ROOT / "tests" / "integration" / "fixtures" / "mcp_echo_server.py")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from agent_platform import (  # noqa: E402
    AgentDispatcher,
    AgentMessage,
    AgentReply,
    ChannelGateway,
    CliAdapter,
    ImAdapter,
    McpCatalog,
    McpCatalogError,
    McpClientPool,
    MessageBus,
    PlatformConfig,
    ScheduleAdapter,
    SkillRegistry,
    load_config,
    parse_server,
    parse_skill,
)

try:
    import yaml  # noqa: F401

    YAML_OK = True
except Exception:
    YAML_OK = False

try:
    import agentscope  # noqa: F401
    import mcp  # noqa: F401

    MCP_OK = True
except Exception as exc:  # noqa: BLE001
    MCP_OK = False
    MCP_SKIP = str(exc)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _home(prefix: str = "tt-platform-") -> Path:
    home = Path(tempfile.mkdtemp(prefix=prefix))
    (home / "skills").mkdir(parents=True, exist_ok=True)
    return home


def _write(path: Path, data) -> None:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _raises_async(coro) -> bool:
    try:
        await coro
    except Exception:  # noqa: BLE001
        return True
    return False


def _run(coro) -> None:
    try:
        asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        FAILURES.append(f"协程执行异常：{exc}")


# ── 1. 消息总线 ──────────────────────────────────────────────────

async def bus_checks() -> None:
    print("--- 1. 消息归一化与总线隔离 ---")
    check("空正文被拒绝", AgentMessage(text="   ").validate() == "消息正文为空")
    check("未知通道被拒绝",
          "未知通道" in (AgentMessage(text="x", channel="web").validate() or ""))
    msg = AgentMessage(text=" 你好 ", channel="cli", channel_id="terminal",
                       conversation_id="c1")
    check("route_key 由通道/渠道/会话组成", msg.route_key == "cli:terminal:c1",
          msg.route_key)

    back = AgentMessage.from_dict(msg.to_dict())
    check("to_dict/from_dict 往返保持正文与标识",
          back.text == "你好" and back.route_key == msg.route_key
          and back.message_id == msg.message_id)
    snake = AgentMessage.from_dict({
        "text": "x", "channel_id": "k", "conversation_id": "c", "work_dir": "/tmp",
    })
    check("兼容 snake_case 入参",
          snake.route_key == "cli:k:c" and snake.work_dir == "/tmp")

    bus = MessageBus()
    m1 = AgentMessage(text="a", channel_id="k", conversation_id="c1")
    m2 = AgentMessage(text="b", channel_id="k", conversation_id="c2")
    await bus.publish(m1)
    await bus.publish(m2)
    got = [await bus.receive(), await bus.receive()]
    check("入站保持发布顺序", [g.text for g in got] == ["a", "b"])

    bus.deliver(AgentReply(message_id=m1.message_id, route_key=m1.route_key,
                           channel="cli", text="回a"))
    bus.deliver(AgentReply(message_id=m2.message_id, route_key=m2.route_key,
                           channel="cli", text="回b"))
    ra = await bus.wait_reply(m1.route_key, timeout=1)
    rb = await bus.wait_reply(m2.route_key, timeout=1)
    check("出站按会话隔离，不串线", ra.text == "回a" and rb.text == "回b")
    check("取走回复后队列清零",
          bus.pending_outbound(m1.route_key) == 0
          and bus.pending_outbound(m2.route_key) == 0)
    check("routes 只列已产生回复的路由",
          set(bus.routes()) == {m1.route_key, m2.route_key}, str(bus.routes()))
    check("非法消息 publish 直接报错",
          await _raises_async(bus.publish(AgentMessage(text=" "))))
    bus.close()
    check("总线关闭后拒绝新消息",
          await _raises_async(bus.publish(AgentMessage(text="x"))))


# ── 2. 技能注册表 ────────────────────────────────────────────────

def skill_checks(home: Path) -> None:
    print("--- 2. 技能包注册表（加载/匹配/热更新/白名单）---")
    _write(home / "skills" / "custom.json", {
        "id": "custom-skill",
        "name": "自定义技能",
        "description": "自测用技能",
        "prompt": "你是自定义技能。",
        "match": {"keywords": ["自定义"], "priority": 7},
        "tools": {"allow": ["view_*"]},
        "mcp": {"servers": ["a"], "allow": ["a/*"]},
    })
    cfg = PlatformConfig(enabled=True, home=home, default_skill="custom-skill")
    reg = SkillRegistry.load(cfg)
    check("用户目录技能可加载", reg.get("custom-skill") is not None, str(reg.errors))
    if YAML_OK:
        check("内置 YAML 技能同时可用",
              {"general", "weekly-report", "mcp-tools"} <= {s.id for s in reg.all()},
              str([s.id for s in reg.all()]))
        weekly = reg.get("weekly-report")
        check("内置技能关键词匹配",
              weekly is not None
              and reg.resolve(None, "帮我写周报")[0].id == "weekly-report")
    else:
        print("SKIP: PyYAML 缺失，内置 YAML 技能分组未运行")

    _write(home / "skills" / "bad.json", {"id": "bad-skill", "prompt": ""})
    _write(home / "skills" / "bad2.json",
           {"id": "bad2", "prompt": "hi", "context": {"mode": "nope"}})
    broken = SkillRegistry.load(cfg)
    check("非法技能逐条报错而不是整目录失败",
          any("prompt 不能为空" in e for e in broken.errors)
          and any("context.mode" in e for e in broken.errors), str(broken.errors))
    check("同一目录里的合法技能仍可用", broken.get("custom-skill") is not None)

    custom = reg.get("custom-skill")
    check("关键词命中给基础分", custom.match_score("自定义任务") > 0)
    check("未命中关键词得 0", custom.match_score("随便聊聊") == 0)
    check("优先级参与打分", custom.match_score("自定义") >= 7)
    chosen, why = reg.resolve(None, "自定义任务")
    check("按关键词选技能", chosen is not None and chosen.id == "custom-skill", why)
    chosen, why = reg.resolve("custom-skill", "随便聊聊")
    check("显式 id 优先于关键词",
          chosen is not None and chosen.id == "custom-skill" and "显式指定" in why, why)
    chosen, why = reg.resolve("自定义技能", "随便聊聊")
    check("按技能名称解析", chosen is not None and chosen.id == "custom-skill", why)
    chosen, why = reg.resolve("no-such-skill", "")
    check("未知技能返回可用清单",
          chosen is None and "不存在" in why and "custom-skill" in why, why)
    chosen, why = reg.resolve(None, "随便聊聊")
    check("无关键词回落默认技能",
          chosen is not None and chosen.id == "custom-skill" and "默认技能" in why, why)

    check("allow 模式命中", custom.tool_allowed("view_text_file"))
    check("allow 模式未命中", not custom.tool_allowed("write_text_file"))
    deny = parse_skill({
        "id": "deny-skill", "prompt": "p",
        "tools": {"allow": ["*"], "deny": ["danger_*"]},
    }, "inline")
    check("deny 优先于 allow",
          deny.tool_allowed("danger_shell") is False
          and deny.tool_allowed("view_text_file") is True)
    check("MCP 白名单按 server/tool 匹配",
          custom.mcp_tool_allowed("a", "read") is True
          and custom.mcp_tool_allowed("b", "read") is False)
    named = parse_skill({"id": "n", "prompt": "p",
                         "mcp": {"allow": ["search"]}}, "inline")
    check("MCP 白名单只写服务器名=该服务器全放行",
          named.mcp_tool_allowed("search", "anything") is True
          and named.mcp_tool_allowed("other", "anything") is False)
    wildcard = parse_skill({"id": "w", "prompt": "p",
                            "mcp": {"allow": ["*/*"]}}, "inline")
    check("MCP 通配放行", wildcard.mcp_tool_allowed("x", "y") is True)
    unbound = parse_skill({"id": "u", "prompt": "p"}, "inline")
    check("未写 MCP 白名单时绑定服务器全放行",
          unbound.mcp_tool_allowed("x", "y") is True)

    runtime = custom.to_runtime_skill()
    check("转成存量引擎认得的技能形状",
          runtime["id"] == "custom-skill" and runtime["kind"] == "skill"
          and runtime["content"] == "你是自定义技能。", str(runtime))
    check("提示词追加段声明安全边界优先",
          "优先" in custom.prompt_block() and "custom-skill" in custom.prompt_block())

    _write(home / "skills" / "hot.json", {
        "id": "hot-skill", "name": "热加载技能", "prompt": "你是热加载技能。",
        "match": {"keywords": ["热更新"], "priority": 50},
    })
    check("技能文件新增触发重载", reg.reload_if_changed() is True)
    check("新技能立即可用", reg.get("hot-skill") is not None)
    check("无变化时不重复加载", reg.reload_if_changed() is False)
    hot = reg.resolve(None, "帮我热更新")
    check("热加载后的关键词立刻参与匹配",
          hot[0] is not None and hot[0].id == "hot-skill", hot[1])

    _write(home / "skills" / "override.json", {
        "id": "custom-skill", "name": "覆盖技能", "prompt": "覆盖后的提示词",
    })
    overridden = SkillRegistry.load(cfg)
    check("用户目录同名技能覆盖内置/同名项",
          overridden.get("custom-skill").name == "覆盖技能")


# ── 3. MCP Catalog 与客户端池 ───────────────────────────────────

def _parse_error(raw) -> bool:
    try:
        parse_server(raw, "inline")
    except McpCatalogError:
        return True
    except Exception:  # noqa: BLE001
        return True
    return False


def catalog_checks(home: Path) -> None:
    print("--- 3. MCP Catalog（注册表 / 技能绑定）---")
    stdio = parse_server({
        "id": "echo", "name": "回声", "transport": "stdio", "command": "python",
        "args": ["x.py"], "env": {"A": "1"}, "timeoutSecs": 9999,
    }, "inline")
    check("stdio 条目解析并钳制超时",
          stdio.transport == "stdio" and stdio.timeout_secs == 600.0
          and stdio.args == ("x.py",) and stdio.env == {"A": "1"})
    http = parse_server({"id": "h", "transport": "streamable_http",
                         "url": "http://127.0.0.1:1/mcp"}, "inline")
    check("HTTP 条目解析",
          http.transport == "streamable_http" and http.url.endswith("/mcp"))
    check("非法传输被拒绝", _parse_error({"id": "x", "transport": "grpc"}))
    check("stdio 缺 command 被拒绝", _parse_error({"id": "x", "transport": "stdio"}))
    check("http 缺 url 被拒绝", _parse_error({"id": "x", "transport": "http"}))
    check("args 非数组被拒绝",
          _parse_error({"id": "x", "command": "p", "args": "a b"}))
    spec = stdio.to_runtime_spec()
    check("运行时形状与 mcp_runtime 一致",
          spec["serverId"] == "echo" and spec["timeoutSecs"] == 600.0
          and spec["transport"] == "stdio")

    _write(home / "mcp_catalog.json", {"servers": [
        {"id": "ok", "name": "可用", "transport": "stdio", "command": "python",
         "args": ["x.py"]},
        {"id": "off", "name": "停用", "transport": "stdio", "command": "python",
         "enabled": False},
        {"id": "bad", "name": "坏条目", "transport": "stdio"},
        "not-an-object",
        {"id": "remote", "transport": "http", "url": "http://127.0.0.1:1/mcp"},
    ]})
    cfg = PlatformConfig(enabled=True, home=home)
    catalog = McpCatalog.load(cfg)
    ids = {s.id for s in catalog.all()}
    check("有效条目全部登记", {"ok", "off", "remote"} <= ids, str(ids))
    check("坏条目逐条报错且不影响其他",
          any("command" in e for e in catalog.errors)
          and any("必须是对象" in e for e in catalog.errors), str(catalog.errors))

    bind_all = parse_skill({"id": "a", "prompt": "p",
                            "mcp": {"servers": ["*"]}}, "inline")
    servers, notes = catalog.servers_for(bind_all)
    check("`*` 绑定全部启用项并跳过停用",
          {s.id for s in servers} == {"ok", "remote"}
          and any("已停用" in n for n in notes), str(notes))
    bind_one = parse_skill({"id": "b", "prompt": "p",
                            "mcp": {"servers": ["ok", "missing"]}}, "inline")
    servers, notes = catalog.servers_for(bind_one)
    check("显式绑定跳过缺失项并说明",
          [s.id for s in servers] == ["ok"]
          and any("不在 Catalog" in n for n in notes), str(notes))
    plain = parse_skill({"id": "c", "prompt": "p"}, "inline")
    check("未绑定服务器时不接 MCP", catalog.servers_for(plain) == ([], []))
    check("to_runtime_mcp 输出 serverId",
          catalog.to_runtime_mcp([catalog.get("ok")])[0]["serverId"] == "ok")


async def pool_checks() -> None:
    print("--- 3b. MCP 客户端池（stdio 发现/调用/收尾）---")
    if not MCP_OK:
        print(f"SKIP: agentscope/mcp 缺失（{MCP_SKIP}）")
        return
    home = _home("tt-platform-mcp-")
    _write(home / "mcp_catalog.json", {"servers": [
        {"id": "echo", "name": "自测回显", "transport": "stdio",
         "command": sys.executable, "args": [FIXTURE], "timeoutSecs": 30},
        {"id": "broken", "name": "连不上", "transport": "stdio",
         "command": "no-such-binary-zzz", "args": []},
    ]})
    catalog = McpCatalog.load(PlatformConfig(home=home))
    pool = McpClientPool(catalog)
    async with pool:
        tools = await pool.discover("echo")
        check("stdio 连接器工具发现",
              {"echo", "now_word", "env_probe"} <= {t["name"] for t in tools},
              str([t["name"] for t in tools]))
        text = await pool.call("echo", "echo", {"text": "华东"})
        check("工具代理调用往返", text == "echo:华东", text)
        try:
            await pool.acquire("nope")
            unknown_server = ""
        except McpCatalogError as exc:
            unknown_server = str(exc)
        check("未知服务器给出中文错误",
              "MCP_UNKNOWN_SERVER" in unknown_server and "nope" in unknown_server,
              unknown_server)
        try:
            await pool.call("echo", "nope", {})
            unknown_tool = ""
        except McpCatalogError as exc:
            unknown_tool = str(exc)
        check("未知工具列出可用项",
              "MCP_UNKNOWN_TOOL" in unknown_tool and "echo" in unknown_tool,
              unknown_tool)
        try:
            await pool.acquire("broken")
            connect_error = ""
        except McpCatalogError as exc:
            connect_error = str(exc)
        check("连不上只影响该连接器并说明原因",
              "MCP_CONNECT_FAILED" in connect_error and "连不上" in connect_error,
              connect_error)
    check("退出上下文后连接池标记关闭", pool._closed is True)
    check("关闭后拒绝再借连接", await _raises_async(pool.acquire("echo")))


# ── 4. 调度层与通道网关（注入 runner）───────────────────────────

async def gateway_checks(home: Path) -> None:
    print("--- 4. 调度层与通道网关 ---")
    cfg = PlatformConfig(enabled=True, home=home, default_skill="general")
    skills = SkillRegistry.load(cfg)
    catalog = McpCatalog.load(cfg)
    calls: list[tuple] = []

    async def runner(message: AgentMessage, skill) -> AgentReply:
        calls.append((message.text, skill.id, message.route_key))
        return AgentReply(
            message_id=message.message_id, route_key=message.route_key,
            channel=message.channel, conversation_id=message.conversation_id,
            skill_id=skill.id, text=f"[{skill.id}] {message.text}",
        )

    dispatcher = AgentDispatcher(cfg, skills, catalog, runner=runner)
    reply = await dispatcher.handle(AgentMessage(text="帮我写周报"))
    check("调度层匹配技能并调用 runner",
          reply.ok and reply.text.startswith("[weekly-report]")
          and calls[-1][1] == "weekly-report", reply.text)
    check("解析依据进入 notes", any("关键词" in n for n in reply.notes), str(reply.notes))
    check("耗时被记录", isinstance(reply.duration_ms, int) and reply.duration_ms >= 0)

    bad = await dispatcher.handle(AgentMessage(text=" "))
    check("非法消息返回 INVALID_MESSAGE",
          bad.status == "error" and bad.error.startswith("INVALID_MESSAGE"), bad.error)
    disabled = AgentDispatcher(PlatformConfig(enabled=False, home=home),
                               skills, catalog, runner=runner)
    off = await disabled.handle(AgentMessage(text="你好"))
    check("默认关闭时拒绝执行并给出开关说明",
          off.error.startswith("PLATFORM_DISABLED") and "TT_PLATFORM" in off.error,
          off.error)

    bus = MessageBus()
    gateway = ChannelGateway(bus, dispatcher, workers=2)
    cli1 = gateway.register(CliAdapter(bus, dispatcher, conversation_id="c1"))
    cli2 = gateway.register(CliAdapter(bus, dispatcher, conversation_id="c2"))
    await gateway.start_workers()
    try:
        r1, r2 = await asyncio.gather(cli1.ask("写周报"), cli2.ask("你好"))
    finally:
        await gateway.stop()
    check("网关消费总线并把回复送回各自会话",
          r1.conversation_id == "c1" and r2.conversation_id == "c2"
          and r1.text.startswith("[weekly-report]") and r2.text.startswith("[general]"),
          f"{r1.text} / {r2.text}")
    check("网关停止后总线关闭", bus._closed is True)

    im = ImAdapter(MessageBus(), dispatcher, "slack")
    im_msg = im.to_message("你好", {"conversationId": "im-1", "sender": "u1"})
    check("IM 适配器转统一消息",
          im_msg.channel == "im" and im_msg.channel_id == "slack"
          and im_msg.route_key == "im:slack:im-1" and im_msg.sender == "u1")
    sch = ScheduleAdapter(MessageBus(), dispatcher, [
        {"id": "daily", "text": "汇总进展", "everySeconds": 5},
        {"id": "empty", "text": ""},
    ])
    sm = sch.to_message({"id": "daily", "text": "汇总进展", "skill": "weekly-report"})
    check("定时适配器转统一消息",
          sm.channel == "schedule" and sm.conversation_id == "schedule-daily"
          and sm.skill == "weekly-report")
    check("空正文定时任务被忽略", len(sch.schedules) == 1)


# ── 5. CLI 管理入口 ─────────────────────────────────────────────

def cli_checks(home: Path) -> None:
    print("--- 5. CLI 管理入口 ---")
    from agent_platform import cli

    _write(home / "skills" / "custom.json", {
        "id": "custom-skill", "name": "自定义技能", "prompt": "你是自定义技能。",
    })
    _write(home / "mcp_catalog.json", {"servers": [
        {"id": "ok", "name": "可用", "transport": "stdio", "command": "python"},
    ]})

    def run(argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    try:
        code, out, err = run(["--home", str(home), "status", "--json"])
        data = json.loads(out)
        check("status --json 可被程序消费",
              code == 0 and data["config"]["enabled"] is False
              and data["skills"]["count"] >= 1, out[:240] + err[:240])
        code, out, err = run(["--home", str(home), "skills", "list", "--json"])
        listed = json.loads(out)
        check("skills list --json 列出技能",
              code == 0 and any(s["id"] == "custom-skill" for s in listed["skills"]),
              out[:240])
        code, out, err = run(["--home", str(home), "skills", "show",
                              "custom-skill", "--json"])
        shown = json.loads(out)
        check("skills show 带提示词", code == 0 and bool(shown["prompt"]), out[:240])
        code, out, err = run(["--home", str(home), "skills", "show", "no-such"])
        check("未知技能返回非零并列出可用项",
              code == 1 and "不存在" in err and "custom-skill" in err, err)
        code, out, err = run(["--home", str(home), "mcp", "list", "--json"])
        catalog = json.loads(out)
        check("mcp list --json 可被程序消费",
              code == 0 and any(s["id"] == "ok" for s in catalog["servers"]),
              out[:240])

        fresh = _home("tt-platform-init-")
        code, out, err = run(["--home", str(fresh), "init"])
        check("init 生成配置与目录",
              code == 0 and (fresh / "platform.yaml").is_file()
              and (fresh / "mcp_catalog.yaml").is_file()
              and (fresh / "skills").is_dir())
        text = (fresh / "platform.yaml").read_text(encoding="utf-8")
        check("init 默认不启用", "enabled: false" in text, text[:120])
        again = run(["--home", str(fresh), "init"])
        check("init 可重复执行且不覆盖已有配置",
              again[0] == 0
              and (fresh / "platform.yaml").read_text(encoding="utf-8") == text)

        code, out, err = run(["--home", str(fresh), "chat", "你好"])
        check("未启用时 chat 明确拒绝并给出开关办法",
              code == 1 and "TT_PLATFORM=1" in err, err[:240])
    finally:
        os.environ.pop("TT_PLATFORM_HOME", None)


# ── 6. 与存量引擎联跑（离线假模型 + 真实 build_agent）───────────

async def engine_checks(home: Path) -> None:
    print("--- 6. 与存量引擎联跑（技能提示词/工具白名单/MCP 调用）---")
    try:
        import server as engine_server
        from agentscope.formatter import DashScopeChatFormatter
        from agentscope.model import ChatModelBase, ChatResponse
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 存量引擎不可用（{exc}）")
        return
    if not MCP_OK:
        print(f"SKIP: agentscope/mcp 缺失（{MCP_SKIP}）")
        return

    _write(home / "platform.json", {"enabled": True, "defaultSkill": "echo-skill"})
    _write(home / "skills" / "echo.json", {
        "id": "echo-skill",
        "name": "回声技能",
        "description": "自测用回声技能",
        "prompt": "你是回声技能，先用 echo 工具核对，再以 [echo-skill] 开头回答。",
        "tools": {"allow": ["view_text_file", "use_skill"]},
        "mcp": {"servers": ["echo"], "allow": ["echo/echo"]},
        "context": {"mode": "general", "max_iters": 4, "lazy_mcp_tools": False},
    })
    _write(home / "mcp_catalog.json", {"servers": [
        {"id": "echo", "name": "自测回显", "transport": "stdio",
         "command": sys.executable, "args": [FIXTURE], "timeoutSecs": 30},
    ]})

    recorded: list[str] = []

    class ToolCallingModel(ChatModelBase):
        def __init__(self):
            super().__init__(model_name="fake-platform-model", stream=False)
            self.calls = 0

        async def __call__(self, messages, *args, **kwargs):
            recorded.append(json.dumps(messages, ensure_ascii=False, default=str))
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(content=[{
                    "type": "tool_use", "id": "call-echo-1", "name": "echo",
                    "input": {"text": "技能白名单"},
                }])
            return ChatResponse(content=[{"type": "text", "text": "已由技能处理。"}])

    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ToolCallingModel(), DashScopeChatFormatter())
    )
    work_dir = Path(tempfile.mkdtemp(prefix="tt-platform-work-"))
    old_home = os.environ.get("TT_PLATFORM_HOME")
    os.environ["TT_PLATFORM_HOME"] = str(home)
    try:
        dispatcher = AgentDispatcher(load_config())
        reply = await dispatcher.handle(AgentMessage(
            text="用回声工具确认这行数据", channel="cli", channel_id="terminal",
            conversation_id="c1", work_dir=str(work_dir),
        ))
    finally:
        if old_home is None:
            os.environ.pop("TT_PLATFORM_HOME", None)
        else:
            os.environ["TT_PLATFORM_HOME"] = old_home

    blob = "\n".join(recorded)
    check("存量引擎跑通并返回正文",
          reply.ok and "已由技能处理" in reply.text, reply.error or reply.text)
    check("技能提示词真的进入 system prompt",
          "回声技能" in blob and "[echo-skill]" in blob, blob[:240])
    check("技能工具白名单落到内置工具上",
          any("未注册工具" in n and "write_text_file" in n for n in reply.notes),
          str(reply.notes))
    check("MCP 工具按技能白名单接入",
          any("接入工具" in n and "echo" in n for n in reply.notes), str(reply.notes))
    check("MCP 工具调用结果回填模型上下文", "echo:技能白名单" in blob, blob[-400:])
    check("工具调用被记入步骤", "echo" in reply.steps, str(reply.steps))


# ── 入口 ────────────────────────────────────────────────────────

def main() -> None:
    bus_home = _home("tt-platform-bus-")
    catalog_home = _home("tt-platform-catalog-")
    gateway_home = _home("tt-platform-gateway-")
    cli_home = _home("tt-platform-cli-")
    engine_home = _home("tt-platform-engine-")

    _run(bus_checks())
    skill_checks(bus_home)
    catalog_checks(catalog_home)
    _run(pool_checks())
    _run(gateway_checks(gateway_home))
    cli_checks(cli_home)
    _run(engine_checks(engine_home))

    print("-" * 52)
    if FAILURES:
        print(f"engine platform selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine platform selftest: ALL PASS")


if __name__ == "__main__":
    main()
