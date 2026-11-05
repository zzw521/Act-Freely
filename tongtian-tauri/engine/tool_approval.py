"""逐工具审批（写文件 / 执行 / 联网 / 连接器 / 子 Agent）

Rust 随 `runtime.toolApproval` 下发档位与一次性回环网关：

    toolApproval = {mode, gateUrl, gateToken, gateError, workspaceId}

- `off`：不注册审批中间件（默认）；
- `sensitive`：副作用与外部工具在真正执行前询问；
- `all`：除浏览器/电脑操控（已有逐操作确认）外的所有工具都询问。

门禁在沙箱与 Hooks 之后执行：越权调用不会先弹确认框；用户拒绝时把拒绝结果作为
工具错误回给模型，不执行真实动作。网关不可用且档位已开启时由上层直接中止任务，
不允许静默绕过。

边界：
- 只覆盖父/子 Agent 的工具调用；浏览器、电脑操控、连接器服务端自身的操作各有
  独立确认/授权，不在这里重复弹窗；
- 审批记录只含工具名与入参摘要，正文与密钥由 Rust 侧再次脱敏后落审计。
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field

import privacy_guard as pg
import side_effect_registry as ser  # #164：副作用/敏感名单的唯一落表处
import write_staging_runtime as wsr  # D-03.5：哪些写入工具被写暂存接管，名单只有一个来源

#: 被写暂存接管的工具（= `write_staging_runtime.STAGED_TOOLS` 的别名，不重抄一遍）
STAGED_WRITE_TOOLS = wsr.STAGED_TOOLS

MODES = ("off", "sensitive", "all")
DEFAULT_MODE = "off"
MODE_LABELS = {
    "off": "关闭",
    "sensitive": "敏感工具",
    "all": "所有工具",
}

# 默认就需要审批的副作用/外部工具；未登记的 MCP 工具也按敏感处理
# 成员本体在 engine/side_effect_registry.py：本表比沙箱写盘面与检查点台账都宽，
# 多出的 run_command / run_shell / fetch_url 是「执行前先问一次人」的那一类。
SENSITIVE_TOOLS = ser.view("approval_sensitive")
READ_ONLY_TOOLS = frozenset({
    "list_directory",
    "view_text_file",
    "read_document",
    "use_skill",
    "search_knowledge",
    "read_knowledge_document",
})
# 已有逐操作确认的能力：不再重复弹一次工具审批
OWN_GATED_TOOLS = frozenset({"screen_capture", "computer_action"})
OWN_GATED_PREFIXES = ("browser_",)

GATE_TIMEOUT = 120
MAX_GATE_BYTES = 64 * 1024
MAX_SUMMARY = 400


def normalize_mode(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def _brief(text: object, limit: int = 240) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _loopback_gate(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:  # noqa: BLE001
        return False
    return parts.scheme == "http" and parts.hostname == "127.0.0.1" and bool(parts.port)


def _own_gated(name: str) -> bool:
    return name in OWN_GATED_TOOLS or name.startswith(OWN_GATED_PREFIXES)


def needs_approval(name: str, mode: str) -> bool:
    mode = normalize_mode(mode)
    if mode == "off" or _own_gated(name):
        return False
    if mode == "all":
        return True
    return name not in READ_ONLY_TOOLS


def _summary_value(value: object, limit: int = 120) -> str:
    if isinstance(value, str):
        return _brief(value, limit)
    return _brief(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value, limit)


def summarize_input(name: str, payload: object) -> str:
    """给用户看的入参摘要：路径/命令/主机等关键信息保留，正文只给长度。"""
    data = payload if isinstance(payload, dict) else {}
    if name == "delete_text_file":
        return (
            f"删除：{_summary_value(data.get('file_path') or data.get('filePath'))}"
            f"；原因：{_summary_value(data.get('reason'), 60)}"
        )
    if name in ("write_text_file", "insert_text_file"):
        content = data.get("content")
        length = len(content) if isinstance(content, str) else 0
        extra = f"（正文 {length} 字）" if length else ""
        return f"文件：{_summary_value(data.get('file_path') or data.get('filePath'))}{extra}"
    if name == "generate_pptx":
        return (
            f"演示文稿：{_summary_value(data.get('file_path') or data.get('filePath'))}"
            f"；标题：{_summary_value(data.get('title'), 60)}"
        )
    if name in ("generate_xlsx", "generate_docx"):
        # 内容参数是一整段 JSON，摘要只报体积：审批卡片要让人认出「往哪儿写哪份文件」，
        # 不是把几十 KB 表格正文糊进弹窗。没给标题就不硬撑出「标题：」空尾巴。
        body_key = "sheets_json" if name == "generate_xlsx" else "blocks_json"
        raw_body = data.get(body_key)
        length = len(raw_body) if isinstance(raw_body, str) else 0
        extra = f"（内容 {length} 字）" if length else ""
        head = "工作簿" if name == "generate_xlsx" else "文档"
        summary = (
            f"{head}：{_summary_value(data.get('file_path') or data.get('filePath'))}{extra}"
        )
        title = _summary_value(data.get("title"), 60)
        return summary + (f"；标题：{title}" if title else "")
    if name == "run_command":
        return f"命令：{_summary_value(data.get('command'), 200)}"
    if name == "run_shell":
        # 这是高风险操作：给用户的摘要保留完整命令便于判断，但审计侧只存哈希
        return f"shell 命令：{_summary_value(data.get('command'), 360)}"
    if name == "fetch_url":
        raw = str(data.get("url") or "").strip()
        try:
            parts = urllib.parse.urlsplit(raw)
            host = parts.hostname or ""
            path = parts.path or "/"
            raw = f"{parts.scheme}://{host}{path}" if parts.scheme else raw
        except Exception:  # noqa: BLE001
            pass
        return f"URL：{_brief(raw, 200)}"
    if name == "spawn_subagents":
        tasks = data.get("tasks_json")
        return f"派发子 Agent（任务参数 {len(str(tasks or ''))} 字）"
    return _brief(json.dumps(data, ensure_ascii=False) if data else "（无参数）", MAX_SUMMARY)


@dataclass
class ToolApproval:
    mode: str = DEFAULT_MODE
    gate_url: str = ""
    gate_token: str = ""
    workspace_id: str = ""
    error: str = ""
    runtime_present: bool = False
    unavailable_reason: str = ""
    #: D-03.5 合并口径：写暂存开启时，文件写入的「批准」由暂存面板的 diff 审批承担，
    #: 这一层不再为同一次写入弹第二个确认框（一次写入两个弹窗不是更严，是让人猜哪个算数）。
    #: 由装配现场按同一份 runtime 设置，缺省 False = 行为与改造前逐字相同。
    staged_writes: bool = False
    _down: str = field(default="", init=False, repr=False)

    def prompts_for(self, name: str) -> bool:
        """这一次调用到底弹不弹窗：档位说该弹，且没被写暂存接管。

        只有**进暂存的那两个工具**被接管：`delete_text_file` / `generate_*` 仍在直接写盘，
        它们照旧逐次确认——把接管扩到没暂存的工具上等于凭空少一道审批。
        """
        if self.staged_writes and name in STAGED_WRITE_TOOLS:
            return False
        return needs_approval(name, self.mode)

    @classmethod
    def from_runtime(cls, runtime: dict | None) -> "ToolApproval":
        block = (runtime or {}).get("toolApproval") if isinstance(runtime, dict) else None
        if not isinstance(block, dict):
            return cls()
        raw_mode = str(block.get("mode") or "off").strip()
        mode = normalize_mode(raw_mode)
        gate_url = str(block.get("gateUrl") or "").strip()
        gate_token = str(block.get("gateToken") or "").strip()
        workspace_id = str(block.get("workspaceId") or "").strip()
        error = ""
        if raw_mode not in MODES:
            error = f"工具审批档位非法「{raw_mode}」，已按关闭处理"
        elif mode == "off":
            error = ""
        elif str(block.get("gateError") or "").strip():
            error = str(block.get("gateError")).strip()
        elif not _loopback_gate(gate_url) or not gate_token:
            error = "本机工具审批网关未启动或地址无效"
        # 档位保持原值：合法档位 + 网关不可用时 enabled 仍为真、ready 为假，
        # 上层据此中止任务；若这里把 mode 降成 off，就会静默绕过审批。
        return cls(
            mode=mode,
            gate_url=gate_url,
            gate_token=gate_token,
            workspace_id=workspace_id,
            error=error,
            runtime_present=True,
        )

    @property
    def enabled(self) -> bool:
        return self.mode in ("sensitive", "all") and self.runtime_present

    @property
    def ready(self) -> bool:
        return self.enabled and not self.error

    def notes(self) -> list[str]:
        if not self.enabled:
            if self.runtime_present and self.error:
                return [self.error]
            return []
        if self.error:
            return [f"工具审批已开启，但本机确认网关不可用：{self.error}"]
        lines = [
            f"工具审批已开启（{MODE_LABELS.get(self.mode, self.mode)}）："
            "写文件/命令/联网/连接器/子 Agent 等工具在真正执行前会弹系统确认框；"
            "浏览器与电脑操控已有逐操作确认，不重复弹窗。"
        ]
        if self.staged_writes:
            # 少弹一次框是看得见的行为变化，必须说清是谁替它把的关，否则用户以为审批失效了
            lines.append(
                "本轮写暂存已开启："
                + "、".join(STAGED_WRITE_TOOLS)
                + " 不再弹逐工具确认框，内容先进暂存区、由界面看过 diff 后批准合并"
                "（一次写入只有一次批准，批准的那一步才真的写盘）"
            )
        return lines

    def install(self, toolkit, emit=None, task_id: str = "") -> str | None:
        if not self.ready:
            return None
        try:
            toolkit.register_middleware(self.middleware(emit, task_id))
            return None
        except Exception as exc:  # noqa: BLE001
            return _brief(exc, 200)

    def middleware(self, emit=None, task_id: str = ""):
        async def middleware(kwargs: dict, next_handler):
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            tool_call = kwargs.get("tool_call") or {}
            name = str(tool_call.get("name") or "")
            # D-03.5 合并口径：写暂存开着时，那两件文本写入由「看过 diff 再批准」的面板接管，
            # 这里不再为同一次写入弹第二个框；其余工具（含 delete_text_file 与 generate_*）照旧。
            if not self.prompts_for(name):
                async for chunk in await next_handler(**kwargs):
                    yield chunk
                return
            # 确认框与审计只看摘要，但命令/URL 里仍可能夹带手机号与明文密钥
            summary = pg.redact(summarize_input(name, tool_call.get("input") or {}))
            allowed, reason = await self._approve(name, summary, task_id)
            if not allowed:
                if emit is not None:
                    try:
                        emit("step_progress", {
                            "step_id": "tool-approval",
                            "progress": 0,
                            "message": f"工具「{name}」未执行：{reason}",
                        })
                    except Exception:  # noqa: BLE001
                        pass
                yield ToolResponse(content=[TextBlock(
                    type="text",
                    text=(
                        f"工具「{name}」未执行：{reason}。"
                        "不要重复尝试同一操作，除非用户明确要求。"
                    ),
                )])
                return
            async for chunk in await next_handler(**kwargs):
                yield chunk

        return middleware

    async def _approve(self, tool: str, summary: str, task_id: str) -> tuple[bool, str]:
        if self._down:
            return False, self._down
        data, error = await _gate_post(
            self.gate_url,
            self.gate_token,
            {
                "tool": tool,
                "summary": summary[:MAX_SUMMARY],
                "taskId": task_id,
                "workspaceId": self.workspace_id,
            },
        )
        if error:
            self._down = error
            return False, error
        if data is not None and data.get("ok") is True:
            return True, ""
        detail = str((data or {}).get("error") or "用户拒绝了本次工具调用").strip()
        return False, _brief(detail, 300)


async def _gate_post(url: str, token: str, payload: dict, timeout: int = GATE_TIMEOUT) -> tuple[dict | None, str]:
    try:
        import aiohttp
    except Exception as exc:  # noqa: BLE001
        return None, f"引擎缺少 aiohttp，无法连接本机工具审批网关：{_brief(exc)}"
    headers = {
        "Content-Type": "application/json",
        "x-tt-tool-token": token,
    }
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                raw = await resp.read()
                if len(raw) > MAX_GATE_BYTES:
                    return None, f"网关响应超过 {MAX_GATE_BYTES} 字节，已拒绝读取"
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception as exc:  # noqa: BLE001
                    return None, f"网关返回了无法解析的响应（HTTP {resp.status}）：{_brief(exc, 120)}"
                if not isinstance(data, dict):
                    return None, "网关响应不是 JSON 对象"
                # 用户拒绝也是合法应答：只有连不上/读不懂才记为网关故障，
                # 否则一次拒绝会把后续所有调用都自动拒绝
                return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"无法连接本机工具审批网关：{_brief(exc)}"
