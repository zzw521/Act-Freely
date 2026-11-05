"""技能统一注册表与共享池（缺陷 D-38）。

现状是两套互不相认的注册表：Rust 侧 `{data_home}/skills/*/SKILL.md`（frontmatter 只取
name / description，正文即技能），Python 平台层一套 YAML 技能（提示词、工具白名单、
上下文策略、窗口配置，默认关闭）。同一个技能要在两个专家下用就得装两遍，
改一次只生效一边，也没有依赖检查。

本模块定义**一份清单**，两边都能消费：

    {data_home}/skill_pool/pool.json

池里存的不是副本，而是**引用**（来源类型 + 路径 + 内容版本）。正文永远现读源文件，
所以「一个技能同时启用给两个专家，改一次两边生效」是结构性的，不是靠同步机制实现的；
启停状态按专家记在同一个文件里，所以「停用立即影响两边」也是同一份事实。

三条不可越过的线：

* **只读适配**：既有 `SKILL.md` 与 YAML 一律不移动、不改写、不删除；池里摘掉一条引用
  不等于卸载技能。
* **draft 不自动启用**：成功任务或浏览器录制生成的草案只有经显式 `promote` 才进执行面。
* **缺依赖不静默跳过**：技能声明的工具 / MCP / 其它技能在当前专家下不可用时，
  给出中文原因并把这条原因带进本次执行，而不是悄悄少一个技能。

与执行面的接缝：`to_runtime_skills()` 的输出就是 `runtime_context.normalize_skills`
认得的九个键，`#164` 的注册面交棒后接上去只是一行调用。本模块**不注册工具、不加端点**
（注册面尚未交棒），也不碰 `SYS_PROMPT` / `MODE_PROMPTS`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

POOL_DIRNAME = "skill_pool"
POOL_FILENAME = "pool.json"
DRAFT_DIRNAME = "drafts"
#: Rust 侧已安装技能的目录（`src-tauri/src/skill/mod.rs::skills_root` 同规则）
INSTALLED_DIRNAME = "skills"
SKILL_MD_NAME = "SKILL.md"

STATUS_READY = "ready"
STATUS_DRAFT = "draft"

SOURCE_SKILL_MD = "skill-md"
SOURCE_YAML = "yaml"
SOURCE_DRAFT = "draft"
SOURCE_KINDS = (SOURCE_SKILL_MD, SOURCE_YAML, SOURCE_DRAFT)

#: 与 Rust `MAX_SKILL_MD_BYTES` 同值：超过这个大小的正文两边都读不到，必须同口径
MAX_SKILL_MD_BYTES = 512 * 1024
#: 草案正文上限（比安装口小一档：草案是给人审的，不该一次灌满）
MAX_DRAFT_CHARS = 60000
#: 清单里 description 的存储上限
MAX_DESCRIPTION_CHARS = 500

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _brief(text: object, limit: int = 200) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def pool_dir(home: Path) -> Path:
    return Path(home) / POOL_DIRNAME


def pool_file(home: Path) -> Path:
    return pool_dir(home) / POOL_FILENAME


def drafts_dir(home: Path) -> Path:
    return pool_dir(home) / DRAFT_DIRNAME


def installed_root(home: Path) -> Path:
    return Path(home) / INSTALLED_DIRNAME


# ── 技能清单条目 ───────────────────────────────────────────────


@dataclass
class SkillSource:
    """技能正文在哪。池只存引用，不存副本。"""

    kind: str
    path: str
    #: 平台层 YAML 内的技能 id（一个 YAML 文件可以带多个技能）
    entry_id: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "entryId": self.entry_id}

    @classmethod
    def from_dict(cls, raw: dict) -> "SkillSource":
        kind = str(raw.get("kind") or "").strip()
        return cls(
            kind=kind if kind in SOURCE_KINDS else SOURCE_SKILL_MD,
            path=str(raw.get("path") or "").strip(),
            entry_id=str(raw.get("entryId") or "").strip(),
        )

    def display(self) -> str:
        labels = {
            SOURCE_SKILL_MD: "已安装技能目录",
            SOURCE_YAML: "平台层 YAML 技能",
            SOURCE_DRAFT: "共享池草案",
        }
        return f"{labels.get(self.kind, self.kind)}（{self.path}）"


@dataclass
class SkillRequirements:
    """启用前要检查的依赖。三族分开写，中文原因才能指到具体是哪一类缺了。"""

    tools: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"tools": list(self.tools), "mcp": list(self.mcp), "skills": list(self.skills)}

    @classmethod
    def from_dict(cls, raw: dict) -> "SkillRequirements":
        def key(name: str) -> list[str]:
            value = raw.get(name)
            if not isinstance(value, list):
                return []
            return [str(v).strip() for v in value if str(v or "").strip()]

        return cls(tools=key("tools"), mcp=key("mcp"), skills=key("skills"))

    def empty(self) -> bool:
        return not (self.tools or self.mcp or self.skills)


@dataclass
class SkillEntry:
    """统一清单里的一条。Rust harness 与 Python 平台读的是同一份字段。"""

    id: str
    title: str
    description: str
    source: SkillSource
    #: 正文内容摘要前 12 位：源文件一改它就变，这就是「版本」
    version: str = ""
    status: str = STATUS_READY
    requires: SkillRequirements = field(default_factory=SkillRequirements)
    #: 按专家的启停；没有记录的专家走 `default_enabled`
    enablements: dict[str, bool] = field(default_factory=dict)
    default_enabled: bool = True
    #: 窗口与上下文策略：平台层 YAML 有，SKILL.md 没有，缺省即「不覆盖执行面默认值」
    context: dict = field(default_factory=dict)
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    #: 由 Rust 静态扫描给出的结论；None 表示本模块无权判定（不许自行猜测）
    scan_blocked: bool | None = None
    added_at_ms: int = 0
    updated_at_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "status": self.status,
            "source": self.source.as_dict(),
            "requires": self.requires.as_dict(),
            "enablements": dict(self.enablements),
            "defaultEnabled": self.default_enabled,
            "context": dict(self.context),
            "toolsAllow": list(self.tools_allow),
            "toolsDeny": list(self.tools_deny),
            "scanBlocked": self.scan_blocked,
            "addedAtMs": self.added_at_ms,
            "updatedAtMs": self.updated_at_ms,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SkillEntry":
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        requires = raw.get("requires") if isinstance(raw.get("requires"), dict) else {}
        enablements_raw = raw.get("enablements") if isinstance(raw.get("enablements"), dict) else {}
        enablements = {str(k): bool(v) for k, v in enablements_raw.items()}
        context = raw.get("context") if isinstance(raw.get("context"), dict) else {}

        def str_list(key: str) -> list[str]:
            value = raw.get(key)
            if not isinstance(value, list):
                return []
            return [str(v).strip() for v in value if str(v or "").strip()]

        status = str(raw.get("status") or STATUS_READY).strip()
        scan = raw.get("scanBlocked")
        return cls(
            id=str(raw.get("id") or "").strip(),
            title=str(raw.get("title") or raw.get("id") or "").strip(),
            description=str(raw.get("description") or "")[:MAX_DESCRIPTION_CHARS],
            source=SkillSource.from_dict(source),
            version=str(raw.get("version") or "").strip(),
            status=status if status in (STATUS_READY, STATUS_DRAFT) else STATUS_DRAFT,
            requires=SkillRequirements.from_dict(requires),
            enablements=enablements,
            default_enabled=raw.get("defaultEnabled") is not False,
            context=dict(context),
            tools_allow=str_list("toolsAllow"),
            tools_deny=str_list("toolsDeny"),
            scan_blocked=None if scan is None else bool(scan),
            added_at_ms=int(raw.get("addedAtMs") or 0),
            updated_at_ms=int(raw.get("updatedAtMs") or 0),
        )

    def is_draft(self) -> bool:
        return self.status == STATUS_DRAFT

    def exists(self) -> bool:
        return Path(self.source.path).is_file()


# ── 正文读取与版本 ─────────────────────────────────────────────


def read_skill_md_body(path: Path) -> tuple[str, str]:
    """读 SKILL.md，返回 (正文, 错误)。与 Rust `read_skill_md` 同一条大小上限。

    超限或读不到时把原因交出去，让调用方如实标「正文不可用」，
    而不是回一个空字符串假装这个技能没内容。
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        return "", f"技能文件读不到：{_brief(exc)}"
    if size > MAX_SKILL_MD_BYTES:
        return "", (
            f"技能文件超过 {MAX_SKILL_MD_BYTES // 1024}KB 上限（实际 {size} 字节），"
            "正文未进入执行面；请精简后重新安装"
        )
    try:
        return target.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return "", f"技能文件读取失败：{_brief(exc)}"


