"""Skill 层：只管理提示词模板、可用工具白名单、上下文策略与动态切换。

不调用 LLM、不碰 MCP 连接、不组装消息。技能包是独立文件（YAML/JSON），
用户目录同名技能按 id 覆盖内置项；`reload_if_changed()` 让改文件后无需重启。

文件形状（一文件一个技能或 `skills:` 数组）：

    id: weekly-report
    name: 周报助手
    description: 汇总本周工作产出
    prompt: |
      你是周报助手……
    match:
      keywords: [周报, weekly]
      priority: 10
    tools:
      allow: [view_text_file, write_text_file, insert_text_file]
      deny: []
    mcp:
      servers: [filesystem]
      allow: ["filesystem/*"]
    context:
      mode: writing
      max_iters: 20
      lazy_mcp_tools: true
      context_window: 128000
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, PlatformConfig, bundled_dir, load_structured

ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MODES = ("general", "slides", "writing", "design")
MAX_PROMPT_CHARS = 20000


class SkillError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _s(value: Any) -> str:
    return str(value or "").strip()


def _str_list(value: Any, field_name: str, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillError("SKILL_INVALID", f"{where} 的 {field_name} 必须是数组")
    out: list[str] = []
    for item in value:
        text = _s(item)
        if not text:
            raise SkillError("SKILL_INVALID", f"{where} 的 {field_name} 含空项")
        if text not in out:
            out.append(text)
    return out


def _int_or_none(value: Any, field_name: str, where: str, lo: int, hi: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SkillError("SKILL_INVALID", f"{where} 的 {field_name} 必须是整数") from None
    if number < lo or number > hi:
        raise SkillError("SKILL_INVALID", f"{where} 的 {field_name} 超出范围 {lo}~{hi}")
    return number


@dataclass(frozen=True)
class SkillContext:
    mode: str = "general"
    max_iters: int | None = None
    lazy_mcp_tools: bool | None = None
    context_window: int | None = None

    def policy(self) -> dict:
        block: dict = {}
        if self.max_iters is not None:
            block["maxIters"] = self.max_iters
        if self.lazy_mcp_tools is not None:
            block["lazyMcpTools"] = self.lazy_mcp_tools
        return block


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    prompt: str
    keywords: tuple[str, ...] = ()
    priority: int = 0
    tools_allow: tuple[str, ...] = ()
    tools_deny: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    mcp_allow: tuple[str, ...] = ()
    context: SkillContext = field(default_factory=SkillContext)
    source: str = ""
    builtin: bool = False

    # ── 匹配与白名单（纯函数，便于单测） ──────────────────────────

    def match_score(self, text: str) -> int:
        lowered = (text or "").lower()
        hits = sum(1 for kw in self.keywords if kw.lower() in lowered)
        return self.priority + hits * 10 if hits else 0

    def tool_allowed(self, name: str) -> bool:
        """内置工具白名单：deny 优先；allow 为空表示不限制。模式用 fnmatch。"""
        if any(fnmatch.fnmatchcase(name, pat) for pat in self.tools_deny):
            return False
        if not self.tools_allow:
            return True
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.tools_allow)

    def mcp_tool_allowed(self, server_id: str, tool_name: str) -> bool:
        """MCP 工具白名单：`server/tool`、`server/*`、`*/*`；allow 为空表示该技能绑定的服务器全放行。"""
        if not self.mcp_allow:
            return True
        target = f"{server_id}/{tool_name}"
        for pat in self.mcp_allow:
            if fnmatch.fnmatchcase(target, pat):
                return True
            # 只写服务器名等价于该服务器全放行
            if "/" not in pat and pat == server_id:
                return True
        return False

    # ── 与存量引擎的适配（只做数据形状转换） ──────────────────────

    def to_runtime_skill(self) -> dict:
        """转成 engine/server.py `normalize_skills` 认得的形状，正文按需由 use_skill 取。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "content": self.prompt,
            "kind": "skill",
            "source": "agent-platform",
        }

    def prompt_block(self) -> str:
        return (
            f"\n\n## 当前技能：{self.name}\n"
            f"技能标识：{self.id}\n"
            f"{self.prompt.strip()}\n"
            "工具白名单、安全与审批边界始终优先于技能设定，不得以技能身份为由绕过。"
        )

    def view(self, with_prompt: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "priority": self.priority,
            "toolsAllow": list(self.tools_allow),
            "toolsDeny": list(self.tools_deny),
            "mcpServers": list(self.mcp_servers),
            "mcpAllow": list(self.mcp_allow),
            "context": {
                "mode": self.context.mode,
                "maxIters": self.context.max_iters,
                "lazyMcpTools": self.context.lazy_mcp_tools,
                "contextWindow": self.context.context_window,
            },
            "source": self.source,
            "builtin": self.builtin,
        }
        if with_prompt:
            data["prompt"] = self.prompt
        return data


