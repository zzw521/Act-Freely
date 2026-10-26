"""执行面 P1-9：工作区沙箱策略进入真实工具调用点

Rust 每次提交任务时随 runtime 下发 `sandbox` 段（security_policies 的 `sandbox:{ws}`）：

    sandbox = { workspaceId, configured, executionLevel,
                toolAllowlist, toolBlocklist, fileAllowlist, fileBlocklist,
                error? }

本模块把它变成两件有约束力的事实：

1. 工具门禁（Toolkit 中间件）—— 白名单外的工具、黑名单内的工具**不执行**，
   拒绝原因作为工具结果回给模型，同时发出 `sandbox_denied` 事件由 Rust 落审计；
2. 文件边界（内置文件工具 + 中间件对 file_path/path 入参的检查）——
   黑名单优先，其次按执行等级限制可访问目录。

铁律：策略读不出来就 fail-closed（拒绝全部工具调用并说明原因），绝不静默放行；
没有下发 sandbox 段（浏览器直连 / IM 通道 / 旧版本壳）时按「未配置」处理，不虚构限制。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 内置会写盘的工具（L0 只读模式据此拒绝；中间件与工具内守卫共用同一口径）
MUTATING_TOOLS = frozenset({"write_text_file", "insert_text_file", "computer_action"})
# 中间件会按沙箱校验的路径入参键（内置工具与常见文件类 MCP 工具）
PATH_KEYS = ("file_path", "path", "filePath")
# 工具名匹配忽略大小写（工具名是标识符，不做大小写敏感的特权通道）
EXECUTION_LEVELS = ("L0", "L1", "L2")


def _brief(text: str, limit: int = 200) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _norm_path(raw) -> str:
    """统一分隔符、去尾部斜杠；Windows 下转小写（文件系统不区分大小写）"""
    s = str(raw or "").strip().replace("\\", "/")
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s.lower() if os.name == "nt" else s


def _glob(pattern: str, text: str) -> bool:
    """轻量 glob：`*` 不跨目录、`**` 跨目录、`?` 单个非分隔字符"""
    if not pattern:
        return not text
    if pattern.startswith("**"):
        rest = pattern[2:]
        if _glob(rest, text):
            return True
        return any(_glob(rest, text[i + 1:]) for i in range(len(text)))
    if pattern[0] == "*":
        rest = pattern[1:]
        if _glob(rest, text):
            return True
        for i, ch in enumerate(text):
            if ch == "/":
                break
            if _glob(rest, text[i + 1:]):
                return True
        return False
    if pattern[0] == "?":
        return bool(text) and text[0] != "/" and _glob(pattern[1:], text[1:])
    return bool(text) and text[0] == pattern[0] and _glob(pattern[1:], text[1:])


def _rule_hit(rule: str, path: str) -> bool:
    """路径规则命中：无通配符按目录前缀，`dir/**` 含目录本身，其余按 glob"""
    r = _norm_path(rule)
    if not r:
        return False
    p = _norm_path(path)
    if r.endswith("/**"):
        base = r[:-3]
        if p == base or p.startswith(base + "/"):
            return True
    if "*" in r or "?" in r:
        return _glob(r, p)
    return p == r or p.startswith(r + "/") or (r == "/" and p.startswith("/"))


def _tool_hit(rule: str, name: str) -> bool:
    r = str(rule or "").strip().lower()
    n = str(name or "").strip().lower()
    if not r or not n:
        return False
    if "*" in r or "?" in r:
        return _glob(r, n)
    return r == n


def _str_list(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _resolve(path, base: Path | None) -> Path:
    p = Path(str(path)).expanduser()
    if not p.is_absolute() and base is not None:
        p = base / p
    try:
        return p.resolve()
    except Exception:  # noqa: BLE001  解析失败时按未规范化路径比对即可，不阻断拒绝判定
        return p


@dataclass
class SandboxPolicy:
    workspace_id: str = ""
    configured: bool = False
    error: str = ""
    execution_level: str = "L1"
    tool_allowlist: list[str] = field(default_factory=list)
    tool_blocklist: list[str] = field(default_factory=list)
    file_allowlist: list[str] = field(default_factory=list)
    file_blocklist: list[str] = field(default_factory=list)
    work_dir: Path | None = None
    _seq: int = 0

    # ── 解析 ────────────────────────────────────────────────────

    @classmethod
    def from_runtime(cls, runtime: dict | None, work_dir: Path | None = None) -> "SandboxPolicy":
        if work_dir is not None:
            try:
                work_dir = Path(work_dir).resolve()
            except Exception:  # noqa: BLE001
                work_dir = Path(work_dir)
        block = (runtime or {}).get("sandbox") if isinstance(runtime, dict) else None
        if isinstance(block, str):
            try:
                block = json.loads(block)
            except Exception:  # noqa: BLE001
                block = {"error": f"沙箱配置不是合法 JSON：{_brief(block)}"}
        if block is None:
            return cls(work_dir=work_dir)
        if not isinstance(block, dict):
            return cls(
                configured=True,
                error="沙箱配置格式非法（不是对象）",
                work_dir=work_dir,
            )

        err = str(block.get("error") or "").strip()
        level = str(block.get("executionLevel") or "L1").strip() or "L1"
        policy = cls(
            workspace_id=str(block.get("workspaceId") or "").strip(),
            configured=bool(block.get("configured")) or bool(err),
            error=err,
            execution_level=level if level in EXECUTION_LEVELS else "L1",
            tool_allowlist=_str_list(block.get("toolAllowlist")),
            tool_blocklist=_str_list(block.get("toolBlocklist")),
            file_allowlist=_str_list(block.get("fileAllowlist")),
            file_blocklist=_str_list(block.get("fileBlocklist")),
            work_dir=work_dir,
        )
        if not err and level not in EXECUTION_LEVELS:
            policy.error = f"未知的沙箱执行等级「{level}」"
        return policy

    # ── 判定 ────────────────────────────────────────────────────

    def tool_denial(self, name: str) -> tuple[str, str, str] | None:
        """(原因, 来源, 命中规则)；None 表示放行"""
        if self.error:
            return (
                f"沙箱策略读取失败，已按 fail-closed 拒绝本轮所有工具调用：{self.error}",
                "policy_error",
                "",
            )
        if not self.configured:
            return None
        for rule in self.tool_blocklist:
            if _tool_hit(rule, name):
                return (f"工具「{name}」命中沙箱工具黑名单（{rule}）", "tool_blocklist", rule)
        if self.tool_allowlist and not any(_tool_hit(r, name) for r in self.tool_allowlist):
            allowed = "、".join(self.tool_allowlist[:8])
            more = f" 等 {len(self.tool_allowlist)} 项" if len(self.tool_allowlist) > 8 else ""
            return (
                f"工具「{name}」不在沙箱工具白名单内（允许：{allowed}{more}）",
                "tool_allowlist",
                "",
            )
        if self.execution_level == "L0" and name in MUTATING_TOOLS:
            return (f"沙箱为 L0 只读，写操作工具「{name}」不可用", "execution_level", "L0")
        return None

    def file_denial(
        self, path, write: bool = False, base: Path | None = None
    ) -> tuple[str, str, str] | None:
        """文件路径判定：黑名单优先；L0 拒绝写；L2 不做目录限制（黑名单仍生效）"""
        if self.error:
            return (
                f"沙箱策略读取失败，已按 fail-closed 拒绝文件访问：{self.error}",
                "policy_error",
                "",
            )
        if not self.configured:
            return None
        if write and self.execution_level == "L0":
            return ("沙箱为 L0 只读，不允许写入文件", "execution_level", "L0")

        resolved = _resolve(path, base or self.work_dir)
        raw_text = _norm_path(path)
        resolved_text = _norm_path(resolved)
        for rule in self.file_blocklist:
            if _rule_hit(rule, raw_text) or _rule_hit(rule, resolved_text):
                return (f"路径命中沙箱文件黑名单（{rule}）", "file_blocklist", rule)
        if self.execution_level == "L2":
            return None

        roots: list[str] = []
        if self.work_dir is not None:
            roots.append(str(self.work_dir))
        roots.extend(self.file_allowlist)
        if not roots:
            return (f"路径不在允许范围内：{resolved_text}", "file_allowlist", "")
        if any(_rule_hit(root, resolved_text) for root in roots):
            return None
        shown = "、".join(roots[:5])
        more = f" 等 {len(roots)} 项" if len(roots) > 5 else ""
        return (
            f"路径不在工作目录或文件白名单内（允许：{shown}{more}）",
            "file_allowlist",
            "",
        )

    # ── 可见说明与事件 ───────────────────────────────────────────

    def notes(self) -> list[str]:
        """开跑前把生效中的沙箱约束摊进上下文说明，避免用户以为策略没生效"""
        if not self.configured:
            return []
        if self.error:
            return [f"沙箱策略读取失败，本轮所有工具与文件访问会被拒绝：{_brief(self.error)}"]
        out: list[str] = []
        if self.execution_level == "L0":
            out.append("沙箱为 L0 只读：写文件工具会被拒绝")
        elif self.execution_level == "L2":
            out.append("沙箱为 L2：文件路径不受工作目录限制（文件黑名单仍生效）")
        if self.tool_allowlist:
            out.append(
                f"工具白名单已生效：仅 {'、'.join(self.tool_allowlist[:8])} 可调用，其余工具会被拒绝"
            )
        if self.tool_blocklist:
            out.append(f"工具黑名单已生效：{'、'.join(self.tool_blocklist[:8])} 会被拒绝")
        if self.file_allowlist:
            out.append(f"文件白名单已生效：额外允许 {len(self.file_allowlist)} 个路径")
        if self.file_blocklist:
            out.append(f"文件黑名单已生效：{'、'.join(self.file_blocklist[:5])} 会被拒绝")
        return out

    def denial_text(self, tool: str, denial: tuple[str, str, str]) -> str:
        reason = denial[0]
        return (
            "<system-info>本次工具调用被沙箱策略拒绝，"
            f"工具 {tool} 没有执行。原因：{reason}</system-info>"
        )

    def report(
        self,
        emit: Callable[[str, dict], None],
        task_id: str,
        tool: str,
        denial: tuple[str, str, str],
    ) -> None:
        reason, source, rule = denial
        self._seq += 1
        emit("sandbox_denied", {
            "workspaceId": self.workspace_id,
            "taskId": task_id,
            "tool": tool,
            "reason": reason,
            "source": source,
            "rule": rule,
            "executionLevel": self.execution_level,
        })
        # 同时摊成步骤事件：审计页能查到，对话里也要当场看得见
        step_id = f"sandbox-{self._seq}"
        note = f"沙箱拦截「{tool}」：{_brief(reason, 160)}"
        emit("step_start", {"step_id": step_id, "stepType": "sandbox", "description": note})
        emit("step_done", {"step_id": step_id, "output": note})

    # ── 安装 ────────────────────────────────────────────────────

    def install(
        self,
        toolkit,
        emit: Callable[[str, dict], None],
        task_id: str = "",
    ) -> str | None:
        """把工具门禁注册进本轮 Toolkit；返回错误说明或 None。

        已配置策略却装不上门禁时必须让调用方中止本轮，而不是无约束继续跑。
        """
        if not self.configured:
            return None
        try:
            toolkit.register_middleware(tool_middleware(self, emit, task_id))
            return None
        except Exception as exc:  # noqa: BLE001
            return _brief(str(exc))


def tool_middleware(policy: SandboxPolicy, emit: Callable[[str, dict], None], task_id: str = ""):
    """工具门禁中间件：工具白/黑名单 + 文件路径沙箱（AgentScope 洋葱中间件）"""

    async def middleware(kwargs: dict, next_handler):
        from agentscope.message import TextBlock
        from agentscope.tool import ToolResponse

        tool_call = kwargs.get("tool_call") or {}
        name = str(tool_call.get("name") or "")
        args = tool_call.get("input") or {}

        denial = policy.tool_denial(name)
        if denial is None and isinstance(args, dict):
            for key in PATH_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    denial = policy.file_denial(value, write=name in MUTATING_TOOLS)
                    if denial is not None:
                        break
        if denial is not None:
            policy.report(emit, task_id, name, denial)
            yield ToolResponse(content=[TextBlock(type="text", text=policy.denial_text(name, denial))])
            return

        async for resp in await next_handler(**kwargs):
            yield resp

    return middleware
