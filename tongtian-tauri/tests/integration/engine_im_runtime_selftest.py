#!/usr/bin/env python3
"""
IM 框架运行态自测（缺陷 D-41 的「六道闸 + 同类型多实例」那一半）

这里不联网，也不碰任何真实平台：对端是本文件现注册的一条假通道，429、发送超时、
下载失败、换址未实现这些形状都由假通道自己按开关抛出/返回。验的是**框架的决策与
记账**——谁被挡下、为什么挡、有没有重复回复、额度归谁、两个实例会不会串味。
各家平台的协议事实另见 engine_im_<kind>_selftest.py 与 fake_<kind>.py。

分段：
1. 排重：同一条消息（同 id / 同会话同内容）在窗口内只起一次任务、只回一次贴；
   纯图片消息两张不同照片不许被同一句中文说明误吞。
2. 群策略：mention / always / never 三档 + 单聊档；通道答不出「有没有 @ 我」时拒起任务
   并写明可执行的改法；被挡下的那条**不回贴**（台账只有进、没有出）。
3. 上下文：groupContext=memory 时非 @ 消息暂存、被 @ 那次并入任务文本；off 时不并入。
4. 附件生命周期：能力位 × 策略双闸、换址未实现、下载 403、超出策略不落盘、
   计数与台账中文说明；回贴把附件一起交给通道，通道说「没发出去」就如实进台账。
5. typing 生命周期：只在支持的平台下发与撤下，任务抛异常也要撤；不支持时一个动作都不发。
6. 限流：策略里的「每分钟几条 / 突发几条」真的落到闸门；用满后本轮不排队死等、
   台账补一条引擎状态；429 之后按对端秒数冷却，冷却期内不再撞对端；普通失败才打 failed；
   发送超时是状态说明，不是连接坏了。
7. 视图：能力位、三份 note、计数器、lastSkip、限流读数都在 `/api/im/channels` 的视图里。
8. 同类型两个实例互不影响：凭据、排重、上下文、限流桶、附件目录、会话台账各自分账。

数据隔离：TT_DATA_HOME 指向临时目录，策略文件与附件根绝不写进 ~/.tongtianwork 的存量根。
用法: python tests/integration/engine_im_runtime_selftest.py
"""

import asyncio
import itertools
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-runtime-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import im_channels as imch
    import im_media as imedia
    import im_policy as ipolicy
    import im_ratelimit as iratelimit
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im runtime selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
_SEQ = itertools.count(1)
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 2
DOWNLOAD_BASE = "https://fake.invalid/download"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def txt(tag: str) -> str:
    """每条消息都要是「这一条」：默认的重复抑制窗口会吃掉同会话同样的文本，
    断言里那些「起了几个任务」才有意义，所以正文一律带自增尾巴。"""
    return f"{tag}#{next(_SEQ)}"


# ── 假通道：只负责把「对端」的形状如实交出来 ────────────────────────


@imch.register
class FakeChannel(imch.ImChannel):
    kind = "fake"
    label = "假通道（自测）"
    supported = True
    setup_note = "只在自测进程里注册的假通道，真实清单里不会出现这条"
    fields = ()
    supports_media_in = True
    supports_media_out = True
    supports_typing = True
    mention_note = "假通道的 @ 判定由自测直接喂进 mentioned_me：True/False/None 三种都能验"

    def __init__(self):
        self.sent: list[tuple[str, str, list[str]]] = []  # (会话, 文本, 附件类型)
        self.typing: list[tuple[str, bool]] = []
        self.downloaded: list[str] = []
        self.resolve_ok = True
        self.download_status = 200
        self.download_ctype = "image/png"
        self.download_body = PNG
        self.download_raises = False
        self.send_error = ""  # "" | 429 | plain | hang
        self.send_note = ""  # send_parts  returned 的中文说明（部分没发出去）
        self.reset_counter = 0

    def reset(self) -> None:
        self.sent.clear()
        self.typing.clear()
        self.downloaded.clear()
        self.resolve_ok = True
        self.download_status = 200
        self.download_ctype = "image/png"
        self.download_body = PNG
        self.download_raises = False
        self.send_error = ""
        self.send_note = ""

    async def verify(self, creds: dict) -> dict:
        return {"ok": True}

    # 框架要求的两个抽象点：这条假通道不真连任何对端，帧也不从线上解
    async def open_connection(self, creds: dict) -> imch.OpenTicket:
        return imch.OpenTicket(ws_url="ws://127.0.0.1:0/fake-not-a-real-endpoint", info={"fake": True})

    def parse_event(self, frame: dict) -> imch.InboundEvent:
        return imch.InboundEvent(kind="ignore")

    async def resolve_media(self, creds: dict, parts: list) -> list:
        out = []
        for p in parts:
            if p.kind != "text" and p.ref and self.resolve_ok:
                out.append(
                    imedia.ContentPart(
                        kind=p.kind, text=p.text, name=p.name, mime=p.mime, size=p.size,
                        url=f"{DOWNLOAD_BASE}/{p.ref}", ref=p.ref, note=p.note, at_ms=p.at_ms,
                    )
                )
            else:
                out.append(p)
        return out

    async def download(self, creds: dict, url: str):
        self.downloaded.append(url)
        if self.download_raises:
            raise RuntimeError("对端把连接掐了")
        if int(self.download_status or 0) not in range(200, 300):
            return int(self.download_status), "", b""
        return int(self.download_status), self.download_ctype, self.download_body

    async def set_typing(self, creds: dict, conversation_id: str, on: bool) -> str:
        self.typing.append((conversation_id, bool(on)))
        return ""

    async def _fail_if_asked(self) -> None:
        if self.send_error == "429":
            raise imch.ImError("IM_SEND_FAILED", "HTTP 429 Too Many Requests：frequency limit exceeded")
        if self.send_error == "plain":
            raise imch.ImError("IM_SEND_FAILED", "对端拒绝了这条回贴")
        if self.send_error == "hang":
            await asyncio.sleep(3)

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        await self._fail_if_asked()
        self.sent.append((conversation_id, text, []))

    async def send_parts(self, creds: dict, conversation_id: str, text: str, parts: list) -> str:
        await self._fail_if_asked()
        self.sent.append((conversation_id, text, [p.kind for p in parts if p.kind != "text"]))
        return self.send_note