def parse_skill(raw: dict, source: str, builtin: bool = False) -> Skill:
    if not isinstance(raw, dict):
        raise SkillError("SKILL_INVALID", f"{source}：技能定义必须是对象")
    sid = _s(raw.get("id"))
    where = f"{source}（{sid or '无 id'}）"
    if not sid:
        raise SkillError("SKILL_INVALID", f"{source}：缺少 id")
    if not ID_RE.match(sid):
        raise SkillError("SKILL_INVALID", f"{where}：id 只能包含字母、数字、点、下划线和横线")
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise SkillError("SKILL_INVALID", f"{where}：prompt 不能为空")
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n…（提示词过长，已截断）"

    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    keywords = tuple(k.lower() for k in _str_list(match.get("keywords"), "match.keywords", where))
    try:
        priority = int(match.get("priority") or 0)
    except (TypeError, ValueError):
        raise SkillError("SKILL_INVALID", f"{where}：match.priority 必须是整数") from None

    tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    tools_allow = tuple(_str_list(tools.get("allow"), "tools.allow", where))
    tools_deny = tuple(_str_list(tools.get("deny"), "tools.deny", where))

    mcp = raw.get("mcp") if isinstance(raw.get("mcp"), dict) else {}
    mcp_servers = tuple(_str_list(mcp.get("servers"), "mcp.servers", where))
    mcp_allow = tuple(_str_list(mcp.get("allow"), "mcp.allow", where))

    ctx_raw = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    mode = _s(ctx_raw.get("mode")) or "general"
    if mode not in MODES:
        raise SkillError(
            "SKILL_INVALID", f"{where}：context.mode 必须是 {'/'.join(MODES)} 之一"
        )
    lazy = ctx_raw.get("lazy_mcp_tools")
    lazy_bool = None if lazy is None else (lazy is True or str(lazy).strip().lower() in ("1", "true", "yes", "on"))
    context = SkillContext(
        mode=mode,
        max_iters=_int_or_none(ctx_raw.get("max_iters"), "context.max_iters", where, 1, 100),
        lazy_mcp_tools=lazy_bool,
        context_window=_int_or_none(
            ctx_raw.get("context_window"), "context.context_window", where, 1000, 2_000_000
        ),
    )
    return Skill(
        id=sid,
        name=_s(raw.get("name")) or sid,
        description=_s(raw.get("description")),
        prompt=prompt,
        keywords=keywords,
        priority=priority,
        tools_allow=tools_allow,
        tools_deny=tools_deny,
        mcp_servers=mcp_servers,
        mcp_allow=mcp_allow,
        context=context,
        source=source,
        builtin=builtin,
    )


def _iter_definitions(path: Path) -> list[dict]:
    raw = load_structured(path)
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("skills"), list):
        return raw["skills"]
    if isinstance(raw, dict):
        return [raw]
    raise SkillError("SKILL_INVALID", f"{path.name}：根节点必须是对象、数组或 skills 数组")


