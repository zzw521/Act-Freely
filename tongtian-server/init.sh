#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "========================================="
echo "  通天办公服务端 初始化启动脚本"
echo "========================================="

# ── 环境变量 ────────────────────────────────────────────────
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  echo "  ✓ 已加载 .env"
fi

# ── [1/3] 安装依赖 ──────────────────────────────────────────
echo "[1/3] 检查 Python 与依赖..."
BASE_PY="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
if [ -z "$BASE_PY" ]; then
  echo "  ✗ 未找到 Python，请先安装 Python 3.10+"
  exit 1
fi
if ! "$BASE_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "  ✗ Python 版本过低，需要 3.10+（当前：$("$BASE_PY" -V 2>&1)）"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "  → 创建虚拟环境 .venv"
  "$BASE_PY" -m venv .venv
fi

if [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
else
  echo "  ✗ 虚拟环境不可用，请删除 .venv 后重试"
  exit 1
fi

if "$VENV_PY" -c "import fastapi, uvicorn, multipart" 2>/dev/null; then
  echo "  ✓ Python 依赖已满足，跳过安装"
else
  echo "  → 安装 Python 依赖（requirements.txt）"
  "$VENV_PY" -m pip install --disable-pip-version-check -q -r requirements.txt
fi

# ── [2/3] 准备运行参数 ──────────────────────────────────────
echo "[2/3] 准备服务..."
ENV_NAME="${TT_SERVER_ENV:-development}"
HOST="${TT_SERVER_HOST:-127.0.0.1}"
PORT="${TT_SERVER_PORT:-8000}"
export TT_SERVER_HOST="$HOST" TT_SERVER_PORT="$PORT"
export TT_SEED_DEMO="${TT_SEED_DEMO:-true}"

if [ "$ENV_NAME" = "production" ] || [ "$ENV_NAME" = "prod" ]; then
  if [ -z "${TT_SERVER_JWT_SECRET:-}" ] || [ "${TT_SERVER_JWT_SECRET}" = "tongtian-dev-secret-change-me" ]; then
    echo "  ✗ 生产环境必须设置独立的 TT_SERVER_JWT_SECRET"
    echo "    生成示例：$VENV_PY -c \"import secrets; print(secrets.token_hex(32))\""
    exit 1
  fi
  if [ "${#TT_SERVER_JWT_SECRET}" -lt 16 ]; then
    echo "  ✗ 生产环境 TT_SERVER_JWT_SECRET 至少 16 个字符"
    exit 1
  fi
  if [ "${TT_CORS_ORIGINS:-*}" = "*" ]; then
    echo "  ⚠ 生产环境 CORS 为 *，建议用 TT_CORS_ORIGINS 收窄来源"
  fi
fi

DATA_DIR="${TT_SERVER_DATA_DIR:-$(pwd)/storage}"
mkdir -p "$DATA_DIR/packages"

DISPLAY_HOST="$HOST"
if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
  DISPLAY_HOST="127.0.0.1"
fi

MODE_DESC="开发模式"
if [ "$ENV_NAME" = "production" ] || [ "$ENV_NAME" = "prod" ]; then
  MODE_DESC="生产模式"
fi

SEED_DESC="关闭"
if [ "$TT_SEED_DEMO" = "true" ] || [ "$TT_SEED_DEMO" = "1" ] || [ "$TT_SEED_DEMO" = "on" ]; then
  SEED_DESC="开启（演示数据幂等写入，不覆盖已有条目）"
fi

ADMIN_DESC="未配置（可通过 TT_ADMIN_EMAIL/TT_ADMIN_PASSWORD 引导管理员）"
if [ -n "${TT_ADMIN_EMAIL:-}" ]; then
  ADMIN_DESC="$TT_ADMIN_EMAIL"
fi

# ── [3/3] 访问信息（前台阻塞前打印，便于开发全程查看） ──────
echo "[3/3] 即将启动：通天办公服务端"
echo "-----------------------------------------"
echo "  服务地址:     http://${DISPLAY_HOST}:${PORT}"
echo "  接口文档:     http://${DISPLAY_HOST}:${PORT}/docs"
echo "  OpenAPI:      http://${DISPLAY_HOST}:${PORT}/openapi.json"
echo "  健康检查:     http://${DISPLAY_HOST}:${PORT}/health"
echo "  市场列表:     /api/v1/skills | /api/v1/suites | /api/v1/mcp"
echo "  运行环境:     ${ENV_NAME}（${MODE_DESC}）"
echo "  数据目录:     ${DATA_DIR}"
echo "  演示数据:     ${SEED_DESC}"
echo "  管理员:       ${ADMIN_DESC}"
echo "  注册开关:     ${TT_ALLOW_REGISTRATION:-true}"
echo "  桌面端联调:   TT_AUTH_MOCK=false TT_AUTH_SERVER_URL=http://${DISPLAY_HOST}:${PORT}"
echo "  测试:         ${VENV_PY} tests/test_units.py && ${VENV_PY} tests/test_server.py"
echo "  自动重载:     TT_RELOAD=true 时开启（默认关闭，避免重复初始化）"
echo "-----------------------------------------"

if [ "${TT_RELOAD:-false}" = "true" ]; then
  exec "$VENV_PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
else
  exec "$VENV_PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
fi
