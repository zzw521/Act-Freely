#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prompt 安全网关 — 审计记录 + 敏感信息拦截（合一）

功能：
  1. 审计：记录每次 prompt 提交到 JSONL 日志（按天轮转）
  2. 拦截：检测敏感信息（手机号、身份证、API Key、私钥等），命中则 exit 1 阻止提交

合并为单脚本避免多 hook 管道协调问题（stdin 消费、exit code 覆盖等）
"""

import sys
import os
import json
import re
from datetime import datetime

# Windows 控制台 UTF-8 编码兼容
if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================
#  敏感信息检测规则
# ============================================
SENSITIVE_RULES = [
    {
        "name": "中国手机号",
        "pattern": r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "description": "检测到疑似中国手机号码（11位）",
        "severity": "HIGH",
    },
    {
        "name": "身份证号",
        "pattern": r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
        "description": "检测到疑似中国居民身份证号码（18位）",
        "severity": "CRITICAL",
    },
    {
        "name": "邮箱地址",
        "pattern": r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        "description": "检测到疑似邮箱地址",
        "severity": "MEDIUM",
    },
    {
        "name": "OpenAI API Key",
        "pattern": r"sk-(?:proj-)?[a-zA-Z0-9]{20,}",
        "description": "检测到疑似 OpenAI API Key（sk- 前缀）",
        "severity": "CRITICAL",
    },
    {
        "name": "AWS Access Key",
        "pattern": r"AKIA[0-9A-Z]{16}",
        "description": "检测到疑似 AWS Access Key ID",
        "severity": "CRITICAL",
    },
    {
        "name": "Google API Key",
        "pattern": r"AIza[a-zA-Z0-9_\-]{35}",
        "description": "检测到疑似 Google API Key",
        "severity": "CRITICAL",
    },
    {
        "name": "GitHub Token",
        "pattern": r"gh[pousr]_[A-Za-z0-9_]{36,}",
        "description": "检测到疑似 GitHub Personal Access Token",
        "severity": "CRITICAL",
    },
    {
        "name": "阿里云 AccessKey",
        "pattern": r"LTAI[A-Za-z0-9]{12,}",
        "description": "检测到疑似阿里云 AccessKey ID",
        "severity": "CRITICAL",
    },
    {
        "name": "通用 API Key/Token/Secret",
        "pattern": r"(?:api[_\-]?key|api[_\-]?secret|access[_\-]?token|auth[_\-]?token|secret[_\-]?key|private[_\-]?key)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}['\"]?",
        "description": "检测到疑似 API Key / Token / Secret 赋值",
        "severity": "CRITICAL",
    },
    {
        "name": "私钥块",
        "pattern": r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
        "description": "检测到私钥内容（PEM 格式）",
        "severity": "CRITICAL",
    },
    {
        "name": "密码赋值",
        "pattern": r"(?:password|passwd|pwd|密码)\s*[=:]\s*['\"][^'\"]{3,}['\"]",
        "description": "检测到密码明文赋值",
        "severity": "HIGH",
    },
    {
        "name": "Bearer Token",
        "pattern": r"Bearer\s+[A-Za-z0-9_\-\.]{20,}",
        "description": "检测到疑似 Bearer Token",
        "severity": "HIGH",
    },
    {
        "name": "连接字符串",
        "pattern": r"(?:mysql|mongodb|redis|postgres|jdbc)://[^\s'\"]{10,}",
        "description": "检测到数据库连接字符串",
        "severity": "HIGH",
    },
    # ====== 危险命令检测 ======
    {
        "name": "rm 删除命令",
        "pattern": r"(?<![a-zA-Z])rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+|-[a-zA-Z]*\s+|/s\s+|/q\s+)",
        "description": "检测到 rm 文件删除命令",
        "severity": "CRITICAL",
    },
    {
        "name": "Remove-Item 删除命令",
        "pattern": r"(?<![a-zA-Z])(?:Remove-Item|Rm\s+Item|del\s+|rd\s+/[sq])",
        "description": "检测到 Windows 文件删除命令（Remove-Item / del / rd）",
        "severity": "CRITICAL",
    },
    {
        "name": "DROP TABLE/DATABASE",
        "pattern": r"(?<![a-zA-Z])DROP\s+(?:TABLE|DATABASE|SCHEMA|INDEX|VIEW)\b",
        "description": "检测到 SQL DROP 语句",
        "severity": "CRITICAL",
    },
    {
        "name": "TRUNCATE TABLE",
        "pattern": r"(?<![a-zA-Z])TRUNCATE\s+(?:TABLE\s+)?\w+",
        "description": "检测到 SQL TRUNCATE 语句",
        "severity": "CRITICAL",
    },
    {
        "name": "DELETE FROM",
        "pattern": r"(?<![a-zA-Z])DELETE\s+FROM\b",
        "description": "检测到 SQL DELETE FROM 语句",
        "severity": "HIGH",
    },
    {
        "name": "危险 Git 操作",
        "pattern": r"git\s+(?:push\s+--force|push\s+-f\b|reset\s+--hard|clean\s+-fd)",
        "description": "检测到危险 Git 操作（force push / reset --hard / clean -fd）",
        "severity": "HIGH",
    },
    {
        "name": "磁盘格式化",
        "pattern": r"(?<![a-zA-Z])(?:mkfs\.|format\s+[a-zA-Z]:|diskpart)",
        "description": "检测到磁盘格式化命令",
        "severity": "CRITICAL",
    },
    {
        "name": "sudo 提权",
        "pattern": r"(?<![a-zA-Z])sudo\s+",
        "description": "检测到 sudo 提权命令",
        "severity": "HIGH",
    },
    {
        "name": "chmod 777",
        "pattern": r"chmod\s+777\b",
        "description": "检测到 chmod 777（过度授权）",
        "severity": "HIGH",
    },
]

SENSITIVE_KEYWORDS = [
    {"keyword": "我的apikey是", "severity": "CRITICAL"},
    {"keyword": "我的 api key 是", "severity": "CRITICAL"},
    {"keyword": "这是我的密钥", "severity": "CRITICAL"},
    {"keyword": "我的密码是", "severity": "HIGH"},
    {"keyword": "帮我登录", "severity": "MEDIUM"},
    {"keyword": "我的token", "severity": "HIGH"},
]


# ============================================
#  核心逻辑
# ============================================

def read_input():
    """读取 stdin 输入"""
    try:
        if not sys.stdin.isatty():
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def parse_prompt(raw_input):
    """从 stdin 输入中解析 prompt 文本"""
    prompt_text = raw_input
    try:
        data = json.loads(raw_input)
        prompt_text = data.get("prompt", data.get("text", data.get("content", raw_input)))
    except (json.JSONDecodeError, TypeError):
        pass
    return prompt_text


def detect_sensitive(text):
    """扫描文本中的敏感信息，返回命中规则列表"""
    hits = []
    for rule in SENSITIVE_RULES:
        try:
            match = re.search(rule["pattern"], text, re.IGNORECASE)
            if match:
                hits.append({
                    "type": rule["name"],
                    "severity": rule["severity"],
                    "description": rule["description"],
                })
        except re.error:
            continue

    text_lower = text.lower()
    for kw in SENSITIVE_KEYWORDS:
        if kw["keyword"].lower() in text_lower:
            hits.append({
                "type": f"敏感关键词: {kw['keyword']}",
                "severity": kw["severity"],
                "description": f"检测到敏感关键词「{kw['keyword']}」",
            })
    return hits


def write_audit_log(prompt_text):
    """写入审计日志（JSONL 按天轮转）"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # 向上查找 .qoder 目录定位项目根
    log_dir = os.path.join(script_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"prompt-audit-{today}.jsonl")

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
        "prompt_length": len(prompt_text),
        "prompt_preview": prompt_text[:200],
        "prompt_full": prompt_text,
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main():
    raw_input = read_input()
    prompt_text = parse_prompt(raw_input)

    # ① 审计：始终记录（无论是否拦截）
    write_audit_log(prompt_text)

    if not prompt_text or not prompt_text.strip():
        print(json.dumps({"decision": "allow"}, ensure_ascii=False))
        sys.exit(0)

    # ② 拦截：检测敏感信息
    hits = detect_sensitive(prompt_text)

    if hits:
        # 命中 → 拦截
        hit_summaries = [f"  [{h['severity']}] {h['description']}" for h in hits]
        reason = (
            f"检测到 {len(hits)} 项敏感信息，已拦截提交：\n"
            + "\n".join(hit_summaries)
            + "\n\n请移除敏感信息后重新提交。"
            + "如确需包含此类信息，请使用环境变量或配置文件管理。"
        )

        result = {
            "decision": "deny",
            "reason": reason,
            "hits": [{"type": h["type"], "severity": h["severity"]} for h in hits],
            "audited": True,
        }
        print(json.dumps(result, ensure_ascii=False))
        sys.stderr.write(reason + "\n")
        sys.exit(1)
    else:
        # 无命中 → 放行
        print(json.dumps({"decision": "allow", "audited": True}, ensure_ascii=False))
        sys.exit(0)


if __name__ == "__main__":
    main()
