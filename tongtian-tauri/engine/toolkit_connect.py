"""connect 工具车道（W5）：连接器 / IM / 定时 / 浏览器相关工具。

开关：connectors。**这条开关目前没接线**（缺省 None ⇒ 本车道一件都不注册）：
MCP 工具今天仍由 build_agent 走 mr.attach(...)、浏览器仍走 browser.install(toolkit)，
都不经过本车道；把主线搬进车道不在 #164 的范围内。
要在这里注册新工具，先提分片给 W4 把开关点亮（build_toolkit 的 extra 里补一个键）。
"""

import toolkit_lanes as tl


LANE = "connect"
SWITCHES = tl.SWITCH_BOARD["connect"]


def register(ctx: tl.ToolkitContext) -> None:
    if ctx.connectors is None:
        return