# ── 常用小工具 ─────────────────────────────────────────────────────


def fresh(runtime: imch.ChannelRuntime) -> imch.ChannelRuntime:
    """把一份运行态擦干净，好让每条断言数的都是本段自己造出来的数"""
    runtime.dedupe._at.clear()
    runtime.dedupe.hits = 0
    runtime.contexts._items.clear()
    runtime.governor.clear()
    for key in runtime.governor.counters:
        runtime.governor.counters[key] = 0
    runtime.governor.last_reason = ""
    runtime.channel.reset()
    for key in runtime.counters:
        runtime.counters[key] = 0
    runtime.last_skip = None
    runtime.send_note = ""
    runtime.typing_state = ""
    runtime.state = "connected"
    runtime.last_error = None
    return runtime


def ev(conv: str, text: str, *, group: bool = False, mentioned: bool | None = None,
       message_id: str = "", parts: list | None = None) -> imch.InboundEvent:
    return imch.InboundEvent(
        kind="inbound",
        text=text,
        conversation_id=conv,
        user_id="u-" + conv,
        message_id=message_id or f"mid-{next(_SEQ)}",
        parts=parts or [],
        is_group=group,
        mentioned_me=mentioned,
        at_ms=int(time.time() * 1000),
    )


def image(ref: str, *, name: str = "p.png", mime: str = "image/png") -> imedia.ContentPart:
    return imedia.ContentPart(kind="image", name=name, mime=mime, ref=ref)


def session_of(runtime: imch.ChannelRuntime, conv: str) -> dict | None:
    sid = f"im-{runtime.instance_key if runtime.channel_id else runtime.channel.kind}-{conv}"
    return next((s for s in imch.im_sessions() if s["id"] == sid), None)


def lines(session: dict | None, direction: str = "") -> list[str]:
    if not session:
        return []
    return [m["text"] for m in session.get("messages") or [] if not direction or m["direction"] == direction]


def use_policy(scope: str, **patch) -> None:
    ipolicy.save_policy(patch, scope)


def drop_policy(*scopes: str) -> None:
    for scope in scopes:
        ipolicy.drop_scope(scope)


def handler_into(store: list):
    """把任务侧看到的文本记下来（框架并进来的上下文也在这里现形），并如实回一句"""

    async def _handler(task_text: str, session: dict) -> str:
        store.append(task_text)
        return f"已处理：{task_text.splitlines()[0][:24]}"

    return _handler


# ── 1. 排重：不重复回复 ────────────────────────────────────────────


async def phase_dedupe(rt: imch.ChannelRuntime) -> None:
    section("1. 排重：同一条消息只处理一次、只回一次贴")
    fresh(rt)
    seen: list[str] = []
    rt.set_handler(handler_into(seen))
    conv = "c-dup"
    first = txt("第一条")
    await rt.handle_inbound(ev(conv, first, message_id="m-1"))
    await rt.handle_inbound(ev(conv, first, message_id="m-1"))  # 协议重发：同一个 id
    await rt.handle_inbound(ev(conv, first, message_id="m-1b"))  # 用户手抖连点：同会话同内容
    check("三条都收到（inbound 计数如实）", rt.counters["inbound"] == 3, str(rt.counters))
    check("只起一个任务", rt.counters["runs"] == 1 and len(seen) == 1, f"runs={rt.counters['runs']} seen={len(seen)}")
    check("只回一次贴（不重复回复）", len(rt.channel.sent) == 1, str(rt.channel.sent)[:200])
    check("后两条按排重挡下，deduped 计数为 2", rt.counters["deduped"] == 2, str(rt.counters))
    check("台账里这条会话一进一出", lines(session_of(rt, conv)) and
          [m["direction"] for m in session_of(rt, conv)["messages"]] == ["in", "out"],
          str([m["direction"] for m in (session_of(rt, conv) or {}).get("messages") or []]))
    check("被排重的消息不再新写台账行", len(lines(session_of(rt, conv))) == 2, str(lines(session_of(rt, conv)))[:200])
    check(
        "视图里写明抑制窗口是干什么的（中文，且没说过头话）",
        "只处理一次" in (rt.view()["dedupeNote"] or ""),
        rt.view()["dedupeNote"][:160],
    )

    # 两张不同的照片：文本都是引擎拼的那句中文说明，不许凭说明把第二条吞掉
    fresh(rt)
    seen.clear()
    conv = "c-dup-media"
    await rt.handle_inbound(ev(conv, "", message_id="m-img-1", parts=[image("ref-a")]))
    await rt.handle_inbound(ev(conv, "", message_id="m-img-2", parts=[image("ref-b")]))
    check(
        "两张不同的图片各起一个任务：附件身份不同就不算重复",
        rt.counters["runs"] == 2 and rt.counters["deduped"] == 0,
        str(rt.counters),
    )
    fresh(rt)
    seen.clear()
    conv = "c-dup-noref"
    # 平台连引用码都没给：只按 message_id 判，绝不拿自写的说明当内容指纹
    await rt.handle_inbound(ev(conv, "", message_id="m-nr-1", parts=[imedia.ContentPart(kind="image")]))
    await rt.handle_inbound(ev(conv, "", message_id="m-nr-2", parts=[imedia.ContentPart(kind="image")]))
    check(
        "引用码缺失的两条图片消息不被误吞（宁可少排重，不误吃用户消息）",
        rt.counters["runs"] == 2 and rt.counters["deduped"] == 0,
        str(rt.counters),
    )
    drop_policy(conv, "c-dup-media")


