"""执行面 P1-9：工作区沙箱策略进入真实工具调用点

Rust 每次提交任务时随 runtime 下发 `sandbox` 段（security_policies 的 `sandbox:{ws}`）：

    sandbox = { workspaceId, configured, executionLevel,
                toolAllowlist, toolBlocklist, fileAllowlist, fileBlocklist,
                error? }

本模块把它变成两件有约束力的事实：

1. 工具门禁（Toolkit 中间件）—— 白名单外的工具、黑名单内的工具**不执行**，
   拒绝原因作为工具结果回给模型，同时发出 `sandbox_denied` 事件由 Rust 落审计；
2. 文件边界（内置文件工具 + 中间件对 file_path/path 入参的检查）——
   黑名单优先，其次按执行等级限制可访问目录；
3. 文件访问档位（顶层 `runtime.filePermission`：每次询问 / 自动审批 / 完全访问）——
   对话输入框里选的那一档在这里落地：前两档写盘限定在绑定工作目录（含工作区文件白名单）内，
   完全访问放开越界但家目录/系统关键目录/引擎数据目录仍在硬拒清单内，删除文件只有完全访问允许。
   这一条与 `sandbox` 段无关：壳侧没下发沙箱配置时，档位照样生效。

铁律：策略读不出来就 fail-closed（拒绝全部工具调用并说明原因），绝不静默放行；
没有下发 sandbox 段（浏览器直连 / IM 通道 / 旧版本壳）时按「未配置沙箱」处理，不虚构限制
（文件访问档位仍按顶层 `runtime.filePermission` 判定，缺省即最保守的「每次询问」边界）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import side_effect_registry as ser  # #164：写盘面/敏感名单的唯一落表处，本文件按视图派生

# 内置会写盘的工具（L0 只读模式据此拒绝；中间件与工具内守卫共用同一口径）
# generate_xlsx / generate_docx 只在通用与写作模式注册，但名单是全量的：
# L0 判定按名字查表，工具没注册时多列一条不会放行任何东西。
# 成员本体在 engine/side_effect_registry.py 的 SANDBOX_MUTATING（含 computer_action，
# 不含 spawn_subagents —— 派发子 Agent 是编排面上的事，不经过文件边界判定）。
MUTATING_TOOLS = ser.view("sandbox_mutating")
# 执行类工具：不在文件写入判定里，但 L0 只读必须一并拒绝
EXECUTION_TOOLS = frozenset({"run_command", "run_shell"})
# 删文件类工具：比写盘更高一档，只有 full_access 允许
DELETING_TOOLS = frozenset({"delete_text_file"})
# 中间件会按沙箱校验的路径入参键（内置工具与常见文件类 MCP 工具）
PATH_KEYS = ("file_path", "path", "filePath")
# 工具名匹配忽略大小写（工具名是标识符，不做大小写敏感的特权通道）
EXECUTION_LEVELS = ("L0", "L1", "L2")

# ── 文件访问档位（与渲染层 FilePermissionMode 同名同义，跨语言只此一套字面量） ──

FILE_PERMISSION_MODES = ("ask_every_time", "auto_approve", "full_access")
DEFAULT_FILE_PERMISSION = "ask_every_time"
FILE_PERMISSION_LABELS = {
    "ask_every_time": "每次询问",
    "auto_approve": "自动审批",
    "full_access": "完全访问",
}

# 硬拒清单：任何档位都不许越过（full_access 也只是放开「工作目录之外」，不是放开整机）。
# 家目录整体钉死是刻意的：会话可以把工作目录选在家目录里（那是用户在目录选择器里
# 明确表过态），但任何「越出工作目录」的落点若落进家目录仍拒绝，避免完全访问顺带放开整个 home。
_SYSTEM_DENY_ROOTS = (
    "c:/windows",
    "c:/program files",
    "c:/program files (x86)",
    "c:/programdata",
    "c:/$recycle.bin",
    "c:/system volume information",
    "/system",
    "/usr",
    "/etc",
    "/bin",
    "/sbin",
    "/lib",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var",
    "/root",
    "/private/var/root",
)


def normalize_file_permission(raw) -> str:
    """非法/缺失一律回到最保守档；绝不因为「没传」而放开"""
    value = str(raw or "").strip().lower()
    return value if value in FILE_PERMISSION_MODES else DEFAULT_FILE_PERMISSION


def ask_state(block) -> tuple[str, str, str, str]:
    """从 runtime.toolApproval 段取「能不能弹确认框」。

    返回 (gateUrl, gateToken, approvalMode, 不可用原因)。

    这里刻意不要求 toolApproval.mode ≠ off：对话里的「每次询问」也走这条回环网关，
    壳侧需要在档位为 off 但文件权限要求逐次确认时照样把 gateUrl/gateToken 下发。
    mode 原样带出，中间件据此判断某个工具是不是已经被逐工具审批弹过一次，
    避免同一件事问两遍。
    """
    if not isinstance(block, dict):
        return "", "", "off", "本轮没有下发确认网关"
    mode = str(block.get("mode") or "off").strip().lower()
    if mode not in ("off", "sensitive", "all"):
        mode = "off"
    gate_url = str(block.get("gateUrl") or "").strip()
    gate_token = str(block.get("gateToken") or "").strip()
    gate_error = str(block.get("gateError") or "").strip()
    if gate_error:
        return "", "", mode, gate_error
    if not _loopback_ok(gate_url) or not gate_token:
        return "", "", mode, "本机确认网关未启动或地址无效"
    return gate_url, gate_token, mode, ""


def _loopback_ok(url: str) -> bool:
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:  # noqa: BLE001
        return False
    return parts.scheme == "http" and parts.hostname == "127.0.0.1" and bool(parts.port)


def _deny_roots() -> list[str]:
    """家目录 + 引擎数据目录 + 系统关键目录，统一成 _rule_hit 可比对的形式"""
    roots: list[str] = []
    try:
        roots.append(str(Path.home()))
    except Exception:  # noqa: BLE001  拿不到家目录时不放大权限，只是少一条钉死项
        pass
    try:
        import data_paths  # noqa: PLC0415  引擎数据目录（与产出物台账同根）

        roots.append(str(data_paths.engine_home()))
    except Exception:  # noqa: BLE001
        pass
    roots.extend(_SYSTEM_DENY_ROOTS)
    return [_norm_path(r) for r in roots if str(r or "").strip()]


def hard_deny_hit(path, base: Path | None = None) -> str:
    """命中硬拒清单返回该根路径，未命中返回空串"""
    resolved_text = _norm_path(_resolve(path, base))
    for root in _deny_roots():
        if not root:
            continue
        if resolved_text == root or resolved_text.startswith(root + "/"):
            return root
    return ""



def _brief(text: str, limit: int = 200) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _strip_namespace_prefix(s: str) -> str:
    r"""剥掉 Windows 命名空间前缀，再进分隔符归一。

    `Path.resolve()` 在长路径/UNC 上可能给出 `\\?\C:\...`、`\\?\UNC\server\share`、
    `\\.\C:\...`。前缀会让同一个系统目录多出一副面孔，硬拒清单按 `c:/windows` 比对
    时就会漏掉 `\\?\c:/windows\...`——与 Rust 侧 `security::file_perm::key()` 同规则。
    """
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[len("\\\\?\\UNC\\"):]
    if s.startswith("\\\\?\\"):
        return s[len("\\\\?\\"):]
    if s.startswith("\\\\.\\"):
        return s[len("\\\\.\\"):]
    return s


def _norm_path(raw) -> str:
    """统一分隔符、去尾部斜杠；Windows 下转小写（文件系统不区分大小写）"""
    s = _strip_namespace_prefix(str(raw or "").strip()).replace("\\", "/")
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
    # 文件访问档位（对话输入框选择，顶层 runtime.filePermission；与 sandbox 段无关）
    file_permission: str = DEFAULT_FILE_PERMISSION
    gate_url: str = ""
    gate_token: str = ""
    approval_mode: str = "off"
    ask_error: str = ""
    _ask_seq: int = 0
    _seq: int = 0

    # ── 解析 ────────────────────────────────────────────────────

    @classmethod
    def from_runtime(cls, runtime: dict | None, work_dir: Path | None = None) -> "SandboxPolicy":
        if work_dir is not None:
            try:
                work_dir = Path(work_dir).resolve()
            except Exception:  # noqa: BLE001
                work_dir = Path(work_dir)
        bundle = runtime if isinstance(runtime, dict) else {}
        gate_url, gate_token, approval_mode, ask_error = ask_state(bundle.get("toolApproval"))
        common = dict(
            file_permission=normalize_file_permission(
                bundle.get("filePermission") or bundle.get("file_permission")
            ),
            gate_url=gate_url,
            gate_token=gate_token,
            approval_mode=approval_mode,
            ask_error=ask_error,
            work_dir=work_dir,
        )
        block = bundle.get("sandbox")
        if isinstance(block, str):
            try:
                block = json.loads(block)
            except Exception:  # noqa: BLE001
                block = {"error": f"沙箱配置不是合法 JSON：{_brief(block)}"}
        if block is None:
            return cls(**common)
        if not isinstance(block, dict):
            return cls(
                configured=True,
                error="沙箱配置格式非法（不是对象）",
                **common,
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
            **common,
        )
        if not err and level not in EXECUTION_LEVELS:
            policy.error = f"未知的沙箱执行等级「{level}」"
        return policy

    # ── 判定 ────────────────────────────────────────────────────

    def ask_available(self) -> bool:
        return bool(self.gate_url and self.gate_token)

    def should_ask(self, name: str) -> bool:
        """本轮是否需要为这次写盘弹确认框"""
        if self.file_permission != "ask_every_time" or not self.ask_available():
            return False
        if name not in MUTATING_TOOLS:
            return False
        try:
            import tool_approval as tap  # noqa: PLC0415

            if tap.needs_approval(name, self.approval_mode):
                # 逐工具审批已经为同一个工具弹窗，不重复打扰用户
                return False
        except Exception:  # noqa: BLE001  取不到审批模块时按「没弹过」处理，宁可多问一次
            pass
        return True

    def in_allowed_area(self, resolved_text: str) -> bool:
        """绑定工作目录 + 工作区文件白名单构成「目录内」"""
        if self.work_dir is not None and _rule_hit(str(self.work_dir), resolved_text):
            return True
        return any(_rule_hit(rule, resolved_text) for rule in self.file_allowlist)

    def permission_denial(
        self, resolved: Path, resolved_text: str, write: bool = False, delete: bool = False
    ) -> tuple[str, str, str] | None:
        """按文件访问档位判定；只介入写/删，读始终放行。

        分档口径（与渲染层档位说明一一对应）：
        - 每次询问 / 自动审批：可写范围 = 绑定工作目录 ∪ 工作区文件白名单，越界即拒；
        - 完全访问：越界也放开，但家目录/系统关键目录/引擎数据目录仍在硬拒清单内；
        - 删除文件：只有完全访问允许，任何档位下目录内删除同样要求完全访问。
        """
        if not (write or delete):
            return None
        mode = self.file_permission
        label = FILE_PERMISSION_LABELS.get(mode, mode)
        action = "删除" if delete else "写入"
        if self.in_allowed_area(resolved_text):
            if delete and mode != "full_access":
                return (
                    f"当前文件访问档位是「{label}」，删除文件需要切到「完全访问」；文件未被删除：{resolved_text}",
                    "file_permission",
                    "delete_requires_full_access",
                )
            return None
        # 越出绑定目录（含工作区白名单）
        if delete and mode != "full_access":
            return (
                f"当前文件访问档位是「{label}」，删除文件需要切到「完全访问」；文件未被删除：{resolved_text}",
                "file_permission",
                "delete_requires_full_access",
            )
        hit = hard_deny_hit(resolved, self.work_dir)
        if hit:
            return (
                f"路径落进硬拒清单（{hit}），任何档位都不允许{action}；"
                f"要在此目录外产出文件，请把工作目录绑到那里。被拒路径：{resolved_text}",
                "file_permission",
                "hard_deny",
            )
        if mode != "full_access":
            boundary = str(self.work_dir) if self.work_dir is not None else "（未绑定工作目录）"
            return (
                f"当前文件访问档位是「{label}」，只允许{action}绑定工作目录（{boundary}）"
                f"及工作区文件白名单内的文件；要越出目录请在对话输入框切到「完全访问」。被拒路径：{resolved_text}",
                "file_permission",
                "outside_boundary",
            )
        return None

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
        if self.execution_level == "L0" and name in EXECUTION_TOOLS:
            return (f"沙箱为 L0 只读，执行工具「{name}」不可用", "execution_level", "L0")
        return None

    def file_denial(
        self, path, write: bool = False, base: Path | None = None, delete: bool = False
    ) -> tuple[str, str, str] | None:
        """文件路径判定：先按对话里的文件访问档位，再按工作区沙箱策略。

        档位判定刻意放在 `configured` 早退之前：没有下发 sandbox 段（浏览器直连 /
        IM 通道 / 旧版本壳）时，用户在对话框里选的档位仍然是本轮的真实约束，
        不能因为「壳没配沙箱」就当成没有限制。
        """
        if self.error:
            return (
                f"沙箱策略读取失败，已按 fail-closed 拒绝文件访问：{self.error}",
                "policy_error",
                "",
            )
        resolved = _resolve(path, base or self.work_dir)
        permission = self.permission_denial(
            resolved, _norm_path(resolved), write=write, delete=delete
        )
        if permission is not None:
            return permission
        if not self.configured:
            return None
        if write and self.execution_level == "L0":
            return ("沙箱为 L0 只读，不允许写入文件", "execution_level", "L0")

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

    def permission_notes(self) -> list[str]:
        """档位摊进上下文说明：让用户当场看到这一轮到底被限制在哪"""
        mode = self.file_permission
        label = FILE_PERMISSION_LABELS.get(mode, mode)
        if mode == "full_access":
            return [
                f"文件访问档位「{label}」已生效：允许越出绑定工作目录读写、删除文件；"
                "家目录、系统关键目录与引擎数据目录仍在硬拒清单内，任何档位都不放开。"
            ]
        boundary = str(self.work_dir) if self.work_dir is not None else "（未绑定工作目录）"
        line = (
            f"文件访问档位「{label}」已生效：写入限定在绑定工作目录（{boundary}）"
            "与工作区文件白名单内；删除文件在任何档位下都需要「完全访问」。"
        )
        if mode == "ask_every_time":
            line += (
                "目录内每次写盘前会弹系统确认框，拒绝即不执行。"
                if self.ask_available()
                else (
                    f"当前无法弹确认框（{self.ask_error or '确认网关未启动'}）："
                    "目录内写盘不弹窗、直接执行，越出目录仍按上面的边界拒绝；"
                    "要真正逐次确认，请在设置页开启「逐工具审批」。"
                )
            )
        return [line]

    # ── 可见说明与事件 ───────────────────────────────────────────

    def notes(self) -> list[str]:
        """开跑前把生效中的约束摊进上下文说明，避免用户以为策略没生效"""
        permission = self.permission_notes()
        if not self.configured:
            return permission
        if self.error:
            return permission + [
                f"沙箱策略读取失败，本轮所有工具与文件访问会被拒绝：{_brief(self.error)}"
            ]
        out: list[str] = list(permission)
        if self.execution_level == "L0":
            out.append("沙箱为 L0 只读：写文件工具会被拒绝")
        elif self.execution_level == "L2":
            out.append("沙箱为 L2：文件路径不受工作目录限制（文件黑名单仍生效）")
            if self.file_permission != "full_access":
                out.append(
                    "工作区沙箱为 L2（目录不限制），但对话里的文件访问档位更严："
                    "写盘仍限定在绑定目录与工作区文件白名单内"
                )
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
        reason, source, _rule = denial
        who = (
            "文件访问权限档位"
            if source in ("file_permission", "file_permission_ask", "user_denied")
            else "沙箱策略"
        )
        return (
            f"<system-info>本次工具调用被{who}拒绝，"
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
            "filePermission": self.file_permission,
        })
        # 同时摊成步骤事件：审计页能查到，对话里也要当场看得见
        step_id = f"sandbox-{self._seq}"
        head = "文件权限拦截" if source.startswith("file_permission") or source == "user_denied" else "沙箱拦截"
        note = f"{head}「{tool}」：{_brief(reason, 160)}"
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
        门禁现在无条件注册：工作区没配沙箱时，对话里的文件访问档位仍是本轮约束。
        """
        try:
            toolkit.register_middleware(tool_middleware(self, emit, task_id))
            return None
        except Exception as exc:  # noqa: BLE001
            return _brief(str(exc))


