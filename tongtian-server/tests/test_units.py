#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端核心纯逻辑单元测试（标准库实现，无 pytest 依赖）。

覆盖：JWT 签名/过期、PBKDF2 密码、标识归一、ZIP 安全校验、
SKILL.md / plugin.json 元数据解析、限流与增量补列。

用法: python tests/test_units.py
"""

import atexit
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile
import zlib

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-unit-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "unit-test-secret"
os.environ["TT_SERVER_ENV"] = "development"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, packages, security  # noqa: E402
from app.errors import ApiError  # noqa: E402

_passed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def expect_api_error(code: str, fn, status: int = 400):
    try:
        fn()
    except ApiError as exc:
        check(f"{code} 错误码", exc.code == code and exc.status == status,
              f"实际 {exc.status}/{exc.code}")
    else:
        raise AssertionError(f"期望 {code}，实际未抛错")


def make_zip(files: dict[str, bytes | str], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def skill_md(name: str = "weekly-report", version: str = "1.0.0", description: str = "整理周报") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"version: {version}\n"
        "category: 效率\n"
        "tags: [周报, 总结]\n"
        "---\n\n"
        "# 周报生成器\n\n把零散进展整理成结构化周报。\n"
    )


def test_jwt():
    print("\n[JWT]")
    payload = {"sub": "u-1", "type": "access", "exp": int(time.time()) + 60}
    token = security.jwt_encode(payload)
    decoded = security.jwt_decode(token)
    check("签名有效可解码", decoded is not None and decoded["sub"] == "u-1")
    check("篡改签名被拒", security.jwt_decode(token[:-2] + "xx") is None)
    check("过期 token 被拒", security.jwt_decode(
        security.jwt_encode({"sub": "u-1", "exp": int(time.time()) - 1})
    ) is None)
    check("畸形 token 被拒", security.jwt_decode("a.b") is None)
    check("空 token 被拒", security.jwt_decode("") is None)


def test_password():
    print("\n[密码哈希]")
    encoded = security.hash_password("secret123")
    check("哈希格式", encoded.startswith("pbkdf2_sha256$260000$"))
    check("同一密码两次哈希不同（随机盐）", encoded != security.hash_password("secret123"))
    check("正确密码通过", security.verify_password("secret123", encoded))
    check("错误密码被拒", not security.verify_password("secret124", encoded))
    check("损坏哈希被拒", not security.verify_password("secret123", "not-a-hash"))


def test_ids():
    print("\n[标识归一]")
    check("ASCII slug", security.slugify("Weekly Report") == "weekly-report")
    check("下划线保留", security.slugify("data_tool") == "data_tool")
    check("空名 slug 为空", security.slugify("   ") == "")
    check("套件 ASCII 标识", security.suite_id_of("Finance Suite") == "finance-suite")
    chinese = "劳动用工专家套件"
    expected = f"suite-{zlib.crc32(chinese.encode('utf-8')) & 0xFFFFFFFF:08x}"
    check("套件非 ASCII 用 CRC-32", security.suite_id_of(chinese) == expected)
    check("技能非 ASCII 前缀", security.skill_id_of("会议纪要").startswith("skill-"))
    check("中文名含 ASCII 词仍用 CRC", security.skill_id_of("会议纪要 MCP").startswith("skill-"))
    check("MCP ASCII 标识", security.item_id_of("mcp", "GitHub MCP") == "github-mcp")
    mcp_name = "本地文件系统 MCP"
    check(
        "MCP 中文名不再退化为 mcp",
        security.mcp_id_of(mcp_name)
        == f"mcp-{zlib.crc32(mcp_name.encode('utf-8')) & 0xFFFFFFFF:08x}",
    )
    check("版本去 v 前缀", security.normalize_version("v1.2.3") == "1.2.3")
    check("非法版本被拒", security.normalize_version("1.2") is None)
    check("预发布版本保留", security.normalize_version("1.2.3-beta.1") == "1.2.3-beta.1")


def test_safe_paths():
    print("\n[ZIP 安全路径]")
    check("普通相对路径", packages.safe_relative_name("a/b.txt") == "a/b.txt")
    check("去除 ./", packages.safe_relative_name("./a/./b.txt") == "a/b.txt")
    check("反斜杠归一", packages.safe_relative_name("a\\b.txt") == "a/b.txt")
    check("重复斜杠归一", packages.safe_relative_name("a//b.txt") == "a/b.txt")
    for bad in ("../a", "/etc/passwd", "C:\\Windows\\a", "a/../../b", "", "a/\x00b"):
        check(f"拒绝不安全路径 {bad!r}", packages.safe_relative_name(bad) is None)


def test_frontmatter():
    print("\n[SKILL.md frontmatter]")
    text = (
        "---\n"
        "name: weekly\n"
        "description: |\n"
        "  第一行\n"
        "  第二行\n"
        "tags: [a, b]\n"
        "---\n\n"
        "# 正文\n\n内容"
    )
    meta, body = packages.split_frontmatter(text)
    check("解析标量", meta.get("name") == "weekly")
    check("解析块标量", meta.get("description") == "第一行\n第二行")
    check("正文去除分隔符", body.startswith("# 正文"))
    meta2, body2 = packages.split_frontmatter("没有 frontmatter")
    check("无 frontmatter 原样返回", meta2 == {} and body2 == "没有 frontmatter")
    meta3, body3 = packages.split_frontmatter("---\nname: x\n没有结束符")
    check("未闭合 frontmatter 不当元数据", meta3 == {} and body3.startswith("---"))


def test_skill_package():
    print("\n[技能包解析]")
    data = make_zip(
        {
            "SKILL.md": skill_md(),
            "references/checklist.md": "# 检查清单",
        }
    )
    meta = packages.inspect_skill_package(data)
    check("技能名", meta["name"] == "weekly-report")
    check("声明版本", meta["declared_version"] == "1.0.0")
    check("摘要", meta["summary"] == "整理周报")
    check("标签", meta["tags"] == ["周报", "总结"])
    check("文件数", meta["file_count"] == 2)
    check("正文进入 readme", "# 周报生成器" in meta["readme"])

    nested = make_zip({"weekly-report/SKILL.md": "---\ndescription: x\n---\n正文"})
    nested_meta = packages.inspect_skill_package(nested)
    check("单层目录名回退为技能名", nested_meta["name"] == "weekly-report")

    fallback = make_zip({"SKILL.md": "只有正文"})
    check("文件名回退", packages.inspect_skill_package(fallback, "uploaded")["name"] == "uploaded")

    expect_api_error(
        "INVALID_SKILL_PACKAGE",
        lambda: packages.inspect_skill_package(make_zip({"README.md": "x"})),
    )
    expect_api_error(
        "INVALID_SKILL_PACKAGE",
        lambda: packages.inspect_skill_package(
            make_zip({"SKILL.md": skill_md(), "../evil.txt": "x"})
        ),
    )
    expect_api_error(
        "INVALID_SKILL_PACKAGE",
        lambda: packages.inspect_skill_package(b"not a zip at all"),
    )
    expect_api_error(
        "INVALID_SKILL_PACKAGE",
        lambda: packages.inspect_skill_package(make_zip({f"f{i}.txt": "x" for i in range(257)})),
    )

    symlink_buffer = io.BytesIO()
    with zipfile.ZipFile(symlink_buffer, "w") as archive:
        archive.writestr("SKILL.md", skill_md())
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target")
    expect_api_error(
        "INVALID_SKILL_PACKAGE",
        lambda: packages.inspect_skill_package(symlink_buffer.getvalue()),
    )


def test_suite_package():
    print("\n[专家套件包解析]")
    manifest = {
        "name": "Finance Suite",
        "version": "2.0.0",
        "description": "月结检查与差异分析",
        "category": "财务",
        "commands": {"month-close": "按清单完成月结检查"},
        "skills": [{"name": "month-close"}],
        "dataLinks": [{"name": "本地目录"}],
        "tags": ["财务", "月结"],
    }
    data = make_zip(
        {
            "plugin.json": json.dumps(manifest, ensure_ascii=False),
            "README.md": "# Finance Suite\n\n说明",
            "skills/month-close/SKILL.md": "---\nname: month-close\n---\n正文",
        }
    )
    meta = packages.inspect_suite_package(data)
    check("套件名", meta["name"] == "Finance Suite")
    check("套件标识", meta["id"] == "finance-suite")
    check("声明版本", meta["declared_version"] == "2.0.0")
    check("命令解析（对象写法）", meta["commands"][0]["name"] == "month-close")
    check("技能计数", meta["skill_count"] == 1)
    check("数据连接计数", meta["data_link_count"] == 1)
    check("文件清单含 plugin.json", "plugin.json" in meta["manifest"]["files"])
    check("README 解析", "Finance Suite" in meta["readme"])

    nested = make_zip(
        {
            "demo/plugin.json": json.dumps(
                {"name": "劳动用工专家套件", "version": "1.0.0", "description": "x",
                 "commands": ["contract-review"]}, ensure_ascii=False
            ),
            "demo/README.md": "说明",
        }
    )
    nested_meta = packages.inspect_suite_package(nested)
    check("中文套件 CRC 标识", nested_meta["id"] == security.suite_id_of("劳动用工专家套件"))
    check("命令解析（字符串数组）", nested_meta["commands"][0]["name"] == "contract-review")

    loose_desc = make_zip({"plugin.json": json.dumps({"name": "X Suite", "description": 123})})
    loose_meta = packages.inspect_suite_package(loose_desc)
    check("description 非字符串容错", loose_meta["description"] == "")

    expect_api_error(
        "INVALID_SUITE_PACKAGE",
        lambda: packages.inspect_suite_package(make_zip({"README.md": "x"})),
    )
    expect_api_error(
        "INVALID_SUITE_PACKAGE",
        lambda: packages.inspect_suite_package(make_zip({"plugin.json": "{bad json"})),
    )
    expect_api_error(
        "INVALID_SUITE_PACKAGE",
        lambda: packages.inspect_suite_package(make_zip({"plugin.json": json.dumps({"name": " "})})),
    )


def test_rate_limiter():
    print("\n[限流]")
    limiter = security.RateLimiter()
    check("窗口内首次放行", limiter.check("k", 2, 60)[0] is True)
    check("窗口内第二次放行", limiter.check("k", 2, 60)[0] is True)
    allowed, retry = limiter.check("k", 2, 60)
    check("超过上限被拒", allowed is False and retry >= 1)
    limiter.reset("k")
    check("重置后恢复", limiter.check("k", 2, 60)[0] is True)
    check("不同 key 互不影响", limiter.check("k2", 1, 60)[0] is True)


def test_additive_migration():
    print("\n[数据库增量补列]")
    os.makedirs(_TMP, exist_ok=True)
    conn = sqlite3.connect(db.settings.db_path)
    conn.execute(
        "CREATE TABLE stars (uid TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT NOT NULL, "
        "created_at_ms INTEGER NOT NULL, PRIMARY KEY (uid, item_type, item_id))"
    )
    conn.execute("INSERT INTO stars VALUES ('u1', 'skill', 's1', 1)")
    conn.commit()
    conn.close()

    db.init_db()
    with db.tx() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(stars)")}
        row = conn.execute("SELECT active FROM stars WHERE uid = 'u1'").fetchone()
    check("旧表补齐 active 列", "active" in columns)
    check("存量收藏默认激活", row is not None and row["active"] == 1)


def run():
    test_jwt()
    test_password()
    test_ids()
    test_safe_paths()
    test_frontmatter()
    test_skill_package()
    test_suite_package()
    test_rate_limiter()
    test_additive_migration()
    print(f"\n全部通过：{_passed} 项断言")


if __name__ == "__main__":
    run()
