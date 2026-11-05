"""voice 工具车道（W7）：语音输入 / 实时语音相关工具。

开关：voice。**目前没接线**（缺省 None ⇒ 本车道一件都不注册）：引擎今天没有语音链路，
装配现场也没有可交给车道的音频句柄。W7 落地适配器后提分片，由 W4 在 build_agent 的
extra 里把这一路的开关点亮；在此之前请把工具留在自己的 engine/audio_*.py 里，
用契约测试与假上游验，别在没有 Rust 实现时把它塞进常驻清单。
"""

import toolkit_lanes as tl


LANE = "voice"
SWITCHES = tl.SWITCH_BOARD["voice"]


def register(ctx: tl.ToolkitContext) -> None:
    if ctx.voice is None:
        return
