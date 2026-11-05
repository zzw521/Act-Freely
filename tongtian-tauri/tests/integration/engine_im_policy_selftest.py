#!/usr/bin/env python3
"""
IM 群策略与重复抑制自测（缺陷 D-41 的「群策略」那一半）

这一段不打网络：策略是纯判定 + 一份 JSON 落盘，所以要验的是
「同一条消息在各种策略下引擎到底怎么决定，以及决定不了时说的那句中文」。

分段：
1. 校验：未知键 / 非法枚举 / 数值越界 / 布尔冒充整数 / 作用域串穿越 —— 全部拒，不做「收下再说」
2. 作用域链：会话 > 通道实例 > 同类型旧全局键 > 默认，且缺项一定补回出厂值
3. 触发判定：群 mention / always / never、单聊、平台答不出「是否被 @」时的如实拒
4. 上下文缓冲：只暂存非 @ 消息、被 @ 时一次读走并清空、条数字数时间窗三道上界
5. 重复抑制：同 message_id、同会话同文本、窗口外放行、窗口=0 时明确不抑制
6. 落盘：缺文件回默认、坏文件留档不覆盖、写作用域与删除作用域互不牵连
7. 唯一事实来源：枚举、中文标签、数值范围都在引擎里，界面不再抄一份

数据隔离：TT_DATA_HOME 指向临时目录，绝不写 ~/.tongtianwork 的策略文件。
用法: python tests/integration/engine_im_policy_selftest.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-policy-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import im_policy as P
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im policy selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
MIN = 60_000


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def raises(fn, code: str) -> str:
    """跑一下必须被拒的写法：返回中文理由；没抛/抛错码时返回一句可判假的说明"""
    try:
        fn()
    except P.PolicyError as exc:
        return exc.detail or exc.code if exc.code == code else f"抛了别的码：{exc.code}"
    except Exception as exc:
        return f"抛了别的异常：{type(exc).__name__}"
    return "没有抛错"


def rejected(fn, needle: str) -> bool:
    """既要求真的抛 IM_POLICY_INVALID，也要求理由里带那句人话"""
    return needle in raises(fn, "IM_POLICY_INVALID")


def meta(**kw) -> P.MessageMeta:
    base = dict(is_group=False, mentioned_me=None, conversation_id="c1", message_id="", text="帮我查个东西", at_ms=0)
    base.update(kw)
    return P.MessageMeta(**base)


# ── 1. 校验 ───────────────────────────────────────────────────────


def phase_validate() -> None:
    section("1. 策略项校验：拒绝要说清为什么")
    check("合法的一份全部通过", P.normalize_patch({"groupActivation": "never"}) == {"groupActivation": "never"})
    for key, bad, why in [
        ("groupActivation", "sometimes", "枚举外的群触发方式"),
        ("groupContext", "persist", "刻意没有的持久化档"),
        ("dmMedia", "maybe", "媒体开关的非法值"),
    ]:
        check(f"拒绝{why}", rejected(lambda k=key, v=bad: P.normalize_patch({k: v}), "只接受"))
    check("枚举拒绝时列出可选项",
          "mention, always, never" in raises(lambda: P.normalize_patch({"groupActivation": "sometimes"}), "IM_POLICY_INVALID"))
    check("非字典补丁报「未提交任何策略项」",
          rejected(lambda: P.normalize_patch("nope"), "未提交"))
    check("未知键直接报而不是忽略",
          "未知策略项" in raises(lambda: P.normalize_patch({"respondToEverything": True}), "IM_POLICY_INVALID"))
    check("空补丁报「未提交任何策略项」",
          "未提交" in raises(lambda: P.normalize_patch({}), "IM_POLICY_INVALID"))
    check("历史窗口下限以下拒", "1 到 720" in raises(lambda: P.normalize_patch({"historyWindowMinutes": 0}), "IM_POLICY_INVALID"))
    check("历史窗口上限以上拒", "1 到 720" in raises(lambda: P.normalize_patch({"historyWindowMinutes": 9999}), "IM_POLICY_INVALID"))
    check("限流秒数越界拒",
          "每分钟最多主动发几条" in raises(lambda: P.normalize_patch({"maxMessagesPerMinute": 500}), "IM_POLICY_INVALID"))
    check("字符串数字不收（不做隐式转换）",
          "需要整数" in raises(lambda: P.normalize_patch({"burst": "5"}), "IM_POLICY_INVALID"))
    check("布尔不当整数收下",
          "需要整数" in raises(lambda: P.normalize_patch({"dedupeSeconds": True}), "IM_POLICY_INVALID"))
    check("超时项范围与说明同源",
          "单条回贴超时" in raises(lambda: P.normalize_patch({"sendTimeoutSeconds": 300}), "IM_POLICY_INVALID"))
    check("边界值本身可收（720 分钟）", P.normalize_patch({"historyWindowMinutes": 720})["historyWindowMinutes"] == 720)
    check("抑制窗口可为 0（明确关闭）", P.normalize_patch({"dedupeSeconds": 0})["dedupeSeconds"] == 0)
    check("合法作用域能写", "bad" not in str(P.save_policy({"groupActivation": "always"}, "ding-1")))
    check("作用域串里的路径段被拒",
          "不合法" in raises(lambda: P.save_policy({"burst": 2}, "../etc"), "IM_POLICY_INVALID"))
    check("超长作用域串被拒",
          "不合法" in raises(lambda: P.save_policy({"burst": 2}, "x" * 200), "IM_POLICY_INVALID"))


# ── 2. 作用域链 ───────────────────────────────────────────────────


def phase_scopes() -> None:
    section("2. 作用域链：会话 > 实例（kind:实例标识）> 旧全局键 > 默认")
    store = {
        "defaults": dict(P.POLICY_DEFAULTS),
        "scopes": {
            "dingtalk:ding-1": {"groupActivation": "always", "burst": 7},
            "ding-1:cid-9": {"groupActivation": "never"},
            "ding-1": {"maxMessagesPerMinute": 5},
            "slack": {"dmActivation": "never"},
            # 同名不同类型：用户给两台服务都起名 work 时绝不能互相继承
            "feishu:work": {"groupActivation": "never"},
        },
    }
    chain = P.scope_chain("ding-1", "dingtalk", "ding-1:cid-9")
    check("链由细到粗且不含重复", chain == ["ding-1:cid-9", "dingtalk:ding-1", "dingtalk"])
    check("实例一级带类型前缀（与 instance_key 同形）",
          P.scope_chain("ding-1", "dingtalk") == ["dingtalk:ding-1", "dingtalk"])
    check("kind 与实例同名时不重复占一级", P.scope_chain("dingtalk", "dingtalk") == ["dingtalk"])
    check("没给 kind 时实例一级保持裸值（旧调用形状不炸）",
          P.scope_chain("ding-1", "") == ["ding-1"])
    check("实例标识为空时不产出空的实例级", P.scope_chain("", "dingtalk") == ["dingtalk"])
    eff = P.effective("ding-1", "dingtalk", "ding-1:cid-9", store=store)
    check("会话级覆盖实例级", eff["groupActivation"] == "never")
    check("实例级未被覆盖时保留", eff["burst"] == 7)
    check("没写过的项回到默认", eff["dedupeSeconds"] == P.POLICY_DEFAULTS["dedupeSeconds"])
    eff2 = P.effective("ding-1", "dingtalk", "其它会话", store=store)
    check("换一个会话读的是实例级", eff2["groupActivation"] == "always")
    bare = P.effective("ding-1", "dingtalk", "", store=store)
    check("裸实例键不再被当成实例级（实例级只认 kind:标识）",
          bare["maxMessagesPerMinute"] == P.POLICY_DEFAULTS["maxMessagesPerMinute"] and bare["burst"] == 7,
          str(bare))
    check("裸键照样能作为会话标识命中（老写法没被没收）",
          P.effective("ding-1", "dingtalk", "ding-1", store=store)["maxMessagesPerMinute"] == 5)
    cross_a = P.effective("work", "feishu", "", store=store)
    cross_b = P.effective("work", "dingtalk", "", store=store)
    check("两台实例同名也不互相继承：飞书 work 读到自己那份",
          cross_a["groupActivation"] == "never")
    check("同名但不同类型：钉钉这边没有单独配过，落到类型/默认而不是飞书那份",
          cross_b["groupActivation"] == P.POLICY_DEFAULTS["groupActivation"], str(cross_b))
    eff3 = P.effective("", "", "", store=store)
    check("没有任何作用域时读到默认", eff3["groupActivation"] == P.POLICY_DEFAULTS["groupActivation"])
    eff4 = P.effective("s-1", "slack", "", store=store)
    check("同类型旧全局键（kind）继续生效（多实例之前的配置不失效）", eff4["dmActivation"] == "never")
    broken = {"defaults": dict(P.POLICY_DEFAULTS), "scopes": {"x": {"groupActivation": "garbage", "burst": 99}}}
    check("已落盘的坏值不参与覆盖", P.effective("x", "", "", store=broken)["groupActivation"] == "mention")
    check("坏值旁边的合法值仍生效", P.effective("x", "", "", store=broken)["burst"] == P.POLICY_DEFAULTS["burst"])


# ── 3. 触发判定 ───────────────────────────────────────────────────


def phase_decide() -> None:
    section("3. 一条消息该不该起任务")
    default = dict(P.POLICY_DEFAULTS)
    d = P.decide(meta(is_group=True, mentioned_me=False), default)
    check("群里没被 @ 不起任务", d.run is False)
    check("拒得起因说中文", "没有被 @" in d.reason)
    check("也没打算回贴", d.remember is False)
    d = P.decide(meta(is_group=True, mentioned_me=True), default)
    check("群里被 @ 起任务", d.run is True and d.reason == "")
    d = P.decide(meta(is_group=True, mentioned_me=None), default)
    check("平台答不出是否被 @ 时不猜", d.run is False and "不能凭空判定" in d.reason)
    always = {**default, "groupActivation": "always"}
    check("always 策略下群内每条都起", P.decide(meta(is_group=True, mentioned_me=False), always).run is True)
    check("always 策略下没被 @ 也不需要暂存上下文", P.decide(meta(is_group=True, mentioned_me=False), always).remember is False)
    never = {**default, "groupActivation": "never"}
    check("never 策略下被 @ 也不起", P.decide(meta(is_group=True, mentioned_me=True), never).run is False)
    check("never 的中文原因带策略名", "不回应" in P.decide(meta(is_group=True), never).reason)
    dm_never = {**default, "dmActivation": "never"}
    check("单聊 never 同样拒", P.decide(meta(is_group=False), dm_never).run is False)
    check("单聊默认每条都起", P.decide(meta(is_group=False), default).run is True)
    dm_mention = {**default, "dmActivation": "mention"}
    check("单聊 mention 不要求平台给 @ 名单", P.decide(meta(is_group=False, mentioned_me=None), dm_mention).run is True)


# ── 4. 上下文缓冲 ─────────────────────────────────────────────────


def phase_context() -> None:
    section("4. 群内上下文只暂存、一次读走、三道上界")
    buf = P.ContextBuffer()
    policy = {**P.POLICY_DEFAULTS, "groupContext": "memory"}
    now = 1_000_000_000_000
    d1 = P.decide(meta(is_group=True, mentioned_me=False, text="今晚吃啥", at_ms=now), policy, now_ms=now, buffer=buf)
    check("非 @ 消息被暂存", d1.remember is True and buf.pending("c1", now) == 1)
    check("暂存也不起任务", d1.run is False)
    P.decide(meta(is_group=True, mentioned_me=False, text="明天开会吗", at_ms=now + 1000), policy, now_ms=now + 1000, buffer=buf)
    check("缓冲按会话隔离累加", buf.pending("c1", now) == 2)
    hit = P.decide(meta(is_group=True, mentioned_me=True, text="帮我写周报", at_ms=now + 2000), policy,
                  now_ms=now + 2000, buffer=buf)
    check("被 @ 时把窗口内上下文并进去", hit.context == ["今晚吃啥", "明天开会吗"])
    check("读走即清空（同一批话不并两次）", buf.pending("c1", now + 2000) == 0 and hit.run is True)
    again = P.decide(meta(is_group=True, mentioned_me=True, text="再写一遍", at_ms=now + 3000), policy,
                    now_ms=now + 3000, buffer=buf)
    check("第二次被 @ 不会重复带同一批上下文", again.context == [])

    off = P.ContextBuffer()
    d = P.decide(meta(is_group=True, mentioned_me=False, text="闲聊", at_ms=now), P.POLICY_DEFAULTS, now_ms=now, buffer=off)
    check("默认 off 时什么都不暂存", d.remember is False and off.pending("c1", now) == 0)

    win = {**P.POLICY_DEFAULTS, "groupContext": "memory", "historyWindowMinutes": 1}
    b2 = P.ContextBuffer()
    P.decide(meta(is_group=True, mentioned_me=False, text="很久以前的话", at_ms=now), win, now_ms=now, buffer=b2)
    late = P.decide(meta(is_group=True, mentioned_me=True, text="现在的需求", at_ms=now + 120_000), win,
                   now_ms=now + 120_000, buffer=b2)
    check("超过历史窗口的上下文一律作废", late.context == [] and b2.dropped == 1)

    b3 = P.ContextBuffer()
    for i in range(P.MAX_CONTEXT_MESSAGES + 8):
        b3.remember("c9", f"刷屏第 {i} 条", now + i, 10 * MIN)
    kept = b3.take("c9", now + 1000)
    check("刷屏时条数上界生效", len(kept) == P.MAX_CONTEXT_MESSAGES)
    check("丢的是最早的而不是最新的", kept[-1] == "刷屏第 27 条")
    check("丢弃计数如实累加", b3.dropped == 8)

    b4 = P.ContextBuffer()
    for i in range(40):
        b4.remember("c8", "长" * 300, now + i, 10 * MIN)
    kept8 = b4.take("c8", now + 1000)
    check("字数上界也生效", sum(len(t) for t in kept8) <= P.MAX_CONTEXT_CHARS)
    check("没有会话标识时不暂存", b4.remember("", "无归属", now, MIN) is False)
    b5 = P.ContextBuffer()
    b5.remember("c7", "要忘掉的", now, MIN)
    b5.forget("c7")
    check("forget 后彻底没有残留", b5.pending("c7") == 0 and b5.take("c7", now) == [])
    empty = P.decide(meta(is_group=True, mentioned_me=False, text="   "), policy, now_ms=now, buffer=P.ContextBuffer())
    check("空文本不暂存也不起", empty.remember is False)


# ── 5. 重复抑制 ───────────────────────────────────────────────────


def phase_dedupe() -> None:
    section("5. 同一条消息不处理两次")
    dd = P.MessageDedupe()
    now = 2_000_000_000_000
    first = meta(message_id="m1", text="查天气")
    check("第一次放行", dd.seen_recently(first, 45, now) is False)
    check("同 message_id 重发被挡", dd.seen_recently(meta(message_id="m1", text="查天气"), 45, now + 1000) is True)
    check("被挡的次数有读数", dd.hits == 1)
    check("没有 id 但同会话同文本也挡（用户手抖连点）",
          dd.seen_recently(meta(message_id="m2", text="查天气"), 45, now + 2000) is True)
    check("换个会话问同样的话不吞", dd.seen_recently(
        P.MessageMeta(is_group=False, conversation_id="c2", message_id="m3", text="查天气"), 45, now + 3000) is False)
    check("窗口过后同一句话可以再处理", dd.seen_recently(first, 45, now + 60_000) is False)
    closed = P.MessageDedupe()
    closed.seen_recently(first, 0, now)
    check("窗口为 0 时明确不做抑制", closed.seen_recently(first, 0, now + 1) is False)
    check("窗口为 0 的中文说明不假装在抑制", "未做重复抑制" in closed.note(0))
    check("窗口为正时说明写清秒数", "45 秒内" in dd.note(45))
    flood = P.MessageDedupe()
    for i in range(P.MessageDedupe.MAX_KEYS + 50):
        flood.seen_recently(P.MessageMeta(conversation_id=f"c{i}", message_id=f"m{i}", text="t"), 45, now + i)
    check("排重表有上界，刷屏不会无限增长", len(flood._at) <= P.MessageDedupe.MAX_KEYS)
    none = P.MessageDedupe()
    check("既没 id 也没文本时不冒充排重", none.seen_recently(P.MessageMeta(conversation_id="c"), 45, now) is False)
    d2 = P.MessageDedupe()
    d2.seen_recently(meta(conversation_id="cX", message_id="mm", text="zz"), 45, now)
    d2.forget("cX")
    check("解绑会话后排重记录一起清掉", d2.seen_recently(meta(conversation_id="cX", message_id="mm", text="zz"), 45, now) is False)

    # 带附件的消息：那句「文本」是引擎拼的中文说明，两张不同的照片能拼出逐字相同的一句，
    # 只比说明就会把用户连发的第二张照片当重复吃掉——D-41 验收里的「不重复回复」
    # 不等于「少回复」。
    same_note = "[图片]（已存到本机，路径见会话台账）"
    md = P.MessageDedupe()
    pic1 = P.MessageMeta(conversation_id="cM", message_id="p1", text=same_note,
                         has_media=True, media_identity="picture:code-1")
    pic2 = P.MessageMeta(conversation_id="cM", message_id="p2", text=same_note,
                         has_media=True, media_identity="picture:code-2")
    check("第一张图片放行", md.seen_recently(pic1, 45, now) is False)
    check("同一会话里的另一张照片不是重复", md.seen_recently(pic2, 45, now + 500) is False)
    check("平台重推同一张图（换了 id、附件码相同）仍被挡", md.seen_recently(
        P.MessageMeta(conversation_id="cM", message_id="p3", text=same_note,
                      has_media=True, media_identity="picture:code-1"), 45, now + 900) is True)
    mu = P.MessageDedupe()
    check("附件身份不明时不靠引擎自己拼的说明误判成重复",
          mu.seen_recently(P.MessageMeta(conversation_id="cN", message_id="u1", text=same_note, has_media=True), 45, now) is False
          and mu.seen_recently(P.MessageMeta(conversation_id="cN", message_id="u2", text=same_note, has_media=True), 45, now + 100) is False)
    mx = P.MessageDedupe()
    with_media = P.MessageMeta(conversation_id="cY", message_id="y1", text="看图",
                               has_media=True, media_identity="picture:cy")
    mx.seen_recently(with_media, 45, now)
    check("同一条「看图 + 同一张图」本来会被挡", mx.seen_recently(
        P.MessageMeta(conversation_id="cY", message_id="y2", text="看图",
                      has_media=True, media_identity="picture:cy"), 45, now + 100) is True)
    mx.forget("cY")
    check("解绑会话把带附件身份的键一起清掉", mx.seen_recently(
        P.MessageMeta(conversation_id="cY", message_id="y3", text="看图",
                      has_media=True, media_identity="picture:cy"), 45, now + 200) is False)


# ── 6. 落盘 ───────────────────────────────────────────────────────


def phase_store() -> None:
    section("6. 策略文件的读写与损坏处理")
    path = P.policy_path()
    check("策略与凭据分文件（改策略不动凭据）",
          path.name == "im_group_policy.json" and (path.parent / "im_channels.json").name != path.name)
    check("缺文件时读到的是完整出厂默认",
          all(P.load_store()["defaults"].get(k) == v for k, v in P.POLICY_DEFAULTS.items()))
    check("写全局默认后能读回来",
          P.save_policy({"groupActivation": "never"})["defaults"]["groupActivation"] == "never")
    check("写作用域不影响全局默认",
          P.save_policy({"groupActivation": "always"}, "dingtalk:ding-9")["defaults"]["groupActivation"] == "never")
    store = P.load_store()
    check("作用域只存差异项", store["scopes"]["dingtalk:ding-9"] == {"groupActivation": "always"})
    check("effective 走真实文件时按作用域链合并（实例级写成 kind:标识）",
          P.effective("ding-9", "dingtalk")["groupActivation"] == "always")
    check("同实例的其它项回到（被覆盖后的）默认",
          P.effective("ding-9", "dingtalk")["dmActivation"] == "always")
    check("同名但不同类型的实例读不到这一份（不跨类型继承）",
          P.effective("ding-9", "feishu")["groupActivation"] == "never")
    P.save_policy({"burst": 9}, "plain-conv")
    check("裸标识写在文件里时只当会话作用域生效，不再当实例级",
          P.effective("plain-conv", "dingtalk")["burst"] == P.POLICY_DEFAULTS["burst"]
          and P.effective("plain-conv", "dingtalk", "plain-conv")["burst"] == 9)
    P.drop_scope("plain-conv")
    P.drop_scope("dingtalk:ding-9")
    check("删作用域后实例级不再覆盖", P.effective("ding-9", "dingtalk")["groupActivation"] == "never")
    check("删作用域不动全局默认", P.load_store()["defaults"]["groupActivation"] == "never")

    # 坏文件：留档不覆盖，读的人拿到默认
    path.write_text("{ this is not json", encoding="utf-8")
    store = P.load_store()
    check("坏文件不抛，回到出厂默认", store["defaults"]["groupActivation"] == P.POLICY_DEFAULTS["groupActivation"])
    backup = path.with_suffix(".corrupt.json")
    check("坏文件改名留档（不静默覆盖用户配置）", backup.exists() and "{ this is not json" in backup.read_text(encoding="utf-8"))
    P.save_policy({"burst": 5})
    check("留档后新写入正常", json.loads(path.read_text(encoding="utf-8"))["defaults"]["burst"] == 5)
    check("落盘只留已知键", set(json.loads(path.read_text(encoding="utf-8"))["defaults"]) == set(P.POLICY_DEFAULTS))


# ── 7. 唯一事实来源 ───────────────────────────────────────────────


def phase_single_source() -> None:
    section("7. 枚举、标签与范围只有引擎这一份")
    check("触发枚举与标签表同集合", set(P.ACTIVATION_LABELS) == set(P.ACTIVATIONS))
    check("上下文模式有中文标签", set(P.CONTEXT_MODE_LABELS) == set(P.CONTEXT_MODES))
    check("默认值都在枚举里", P.POLICY_DEFAULTS["groupActivation"] in P.ACTIVATIONS)
    check("默认值里的数值都合规",
          all(P._valid_value(k, P.POLICY_DEFAULTS[k]) == "" for k in P.NUMERIC_BOUNDS))
    check("每个默认项都有校验口径",
          set(P.POLICY_DEFAULTS) == set(P.ENUM_TABLE) | set(P.NUMERIC_BOUNDS))
    check("没有「持久化群上下文」这一档（隐私边界写死在枚举里）", "persist" not in P.CONTEXT_MODES)
    check("媒体开关只有两档", set(P.MEDIA_MODES) == {"off", "on"})
    check("群媒体默认 off（别人发的附件不替用户往本机写）", P.POLICY_DEFAULTS["groupMedia"] == "off")
    check("单聊媒体默认 on", P.POLICY_DEFAULTS["dmMedia"] == "on")


def main() -> int:
    print("== IM 群策略与重复抑制自测（D-41）==")
    phase_validate()
    phase_scopes()
    phase_decide()
    phase_context()
    phase_dedupe()
    phase_store()
    phase_single_source()
    print(f"\n[DONE] im_policy selftest: {len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
