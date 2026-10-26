"""执行面 P0-3：用户配置的 Hooks 进入任务生命周期

引擎不执行任何脚本。脚本执行（shell 解释、stdin 喂事件 JSON、按秒超时、进程树回收）
全部在桌面壳里，由 `src-tauri/src/bridge/hook_gate.rs` 的环回网关完成，并且与 Hooks 页
「试运行」共用同一个 `align::hooks::run_command` —— 试运行看到什么，真实任务就是什么。

这里只负责三件事：
1. 在真实存在的执行节点触发事件（SessionStart / UserPromptSubmit / PreCompact /
   PreToolUse / PostToolUse / PostToolUseFailure / Stop / SessionEnd）；
2. `PreToolUse` 阻断时让工具**不执行**，把阻断原因作为工具结果交回模型；
3. 把「没能触发」的部分如实变成可见说明：命令未放行、网关失联、配置了但引擎没有触发点。

SubagentStart/SubagentStop 由 `subagent_runtime.py` 在子 Agent 真正启动/结束时触发，
共用这里的 HookRunner。仍没有触发点的是 Notification/PermissionRequest —— 由 Rust 侧
`FIRED_EVENTS`/`UNFIRED_EVENTS` 给出事实，页面与事件流按同一口径说明。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx

# 网关侧单条命令上限 600s（与 align::hooks::MAX_TIMEOUT_SEC 同口径），这里留出网络与串行余量
GATE_TIMEOUT_SECONDS = 630.0
# 单轮最多摊出多少条 Hook 说明（再多就折叠，避免事件流被脚本噪音淹没）
MAX_HOOK_NOTES = 12
# 引擎没有触发点、由 Rust 侧在 runtime.hooks 里回传原因的事件集合键名
UNFIRED_KEY = "mountedUnfired"
# AgentScope 把工具异常吞成文本返回，失败事件只能按这些已知前缀判定（不猜其它形态）
_FAILURE_PREFIXES = (
    "Error:",
    "Error occurred when calling MCP tool:",
    "FunctionNotFoundError",
    "FunctionInactiveError",
    "<system-info>",
)


def _brief(text: str, limit: int = 180) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


class HookGateway:
    """本机 Hook 网关客户端（只在桌面壳内可用）。"""

    def __init__(self, block: dict | None):
        block = block if isinstance(block, dict) else {}
        self.error = str(block.get("error") or "").strip()
        self.gate_url = str(block.get("gateUrl") or "").strip()
        self.token = str(block.get("token") or "").strip()
        self.gate_error = str(block.get("gateError") or "").strip()
        try:
            self.configured = int(block.get("configured") or 0)
        except (TypeError, ValueError):
            self.configured = 0
        self.mounted_unfired = [str(e) for e in (block.get(UNFIRED_KEY) or []) if str(e).strip()]
        self.ungranted = [str(c) for c in (block.get("ungranted") or []) if str(c).strip()]
        self.available = bool(self.gate_url and self.token)
        # 没配脚本就不产生任何网关往返：这是每次工具调用都会经过的路径
        self.enabled = self.available and self.configured > 0

    def notes(self) -> list[str]:
        """开跑前就该摊出来的 Hook 事实（读不到 / 没通道 / 没触发点 / 没放行）"""
        notes: list[str] = []
        if self.error:
            notes.append(f"Hooks 配置读取失败，本轮不会触发任何脚本：{_brief(self.error)}")
            return notes
        if self.configured and not self.available:
            notes.append(
                f"本轮没有本机 Hook 执行通道：{self.gate_error or '网关未启动'}，"
                f"配置里的 {self.configured} 条脚本没有执行"
            )
        if self.mounted_unfired:
            notes.append(
                f"事件 {'、'.join(self.mounted_unfired)} 在当前引擎没有触发点，"
                "这些 Hook 不会执行"
            )
        if self.ungranted and self.available:
            shown = "；".join(f"`{_brief(c, 60)}`" for c in self.ungranted[:3])
            more = f" 等 {len(self.ungranted)} 条" if len(self.ungranted) > 3 else ""
            notes.append(
                f"以下 Hook 命令尚未放行，本轮不会执行：{shown}{more}"
                "（在 Hooks 页点「放行」后才会真的跑）"
            )
        return notes

    def fire(
        self,
        event: str,
        tool: str | None = None,
        payload: dict | None = None,
        task_id: str = "",
    ) -> dict:
        """同步触发一个事件（调用方负责放线程池，别卡住事件循环）

        返回 `{"blocked": bool, "notes": [...], "results": [...]}`；
        网关失联不抛异常 —— 把失败变成可见说明，而不是让整轮任务 500。
        """
        if not self.enabled:
            return {"blocked": False, "notes": [], "results": []}
        body = {"event": event, "payload": payload or {}, "taskId": task_id}
        if tool:
            body["tool"] = tool
        try:
            resp = httpx.post(
                self.gate_url,
                json=body,
                headers={"x-tt-hook-token": self.token},
                timeout=GATE_TIMEOUT_SECONDS,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001  网关是本机服务，任何异常都要摊出来而不是静默
            return {
                "blocked": False,
                "notes": [
                    f"Hook「{event}」没有结果：本机网关未响应（{_brief(type(exc).__name__ + ': ' + str(exc))}）"
                ],
                "results": [],
                "error": str(exc),
            }
        if resp.status_code != 200 or not data.get("ok"):
            return {
                "blocked": False,
                "notes": [
                    f"Hook「{event}」执行失败：{_brief(data.get('error') or f'HTTP {resp.status_code}')}"
                ],
                "results": [],
            }
        result = data.get("result") or {}
        return {
            "blocked": bool(result.get("blocked")),
            "notes": [str(n) for n in (result.get("notes") or [])],
            "results": result.get("results") or [],
        }


class HookRunner:
    """把网关挂到执行链路上，并把每条说明变成可见的步骤事件。"""

    def __init__(
        self,
        gateway: HookGateway,
        emit: Callable[[str, dict], None],
        task_id: str = "",
        cwd: str = "",
    ):
        self.gw = gateway
        self.emit = emit
        self.task_id = task_id
        # 脚本侧的通行字段：网关会把 sessionId/cwd 映射成 stdin JSON 的 session_id/cwd
        self.base_payload = {"sessionId": task_id, "cwd": cwd}
        self._seq = 0
        self.notes_total = 0

    def surface(self, notes: list[str]) -> None:
        """说明必须进事件流；只在日志里打印等于让用户以为 Hook 没触发是模型的问题"""
        for note in notes:
            self.notes_total += 1
            if self.notes_total > MAX_HOOK_NOTES:
                continue
            self._seq += 1
            step_id = f"hook-{self._seq}"
            self.emit("step_start", {
                "step_id": step_id,
                "stepType": "hook",
                "description": note,
            })
            self.emit("step_done", {"step_id": step_id, "output": note})

    async def fire(
        self,
        event: str,
        tool: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        body = {**self.base_payload, **(payload or {})}
        result = await asyncio.to_thread(
            self.gw.fire, event, tool, body, self.task_id
        )
        self.surface(result.get("notes") or [])
        return result


def response_text(chunks: list[Any]) -> str:
    """把工具响应块拼成文本（PostToolUse 的 tool_response 字段用）"""
    out: list[str] = []
    for resp in chunks:
        for block in getattr(resp, "content", None) or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if text:
                out.append(str(text))
    return "\n".join(out)


def is_failure(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(p) for p in _FAILURE_PREFIXES)


def tool_middleware(runner: HookRunner):
    """PreToolUse 阻断 + PostToolUse/PostToolUseFailure 上报（Toolkit 洋葱中间件）

    中间件必须是 async generator 函数：AgentScope 直接消费 `mw(kwargs, handler)` 的返回。
    """

    async def middleware(kwargs: dict, next_handler):
        from agentscope.message import TextBlock
        from agentscope.tool import ToolResponse

        tool_call = kwargs.get("tool_call") or {}
        name = str(tool_call.get("name") or "")
        payload = {
            "toolInput": tool_call.get("input") or {},
            "toolUseId": tool_call.get("id") or "",
        }

        if runner.gw.enabled:
            pre = await runner.fire("PreToolUse", name, payload)
            if pre.get("blocked"):
                reason = "；".join(
                    (pre.get("notes") or [])[:2]
                ) or "Hook 以 exit 2 阻止了本次工具调用"
                yield ToolResponse(content=[TextBlock(
                    type="text",
                    text=(
                        "<system-info>本次工具调用被用户配置的 PreToolUse Hook 阻断，"
                        f"工具 {name} 没有执行。原因：{reason}"
                        "</system-info>"
                    ),
                )])
                return

        chunks: list[Any] = []
        async for resp in await next_handler(**kwargs):
            chunks.append(resp)
            yield resp

        if not runner.gw.enabled:
            return
        text = response_text(chunks)
        event = "PostToolUseFailure" if is_failure(text) else "PostToolUse"
        after = dict(payload)
        after["tool_response"] = _brief(text, 2000)
        await runner.fire(event, name, after)

    return middleware


def install(toolkit, runner: HookRunner) -> bool:
    """把中间件注册进本轮 Toolkit（注册失败必须可见，不能静默丢掉 Hook 门禁）"""
    try:
        toolkit.register_middleware(tool_middleware(runner))
        return True
    except Exception as exc:  # noqa: BLE001
        runner.surface([f"PreToolUse 门禁没有装上：{_brief(str(exc))}（工具调用不会被 Hook 拦截）"])
        return False


def from_runtime(runtime: dict | None) -> HookGateway:
    """从 /api/chat 的 runtime.hooks 段还原网关配置

    Rust 侧给的是 JSON 对象；被中间层转成字符串时也要能用（与 mcp 段同口径）。
    """
    block = (runtime or {}).get("hooks") if isinstance(runtime, dict) else None
    if isinstance(block, str):
        try:
            block = json.loads(block)
        except Exception:  # noqa: BLE001
            block = {"error": block}
    return HookGateway(block if isinstance(block, dict) else None)
