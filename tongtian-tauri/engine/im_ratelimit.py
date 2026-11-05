"""
通天办公 - IM 发送限流与退避（缺陷 D-41 的「限流」那一半）

框架此前对回贴没有任何节流：收到一条就发一条，一次会话里连问五个问题就贴五段。
这在真实网关上只有两种结局——被平台 429 掉（然后连接循环里的发送卡到超时），
或者把机器人自己的消息洪水般刷进群。两种都不可读，用户看到的只是「它不回了」。

这一层给每次发送加一道闸，并把「现在为什么不发」说成中文：

1. **令牌桶按通道实例独立**。配额是 per-token / per-app 的，不是全机共享的：
   两个钉钉企业各配一条实例时，A 群刷屏不该把 B 群的回贴挤掉。桶的键是
   `channel_id`（实例）+ 会话，参数取自 `im_policy`（`maxMessagesPerMinute` / `burst`）。
2. **等待有上限，超时不是失败而是「本轮不发」**。`acquire()` 给出还要等多少秒；
   框架不为了回一句话把任务挂住，等不到就落一条中文状态（`view()` 里能看到），
   下一条消息到达时如果冷却过了照样回。
3. **平台说 429 就按平台说的退**。`Retry-After` 支持秒数与 HTTP-date 两种形状，
   解析不出来时用兜底值；上限封顶，避免一个畸形头把发送锁死几小时。
   冷却期内 `check()` 直接拒并给出还剩几秒——这是「退避」的真实含义，
   而不是「再发一次试试」。
4. **发送本身有超时**。`bounded_send()` 用 `asyncio.timeout` 包住一次发送：
   对端不回时不能永远占着这个会话锁（同会话串行，卡住一条就卡住整个群）。
   这条约束也来自常驻连接：作用域栈按任务记录，有界等待只用 `asyncio.timeout`，
   不另起计时器。

诚实边界：桶只统计**本进程发出去与拒掉的**，平台侧的账号级配额（钉钉按 appKey 算、
企业微信按机器人算）我们看不见；所以冷却只能由真实 429 或本地额度推导，
不能声称「已避免一切限流」。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

# ── 常量 ───────────────────────────────────────────────────────────

#: 本地桶最多攒多少待发秒数：桶按分钟配，等待超过这个数就当作「本轮不发」
MAX_WAIT_SECONDS = 120
#: Retry-After 解析失败时的兜底冷却
DEFAULT_RETRY_AFTER_SECONDS = 30
#: 平台头再离谱也不把发送锁超过这个数（畸形 5 亿秒的头不该让机器人哑一天）
MAX_COOLDOWN_SECONDS = 600
#: 记录多少个会话的冷却（超出按到期时间从早到晚清）
MAX_TRACKED_KEYS = 512


class RateLimitError(Exception):
    """`CODE: 中文原因`——IM 框架的发送面用它（与 ImError 同形状，不引 im_channels 以免循环 import）"""

    def __init__(self, code: str, detail: str = "", retryable: bool = True):
        self.code = code
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class Gate:
    """一次发送申请的结论"""

    allowed: bool
    wait_seconds: float = 0.0
    reason: str = ""

    def as_view(self) -> dict:
        return {"allowed": self.allowed, "waitSeconds": round(self.wait_seconds, 2), "reason": self.reason}


class TokenBucket:
    """容量 = burst，速率 = 每分钟条数 / 60

    时间由调用方给（`now_ms`），不自取时钟：自测要能在假时钟上推进，
    而本仓库随包 Python 的单调钟粒度是 15.6ms，靠 sleep 排不出「1 秒后」。
    """

    def __init__(self, *, per_minute: int, burst: int, now_ms: int | None = None) -> None:
        if per_minute < 1 or burst < 1:
            raise RateLimitError("IM_RATE_LIMIT_INVALID", f"限流参数不合法：每分钟 {per_minute} 条、突发 {burst}")
        self.per_minute = int(per_minute)
        self.capacity = float(max(1, min(burst, per_minute)))
        self.rate = self.per_minute / 60.0
        self.tokens = self.capacity
        self.updated_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    def refill(self, now_ms: int) -> None:
        gained = max(0, int(now_ms) - self.updated_ms) / 1000.0 * self.rate
        if gained > 0:
            self.tokens = min(self.capacity, self.tokens + gained)
            self.updated_ms = int(now_ms)

    def try_take(self, now_ms: int) -> bool:
        self.refill(now_ms)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def wait_seconds(self, now_ms: int) -> float:
        """还差多少秒才够发一条（0 表示现在就能发）"""
        self.refill(now_ms)
        if self.tokens >= 1.0:
            return 0.0
        return min(MAX_WAIT_SECONDS, (1.0 - self.tokens) / self.rate)


class SendGovernor:
    """按「通道实例 + 会话」记账的发送闸门：本地额度 + 平台冷却两件事分开算

    两件事必须分开：本地额度用完是「等 3 秒」，平台 429 是「它明确说了多久」。
    混在一个数里就会出现「平台叫我们等 60 秒，我们 5 秒后又去撞一次」。
    """

    def __init__(self, *, per_minute: int = 10, burst: int = 3, now_ms: int | None = None) -> None:
        self.per_minute = int(per_minute)
        self.burst = int(burst)
        self._start_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        self._buckets: dict[str, TokenBucket] = {}
        self._cooldown_ms: dict[str, int] = {}
        self.counters = {"sent": 0, "throttled": 0, "cooled": 0, "timeouts": 0}
        self.last_reason = ""

    # ── 申请 ──────────────────────────────────────────────────────

    def check(self, key: str, now_ms: int | None = None) -> Gate:
        now_ms = self._now(now_ms)
        until = self._cooldown_ms.get(key, 0)
        if until > now_ms:
            wait = (until - now_ms) / 1000.0
            reason = f"平台要求冷却，约 {int(wait) + 1} 秒后才恢复发送"
            self.last_reason = reason
            self.counters["cooled"] += 1
            return Gate(allowed=False, wait_seconds=wait, reason=reason)
        bucket = self._bucket(key, now_ms)
        if bucket.try_take(now_ms):
            self.counters["sent"] += 1
            return Gate(allowed=True)
        wait = bucket.wait_seconds(now_ms)
        reason = (
            f"本地发送限额已到（每分钟 {self.per_minute} 条、突发 {self.burst} 条），"
            f"约 {int(wait) + 1} 秒后可发；本轮不排队死等"
        )
        self.last_reason = reason
        self.counters["throttled"] += 1
        return Gate(allowed=False, wait_seconds=wait, reason=reason)

    def allow(self, key: str, now_ms: int | None = None) -> bool:
        return self.check(key, now_ms).allowed

    def apply_limits(self, per_minute: Any, burst: Any) -> bool:
        """把用户在策略里改的「每分钟条数 / 突发」落到本闸门上

        策略项改了但不落到这里，界面就会出现「用户设了每分钟 60 条、引擎仍按 10 条挡」
        这种说不通的状态；所以每条入站消息都按当前生效策略同步一次。

        只换额度，不碰平台冷却：429 是对方给的秒数，用户改自己的额度不该把它抹掉。
        额度真的变了才重起桶（旧桶的容量是按旧额度算的，留着等于新旧混着扣），
        非法/缺省的取值保持原样，不静默变成 1 条那么严，也不变成不限。
        """
        try:
            minutes = int(per_minute)
            bursts = int(burst)
        except (TypeError, ValueError):
            return False
        if minutes < 1 or bursts < 1:
            return False
        if minutes == self.per_minute and bursts == self.burst:
            return False
        self.per_minute = minutes
        self.burst = bursts
        self._buckets.clear()
        return True

    # ── 平台反馈 ──────────────────────────────────────────────────

    def penalize(self, key: str, retry_after: Any, now_ms: int | None = None, *, why: str = "") -> Gate:
        """收到 429 / 配额提示：按平台说的秒数锁住这个键"""
        now_ms = self._now(now_ms)
        seconds = retry_after_seconds(retry_after)
        self._cooldown_ms[key] = now_ms + int(seconds * 1000)
        self._buckets.pop(key, None)  # 冷却结束重新起桶：带着半桶 token 出去只会再撞一次
        reason = f"对端限流（{why or 'HTTP 429'}）：已暂停发送 {int(seconds)} 秒"
        self.last_reason = reason
        self.counters["cooled"] += 1
        return Gate(allowed=False, wait_seconds=seconds, reason=reason)

    def mark_timeout(self) -> None:
        """一次发送超时：只记一笔账，不加冷却也不动桶

        超时既不是对端限流（那一路是 429 → 按对方秒数冷却），也不等于连接坏了（框架
        因此不打 failed）；但「本轮放弃、不是已送达」这件事必须在读数里数得出来，
        否则界面上只会看到「发出去了又好像没发」。
        """
        self.counters["timeouts"] += 1

    def clear(self, key: str = "") -> None:
        """解绑实例 / 会话消失时清账，不留无人认领的冷却"""
        if key:
            self._cooldown_ms.pop(key, None)
            self._buckets.pop(key, None)
        else:
            self._cooldown_ms.clear()
            self._buckets.clear()

    # ── 读数 ──────────────────────────────────────────────────────

    def status(self, key: str, now_ms: int | None = None) -> dict:
        now_ms = self._now(now_ms)
        until = self._cooldown_ms.get(key, 0)
        bucket = self._bucket(key, now_ms)
        return {
            "key": key,
            "blocked": until > now_ms,
            "cooldownLeftSeconds": max(0.0, (until - now_ms) / 1000.0) if until > now_ms else 0.0,
            "tokens": round(bucket.tokens, 2),
            "capacity": bucket.capacity,
            "perMinute": self.per_minute,
            "waitSeconds": round(self.wait_seconds(key, now_ms), 2),
            "note": self.note(key, now_ms),
        }

    def wait_seconds(self, key: str, now_ms: int | None = None) -> float:
        now_ms = self._now(now_ms)
        until = self._cooldown_ms.get(key, 0)
        if until > now_ms:
            return (until - now_ms) / 1000.0
        return self._bucket(key, now_ms).wait_seconds(now_ms)

    def note(self, key: str, now_ms: int | None = None) -> str:
        """界面直接显示的中文状态（验收项里的「429 时退避并显示中文状态」）"""
        now_ms = self._now(now_ms)
        until = self._cooldown_ms.get(key, 0)
        if until > now_ms:
            return f"对端要求退避，还剩 {int((until - now_ms) / 1000) + 1} 秒"
        bucket = self._bucket(key, now_ms)
        if bucket.tokens >= 1.0:
            return f"发送通畅（每分钟 {self.per_minute} 条额度，此刻余 {int(bucket.tokens)} 条）"
        return f"本地额度已用满，约 {int(bucket.wait_seconds(now_ms)) + 1} 秒后恢复"

    def sweep(self, now_ms: int | None = None) -> int:
        """清掉已到期冷却与长期不活跃的桶，键数有上界"""
        now_ms = self._now(now_ms)
        dropped = 0
        for key in [k for k, v in self._cooldown_ms.items() if v <= now_ms]:
            self._cooldown_ms.pop(key, None)
            dropped += 1
        while len(self._buckets) > MAX_TRACKED_KEYS:
            oldest = min(self._buckets, key=lambda k: self._buckets[k].updated_ms)
            self._buckets.pop(oldest, None)
            dropped += 1
        return dropped

    def view(self, now_ms: int | None = None) -> dict:
        """通道视图里带的限流读数：只有计数与当前状态，没有任何会话内容"""
        now_ms = self._now(now_ms)
        active = {k: v for k, v in self._cooldown_ms.items() if v > now_ms}
        return {
            "perMinute": self.per_minute,
            "burst": self.burst,
            "maxWaitSeconds": MAX_WAIT_SECONDS,
            "counters": dict(self.counters),
            "blockedConversations": len(active),
            "cooldowns": sorted((round((v - now_ms) / 1000.0, 1) for v in active.values()))[:8],
            "trackedKeys": len(self._buckets),
            "lastReason": self.last_reason,
        }

    # ── 内部 ──────────────────────────────────────────────────────

    def _now(self, now_ms: int | None) -> int:
        return int(now_ms if now_ms is not None else time.time() * 1000)

    def _bucket(self, key: str, now_ms: int) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(per_minute=self.per_minute, burst=self.burst, now_ms=now_ms)
            self._buckets[key] = bucket
        return bucket


def retry_after_seconds(value: Any, default: float = DEFAULT_RETRY_AFTER_SECONDS) -> float:
    """把平台的 Retry-After 变成秒数：支持「120」与 HTTP-date 两种官方形状

    解析不出来才用兜底值，且一律封顶在 MAX_COOLDOWN_SECONDS——
    一个畸形的超大头不该让机器人哑一天，但「什么都不认」也不该让它立刻再撞一次。
    """
    if value is None or value == "":
        return min(MAX_COOLDOWN_SECONDS, float(default))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError:
            try:
                when = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return min(MAX_COOLDOWN_SECONDS, float(default))
            if when is None:
                return min(MAX_COOLDOWN_SECONDS, float(default))
            seconds = when.timestamp() - time.time()
    if seconds <= 0:
        return 0.0
    return min(MAX_COOLDOWN_SECONDS, seconds)


def is_rate_limited(*, status: int = 0, code: str = "", message: str = "") -> bool:
    """这算不算平台的限流信号（各家写法不同，宁可多认一类也别把它当普通失败重试）"""
    if int(status or 0) == 429:
        return True
    lowered = f"{code} {message}".lower()
    return any(h in lowered for h in ("rate limit", "ratelimit", "too many request", "frequency", "限流", "访问频率"))


async def bounded_send(
    send: Callable[[], Awaitable["str | None"]], seconds: float, *, label: str = "回贴",
    governor: "SendGovernor | None" = None,
) -> str:
    """给一次发送加硬超时，返回中文状态（'' = 文本与附件都发出去了）

    用 `asyncio.timeout` 而不是 `wait_for`：常驻连接一侧的约束是有界等待只走这一条路，
    且超时后任务取消要干净，不能留下挂在 socket 上的半截发送。

    带 `governor` 时超时会记在它的事务里一笔（`counters.timeouts`）：调用方拿到的只是
    一句中文说明，分不清「本轮放弃」和「通道自己说附件没发出去」，这两件事在界面上
    必须能分开数。
    """
    if seconds <= 0:
        raise RateLimitError("IM_RATE_LIMIT_INVALID", f"发送超时秒数不合法：{seconds}")
    try:
        async with asyncio.timeout(seconds):
            returned = await send()
    except asyncio.TimeoutError:
        if governor is not None:
            governor.mark_timeout()
        return f"{label}在 {seconds:g} 秒内没有被对端接收，本轮放弃（不是已送达）"
    except RateLimitError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise RateLimitError("IM_SEND_FAILED", f"{label}失败：{type(exc).__name__}：{str(exc)[:160]}") from exc
    # 通道自己报的中文说明（例如「附件没发出去，只发了文本」）必须原样交回调用方：
    # 丢掉它等于替用户宣布「都送达了」，而通道明明说没有。
    return str(returned or "")


__all__ = [
    "DEFAULT_RETRY_AFTER_SECONDS",
    "MAX_COOLDOWN_SECONDS",
    "MAX_TRACKED_KEYS",
    "MAX_WAIT_SECONDS",
    "Gate",
    "RateLimitError",
    "SendGovernor",
    "TokenBucket",
    "bounded_send",
    "is_rate_limited",
    "retry_after_seconds",
]