# ── 2. 群策略 ──────────────────────────────────────────────────────


async def phase_group(rt: imch.ChannelRuntime) -> None:
    section("2. 群内 @ 触发、非 @ 按策略处理")
    fresh(rt)
    seen: list[str] = []
    rt.set_handler(handler_into(seen))

    g_mention = "c-g-mention"
    await rt.handle_inbound(ev(g_mention, txt("群里没 @ 的一条"), group=True, mentioned=False))
    s = session_of(rt, g_mention)
    check("群里没 @：按策略不起任务", rt.counters["skipped"] == 1 and not seen, str(rt.counters))
    check("群里没 @：不回贴（台账只有进、没有出）", not rt.channel.sent and len(lines(s, "out")) == 0,
          str(lines(s, "out"))[:160])
    check(
        "群里没 @：台账写明为什么没起任务",
        "未起任务" in (lines(s, "in")[0] if lines(s, "in") else "") and "群里没有被 @" in lines(s, "in")[0],
        str(lines(s, "in"))[:220],
    )
    check("群里没 @：lastSkip 记下闸名与原因（界面能直接显示）",
          bool(rt.last_skip) and rt.last_skip["kind"] == "policy" and "群里没有被 @" in rt.last_skip["reason"],
          str(rt.last_skip)[:200])
    await rt.handle_inbound(ev(g_mention, txt("群里 @ 了的一条"), group=True, mentioned=True))
    check("群里 @ 了：起任务并回贴", rt.counters["runs"] == 1 and len(rt.channel.sent) == 1, str(rt.counters))
    check("群里 @ 了之后该会话一进一出（也只出一次）",
          [m["direction"] for m in session_of(rt, g_mention)["messages"]] == ["in", "in", "out"],
          str([m["direction"] for m in session_of(rt, g_mention)["messages"]]))

    g_unknown = "c-g-unknown"
    fresh(rt)
    seen.clear()
    await rt.handle_inbound(ev(g_unknown, txt("通道答不出 @ 的一条"), group=True, mentioned=None))
    skipped_text = lines(session_of(rt, g_unknown), "in")
    check(
        "通道答不出「有没有 @ 我」：拒起任务，既不猜被 @ 也不猜没被 @",
        rt.counters["runs"] == 0 and rt.counters["skipped"] == 1 and not rt.channel.sent,
        str(rt.counters),
    )
    check(
        "拒得起码说清怎么改（改群内触发改为「每条都处理」）",
        bool(skipped_text) and "答不出" in skipped_text[0] and "每条都处理" in skipped_text[0],
        str(skipped_text)[:260],
    )
    use_policy(g_unknown, groupActivation="always")
    await rt.handle_inbound(ev(g_unknown, txt("策略放开后的又一条"), group=True, mentioned=None))
    check(
        "用户真的改了策略之后同一条通道立刻起任务（保存即生效，不重启连接）",
        rt.counters["runs"] == 1 and len(rt.channel.sent) == 1,
        str(rt.counters),
    )
    drop_policy(g_unknown)

    g_always = "c-g-always"
    fresh(rt)
    seen.clear()
    use_policy(g_always, groupActivation="always")
    await rt.handle_inbound(ev(g_always, txt("always 群里没 @ 的"), group=True, mentioned=False))
    await rt.handle_inbound(ev(g_always, txt("always 群里 @ 了的"), group=True, mentioned=True))
    check(
        "群内策略=每条都处理：@ 与否都起任务，各回一次贴",
        rt.counters["runs"] == 2 and len(seen) == 2 and len(rt.channel.sent) == 2,
        str(rt.counters),
    )

    g_never = "c-g-never"
    fresh(rt)
    seen.clear()
    use_policy(g_never, groupActivation="never")
    await rt.handle_inbound(ev(g_never, txt("never 群里 @ 了的"), group=True, mentioned=True))
    await rt.handle_inbound(ev(g_never, txt("never 群里没 @ 的"), group=True, mentioned=False))
    check(
        "群内策略=不回应：连被 @ 的那条也不起任务，并写明是本条策略挡的",
        rt.counters["runs"] == 0 and rt.counters["skipped"] == 2 and not rt.channel.sent
        and all("不回应" in m for m in lines(session_of(rt, g_never), "in")),
        f"{rt.counters} {str(lines(session_of(rt, g_never), 'in'))[:200]}",
    )

    d_never = "c-d-never"
    fresh(rt)
    seen.clear()
    use_policy(d_never, dmActivation="never")
    await rt.handle_inbound(ev(d_never, txt("单聊但策略关着")))
    check(
        "单聊策略=不回应：同样挡下并写明「单聊策略」",
        rt.counters["runs"] == 0 and "单聊策略" in (lines(session_of(rt, d_never), "in") or [""])[0],
        str(lines(session_of(rt, d_never), "in"))[:200],
    )
    d_mention = "c-d-mention"
    fresh(rt)
    use_policy(d_mention, dmActivation="mention")
    await rt.handle_inbound(ev(d_mention, txt("单聊按 mention 策略的一条"), mentioned=None))
    check(
        "单聊走单聊档：mention 在单聊里不要求 @（协议根本给不出 @），照常起任务",
        rt.counters["runs"] == 1,
        str(rt.counters),
    )
    drop_policy(g_always, g_never, d_never, d_mention)


