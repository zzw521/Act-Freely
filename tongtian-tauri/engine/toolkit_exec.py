"""exec 工具车道（W4）：受控命令执行 / 联网获取 / 写暂存审批。

开关：grants（用户在工作区里的显式授权，tool_grants.ToolGrantPolicy），
见 toolkit_lanes.SWITCH_BOARD。未授权或白名单为空时这里必须一件都不注册——
「模型知道有这能力但调不到」比不提更糟，提示词那边也是同一个开关（build_agent 里 grants.system_hint()）。
"""

import toolkit_lanes as tl


LANE = "exec"
SWITCHES = tl.SWITCH_BOARD["exec"]


def register(ctx: tl.ToolkitContext) -> None:
    if ctx.grants is not None:
        ctx.grants.install(ctx.toolkit, ctx.base, emit=ctx.emit, task_id=ctx.task_id)
