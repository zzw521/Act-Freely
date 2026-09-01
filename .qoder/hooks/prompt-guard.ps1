# ============================================
#  Prompt 安全网关 (PowerShell)
#  职责：审计记录 + 敏感信息拦截（合一脚本）
#  触发时机：UserPromptSubmit
# ============================================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& python "$ScriptDir\prompt-guard.py"
exit $LASTEXITCODE
