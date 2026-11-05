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

# 优先项目内 .venv：系统 Python 可能被别的应用占用（如 QwenPaw 钉死 agentscope==1.0.18）。
# 必须前置到 PATH，Rust bridge 是按 PATH 里的 python 启动引擎进程的。
$venvPy = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $env:Path = "$(Join-Path (Get-Location) '.venv\Scripts');$env:Path"
    Write-Host "  ✓ 使用项目内虚拟环境: .venv" -ForegroundColor Green
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

# 账号服务：综合服务端（tongtian-server，默认 8000）；已退役的本地 Auth 服务不再由本脚本拉起
$AuthBase = if ($env:TT_AUTH_SERVER_URL) { $env:TT_AUTH_SERVER_URL.TrimEnd('/') } else { 'http://127.0.0.1:8000' }
if ($env:TT_AUTH_MOCK -eq 'true') {
    Write-Host "  ✓ 账号服务未启用（Mock 登录：TT_AUTH_MOCK=true）" -ForegroundColor DarkYellow
} else {
    $Ready = $false
    try {
        $resp = Invoke-WebRequest -Uri "$AuthBase/health" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        $Ready = ($resp.StatusCode -eq 200)
    } catch {
        $Ready = $false
    }
    if ($Ready) {
        Write-Host "  ✓ 综合账号服务已就绪: $AuthBase" -ForegroundColor Green
    } else {
        Write-Host "  ⚠ 未检测到综合账号服务: $AuthBase" -ForegroundColor DarkYellow
        Write-Host "    请另开终端运行: bash ../tongtian-server/init.sh（默认 127.0.0.1:8000）"
        Write-Host "    离线开发可设 TT_AUTH_MOCK=true 使用模拟登录"
    }
}

Write-Host "-----------------------------------------"
Write-Host "  前端地址:     http://localhost:7173"
if ($env:VITE_MOCK -eq 'false') {
    Write-Host "  运行模式:     真实模式（Mock 已关闭）"
} else {
    Write-Host "  运行模式:     Mock 模式（开发阶段）"
}
if ($env:TT_AUTH_MOCK -eq 'true') {
    Write-Host "  登录模式:     Mock 登录（离线开发，不连接服务端）"
} else {
    Write-Host "  登录模式:     真实网页验证登录（综合服务端）"
}
Write-Host "  账号服务:     $AuthBase"
Write-Host "  综合服务端:   bash ../tongtian-server/init.sh（另开终端，默认 8000）"
Write-Host "  桌面应用:     tongtian-tauri (Tauri 2.x)"
Write-Host "  后端能力:     29 个 Tauri command（含两阶段审阅/模型五件套/文件能力/附件上传/登录态三件套）"
Write-Host "  测试:         npx vitest run（前端）/ cargo test --lib（Rust）/ python ../tongtian-server/tests/test_server.py（服务端）"
Write-Host "-----------------------------------------"

if ($HasRust) {
    npm run tauri dev
} else {
    Write-Host "  提示：安装 Rust (https://rustup.rs) 后可运行完整桌面应用"
    npm run dev
}
