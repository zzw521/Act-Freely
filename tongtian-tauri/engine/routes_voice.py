"""voice 车道（W7）：语音链路（ASR / TTS / 实时）。

占用前缀见 routes_lanes.LANE_PREFIXES["voice"]。
分工文档按六车道拆，语音单列为第七条：W7 若把音频端点塞进 connect 就会和 W5 抢同一个文件，
没有 git 的仓库里那就是后写的覆盖先写的。
ASR/TTS 用可插拔适配器 + 假上游契约测试，真机麦克风与厂商端到端在交棒前一律标未验收。
"""

from fastapi import APIRouter

import routes_lanes as rl

LANE = "voice"
OWNER = rl.LANE_OWNERS["voice"]

router = APIRouter()
