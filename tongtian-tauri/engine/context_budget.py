"""上下文预算与单轮迭代上限（P1 上下文效率）

输入是两份已经存在的事实：

* `runtime.agentPolicy`：桌面版 Rust 从 `settings_kv` 读取，随每次提交下发；
  浏览器直连随 `/api/chat` 的 `agentPolicy` 下发。
* `modelConfig.contextWindow`：模型条目里由用户填写的上下文窗口。

本模块只做纯计算（不 import agentscope），把两件事算清楚：

* 单轮最多执行多少次「思考 → 工具调用」循环；
* 是否启用 AgentScope 的上下文压缩，以及触发阈值。

边界必须如实写进用户可见说明：

* 阈值按上下文窗口的比例估算，工具 schema 不计入 AgentScope 的压缩计数，
  所以预留了较大余量；它不是厂商 tokenizer 的精确值。
* 压缩本身要额外调用一次模型生成摘要，摘要质量取决于模型；
  模型不支持结构化输出时 AgentScope 只记警告、不做压缩，任务不会因此中断。
* 上下文窗口没填时按 8192 估算，不假装知道真实值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MAX_ITERS = 8
MIN_MAX_ITERS = 1
MAX_MAX_ITERS = 32

DEFAULT_AUTO_COMPRESS = True
DEFAULT_LAZY_MCP_TOOLS = True

DEFAULT_CONTEXT_WINDOW = 8192
MIN_CONTEXT_WINDOW = 1024
MAX_CONTEXT_WINDOW = 2_000_000

# 预算比例：给系统提示、工具 schema、模型输出留出约 40% 余量。
# AgentScope 只统计 system + 消息，不统计 tools，这个余量不能省。
COMPRESSION_RATIO = 0.6
COMPRESSION_RESERVE_TOKENS = 512
MIN_TRIGGER_THRESHOLD = 512
KEEP_RECENT_MESSAGES = 3


@dataclass(frozen=True)
class AgentPolicy:
    max_iters: int = DEFAULT_MAX_ITERS
    auto_compress: bool = DEFAULT_AUTO_COMPRESS
    lazy_mcp_tools: bool = DEFAULT_LAZY_MCP_TOOLS
    context_window: int = DEFAULT_CONTEXT_WINDOW
    context_window_from_model: bool = False
    trigger_threshold: int = 0
    keep_recent: int = KEEP_RECENT_MESSAGES
    config_notes: tuple[str, ...] = field(default_factory=tuple)

    def notes(self, mcp_server_count: int = 0) -> list[str]:
        """给用户看的真实口径；不返回空话，每条都能对应一个执行事实。"""
        out = [
            f"单轮工具循环上限：{self.max_iters} 轮；达到上限时会请求模型总结当前进展"
            "并结束本轮（不会继续调用工具），已产生的文件与结果不受影响。"
        ]
        window_note = ""
        if not self.context_window_from_model:
            window_note = (
                f"模型未填写上下文窗口，按 {DEFAULT_CONTEXT_WINDOW} tokens 估算；"
                "可在「设置 → 模型与用量」中填写真实值。"
            )
        if self.auto_compress:
            out.append(
                f"上下文压缩已开启：按 {self.context_window} tokens 窗口的 "
                f"{int(COMPRESSION_RATIO * 100)}% 估算，约 {self.trigger_threshold} tokens "
                f"触发，保留最近 {self.keep_recent} 条消息。压缩会额外调用一次模型生成摘要；"
                "模型不支持结构化输出时 AgentScope 只记警告、不做压缩，任务不会因此中断。"
                + (f" {window_note}" if window_note else "")
            )
        else:
            out.append(
                "上下文压缩已关闭：长任务超出模型上下文窗口时可能被模型拒绝，"
                "请缩短任务或换更大窗口的模型。"
                + (f" {window_note}" if window_note else "")
            )
        if mcp_server_count > 0 and self.lazy_mcp_tools:
            out.append(
                f"连接器工具按需载入：本轮 {mcp_server_count} 个连接器的工具默认不占上下文，"
                "需要时先调用 search_tools 按关键词搜索并启用。"
            )
        out.extend(self.config_notes)
        return out


def _int_value(raw, default: int) -> tuple[int, bool]:
    if isinstance(raw, bool):
        return default, False
    try:
        return int(raw), True
    except (TypeError, ValueError):
        return default, False


def _bool_value(raw, default: bool) -> tuple[bool, bool]:
    if isinstance(raw, bool):
        return raw, True
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("true", "1", "yes", "on"):
            return True, True
        if value in ("false", "0", "no", "off"):
            return False, True
    return default, False


def normalize_max_iters(raw) -> tuple[int, str]:
    """返回 (生效值, 说明)。缺省不报错；越界/非整数要留下可见说明。"""
    value, present = _int_value(raw, DEFAULT_MAX_ITERS)
    if not present:
        if raw is not None and raw != "":
            return (
                DEFAULT_MAX_ITERS,
                f"单轮工具循环上限「{raw}」不是整数，已按默认 {DEFAULT_MAX_ITERS} 执行。",
            )
        return DEFAULT_MAX_ITERS, ""
    if value < MIN_MAX_ITERS or value > MAX_MAX_ITERS:
        return (
            max(MIN_MAX_ITERS, min(MAX_MAX_ITERS, value)),
            f"单轮工具循环上限 {value} 超出允许范围 {MIN_MAX_ITERS}~{MAX_MAX_ITERS}，"
            f"已按边界值 {max(MIN_MAX_ITERS, min(MAX_MAX_ITERS, value))} 执行。",
        )
    return value, ""


def fallback_window(model_config: dict | None) -> int | None:
    """转移链里最小的可用上下文窗口；没有转移目标或窗口都不可用时返回 None。

    切换到备用模型后，同一份上下文要同时装得下；压缩阈值只能按链上最小的
    窗口算，否则主模型窗口大、备用模型小时，切过去第一轮就超限。
    """
    cfg = model_config if isinstance(model_config, dict) else {}
    fallbacks = cfg.get("fallbacks")
    if not isinstance(fallbacks, list):
        return None
    windows = []
    for item in fallbacks:
        if not isinstance(item, dict):
            continue
        value, present = _int_value(item.get("contextWindow"), 0)
        if present and MIN_CONTEXT_WINDOW <= value <= MAX_CONTEXT_WINDOW:
            windows.append(value)
    return min(windows) if windows else None


def resolve(runtime: dict | None, model_config: dict | None = None) -> AgentPolicy:
    """把 runtime.agentPolicy + modelConfig.contextWindow 归一成可执行策略。"""
    runtime = runtime if isinstance(runtime, dict) else {}
    model_config = model_config if isinstance(model_config, dict) else {}
    block = runtime.get("agentPolicy")
    block = block if isinstance(block, dict) else {}

    notes: list[str] = []
    max_iters, max_note = normalize_max_iters(block.get("maxIters"))
    if max_note:
        notes.append(max_note)
    # Rust 装配侧已做过的边界归一说明（settings_kv 里的原值），同样要可见
    for item in block.get("notes") or []:
        text = " ".join(str(item).split()).strip()
        if text and text not in notes:
            notes.append(text)

    auto_compress, present = _bool_value(block.get("autoCompress"), DEFAULT_AUTO_COMPRESS)
    if not present and "autoCompress" in block:
        notes.append("上下文压缩开关不是布尔值，已按默认开启执行。")

    lazy_mcp, present = _bool_value(block.get("lazyMcpTools"), DEFAULT_LAZY_MCP_TOOLS)
    if not present and "lazyMcpTools" in block:
        notes.append("连接器工具按需载入开关不是布尔值，已按默认开启执行。")

    window, present = _int_value(model_config.get("contextWindow"), 0)
    from_model = present and MIN_CONTEXT_WINDOW <= window <= MAX_CONTEXT_WINDOW
    if not from_model:
        if "contextWindow" in model_config:
            notes.append(
                f"模型上下文窗口 {model_config.get('contextWindow')!r} 不可用"
                f"（允许 {MIN_CONTEXT_WINDOW}~{MAX_CONTEXT_WINDOW}），"
                f"已按 {DEFAULT_CONTEXT_WINDOW} tokens 估算压缩阈值。"
            )
        window = DEFAULT_CONTEXT_WINDOW

    trigger = max(
        MIN_TRIGGER_THRESHOLD,
        int(window * COMPRESSION_RATIO) - COMPRESSION_RESERVE_TOKENS,
    )
    return AgentPolicy(
        max_iters=max_iters,
        auto_compress=auto_compress,
        lazy_mcp_tools=lazy_mcp,
        context_window=window,
        context_window_from_model=from_model,
        trigger_threshold=trigger,
        keep_recent=KEEP_RECENT_MESSAGES,
        config_notes=tuple(notes),
    )


_SAFE_COUNTER_CLS = None


def _safe_counter_class():
    """延迟构造包装类：纯计算模块不在引擎启动时 import agentscope。

    AgentScope 的 CompressionConfig 用 pydantic 校验 `TokenCounterBase` 实例，
    所以包装类必须真的继承它，不能只用鸭子类型（会直接校验失败）。
    """
    global _SAFE_COUNTER_CLS
    if _SAFE_COUNTER_CLS is None:
        from agentscope.token import TokenCounterBase

        class SafeTokenCounter(TokenCounterBase):
            """包一层 token 计数：计数失败时返回 0（本轮不压缩），不让整个任务失败。

            AgentScope 在 ReAct 循环里直接 await 计数器，异常会冒到 reply() 外面；
            图片、未知内容块或 tiktoken 缺包都可能触发，这个包装是任务稳定性的兜底。
            """

            def __init__(self, inner):
                self._inner = inner
                self.last_error = ""

            async def count(self, messages, tools=None, **kwargs) -> int:
                try:
                    return await self._inner.count(messages, tools=tools, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = " ".join(str(exc).split())[:240]
                    return 0

        _SAFE_COUNTER_CLS = SafeTokenCounter
    return _SAFE_COUNTER_CLS


def build_token_counter(inner):
    """构造安全 token 计数器（见 `_safe_counter_class` 的说明）。"""
    return _safe_counter_class()(inner)


def build_compression(policy: AgentPolicy, model_name: str):
    """构造 AgentScope 的 CompressionConfig；不可用时返回 (None, 原因)。"""
    if not policy.auto_compress:
        return None, ""
    try:
        # tiktoken 是 count() 内部才 import 的，缺包时构造不会报错、
        # 但阈值永远不会触发；这里先探测，避免界面声称「压缩已开启」。
        import importlib.util

        if importlib.util.find_spec("tiktoken") is None:
            return None, (
                "上下文压缩未启用：未安装 tiktoken，无法统计 token；"
                "执行 pip install -r engine/requirements.txt 后重启引擎"
            )
        from agentscope.agent import ReActAgent
        from agentscope.token import OpenAITokenCounter

        counter = build_token_counter(OpenAITokenCounter(model_name or "unknown"))
        config = ReActAgent.CompressionConfig(
            enable=True,
            agent_token_counter=counter,
            trigger_threshold=policy.trigger_threshold,
            keep_recent=policy.keep_recent,
        )
        return config, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"上下文压缩未启用：token 计数依赖不可用（{exc}）"
