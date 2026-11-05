"""工单执行体的真实模型通道（缺陷 D-48.7）

D-48.5b 把 `model_call(payload)` 留成注入点：输入 `{system, messages, tools}`，输出
`{text, calls, outcome, reason}`。此前所有自测喂的都是脚本化的假通道，本模块补真的那一半——
把这份契约翻译成一次 **OpenAI 兼容** 的 `POST {baseUrl}/chat/completions`。接线侧（server.py
的端点）只需要把已经算好的 `modelConfig`（baseUrl/apiKey/modelName 或 gateway 段）递进来。

三条口径先钉住：

* **非流式**。worker 回合要的是「这一轮模型让我干什么」，不是逐字回显；回合结果进检查点，
  不进 SSE。真要流式的是会话页，那是 server.py 的既有通道，这里不复用也不抢它的语义。
* **工具面只有一个来源**。`worker_tool_schemas()` 由 `ticket_worker_adapter.tool_specs()`
  生成——模型能看见的工具名与参数，永远等于执行体会执行的那 6 个。两处各写一份迟早漂移，
  漂移的结果是模型反复调一个「不存在的工具」白烧轮次。
* **通道不解释路径、不起进程、不发 IM**。它只把话说到 `{text, calls}`；落盘、跑命令、
  外发全在适配器与 `ticket_worker_service` 里，模型说什么不等于系统做了什么。

诚实面（做不到的部分写在这里，不在代码里假装）：DashScope 原生协议（`enable_search`、
原生 formatter）本层不复刻，走它的 OpenAI 兼容端点；工具调用参数被上游截断时本层**不补全**，
只把它标成「这条调用没被执行」的拒绝回给下一轮；网络失败重试有上限，超限就抛中文理由，
由适配器按「回合失败、外发一条也不提交」收口。密钥只进请求头，绝不进异常文本、日志或快照。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import context_budget as cb
import ticket_worker_adapter as ta

#: 端点地址的拼接后缀（baseUrl 允许带或不带尾斜杠，也允许已经带到 /v1）
COMPLETIONS_SUFFIX = "chat/completions"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.8
#: 响应体上限：worker 的一轮回复是「说什么 + 调哪些工具」，超过这个量基本是上游在吐错误页
MAX_RESPONSE_BYTES = 4_000_000
#: 没有自定义端点、也没有 TT_BASE_URL 时补的 DashScope 兼容模式地址（与 TT_PROVIDER 缺省一致）
DASHSCOPE_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL_NAME = "qwen-plus"
#: 提示词里追加的协议说明上限：通道自带的那段不该把工单目标挤出去
MAX_REPLY_CHARS = 200_000

#: 本层的自述——`describe()` 与快照只报这些，绝不报密钥
USAGE_ORIGIN = "ticket-worker"

PROTOCOL_NOTE = (
    "【通道约定】要调用工具就用工具调用（function call）。若你所在的端点不支持工具调用，"
    "就把整条回复写成一个 JSON 对象、不要其它文字："
    '{"text":"要说的话","calls":[{"tool":"read_file","args":{"path":"a.txt"}}],'
    '"outcome":"running 或 finished","reason":"为什么"}。'
    "calls 里的 tool 只能是上面列出的 6 个名字；outcome 只有真的调用过 finish_work 之后才填 "
    "finished，否则填 running 或留空（留空按「仍在进行」处理，槽位与检查点都会留着）。"
)


def _int_or_zero(value: Any) -> int:
    """用量字段可能是 None / 字符串 / 负数：统一成一个非负整数，不为此抛穿一整轮。"""
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


class ModelChannelError(RuntimeError):
    """模型通道失败：中文理由，进适配器就是「本轮失败、外发不提交」。"""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int = 0):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = int(status_code)


# ── 配置解析（不 import server.py：那边的 modelConfig 由接线侧递进来）──────
def resolve_model_config(model_config: Any = None, *, env: Any = None) -> dict[str, Any]:
    """端点判定 + **上下文窗口**：窗口是「按剩余窗口取页」（D-48.8）唯一的窗口来源。

    窗口从 `context_budget.resolve` 借同一份归一化，本层不再自己算一遍：填了就用填的，
    没填或填得离谱就按 `DEFAULT_CONTEXT_WINDOW` 估算，并把 `contextWindowFromModel=False`
    带出来。这个字段是给工具页面用的诚实标记——估算窗口下压小一页是「按 8192 猜的」，
    与「按你填的 128000 算的」不是一句话，页面不许把两者说成同一种确定性。"""
    resolved = _resolve_endpoint(model_config, env=env)
    policy = cb.resolve(None, model_config if isinstance(model_config, dict) else {})
    resolved["contextWindow"] = int(policy.context_window)
    resolved["contextWindowFromModel"] = bool(policy.context_window_from_model)
    return resolved


def _resolve_endpoint(model_config: Any = None, *, env: Any = None) -> dict[str, Any]:
    """把「一次回合该打哪儿」算清楚：网关 / 自定义端点 / 环境 OpenAI 兼容 / 环境 DashScope。

    判定顺序与引擎的 `_model_kind` 一致（gateway > baseUrl > TT_PROVIDER），差别只在
    DashScope 这一档：引擎用 agentscope 的原生模型对象，本层用它的 OpenAI 兼容端点，
    所以在这里补一个 `DASHSCOPE_COMPATIBLE_BASE_URL`。密钥缺失时不猜、不退回假端点，
    直接给 `mode="none"` 与中文理由——缺配置是「现在还不能跑」，不是「跑了必然失败」。"""
    environ = env if env is not None else os.environ
    cfg = model_config if isinstance(model_config, dict) else {}
    gateway = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else None
    model_name = str(cfg.get("modelName") or "").strip() or str(environ.get("TT_MODEL", "")).strip() or DEFAULT_MODEL_NAME
    custom_base = str(cfg.get("baseUrl") or "").strip()
    custom_key = str(cfg.get("apiKey") or "").strip()
    env_key = str(environ.get("TT_API_KEY", "")).strip()
    env_base = str(environ.get("TT_BASE_URL", "")).strip()

    if gateway:
        base = str(gateway.get("baseUrl") or "").strip()
        control = str(gateway.get("controlPlaneUrl") or "").strip()
        if not base or not control:
            return {"mode": "none", "baseUrl": "", "apiKey": "", "modelName": model_name,
                    "reason": "网关模式配置不完整：需要 gateway.baseUrl 与 gateway.controlPlaneUrl（缺任一项不静默退回直连）"}
        return {"mode": "gateway", "baseUrl": base.rstrip("/"), "apiKey": "", "modelName": model_name,
                "controlPlaneUrl": control, "dept": str(gateway.get("dept") or "").strip(),
                "accessToken": str(gateway.get("accessToken") or "").strip(), "reason": ""}
    if custom_base:
        return {"mode": "custom", "baseUrl": custom_base.rstrip("/"), "apiKey": custom_key or env_key,
                "modelName": model_name, "reason": ""}
    if env_base:
        return {"mode": "openai", "baseUrl": env_base.rstrip("/"), "apiKey": env_key, "modelName": model_name, "reason": ""}
    if str(environ.get("TT_PROVIDER", "dashscope").strip().lower()) == "dashscope":
        if not env_key:
            return {"mode": "none", "baseUrl": "", "apiKey": "", "modelName": model_name,
                    "reason": "没有可用模型：请在「设置 → 模型与用量」里配置模型（或配 TT_API_KEY 兜底）后再接工单执行体"}
        return {"mode": "dashscope", "baseUrl": DASHSCOPE_COMPATIBLE_BASE_URL, "apiKey": env_key,
                "modelName": model_name, "reason": ""}
    return {"mode": "none", "baseUrl": "", "apiKey": "", "modelName": model_name,
            "reason": "没有可用模型：既没有 baseUrl 也没有 TT_BASE_URL，且 TT_PROVIDER 不是 dashscope"}


# ── 工具面：与适配器同一把源 ────────────────────────────────────────
_ARG_TYPES = {"units": "integer", "limit": "integer", "background": "boolean"}
_ARG_DOC = {
    "path": "工单目录内的相对路径（越出工单目录会被拒）",
    "start": "分页起点（字符偏移）",
    "units": "本次最多读多少单元（单元=字符，超限按预算截断）",
    "limit": "最多列几项（超限只给前 N 项并说明还剩多少）",
    "content": "要写入的正文（整文件覆盖）",
    "command": "要跑的一条 shell 命令；不许用 sleep / setTimeout 这类睡等占槽位",
    "background": "true 表示长驻作业（起进程并登记，30 分钟到点由看门狗处置）",
    "kind": "外发形式：只对会话说话的那三种",
    "sessionKey": "目标会话，形状是「通道:会话id」，例如 slack:C2",
    "text": "要发出去的正文",
    "dedupeKey": "幂等键（可留空，留空按内容摘要）：同一键不会被发两次",
    "summary": "收口时给的一句话结论（做了什么、产物在哪）",
}
_ENUM_ARGS = {"kind": lambda: sorted(ta.MODEL_SEND_KINDS.keys())}


def worker_tool_schemas() -> list[dict[str, Any]]:
    """把适配器的 6 个工具翻成 OpenAI function-tool 声明（单一来源，不另写一份名字表）。"""
    schemas: list[dict[str, Any]] = []
    for spec in ta.tool_specs():
        name = str(spec.get("name") or "")
        properties: dict[str, Any] = {}
        for arg in spec.get("args") or []:
            prop: dict[str, Any] = {
                "type": _ARG_TYPES.get(str(arg), "string"),
                "description": _ARG_DOC.get(str(arg), f"{arg}（执行体参数，见工单提示词）"),
            }
            enum_of = _ENUM_ARGS.get(str(arg))
            if enum_of is not None:
                prop["enum"] = enum_of()
                prop["description"] = _ARG_DOC.get(str(arg), "")
            properties[str(arg)] = prop
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"{name}（工单执行体工具；预算：{spec.get('budget') or ''}）",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": [a for a in (spec.get("args") or []) if str(a) not in ("start", "dedupeKey", "background")],
                    },
                },
            }
        )
    return schemas


# ── 请求侧：内部对话字典 → OpenAI messages ─────────────────────────
def to_openai_messages(system: str, messages: Any) -> list[dict[str, Any]]:
    """把适配器的 `{role: user|assistant|tool, content}` 翻成 OpenAI 的 messages。

    `role="tool"` 在这里翻成**用户消息 + 工具回执标记**，不是照抄角色名：适配器把一轮里
    所有工具的结果并成一条反馈（含被拒理由），而 OpenAI 协议的 `tool` 角色必须逐条对上
    `tool_call_id`。对不上号还硬发 `tool`，上游要么 400 要么把回执丢掉——丢掉的那条正是
    「你刚才那条命令没跑成」，模型下一轮就看不见自己哪里没做成。"""
    out: list[dict[str, Any]] = []
    sys_text = str(system or "")
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for item in messages or []:
        entry = item if isinstance(item, dict) else {}
        role = str(entry.get("role") or "user").strip().lower()
        content = entry.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if role == "assistant":
            out.append({"role": "assistant", "content": text})
        elif role == "tool":
            out.append({"role": "user", "content": f"【工具回执】{text}"})
        else:
            out.append({"role": "user", "content": text})
    return out


# ── 响应侧：OpenAI 回复 → 适配器契约 ────────────────────────────────
def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for piece in content:
            if isinstance(piece, dict):
                parts.append(str(piece.get("text") or piece.get("content") or ""))
            else:
                parts.append(str(piece))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def parse_protocol_json(text: str) -> dict[str, Any] | None:
    """把「模型改用正文回 JSON」这条路解析成契约字典；不是 JSON 就返回 None。

    容错只有两条，都不算猜：剥掉代码围栏、从第一个 `{` 取到最后一个 `}`。字段不认识就忽略，
    `calls` 不是列表则整条判为解析失败（宁缺毋滥：把一句话当工具调用执行是事故，
    把一次调用当没发生只是慢一轮）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = raw[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("calls") is not None and not isinstance(parsed.get("calls"), list):
        return None
    known = {"text", "calls", "outcome", "reason"}
    if not (known & set(parsed.keys())):
        return None
    return parsed


def normalize_reply(payload: Any) -> dict[str, Any]:
    """上游响应 → `{text, calls, outcome, reason, usage}`（适配器认的那份契约）。

    工具调用优先；没有工具调用时才试正文里的协议 JSON。参数不是合法 JSON 的那条调用
    **原样保留在 calls 里、但标上 `rejectReason`**，由适配器在进账本之前拒掉并回给下一轮——
    这里补全或丢弃都是替模型编话。"""
    body = payload if isinstance(payload, dict) else {}
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelChannelError("模型响应里没有 choices（上游返回的形状不对，本轮不采信）")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    text = _content_to_text(message.get("content"))
    calls: list[dict[str, Any]] = []
    native = message.get("tool_calls")
    if isinstance(native, list):
        for item in native:
            entry = item if isinstance(item, dict) else {}
            fn = entry.get("function") if isinstance(entry.get("function"), dict) else {}
            name = str(fn.get("name") or entry.get("name") or "").strip()
            raw_args = fn.get("arguments", entry.get("args"))
            call: dict[str, Any] = {"tool": name, "args": {}, "callId": str(entry.get("id") or "")}
            if not name:
                call["rejectReason"] = "工具调用里没有工具名，这条调用没被执行（不是「当成别的工具跑掉」）。"
            elif isinstance(raw_args, dict):
                call["args"] = dict(raw_args)
            elif raw_args in (None, ""):
                call["args"] = {}
            elif isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:  # noqa: BLE001
                    call["rejectReason"] = (
                        f"工具「{name}」的参数不是合法 JSON（多半是被截断了），这条调用没被执行；"
                        "要重发就缩短正文或分页，不要指望系统替你补全。"
                    )
                else:
                    if isinstance(parsed_args, dict):
                        call["args"] = parsed_args
                    else:
                        call["rejectReason"] = f"工具「{name}」的参数不是对象（解析成了 {type(parsed_args).__name__}），这条调用没被执行。"
            else:
                call["rejectReason"] = f"工具「{name}」的参数形状不认识（{type(raw_args).__name__}），这条调用没被执行。"
            calls.append(call)

    outcome = ""
    reason = ""
    if not calls:
        protocol = parse_protocol_json(text)
        if protocol is not None:
            text = str(protocol.get("text") or "")
            outcome = str(protocol.get("outcome") or "").strip()
            reason = str(protocol.get("reason") or "")
            for item in protocol.get("calls") or []:
                entry = item if isinstance(item, dict) else {}
                args = entry.get("args") if isinstance(entry.get("args"), dict) else (entry.get("input") if isinstance(entry.get("input"), dict) else {})
                calls.append({"tool": str(entry.get("tool") or entry.get("name") or ""), "args": dict(args or {})})
        elif text.strip().startswith(("{", "```")):
            # 明显是想回 JSON 却没回对：说清楚，不比着一句正文把回合当「仍在进行」拖过去
            raise ModelChannelError(
                "模型回的正文像 JSON 但解析不了，本轮按失败收口（外发一条也不提交）。"
                f"原文前 120 字：{text.strip()[:120]}"
            )
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS] + f"\n（模型正文超过 {MAX_REPLY_CHARS} 字，本层截断，后面还有 {len(text) - MAX_REPLY_CHARS} 字没进对话）"
    out: dict[str, Any] = {"text": text, "calls": calls}
    if outcome:
        out["outcome"] = outcome
    if reason:
        out["reason"] = reason
    if usage:
        out["usage"] = dict(usage)
    return out