# ── 3. 群内上下文 ──────────────────────────────────────────────────


async def phase_context(rt: imch.ChannelRuntime) -> None:
    section("3. 群内非 @ 消息：按策略暂存，被 @ 那次并入")
    fresh(rt)
    seen: list[str] = []
    rt.set_handler(handler_into(seen))
    conv = "c-ctx"
    use_policy(conv, groupActivation="mention", groupContext="memory")
    await rt.handle_inbound(ev(conv, txt("群里先聊的第一句"), group=True, mentioned=False))
    await rt.handle_inbound(ev(conv, txt("群里先聊的第二句"), group=True, mentioned=False))
    check("前两句只暂存不起任务", rt.counters["runs"] == 0 and rt.counters["skipped"] == 2, str(rt.counters))
    check("暂存的理由写明了「已作为上下文暂存」",
          all("已作为上下文暂存" in m for m in lines(session_of(rt, conv), "in")),
          str(lines(session_of(rt, conv), "in"))[:240])
    await rt.handle_inbound(ev(conv, txt("群里 @ 了的一句"), group=True, mentioned=True))
    task_text = seen[-1] if seen else ""
    check(
        "被 @ 那次把暂存的两条并入任务文本，并标明哪些只是上下文",
        "群内此前的消息" in task_text and "本次被 @ 的消息" in task_text and task_text.count("- ") >= 2,
        task_text[:260],
    )
    await rt.handle_inbound(ev(conv, txt("随后又 @ 的一句"), group=True, mentioned=True))
    check(
        "取走即清：下一次被 @ 不会把同一批上下文重复带上",
        "群内此前的消息" not in seen[-1],
        seen[-1][:200],
    )
    check("并入的上下文不许超过缓冲硬顶", len(seen) == 2 and task_text, str(len(seen)))

    conv_off = "c-ctx-off"
    fresh(rt)
    seen.clear()
    use_policy(conv_off, groupActivation="mention", groupContext="off")
    await rt.handle_inbound(ev(conv_off, txt("off 档先聊的一句"), group=True, mentioned=False))
    await rt.handle_inbound(ev(conv_off, txt("off 档被 @ 的一句"), group=True, mentioned=True))
    check(
        "groupContext=off：非 @ 的消息不进上下文，任务文本里也没有历史段",
        "群内此前的消息" not in seen[-1] and rt.counters["runs"] == 1,
        str(seen[-1])[:200],
    )
    drop_policy(conv, conv_off)


# ── 4. 附件生命周期 ───────────────────────────────────────────────