def _scan(dir_path: Path, builtin: bool, into: dict[str, Skill], errors: list[str]) -> list[tuple]:
    signature: list[tuple] = []
    if not dir_path.is_dir():
        return signature
    for path in sorted(dir_path.iterdir()):
        if path.suffix.lower() not in (".yaml", ".yml", ".json") or not path.is_file():
            continue
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
        try:
            definitions = _iter_definitions(path)
        except (SkillError, ConfigError, Exception) as exc:  # noqa: BLE001
            errors.append(f"{path.name} 解析失败：{exc}")
            continue
        seen: set[str] = set()
        for item in definitions:
            try:
                skill = parse_skill(item, str(path), builtin=builtin)
            except SkillError as exc:
                errors.append(str(exc))
                continue
            if skill.id in seen:
                errors.append(f"{path.name} 内技能 id 重复：{skill.id}（保留第一个）")
                continue
            seen.add(skill.id)
            into[skill.id] = skill
    return signature


class SkillRegistry:
    def __init__(self, skills: dict[str, Skill], errors: list[str],
                 signature: list[tuple], config: PlatformConfig):
        self._skills = skills
        self.errors = errors
        self.signature = signature
        self.config = config

    @classmethod
    def load(cls, config: PlatformConfig | None = None) -> "SkillRegistry":
        config = config or PlatformConfig()
        skills: dict[str, Skill] = {}
        errors: list[str] = []
        signature = _scan(bundled_dir() / "skills", True, skills, errors)
        signature += _scan(config.skills_dir, False, skills, errors)
        return cls(skills, errors, signature, config)

    def reload_if_changed(self) -> bool:
        """技能文件增删改后自动重载；返回是否真的重载过。"""
        current: list[tuple] = []
        for dir_path in (bundled_dir() / "skills", self.config.skills_dir):
            if not dir_path.is_dir():
                continue
            for path in sorted(dir_path.iterdir()):
                if path.suffix.lower() in (".yaml", ".yml", ".json") and path.is_file():
                    try:
                        stat = path.stat()
                        current.append((str(path), stat.st_mtime_ns, stat.st_size))
                    except OSError:
                        continue
        if current == self.signature:
            return False
        fresh = type(self).load(self.config)
        self._skills, self.errors, self.signature = fresh._skills, fresh.errors, fresh.signature
        return True

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(_s(skill_id))

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: (-s.priority, s.id))

    def match(self, text: str) -> Skill | None:
        best: Skill | None = None
        best_score = 0
        for skill in self.all():
            score = skill.match_score(text)
            if score > best_score:
                best, best_score = skill, score
        return best

    def resolve(self, ref: str | None, text: str = "") -> tuple[Skill | None, str]:
        """显式 id/名称 > 关键词匹配 > 默认技能；返回 (skill, 说明)。"""
        if _s(ref):
            skill = self.get(_s(ref))
            if skill is not None:
                return skill, f"显式指定技能 {skill.id}"
            for item in self._skills.values():
                if item.name == _s(ref) or item.name.lower() == _s(ref).lower():
                    return item, f"按名称解析技能 {item.id}"
            available = "、".join(s.id for s in self.all()) or "（无）"
            return None, f"技能「{_s(ref)}」不存在，可用：{available}"
        matched = self.match(text)
        if matched is not None:
            return matched, f"按关键词匹配技能 {matched.id}"
        default = self.get(self.config.default_skill)
        if default is not None:
            return default, f"未匹配关键词，使用默认技能 {default.id}"
        first = self.all()[0] if self._skills else None
        if first is not None:
            return first, f"默认技能不可用，使用 {first.id}"
        return None, "没有任何可用技能，请先在 skills 目录添加技能包"

    def view(self, with_prompt: bool = False) -> dict:
        return {
            "skills": [s.view(with_prompt=with_prompt) for s in self.all()],
            "errors": list(self.errors),
            "defaultSkill": self.config.default_skill,
        }
