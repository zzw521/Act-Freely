#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prompt 审计日志记录器

功能：
  - 记录每次用户提交的 prompt 到 JSONL 日志文件
  - 自动按天轮转日志（prompt-audit-YYYY-MM-DD.jsonl）
  - 记录时间戳、prompt 内容、长度等信息
  - 不影响提交流程（始终 exit 0）

日志格式：JSONL（每行一个 JSON 对象）
"""

import sys
import os
import json
from datetime import datetime

# Windows 控制台 UTF-8 编码兼容
if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_project_root():
    """从 hooks 目录向上查找项目根（包含 .qoder 目录）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    current = script_dir
    for _ in range(5):
        if os.path.isdir(os.path.join(current, ".qoder")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    # fallback: 使用脚本所在目录的上两级
    return os.path.dirname(os.path.dirname(script_dir))


def read_input():
    """读取 stdin 输入（hook 传入的 prompt 内容）"""
    try:
        if not sys.stdin.isatty():
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def main():
    # 读取 prompt 内容
    raw_input = read_input()

    # 尝试解析为 JSON（hook 可能以 JSON 格式传递）
    prompt_text = raw_input
    try:
        data = json.loads(raw_input)
        prompt_text = data.get("prompt", data.get("text", data.get("content", raw_input)))
    except (json.JSONDecodeError, TypeError):
        pass

    # 确定日志目录
    project_root = get_project_root()
    log_dir = os.path.join(project_root, ".qoder", "hooks", "logs")
    os.makedirs(log_dir, exist_ok=True)

    # 按天轮转日志文件
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"prompt-audit-{today}.jsonl")

    # 构造审计记录
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
        "prompt_length": len(prompt_text),
        "prompt_preview": prompt_text[:200],  # 仅记录前200字符作为预览
        "prompt_full": prompt_text,
    }

    # 写入日志
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 审计 hook 始终放行
    print(json.dumps({"decision": "allow"}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
