"""可选的演示数据（明确标注为示例，幂等：已存在的条目不会覆盖）。

演示内容按分类铺满市场，方便开发环境直接看到有内容的市场：

- 技能：覆盖 ``catalog.TYPE_SPECS["skill"]`` 的全部 7 个分类（效率 / 文档 / 数据 /
  开发 / 运营 / 内容 / 其他）；
- 专家套件：覆盖 13 个分类，每个分类至少 1 个；
- MCP 连接器：覆盖 6 个分类，配置均取自真实存在的 npm 包或可访问的远程端点。

技能正文一律放在 ``app/demo_skills/<slug>/`` 目录下，发布时原样打包（``SKILL.md`` 位于
ZIP 根目录）：

- ``vendored=True`` 的条目取自公开技能库 `skills.sh <https://skills.sh/>`_ 上的开源
  仓库（MIT 许可），``SKILL.md`` 保持上游原样未作改写，目录内随包 ``LICENSE.txt`` 与
  ``SOURCE.txt`` 记录仓库、上游路径、提交号与获取时间；缺任何一份都会拒绝启动。
  市场卡片上的中文摘要、分类与标签由本模块的发布参数提供，不属于上游内容。
- ``weekly-report``、``meeting-minutes`` 为通天办公原创演示内容，无随包许可声明。
"""

from __future__ import annotations

import io
import os
import secrets
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import market
from .db import now_ms, tx
from .errors import ApiError
from .security import hash_password, new_uid

DEMO_EMAIL = "demo@tongtian.local"
DEMO_NICKNAME = "通天云"
# 早期演示库里的发布者叫「通天演示发布者」；已存在的演示账号只在仍是该旧名时改名，
# 用户手动改过的昵称不动。
LEGACY_DEMO_NICKNAME = "通天演示发布者"

DEMO_SKILLS_DIR = Path(__file__).resolve().parent / "demo_skills"

_SOURCE_NOTE = (
    "\n\n来源：公开技能库 skills.sh 上的开源仓库（MIT 或 Apache-2.0，正文未改写），"
    "完整出处见包内 SOURCE.txt 与 LICENSE.txt；中文摘要与分类为通天办公整理，仅供演示。"
)


# ── 打包辅助 ─────────────────────────────────────────────────


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _zip_dir(root: Path) -> bytes:
    """把演示技能目录原样打包，条目路径相对该目录（SKILL.md 位于根目录）。"""
    if not root.is_dir():
        raise RuntimeError(f"演示技能目录缺失：{root}")
    if not (root / "SKILL.md").is_file():
        raise RuntimeError(f"演示技能 {root.name} 缺少 SKILL.md")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            archive.writestr(path.relative_to(root).as_posix(), path.read_bytes())
    return buffer.getvalue()


def _suite_package(
    name: str,
    version: str,
    description: str,
    author: str,
    category: str,
    commands: list[dict[str, str]],
    skill_names: list[str],
    readme: str,
    folder: str = "demo-suite",
    tags: list[str] | None = None,
) -> bytes:
    import json

    manifest: dict = {
        "name": name,
        "version": version,
        "description": description,
        "author": author,
        "category": category,
        "commands": commands,
        "skills": [{"name": item} for item in skill_names],
        "dataLinks": [{"name": "本地工作目录"}],
    }
    if tags:
        manifest["tags"] = tags

    files = {
        f"{folder}/plugin.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        f"{folder}/README.md": readme,
    }
    for index, skill in enumerate(skill_names):
        files[f"{folder}/skills/{index + 1}-{skill}/SKILL.md"] = (
            f"---\nname: {skill}\ndescription: {name} 的配套技能\n---\n\n"
            f"# {skill}\n\n根据套件指令的输入完成对应工作，并给出可执行的下一步。\n"
        )
    return _zip(files)


# ── 演示条目清单（分类口径以 catalog.TYPE_SPECS 为准）─────────