def content_version(body: str) -> str:
    """内容摘要前 12 位作为版本：不需要维护计数器，改一次就变一次"""
    return hashlib.sha256(str(body or "").encode("utf-8", "replace")).hexdigest()[:12]


def parse_frontmatter(text: str) -> tuple[str, str]:
    """解析 SKILL.md 的 frontmatter，取 name / description。

    与 Rust `skill::parse_skill_md` 逐条对齐：只认这两个键、值去引号、
    `>` / `|` 折叠块取缩进续行、无 frontmatter 时用正文首个非标题段落当描述。
    对齐是硬要求——同一份文件两边给出不同的名字，统一注册表就白做了。
    """
    lines = str(text or "").splitlines()
    cursor = 0
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines) or lines[cursor].strip() != "---":
        return "", _first_paragraph(lines[cursor:])
    cursor += 1

    name = ""
    desc = ""
    block_key = ""
    block: list[str] = []
    closed = False
    while cursor < len(lines):
        raw = lines[cursor]
        cursor += 1
        if raw.strip() == "---":
            closed = True
            break
        indent = len(raw) - len(raw.lstrip())
        if indent > 0:
            if block_key:
                block.append(raw.strip())
            continue
        if block_key:
            value = " ".join(block).strip()
            if value:
                if block_key == "name":
                    name = value
                else:
                    desc = value
            block_key = ""
            block = []
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        if key not in ("name", "description"):
            continue
        value = value.strip().strip('"').strip("'")
        if not value or value.startswith(">") or value.startswith("|"):
            block_key = key
            block = []
        elif key == "name":
            name = value
        else:
            desc = value
    if block_key:
        value = " ".join(block).strip()
        if value:
            if block_key == "name":
                name = value
            else:
                desc = value
    if not name and not desc and closed:
        # frontmatter 存在但两个键都没有：正文首段兜底做描述（与 Rust 同一条）
        desc = _first_paragraph(lines[cursor:])
    return name, desc