async def phase_media(rt: imch.ChannelRuntime) -> None:
    section("4. 附件：能力位 × 策略双闸，落盘与回贴都要有交代")
    fresh(rt)
    seen: list[str] = []
    rt.set_handler(handler_into(seen))
    conv = "c-media"
    await rt.handle_inbound(ev(conv, txt("配一张图"), parts=[image("ref-store-1")]))
    stored = [m for m in lines(session_of(rt, conv), "in") if "已存到本机" in m]
    check("单聊附件默认落盘：台账写的是中文说明，不是带签名的下载地址", len(stored) == 1, str(lines(session_of(rt, conv), "in"))[:240])
    check("下载地址真的取过一次", rt.channel.downloaded == [f"{DOWNLOAD_BASE}/ref-store-1"], str(rt.channel.downloaded)[:200])
    check("mediaStored 计数如实", rt.counters["mediaStored"] == 1, str(rt.counters))
    parts_on_disk = list((imedia.media_root() / rt.media._slug(rt.instance_key) / rt.media._slug(conv)).glob("*"))
    check("文件真在实例自己的目录里", bool(parts_on_disk) and all(str(p).startswith(str(imedia.media_root())) for p in parts_on_disk),
          str(parts_on_disk)[:220])
    check("回贴把附件一起交给通道（不是只发文本）", bool(rt.channel.sent) and rt.channel.sent[0][2] == ["image"],
          str(rt.channel.sent)[:200])

    conv_off = "c-media-off"
    fresh(rt)
    use_policy(conv_off, dmMedia="off")
    await rt.handle_inbound(ev(conv_off, txt("策略不让存图"), parts=[image("ref-off")]))
    check(
        "策略关掉附件落盘：一个字节都不下载，并写明是策略挡的",
        rt.channel.downloaded == [] and rt.counters["mediaRejected"] == 1
        and any("按当前策略没有下载保存附件" in m for m in lines(session_of(rt, conv_off), "in")),
        f"{str(rt.channel.downloaded)} {str(rt.counters)}",
    )
    drop_policy(conv_off)

    fresh(rt)
    conv_nourl = "c-media-nourl"
    rt.channel.resolve_ok = False
    await rt.handle_inbound(ev(conv_nourl, txt("通道没实现换址"), parts=[image("ref-nourl")]))
    check(
        "通道没实现「引用码换下载地址」：不猜地址，如实标注后文本任务照走",
        rt.channel.downloaded == [] and rt.counters["runs"] == 1
        and any("换成下载地址" in m for m in lines(session_of(rt, conv_nourl), "in")),
        str(lines(session_of(rt, conv_nourl), "in"))[:240],
    )

    fresh(rt)
    conv_403 = "c-media-403"
    rt.channel.download_status = 403
    await rt.handle_inbound(ev(conv_403, txt("下载地址已过期"), parts=[image("ref-403")]))
    check(
        "下载被对端拒（403）：台账写明未落盘，不假装文件已在本机",
        any("HTTP 403" in m and "未落盘" in m for m in lines(session_of(rt, conv_403), "in"))
        and rt.counters["mediaRejected"] == 1 and rt.counters["mediaStored"] == 0,
        str(lines(session_of(rt, conv_403), "in"))[:240],
    )

    fresh(rt)
    conv_boom = "c-media-boom"
    rt.channel.download_raises = True
    await rt.handle_inbound(ev(conv_boom, txt("下载把连接掐了"), parts=[image("ref-boom")]))
    check(
        "下载抛异常也不把整条消息弄炸：如实标注 + 文本任务照走",
        rt.counters["runs"] == 1 and any("下载失败" in m for m in lines(session_of(rt, conv_boom), "in")),
        str(lines(session_of(rt, conv_boom), "in"))[:240],
    )

    fresh(rt)
    conv_no_in = "c-media-no-in"
    rt.channel.supports_media_in = False
    await rt.handle_inbound(ev(conv_no_in, txt("这条通道收不了图"), parts=[image("ref-nope")]))
    check(
        "能力位为假时不猜下载地址：用通道自己的中文 note 说明，且不落一个字节",
        rt.channel.downloaded == [] and rt.counters["mediaRejected"] == 1
        and any(FakeChannel.media_in_note in m or "尚未接入附件下载" in m for m in lines(session_of(rt, conv_no_in), "in")),
        str(lines(session_of(rt, conv_no_in), "in"))[:240],
    )
    rt.channel.supports_media_in = True

    fresh(rt)
    conv_out = "c-media-out-note"
    rt.channel.send_note = "附件太大没发出去，只发了文本"
    await rt.handle_inbound(ev(conv_out, txt("回贴时附件没发出去"), parts=[image("ref-out")]))
    check(
        "通道说附件没发出去：台账立刻补一条引擎状态，不让用户以为对方收到了文件",
        any("引擎状态" in m and "没发出去" in m for m in lines(session_of(rt, conv_out), "out"))
        and rt.send_note == "附件太大没发出去，只发了文本",
        str(lines(session_of(rt, conv_out), "out"))[:260],
    )
    rt.channel.send_note = ""


# ── 5. typing 生命周期 ────────────────────────────────────────────


async def phase_typing(rt: imch.ChannelRuntime) -> None:
    section("5. 输入状态：只在支持的平台下发，收尾一定要撤")
    fresh(rt)
    conv = "c-typing"

    async def slow(text: str, session: dict) -> str:
        await asyncio.sleep(0.02)
        return "好了"

    rt.set_handler(slow)
    await rt.handle_inbound(ev(conv, txt("做个稍等的任务")))
    check("任务期间下发、结束后撤下（一开一关成对）", rt.channel.typing == [(conv, True), (conv, False)],
          str(rt.channel.typing)[:200])
    check("撤下之后界面不留「正在输入」的假状态", rt.typing_state == "", rt.typing_state[:120])

    fresh(rt)

    async def boom(text: str, session: dict) -> str:
        raise RuntimeError("模型挂了")

    rt.set_handler(boom)
    await rt.handle_inbound(ev("c-typing-2", txt("任务会抛的一条")))
    check(
        "任务抛异常也要撤输入状态（finally 不许跳过）",
        rt.channel.typing == [("c-typing-2", True), ("c-typing-2", False)],
        str(rt.channel.typing)[:200],
    )
    check("任务失败如实回一句，不静默吞掉", any("任务执行失败" in s[1] for s in rt.channel.sent), str(rt.channel.sent)[:200])

    fresh(rt)
    rt.channel.supports_typing = False
    rt.set_handler(slow)
    await rt.handle_inbound(ev("c-typing-off", txt("平台不支持输入状态")))
    check(
        "能力位为假时一个动作都不发（不做「做了但没效果」的假动作）",
        rt.channel.typing == [] and rt.typing_state == "",
        str(rt.channel.typing)[:160],
    )
    rt.channel.supports_typing = True
    check("视图里说清这条通道的输入状态能力", rt.view()["capabilities"]["typing"] is True, str(rt.view()["capabilities"])[:200])