# ── 传输：一次 urllib POST（引擎多处同款，不新引依赖）──────────────
def completions_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ModelChannelError("模型通道没有可用的 baseUrl（配置解析成了 none），本轮不起回合")
    if base.endswith(COMPLETIONS_SUFFIX):
        return base
    return f"{base}/{COMPLETIONS_SUFFIX}"


class TicketModelChannel:
    """工单回合的模型通道：`channel(request)` → `{text, calls, outcome, reason}`。

    两个注入点让这一层在自测里不碰网络，也是真接线时的扩展位：

    * `chat(body, *, url, api_key, timeout)` —— 传输。缺省用 stdlib `urllib`；
      网关模式的短期令牌每次现取（`token_provider()`），不进缓存、不进异常文本。
    * `sleep(seconds)` —— 退避等待，自测里换成记账的假睡眠。
    """

    def __init__(
        self,
        model_config: Any = None,
        *,
        chat: Callable[..., dict[str, Any]] | None = None,
        token_provider: Callable[[], str] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        env: Any = None,
    ):
        self._model_config = model_config
        self._env = env
        self.config = resolve_model_config(model_config, env=env)
        self._chat = chat
        self._token_provider = token_provider
        self._on_usage = on_usage
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep
        #: 上一次调用回报的用量（`promptTokens` 是「按剩余窗口取页」的实测用量来源）
        self.last_usage: dict[str, Any] = {}
        #: 观测用计数（快照里给接线方看，不是账本）
        self.calls = 0
        self.retries = 0
        self.last_error = ""

    # ── 配置现读 ──────────────────────────────────────────────
    def resolve(self) -> dict[str, Any]:
        """每用一次重算一次「这轮该打哪儿」，不缓存构造期那份判定。

        模型配置是用户在设置页里改的，引擎不重启：构造时算好的那份会一直打到旧端点，而
        `ready()` 与 `describe()` 说的是新配置——两边不一致时，「配好了」的面板底下跑的
        还是没配好的那条路。重算很便宜（读几个环境变量），代价换的是「配好模型之后下一轮
        立刻生效」，也是 `configure(model_config=…)` 不必重建装配的前提。"""
        fresh = resolve_model_config(self._model_config, env=self._env)
        if fresh != self.config:
            self.config = fresh
        return self.config

    def set_model_config(self, model_config: Any, *, transport: dict[str, Any] | None = None) -> dict[str, Any]:
        """换掉配置来源并当场重算（端点「保存模型设置」之后不必重启引擎、不必重建装配）。

        只换配置，不换通道对象：活着的 worker、槽位、耐久箱都挂在这套装配上，为了换个
        模型把它们拆掉是比「多等一轮」严重得多的事。

        `transport` 给的是随配置一起换的配套件（`token_provider` / `on_usage`）：**键在就换、
        不在就不动**。直连模式换成网关模式时，配置换了但换票件还是 None，`_api_key()` 会当场
        抛「网关模式没有换票件」——那条路只有换过模式的下一轮才会撞上，构造期的自测看不见。
        省略 transport 就是「只换端点、不换传输」，那是改模型名/换 baseUrl 的常态。"""
        if transport is not None:
            if "token_provider" in transport:
                self._token_provider = transport["token_provider"]
            if "on_usage" in transport:
                self._on_usage = transport["on_usage"]
        self._model_config = model_config
        self.config = resolve_model_config(model_config, env=self._env)
        return self.config

    # ── 对外 ──────────────────────────────────────────────────
    def ready(self) -> bool:
        return self.resolve().get("mode") != "none"

    def describe(self) -> dict[str, Any]:
        """自述：模式 / 模型名 / 端点主机。密钥与令牌一律不出现在这份字典里。"""
        self.resolve()
        base = str(self.config.get("baseUrl") or "")
        host = ""
        try:
            from urllib.parse import urlsplit

            host = (urlsplit(base).hostname or "").strip().lower()
        except Exception:  # noqa: BLE001
            host = ""
        return {
            "mode": self.config.get("mode", "none"),
            "modelName": self.config.get("modelName", ""),
            "host": host,
            "reason": self.config.get("reason", ""),
            # 窗口与本轮实测用量：状态页靠它说清「按剩余窗口取页」到底生没生效。
            # `contextWindowFromModel=False` 意味着这个窗口是估算值，页面不能把它说成用户填的。
            "contextWindow": int(self.config.get("contextWindow") or 0),
            "contextWindowFromModel": bool(self.config.get("contextWindowFromModel")),
            "lastPromptTokens": _int_or_zero(self.last_usage.get("promptTokens")),
            "usageFromReport": bool(self.last_usage.get("promptTokensFromReport")),
            "calls": self.calls,
            "retries": self.retries,
            "lastError": self.last_error,
        }

    def __call__(self, request: Any) -> dict[str, Any]:
        req = request if isinstance(request, dict) else {}
        if not self.ready():
            raise ModelChannelError(str(self.config.get("reason") or "模型通道没有可用配置，本轮不起回合"))
        system = str(req.get("system") or "")
        if PROTOCOL_NOTE not in system:
            system = f"{system}\n\n{PROTOCOL_NOTE}".strip()
        body: dict[str, Any] = {
            "model": self.config.get("modelName") or DEFAULT_MODEL_NAME,
            "messages": to_openai_messages(system, req.get("messages")),
            "tools": worker_tool_schemas(),
            "tool_choice": "auto",
            "stream": False,
        }
        started = time.monotonic()
        payload = self._post_with_retry(body)
        reply = normalize_reply(payload)
        usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
        reported = _int_or_zero(usage.get("prompt_tokens"))
        latency_ms = int((time.monotonic() - started) * 1000)
        # 进**用量台账**的那一份：只有上游真回了 usage 才回调，字段一个不自造。
        # 这条既有口径被自测钉着（「模型没回 usage 就不自造台账」）：本地折算是给取页用的
        # 保守读数，把它当实测写进台账，用量页就出现了一行谁也没花掉的 tokens。
        ledger_record: dict[str, Any] = {
            "origin": USAGE_ORIGIN,
            "model": body["model"],
            "promptTokens": usage.get("prompt_tokens"),
            "completionTokens": usage.get("completion_tokens"),
            "totalTokens": usage.get("total_tokens"),
            # 端到端耗时，含退避重试的等待：工单回合占着槽位等的那段时间是真成本，
            # 只记最后一次往返会让「一次调用 40 秒」在用量页上显示成「一次很快」。
            "latencyMs": latency_ms,
        }
        # 取页用的那份**允许**折算（并带 `promptTokensFromReport` 说明它是折算值）：
        # 宁可这一页按保守量收窄，也不要「没有实测用量 ⇒ 当窗口空着」把整轮顶穿。
        self.last_usage = dict(
            ledger_record,
            promptTokens=reported or self._estimate_prompt_tokens(body, system),
            promptTokensFromReport=bool(reported),
        )
        if isinstance(reply.get("usage"), dict) and self._on_usage is not None:
            self._on_usage(ledger_record)
        return reply

    def _estimate_prompt_tokens(self, body: dict[str, Any], system: str) -> int:
        """上游没回用量时，按**发出去的字符**折算本轮 prompt 的量（与压缩层同一族折算口径）。"""
        chars = len(str(system or ""))
        for message in body.get("messages") or []:
            chars += len(str(message.get("content") or ""))
        try:
            chars += len(json.dumps(body.get("tools") or [], ensure_ascii=False))
        except Exception:  # noqa: BLE001  工具清单里塞了不可序列化对象，不能因此让整轮失败
            pass
        return max(1, int(chars / max(float(ta.TOKENS_TO_UNITS), 1.0)))

    def window_state(self) -> dict[str, Any]:
        """给工具面的本轮窗口读数：`{contextWindowTokens, usedTokens, fromModel, usageFromReport}`。

        `usedTokens` 取**上一次调用回报的 prompt 量**——本轮新产生的工具结果还没被算进去，那一部分
        由 `window_budget` 的预留比例兜。这不是精确值，是一个明显偏保守的实测下界：宁可这一页
        少给一点，也不要按「猜还剩很多」把整轮顶穿。"""
        self.resolve()
        used = _int_or_zero(self.last_usage.get("promptTokens"))
        return {
            "contextWindowTokens": int(self.config.get("contextWindow") or 0),
            "contextWindowFromModel": bool(self.config.get("contextWindowFromModel")),
            "usedTokens": used,
            "usageFromReport": bool(self.last_usage.get("promptTokensFromReport")),
        }

    # ── 传输细节 ───────────────────────────────────────────────
    def _api_key(self) -> str:
        if self.config.get("mode") == "gateway":
            if self._token_provider is None:
                raise ModelChannelError("网关模式没有换票件（token_provider）：拿不到短期令牌，本轮不起回合")
            token = str(self._token_provider() or "").strip()
            if not token:
                raise ModelChannelError("网关令牌换取结果为空：登录状态可能已失效，本轮不起回合")
            return token
        return str(self.config.get("apiKey") or "")

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        url = completions_url(str(self.config.get("baseUrl") or ""))
        attempts = self.max_retries + 1
        last: ModelChannelError | None = None
        for index in range(attempts):
            self.calls += 1
            try:
                if self._chat is not None:
                    return dict(self._chat(body, url=url, api_key=self._api_key(), timeout=self.timeout_seconds) or {})
                return self._urllib_chat(body, url)
            except ModelChannelError as exc:
                last = exc
                self.last_error = str(exc)
                if not exc.retryable or index + 1 >= attempts:
                    break
                self.retries += 1
                self._sleep(RETRY_BACKOFF_SECONDS * (index + 1))
        if last is None:  # 不会到这里，留个兜底不吞异常
            raise ModelChannelError("模型通道没有产生任何结果，本轮不起回合")
        raise last

    def _urllib_chat(self, body: dict[str, Any], url: str) -> dict[str, Any]:
        api_key = self._api_key()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            request = urllib.request.Request(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers=headers
            )
        except ValueError as exc:
            raise ModelChannelError(f"模型端点地址无效：{type(exc).__name__}: {exc}") from exc
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            detail = ""
            try:
                detail = exc.read(2048).decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            raise ModelChannelError(
                f"模型调用被上游拒绝（HTTP {exc.code}）：{detail or exc.reason or '无更多说明'}",
                retryable=retryable,
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ModelChannelError(f"模型端点连不上：{exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise ModelChannelError(f"模型调用超过 {self.timeout_seconds} 秒没回话", retryable=True) from exc
        except OSError as exc:
            raise ModelChannelError(f"模型调用遇到网络错误：{type(exc).__name__}: {exc}", retryable=True) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelChannelError(f"模型响应超过 {MAX_RESPONSE_BYTES} 字节上限（多半不是 JSON 响应），本轮不采信")
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            raise ModelChannelError(f"模型响应不是合法 JSON：{type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ModelChannelError(f"模型响应顶层不是对象（是 {type(payload).__name__}），本轮不采信")
        return payload


def build_model_channel(
    model_config: Any = None,
    *,
    chat: Callable[..., dict[str, Any]] | None = None,
    token_provider: Callable[[], str] | None = None,
    on_usage: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> TicketModelChannel:
    """接线侧的一行构造（端点里 `model_call=build_model_channel(model_config)`）。"""
    return TicketModelChannel(
        model_config=model_config,
        chat=chat,
        token_provider=token_provider,
        on_usage=on_usage,
        **kwargs,
    )
