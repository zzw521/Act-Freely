"""平台层 CLI：管理 + 调试入口（CLI 与 IM / 定时任务同为总线上的平等通道）。

命令只做配置查看、连通性验证和单次任务；不启动桌面壳，也不依赖 Web 后台。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .adapters import ChannelGateway, CliAdapter, ImAdapter, ScheduleAdapter
from .config import data_home, load_config
from .dispatcher import AgentDispatcher
from .mcp_catalog import McpCatalog, McpClientPool
from .message import MessageBus
from .skills import SkillRegistry


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _ensure_enabled(dispatcher: AgentDispatcher) -> bool:
    if dispatcher.config.enabled:
        return True
    print(
        "平台层未启用：设置环境变量 TT_PLATFORM=1，或在 "
        f"{dispatcher.config.home / 'platform.yaml'} 写 enabled: true",
        file=sys.stderr,
    )
    return False


# ── 查询类命令 ───────────────────────────────────────────────────


def cmd_status(args) -> int:
    cfg = load_config()
    skills = SkillRegistry.load(cfg)
    catalog = McpCatalog.load(cfg)
    data = {
        "config": cfg.view(),
        "skills": {
            "count": len(skills.all()),
            "ids": [s.id for s in skills.all()],
            "errors": skills.errors,
        },
        "mcp": {
            "count": len(catalog.all()),
            "ids": [s.id for s in catalog.all()],
            "errors": catalog.errors,
        },
        "channels": _im_views(),
    }
    if args.json:
        _print_json(data)
        return 0
    print(f"平台层：{'已启用' if cfg.enabled else '未启用'}（来源：{cfg.source}）")
    print(f"配置根：{cfg.home}")
    print(f"技能：{len(skills.all())} 个 — {'、'.join(s['id'] for s in skills.view()['skills']) or '无'}")
    print(f"MCP：{len(catalog.all())} 个 — {'、'.join(s['id'] for s in catalog.view()['servers']) or '无'}")
    for err in [*cfg.errors, *skills.errors, *catalog.errors]:
        print(f"配置错误：{err}")
    return 0


def cmd_skills(args) -> int:
    cfg = load_config()
    registry = SkillRegistry.load(cfg)
    if args.action == "show":
        skill = registry.get(args.id)
        if skill is None:
            print(f"技能「{args.id}」不存在，可用：{'、'.join(s.id for s in registry.all()) or '无'}",
                  file=sys.stderr)
            return 1
        data = skill.view(with_prompt=True)
        if args.json:
            _print_json(data)
        else:
            print(f"{data['id']} — {data['name']}")
            print(f"描述：{data['description'] or '（无）'}")
            print(f"关键词：{'、'.join(data['keywords']) or '（无）'}")
            print(f"工具白名单：{'、'.join(data['toolsAllow']) or '（不限制）'}")
            print(f"禁用工具：{'、'.join(data['toolsDeny']) or '（无）'}")
            print(f"MCP 服务器：{'、'.join(data['mcpServers']) or '（不接）'}")
            print(f"上下文：mode={data['context']['mode']} maxIters={data['context']['maxIters']} "
                  f"lazyMcp={data['context']['lazyMcpTools']} window={data['context']['contextWindow']}")
            print(f"来源：{data['source']}")
            print("提示词：")
            print(data["prompt"])
        return 0
    data = registry.view(with_prompt=False)
    if args.json:
        _print_json(data)
        return 0
    print(f"技能目录：{cfg.skills_dir}")
    for skill in data["skills"]:
        origin = "内置" if skill["builtin"] else skill["source"]
        print(f"- {skill['id']}  {skill['name']}  [{origin}]  {skill['description']}")
    for err in data["errors"]:
        print(f"错误：{err}", file=sys.stderr)
    return 0


def cmd_mcp(args) -> int:
    cfg = load_config()
    catalog = McpCatalog.load(cfg)
    if args.action == "list":
        data = catalog.view()
        if args.json:
            _print_json(data)
            return 0
        print(f"Catalog：{data['source'] or '（未配置）'}")
        for server in data["servers"]:
            state = "启用" if server["enabled"] else "停用"
            target = server.get("url") or f"{server.get('command')} {' '.join(server.get('args') or [])}"
            print(f"- {server['id']}  {server['name']}  [{server['transport']}/{state}]  {target}")
        for err in data["errors"]:
            print(f"错误：{err}", file=sys.stderr)
        return 0

    server = catalog.get(args.id)
    if server is None:
        print(f"Catalog 中没有服务器「{args.id}」", file=sys.stderr)
        return 1
    if args.action in ("tools", "test"):
        return asyncio.run(_mcp_probe(catalog, args))
    if args.action == "call":
        return asyncio.run(_mcp_call(catalog, args))
    return 2


async def _mcp_probe(catalog: McpCatalog, args) -> int:
    async with McpClientPool(catalog) as pool:
        try:
            tools = await pool.discover(args.id)
        except Exception as exc:  # noqa: BLE001
            print(f"连接失败：{exc}", file=sys.stderr)
            return 1
    if args.json:
        _print_json({"server": args.id, "tools": tools})
        return 0
    print(f"连接器「{args.id}」可用工具 {len(tools)} 个：")
    for tool in tools:
        print(f"- {tool['name']}：{tool['description'] or '（无描述）'}")
    return 0


async def _mcp_call(catalog: McpCatalog, args) -> int:
    try:
        arguments = json.loads(args.args or "{}")
    except Exception as exc:  # noqa: BLE001
        print(f"--args 不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    async with McpClientPool(catalog) as pool:
        try:
            text = await pool.call(args.id, args.tool, arguments)
        except Exception as exc:  # noqa: BLE001
            print(f"调用失败：{exc}", file=sys.stderr)
            return 1
    if args.json:
        _print_json({"server": args.id, "tool": args.tool, "result": text})
    else:
        print(text)
    return 0


def cmd_channels(args) -> int:
    views = _im_views()
    if args.json:
        _print_json({"channels": views})
        return 0
    if not views:
        print("没有可用的 IM 通道实现")
        return 0
    for view in views:
        state = view.get("state") or ("已接入" if view.get("supported") else "尚未接入")
        print(f"- {view.get('kind')}  {view.get('label')}  [{state}]  "
              f"已配置={'是' if view.get('configured') else '否'}  {view.get('unsupportedReason') or ''}")
    print("\nCLI 通道：python scripts/agentctl.py chat \"...\"")
    print("定时通道：python scripts/agentctl.py schedule run")
    return 0


def _im_views() -> list[dict]:
    try:
        from .adapters import load_im_channels

        imc, errors = load_im_channels()
        views = [runtime.view() for runtime in imc.all_runtimes()]
        for err in errors:
            views.append({"kind": "im", "label": "IM", "state": "unavailable",
                          "unsupportedReason": err})
        return views
    except Exception as exc:  # noqa: BLE001
        return [{"kind": "im", "label": "IM", "state": "unavailable",
                 "unsupportedReason": f"IM 通道不可用：{exc}"}]


# ── 执行类命令 ───────────────────────────────────────────────────


def cmd_chat(args) -> int:
    dispatcher = AgentDispatcher()
    if not _ensure_enabled(dispatcher):
        return 1

    async def run() -> int:
        bus = MessageBus()
        gateway = ChannelGateway(bus, dispatcher, workers=1)
        cli = CliAdapter(bus, dispatcher, conversation_id=args.conversation)
        await gateway.start_workers()
        try:
            reply = await cli.ask(
                args.text, skill=args.skill, model=args.model, work_dir=args.work_dir
            )
        finally:
            await gateway.stop()
        if args.json:
            _print_json(reply.to_dict())
        else:
            print(f"技能：{reply.skill_id or '（未解析）'}")
            for note in reply.notes:
                print(f"说明：{note}")
            print(CliAdapter.render(reply))
        return 0 if reply.ok else 1

    return asyncio.run(run())


def cmd_im(args) -> int:
    dispatcher = AgentDispatcher()
    if not _ensure_enabled(dispatcher):
        return 1

    async def run() -> int:
        bus = MessageBus()
        gateway = ChannelGateway(bus, dispatcher)
        gateway.register(ImAdapter(bus, dispatcher, args.kind))
        await gateway.start()
        print(f"IM 通道 {args.kind} 已接入平台总线；Ctrl+C 结束", flush=True)
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await gateway.stop()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def cmd_schedule(args) -> int:
    dispatcher = AgentDispatcher()
    if not _ensure_enabled(dispatcher):
        return 1
    if not dispatcher.config.schedules:
        print(f"没有定时任务：请在 {dispatcher.config.home / 'platform.yaml'} 配置 schedules",
              file=sys.stderr)
        return 1

    async def run() -> int:
        bus = MessageBus()
        gateway = ChannelGateway(bus, dispatcher)
        gateway.register(ScheduleAdapter(bus, dispatcher, dispatcher.config.schedules))
        await gateway.start()
        print(f"定时通道已启动：{len(dispatcher.config.schedules)} 个任务；Ctrl+C 结束", flush=True)
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await gateway.stop()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def cmd_init(args) -> int:
    """生成最小可用配置：技能目录 + MCP 目录 + platform.yaml（已存在不覆盖）。"""
    home = Path(args.home).expanduser() if args.home else data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "skills").mkdir(exist_ok=True)
    (home / "workspace").mkdir(exist_ok=True)
    platform = home / "platform.yaml"
    if not platform.exists():
        platform.write_text(
            "# 通天 Agent 平台（单用户本地运行）\n"
            "enabled: false          # 或设置环境变量 TT_PLATFORM=1\n"
            "defaultSkill: general\n"
            "maxParallel: 2\n"
            "schedules: []\n",
            encoding="utf-8",
        )
    catalog = home / "mcp_catalog.yaml"
    if not catalog.exists():
        catalog.write_text(
            "servers: []\n"
            "# 示例：\n"
            "# servers:\n"
            "#   - id: filesystem\n"
            "#     name: 本地文件系统\n"
            "#     transport: stdio\n"
            "#     command: npx\n"
            "#     args: [\"-y\", \"@modelcontextprotocol/server-filesystem\", \".\"]\n",
            encoding="utf-8",
        )
    print(f"已初始化：{home}")
    print(f"  配置：{platform}")
    print(f"  技能目录：{home / 'skills'}")
    print(f"  MCP 目录：{catalog}")
    print("下一步：复制 agent_platform/builtin/skills 下的示例，或直接使用内置技能。")
    print("启用：设置 TT_PLATFORM=1 后运行 python scripts/agentctl.py chat \"你好\"")
    return 0


# ── 参数解析 ────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description="通天 Agent 平台管理/调试 CLI（CLI、IM、定时任务同为总线通道）",
    )
    parser.add_argument("--home", help="覆盖平台配置根（等价于 TT_PLATFORM_HOME）")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="查看开关、技能、MCP、通道状态")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    skills = sub.add_parser("skills", help="技能管理")
    skills_sub = skills.add_subparsers(dest="action", required=True)
    skills_list = skills_sub.add_parser("list", help="列出技能")
    skills_list.add_argument("--json", action="store_true")
    skills_list.set_defaults(func=cmd_skills)
    skills_show = skills_sub.add_parser("show", help="查看技能详情与提示词")
    skills_show.add_argument("id")
    skills_show.add_argument("--json", action="store_true")
    skills_show.set_defaults(func=cmd_skills)
    skills_reload = skills_sub.add_parser("reload", help="重新扫描（运行中也会自动热加载）")
    skills_reload.add_argument("--json", action="store_true")
    skills_reload.set_defaults(func=cmd_skills, id=None)

    mcp = sub.add_parser("mcp", help="MCP Catalog 与工具调试")
    mcp_sub = mcp.add_subparsers(dest="action", required=True)
    mcp_list = mcp_sub.add_parser("list", help="列出 Catalog")
    mcp_list.add_argument("--json", action="store_true")
    mcp_list.set_defaults(func=cmd_mcp, id=None)
    for action, help_text in (("tools", "连接并列出工具"), ("test", "同上，验证连通性")):
        item = mcp_sub.add_parser(action, help=help_text)
        item.add_argument("id")
        item.add_argument("--json", action="store_true")
        item.set_defaults(func=cmd_mcp)
    call = mcp_sub.add_parser("call", help="代理调用一个 MCP 工具")
    call.add_argument("id")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="JSON 参数对象")
    call.add_argument("--json", action="store_true")
    call.set_defaults(func=cmd_mcp)

    channels = sub.add_parser("channels", help="列出通道（IM/CLI/定时）")
    channels.add_argument("--json", action="store_true")
    channels.set_defaults(func=cmd_channels)

    chat = sub.add_parser("chat", help="单次任务（经消息总线与调度层）")
    chat.add_argument("text")
    chat.add_argument("-s", "--skill", help="指定技能 id（缺省按关键词匹配）")
    chat.add_argument("--model", help="覆盖本轮模型名")
    chat.add_argument("--work-dir", help="工作目录（缺省为 platform home/workspace/<conversation>）")
    chat.add_argument("--conversation", default="", help="会话 id（用于隔离历史与回复路由）")
    chat.add_argument("--json", action="store_true")
    chat.set_defaults(func=cmd_chat)

    im = sub.add_parser("im", help="把已配置的 IM 通道接入平台总线")
    im.add_argument("kind", help="slack / dingtalk / feishu / wecom")
    im.set_defaults(func=cmd_im)

    schedule = sub.add_parser("schedule", help="运行定时任务通道")
    schedule.add_argument("action", choices=["run"])
    schedule.set_defaults(func=cmd_schedule)

    init = sub.add_parser("init", help="初始化平台配置目录")
    init.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    if getattr(args, "home", None):
        os.environ["TT_PLATFORM_HOME"] = str(Path(args.home).expanduser())
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"执行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
