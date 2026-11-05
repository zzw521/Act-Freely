"""容器健康检查：GET /health 返回 200 视为健康。"""

import os
import sys
import urllib.request

port = os.environ.get("TT_SERVER_PORT", "8000")

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
