"""
通天办公 - IM 群策略与重复抑制（缺陷 D-41 的「群策略」那一半）

此前框架对入站消息只有一个判断：`kind == "inbound"` 就起任务、就回贴。群里一个人连发
「在吗」「好的」「[图片]」三条，机器人就答三次；不 @ 它的闲聊也会被当成任务。这不是
「策略太宽松」，是**根本没有策略**——回贴次数、群内是否响应、上下文算不算数，
全由「对端发了一条文本」这一件事决定。

这一层把决定权拿回来，并且把口径写成引擎里唯一的一份事实（界面不另抄一份映射）：

- **群内触发**（`groupActivation`）：`mention`（默认，被 @ 才起任务）/ `always`（每条都起）
  / `never`（群里一律不起任务）。默认是 mention：机器人在群里对每句话都答，
  等于替所有群成员做决定，也一定会踩到平台限流。
- **单聊触发**（`dmActivation`）：`always` / `mention` / `never`。`never` 是给「只想用群、
  不想让人私聊驱动本机 Agent」留的口子；拒答时给出中文原因，不是静默不回。
- **上下文**（`groupContext`）：`off`（默认，非 @ 消息直接丢弃不记）/ `memory`（非 @ 消息
  只在本进程内存里攒着，被 @ 时作为上下文并进任务文本）。**没有 persist 这一档**：
  群聊内容属于群里所有人，落盘等于把别人的话存到本机台账里，这一层不提供该开关。
  攒的条数与字数都有上限，超了从旧到新丢。
- **历史窗口**（`historyWindowMinutes`）：`memory` 模式下超过这个时长的上下文一律作废——
  三小时前的闲聊和刚才这句「帮我改一下」不是一件事。
- **重复抑制**（`dedupeSeconds`）：同 `message_id` 或同会话同文本在窗口内只处理一次。
  钉钉/Slack 都会因网络重发同一帧，企业微信官方明确要求按 msgid 排重；框架层再兜一次，
  因为「重复回贴」在用户看到的就是机器人发了两遍。
- **发送限额**（`maxMessagesPerMinute` / `burst`）与**回贴超时**（`sendTimeoutSeconds`）：
  数值在这里（配置事实），令牌桶机制在 `im_ratelimit.py`（运行事实）。

诚实边界：
1. 「被 @」的判定来自各通道解析的 `mentioned_me`，框架不猜。通道答不出来的平台，
   `mention` 策略会如实拒（原因里说明「本通道未解析 @ 名单」），而不是当成被 @ 起任务。
2. 这一层不碰任何网络、不写凭据、不起连接；`save_policy` 只落一份 JSON。
3. 上下文缓冲**只在内存**：进程重启就没了，界面上不许画成「已保存的群历史」。
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import data_paths as dp

# ── 枚举与出厂默认（界面直接读这张表，不在前端复制第二份） ────────────

ACTIVATIONS = ("mention", "always", "never")
CONTEXT_MODES = ("off", "memory")

ACTIVATION_LABELS = {
    "mention": "被 @ 时才回应",
    "always": "每条都回应",
    "never": "不回应",
}
CONTEXT_MODE_LABELS = {
    "off": "不保留群内其它消息",
    "memory": "被 @ 时把窗口内的群消息作为上下文（仅内存，重启即失）",
}

#: 数值项的范围与中文说明：越界一律拒，不做「收下但按默认跑」
NUMERIC_BOUNDS: dict[str, tuple[int, int, str]] = {
    "historyWindowMinutes": (1, 720, "历史窗口（分钟）"),
    "dedupeSeconds": (0, 600, "重复抑制窗口（秒）"),
    "maxMessagesPerMinute": (1, 120, "每分钟最多主动发几条"),
    "burst": (1, 20, "允许突发几条"),
    "sendTimeoutSeconds": (2, 120, "单条回贴超时（秒）"),
}

#: 上下文缓冲的硬顶：群聊刷屏不能把内存吃满，也决定了并进任务文本的最大字数
MAX_CONTEXT_MESSAGES = 20
MAX_CONTEXT_CHARS = 4000

POLICY_DEFAULTS: dict[str, Any] = {
    "groupActivation": "mention",
    "dmActivation": "always",
    "groupContext": "off",
    "historyWindowMinutes": 30,
    "dedupeSeconds": 45,
    "maxMessagesPerMinute": 10,
    "burst": 3,
    "sendTimeoutSeconds": 20,
    # 群消息里的附件是否下载落盘：默认 off。附件是别人发的，替用户往本机写文件
    # 需要一个明确的选择；关掉时消息仍会带着「收到了 1 张图片」的中文说明进任务文本。
    "groupMedia": "off",
    "dmMedia": "on",
}
MEDIA_MODES = ("off", "on")

ENUM_KEYS = ("groupActivation", "dmActivation", "groupContext", "groupMedia", "dmMedia")
ENUM_TABLE = {
    "groupActivation": ACTIVATIONS,
    "dmActivation": ACTIVATIONS,
    "groupContext": CONTEXT_MODES,
    "groupMedia": MEDIA_MODES,
    "dmMedia": MEDIA_MODES,
}


class PolicyError(Exception):
    """`CODE: 中文原因`——与 IM 框架同一口径，由 connect 车道翻成 ok=false + message"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ── 落盘 ───────────────────────────────────────────────────────────