def _first_paragraph(lines: list[str]) -> str:
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("---"):
            continue
        return text[:200]
    return ""


def read_body(entry: SkillEntry) -> tuple[str, str]:
    """按来源类型现读正文；返回 (正文, 错误说明)。"""
    path = Path(entry.source.path)
    if entry.source.kind == SOURCE_YAML:
        return _read_yaml_body(entry)
    if not path.is_file():
        return "", f"技能正文文件不存在：{path}"
    body, error = read_skill_md_body(path)
    if error:
        return "", error
    return body, ""


def _read_yaml_body(entry: SkillEntry) -> tuple[str, str]:
    registry, error = _platform_registry()
    if registry is None:
        return "", error
    skill = registry.get(entry.source.entry_id or entry.id)
    if skill is None:
        return "", (
            f"平台层技能「{entry.source.entry_id or entry.id}」当前不在注册表里："
            "技能文件可能已改名或删除，请重新共享"
        )
    return skill.prompt, ""


# ── 两个只读适配器 ─────────────────────────────────────────────


def discover_installed(home: Path) -> tuple[list[SkillEntry], list[str]]:
    """把 Rust 已安装的 `{home}/skills/*/SKILL.md` 适配成清单项（只读，不动文件）。"""
    notes: list[str] = []
    root = installed_root(home)
    if not root.is_dir():
        return [], []
    out: list[SkillEntry] = []
    for directory in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        skill_md = directory / SKILL_MD_NAME
        if not skill_md.is_file():
            continue
        if not ID_RE.match(directory.name):
            notes.append(f"技能目录名「{directory.name}」不能用作技能标识，未纳入共享池")
            continue
        body, error = read_skill_md_body(skill_md)
        if error:
            notes.append(f"技能「{directory.name}」{error}")
            continue
        name, desc = parse_frontmatter(body)
        out.append(SkillEntry(
            id=directory.name,
            title=name or directory.name,
            description=desc[:MAX_DESCRIPTION_CHARS],
            source=SkillSource(kind=SOURCE_SKILL_MD, path=str(skill_md)),
            version=content_version(body),
            added_at_ms=_now_ms(),
            updated_at_ms=_now_ms(),
        ))
    return out, notes


