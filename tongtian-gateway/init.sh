#!/usr/bin/env bash
# 网关本地初始化：建 venv、装依赖、导出 Schema、跑契约测试，然后启动。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON:-python}"
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi

if [ -x .venv/Scripts/python.exe ]; then
  VENV_PY=.venv/Scripts/python.exe
else
  VENV_PY=.venv/bin/python
fi

"$VENV_PY" -m pip install --upgrade pip >/dev/null
if ! "$VENV_PY" -m pip install -r requirements.txt; then
  # 国内镜像常缺 aiohttp>=3.14.2 与 mcp>=2.2，改从官方源补这两条后再装
  echo "默认镜像缺新版 aiohttp/mcp，改用官方 PyPI 补装" >&2
  "$VENV_PY" -m pip install "aiohttp==3.14.5" "mcp==2.3.0" --index-url https://pypi.org/simple
  "$VENV_PY" -m pip install -r requirements.txt
fi

"$VENV_PY" scripts/export_schemas.py
"$VENV_PY" tests/test_contract.py

exec "$VENV_PY" -m app.main
