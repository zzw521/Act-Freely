#!/bin/bash
# 准备随包 Python 运行时（发布方案 B：embeddable 解释器 + pip --target 锁定依赖）
#
# 产物：vendor/python/  —— 打包时由 tauri.conf.json 的 bundle.resources
#       以 "../vendor/python" → "python" 随包分发，Rust 侧 bridge::python_env::bundled_python() 优先使用它。
#       （resources 的相对路径以 tauri.conf.json 所在目录即 src-tauri/ 为基准，故必须放在项目根）
#
# 幂等：重复执行会跳过已完成的步骤；FORCE=1 彻底重建。
# 用法：bash scripts/prepare-python.sh   （或 FORCE=1 重建）
set -euo pipefail

PY_VER="${PY_VER:-3.12.10}"
PY_SERIES="${PY_VER%.*}"                      # 3.12
PIP_VER="${PIP_VER:-25.0.1}"

# 完整性优先：下载源只是传输通道，SHA-256 不匹配立即失败
EMBED_SHA256="${EMBED_SHA256:-4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3}"
PIP_SHA256="${PIP_SHA256:-c46efd13b6aa8279f33f2864459c8ce587ea6a1a59ee20de055868d8f7688f7f}"

PYTHON_MIRROR="${PYTHON_MIRROR:-https://registry.npmmirror.com/-/binary/python}"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_WHEEL_HOST="${PIP_WHEEL_HOST:-https://pypi.tuna.tsinghua.edu.cn/packages}"
# PyPI 上的 wheel 哈希路径；换版本时同步更新（脚本会核对文件名，避免下错包）
PIP_WHEEL_PATH="${PIP_WHEEL_PATH:-c9/bc/b7db44f5f39f9d0494071bddae6880eb645970366d0a200022a1a93d57f5}"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$SCRIPT_DIR")                  # tongtian-tauri/
SRC_TAURI="$ROOT/src-tauri"
VENDOR="$ROOT/vendor/python"
SITE="$VENDOR/Lib/site-packages"
DL="$SRC_TAURI/target/dl/python-vendor"
EMBED_ZIP="python-${PY_VER}-embed-amd64.zip"
LOCK="$ROOT/engine/requirements.lock.txt"

verify_sha256() { # <file> <expected> <label>
  echo "  校验 $3 SHA-256..."
  if ! echo "$2  $1" | sha256sum -c --status -; then
    echo "  ✗ $3 校验失败：文件内容与锁定值不一致，拒绝使用" >&2
    exit 1
  fi
}

if [ "${FORCE:-0}" = "1" ] && [ -d "$VENDOR" ]; then
  echo "FORCE=1：删除现有 $VENDOR"
  rm -rf "$VENDOR"
fi
mkdir -p "$DL" "$SITE"

echo "========================================="
echo "  随包 Python 运行时准备（embeddable + 锁定依赖）"
echo "========================================="

# ── [1/6] 解释器 ────────────────────────────────────────────
if [ -x "$VENDOR/python.exe" ]; then
  echo "[1/6] 解释器已存在，跳过下载解压"
else
  echo "[1/6] 下载 CPython ${PY_VER} embeddable (win_amd64)..."
  if [ ! -f "$DL/$EMBED_ZIP" ]; then
    curl -fSL --retry 3 -o "$DL/$EMBED_ZIP" "$PYTHON_MIRROR/$PY_VER/$EMBED_ZIP"
  fi
  verify_sha256 "$DL/$EMBED_ZIP" "$EMBED_SHA256" "embeddable 发行包"
  (cd "$VENDOR" && unzip -oq "$DL/$EMBED_ZIP")
fi

# pythonNN._pth 决定解释器可见的搜索路径：相对路径以 python.exe 所在目录为基准，
# 因此安装到任意目录都成立。embeddable 默认不执行 site.main()，故 .pth 文件不会被处理，
# pywin32 需要的 win32 / win32/lib 必须在此显式列出（否则 agentscope→mcp 导入即失败）。
PTH="$VENDOR/python${PY_SERIES/./}._pth"
cat > "$PTH" <<EOF
python${PY_SERIES/./}.zip
.
Lib\\site-packages
Lib\\site-packages\\win32
Lib\\site-packages\\win32\\lib
Lib\\site-packages\\pythonwin
EOF
echo "  ✓ 写入 $(basename "$PTH")"

# ── [2/6] 按需引导 pip（embeddable 无 ensurepip） ────────────
# pip 在 [4/6] 会被剔除，所以“是否需要装依赖”必须先判断：只看 pip 目录会导致每次重跑都
# 重新解包再剔除，而只看标记文件则会在锁文件变更时误判 pip 可用、让 [3/6] 直接失败。
MARKER="$SITE/.tongtian-engine-lock"
LOCK_HASH=$(sha256sum "$LOCK" | cut -d' ' -f1)
NEED_INSTALL=1
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$LOCK_HASH" ]; then
  NEED_INSTALL=0
fi

if [ "$NEED_INSTALL" = "0" ]; then
  echo "[2/6] 依赖已与锁文件一致，无需引导 pip"
elif [ -d "$SITE/pip" ]; then
  echo "[2/6] pip 已在 site-packages 中，跳过引导"