def policy_path() -> Path:
    """策略与凭据同根但**不同文件**：改策略不该重写凭据，反之亦然"""
    return dp.engine_home() / "im_group_policy.json"


def _read_raw() -> dict:
    try:
        raw = json.loads(policy_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        # 与台账同法：坏了不覆盖，改名留档，读的人拿到的永远是出厂默认而不是半套配置
        backup = policy_path().with_suffix(".corrupt.json")
        try:
            policy_path().replace(backup)
        except Exception:
            pass
        return {}
    return raw if isinstance(raw, dict) else {}


def load_store() -> dict:
    """整本策略表：{defaults, scopes:{scope: {...}}}，缺项一律补出厂默认"""
    raw = _read_raw()
    defaults = dict(POLICY_DEFAULTS)
    stored_defaults = raw.get("defaults")
    if isinstance(stored_defaults, dict):
        for key in POLICY_DEFAULTS:
            value = stored_defaults.get(key)
            if _valid_value(key, value) == "":
                defaults[key] = value
    scopes = raw.get("scopes")
    clean_scopes: dict[str, dict] = {}
    if isinstance(scopes, dict):
        for scope, patch in scopes.items():
            if not isinstance(patch, dict):
                continue
            cleaned = {}
            for key, value in patch.items():
                if key in POLICY_DEFAULTS and _valid_value(key, value) == "":
                    cleaned[key] = value
            if cleaned:
                clean_scopes[str(scope)] = cleaned
    return {"defaults": defaults, "scopes": clean_scopes}


def _flush(store: dict) -> None:
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def normalize_patch(patch: dict) -> dict:
    """只认已知键 + 正确取值：未知键直接报，不做「先收下再说」

    设置页拨了个开关若没真的生效，用户看到的就是假状态——与偏好项同一口径。
    """
    if not isinstance(patch, dict) or not patch:
        raise PolicyError("IM_POLICY_INVALID", "未提交任何策略项")
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in POLICY_DEFAULTS:
            raise PolicyError("IM_POLICY_INVALID", f"未知策略项：{key}")
        reason = _valid_value(key, value)
        if reason:
            raise PolicyError("IM_POLICY_INVALID", reason)
        out[key] = value
    return out


def _valid_value(key: str, value: Any) -> str:
    """返回中文拒绝理由；空串表示这个值可用"""
    if key in ENUM_TABLE:
        allowed = ENUM_TABLE[key]
        if not isinstance(value, str) or value not in allowed:
            label = {
                "groupActivation": "群内触发方式",
                "dmActivation": "单聊触发方式",
                "groupContext": "群内上下文保留",
                "groupMedia": "群内附件落盘",
                "dmMedia": "单聊附件落盘",
            }.get(key, key)
            return f"{label}只接受 {', '.join(allowed)}（收到 {value!r}）"
        return ""
    if key in NUMERIC_BOUNDS:
        low, high, label = NUMERIC_BOUNDS[key]
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{label}需要整数（收到 {type(value).__name__}）"
        if not low <= value <= high:
            return f"{label}要在 {low} 到 {high} 之间（收到 {value}）"
        return ""
    return f"未知策略项：{key}"


def save_policy(patch: dict, scope: str = "") -> dict:
    """写策略：scope 为空写全局默认，否则写该作用域（通道实例标识或会话标识）

    作用域串同样要消毒：它会成为文件里的键与目录名的一部分，`"../x"` 不是合法作用域。
    """
    cleaned = normalize_patch(patch)
    store = load_store()
    scope = str(scope or "").strip()
    if scope:
        if len(scope) > 120 or any(ch in scope for ch in ("/", "\\", "..", "\n")):
            raise PolicyError("IM_POLICY_INVALID", f"作用域标识不合法：{scope[:40]}")
        entry = dict(store["scopes"].get(scope) or {})
        entry.update(cleaned)
        store["scopes"][scope] = entry
    else:
        store["defaults"].update(cleaned)
    _flush(store)
    return store


def drop_scope(scope: str) -> dict:
    """删掉某个作用域（解绑通道实例时用），全局默认不动"""
    store = load_store()
    store["scopes"].pop(str(scope or "").strip(), None)
    _flush(store)
    return store


def instance_scope(channel_id: str = "", kind: str = "") -> str:
    """通道实例的作用域标识：与 `ChannelRuntime.instance_key` 同形（`kind:实例标识`）

    不带类型就会跨类型撞车：用户给钉钉企业起的实例名和给飞书 workspace 起的实例名
    都叫 `work` 时，其中一条改的「群内触发」会静默作用到另一条上——那是凭空放宽了
    哪些群消息会起任务。媒体目录、限流桶、会话台账、凭据都按 instance_key 分账，
    策略面没有理由继续用裸标识（`ChannelRuntime` 的注释早就写了「群策略挂在
    instance_key 上」，这一条是把它做到）。

    `channel_id` 为空或就等于 `kind` 时回裸值：那是 D-41 之前的唯一实例形态，
    旧配置文件与旧作用域不改写也照样生效。
    """
    ident = str(channel_id or "").strip()
    if not ident:
        return ""
    name = str(kind or "").strip()
    if not name or ident == name:
        return ident
    return f"{name}:{ident}"


def scope_chain(channel_id: str = "", kind: str = "", conversation_id: str = "") -> list[str]:
    """由细到粗的作用域链：会话 → 通道实例（`kind:实例标识`）→ 同类型通道的旧全局键 → 默认

    第三级是为了兼容 D-41 之前的单实例形态：那时 `channel_id` 就等于 `kind`，
    老配置（以及只填了 kind 的调用）不能因为引入多实例而失效。
    """
    chain: list[str] = []
    if conversation_id:
        chain.append(str(conversation_id))
    instance = instance_scope(channel_id, kind)
    if instance and instance not in chain:
        chain.append(instance)
    if kind and kind != instance:
        chain.append(str(kind))
    return chain


def effective(channel_id: str = "", kind: str = "", conversation_id: str = "", store: dict | None = None) -> dict:
    """生效策略：作用域链从粗到细逐项覆盖，返回的一定是完整一份"""
    store = store or load_store()
    merged = dict(store.get("defaults") or POLICY_DEFAULTS)
    for scope in reversed(scope_chain(channel_id, kind, conversation_id)):
        override = (store.get("scopes") or {}).get(scope)
        if isinstance(override, dict):
            for key, value in override.items():
                if key in merged and _valid_value(key, value) == "":
                    merged[key] = value
    return merged


# ── 决定：一条入站消息该不该起任务 ──────────────────────────────────


@dataclass
class MessageMeta:
    """判定需要的最小事实（由通道的 parse_event 填；答不出来的字段就留空）"""

    is_group: bool = False
    mentioned_me: bool | None = None  # None = 平台/通道没告诉我们
    conversation_id: str = ""
    message_id: str = ""
    text: str = ""
    at_ms: int = 0
    # 附件身份（`im_media.parts_identity`）：一条带图片的消息，它的「文本」是引擎拼的
    # 中文说明，两张不同的照片说明可能一模一样——只看文本会把第二张当重复吞掉。
    media_identity: str = ""
    has_media: bool = False


@dataclass
class Decision:
    run: bool  # 起不起任务
    reason: str = ""  # 不起任务的中文原因（会进台账与界面，绝不空着糊过去）
    context: list[str] = field(default_factory=list)  # 决定起任务时并进去的上下文
    remember: bool = False  # 这条是否作为上下文攒着（不起任务但保留）

    def as_view(self) -> dict:
        return {
            "run": self.run,
            "reason": self.reason,
            "contextCount": len(self.context),
            "remember": self.remember,
        }


def decide(meta: MessageMeta, policy: dict, *, now_ms: int | None = None,
           buffer: "ContextBuffer | None" = None) -> Decision:
    """纯判定：不碰磁盘、不碰网络、不看时钟以外的可变量

    `now_ms` 由调用方给（测试注入），缺省取当前毫秒。
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    at_ms = int(meta.at_ms or now_ms)
    mode = str(policy.get("groupActivation" if meta.is_group else "dmActivation") or "mention")

    if meta.is_group and mode == "never":
        return Decision(run=False, reason="群内策略为「不回应」：本条消息未起任务，也不回贴")
    if not meta.is_group and mode == "never":
        return Decision(run=False, reason="单聊策略为「不回应」：本条消息未起任务，也不回贴")

    if meta.is_group and mode == "mention":
        if meta.mentioned_me is True:
            return _run_with_context(meta, policy, now_ms, buffer)
        if meta.mentioned_me is False:
            remember = _note_context(meta, policy, now_ms, buffer)
            return Decision(
                run=False,
                reason="群里没有被 @：按策略不起任务" + ("（已作为上下文暂存，等被 @ 时并入）" if remember else ""),
                remember=remember,
            )
        # 通道答不出 @ 名单：mention 策略下不能猜「大概是被 @ 了」，也不能当成没被 @
        return Decision(
            run=False,
            reason="本通道答不出「这条有没有 @ 我」，按「被 @ 才回应」的策略不能凭空判定，已拒起任务"
                   "（要把群内每条消息都交给任务，请在 IM 设置里把该通道/该群的群内触发改为「每条都处理」）",
        )

    # always（群与单聊都到这）：起任务；群内 always 也照收上下文，行为一致
    return _run_with_context(meta, policy, now_ms, buffer)


def _run_with_context(meta: MessageMeta, policy: dict, now_ms: int, buffer: "ContextBuffer | None") -> Decision:
    drained = buffer.take(meta.conversation_id, now_ms) if buffer else []
    return Decision(run=True, reason="", context=drained)


def _note_context(meta: MessageMeta, policy: dict, now_ms: int, buffer: "ContextBuffer | None") -> bool:
    if not meta.is_group or not buffer:
        return False
    if str(policy.get("groupContext") or "off") != "memory":
        return False
    text = str(meta.text or "").strip()
    if not text:
        return False
    window_ms = int(policy.get("historyWindowMinutes") or POLICY_DEFAULTS["historyWindowMinutes"]) * 60_000
    return buffer.remember(meta.conversation_id, text, now_ms, window_ms)


class ContextBuffer:
    """群内非 @ 消息的**内存**缓冲：只在被 @ 那一次被读走，不落盘

    三道上界，都是从旧到新丢：条数（`MAX_CONTEXT_MESSAGES`）、字数（`MAX_CONTEXT_CHARS`）、
    时间窗（`historyWindowMinutes`）。刷屏最坏只会丢掉最早的闲聊，不会把这段缓冲变成
    无限增长的内存；三小时前的话也不会被当成「刚才那句的上下文」。
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, deque[tuple[int, str]]] = OrderedDict()
        self._window: dict[str, int] = {}
        self.dropped = 0

    def remember(self, conversation_id: str, text: str, now_ms: int, window_ms: int) -> bool:
        if not conversation_id or not text:
            return False
        self._window[conversation_id] = max(int(window_ms), 0)
        bucket = self._items.setdefault(conversation_id, deque())
        bucket.append((now_ms, text))
        self._trim(conversation_id, bucket, now_ms)
        return bool(bucket) and bucket[-1] == (now_ms, text)

    def pending(self, conversation_id: str, now_ms: int | None = None) -> int:
        """窗口内还留着几条（读数用 `now_ms` 注入，测试不靠真实时钟）"""
        bucket = self._items.get(conversation_id)
        if not bucket:
            return 0
        return len(self._fresh(conversation_id, bucket, int(now_ms if now_ms is not None else time.time() * 1000)))

    def take(self, conversation_id: str, now_ms: int) -> list[str]:
        """被 @ 时读走窗口内的上下文，并清空（同一批话不该在两次任务里各并一次）

        窗口值要在弹出后自己带着用：先弹窗口再判新鲜度等于把「过期」判没了。
        """
        bucket = self._items.pop(conversation_id, None)
        window_ms = self._window.pop(conversation_id, 0)
        if not bucket:
            return []
        fresh = [(at, text) for at, text in bucket if window_ms <= 0 or now_ms - int(at) < window_ms]
        self.dropped += len(bucket) - len(fresh)
        return [text for _, text in fresh]

    def forget(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)
        self._window.pop(conversation_id, None)

    def _fresh(self, conversation_id: str, bucket: deque[tuple[int, str]], now_ms: int) -> list[tuple[int, str]]:
        window_ms = self._window.get(conversation_id, 0)
        if window_ms <= 0:
            return list(bucket)
        return [(at, text) for at, text in bucket if now_ms - int(at) < window_ms]

    def _trim(self, conversation_id: str, bucket: deque[tuple[int, str]], now_ms: int) -> None:
        fresh = self._fresh(conversation_id, bucket, now_ms)
        self.dropped += len(bucket) - len(fresh)
        bucket.clear()
        bucket.extend(fresh)
        while len(bucket) > MAX_CONTEXT_MESSAGES:
            bucket.popleft()
            self.dropped += 1
        total = sum(len(t) for _, t in bucket)
        while bucket and total > MAX_CONTEXT_CHARS:
            total -= len(bucket.popleft()[1])
            self.dropped += 1