def _platform_registry():
    """平台层 YAML 注册表；缺包或解析不了就如实说明，不假装技能在场。"""
    try:
        from agent_platform.config import PlatformConfig
        from agent_platform.skills import SkillRegistry
    except Exception as exc:  # noqa: BLE001
        return None, f"平台层 YAML 技能不可用：{_brief(exc)}"
    try:
        registry = SkillRegistry.load(PlatformConfig())
    except Exception as exc:  # noqa: BLE001
        return None, f"平台层 YAML 技能加载失败：{_brief(exc)}"
    return registry, ""


def discover_yaml(home: Path) -> tuple[list[SkillEntry], list[str]]:
    """把平台层 YAML 技能适配成清单项（只读）。

    YAML 侧带着 SKILL.md 没有的东西：工具白名单、上下文模式、窗口配置——
    统一清单必须留得住这些字段，否则「统一」就是把弱的两边拉平。
    """
    registry, error = _platform_registry()
    if registry is None:
        return [], [error] if error else []
    out: list[SkillEntry] = []
    notes = [f"平台层技能解析提示：{e}" for e in registry.errors]
    for skill in registry.all():
        entry = SkillEntry(
            id=skill.id,
            title=skill.name or skill.id,
            description=skill.description[:MAX_DESCRIPTION_CHARS],
            source=SkillSource(kind=SOURCE_YAML, path=skill.source, entry_id=skill.id),
            version=content_version(skill.prompt),
            # YAML 的 `tools.allow` 是**白名单**（限制能用哪些），不是依赖（缺了就跑不了）；
            # 把它当依赖会让技能因为本机没装某个工具而被挡在门外——语义完全不同。
            # 服务器绑定不同：写了 `mcp.servers` 就是声明这条技能要靠这些连接器干活，
            # 连接器没接入时技能确实执行不了，按依赖检查并给出中文原因是诚实的。
            requires=SkillRequirements(mcp=list(skill.mcp_servers)),
            # 平台层 YAML 技能历史上就是「默认关闭、显式引用才生效」；统一清单不借
            # 「进了池子」这件事把它变成对所有专家默认开启。
            default_enabled=False,
            context={
                "mode": skill.context.mode,
                "maxIters": skill.context.max_iters,
                "lazyMcpTools": skill.context.lazy_mcp_tools,
                "contextWindow": skill.context.context_window,
            },
            tools_allow=list(skill.tools_allow),
            tools_deny=list(skill.tools_deny),
            added_at_ms=_now_ms(),
            updated_at_ms=_now_ms(),
        )
        out.append(entry)
    return out, [n for n in notes if n]


# ── 共享池 ─────────────────────────────────────────────────────