@dataclass(frozen=True)
class SkillSeed:
    slug: str
    summary: str
    description: str
    category: str
    tags: list[str]
    icon: str
    vendored: bool = False
    featured: bool = False


@dataclass(frozen=True)
class SuiteSeed:
    name: str
    folder: str
    version: str
    description: str
    category: str
    tags: list[str]
    icon: str
    commands: list[tuple[str, str]]
    skills: list[str]
    usage: str = ""
    featured: bool = False


SKILL_SEEDS: list[SkillSeed] = [
    SkillSeed(
        slug="weekly-report",
        summary="把一周的零散进展整理成结构清晰、可直接发送的周报",
        description=(
            "通天办公原创演示技能：按「结果 / 数据 / 风险 / 待办」四段输出，先给三条结论，"
            "只保留可验证的数据，待办写明责任人与时间。"
        ),
        category="效率",
        tags=["周报", "总结"],
        icon="📋",
    ),
    SkillSeed(
        slug="writing-plans",
        summary="把已确认的方案写成实现计划：文件、签名与验收测试逐项列明",
        description=(
            "面向没有接触过该代码库的工程师：写清要改哪些文件、接口签名与命名、每个任务用哪些测试"
            "证明完成，把方案拆成可顺序执行的小步任务，保持 DRY / YAGNI / TDD 与频繁提交。" + _SOURCE_NOTE
        ),
        category="效率",
        tags=["实现计划", "任务拆分", "工程流程"],
        icon="🗺️",
        vendored=True,
        featured=True,
    ),
    SkillSeed(
        slug="meeting-minutes",
        summary="把会议录音转写整理成决议、责任人与截止时间的纪要",
        description=(
            "通天办公原创演示技能：识别参会人与议题，抽取决议与分歧，形成责任到人的行动项；"
            "没有结论的讨论标记为「待定」，不替参会人编造决定。"
        ),
        category="文档",
        tags=["会议", "纪要"],
        icon="📝",
    ),
    SkillSeed(
        slug="copy-editing",
        summary="对已有文案做分轮校对：结构、语气、措辞到 AI 痕迹逐条给修改",
        description=(
            "把现成文案改好而不是重写：先查结构与语气，再逐句收紧措辞，标注每处修改的理由；"
            "只被要求改一句时输出单点修改，不做整篇重写。" + _SOURCE_NOTE
        ),
        category="文档",
        tags=["校对", "文案", "审阅"],
        icon="✏️",
        vendored=True,
    ),
    SkillSeed(
        slug="analytics",
        summary="埋点与度量体系落地：事件命名、UTM 规范与跟踪方案审计",
        description=(
            "从「怎么知道有没有效果」出发设计埋点：事件与属性命名、UTM 规范、GA4 或自建方案实现，"
            "再逐项审计漏报、重复上报与跨工具口径冲突。" + _SOURCE_NOTE
        ),
        category="数据",
        tags=["埋点", "指标", "GA4"],
        icon="📊",
        vendored=True,
        featured=True,
    ),
    SkillSeed(
        slug="test-driven-development",
        summary="先写测试看着它失败，再写最小实现，然后重构",
        description=(
            "红—绿—重构循环：任何功能或缺陷修复都先落测试并观察到失败，再写刚好通过的最小实现；"
            "没有亲眼见过测试失败，就不能声称它验证了正确的行为。" + _SOURCE_NOTE
        ),
        category="开发",
        tags=["TDD", "测试", "质量"],
        icon="🧪",
        vendored=True,
        featured=True,
    ),
    SkillSeed(
        slug="systematic-debugging",
        summary="遇到缺陷或异常行为先定位根因，不做症状级修补",
        description=(
            "把排障变成可重复的流程：构造最小可复现用例、隔离变量、提出并验证假设，"
            "在找到根因之前不提交修复方案。" + _SOURCE_NOTE
        ),
        category="开发",
        tags=["调试", "根因分析", "排障"],
        icon="🐛",
        vendored=True,
    ),
    SkillSeed(
        slug="frontend-design",
        summary="落地页与产品界面的前端设计规则：排版层级、配色与动效取向",
        description=(
            "把界面从「默认模板感」推向有主张的设计：先定排版层级与字体尺度，再处理配色、"
            "留白、动效与断点，逐条给出可落地的 CSS/组件级建议。" + _SOURCE_NOTE
        ),
        category="开发",
        tags=["前端", "UI", "视觉设计"],
        icon="🎨",
        vendored=True,
    ),
    SkillSeed(
        slug="ab-testing",
        summary="把改动变成可判定的实验：假设、样本量、显著性与实验节奏",
        description=(
            "设计能给出结论的 A/B 实验：主指标与护栏指标、样本量与最短运行时长、变体与控制组，"
            "并给出实验积压队列与 ICE 排序，避免频繁改动导致实验做不完。" + _SOURCE_NOTE
        ),
        category="运营",
        tags=["A/B 实验", "增长", "统计"],
        icon="🎯",
        vendored=True,
    ),
    SkillSeed(
        slug="churn-prevention",
        summary="区分主动流失与扣款失败，设计取消流程、挽留与挽回策略",
        description=(
            "同时处理主动流失与非主动流失：取消流程与动态挽留 offer、dunning 扣款失败挽回节奏、"
            "退出调研与订阅暂停；给出的每个判断都要求可回收的数据支撑。" + _SOURCE_NOTE
        ),
        category="运营",
        tags=["留存", "流失", "订阅"],
        icon="🔄",
        vendored=True,
    ),
    SkillSeed(
        slug="copywriting",
        summary="面向页面的营销文案：信息结构、标题与 CTA，主张需可支撑",
        description=(
            "首页、落地页、定价页、功能页的文案写法：先诊断输入是空白需求、已有声音还是单点修改，"
            "只改该改的部分；每条主张都要能被证据支撑，写不出证据就列出需要补的材料。" + _SOURCE_NOTE
        ),
        category="内容",
        tags=["文案", "转化", "营销"],
        icon="✍️",
        vendored=True,
        featured=True,
    ),
    SkillSeed(
        slug="git-guardrails-claude-code",
        summary="为 AI 编码工具拦截危险 Git 命令：push、reset --hard、clean",
        description=(
            "在工具执行前设置 PreToolUse 钩子，拦截 git push、reset --hard、clean、branch -D "
            "等破坏性命令并说明原因，避免误删未提交工作。" + _SOURCE_NOTE
        ),
        category="其他",
        tags=["Git", "安全护栏", "钩子"],
        icon="🛡️",
        vendored=True,
    ),
]


