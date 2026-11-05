"""knowledge 工具车道（W2）：知识库检索 / 技能正文 / 产物历史相关工具。

开关：knowledge（装配现场的知识库句柄），见 toolkit_lanes.SWITCH_BOARD。
开关：knowledge（装配现场的知识库句柄，已从 build_agent 接线）。
注意：既有知识库工具的注册仍在 build_agent 里走 knowledge.install(toolkit)
（knowledge_runtime 自带 install，#164 不动主线装配）；本车道用于 W2 新增的工具。
新工具名一律挂开关，不许进常驻清单——Rust 侧没有实现的能力进清单就是空头承诺。
"""

import toolkit_lanes as tl


LANE = "knowledge"
SWITCHES = tl.SWITCH_BOARD["knowledge"]


def register(ctx: tl.ToolkitContext) -> None:
    if ctx.knowledge is None:
        return
