"""可选的演示数据（明确标注为示例，幂等：已存在的条目不会覆盖）。"""

from __future__ import annotations

import io
import os
import secrets
import sqlite3
import sys
import zipfile

from . import market
from .db import now_ms, tx
from .errors import ApiError
from .security import hash_password, new_uid

DEMO_EMAIL = "demo@tongtian.local"
DEMO_NICKNAME = "通天演示发布者"


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _skill_package(
    name: str, description: str, version: str, category: str, tags: list[str], body: str
) -> bytes:
    frontmatter = "\n".join(
        [
            "---",
            f"name: {name}",
            f"description: {description}",
            f"version: {version}",
            f"category: {category}",
            f"tags: [{', '.join(tags)}]",
            "---",
            "",
            body,
            "",
        ]
    )
    return _zip(
        {
            "SKILL.md": frontmatter,
            "references/checklist.md": "# 检查清单\n\n- 收集输入材料\n- 核对关键数据\n- 输出结论与待办\n",
        }
    )


def _suite_package(
    name: str,
    version: str,
    description: str,
    author: str,
    category: str,
    commands: list[dict[str, str]],
    skill_names: list[str],
    readme: str,
) -> bytes:
    import json

    files = {
        f"demo-suite/plugin.json": json.dumps(
            {
                "name": name,
                "version": version,
                "description": description,
                "author": author,
                "category": category,
                "commands": commands,
                "skills": [{"name": item} for item in skill_names],
                "dataLinks": [{"name": "本地工作目录"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "demo-suite/README.md": readme,
    }
    for index, skill in enumerate(skill_names):
        files[f"demo-suite/skills/{index + 1}-{skill}/SKILL.md"] = (
            f"---\nname: {skill}\ndescription: {name} 的配套技能\n---\n\n"
            f"# {skill}\n\n根据套件指令的输入完成对应工作，并给出可执行的下一步。\n"
        )
    return _zip(files)


def _ensure_demo_user() -> tuple[sqlite3.Row, str | None]:
    password = os.environ.get("TT_DEMO_PASSWORD") or secrets.token_urlsafe(9)
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?1", (DEMO_EMAIL,)).fetchone()
        if row is not None:
            return row, None
        uid = new_uid()
        conn.execute(
            "INSERT INTO users (uid, email, password_hash, nickname, role, plan, created_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, 'user', '个人免费版', ?5)",
            (uid, DEMO_EMAIL, hash_password(password), DEMO_NICKNAME, now_ms()),
        )
        row = conn.execute("SELECT * FROM users WHERE uid = ?1", (uid,)).fetchone()
    return row, password


def _exists(item_type: str, name: str) -> bool:
    with tx() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM items WHERE item_type = ?1 AND name = ?2", (item_type, name)
            ).fetchone()
            is not None
        )


def seed_demo() -> None:
    user_row, password = _ensure_demo_user()
    user = dict(user_row)
    created = 0

    skills = [
        (
            "weekly-report",
            "把一周的零散进展整理成结构清晰、可直接发送的周报",
            "1.0.0",
            "效率",
            ["周报", "总结"],
            "# 周报生成器\n\n## 使用方式\n\n提供本周完成事项、风险与下周计划，按「结果 / 数据 / 风险 / 待办」四段输出。\n\n## 输出要求\n\n- 先给 3 条结论\n- 只保留可验证的数据\n- 待办写明责任人与时间\n",
        ),
        (
            "meeting-minutes",
            "把会议录音转写整理成决议、责任人与截止时间的纪要",
            "1.0.0",
            "文档",
            ["会议", "纪要"],
            "# 会议纪要整理\n\n## 步骤\n\n1. 识别参会人与议题\n2. 抽取决议与分歧\n3. 形成责任到人的行动项\n\n对没有结论的讨论明确标记为「待定」，不要替参会人编造决定。\n",
        ),
    ]
    for name, summary, version, category, tags, body in skills:
        if _exists("skill", name):
            continue
        market.publish_package(
            user,
            "skill",
            _skill_package(name, summary, version, category, tags, body),
            f"{name}.zip",
            summary=summary,
            category=category,
            tags=", ".join(tags),
            version=version,
            changelog="首次发布",
        )
        created += 1

    suites = [
        (
            "劳动用工专家套件",
            "1.2.0",
            "覆盖劳动合同审查、试用期风险与劳动争议应对的岗位工作流包。",
            "@通天演示",
            "律师",
            [
                {"name": "contract-review", "description": "审查劳动合同的关键条款与风险点"},
                {"name": "probation-risk", "description": "评估试用期约定与解除风险"},
            ],
            ["contract-review", "probation-risk"],
            "# 劳动用工专家套件\n\n本套件为演示数据，展示如何把岗位 SOP 固化为可调用的套件指令。\n\n## 使用方式\n\n在对话框中输入 `/contract-review` 等指令，按提示粘贴合同文本即可。\n",
        ),
        (
            "财务月结套件",
            "1.0.0",
            "月结检查清单、差异分析与结账说明生成，面向中小企业财务岗。",
            "@通天演示",
            "财务",
            [
                {"name": "month-close", "description": "按清单完成月结检查"},
                {"name": "variance-analysis", "description": "生成预算与实际差异分析"},
            ],
            ["month-close", "variance-analysis"],
            "# 财务月结套件\n\n演示套件：包含月结检查与差异分析两条指令。\n\n> 所有金额与结论需由财务人员复核，本套件不替代专业判断。\n",
        ),
    ]
    for name, version, description, author, category, commands, skill_names, readme in suites:
        if _exists("suite", name):
            continue
        package = _suite_package(
            name, version, description, author, category, commands, skill_names, readme
        )
        market.publish_package(
            user,
            "suite",
            package,
            f"{name}.zip",
            summary=description,
            category=category,
            version=version,
            changelog="首次发布",
        )
        created += 1

    mcp_items = [
        market.McpCreateIn(
            name="本地文件系统 MCP",
            version="1.0.0",
            summary="通过 MCP 协议访问指定目录下的文件，适合本地资料整理。",
            description="官方 filesystem 服务的演示登记项；命令通过 npx 启动，仅能访问启动参数指定的目录。",
            category="办公",
            tags=["文件", "本地"],
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "."],
            timeoutSecs=30,
            readme="# 本地文件系统 MCP\n\n演示配置：把 `.` 替换为需要授权的目录。stdio 插件会在本机执行命令，请只登记可信来源。\n",
        ),
        market.McpCreateIn(
            name="GitHub MCP",
            version="1.0.0",
            summary="读取仓库 Issue 与 PR 信息，需要配置个人访问令牌。",
            description="演示数据：客户端登记后需填写 GITHUB_PERSONAL_ACCESS_TOKEN，令牌不会存放在市场服务端。",
            category="开发",
            tags=["GitHub", "仓库"],
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            envKeys=[
                market.KeyHintIn(
                    key="GITHUB_PERSONAL_ACCESS_TOKEN",
                    description="GitHub 个人访问令牌（只读权限即可）",
                    required=True,
                    sensitive=True,
                )
            ],
            timeoutSecs=30,
            readme="# GitHub MCP\n\n演示配置：市场只分发配置与所需环境变量名，不接触也不保存令牌值。\n",
        ),
        market.McpCreateIn(
            name="示例：远程搜索 MCP",
            version="1.0.0",
            summary="演示远程 streamable_http 连接器的登记方式（地址为占位符）。",
            description="演示数据：请发布者替换为真实可用的 MCP 地址，并在客户端填写鉴权请求头。",
            category="搜索",
            tags=["远程", "示例"],
            transport="streamable_http",
            url="https://mcp.example.com/mcp",
            headerKeys=[
                market.KeyHintIn(
                    key="Authorization",
                    description="Bearer <your-token>（由客户端填写，服务端不保存）",
                    required=True,
                    sensitive=True,
                )
            ],
            timeoutSecs=30,
            readme="# 远程搜索 MCP\n\n演示配置：远程连接器的请求头值由客户端填写，市场只登记键名与说明。\n",
        ),
    ]
    for payload in mcp_items:
        if _exists("mcp", payload.name):
            continue
        try:
            market.publish_mcp(user, payload)
        except ApiError as exc:
            if exc.status != 409:
                raise
            continue
        created += 1

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"演示数据：本次新增 {created} 个条目（已存在的条目保持不变）")
    print(f"演示发布账号：{DEMO_EMAIL}")
    if password:
        print(f"演示账号密码：{password}（仅本次初始化显示，请妥善保存）")
    else:
        print("演示账号已存在：密码沿用原值，可用 TT_DEMO_PASSWORD 初始化全新数据目录")


if __name__ == "__main__":
    from .db import init_db

    init_db()
    seed_demo()
