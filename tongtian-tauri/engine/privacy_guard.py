"""模型出站内容脱敏（PII / 明文密钥）

规则与 Rust `security/classify.rs` 同源：身份证号、手机号、银行卡号（容忍空格/
连字符分组）、邮箱、`sk-`/`sk_` 明文密钥。这里只处理**发给模型的文本**：

- `off`：原样发送（默认，保持既有行为）；
- `mask`：命中即掩码后发送，并把命中数量报给上层事件流；
- `block`：命中即中止本次模型调用（任务以 `PRIVACY_OUTBOUND_BLOCKED` 收尾）。

边界：
- 只覆盖文本；图片附件/截图以 base64 下发，内容无法检测或脱敏；
- 工具结果与历史消息一旦进入模型上下文也会脱敏，模型看到的可能与本地文件不完全一致；
- 浏览器、连接器子进程、Hook 脚本自身的外发不经过这里（它们各自有独立网关/授权）。
"""

from __future__ import annotations

import copy
import json
from typing import Any

MODES = ("off", "mask", "block")
DEFAULT_MODE = "off"
MODE_LABELS = {
    "off": "关闭",
    "mask": "脱敏后发送",
    "block": "命中即中止",
}

# 这些键下的整棵子树原样保留：图片 base64、媒体地址与结构字段
_SKIP_KEYS = frozenset({
    "data", "url", "image_url", "type", "role", "name", "id", "tool_call_id",
    "media_type", "model", "provider", "format", "source",
})
# 这些键下的字符串按正文处理（包括嵌套的非固定结构，如工具入参）
_TEXT_KEYS = frozenset({
    "content", "text", "arguments", "output", "input", "detail", "prompt",
    "message", "function", "query", "body",
})

_COUNT_LABELS = (
    ("idCard", "身份证号"),
    ("phone", "手机号"),
    ("bankCard", "银行卡号"),
    ("email", "邮箱"),
    ("secret", "明文密钥"),
)


class PrivacyBlocked(RuntimeError):
    """出站内容命中敏感信息且档位为 block。"""

    def __init__(self, counts: dict[str, int]):
        self.counts = dict(counts)
        super().__init__(
            "PRIVACY_OUTBOUND_BLOCKED: 出站内容命中"
            f"{describe(counts)}，已中止本次模型调用；"
            "可在「设置 → 系统设置 → 数据共享」改为脱敏后发送或关闭。"
        )