# ── 重复抑制 ───────────────────────────────────────────────────────


class MessageDedupe:
    """同 `message_id` 或同会话同内容在窗口内只放行一次

    为什么两条都判：钉钉/Slack 的重发有时带同一个 message_id，有时（用户手抖连点）
    是两条不同 id 的同样文本。只按 id 排重挡得住协议重发，挡不住后者；
    只按文本排重会把「两小时里问了两次同一个问题」也吞掉，所以有窗口。

    带附件的那条按「文本 + 附件身份」一起判：纯图片消息的文本是引擎拼的中文说明，
    两张不同的照片能拼出逐字相同的一句，只比文本就会误吞（见 `_keys` 里那个分支）。
    """

    MAX_KEYS = 2048

    def __init__(self) -> None:
        self._at: OrderedDict[str, int] = OrderedDict()
        self.hits = 0

    def _keys(self, meta: MessageMeta) -> list[str]:
        out = []
        if meta.message_id:
            out.append(f"id:{meta.conversation_id}|{meta.message_id}")
        text = str(meta.text or "").strip()
        media = str(meta.media_identity or "").strip()
        if meta.has_media and not media:
            # 带附件但平台没给可区分的引用码：这条的「文本」是引擎拼的说明，
            # 连发的两条不同消息说明可能逐字相同——宁可只按 message_id 判，
            # 也不凭一句自己写出来的话把用户的第二条消息当重复吃掉。
            return out
        if text or media:
            out.append(f"text:{meta.conversation_id}|{text[:200]}|{media[:200]}")
        return out

    def seen_recently(self, meta: MessageMeta, window_seconds: int, now_ms: int) -> bool:
        keys = self._keys(meta)
        if not keys:
            return False
        window_ms = max(int(window_seconds), 0) * 1000
        hit = any(self._is_recent(key, now_ms, window_ms) for key in keys)
        if hit:
            self.hits += 1
            return True
        for key in keys:
            self._at[key] = now_ms
            self._at.move_to_end(key)
        while len(self._at) > self.MAX_KEYS:
            self._at.popitem(last=False)
        return False

    def _is_recent(self, key: str, now_ms: int, window_ms: int) -> bool:
        at = self._at.get(key)
        if at is None:
            return False
        if window_ms <= 0:
            return False
        return 0 <= now_ms - int(at) < window_ms

    def forget(self, conversation_id: str) -> None:
        for key in [k for k in self._at if f":{conversation_id}|" in k]:
            self._at.pop(key, None)

    def note(self, window_seconds: int) -> str:
        """界面用的中文读数（不是错误，是状态说明）"""
        if window_seconds <= 0:
            return "未做重复抑制：同一句话重发会各起一次任务"
        return f"{window_seconds} 秒内同一条消息（或同会话同样的内容）只处理一次"


__all__ = [
    "ACTIVATION_LABELS",
    "ACTIVATIONS",
    "CONTEXT_MODE_LABELS",
    "CONTEXT_MODES",
    "ContextBuffer",
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_MESSAGES",
    "MessageDedupe",
    "MessageMeta",
    "NUMERIC_BOUNDS",
    "POLICY_DEFAULTS",
    "Decision",
    "PolicyError",
    "decide",
    "drop_scope",
    "effective",
    "instance_scope",
    "load_store",
    "normalize_patch",
    "policy_path",
    "save_policy",
    "scope_chain",
]
