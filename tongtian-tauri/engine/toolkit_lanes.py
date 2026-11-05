"""#164 工具注册分片的开关集中声明处（W4 领地）。

engine/server.py 的 build_toolkit() 原先用 ``if slides:`` / ``if office:`` /
``if grants is not None:`` 三处内联条件注册。七窗口同时打开 build_toolkit 一定会互相覆盖，
所以条件注册改由各车道的 toolkit_<domain>.register(ctx) 承担，**开关在哪条车道、由谁授权**
只在下面 SWITCH_BOARD 里说一次。

为什么新能力一律要挂在开关上（不是风格问题）：
tests/unit/extensions/builtin-tools-contract.test.ts 要求「常驻清单里每个 id 都必须在
src-tauri/src/harness/tools.rs 有实现」。没有 Rust 实现就塞进常驻清单，等于界面上多两条
空头承诺。常驻六件（list_directory / view_text_file / read_document / write_text_file /
insert_text_file / delete_text_file）刻意留在 build_toolkit 里，不进任何车道。

运行时就把住这条口径（见 register_all 的三条判定），不靠测试事后追。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# 常驻六件：车道不许注册同名工具，也不许把它们当成自己的成果声明。
RESIDENT_TOOLS = (
    "list_directory",
    "view_text_file",
    "read_document",
    "write_text_file",
    "insert_text_file",
    "delete_text_file",
)

# 车道 → 它的开关（ToolkitContext 上的属性名）。开关全为假时该车道必须一件都不注册。
# 新开关/新车道都改这里，然后提分片给 W4；车道文件里不写别的判定来源。
SWITCH_BOARD = {
    # artifact_history 由 W4 按分片 138.md §5-a 补进 knowledge 车道：产物版本历史与知识库
    # 是两件事，用它自己的句柄当门，不复用 knowledge（那条门管的是检索工具）。
    "knowledge": ("knowledge", "artifact_history"),
    "artifact": ("slides", "office"),
    "workspace": ("workspace_backend",),
    "exec": ("grants",),
    "connect": ("connectors",),
    "orchestrate": ("subagent",),
    "voice": ("voice",),
}

LANE_MODULE_NAMES = (
    "toolkit_knowledge",
    "toolkit_artifact",
    "toolkit_workspace",
    "toolkit_exec",
    "toolkit_connect",
    "toolkit_orchestrate",
    "toolkit_voice",
)


class ToolkitLaneError(RuntimeError):
    """车道声明与开关对不上（越界、无开关注册、抢常驻名、跨车道重名）。"""


class ToolkitContext:
    """build_toolkit 交给车道的装配现场。

    tools 里放 build_toolkit 内部定义、但由车道负责注册的工具闭包
    （generate_pptx / generate_xlsx / generate_docx：正文与沙箱边界都在 build_toolkit 里，
    挪进车道文件要连 500 行 Office 排版一起搬，接缝阶段不动它，只把「注册与否」挪出来）。
    """

    def __init__(
        self,
        *,
        toolkit: Any,
        work_dir: Any,
        base: Any,
        sandbox: Any,
        emit: Callable[..., None] | None,
        task_id: str,
        slides: bool,
        office: bool,
        grants: Any,
        pptx_templates: dict | None,
        tools: dict[str, Callable] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.toolkit = toolkit
        self.work_dir = work_dir
        self.base = base
        self.sandbox = sandbox
        self.emit = emit
        self.task_id = task_id
        self.slides = slides
        self.office = office
        self.grants = grants
        self.pptx_templates = pptx_templates
        self.tools = dict(tools or {})
        # 开关读数：knowledge 与 subagent 由 build_agent 经 extra 接线（值是装配现场已有的句柄）；
        # workspace_backend / connectors / voice 尚未接线，恒为 None ⇒ 对应车道一件都不注册，
        # 与今日行为一致。要点亮请提分片给 W4，在 build_agent 的 extra 里补键。
        # 开关为 None 即「不注册」，车道只读不猜。
        self.knowledge = (extra or {}).get("knowledge")
        self.artifact_history = (extra or {}).get("artifact_history")
        self.workspace_backend = (extra or {}).get("workspace_backend")
        self.connectors = (extra or {}).get("connectors")
        self.subagent = (extra or {}).get("subagent")
        self.voice = (extra or {}).get("voice")

    def tool(self, name: str) -> Callable:
        """取 build_toolkit 交下来的闭包；取不到就是有人改了闭包名却没同步车道。"""
        try:
            return self.tools[name]
        except KeyError:
            raise ToolkitLaneError(
                f"装配现场没交出工具 {name!r}（build_toolkit 的 tools 字典里只有："
                f"{', '.join(sorted(self.tools)) or '空'}）"
            ) from None


def _registered_names(toolkit) -> set[str]:
    return set(getattr(toolkit, "tools", {}).keys())


def _switch_state(ctx, switches: Iterable[str]) -> tuple[str, ...]:
    return tuple(name for name in switches if getattr(ctx, name, None))


def register_all(ctx, lane_names: Iterable[str] = LANE_MODULE_NAMES) -> list[dict]:
    """按声明顺序跑各车道的注册，返回「车道 → 实际注册了什么」。

    三条判定（越界就地抛，不带病跑完一轮才在界面上露馅）：
    1. 开关全为假的车道必须一件都不注册 —— 常驻清单不因为某条车道写错而变胖；
    2. 车道不许注册常驻六件里的名字；
    3. 同一工具名不许两条车道各注册一次（后写的会把前写的静默盖掉）。
    """
    import importlib

    report: list[dict] = []
    claimed: dict[str, str] = {}
    for module_name in lane_names:
        lane = importlib.import_module(module_name)
        lane_name = getattr(lane, "LANE", module_name)
        if lane_name not in SWITCH_BOARD:
            raise ToolkitLaneError(f"{module_name} 的 LANE={lane_name!r} 不在开关表里")
        switches = SWITCH_BOARD[lane_name]
        before = _registered_names(ctx.toolkit)
        lane.register(ctx)
        added = sorted(_registered_names(ctx.toolkit) - before)
        active = _switch_state(ctx, switches)
        if added and not active:
            raise ToolkitLaneError(
                f"{lane_name} 车道在开关 {', '.join(switches)} 全为假时注册了 "
                f"{', '.join(added)}：这会把没有 Rust 实现的能力塞进常驻清单"
            )
        for name in added:
            if name in RESIDENT_TOOLS:
                raise ToolkitLaneError(f"{lane_name} 车道重复声明常驻工具 {name}")
            if name in claimed:
                raise ToolkitLaneError(
                    f"工具 {name} 被 {claimed[name]} 和 {lane_name} 两条车道各注册一次"
                )
            claimed[name] = lane_name
        report.append({
            "lane": lane_name,
            "switches": list(switches),
            "active_switches": list(active),
            "registered": added,
        })
    return report