def normalize_mode(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def from_runtime(runtime: dict | None, fallback: str = DEFAULT_MODE) -> str:
    """runtime.privacy.mode 优先，其次浏览器直连随请求下发的档位。"""
    block = (runtime or {}).get("privacy") if isinstance(runtime, dict) else None
    if isinstance(block, dict) and block.get("mode") is not None:
        return normalize_mode(block.get("mode"))
    return normalize_mode(fallback)


def runtime_error(runtime: dict | None) -> str:
    """Rust 侧读档位失败时（非法值 fail-closed 为 off）把原因摊给用户。"""
    block = (runtime or {}).get("privacy") if isinstance(runtime, dict) else None
    if isinstance(block, dict):
        return str(block.get("error") or "").strip()
    return ""


def mode_note(mode: str) -> str:
    if mode == "mask":
        return (
            "出站脱敏已开启（脱敏后发送）：身份证号、手机号、银行卡号、邮箱与明文密钥"
            "在发给模型前替换为掩码；图片附件与工具自身的外发不经过这里。"
        )
    if mode == "block":
        return (
            "出站脱敏已开启（命中即中止）：模型调用前检测到身份证号、手机号、银行卡号、"
            "邮箱或明文密钥会直接中止任务；图片附件与工具自身的外发不经过这里。"
        )
    return ""


def _digit_runs(text: str) -> list[tuple[int, int, str]]:
    """数字串扫描：单个空格/连字符若两侧均为数字则并入同一串。

    17 位数字后紧跟 X/x 时并入校验位（18 位身份证）。
    """
    out: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if not text[i].isdigit():
            i += 1
            continue
        start = i
        digits: list[str] = []
        end = start
        j = i
        while j < n:
            ch = text[j]
            if ch.isdigit():
                digits.append(ch)
                end = j + 1
                j += 1
            elif (
                ch in (" ", "-")
                and j + 1 < n
                and text[j + 1].isdigit()
                and digits
            ):
                j += 1
            else:
                break
        if len(digits) == 17 and j < n and text[j] in ("X", "x"):
            digits.append(text[j])
            end = j + 1
            j += 1
        out.append((start, end, "".join(digits)))
        i = j
    return out


def _word_runs(text: str, pred) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    start: int | None = None
    for off, ch in enumerate(text):
        if pred(ch):
            if start is None:
                start = off
        elif start is not None:
            out.append((start, off, text[start:off]))
            start = None
    if start is not None:
        out.append((start, len(text), text[start:]))
    return out


def _is_email_char(ch: str) -> bool:
    return ch.isalnum() or ch in "@._-+"


def _is_secret_char(ch: str) -> bool:
    return ch.isalnum() or ch in "-_"


def is_id_card(value: str) -> bool:
    return (
        len(value) == 18
        and value[:17].isdigit()
        and (value[17].isdigit() or value[17] in ("X", "x"))
    )


def is_phone(value: str) -> bool:
    return len(value) == 11 and value[0] == "1" and value[1] in "3456789" and value.isdigit()


def is_bank_card(value: str) -> bool:
    return 16 <= len(value) <= 19 and value.isdigit()


def is_email(value: str) -> bool:
    parts = value.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    return (
        bool(local)
        and len(domain) >= 3
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
    )


def is_secret(value: str) -> bool:
    return len(value) >= 11 and (value.startswith("sk-") or value.startswith("sk_"))


def _mask(segment: str) -> str:
    n = len(segment)
    if n <= 5:
        return "*" * max(n, 1)
    return segment[:3] + "*" * (n - 5) + segment[-2:]


def _apply_masks(text: str, ranges: list[tuple[int, int]]) -> str:
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out: list[str] = []
    cursor = 0
    for start, end in merged:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(_mask(text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def redact(text: str) -> str:
    """PII 与明文密钥替换为掩码（不返回计数）。"""
    cleaned, _ = redact_with_counts(text)
    return cleaned


def redact_with_counts(text: str) -> tuple[str, dict[str, int]]:
    counts = {key: 0 for key, _ in _COUNT_LABELS}
    ranges: list[tuple[int, int]] = []
    for start, end, digits in _digit_runs(text):
        if is_id_card(digits):
            counts["idCard"] += 1
            ranges.append((start, end))
        elif is_phone(digits):
            counts["phone"] += 1
            ranges.append((start, end))
        elif is_bank_card(digits):
            counts["bankCard"] += 1
            ranges.append((start, end))
    for start, end, token in _word_runs(text, _is_email_char):
        if is_email(token):
            counts["email"] += 1
            ranges.append((start, end))
    for start, end, token in _word_runs(text, _is_secret_char):
        if is_secret(token):
            counts["secret"] += 1
            ranges.append((start, end))
    return _apply_masks(text, ranges), {k: v for k, v in counts.items() if v}


def describe(counts: dict[str, int] | None) -> str:
    parts = [f"{label} {int((counts or {}).get(key) or 0)} 处" for key, label in _COUNT_LABELS
             if int((counts or {}).get(key) or 0) > 0]
    return "、".join(parts)


def _merge_counts(into: dict[str, int], extra: dict[str, int]) -> None:
    for key, value in extra.items():
        into[key] = into.get(key, 0) + int(value or 0)


def _sanitize(value: Any, mode: str, *, text: bool) -> tuple[Any, dict[str, int]]:
    if isinstance(value, str):
        if not text:
            return value, {}
        if mode == "block":
            _, counts = redact_with_counts(value)
            return value, counts
        return redact_with_counts(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        counts: dict[str, int] = {}
        for key, item in value.items():
            if key in _SKIP_KEYS:
                out[key] = item
                continue
            child, extra = _sanitize(item, mode, text=text or key in _TEXT_KEYS)
            out[key] = child
            _merge_counts(counts, extra)
        return out, counts
    if isinstance(value, (list, tuple)):
        items = []
        counts = {}
        for item in value:
            child, extra = _sanitize(item, mode, text=text)
            items.append(child)
            _merge_counts(counts, extra)
        return (items if isinstance(value, list) else tuple(items)), counts
    # AgentScope 的 Msg 是普通对象而不是 dict：只复制并替换 content/metadata，
    # 不改原消息（检查点、历史轮次与事件流仍拿原文）
    if hasattr(value, "content") and hasattr(value, "role"):
        try:
            clone = copy.copy(value)
            content, counts = _sanitize(value.content, mode, text=True)
            clone.content = content
            meta = getattr(value, "metadata", None)
            if meta is not None:
                clean_meta, extra = _sanitize(meta, mode, text=True)
                clone.metadata = clean_meta
                _merge_counts(counts, extra)
            return clone, counts
        except Exception:  # noqa: BLE001
            return value, {}
    return value, {}


def sanitize_payload(value: Any, mode: str) -> tuple[Any, dict[str, int]]:
    """按档位处理一次模型调用的入参；block 档只检测、不改写。"""
    mode = normalize_mode(mode)
    if mode == "off":
        return value, {}
    return _sanitize(value, mode, text=True)


class RedactingModel:
    """模型包装层：每次调用前处理发给模型的文本。

    与 `UsageTrackingModel` / `CheckpointedModel` 一样只透传 `stream` 与
    `__call__`，不改变 ReActAgent 的内核行为。
    """

    def __init__(self, inner, mode: str = DEFAULT_MODE, on_redaction=None):
        self._inner = inner
        self.mode = normalize_mode(mode)
        self._on_redaction = on_redaction
        self.redactions: dict[str, int] = {}

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    @property
    def stream(self) -> bool:
        return self._inner.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._inner.stream = value

    async def __call__(self, *args, **kwargs):
        if self.mode == "off":
            return await self._inner(*args, **kwargs)
        counts: dict[str, int] = {}
        clean_args = []
        for arg in args:
            clean, extra = sanitize_payload(arg, self.mode)
            clean_args.append(clean)
            _merge_counts(counts, extra)
        clean_kwargs = {}
        for key, value in kwargs.items():
            clean, extra = sanitize_payload(value, self.mode)
            clean_kwargs[key] = clean
            _merge_counts(counts, extra)
        self.redactions = counts
        if counts and self.mode == "block":
            raise PrivacyBlocked(counts)
        if counts and self._on_redaction is not None:
            try:
                self._on_redaction(counts)
            except Exception as exc:  # noqa: BLE001
                print(f"[engine] 脱敏回调失败: {exc}", flush=True)
        return await self._inner(*clean_args, **clean_kwargs)


def wrap_model(model, mode: str = DEFAULT_MODE, on_redaction=None):
    normalized = normalize_mode(mode)
    if normalized == "off":
        return model
    return RedactingModel(model, normalized, on_redaction=on_redaction)


def safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)