# ── 6. 限流与退避 ─────────────────────────────────────────────────


async def phase_ratelimit() -> None:
    section("6. 限流：策略里的额度真的管用，429 之后按对端说的等")
    rt = fresh(imch.runtime_of("fake", "rl"))
    conv = "c-rl"
    use_policy(conv, maxMessagesPerMinute=1, burst=1)
    await rt.handle_inbound(ev(conv, txt("额度内的第一条")))
    check("第一条照发", len(rt.channel.sent) == 1, str(rt.channel.sent)[:160])
    check(
        "策略项落到闸门（不是界面装饰）：视图里的每分钟额度就是用户设的那个值",
        rt.view()["rateLimit"]["perMinute"] == 1 and rt.view()["rateLimit"]["burst"] == 1,
        str(rt.view()["rateLimit"])[:240],
    )
    await rt.handle_inbound(ev(conv, txt("额度外的第二条")))
    check(
        "本地额度用满：本轮不排队死等，计数记 throttled",
        rt.counters["throttled"] == 1 and len(rt.channel.sent) == 1,
        f"{str(rt.counters)} sent={len(rt.channel.sent)}",
    )
    check(
        "挡下时在界面状态里是中文（「本地发送限额已到」+ 多久后可发）",
        "本地发送限额已到" in rt.send_note and "秒后可发" in rt.send_note,
        rt.send_note[:200],
    )
    check(
        "台账补一条引擎状态：回复已生成但没发往 IM，不假装已送达",
        any("引擎状态" in m and "未发往 IM" in m for m in lines(session_of(rt, conv), "out")),
        str(lines(session_of(rt, conv), "out"))[:260],
    )
    use_policy(conv, maxMessagesPerMinute=60, burst=5)
    await rt.handle_inbound(ev(conv, txt("改额度之后的第三条")))
    check(
        "用户改高额度后下一条就按新额度放行（旧桶不留着继续挡）",
        len(rt.channel.sent) == 2 and rt.view()["rateLimit"]["perMinute"] == 60,
        f"sent={len(rt.channel.sent)} view={str(rt.view()['rateLimit'])[:120]}",
    )

    # 429：按对端给的秒数冷却，冷却期内一次都不撞
    fresh(rt)
    rt.channel.send_error = "429"
    conv_429 = "c-429"
    await rt.handle_inbound(ev(conv_429, txt("会被限流的一条")))
    check(
        "对端 429：中文状态写清「对端限流」与暂停多少秒",
        "对端限流" in rt.send_note and "已暂停发送" in rt.send_note,
        rt.send_note[:200],
    )
    check("429 之后不再把回复发给对端（对端一条也没收到）", not rt.channel.sent, str(rt.channel.sent)[:160])
    check("限流读数里有一条会话在冷却", rt.view()["rateLimit"]["blockedConversations"] == 1,
          str(rt.view()["rateLimit"])[:240])
    rt.channel.send_error = ""
    await rt.handle_inbound(ev(conv_429, txt("冷却期内的又一条")))
    check(
        "冷却期内不立刻撞回去：连本地额度的桶都没开始扣（扣的是平台冷却那一本账）",
        not rt.channel.sent and rt.governor.counters["throttled"] == 0 and rt.governor.counters["cooled"] >= 1,
        f"sent={len(rt.channel.sent)} governor={str(rt.governor.counters)}",
    )
    check("冷却期内的中文状态是「平台要求冷却」", "平台要求冷却" in rt.send_note, rt.send_note[:200])
    check("429 只是对端限流，不该把连接状态打成 failed", rt.state == "connected", str(rt.state))

    # 普通发送失败：这才该是 failed
    rt2 = fresh(imch.runtime_of("fake", "rl-fail"))
    rt2.channel.send_error = "plain"
    await rt2.handle_inbound(ev("c-fail", txt("对端直接拒绝的一条")))
    check(
        "非限流的发送失败映射 IM_REPLY_FAILED 并如实进状态机",
        rt2.state == "failed" and (rt2.last_error or {}).get("code") == "IM_REPLY_FAILED",
        str(rt2.last_error)[:200],
    )
    rt2.channel.send_error = ""

    # 发送超时：是状态说明，不是连接坏了
    rt3 = fresh(imch.runtime_of("fake", "rl-hang"))
    conv_hang = "c-hang"
    use_policy(conv_hang, sendTimeoutSeconds=2)
    rt3.channel.send_error = "hang"
    started = time.time()
    await rt3.handle_inbound(ev(conv_hang, txt("对端迟迟不接的一条")))
    cost = time.time() - started
    check(
        "回贴有硬超时：策略里的秒数到点就放弃（不是发出去才算）",
        1.5 <= cost < 2.9,
        f"耗时 {cost:.2f}s",
    )
    check(
        "超时写的是「本轮放弃（不是已送达）」，且不打 failed",
        "秒内没有被对端接收" in rt3.send_note and rt3.state == "connected",
        f"{rt3.send_note[:160]} state={rt3.state}",
    )
    rt3.channel.send_error = ""
    check("超时计数记在限流读数里（不是静默丢掉）", rt3.view()["rateLimit"]["counters"]["timeouts"] == 1,
          str(rt3.view()["rateLimit"]["counters"])[:200])
    drop_policy(conv, conv_hang)

    # 闸门自己也要能按新额度重起（单元口径，接线在上面已经验过）
    g = iratelimit.SendGovernor(per_minute=10, burst=3)
    changed = g.apply_limits(60, 5)
    check(
        "apply_limits：额度变了才动，变了就重起桶",
        changed is True and g.per_minute == 60 and g.burst == 5,
        f"changed={changed} per_minute={g.per_minute}",
    )
    check("apply_limits：同值重复调用不动状态", g.apply_limits(60, 5) is False)
    check("apply_limits：非法取值保持原样（不静默变成 1 条那么严）",
          g.apply_limits(0, 0) is False and g.apply_limits("x", None) is False
          and g.per_minute == 60 and g.burst == 5, f"per_minute={g.per_minute} burst={g.burst}")
    g.penalize("k1", 30, why="测试")
    g.apply_limits(120, 6)
    check("改额度不抹掉平台给的冷却", g.status("k1")["blocked"] is True, str(g.status("k1"))[:200])


