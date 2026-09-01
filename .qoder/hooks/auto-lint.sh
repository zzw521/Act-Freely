#!/bin/bash
# PostToolUse Hook: 自动 lint 并通过 feedback 直接反馈结果
# 关键：hook stdout 只能输出一行 JSON，所有外部命令的 stdout 必须捕获或吞掉

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

input=$(cat)

# 提前计算路径（在 cd 之前，避免相对路径污染）
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"

# 从 PostToolUse JSON 中提取 file_path（JSON 含多行 tool_response，jq 会失败，改用 sed）
file_path=$(printf '%s' "$input" | sed -n 's/.*"tool_input":{"file_path":"\([^"]*\)".*/\1/p' | head -1)
[ -z "$file_path" ] && exit 0

# 只检查 JS/TS 文件
case "$file_path" in
  *.js|*.ts|*.jsx|*.tsx) ;;
  *) exit 0 ;;
esac

# 定位 web 项目目录（web/b2c-shop, web/b2c-console 等）
project_dir=$(echo "$file_path" | sed -n 's|\(.*web/[^/]*\)/.*|\1|p')
[ -z "$project_dir" ] || [ ! -f "$project_dir/node_modules/.bin/eslint" ] && exit 0

cd "$project_dir" || exit 0

# eslint --fix: stdout/stderr 全部吞掉（避免污染 hook stdout）
ESLINT_USE_FLAT_CONFIG=false NODE_NO_WARNINGS=1 \
  ./node_modules/.bin/eslint --fix "$file_path" >/dev/null 2>&1

# 再次检查剩余问题，用 $() 捕获 stdout
result=$(ESLINT_USE_FLAT_CONFIG=false NODE_NO_WARNINGS=1 \
  ./node_modules/.bin/eslint --no-color "$file_path" 2>/dev/null)
exit_code=$?

if [ $exit_code -ne 0 ] && [ -n "$result" ]; then
  # 通过 feedback 反馈 lint 结果（python3 安全序列化 UTF-8）
  printf '%s' "$result" | python3 -c "
import sys, json
content = sys.stdin.read()
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'feedback': content}}))
"
else
  echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","feedback":"ESLint: all issues auto-fixed."}}'
fi

exit 0
