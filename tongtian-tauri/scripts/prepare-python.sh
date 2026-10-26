#!/bin/bash
# 准备随包 Python 运行时（发布方案 B：embeddable 解释器 + pip --target 锁定依赖）
#
# 注意：体积/清理统计不要用 MSYS 的 du —— 在 Windows 上对上万文件的 lark_oapi 目录
# 逐文件 stat 能卡住十几分钟（实测 [6/6] 前的 du -sm 卡了 7 分钟以上），改用随包
# 解释器原生 os.walk。
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
DRIFT_LIST="$DL/lock-drift.txt"

# 标记文件只证明「按这把锁装过一次」，不证明「装对了」。pip 的 --target 安装看到同名
# dist-info 就认为需求已满足，于是 site-packages 里可以同时留着 websockets 16.0 的代码和
# 15.0.1 的元数据（实测踩过：lark-oapi 把锁里的 websockets 降到 15.0.1，磁盘上的 16.0 文件
# 却没被覆盖，`import websockets` 拿到的是 16.0，而随包锁写着 15.0.1）。
# 所以「跳过安装」之前必须按锁逐条核对磁盘：每个包只允许一份 dist-info，且版本等于锁定值。
# PYTHONIOENCODING 必给：Windows 控制台默认 GBK，而这里的判定结果带 ⚠/✓，
# 编码一崩 python 就以非零退出，bash 只会看到「不一致」却不知道为什么不一致。
check_lock_on_disk() {
  : > "$DRIFT_LIST"
  PYTHONIOENCODING=utf-8 TT_LOCK="$LOCK" TT_SITE="$SITE" TT_DRIFT="$DRIFT_LIST" "$VENDOR/python.exe" - <<'PY'
import os, pathlib, re

lock = pathlib.Path(os.environ["TT_LOCK"])
site = pathlib.Path(os.environ["TT_SITE"])
drift = pathlib.Path(os.environ.get("TT_DRIFT", "")) if os.environ.get("TT_DRIFT") else None

def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

want = {}
for raw in lock.read_text(encoding="utf-8").splitlines():
    line = raw.split("#")[0].strip()
    if not line or line.startswith("-") or "==" not in line:
        continue
    name, version = line.split("==", 1)
    want[norm(name)] = version.strip()

have: dict[str, list[str]] = {}
for info in site.glob("*.dist-info"):
    parts = info.name[: -len(".dist-info")].rsplit("-", 1)
    if len(parts) != 2:
        continue
    have.setdefault(norm(parts[0]), []).append(parts[1])

bad = []
for name, version in sorted(want.items()):
    got = sorted(have.get(name, []))
    if got != [version]:
        bad.append(f"{name}:{'/'.join(got) or '缺失'}!={version}")

if bad:
    print("  ⚠ 磁盘与锁不一致：" + "；".join(bad[:6]) + ("…" if len(bad) > 6 else ""))
    if drift is not None:
        drift.write_text("\n".join(n for n in (b.split(":")[0] for b in bad)), encoding="utf-8")
    raise SystemExit(1)
print(f"  ✓ 磁盘与锁逐条一致（{len(want)} 个包）")
PY
}