@dataclass
class SkillPool:
    entries: dict[str, SkillEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    #: 清单文件本身读坏了：所有结论都要带上这条，不能当没发生过
    load_error: str = ""
    path: str = ""

    # ── 读写 ─────────────────────────────────────────────────

    @classmethod
    def load(cls, home: Path) -> "SkillPool":
        file_path = pool_file(home)
        pool = cls(path=str(file_path))
        if not file_path.is_file():
            return pool
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            pool.load_error = (
                f"共享池清单读坏了（{file_path}）：{_brief(exc)}；"
                "本池按空池处理，既有条目不猜测、也不覆盖写回"
            )
            return pool
        if not isinstance(raw, dict):
            pool.load_error = f"共享池清单根节点不是对象（{file_path}），按空池处理"
            return pool
        try:
            pool.schema_version = int(raw.get("schemaVersion") or SCHEMA_VERSION)
        except (TypeError, ValueError):
            pool.schema_version = SCHEMA_VERSION
        items = raw.get("skills")
        if not isinstance(items, list):
            pool.load_error = f"共享池清单缺少 skills 数组（{file_path}），按空池处理"
            return pool
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = SkillEntry.from_dict(item)
            if not entry.id or not ID_RE.match(entry.id):
                continue
            pool.entries[entry.id] = entry
        return pool

    def save(self, home: Path) -> tuple[bool, str]:
        """原子写：先落临时文件再 replace。清单坏过一次就不许再写第二次覆盖真相。"""
        if self.load_error:
            return False, (
                f"共享池当前带读取错误，拒绝覆盖写入：{self.load_error}；"
                "请先修好或删掉清单文件再重试"
            )
        file_path = pool_file(home)
        payload = {
            "schemaVersion": self.schema_version,
            "updatedAtMs": _now_ms(),
            "skills": [self.entries[k].as_dict() for k in sorted(self.entries)],
        }
        tmp = file_path.with_suffix(f".json.tmp-{os.getpid()}")
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, file_path)
            self.path = str(file_path)
            return True, ""
        except (OSError, ValueError) as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return False, f"共享池清单写入失败：{_brief(exc)}"

    def writable(self) -> bool:
        return not self.load_error

    # ── 同步 ─────────────────────────────────────────────────

    def sync(self, discovered: list[SkillEntry]) -> list[str]:
        """把发现到的技能并进池：新增按引用登记，已有的只更新版本与描述。

        关键约定：**源文件消失时不删条目**，留着并标成「源文件不在」——
        用户可能只是挂了个外接盘，静默删除会把启停状态一起抹掉。
        """
        notes: list[str] = []
        seen: set[str] = set()
        for item in discovered:
            if item.id in seen:
                continue
            seen.add(item.id)
            existing = self.entries.get(item.id)
            if existing is None:
                item.added_at_ms = _now_ms()
                item.updated_at_ms = _now_ms()
                self.entries[item.id] = item
                notes.append(f"共享池新增技能「{item.title}」（{item.id}），来源：{item.source.display()}")
                continue
            if existing.version != item.version:
                notes.append(
                    f"技能「{existing.title}」（{item.id}）正文已更新："
                    f"{existing.version or '（无版本）'} → {item.version}；引用它的专家下次执行自动读到新正文"
                )
            existing.version = item.version
            existing.description = item.description or existing.description
            existing.title = item.title or existing.title
            existing.updated_at_ms = _now_ms()
            if existing.source.path != item.source.path:
                # 同一 id 的来源换了（例如内置 YAML 的同名技能被用户装进了本地目录）：
                # 引用要跟着换，否则池子永远指着老文件；启停状态属于专家，不属于文件，保留。
                notes.append(
                    f"技能「{item.title}」（{item.id}）的来源从 {existing.source.display()} "
                    f"改指到 {item.source.display()}；该 id 已安装的这一份优先"
                )
                existing.source = item.source
        for entry in self.entries.values():
            if entry.id in seen or entry.is_draft():
                continue
            if not entry.exists() and entry.source.kind != SOURCE_YAML:
                notes.append(
                    f"技能「{entry.title}」（{entry.id}）的正文文件当前不在盘上："
                    f"{entry.source.path}；条目保留，等它回来"
                )
        return notes

    def add_to_pool(self, entry: SkillEntry) -> tuple[bool, str]:
        if not entry.id or not ID_RE.match(entry.id):
            return False, (
                f"技能标识「{entry.id}」不合法：只能用字母、数字、点、下划线和横线，最长 64 个字符"
            )
        if not self.writable():
            return False, self.load_error
        existing = self.entries.get(entry.id)
        if existing is not None and existing.source.path != entry.source.path:
            return False, (
                f"共享池里已有同名技能「{entry.id}」（来源 {existing.source.display()}）；"
                "要改引用来源请先从池中摘掉，不静默顶掉别人的技能"
            )
        entry.added_at_ms = entry.added_at_ms or _now_ms()
        entry.updated_at_ms = _now_ms()
        self.entries[entry.id] = entry
        return True, ""

    def drop(self, skill_id: str) -> tuple[bool, str]:
        """从池中摘掉引用。**不是卸载**：源文件一个字都不动。"""
        if not self.writable():
            return False, self.load_error
        if skill_id not in self.entries:
            return False, f"共享池里没有技能「{skill_id}」"
        entry = self.entries.pop(skill_id)
        return True, (
            f"技能「{entry.title}」（{skill_id}）已从共享池摘掉引用；"
            f"原文件保持在 {entry.source.path}，未删除"
        )

    # ── 启停 ─────────────────────────────────────────────────

    def set_enabled(self, skill_id: str, expert_id: str, enabled: bool) -> tuple[bool, str]:
        if not self.writable():
            return False, self.load_error
        entry = self.entries.get(skill_id)
        if entry is None:
            return False, f"共享池里没有技能「{skill_id}」，无法{'启用' if enabled else '停用'}"
        key = str(expert_id or "").strip()
        if not key:
            entry.default_enabled = enabled
            affected = "全部未单独设置的专家"
        else:
            entry.enablements[key] = bool(enabled)
            affected = f"专家「{key}」"
        entry.updated_at_ms = _now_ms()
        if entry.is_draft():
            return True, (
                f"技能「{entry.title}」（{skill_id}）是草案：草案只记录待启用意图，"
                f"未经人工确认不会进入 {affected} 的执行面"
            )
        return True, f"技能「{entry.title}」对 {affected} 已{'启用' if enabled else '停用'}"

    def is_enabled_for(self, entry: SkillEntry, expert_id: str) -> bool:
        key = str(expert_id or "").strip()
        if key in entry.enablements:
            return bool(entry.enablements[key])
        return bool(entry.default_enabled)

    def experts_using(self, skill_id: str) -> list[str]:
        entry = self.entries.get(skill_id)
        if entry is None:
            return []
        on = [k for k, v in entry.enablements.items() if v]
        if entry.default_enabled:
            return sorted(set(on)) or ["（默认启用，未单独设置的专家都用）"]
        return sorted(set(on))

    # ── 依赖检查与执行面装配 ───────────────────────────────────

    def check_dependencies(
        self, entry: SkillEntry, expert_id: str,
        available_tools: set[str] | None, available_mcp: set[str] | None,
    ) -> tuple[bool, list[str]]:
        """启用前检查技能 / MCP / 工具依赖；缺就给出中文原因。

        `available_*` 为 None 表示调用方没给这一族的可用清单 ⇒ **不做判断也不谎称通过**，
        而是说明「这一类依赖未提供可用清单，未检查」。
        """
        missing: list[str] = []
        unchecked: list[str] = []
        if entry.requires.tools:
            if available_tools is None:
                unchecked.append("工具依赖未检查（调用方没有给出可用工具清单）")
            else:
                gone = [t for t in entry.requires.tools if t not in available_tools]
                if gone:
                    missing.append("缺少工具：" + "、".join(gone))
        if entry.requires.mcp:
            if available_mcp is None:
                unchecked.append("MCP 依赖未检查（调用方没有给出可用连接器清单）")
            else:
                gone = [m for m in entry.requires.mcp if m not in available_mcp]
                if gone:
                    missing.append("缺少 MCP 连接器：" + "、".join(gone))
        if entry.requires.skills:
            gone = [
                s for s in entry.requires.skills
                if s not in self.entries or not self.is_enabled_for(self.entries[s], expert_id)
            ]
            if gone:
                missing.append("缺少已启用的前置技能：" + "、".join(gone))
        if missing:
            return False, [
                f"技能「{entry.title}」（{entry.id}）未进入本轮执行面 —— " + "；".join(missing),
                *unchecked,
            ]
        return True, unchecked

    def resolve_for_expert(
        self, expert_id: str,
        available_tools: set[str] | None = None,
        available_mcp: set[str] | None = None,
    ) -> tuple[list[dict], list[str]]:
        """装配某个专家本轮可用的技能清单（`normalize_skills` 的输入形状）。

        被跳过的一条都不许悄悄消失：草案、缺依赖、正文读不到、扫描判定高风险、
        源文件不在——五种情况各给一句中文说明。
        """
        skills: list[dict] = []
        notes: list[str] = []
        for entry in sorted(self.entries.values(), key=lambda e: e.id):
            if not self.is_enabled_for(entry, expert_id):
                continue
            if entry.is_draft():
                notes.append(
                    f"技能「{entry.title}」（{entry.id}）是未确认的草案，未进入执行面；"
                    "人工确认后在共享池里启用"
                )
                continue
            ok, reasons = self.check_dependencies(entry, expert_id, available_tools, available_mcp)
            notes.extend(reasons)
            if not ok:
                continue
            if entry.scan_blocked is True:
                skills.append(self._runtime_row(entry, "", error=(
                    "静态安全扫描判定为高风险，正文未进入执行面；请修改后重新安装"
                ), scan_blocked=True))
                notes.append(
                    f"技能「{entry.title}」（{entry.id}）被安全扫描判定为高风险，正文不可用"
                )
                continue
            body, error = read_body(entry)
            if error:
                skills.append(self._runtime_row(entry, "", error=error))
                notes.append(f"技能「{entry.title}」（{entry.id}）未提供正文：{error}")
                continue
            if not body.strip():
                # 正文一片空白：Rust 会把原样文件交给模型，这里不拦（拦了就是两条链路
                # 行为不一致），但必须把「这条技能其实什么也没说」写在 error 里。
                blank = "技能正文是空的（除空白字符外没有内容），模型读到的是空提示；请补全后重新安装"
                skills.append(self._runtime_row(entry, body, error=blank))
                notes.append(f"技能「{entry.title}」（{entry.id}）正文为空，进入执行面也不起作用")
                continue
            skills.append(self._runtime_row(entry, body))
        return skills, notes

    @staticmethod
    def _runtime_row(entry: SkillEntry, body: str, error: str = "", scan_blocked: bool = False) -> dict:
        return {
            "id": entry.id,
            "name": entry.title or entry.id,
            "description": entry.description,
            "content": body,
            "kind": "skill",
            "source": entry.source.kind,
            "suite": "",
            "error": error,
            "scanBlocked": scan_blocked,
        }