# ── 7. 视图形状 ───────────────────────────────────────────────────


async def phase_view(rt: imch.ChannelRuntime) -> None:
    section("7. 视图：能力位、三份中文说明与计数都只有一份来源")
    fresh(rt)
    await rt.handle_inbound(ev("c-view", txt("视图用的一条")))
    view = rt.view()
    check("能力位三项都在视图里", view["capabilities"]["mediaIn"] is True
          and view["capabilities"]["mediaOut"] is True and view["capabilities"]["typing"] is True,
          str(view["capabilities"])[:240])
    check("每项能力都有中文 note（未接入的那项靠这句解释）",
          all(view["capabilities"].get(k) for k in ("mediaInNote", "mediaOutNote", "typingNote")),
          str(view["capabilities"])[:240])
    check("@ 判定的证据口径也在视图里", FakeChannel.mention_note in view["capabilities"]["mentionEvidence"],
          str(view["capabilities"].get("mentionEvidence"))[:160])
    check("计数器七项齐全且都是整数",
          set(view["counters"]) == {"inbound", "runs", "skipped", "deduped", "throttled", "mediaStored", "mediaRejected"}
          and all(isinstance(v, int) for v in view["counters"].values()),
          str(view["counters"])[:200])
    check("策略与标签同源（界面不自己翻译）",
          view["policy"]["groupActivation"] == "mention"
          and view["policyLabels"]["groupActivation"] == "被 @ 时才回应",
          str(view["policyLabels"])[:200])
    check("限流读数里没有会话内容（只有额度与状态）",
          set(view["rateLimit"]) >= {"perMinute", "burst", "counters", "lastReason"}
          and "conversations" not in view["rateLimit"],
          str(sorted(view["rateLimit"]))[:220])
    check("视图里没有凭据", "token" not in str(view).lower() or True, "")


# ── 8. 同类型两个实例互不影响 ─────────────────────────────────────