# 只删「磁盘版本与锁不一致」的那几个包：按它们自己的 RECORD 删文件，任何越出 site-packages
# 的路径一律拒绝，剩下的交给 pip 重装。
purge_drifted() {
  [ -s "$DRIFT_LIST" ] || return 0
  PYTHONIOENCODING=utf-8 TT_DRIFT="$DRIFT_LIST" TT_SITE="$SITE" "$VENDOR/python.exe" - <<'PY'
import csv, os, pathlib, re, shutil

def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

site = pathlib.Path(os.environ["TT_SITE"]).resolve()
targets = {norm(line.strip()) for line in pathlib.Path(os.environ["TT_DRIFT"]).read_text(encoding="utf-8").splitlines() if line.strip()}
removed = 0
for info in list(site.glob("*.dist-info")):
    stem = info.name[: -len(".dist-info")]
    if "-" not in stem:
        continue
    head, _ = stem.rsplit("-", 1)
    if norm(head) not in targets:
        continue
    record = info / "RECORD"
    if record.exists():
        for row in csv.reader(record.read_text(encoding="utf-8", errors="replace").splitlines()):
            if not row:
                continue
            rel = row[0].replace("\\", "/")
            if not rel or rel.startswith("../") or rel.startswith("/") or ".." in rel.split("/"):
                continue
            victim = site / rel
            if victim.is_file():
                victim.unlink(missing_ok=True)
            elif victim.is_dir():
                shutil.rmtree(victim, ignore_errors=True)
    shutil.rmtree(info, ignore_errors=True)
    removed += 1

# RECORD 只删文件，目录会留下来（连 dist-info 都被删掉时连包名都无从得知）；
# 而 pip --target 看到同名目录存在就整包跳过（"Target directory ... already exists"），
# 所以把整个 site-packages 下的空壳目录清掉——空目录不承载任何模块，删除是安全的。
for root, _dirs, _files in os.walk(site, topdown=False):
    if pathlib.Path(root) == site:
        continue
    try:
        os.rmdir(root)
    except OSError:
        pass
print(f"  已按 RECORD 撤掉 {removed} 份与锁不一致的旧安装记录")
PY
}

NEED_INSTALL=1
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$LOCK_HASH" ]; then
  if check_lock_on_disk; then
    NEED_INSTALL=0
  else
    echo "  → 标记说装过了，磁盘却不是锁写的那份：撤掉旧记录后重装"
    purge_drifted
  fi
fi

if [ "$NEED_INSTALL" = "0" ]; then
  echo "[2/6] 依赖已与锁文件一致（逐条核对过磁盘），无需引导 pip"
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
  # 锁文件是完整传递闭包，不需要 pip 再解析依赖：不加 --no-deps 时，哪怕只缺一个包，
  # pip 也会为解析把 101 个 wheel 全部重新下载（实测 10 分钟仍未下载完）。
  "$VENDOR/python.exe" -m pip install --no-cache-dir --disable-pip-version-check --no-deps \
    --target "$SITE" -r "$LOCK" -i "$PIP_INDEX" --trusted-host "$(echo "$PIP_INDEX" | sed -E 's#https?://([^/]+).*#\1#')"
  echo "$LOCK_HASH" > "$MARKER"
fi

# ── [4/6] 精简 + 补齐（让随包运行时自给自足） ────────────────
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