def load_pool(home: Path) -> SkillPool:
    return SkillPool.load(home)


def sync_pool(home: Path) -> tuple[SkillPool, list[str]]:
    """发现 → 并入 → 落盘。返回池与这一轮对用户说的话。"""
    pool = SkillPool.load(home)
    installed, installed_notes = discover_installed(home)
    yaml_entries, yaml_notes = discover_yaml(home)
    # 同 id 撞车时：已安装目录是用户显式装的那一份，优先于平台内置 YAML
    by_source: dict[str, SkillEntry] = {}
    for entry in yaml_entries:
        by_source[entry.id] = entry
    for entry in installed:
        by_source[entry.id] = entry
    notes = pool.sync(list(by_source.values()))
    notes = installed_notes + yaml_notes + notes
    if pool.writable():
        saved, error = pool.save(home)
        if not saved:
            notes.append(error)
    else:
        notes.append(pool.load_error)
    return pool, notes


# ── 草案：成功任务与浏览器录制的落点 ──────────────────────────


def _scalar(value: str) -> str:
    """frontmatter 单行标量：去掉会让解析器误判为折叠块的起始符号"""
    return str(value or "").strip().lstrip(">|").strip()


def add_draft(
    home: Path, skill_id: str, body: str,
    title: str = "", description: str = "", origin: str = "任务蒸馏",
) -> tuple[SkillEntry | None, str]:
    """把草案技能写进共享池目录，状态 `draft`。

    草案**永远不会**因为「写出来了」就出现在执行面里：它只落在
    `{home}/skill_pool/drafts/{id}/SKILL.md`，等人确认。
    """
    clean = str(skill_id or "").strip()
    if not ID_RE.match(clean):
        return None, (
            f"草案标识「{clean}」不合法：只能用字母、数字、点、下划线和横线，最长 64 个字符"
        )
    text = str(body or "").strip()
    if not text:
        return None, "草案正文为空，不写入：请先确认这次任务真的产出了可复用的步骤"
    if len(text) > MAX_DRAFT_CHARS:
        return None, f"草案正文超过 {MAX_DRAFT_CHARS} 字上限（实际 {len(text)} 字），请精简后重试"
    directory = drafts_dir(home) / clean
    skill_md = directory / SKILL_MD_NAME
    if skill_md.exists():
        return None, (
            f"草案「{clean}」已存在于 {skill_md}；不覆盖已有草案，"
            "要改请先人工处理那份文件"
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"草案目录创建失败：{_brief(exc)}"
    name = _scalar(title) or clean
    desc = _scalar(description) or _scalar(_brief(text, MAX_DESCRIPTION_CHARS))
    payload = "\n".join([
        "---",
        f"name: {name}",
        "status: draft",
        f"description: {desc}",
        f"origin: {_scalar(str(origin or '任务蒸馏'))}",
        "---",
        "",
        text,
        "",
    ])
    try:
        skill_md.write_text(payload, encoding="utf-8")
    except OSError as exc:
        return None, f"草案写入失败：{_brief(exc)}"

    pool = SkillPool.load(home)
    if not pool.writable():
        return None, pool.load_error
    entry = SkillEntry(
        id=clean,
        title=name,
        description=desc,
        source=SkillSource(kind=SOURCE_DRAFT, path=str(skill_md)),
        version=content_version(payload),
        status=STATUS_DRAFT,
        # 草案的缺省就是「不启用」：即使某个专家默认启用一切，也不该把没审过的东西塞进去
        default_enabled=False,
    )
    ok, error = pool.add_to_pool(entry)
    if not ok:
        return None, error
    saved, error = pool.save(home)
    if not saved:
        return None, error
    return entry, (
        f"草案技能「{name}」（{clean}）已写入 {skill_md}，状态为未确认；"
        "它不会自动进入任何专家的执行面，需人工确认后启用"
    )


def promote_draft(home: Path, skill_id: str, reviewer: str = "") -> tuple[bool, str]:
    """人工确认草案：状态转 ready，并复制进 Rust 的技能目录让两条链路都看得见。

    复制而非移动：草案原件留在池里做审计痕迹。目标已存在时**拒绝覆盖**——
    与 Rust `skill_install` 的「拒绝覆盖同名技能」同一条规矩。
    """
    pool = SkillPool.load(home)
    if not pool.writable():
        return False, pool.load_error
    entry = pool.entries.get(skill_id)
    if entry is None:
        return False, f"共享池里没有技能「{skill_id}」"
    if not entry.is_draft():
        return False, f"技能「{entry.title}」（{skill_id}）已经是正式技能，无需确认"
    body, error = read_body(entry)
    if error:
        return False, f"草案正文不可用，不能确认：{error}"

    target_dir = installed_root(home) / entry.id
    target = target_dir / SKILL_MD_NAME
    if target.exists():
        return False, (
            f"技能目录里已有同名技能「{entry.id}」（{target}）；确认操作拒绝覆盖，"
            "请先给草案换个标识或处理掉已有的那一份"
        )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
    except OSError as exc:
        return False, f"草案确认落盘失败：{_brief(exc)}"

    entry.status = STATUS_READY
    entry.source = SkillSource(kind=SOURCE_SKILL_MD, path=str(target))
    entry.version = content_version(body)
    entry.updated_at_ms = _now_ms()
    saved, error = pool.save(home)
    if not saved:
        return False, error
    who = str(reviewer or "").strip()
    return True, (
        f"草案「{entry.title}」（{entry.id}）已确认为正式技能，正文同步到 {target}"
        + (f"；确认人：{who}" if who else "")
        + "。引用它的专家下一轮就能读到，但启停仍按各专家自己的设置"
    )


# ── 视图 ───────────────────────────────────────────────────────


def pool_view(home: Path) -> dict:
    """给页面用的只读视图（含每条技能的来源与生效专家，方便核「改一次两边生效」）"""
    pool = SkillPool.load(home)
    rows = []
    for entry in sorted(pool.entries.values(), key=lambda e: e.id):
        row = entry.as_dict()
        row["experts"] = pool.experts_using(entry.id)
        row["sourceExists"] = Path(entry.source.path).is_file()
        rows.append(row)
    return {
        "schemaVersion": pool.schema_version,
        "poolFile": pool.path or str(pool_file(home)),
        "writable": pool.writable(),
        "loadError": pool.load_error,
        "skills": rows,
    }
