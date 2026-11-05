#!/usr/bin/env bash
# W4 合并轮复跑：逐个引擎自测只数 ^[PASS]，结果写文件再读（回显不可信）。
set -u
cd "$(dirname "$0")/.." || exit 1
PY=./vendor/python/python.exe
LOG=../docs/并行实施/_w4-logs/recount.txt
mkdir -p "$(dirname "$LOG")"
: > "$LOG"
FAMILIES=(
  engine_extension_sandbox_selftest
  engine_tool_grants_selftest
  engine_artifact_history_selftest
  engine_artifact_history_wiring_selftest
  engine_runtime_selftest
  engine_route_lanes_selftest
  engine_document_tools_selftest
  engine_office_tools_selftest
  engine_sandbox_selftest
  engine_checkpoint_selftest
  engine_task_cancel_selftest
  engine_privacy_approval_selftest
  engine_hooks_selftest
  engine_skill_registry_selftest
  engine_knowledge_hybrid_selftest
  engine_knowledge_selftest
  engine_experts_selftest
)
for f in "${FAMILIES[@]}"; do
  out=/tmp/w4-one.txt
  "$PY" -X utf8 "tests/integration/$f.py" > "$out" 2>&1
  rc=$?
  pass=$(grep -c '^\[PASS\]' "$out")
  fail=$(grep -c '^\[FAIL\]' "$out")
  echo "$f pass=$pass fail=$fail rc=$rc" >> "$LOG"
done
for f in $(ls tests/integration/engine_ticket_*.py); do
  out=/tmp/w4-ticket.txt
  base=$(basename "$f")
  "$PY" -X utf8 "$f" > "$out" 2>&1
  rc=$?
  pass=$(grep -c '^\[PASS\]' "$out")
  fail=$(grep -c '^\[FAIL\]' "$out")
  echo "$base pass=$pass fail=$fail rc=$rc" >> "$LOG"
done
echo DONE >> "$LOG"
