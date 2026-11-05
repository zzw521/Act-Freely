"""artifact 车道（W4）：工作产物、发布与更新清单。

占用前缀见 routes_lanes.LANE_PREFIXES["artifact"]。既有的 /api/files/upload、
/api/files/content、/api/files/open-folder 留在 server.py（被契约测试逐字钉住），
新端点写这里。
"""

from fastapi import APIRouter

import routes_lanes as rl

LANE = "artifact"
OWNER = rl.LANE_OWNERS["artifact"]

router = APIRouter()
