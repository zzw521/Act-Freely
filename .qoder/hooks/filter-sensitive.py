#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prompt 敏感信息拦截过滤器

功能：
  - 检测用户 prompt 中的敏感信息并拦截提交
  - 覆盖：手机号、身份证号、API Key/Token、邮箱、私钥、密码等
  - 拦截时返回明确的错误提示，告知命中类型

检测规则：
  1. 中国手机号：1[3-9]开头的11位数字
  2. 中国身份证号：18位（含X）
  3. 邮箱地址：xxx@xxx.xxx
  4. API Key/Secret/Token：sk-xxx、AKIA、AIza 等常见前缀
  5. 私钥块：-----BEGIN PRIVATE KEY-----
  6. 密码/密钥赋值：password="xxx"、secret=xxx
  7. 敏感关键词：API密钥、私钥、密码等
"""

import sys
import os
import json
import re

# Windows 控制台 UTF-8 编码兼容
if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================
#  敏感信息检测规则定义
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
]

# 敏感关键词（出现在 prompt 中即拦截）
SENSITIVE_KEYWORDS = [
    {"keyword": "我的apikey是", "severity": "CRITICAL"},
    {"keyword": "我的 api key 是", "severity": "CRITICAL"},
    {"keyword": "这是我的密钥", "severity": "CRITICAL"},
    {"keyword": "我的密码是", "severity": "HIGH"},
    {"keyword": "帮我登录", "severity": "MEDIUM"},
    {"keyword": "我的token", "severity": "HIGH"},
]


def read_input():
    """读取 stdin 输入"""
    try:
        if not sys.stdin.isatty():
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def detect_sensitive(text):
    """
    扫描文本中的敏感信息
    返回命中的规则列表
    """
    hits = []

    # 正则规则检测
    for rule in SENSITIVE_RULES:
        try:
            match = re.search(rule["pattern"], text, re.IGNORECASE)
            if match:
                hits.append({
                    "type": rule["name"],
                    "severity": rule["severity"],
                    "description": rule["description"],
                    "matched_snippet": match.group()[:20] + "***" if len(match.group()) > 20 else "***",
                })
        except re.error:
            continue

    # 关键词检测
    text_lower = text.lower()
    for kw in SENSITIVE_KEYWORDS:
        if kw["keyword"].lower() in text_lower:
            hits.append({
                "type": f"敏感关键词: {kw['keyword']}",
                "severity": kw["severity"],
                "description": f"检测到敏感关键词「{kw['keyword']}」",
                "matched_snippet": "***",
            })

    return hits


def main():
    raw_input = read_input()

    # 解析 prompt 文本
    prompt_text = raw_input
    try:
        data = json.loads(raw_input)
        prompt_text = data.get("prompt", data.get("text", data.get("content", raw_input)))
    except (json.JSONDecodeError, TypeError):
        pass

    if not prompt_text or not prompt_text.strip():
        # 空 prompt 直接放行
        print(json.dumps({"decision": "allow"}, ensure_ascii=False))
        sys.exit(0)

    # 执行敏感信息检测
    hits = detect_sensitive(prompt_text)

    if hits:
        # 有命中 → 拦截
        # 构造拦截反馈
        hit_summaries = []
        for h in hits:
            hit_summaries.append(f"  [{h['severity']}] {h['description']}")

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
        }
        print(json.dumps(result, ensure_ascii=False))
        sys.stderr.write(reason + "\n")
        sys.exit(1)
    else:
        # 无命中 → 放行
        print(json.dumps({"decision": "allow"}, ensure_ascii=False))
        sys.exit(0)


if __name__ == "__main__":
    main()
