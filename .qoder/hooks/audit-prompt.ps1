# ============================================
#  Prompt 审计日志 Hook (PowerShell)
#  职责：记录所有用户提交的 prompt 信息，用于安全审计与回溯
#  触发时机：UserPromptSubmit
#  日志位置：.qoder/hooks/logs/prompt-audit-YYYY-MM-DD.jsonl
# ============================================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& python "$ScriptDir\audit-prompt.py"