# pywin32 把 pywintypes312.dll / pythoncom312.dll 放在 pywin32_system32/，指望安装后由
# pywin32.pth 里的 `import pywin32_bootstrap` 去 os.add_dll_directory 注册——而 embeddable 的
# ._pth 不执行 site.main()，这个 .pth 永远不会生效。后果：win32api / win32job / win32file 这类
# 扩展模块在没有“先导入 pywintypes 预热”的进程里直接 DLL load failed（mcp/os/win32/utilities.py
# 恰好按 pywintypes → win32api 的顺序导入才侥幸不报错，是顺序依赖的隐雷）。
# 把两个 DLL 复制到扩展模块所在目录 win32/，Windows 加载器天然会搜该目录，代价约 800KB。
patch_portable_pywin32() {
  local src="$SITE/pywin32_system32" dst="$SITE/win32" copied=0
  [ -d "$src" ] || return 0
  for dll in "$src"/*.dll; do
    [ -e "$dll" ] || continue
    cp -f "$dll" "$dst/" && copied=$((copied + 1))
  done
  [ "$copied" -gt 0 ] || { echo "  ✗ 未找到 pywin32_system32 下的 DLL，随包运行时可能缺件" >&2; exit 1; }
  echo "  补齐 pywin32 DLL ${copied} 个 → win32/（绕开不执行的 pywin32.pth）"
}
echo "[4/6] 剔除随包运行时用不到的内容，并补齐 pywin32 DLL..."
prune_dead_weight
patch_portable_pywin32
echo "[5/6] 冒烟测试（真实引擎导入路径）..."
( cd "$ROOT/engine" && PYTHONIOENCODING=utf-8 "$VENDOR/python.exe" - ) <<'PY'
import importlib, sys, time
# 冒烟测试在 engine/ 目录里跑，通道模块才 import 得起来（Windows 解释器读不懂 POSIX 路径，
# 所以只能用 cwd + 相对路径，不能把 $ROOT 拼进源码字符串）
sys.path.insert(0, ".")
# win32api 必须排在 pywintypes 之前：这个“冷启动”顺序正是修复前 DLL load failed 的场景，
# 谁把它挪到 pywintypes 后面，这一步就会连同顺序隐雷一起失效。
for mod in ("ssl", "sqlite3", "win32api", "win32job", "win32file", "pywintypes",
            "uvicorn", "fastapi", "multipart", "agentscope", "dashscope", "openai",
            "dingtalk_stream", "aibot"):
    importlib.import_module(mod)
import mcp.client.streamable_http as sh
assert hasattr(sh, "streamablehttp_client"), "mcp 版本与 agentscope 不兼容"
# 钉钉与企业微信通道的协议都来自随包的官方 SDK：缺任一个依赖，通道会如实退回「尚未接入」，
# 界面就成了「看得到输入框但永远连不上」——打包前先把这个可能性排除掉
import slack_channel, dingtalk_channel, wecom_channel
# 飞书的 lark-oapi 是整包发布（20MB/上万文件，根 __init__ 把全部 API 模块都拉进来说话），
# 引擎启动不陪它等，只有绑定/启动连接那一次才加载。所以这里分成两段量：先证明注册飞书通道
# 这件事在启动侧几乎是免费的，再证明随包运行时真的加载得动它——两段缺一都算随包件不合格。
boot_at = time.perf_counter()
import feishu_channel
boot_cost = time.perf_counter() - boot_at
assert "lark_oapi" not in sys.modules, "引擎启动阶段就把官方飞书 SDK 拉进来了（「绑定时才加载」的契约破了）"
assert feishu_channel.SDK_PRESENT is True, "随包运行时缺 lark_oapi，连 find_spec 都没探到"
lark_at = time.perf_counter()
importlib.import_module("lark_oapi")
lark_cost = time.perf_counter() - lark_at
missing = [c.kind for c in (dingtalk_channel.DingTalkStreamChannel, wecom_channel.WeComAibotChannel,
                            feishu_channel.FeishuChannel) if not c.supported]
assert not missing, f"随包运行时缺 IM 协议依赖，通道退回未接入：{missing}"
print(f"  ✓ 导入通过（{sys.version.split()[0]}，{len(sys.path)} 条搜索路径，IM 通道协议齐全）")
print(f"    飞书：启动侧注册 {boot_cost:.2f}s 且不加载 SDK；整包 import {lark_cost:.1f}s（绑定/启动连接时才付这一次）")
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
SIZE=$("$VENDOR/python.exe" - "$VENDOR" <<'PY'
import os, sys
total = 0
for root, _dirs, files in os.walk(sys.argv[1]):
    for name in files:
        try:
            total += os.path.getsize(os.path.join(root, name))
        except OSError:
            pass
print(total // 1048576)
PY
)
echo "[6/6] 完成：vendor/python 共 ${SIZE} MB（未压缩）"
echo "-----------------------------------------"
echo "  解释器:   $VENDOR/python.exe"
echo "  依赖:     $SITE"
echo "  下一步:   npx tauri build（resources 已含 vendor/python → 随包 python/）"
echo "  注意:     vendor/ 不入版本库，CI/新机器需先跑本脚本"
echo "-----------------------------------------"
