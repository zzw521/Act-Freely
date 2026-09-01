# tongtian-tauri dev launcher (adds cargo to PATH)
#
# 引擎模式开关（环境变量）:
#   TT_AGENT_MOCK=1        → Mock Agent（纯模拟事件，无需模型配置）
#   TT_ENGINE=qwenpaw      → 真实 qwenpaw（需已安装）
#   默认                   → AgentScope 自研引擎（需项目根 .env 配置 API Key）
if (-not $env:TT_AGENT_MOCK) { $env:TT_AGENT_MOCK = '0' }
if (-not $env:TT_ENGINE) { $env:TT_ENGINE = 'agentscope' }
Write-Host "TT_AGENT_MOCK = $env:TT_AGENT_MOCK | TT_ENGINE = $env:TT_ENGINE"
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
Set-Location 'd:\qoder\tontianwork\tongtian-tauri'
npx tauri dev