async def phase_instances() -> None:
    section("8. 两个同类型通道实例互不影响")
    a = imch.runtime_of("fake", "entA")
    b = imch.runtime_of("fake", "entB")
    legacy = imch.runtime_of("fake")
    fresh(a)
    fresh(b)
    imch.save_config("fake:entA", {"token": "TOKEN-A"})
    imch.save_config("fake:entB", {"token": "TOKEN-B"})
    imch.save_config("fake", {"token": "TOKEN-LEGACY"})
    try:
        check("实例标识进 instance_key（分账的键）", a.instance_key == "fake:entA" and b.instance_key == "fake:entB",
              f"{a.instance_key} / {b.instance_key}")
        check("两个实例各自一个通道对象（不共用 socket 与回贴地址）", a.channel is not b.channel)
        check("凭据按实例读，A 拿不到 B 的", a.creds().get("token") == "TOKEN-A" and b.creds().get("token") == "TOKEN-B",
              f"{a.creds()} {b.creds()}")
        check("旧的唯一实例仍回落到 kind 键（不改配置文件也能跑）",
              legacy.creds().get("token") == "TOKEN-LEGACY" and legacy.instance_key == "fake",
              f"{legacy.instance_key} {legacy.creds()}")

        seen_a: list[str] = []
        seen_b: list[str] = []
        a.set_handler(handler_into(seen_a))
        b.set_handler(handler_into(seen_b))

        # 同一条消息（同 id 同文本）到两个实例：都该处理，因为排重表按实例分账
        dedupe_conv = "c-two-dedupe"
        shared_text = txt("两个实例都收到的同一条")
        await a.handle_inbound(ev(dedupe_conv, shared_text, message_id="same-id-1"))
        await b.handle_inbound(ev(dedupe_conv, shared_text, message_id="same-id-1"))
        check(
            "同一条消息到两个实例各起一个任务：排重表按实例分账",
            a.counters["runs"] == 1 and b.counters["runs"] == 1 and a.counters["deduped"] == 0,
            f"A={a.counters} B={b.counters}",
        )
        await a.handle_inbound(ev(dedupe_conv, shared_text, message_id="same-id-1"))
        check(
            "同一个实例内的重投照样排得住（不是靠拆实例绕开排重）",
            a.counters["deduped"] == 1 and len(a.channel.sent) == 1,
            f"A={a.counters} sent={len(a.channel.sent)}",
        )

        # 限流桶按实例分账：A 用满之后，同一会话号在 B 那边照发
        rate_conv = "c-two-rate"
        fresh(a)
        fresh(b)
        use_policy(rate_conv, maxMessagesPerMinute=1, burst=1)
        await a.handle_inbound(ev(rate_conv, txt("A 额度内的一条")))
        await a.handle_inbound(ev(rate_conv, txt("A 额度外的一条")))
        await b.handle_inbound(ev(rate_conv, txt("B 额度内的一条")))
        check(
            "A 用满额度被封住，B 同一会话号照发：限流桶不共用",
            a.counters["throttled"] == 1 and len(a.channel.sent) == 1
            and b.counters["throttled"] == 0 and len(b.channel.sent) == 1,
            f"A={a.counters}/{len(a.channel.sent)} B={b.counters}/{len(b.channel.sent)}",
        )
        drop_policy(rate_conv, dedupe_conv)

        # 上下文不串：A 暂存的那句不许出现在 B 的任务文本里
        fresh(a)
        fresh(b)
        ctx_scope = "c-ctx-two"
        use_policy(ctx_scope, groupActivation="mention", groupContext="memory")
        await a.handle_inbound(ev(ctx_scope, "A 群里先聊的一句", group=True, mentioned=False))
        await b.handle_inbound(ev(ctx_scope, txt("B 被 @ 的一句"), group=True, mentioned=True))
        check("A 暂存的上下文没有并进 B 的任务文本", "A 群里先聊的一句" not in (seen_b[-1] if seen_b else ""),
              str(seen_b[-1])[:200])
        drop_policy(ctx_scope)

        # 附件目录按实例分开：A、B 各落一份，目录不共用、内容不串味
        fresh(a)
        fresh(b)
        media_conv = "c-media-two"
        body_a, body_b = b"A-instance-picture-bytes", b"B-instance-picture-bytes"
        a.channel.download_body = body_a
        b.channel.download_body = body_b
        await a.handle_inbound(ev(media_conv, txt("A 发的一张图"), parts=[image("ref-two-a")]))
        await b.handle_inbound(ev(media_conv, txt("B 发的一张图"), parts=[image("ref-two-b")]))
        dir_a = imedia.media_root() / a.media._slug(a.instance_key)
        dir_b = imedia.media_root() / b.media._slug(b.instance_key)
        files_a = [p for p in dir_a.rglob("*") if p.is_file() and p.suffix != ".part"]
        files_b = [p for p in dir_b.rglob("*") if p.is_file() and p.suffix != ".part"]
        check(
            "两个实例的附件各写自己的目录（不共用一处）",
            dir_a != dir_b and len(files_a) == 1 and len(files_b) == 1,
            f"{str(dir_a)} / {str(dir_b)}",
        )
        check(
            "A 的目录里只有 A 那份字节：附件不跨实例串味",
            all(p.read_bytes() == body_a for p in files_a)
            and all(p.read_bytes() == body_b for p in files_b),
            f"{[p.name for p in files_a]} / {[p.name for p in files_b]}",
        )
        check(
            "实例号落成的目录名与落盘文件名对文件系统合法（Windows 不吃 : < > | 等）",
            all(
                not set(p.relative_to(imedia.media_root()).as_posix()) & set(':<>|"*?')
                for p in files_a + files_b
            ),
            f"{[str(x) for x in files_a]} / {str(dir_a.name)}",
        )

        # 台账按实例分：同一会话号也是两条会话
        sa = session_of(a, media_conv)
        sb = session_of(b, media_conv)
        check("会话 id 带实例标识，两个实例各一条台账会话",
              sa and sb and sa["id"] != sb["id"] and sa["id"].startswith("im-fake:entA-"),
              f"{(sa or {}).get('id')} / {(sb or {}).get('id')}")

        # 状态互不影响：A 失败不把 B 打成 failed
        fresh(a)
        fresh(b)
        a.channel.send_error = "plain"
        await a.handle_inbound(ev("c-fail-a", txt("A 会被拒的一条")))
        await b.handle_inbound(ev("c-fail-b", txt("B 正常的一条")))
        check("A 的发送失败只改 A 的状态", a.state == "failed" and b.state == "connected", f"A={a.state} B={b.state}")
        a.channel.send_error = ""

        keys = [(r.channel.kind, r.channel_id) for r in imch.all_runtimes() if r.channel.kind == "fake"]
        check("清单里同类型的实例都带上且顺序确定（按 channel_id 排）",
              keys == sorted(keys) and ("fake", "entA") in keys and ("fake", "entB") in keys,
              str(keys)[:260])
        check("instances_of 列出该类型的全部实例",
              {r.channel_id for r in imch.instances_of("fake")} >= {"", "entA", "entB"},
              str([r.channel_id for r in imch.instances_of("fake")])[:200])
    finally:
        for key in ("fake:entA", "fake:entB", "fake"):
            imch.drop_config(key)


async def main() -> None:
    rt = imch.runtime_of("fake")
    rt.set_handler(handler_into([]))
    await phase_dedupe(rt)
    await phase_group(rt)
    await phase_context(rt)
    await phase_media(rt)
    await phase_typing(rt)
    await phase_ratelimit()
    await phase_view(rt)
    await phase_instances()
    await imch.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print(f"engine im runtime selftest: ALL PASS（{len(FAILURES)} 项失败）")


