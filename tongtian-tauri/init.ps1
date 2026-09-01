# ============================================================
# 通天办公 (Tauri 版) 初始化启动脚本 (PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File init.ps1
#       关闭 Mock: $env:VITE_MOCK='false' 后运行
# ============================================================

$ErrorActionPreference = "Stop"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  通天办公 (Tauri 版) 初始化启动脚本" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# ── 环境检测 ────────────────────────────────────────────────
try { node --version | Out-Null } catch { Write-Host "❌ 未找到 Node.js" -ForegroundColor Red; exit 1 }

$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
if (Test-Path (Join-Path $cargoBin "cargo.exe")) {
    $env:Path = "$cargoBin;$env:Path"
    Write-Host "  ✓ 检测到 Rust: $(cargo --version)" -ForegroundColor Green
    $HasRust = $true
} else {
    Write-Host "  ⚠ 未检测到 Rust，将降级为浏览器 Mock 模式" -ForegroundColor DarkYellow
    $HasRust = $false
}

# ── [1/2] 安装依赖 ──────────────────────────────────────────
Write-Host "[1/2] 检查依赖..." -ForegroundColor Yellow
if (-not (Test-Path "node_modules")) {
    npm install
} else {
    Write-Host "  ✓ node_modules 已存在，跳过安装" -ForegroundColor Green
}

# ── [2/2] 启动服务并输出访问信息 ────────────────────────────
Write-Host "[2/2] 启动服务..." -ForegroundColor Yellow

# 联调模式（TT_AUTH_MOCK=false）：拉起本地 Auth 服务（网页登录/注册页）
$AuthUrl = "未启动（Mock 模式）"
if ($env:TT_AUTH_MOCK -eq 'false') {
    $AuthPort = if ($env:TT_AUTH_PORT) { $env:TT_AUTH_PORT } else { '18080' }
    $AuthUrl = "http://127.0.0.1:$AuthPort"
    Write-Host "  → 启动本地 Auth 服务（网页验证登录）: $AuthUrl" -ForegroundColor Green
    Start-Process python -ArgumentList "auth-server\server.py","--port",$AuthPort -WindowStyle Hidden
}

Write-Host "-----------------------------------------"
Write-Host "  前端地址:     http://localhost:7173"
if ($env:VITE_MOCK -eq 'false') {
    Write-Host "  运行模式:     真实模式（Mock 已关闭）"
} else {
    Write-Host "  运行模式:     Mock 模式（开发阶段）"
}
if ($env:TT_AUTH_MOCK -eq 'false') {
    Write-Host "  登录模式:     网页验证登录（TT_AUTH_MOCK=false）"
} else {
    Write-Host "  登录模式:     Mock 登录（TT_AUTH_MOCK=false 可切网页验证）"
}
Write-Host "  Auth 服务:    $AuthUrl"
Write-Host "  桌面应用:     tongtian-tauri (Tauri 2.x)"
Write-Host "  后端能力:     29 个 Tauri command（含两阶段审阅/模型五件套/文件能力/附件上传/登录态三件套）"
Write-Host "  测试:         npx vitest run（前端）/ cargo test --lib（Rust）/ python auth-server\test_auth.py（Auth）"
Write-Host "-----------------------------------------"

if ($HasRust) {
    npm run tauri dev
} else {
    Write-Host "  提示：安装 Rust (https://rustup.rs) 后可运行完整桌面应用"
    npm run dev
}