SUITE_SEEDS: list[SuiteSeed] = [
    SuiteSeed(
        name="劳动用工专家套件",
        folder="labor-employment-suite",
        version="1.2.0",
        description="覆盖劳动合同审查、试用期风险与劳动争议应对的岗位工作流包。",
        category="律师",
        tags=["劳动合同", "用工风险"],
        icon="⚖️",
        commands=[
            ("contract-review", "审查劳动合同的关键条款与风险点"),
            ("probation-risk", "评估试用期约定与解除风险"),
        ],
        skills=["contract-review", "probation-risk"],
        usage="在对话框中输入 `/contract-review` 等指令，按提示粘贴合同文本即可。",
    ),
    SuiteSeed(
        name="财务月结套件",
        folder="month-close-suite",
        version="1.0.0",
        description="月结检查清单、差异分析与结账说明生成，面向中小企业财务岗。",
        category="财务",
        tags=["月结", "差异分析"],
        icon="🧮",
        commands=[
            ("month-close", "按清单完成月结检查"),
            ("variance-analysis", "生成预算与实际差异分析"),
        ],
        skills=["month-close", "variance-analysis"],
        usage="所有金额与结论需由财务人员复核，本套件不替代专业判断。",
    ),
    SuiteSeed(
        name="会议与行程管理套件",
        folder="admin-meeting-suite",
        version="1.0.0",
        description="会前议程与材料准备、会中冲突协调、会后行动项跟踪的行政岗位包。",
        category="行政",
        tags=["会议", "行程"],
        icon="📅",
        commands=[
            ("agenda-prepare", "按议题与参会人生成议程与材料清单"),
            ("calendar-conflict", "检查多方时间与场地冲突并给出备选"),
        ],
        skills=["agenda-prepare", "calendar-conflict"],
    ),
    SuiteSeed(
        name="招聘漏斗套件",
        folder="hr-recruiting-suite",
        version="1.0.0",
        description="从岗位画像、JD 撰写到面试评分表与 offer 沟通的招聘工作流包。",
        category="HR",
        tags=["招聘", "面试"],
        icon="🧑‍💼",
        commands=[
            ("jd-write", "按岗位画像生成结构化 JD 与评估要点"),
            ("interview-scorecard", "输出分维度面试评分表与追问清单"),
        ],
        skills=["jd-write", "interview-scorecard"],
    ),
    SuiteSeed(
        name="市场投放套件",
        folder="marketing-campaign-suite",
        version="1.0.0",
        description="内容日历编排、渠道简报与投放复盘的市场岗位工作流包。",
        category="市场",
        tags=["投放", "内容日历"],
        icon="📣",
        commands=[
            ("content-calendar", "按主题与节点排月度内容日历"),
            ("channel-brief", "为单个渠道生成交互简报与素材需求"),
        ],
        skills=["content-calendar", "channel-brief"],
    ),
    SuiteSeed(
        name="销售商机跟进套件",
        folder="sales-pipeline-suite",
        version="1.0.0",
        description="线索分级、跟进话术与商机阶段推进的销售岗位工作流包。",
        category="销售",
        tags=["商机", "跟进"],
        icon="🤝",
        commands=[
            ("lead-triage", "按画像与信号给线索分级并给出下一步"),
            ("followup-script", "生成分场景跟进话术与异议应对"),
        ],
        skills=["lead-triage", "followup-script"],
    ),
    SuiteSeed(
        name="需求到发布套件",
        folder="product-delivery-suite",
        version="1.1.0",
        description="需求评审、验收标准与发布说明串起来的产研协作工作流包。",
        category="产研",
        tags=["需求评审", "发布"],
        icon="🚀",
        commands=[
            ("spec-review", "评审需求文档并列出未定义边界"),
            ("release-notes", "按变更清单生成面向用户的发布说明"),
        ],
        skills=["spec-review", "release-notes"],
        featured=True,
    ),
    SuiteSeed(
        name="用户运营增长套件",
        folder="growth-ops-suite",
        version="1.0.0",
        description="实验队列管理、指标护栏与用户分层的运营增长工作流包。",
        category="运营",
        tags=["增长", "实验"],
        icon="📈",
        commands=[
            ("experiment-queue", "维护实验积压队列并排序优先级"),
            ("metrics-guardrail", "定义主指标与不可劣化的护栏指标"),
        ],
        skills=["experiment-queue", "metrics-guardrail"],
    ),
    SuiteSeed(
        name="指标看板套件",
        folder="analytics-dashboard-suite",
        version="1.0.0",
        description="指标口径定义、看板搭建审计与口径冲突定位的数据分析工作流包。",
        category="数据分析",
        tags=["指标口径", "看板"],
        icon="🔍",
        commands=[
            ("metric-definition", "把业务目标拆成可计算的指标定义"),
            ("dashboard-audit", "审计看板数据源与口径一致性"),
        ],
        skills=["metric-definition", "dashboard-audit"],
    ),
    SuiteSeed(
        name="采购与库存套件",
        folder="supply-chain-suite",
        version="1.0.0",
        description="再订购点测算、供应商评分与库存健康检查的供应链工作流包。",
        category="供应链",
        tags=["采购", "库存"],
        icon="📦",
        commands=[
            ("reorder-point", "按需求波动与安全库存测算再订购点"),
            ("supplier-scorecard", "输出供应商交付与质量评分表"),
        ],
        skills=["reorder-point", "supplier-scorecard"],
    ),
    SuiteSeed(
        name="品牌一致性套件",
        folder="brand-design-suite",
        version="1.0.0",
        description="设计令牌梳理、素材走查与品牌口径维护的设计岗位工作流包。",
        category="设计",
        tags=["品牌", "设计走查"],
        icon="🎨",
        commands=[
            ("brand-tokens", "把品牌规范整理成可复用的设计令牌"),
            ("asset-review", "按规范走查素材并列出偏差项"),
        ],
        skills=["brand-tokens", "asset-review"],
    ),
    SuiteSeed(
        name="政策材料写作套件",
        folder="public-affairs-suite",
        version="1.0.0",
        description="材料提纲搭建、数据来源核对与表述规范检查的政务公共岗位包。",
        category="政务公共",
        tags=["公文", "材料"],
        icon="🏛️",
        commands=[
            ("material-outline", "按主题与受众生成材料提纲骨架"),
            ("data-citation-check", "核对引用数据来源与表述准确性"),
        ],
        skills=["material-outline", "data-citation-check"],
    ),
    SuiteSeed(
        name="通用办公助理套件",
        folder="general-assistant-suite",
        version="1.0.0",
        description="跨岗位的日常收口：任务登记、进展摘要与次日计划。",
        category="其他",
        tags=["待办", "摘要"],
        icon="🧰",
        commands=[
            ("task-intake", "把零散请求登记成带责任人的待办"),
            ("daily-brief", "生成当日进展摘要与次日计划"),
        ],
        skills=["task-intake", "daily-brief"],
    ),
]


