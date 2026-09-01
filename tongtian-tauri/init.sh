#!/bin/bash
set -e

echo "========================================="
echo "  通天办公 (Tauri 版) 初始化启动脚本"
echo "========================================="

# ── 环境检测 ────────────────────────────────────────────────
command -v node >/dev/null 2>&1 || { echo "❌ 未找到 Node.js，请先安装"; exit 1; }

# Rust 可选：缺失时降级为纯前端 Mock 模式
HAS_RUST=0
if command -v cargo >/dev/null 2>&1 || [ -x "$HOME/.cargo/bin/cargo" ]; then
  HAS_RUST=1
  export PATH="$HOME/.cargo/bin:$PATH"
fi

# ── [1/3] 安装依赖 ──────────────────────────────────────────
echo "[1/3] 检查依赖..."
if [ ! -d node_modules ]; then
  npm install
else
  echo "  ✓ node_modules 已存在，跳过安装"
fi

# ── [2/3] 启动服务 ──────────────────────────────────────────
echo "[2/3] 启动服务..."
MOCK_ENABLED="${MOCK_ENABLED:-true}"

if [ "$HAS_RUST" = "1" ]; then
  echo "  ✓ 检测到 Rust 工具链，启动 Tauri 桌面应用"
  if [ "$MOCK_ENABLED" = "true" ]; then
    echo "  ✓ Mock 模式开启（VITE_MOCK 默认启用）"
    npm run tauri dev
  else
    echo "  ⚠ Mock 模式关闭，将调用真实后端 command"
    VITE_MOCK=false npm run tauri dev
  fi
else
  echo "  ⚠ 未检测到 Rust，降级为浏览器 Mock 模式（仅前端自测）"
  echo "  提示：安装 Rust 后可运行完整桌面应用 (https://rustup.rs)"
  npm run dev
fi

# ── [3/3] 访问信息（浏览器降级模式下可见） ─────────────────
echo "[3/3] 启动完成！"
echo "-----------------------------------------"
echo "  前端地址:     http://localhost:7173"
echo "  运行模式:     Mock 模式（VITE_MOCK=false 可关闭）"
echo "  桌面应用:     tongtian-tauri (Tauri 2.x)"
echo "  后端能力:     26 个 Tauri command（含两阶段审阅/模型五件套/文件能力/附件上传）"
echo "  测试:         npx vitest run（前端）/ cargo test --lib（Rust）"
echo "-----------------------------------------"
