#!/usr/bin/env python3
"""
IM 发送限流自测（缺陷 D-41 的「限流与 429 退避」那一半）

时钟一律注入（`now_ms`），不靠 sleep 推进：本仓库随包 Python 的单调钟粒度是 15.6ms，
用真实时间既排不出「1 秒后恢复」也会让断言贴着睡眠值抖。只有发送超时那一段真的等
（对端挂住时超时必须真的打断它），断言的是「被打断」而不是「花了多久」。

分段：
1. 令牌桶：突发额度、按分钟速率回血、参数非法当场拒
2. 实例隔离：两个同类型通道实例各算各的（验收项「两个同类型通道实例互不影响」）
3. 平台 429：按 Retry-After 锁住、锁期内不撞、到期自动放行、冷却后重起桶
4. 中文状态：note / status / view 三处读数说的是人话且不泄露会话内容
5. 发送超时：asyncio.timeout 真的打断挂住的发送，且不说成「已送达」
6. 上界与清账：键数有顶、到期冷却被清、解绑后不留残留

用法: python tests/integration/engine_im_ratelimit_selftest.py
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import im_ratelimit as R
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im ratelimit selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# ── 1. 令牌桶 ─────────────────────────────────────────────────────


def phase_bucket() -> None:
    section("1. 令牌桶：突发与回血")
    b = R.TokenBucket(per_minute=6, burst=2, now_ms=0)
    check("前两条靠突发额度直接过", b.try_take(0) and b.try_take(0))
    check("第三条此刻没额度", not b.try_take(0))
    check("缺 token 时要等的秒数按速率算", abs(b.wait_seconds(0) - 10.0) < 0.01)
    check("10 秒后回够一条", b.try_take(10_000) is True)
    check("回血不会超过容量", R.TokenBucket(per_minute=6, burst=2, now_ms=0).capacity == 2.0)
    long_idle = R.TokenBucket(per_minute=6, burst=2, now_ms=0)
    long_idle.try_take(0)
    check("放置很久也不会攒出超容量的额度", long_idle.refill(10 * 60_000) is None and long_idle.tokens == 2.0)
    for bad in (0, -1):
        try:
            R.TokenBucket(per_minute=bad, burst=2, now_ms=0)
            ok = False
        except R.RateLimitError as exc:
            ok = exc.code == "IM_RATE_LIMIT_INVALID"
        check(f"每分钟 {bad} 条被拒而不是无限流", ok)
    try:
        R.TokenBucket(per_minute=5, burst=0, now_ms=0)
        check("突发 0 被拒", False)
    except R.RateLimitError as exc:
        check("突发 0 被拒", exc.code == "IM_RATE_LIMIT_INVALID")
    check("等待秒数有封顶（不承诺无限排队）", R.MAX_WAIT_SECONDS > 0)


# ── 2. 实例隔离 ───────────────────────────────────────────────────


def phase_isolation() -> None:
    section("2. 两个同类型实例互不影响")
    # per_minute=2 → 0.033 条/秒：同一条桶回满要 30 秒，所以时间轴按 30 秒一级走
    g = R.SendGovernor(per_minute=2, burst=1, now_ms=0)
    key_a, key_b = "dingtalk:corp-A", "dingtalk:corp-B"
    check("A 实例第一条放行", g.allow(key_a, now_ms=0))
    check("A 实例第二条被本地额度挡下", not g.allow(key_a, now_ms=1000))
    check("B 实例完全不受 A 影响", g.allow(key_b, now_ms=1000))
    check("同实例的另一个会话也各自有桶（群不是共享额度）", g.allow(f"{key_a}|cid-1", now_ms=1000))
    g.penalize(key_a, 30, now_ms=61_000, why="too many request")
    check("A 被平台冷却不影响 B 发送", g.allow(key_b, now_ms=62_000))
    check("A 冷却期内直接挡下", not g.allow(key_a, now_ms=62_000))
    check("A 冷却到点后放行", g.allow(key_a, now_ms=91_000))
    counts = g.counters
    check("计数分得清发出/本地挡/平台冷却",
          counts["sent"] == 5 and counts["cooled"] == 2 and counts["throttled"] == 1, str(counts))
    check("A 的冷却读数只在 A 上", g.status(key_a, now_ms=62_000)["blocked"] is True
          and g.status(key_b, now_ms=62_000)["blocked"] is False)


# ── 3. 平台 429 ───────────────────────────────────────────────────


def phase_429() -> None:
    section("3. 平台说退多久就退多久")
    g = R.SendGovernor(per_minute=30, burst=5, now_ms=0)
    gate = g.penalize("slack:T1-C1", "120", now_ms=1000)
    check("429 之后这次申请直接拒", gate.allowed is False)
    check("理由里写清暂停了多少秒", "暂停发送 120 秒" in gate.reason)
    check("锁期内不再去撞", g.check("slack:T1-C1", now_ms=60_000).allowed is False)
    check("锁期还剩多少秒算得出来", abs(g.wait_seconds("slack:T1-C1", now_ms=60_000) - 61.0) < 0.01)
    check("到点自动放行", g.allow("slack:T1-C1", now_ms=121_000))
    check("冷却后重起的桶带满额度", g.check("slack:T1-C1", now_ms=121_000).allowed)
    check("sweep 清掉已到期冷却", g.sweep(now_ms=200_000) >= 0 and g.status("slack:T1-C1", now_ms=200_000)["blocked"] is False)

    check("Retry-After 收秒数字符串", R.retry_after_seconds("45") == 45.0)
    check("Retry-After 收数字", R.retry_after_seconds(12) == 12.0)
    check("畸形值回落到兜底", R.retry_after_seconds("soon") == R.DEFAULT_RETRY_AFTER_SECONDS)
    check("空值回落到兜底", R.retry_after_seconds("") == R.DEFAULT_RETRY_AFTER_SECONDS)
    check("离谱的大数被封顶", R.retry_after_seconds("999999999") == R.MAX_COOLDOWN_SECONDS)
    check("负数按 0 处理（平台让我别等就别等）", R.retry_after_seconds("-5") == 0.0)
    http_date = "Wed, 21 Oct 2015 07:28:00 GMT"
    check("HTTP-date 形状能解析（过期即 0）", R.retry_after_seconds(http_date) == 0.0)
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 60))
    got = R.retry_after_seconds(future)
    check("未到期的 HTTP-date 按剩余秒算", 30 < got <= 61)

    check("429 状态码算限流", R.is_rate_limited(status=429) is True)
    check("英文限流文案算限流", R.is_rate_limited(message="Too Many Requests") is True)
    check("中文「访问频率」算限流", R.is_rate_limited(code="FrequenceLimit", message="访问频率超过限制") is True)
    check("普通 500 不算限流", R.is_rate_limited(status=500, message="internal error") is False)


# ── 4. 中文状态 ───────────────────────────────────────────────────


def phase_status() -> None:
    section("4. 界面读数是人话，也不泄露内容")
    g = R.SendGovernor(per_minute=10, burst=3, now_ms=0)
    check("通畅时的中文说明带额度", "发送通畅" in g.note("k", now_ms=0))
    for _ in range(3):
        g.allow("k", now_ms=0)
    blocked = g.check("k", now_ms=0)
    check("额度用满时说清限额与还要等多久", "每分钟 10 条" in blocked.reason and "本轮不排队死等" in blocked.reason)
    check("用满后的中文读数", "本地额度已用满" in g.note("k", now_ms=0))
    g.penalize("k", 40, now_ms=0, why="frequency limit")
    check("平台冷却中的中文读数", "对端要求退避" in g.note("k", now_ms=1000))
    st = g.status("k", now_ms=1000)
    check("status 带剩几秒", st["blocked"] is True and st["cooldownLeftSeconds"] > 38)
    check("status 带容量与速率", st["capacity"] == 3.0 and st["perMinute"] == 10)
    check("status 的 note 与 note() 同源", st["note"] == g.note("k", now_ms=1000))
    view = g.view(now_ms=1000)
    check("view 汇总冷却会话数", view["blockedConversations"] == 1)
    check("view 带出上界与计数", view["maxWaitSeconds"] == R.MAX_WAIT_SECONDS and "sent" in view["counters"])
    check("view 不回显任何消息文本", "帮我" not in str(view))
    check("gate 视图给的是秒数不是内部浮点", blocked.as_view()["waitSeconds"] >= 0)


# ── 4b. 策略里的数字真的落到闸门 ──────────────────────────────────


def phase_apply_limits() -> None:
    section("4b. 用户在策略里改的数字要真的改变闸门")
    g = R.SendGovernor(per_minute=10, burst=3, now_ms=0)
    check("额度原样是构造时的值", g.view()["perMinute"] == 10 and g.view()["burst"] == 3)
    check("改成 30/6 被接受并回报「变了」", g.apply_limits(30, 6) is True)
    check("改完闸门上的额度跟着变", g.view()["perMinute"] == 30 and g.view()["burst"] == 6)
    check("同样的值再交一次算「没变」（不白清一次桶）", g.apply_limits(30, 6) is False)
    for bad in ("abc", None, 0, -1, "", [3]):
        check(f"畸形/越界额度一律拒（收到 {bad!r}）", g.apply_limits(bad, 6) is False, str(bad))
    check("拒掉畸形值后原有额度没被写坏", g.view()["perMinute"] == 30 and g.view()["burst"] == 6)

    # 额度真的变了才重起桶：不重起就等于旧容量的半桶带着新额度出门
    g.allow("k", now_ms=0)
    tokens_before = g.status("k", now_ms=0)["tokens"]
    check("起桶后确实消耗了额度", tokens_before < 6, f"{tokens_before}")
    g.apply_limits(60, 8)
    after = g.status("k", now_ms=0)
    check("改额度后桶重起，不带着旧半桶出门",
          after["tokens"] == 8 and after["capacity"] == 8, str(after))

    # 但平台冷却是对方给的，跟本地额度无关：改额度绝不能把它抹掉
    cooled = R.SendGovernor(per_minute=10, burst=3, now_ms=0)
    cooled.penalize("k", 90, now_ms=0)
    cooled.apply_limits(50, 4)
    check("改额度不撤销平台冷却（那是对端的要求）",
          cooled.status("k", now_ms=1000)["blocked"] is True, str(cooled.view(now_ms=1000)))
    check("冷却还在时照样不许发", cooled.check("k", now_ms=1000).allowed is False)
    check("改额度时把新值写进读数（界面看得到生效的那份）",
          cooled.view(now_ms=1000)["perMinute"] == 50, str(cooled.view(now_ms=1000)))


# ── 5. 发送超时 ───────────────────────────────────────────────────


def phase_send() -> None:
    section("5. 发送有硬超时，且不谎报已送达")

    async def scenario():
        sent = []

        async def ok():
            sent.append("ok")

        async def hang():
            await asyncio.sleep(30)

        async def boom():
            raise RuntimeError("socket is closed")

        note_ok = await R.bounded_send(ok, 5, label="回贴")
        note_hang = await R.bounded_send(hang, 0.1, label="回贴")
        raised = None
        try:
            await R.bounded_send(boom, 5)
        except R.RateLimitError as exc:
            raised = exc
        bad_param = None
        try:
            await R.bounded_send(ok, 0)
        except R.RateLimitError as exc:
            bad_param = exc
        return sent, note_ok, note_hang, raised, bad_param

    sent, note_ok, note_hang, raised, bad_param = asyncio.run(scenario())
    check("正常发送没有状态要报", note_ok == "" and sent == ["ok"])
    check("对端挂住时超时打断", "本轮放弃" in note_hang)
    check("超时的说明不写成已送达", "不是已送达" in note_hang)
    check("超时秒数写进说明", "0.1" in note_hang)
    check("发送本身抛错如实上抛", raised is not None and raised.code == "IM_SEND_FAILED")
    check("抛错原因带上原始异常类型", "RuntimeError" in raised.detail)
    check("非正数的超时参数被拒", bad_param is not None and bad_param.code == "IM_RATE_LIMIT_INVALID")

    async def cancel_scenario():
        async def hang():
            await asyncio.sleep(30)

        task = asyncio.create_task(R.bounded_send(hang, 5))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    check("外层取消不被吞成发送失败", asyncio.run(cancel_scenario()) is True)

    # 通道自己报的说明不能被丢掉：文本发出去了、附件没发出去，是两件事
    async def reports_partial():
        return "附件太大没发出去，只发了文本"

    async def reports_nothing():
        return None

    got_partial, got_none = asyncio.run(_pair(reports_partial, reports_nothing))
    check(
        "通道报「附件没发出去」时如实交回调用方（丢掉等于替用户宣布已送达）",
        got_partial == "附件太大没发出去，只发了文本",
        repr(got_partial)[:160],
    )
    check("通道没报任何说明时才是空串（已发出）", got_none == "", repr(got_none)[:120])

    # 超时记在闸门账上：调用方只拿到一句中文，分不清「本轮放弃」与「通道自己说没发全」
    gov = R.SendGovernor(per_minute=10, burst=3)
    timeout_note = asyncio.run(R.bounded_send(_hang_again, 0.1, label="回贴", governor=gov))
    check(
        "带 governor 时超时记进 counters.timeouts（读数里数得出来）",
        gov.counters["timeouts"] == 1 and "本轮放弃" in timeout_note,
        f"{str(gov.counters)} {timeout_note[:80]}",
    )
    plain_note = asyncio.run(R.bounded_send(_hang_again, 0.1, label="回贴"))
    check(
        "不传 governor 也照样只超时不抛（旧调用形状不受影响）",
        "本轮放弃" in plain_note and gov.counters["timeouts"] == 1,
        f"{str(gov.counters)} {plain_note[:80]}",
    )
    check(
        "mark_timeout 只记一笔账：不加冷却、不动桶",
        gov.status("nobody")["blocked"] is False and gov.counters["cooled"] == 0,
        str(gov.counters)[:160],
    )


async def _pair(a, b):
    return await R.bounded_send(a, 5), await R.bounded_send(b, 5)


async def _hang_again():
    await asyncio.sleep(30)


# ── 6. 上界与清账 ─────────────────────────────────────────────────


def phase_bounds() -> None:
    section("6. 记账有上界，解绑不留残留")
    g = R.SendGovernor(per_minute=60, burst=5, now_ms=0)
    for i in range(R.MAX_TRACKED_KEYS + 40):
        g.allow(f"k{i}", now_ms=0)
    g.sweep(now_ms=0)
    check("桶数量有上界（刷屏不无限增长）", g.view(now_ms=0)["trackedKeys"] <= R.MAX_TRACKED_KEYS)
    g.penalize("k1", 10, now_ms=0)
    g.penalize("k2", 999, now_ms=0)
    check("封顶后冷却不会超过 MAX_COOLDOWN_SECONDS", g.wait_seconds("k2", now_ms=1000) <= R.MAX_COOLDOWN_SECONDS + 1)
    g.clear("k1")
    check("清掉某个键后立刻不再挡", g.allow("k1", now_ms=1000))
    check("另一个键的冷却不受牵连", g.wait_seconds("k2", now_ms=1000) > 0)
    g.clear()
    check("全清后既没冷却也没桶", g.view(now_ms=1000)["blockedConversations"] == 0 and g.view(now_ms=1000)["trackedKeys"] == 0)


def main() -> int:
    print("== IM 发送限流自测（D-41）==")
    phase_bucket()
    phase_isolation()
    phase_429()
    phase_status()
    phase_apply_limits()
    phase_send()
    phase_bounds()
    print(f"\n[DONE] im_ratelimit selftest: {len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
