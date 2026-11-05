#!/usr/bin/env python3
"""给假模型端点改运行时参数（在模型容器内调用自己，宿主够不到 bridge 内网）。

用法：python modelctl.py '{"exec_delay_s":150,"tool_rounds":5}'
"""

import json
import sys
import urllib.request

payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
req = urllib.request.Request(  # noqa: S310 - 固定演练回环地址
    "http://127.0.0.1:9000/__config",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(resp.read().decode("utf-8"))
