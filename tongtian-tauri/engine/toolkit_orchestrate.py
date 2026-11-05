"""orchestrate 工具车道（W6）：团队主持人的派工工具与提示词段。

开关：`subagent`（装配现场的 subagent_runner，已接线）+ `team`（**待接线**）。
注意两条：

1. 父 Agent 的 `spawn_subagents` 今天仍由 build_agent 用 `subagent_runner.tool()` 注册，
   子 Agent 传 `subagent_runner=None` 从工具面上杜绝递归派生——这条 None 语义是安全边界，
   本车道注册新工具沿用同一个开关，不造「总是注册」的第二条路径。
2. 团队工具缺的不是 `subagent` 那一道门，而是**房间**：`team_dispatch` 只能把活投进
   「本轮主持人会话自己那个房间」，所以它必须拿到 `(runtime, room)` 这一对。装配现场
   今天没有下发 `team`，`ToolkitContext` 也没有这个属性 ⇒ 本车道**今天一件都不注册**，
   与今日行为一致（工具面上看不见 team_dispatch，提示词里也不会出现团队段）。
   要点亮请看 `docs/并行实施/148.md` §2：W4 在 `SWITCH_BOARD["orchestrate"]` 里加
   `team`、在 build_agent 的 `extra` 里按「本轮会话绑过编制」补那一句。

新工具名一律挂开关，不许进常驻清单——Rust 侧没有实现的能力进清单就是空头承诺：
`team_dispatch` / `team_room_status` 需要 `src-tauri/src/harness/tools.rs` 的领取回路
先接上（那份在别的窗口领地内，本卡没动）。
"""

from __future__ import annotations

from typing import Any

import toolkit_lanes as tl


LANE = "orchestrate"
SWITCHES = tl.SWITCH_BOARD["orchestrate"]

#: 车道注册的工具名（只有这一处声明，测试与分片都从这里取）。
TEAM_TOOLS = ("team_dispatch", "team_room_status")


def _team_handle(ctx: tl.ToolkitContext) -> Any:
    """取装配现场下发的团队句柄。

    句柄要同时给得出**运行时**和**房间**：只给运行时等于允许模型「派给任意一个房间」，
    那是把别的会话的槽位与目录当成自己的；只给房间又起不了投递。两个都齐才注册，
    缺一个就退回今日行为，并让 notes 说清楚缺的是哪一个。
    """
    handle = getattr(ctx, "team", None)
    if handle is None:
        return None
    runtime = getattr(handle, "runtime", None) or (handle.get("runtime") if isinstance(handle, dict) else None)
    room = getattr(handle, "room", "") or (handle.get("room") if isinstance(handle, dict) else "")
    if runtime is None or not str(room or "").strip():
        return None
    return (runtime, str(room).strip())


def host_hint(ctx: tl.ToolkitContext) -> str:
    """主持人本轮的编制提示词；没团队句柄就是空串（提示词里不出现团队段）。

    给 build_agent 拼 system prompt 用——**这条也走同一个门**：工具没注册却在提示词里
    告诉模型「你有 team_dispatch」，模型第一轮就会去调一个不存在的工具。
    """
    handle = _team_handle(ctx)
    if handle is None:
        return ""
    runtime, room = handle
    try:
        return str(runtime.system_hint(room=room) or "")
    except Exception:  # noqa: BLE001  提示词缺失只影响引导，不该炸掉整轮装配
        return ""


def register(ctx: tl.ToolkitContext) -> None:
    if ctx.subagent is None:
        # 子 Agent 那侧的 None 语义是「递归派生从工具面上杜绝」；团队工具同一条门：
        # 成员执行体不是主持人，它没有 subagent_runner，也就不该在这里拿到派工工具。
        return
    handle = _team_handle(ctx)
    if handle is None:
        return
    runtime, room = handle
    for fn in runtime.tools(room=room, emit=ctx.emit):
        ctx.toolkit.register_tool_function(fn)
