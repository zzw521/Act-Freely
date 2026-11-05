"""artifact 工具车道（W4）：办公产物与发布产物面。

开关：slides（幻灯片模式）/ office（通用与写作模式），见 toolkit_lanes.SWITCH_BOARD。
这两件刻意不常驻注册：Rust 侧 harness/tools.rs 没有它们的实现，进常驻清单就是空头承诺
（tests/unit/extensions/builtin-tools-contract.test.ts 锁的就是这条口径）。
生成正文仍在 server.py 的 build_toolkit 闭包里（连 500 行 Office 排版一起搬出去不在接缝阶段做），
本车道只承担「注册与否」。
"""

import toolkit_lanes as tl


LANE = "artifact"
SWITCHES = tl.SWITCH_BOARD["artifact"]


def register(ctx: tl.ToolkitContext) -> None:
    if ctx.slides:
        ctx.toolkit.register_tool_function(ctx.tool("generate_pptx"))
    if ctx.office:
        ctx.toolkit.register_tool_function(ctx.tool("generate_xlsx"))
        ctx.toolkit.register_tool_function(ctx.tool("generate_docx"))