else
  echo "[2/6] 引导 pip ${PIP_VER}（wheel 解包进 site-packages）..."
  WHEEL="pip-${PIP_VER}-py3-none-any.whl"
  if [ ! -f "$DL/$WHEEL" ]; then
    curl -fSL --retry 3 -o "$DL/$WHEEL" "$PIP_WHEEL_HOST/$PIP_WHEEL_PATH/$WHEEL"
  fi
  verify_sha256 "$DL/$WHEEL" "$PIP_SHA256" "pip wheel"
  (cd "$SITE" && unzip -oq "$DL/$WHEEL")
fi
if [ -d "$SITE/pip" ]; then
  "$VENDOR/python.exe" -m pip --version >/dev/null
  echo "  ✓ $("$VENDOR/python.exe" -m pip --version)"
fi

# ── [3/6] 安装锁定依赖 ──────────────────────────────────────
if [ "$NEED_INSTALL" = "0" ]; then
  echo "[3/6] 依赖与 requirements.lock.txt 一致，跳过安装"
else
  echo "[3/6] 安装 engine/requirements.lock.txt（$(grep -vc '^#' "$LOCK") 个包）..."
  "$VENDOR/python.exe" -m pip install --no-cache-dir --disable-pip-version-check \
    --target "$SITE" -r "$LOCK" -i "$PIP_INDEX" --trusted-host "$(echo "$PIP_INDEX" | sed -E 's#https?://([^/]+).*#\1#')"
  echo "$LOCK_HASH" > "$MARKER"
fi

# ── [4/6] 剔除运行时用不到的内容 ────────────────────────────
# 只删“引擎导入链证明用不到”的东西：numpy / sqlalchemy / grpc 已实测为 agentscope
# 导入期硬依赖，必须保留；__pycache__ 保留（安装目录可能只读，去掉会让每次启动重编译）。
prune_dead_weight() {
  local pruned=0
  for rel in \
    pip pip-*.dist-info bin \
    pythonwin win32com win32comext isapi adodbapi PyWin32.chm \
    win32/Demos win32/libs win32/scripts include \
    numpy/tests numpy/f2py numpy-*.dist-info/RECORD.txt \
    sqlalchemy/testing docutils
  do
    for hit in $SITE/$rel; do
      [ -e "$hit" ] || continue
      pruned=$((pruned + $(du -sk "$hit" | cut -f1)))
      rm -rf "$hit"
    done
  done
  # pywin32 的 _pth 条目里 pythonwin 已被剔除，保留条目不影响导入（多余路径会被忽略）
  echo "  剔除 ${pruned} KB"
}
echo "[4/6] 剔除随包运行时用不到的内容..."
prune_dead_weight
echo "[5/6] 冒烟测试（真实引擎导入路径）..."
PYTHONIOENCODING=utf-8 "$VENDOR/python.exe" - <<'PY'
import importlib, sys
for mod in ("ssl", "sqlite3", "pywintypes", "uvicorn", "fastapi", "multipart", "agentscope", "dashscope", "openai"):
    importlib.import_module(mod)
import mcp.client.streamable_http as sh
assert hasattr(sh, "streamablehttp_client"), "mcp 版本与 agentscope 不兼容"
print(f"  ✓ 导入通过（{sys.version.split()[0]}，{len(sys.path)} 条搜索路径）")
PY
"$VENDOR/python.exe" -m uvicorn --version >/dev/null && echo "  ✓ uvicorn 可执行"

# 引擎按 Rust bridge 的同一命令行真实启动，/health 探活后才算数
echo "  → 真实启动 engine/server.py 并探活 /health ..."
BOOT_PORT="${BOOT_PORT:-18977}"
mkdir -p "$DL/workdir"
( cd "$DL/workdir" && exec env PYTHONIOENCODING=utf-8 "$VENDOR/python.exe" "$ROOT/engine/server.py" \
    --port "$BOOT_PORT" --work-dir "$DL/workdir" > "$DL/engine-boot.log" 2>&1 ) &
BOOT_PID=$!   # exec 后子 shell 被 python 取代，$! 即解释器 PID，kill 才不会留下孤儿进程
HEALTH=""
for _ in $(seq 1 60); do
  if HEALTH=$(curl -sf --max-time 2 "http://127.0.0.1:$BOOT_PORT/health" 2>/dev/null) && [ -n "$HEALTH" ]; then
    break
  fi
  sleep 0.5
done
kill "$BOOT_PID" 2>/dev/null || true
sleep 1
kill -9 "$BOOT_PID" 2>/dev/null || true
wait "$BOOT_PID" 2>/dev/null || true
if [ -z "$HEALTH" ]; then
  echo "  ✗ 引擎未在 30s 内响应 /health，启动日志末尾：" >&2
  tail -20 "$DL/engine-boot.log" >&2
  exit 1
fi
echo "  ✓ 引擎启动正常：$HEALTH"

# ── [6/6] 结果 ──────────────────────────────────────────────
SIZE=$(du -sm "$VENDOR" | cut -f1)
echo "[6/6] 完成：vendor/python 共 ${SIZE} MB（未压缩）"
echo "-----------------------------------------"
echo "  解释器:   $VENDOR/python.exe"
echo "  依赖:     $SITE"
echo "  下一步:   npx tauri build（resources 已含 vendor/python → 随包 python/）"
echo "  注意:     vendor/ 不入版本库，CI/新机器需先跑本脚本"
echo "-----------------------------------------"
