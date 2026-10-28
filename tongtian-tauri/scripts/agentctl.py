#!/usr/bin/env python3
"""通天 Agent 平台 CLI 入口（无第三方依赖；依赖引擎目录内的实现）

用法：
    python scripts/agentctl.py status
    python scripts/agentctl.py skills list
    python scripts/agentctl.py mcp tools <server-id>
    TT_PLATFORM=1 python scripts/agentctl.py chat -s weekly-report "生成本周周报"
    TT_PLATFORM=1 python scripts/agentctl.py im slack
    TT_PLATFORM=1 python scripts/agentctl.py schedule run
"""

import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE))

from agent_platform.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