def _suite_readme(seed: SuiteSeed) -> str:
    commands = "\n".join(f"- `/{name}`：{desc}" for name, desc in seed.commands)
    lines = [
        f"# {seed.name}",
        "",
        seed.description,
        "",
        "## 内置指令",
        "",
        commands,
        "",
        "> 本套件为演示数据，展示如何把岗位 SOP 固化为可调用的套件指令；"
        "输出需由岗位人员复核，不构成专业意见。",
    ]
    if seed.usage:
        lines.insert(7, "")
        lines.insert(6, f"## 使用提示\n\n{seed.usage}")
    return "\n".join(lines) + "\n"


def _mcp_seeds() -> list[market.McpCreateIn]:
    return [
        market.McpCreateIn(
            name="本地文件系统 MCP",
            version="1.0.0",
            summary="通过 MCP 协议访问指定目录下的文件，适合本地资料整理。",
            description="官方 filesystem 服务的演示登记项；命令通过 npx 启动，仅能访问启动参数指定的目录。",
            category="办公",
            tags=["文件", "本地"],
            icon="🗂️",
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
            icon="🐙",
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
            name="Playwright MCP",
            version="1.0.0",
            summary="驱动真实浏览器完成页面操作、截图与端到端验证。",
            description=(
                "官方 @playwright/mcp 包（Apache-2.0）：会在本机启动浏览器进程并访问网页，"
                "属于高风险 stdio 连接器，请只在需要 UI 验证时启用。"
            ),
            category="开发",
            tags=["浏览器", "自动化", "E2E"],
            icon="🌐",
            transport="stdio",
            command="npx",
            args=["-y", "@playwright/mcp@latest"],
            timeoutSecs=120,
            readme="# Playwright MCP\n\n演示配置：`npx -y @playwright/mcp@latest`。首次使用需在本机安装浏览器内核；"
            "启动后会真实访问网页，请评估合规与数据外发风险。\n",
        ),
        market.McpCreateIn(
            name="PostgreSQL MCP",
            version="1.0.0",
            summary="以只读方式查询指定 PostgreSQL 库，用于取数与口径核对。",
            description=(
                "演示数据：连接串作为启动参数传入，请把示例地址换成自己的库；"
                "建议使用只读账号，连接串包含的口令不会存放在市场服务端。"
            ),
            category="数据",
            tags=["PostgreSQL", "取数", "只读"],
            icon="🐘",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost:5432/example"],
            timeoutSecs=60,
            readme="# PostgreSQL MCP\n\n演示配置：把连接串替换为实际库地址，并优先使用只读账号。\n\n"
            "> 连接串会随配置下发到客户端，请勿在其中写入生产环境凭据；需要凭据时改用本地环境注入。\n",
        ),
        market.McpCreateIn(
            name="Brave Search MCP",
            version="1.0.0",
            summary="调用 Brave 搜索接口做网页检索，需要 API 密钥。",
            description="演示数据：客户端登记后填写 BRAVE_API_KEY，密钥值由客户端保存，市场服务端只登记键名。",
            category="搜索",
            tags=["搜索", "网页"],
            icon="🔎",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-brave-search"],
            envKeys=[
                market.KeyHintIn(
                    key="BRAVE_API_KEY",
                    description="Brave Search API 密钥",
                    required=True,
                    sensitive=True,
                )
            ],
            timeoutSecs=30,
            readme="# Brave Search MCP\n\n演示配置：需要在 Brave 开放平台申请搜索 API 密钥后在客户端填写。\n",
        ),
        market.McpCreateIn(
            name="Context7 MCP",
            version="1.0.0",
            summary="远程 streamable_http 连接器示例：检索开源库的当前版本文档。",
            description=(
                "演示数据：真实可访问的远程 MCP 端点（已连通验证），不携带命令行；"
                "远程连接器只允许填写 url 与请求头键名，鉴权值由客户端保存。"
            ),
            category="搜索",
            tags=["远程", "文档", "库版本"],
            icon="📚",
            transport="streamable_http",
            url="https://mcp.context7.com/mcp",
            headerKeys=[
                market.KeyHintIn(
                    key="Authorization",
                    description="可选：Bearer <api-key>，提升配额时填写（由客户端保存）",
                    required=False,
                    sensitive=True,
                )
            ],
            timeoutSecs=60,
            readme="# Context7 MCP\n\n演示配置：远程连接器的请求头值由客户端填写，市场只登记键名与说明。\n",
        ),
        market.McpCreateIn(
            name="示例：远程搜索 MCP",
            version="1.0.0",
            summary="演示远程 streamable_http 连接器的登记方式（地址为占位符）。",
            description="演示数据：请发布者替换为真实可用的 MCP 地址，并在客户端填写鉴权请求头。",
            category="搜索",
            tags=["远程", "示例"],
            icon="🧪",
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
        market.McpCreateIn(
            name="Slack MCP",
            version="1.0.0",
            summary="收发 Slack 消息与频道内容，需要机器人令牌。",
            description=(
                "演示数据：客户端登记后填写机器人令牌与频道范围；"
                "令牌不会存放在市场服务端，请在 Slack 侧按最小权限签发。"
            ),
            category="通讯",
            tags=["Slack", "消息", "协作"],
            icon="💬",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-slack"],
            envKeys=[
                market.KeyHintIn(
                    key="SLACK_BOT_TOKEN",
                    description="xoxb- 开头的机器人令牌（建议只授予只读与发送消息权限）",
                    required=True,
                    sensitive=True,
                ),
                market.KeyHintIn(
                    key="SLACK_TEAM_ID",
                    description="工作区 ID，用于限定访问范围",
                    required=False,
                    sensitive=False,
                ),
                market.KeyHintIn(
                    key="SLACK_CHANNEL_IDS",
                    description="逗号分隔的频道 ID 白名单",
                    required=False,
                    sensitive=False,
                ),
            ],
            timeoutSecs=30,
            readme="# Slack MCP\n\n演示配置：令牌与频道范围都在客户端填写，市场只分发键名与说明。\n",
        ),
        market.McpCreateIn(
            name="Memory Graph MCP",
            version="1.0.0",
            summary="本地知识图谱记忆，跨会话沉淀实体与关系。",
            description="演示数据：官方 server-memory 包，图谱存放在本机文件，不涉及外部网络调用。",
            category="其他",
            tags=["记忆", "知识图谱", "本地"],
            icon="🧠",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-memory"],
            envKeys=[
                market.KeyHintIn(
                    key="MEMORY_FILE_PATH",
                    description="图谱持久化文件路径，留空则使用默认位置",
                    required=False,
                    sensitive=False,
                )
            ],
            timeoutSecs=30,
            readme="# Memory Graph MCP\n\n演示配置：通过 `MEMORY_FILE_PATH` 指定图谱存放位置，便于多会话共享同一份记忆。\n",
        ),
    ]


SEED_COUNTS: dict[str, int] = {
    "skill": len(SKILL_SEEDS),
    "suite": len(SUITE_SEEDS),
    "mcp": len(_mcp_seeds()),
}


# ── 写入 ─────────────────────────────────────────────────────


def _ensure_demo_user() -> tuple[sqlite3.Row, str | None]:
    password = os.environ.get("TT_DEMO_PASSWORD") or secrets.token_urlsafe(9)
    with tx() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?1", (DEMO_EMAIL,)).fetchone()
        if row is not None:
            # 旧库里的演示发布者仍是默认旧名时改名一次；其他情况一律不动
            if (row["nickname"] or "") == LEGACY_DEMO_NICKNAME:
                conn.execute(
                    "UPDATE users SET nickname = ?2 WHERE uid = ?1",
                    (row["uid"], DEMO_NICKNAME),
                )
                row = conn.execute("SELECT * FROM users WHERE uid = ?1", (row["uid"],)).fetchone()
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


def _mark_featured(item_type: str, item_id: str, author_uid: str) -> None:
    """把演示条目置为推荐；只动刚由本次演示写入且属于演示账号的条目。"""
    with tx() as conn:
        conn.execute(
            "UPDATE items SET featured = 1, updated_at_ms = ?3 "
            "WHERE item_type = ?1 AND item_id = ?2 AND author_uid = ?4 AND featured = 0",
            (item_type, item_id, now_ms(), author_uid),
        )


def seed_demo() -> None:
    user_row, password = _ensure_demo_user()
    user = dict(user_row)
    created = 0

    for seed in SKILL_SEEDS:
        if _exists("skill", seed.slug):
            continue
        root = DEMO_SKILLS_DIR / seed.slug
        if seed.vendored:
            # 公开库正文必须带着许可与出处一起进包，缺任何一份都拒绝启动
            for notice in ("LICENSE.txt", "SOURCE.txt"):
                if not (root / notice).is_file():
                    raise RuntimeError(f"演示技能 {seed.slug} 缺少随包 {notice}")
        package = _zip_dir(root)
        item = market.publish_package(
            user,
            "skill",
            package,
            f"{seed.slug}.zip",
            summary=seed.summary,
            description=seed.description,
            category=seed.category,
            tags=", ".join(seed.tags),
            icon=seed.icon,
            changelog="首次发布",
        )
        if seed.featured:
            _mark_featured("skill", item["id"], user["uid"])
        created += 1

    for seed in SUITE_SEEDS:
        if _exists("suite", seed.name):
            continue
        package = _suite_package(
            seed.name,
            seed.version,
            seed.description,
            f"@{DEMO_NICKNAME}",
            seed.category,
            [{"name": name, "description": desc} for name, desc in seed.commands],
            seed.skills,
            _suite_readme(seed),
            folder=seed.folder,
            tags=seed.tags,
        )
        item = market.publish_package(
            user,
            "suite",
            package,
            f"{seed.folder}.zip",
            summary=seed.description,
            description=seed.description,
            category=seed.category,
            tags=", ".join(seed.tags),
            icon=seed.icon,
            version=seed.version,
            changelog="首次发布",
        )
        if seed.featured:
            _mark_featured("suite", item["id"], user["uid"])
        created += 1

    for payload in _mcp_seeds():
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