async def ask_user(policy: SandboxPolicy, tool: str, summary: str, task_id: str) -> tuple[bool, str]:
    """经本机回环网关问用户一次；返回 (是否放行, 拒绝原因)

    复用逐工具审批的网关与审计通道（多带一个 kind=file_permission，壳侧据此
    走「文件访问」文案）。连不上/读不懂按「未获批准」处理，不静默放行。
    """
    try:
        import tool_approval as tap  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"确认模块不可用：{_brief(exc)}"
    data, error = await tap._gate_post(
        policy.gate_url,
        policy.gate_token,
        {
            "tool": tool,
            "summary": _brief(summary, tap.MAX_SUMMARY),
            "taskId": task_id,
            "workspaceId": policy.workspace_id,
            "kind": "file_permission",
            "filePermission": policy.file_permission,
        },
    )
    if error:
        return False, error
    if isinstance(data, dict) and data.get("ok") is True:
        return True, ""
    detail = str((data or {}).get("error") or "用户拒绝了本次文件写入").strip()
    return False, _brief(detail, 300)


def tool_middleware(policy: SandboxPolicy, emit: Callable[[str, dict], None], task_id: str = ""):
    """工具门禁中间件：工具白/黑名单 + 文件路径沙箱 + 文件访问档位（AgentScope 洋葱中间件）"""

    async def middleware(kwargs: dict, next_handler):
        from agentscope.message import TextBlock
        from agentscope.tool import ToolResponse

        tool_call = kwargs.get("tool_call") or {}
        name = str(tool_call.get("name") or "")
        args = tool_call.get("input") or {}
        deleting = name in DELETING_TOOLS

        denial = policy.tool_denial(name)
        first_path = ""
        if denial is None and isinstance(args, dict):
            for key in PATH_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    if not first_path:
                        first_path = value
                    denial = policy.file_denial(
                        value, write=name in MUTATING_TOOLS, delete=deleting
                    )
                    if denial is not None:
                        break
        if denial is None and policy.should_ask(name):
            label = "删除" if deleting else "写入"
            allowed, why = await ask_user(
                policy, name, f"文件访问确认：{label} {first_path or '（未给出路径）'}", task_id
            )
            if not allowed:
                denial = (f"{why}（档位「每次询问」，操作未执行）", "file_permission_ask", "user")

        if denial is not None:
            policy.report(emit, task_id, name, denial)
            yield ToolResponse(content=[TextBlock(type="text", text=policy.denial_text(name, denial))])
            return

        async for resp in await next_handler(**kwargs):
            yield resp

    return middleware
