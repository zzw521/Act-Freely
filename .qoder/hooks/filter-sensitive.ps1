# ============================================
#  Prompt 敏感词拦截 Hook (PowerShell)
#  职责：检测 prompt 中的敏感信息并拦截提交
#  检测项：手机号、身份证号、API Key/Token、邮箱、私钥、密码等
#  触发时机：UserPromptSubmit
#  拦截行为：exit 1 阻止提交，exit 0 放行
# ============================================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& python "$ScriptDir\filter-sensitive.py"
exit $LASTEXITCODE
