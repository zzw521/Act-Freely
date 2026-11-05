#!/usr/bin/env python3
"""D-13（#140）应用快照与浏览器上下文接力：引擎侧自测（离线，零真机）。

跑法（必须用 vendor，`.venv` 缺依赖会整族 SKIP 且 rc=0，看着像绿其实没跑）：

    ./vendor/python/python.exe -X utf8 tests/integration/engine_snapshot_selftest.py

**离线自测绿不等于真机可用。** 本模块不截屏（OS 采集在桌面壳里，workspace 车道的 IPC
未交棒），也不发起任何浏览器动作（那些在 ``browser_use`` 与 W5 的 ``/api/browser`` 里）。
本自测钉的是：分级判定与 Rust ``security/classify.rs`` **逐字同源**、触发→预览→确认→上送
这条流水线**预览与上送是同一段文本**、机密级**没有绕过开关**、正文**不落盘**、
接力边界的四类拒绝**各有各的一句话**。

判定只看 ``^[PASS]``；``^[FAIL]`` 必须为零。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-snapshot-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

try:
    import privacy_guard as pg
    import snapshot_relay as relay
    import snapshot_runtime as snap
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: snapshot selftest unavailable ({exc})")
    sys.exit(0)

FAILURES: list[str] = []
RUST_CLASSIFY = PROJECT_ROOT / "src-tauri" / "src" / "security" / "classify.rs"


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def fresh_snapshot(**kwargs) -> snap.SnapshotRegistry:
    os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")
    return snap.reset_registry(**kwargs)


def fresh_relay(**kwargs) -> relay.RelayRegistry:
    os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")
    return relay.reset_registry(**kwargs)


def rust_keyword_table(source: str, name: str) -> list[str]:
    """从 Rust 源码里抠出一张关键词表的字面量（跨语言钉就靠它）。"""
    match = re.search(rf"pub const {name}: &\[&str\] = &\[(.*?)\];", source, re.S)
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def controlled_runtime(**extra) -> dict:
    block = {
        "running": True,
        "persisted": True,
        "policy": "auto",
        "profileDir": "C:/Users/dev/AppData/Roaming/tongtianwork/browser/root/profile",
    }
    block.update(extra)
    return {"browserUse": block}


# ── 1. 分级与 Rust 同源 ─────────────────────────────────────────


def classification_parity_checks() -> None:
    section("1. 分级判定与 src-tauri/src/security/classify.rs 逐字同源")
    if not RUST_CLASSIFY.exists():
        check("Rust 分级源码可读（跨语言钉的前提）", False, str(RUST_CLASSIFY))
        return
    source = RUST_CLASSIFY.read_text(encoding="utf-8")
    check("CERT_KEYWORDS 三张表逐字一致", tuple(rust_keyword_table(source, "CERT_KEYWORDS")) == snap.CERT_KEYWORDS,
          f"rust={rust_keyword_table(source, 'CERT_KEYWORDS')} python={list(snap.CERT_KEYWORDS)}")
    check("COMMERCIAL_KEYWORDS 逐字一致",
          tuple(rust_keyword_table(source, "COMMERCIAL_KEYWORDS")) == snap.COMMERCIAL_KEYWORDS,
          f"rust={rust_keyword_table(source, 'COMMERCIAL_KEYWORDS')}")
    check("INTERNAL_KEYWORDS 逐字一致", tuple(rust_keyword_table(source, "INTERNAL_KEYWORDS")) == snap.INTERNAL_KEYWORDS,
          f"rust={rust_keyword_table(source, 'INTERNAL_KEYWORDS')}")
    for literal in ('pub const PUBLIC: &str = "public"', 'pub const INTERNAL: &str = "internal"',
                    'pub const CONFIDENTIAL: &str = "confidential"'):
        check(f"分级取值与 Rust 同一组字面量（{literal.split('\"')[1]}）", literal in source)
    check("三档取值在本模块等于 Rust 的那三个字符串",
          (snap.SENSITIVITY_PUBLIC, snap.SENSITIVITY_INTERNAL, snap.SENSITIVITY_CONFIDENTIAL) == ("public", "internal", "confidential"))

    # 置信度阶梯：Rust 里 pii=0.95 / high=0.9 / medium=0.85 / low=0.7 / 其余 0.5
    check("PII 命中按 confidential 且置信度 0.95", snap.classify_text("联系 13800001234").confidence == 0.95,
          str(snap.classify_text("联系 13800001234").confidence))
    check("证件类关键词按 confidential 0.9", snap.classify_text("请带上护照复印件").confidence == 0.9)
    check("商业类关键词按 confidential 0.85", snap.classify_text("附件是报价").confidence == 0.85)
    check("内部标记按 internal 0.7", snap.classify_text("仅限内网访问").confidence == 0.7)
    check("什么都没命中按 public 0.5", snap.classify_text("今天天气不错").confidence == 0.5)

    # 同一句里 PII 与关键词并存时取最高档（Rust 的判定顺序是 pii 先）
    both = snap.classify_text("内部机密，身份证 110101199003077757")
    check("PII 与内部标记并存时取 PII 那一档", both.sensitivity == snap.SENSITIVITY_CONFIDENTIAL and both.confidence == 0.95,
          f"{both.sensitivity}/{both.confidence}")
    check("同一特征重复命中只记一条（Rust 的 push_finding 去重口径）",
          len([f for f in snap.classify_text("身份证 身份证 身份证").findings if f["pattern"] == "身份证"]) == 1)

    # 分组写法与校验位（Rust 的 digit_runs 容忍空格/连字符分组、17 位 + X）
    check("带空格分组的银行卡号仍算 PII", snap.classify_text("卡号 6222 0212 3456 7890").pii)
    check("带连字符分组的银行卡号仍算 PII", snap.classify_text("卡号 6222-0212-3456-7890").pii)
    check("17 位 + X 的身份证号算 PII", snap.classify_text("证号 11010119900307775X").pii)
    check("邮箱地址算 PII 且只记一条", len([f for f in snap.classify_text("a@b.com 与 c@d.org").findings if f["kind"] == "pii"]) == 1)
    check("普通长数字不误判成 PII", not snap.classify_text("一共 12345678 个字符").pii)
    check("空文本按 public 且不报错", snap.classify_text("").sensitivity == snap.SENSITIVITY_PUBLIC)

    # force_local：Rust 口径是「PII 或机密即为真」
    check("PII 命中即要求本地通道", snap.classify_text("手机 13800001234").force_local)
    check("internal 不要求本地通道", not snap.classify_text("内部讨论").force_local)
    check("public 既不 pii 也不 force_local", not snap.classify_text("天气").pii and not snap.classify_text("天气").force_local)

    # 三句 summary 与 Rust 的 match 分支逐字一致
    for sample, expected in (
        ("身份证 110101199003077757", "检测到高度敏感信息，建议仅使用本地模型处理"),
        ("内部讨论纪要", "检测到内部敏感内容，建议限制使用范围"),
        ("今天开会聊排期", "未检测到敏感信息，可正常使用"),
    ):
        got = snap.classify_text(sample).summary
        check(f"summary 与 Rust 同句（{expected[:12]}…）", got == expected, got)
    check("三句 summary 原文都出现在 Rust 源码里（不是自造文案）",
          all(needle in source for needle in ("检测到高度敏感信息，建议仅使用本地模型处理",
                                              "检测到内部敏感内容，建议限制使用范围",
                                              "未检测到敏感信息，可正常使用")))


def secret_divergence_checks() -> None:
    section("2. 明文密钥是本模块有意的分歧（Rust 只脱敏不分级）")
    secret_text = "顺手贴一个 sk-abcdefghijklmnop1234"
    result = snap.classify_text(secret_text)
    check("明文密钥被算成机密（Rust 的 classify 里没有这一条）",
          result.sensitivity == snap.SENSITIVITY_CONFIDENTIAL and result.secret_hits == 1, f"{result.sensitivity}/{result.secret_hits}")
    check("命中的是 secret 这一类而不是 keyword/pii", all(f["kind"] == "secret" for f in result.findings) and bool(result.findings))
    source = RUST_CLASSIFY.read_text(encoding="utf-8") if RUST_CLASSIFY.exists() else ""
    check("Rust 只在 redact 里用 is_secret，classify 里没有（分歧是被读出来的，不是猜的）",
          "if is_secret(&t)" in source and 'push_finding(&mut findings, "secret"' not in source
          and "is_secret" not in source[source.index("pub fn classify"):source.index("pub fn classify") + 2000],
          "classify() 里出现了 is_secret")
    check("分歧在模块 docstring 里点名（不藏进实现细节）",
          "sk-" in (Path(__file__).resolve().parents[2] / "engine" / "snapshot_runtime.py").read_text(encoding="utf-8"))
    check("非 sk- 形态的令牌不误判（与 Rust 的已知边界一致）",
          not snap.classify_text("Bearer zzzzzzzzzzzzzzzzzzzz").secret_hits)


# ── 2. 触发与校验 ───────────────────────────────────────────────


def capture_checks() -> None:
    section("3. 触发：收采集物，不收屏幕（本模块不截屏）")
    reg = fresh_snapshot()
    draft, code, message = reg.capture(text="", image_b64="", app_label="记事本")
    check("什么都没交上来时不当成功（且文案点名本模块不截屏）",
          draft is None and code == snap.CODE_NOTHING_CAPTURED and "不截屏" in message, message)

    draft, code, _ = reg.capture(text="x" * (snap.MAX_CAPTURE_TEXT_CHARS + 1))
    check("超长正文被拒而不是被截断收下", draft is None and code == snap.CODE_TEXT_TOO_LARGE)

    draft, code, message = reg.capture(text="正文", image_b64="aGVsbG8=", image_mime="image/gif")
    check("非截图形态的图片被拒", draft is None and code == snap.CODE_IMAGE_FORMAT and "image/png" in message)

    # 先确认标准库在严格模式下真会拒绝这串（否则下面那条断言是空的）
    try:
        base64.b64decode("!!not-base64!!", validate=True)
        strict_rejects = False
    except Exception:  # noqa: BLE001
        strict_rejects = True
    check("自测前提：严格 base64 校验确实会拒这串", strict_rejects)

    reg_probe = fresh_snapshot()
    draft, code, _ = reg_probe.capture(text="正文", image_b64="!!not-base64!!", image_mime="image/png")
    check("坏 base64 被拒且不留草稿（validate=False 会静默解成空图，那是假绿）",
          draft is None and code == snap.CODE_BAD_BASE64, f"{code} / {draft}")

    empty, code_e, _ = reg_probe.capture(text="正文", image_b64="", image_mime="image/png")
    check("只声明 png 却没给正文时不算带图（空 base64 走「没有图片」那条）",
          empty is not None and empty.has_image is False and code_e == "")

    zero, code_z, _ = reg_probe.capture(text="正文", image_b64="====", image_mime="image/png")
    check("解出来 0 字节的图被拒而不是当成功", zero is None and code_z == snap.CODE_BAD_BASE64, code_z)

    draft, code, _ = reg_probe.capture(text="正文", image_b64="aGk=", image_mime="image/png")
    check("合法 png 被收下且记了字节数", draft is not None and draft.has_image and draft.image_bytes == b"hi", code)
    folded_input = "aG" + chr(10) + "k" + chr(10) + "="
    folded, code_f, _ = reg_probe.capture(text="正文", image_b64=folded_input, image_mime="image/png")
    check("76 列折行的 base64 仍算合法输入（换行不是坏输入）", folded is not None and folded.has_image, code_f)
    data_url, code_d, _ = reg_probe.capture(text="正文", image_b64="data:image/png;base64,aGk=", image_mime="image/png")
    check("也收 data URL 形态的采集物", data_url is not None and data_url.image_bytes == b"hi", code_d)

    reg = fresh_snapshot()
    over = base64.b64encode(b"Y" * (snap.MAX_IMAGE_BYTES + 1)).decode("ascii")
    draft, code, _ = reg.capture(text="正文", image_b64=over, image_mime="image/jpeg")
    check("超限图片被拒（不是默默收下）", draft is None and code == snap.CODE_IMAGE_TOO_LARGE, code)
    check("超限图片不留草稿", draft is None)

    reg = fresh_snapshot()
    draft, code, _ = reg.capture(text="项目进度 30%", app_label="浏览器", task_id="t-1", origin="desktop")
    check("一次正常触发进待确认态（尚未上送）", draft is not None and draft.status == snap.STATUS_PENDING and code == "")
    check("待确认态的中文说清「尚未上送」", snap.STATUS_LABELS[snap.STATUS_PENDING] == "待确认（尚未上送）")
    check("触发不写正文到任何数据文件", not any(
        (path).read_text(encoding="utf-8", errors="replace").find("项目进度 30%") >= 0
        for path in (_TMP / "home").rglob("*.jsonl")))


# ── 3. 预览与上送同一段文本 ─────────────────────────────────────


def preview_checks() -> None:
    section("4. 上送前可见可取消：预览与上送是同一条代码路径的产出")
    clock = {"now": 1000.0}
    reg = fresh_snapshot(clock=lambda: clock["now"])
    draft, _c, _m = reg.capture(text="第一段\n第二段", app_label="记事本")
    preview, code, _msg, status = reg.preview(draft.draft_id)
    payload, code2, _msg2, _s2 = reg.confirm(draft.draft_id)
    check("预览成功且没有 code", preview is not None and code == "" and status == 200, code)
    check("预览正文 == 真正上送的正文（不是演一场戏）",
          preview["preview"] == payload["contextBlocks"][0]["text"], f"{preview['preview']!r} vs {payload['contextBlocks'][0]['text']!r}")
    check("上送的只有一个 text 块（没勾图片就不带图片）",
          [block["type"] for block in payload["contextBlocks"]] == ["text"])
    check("预览把长度与截断说清楚", preview["previewChars"] == len(preview["preview"]) and preview["truncated"] is False)

    reg = fresh_snapshot()
    long_draft, _c, _m = reg.capture(text="长" * (snap.MAX_UPLINK_TEXT_CHARS + 500))
    long_preview = reg.preview(long_draft.draft_id)[0]
    check("超上限的正文在预览里就标出被截断", long_preview["truncated"] is True and long_preview["previewChars"] == snap.MAX_UPLINK_TEXT_CHARS)
    long_payload = reg.confirm(long_draft.draft_id)[0]
    check("截断后的预览与截断后的上送仍然相等", long_preview["preview"] == long_payload["contextBlocks"][0]["text"])

    missing = reg.preview("does-not-exist")
    check("预览不存在的草稿回 404 与 SNAPSHOT_NOT_FOUND", missing[1] == snap.CODE_NOT_FOUND and missing[3] == 404, missing[1])

    pend_preview = reg.preview(long_draft.draft_id)[0]
    check("预览里公开了存活分钟数与「正文不落盘」的代价",
          pend_preview["ttlMinutes"] == reg.ttl_minutes and "重启" in pend_preview["storageNote"])


def uplink_gate_checks() -> None:
    section("5. 机密级不上送（硬闸）· internal 要显式勾选 · 脱敏档位影响载荷")
    reg = fresh_snapshot()
    confidential, _c, _m = reg.capture(text="这份合同里的身份证 110101199003077757 请核对")
    check("机密正文的预览标出「不会上送」", reg.preview(confidential.draft_id)[0]["wouldUplink"] is False)
    row, code, message, status = reg.confirm(confidential.draft_id)
    check("确认机密快照被拒（403 + 点名没有开关可绕）",
          row is None and code == snap.CODE_CONFIDENTIAL and status == 403 and "没有开关可以绕过" in message, message)
    again = reg.confirm(confidential.draft_id)
    check("重复确认同一句拒绝（不因重试就放行）", again[1] == snap.CODE_CONFIDENTIAL)
    check("机密草稿勾选图片与「我知道」也照样拦（勾选不是绕过口）",
          reg.confirm(confidential.draft_id, include_image=True, acknowledge_internal=True)[1] == snap.CODE_CONFIDENTIAL)

    reg = fresh_snapshot()
    internal, _c, _m = reg.capture(text="内部讨论纪要：本周排期")
    check("internal 的预览提示需要显式勾选", reg.preview(internal.draft_id)[0]["needsAck"] is True)
    blocked = reg.confirm(internal.draft_id)
    check("没勾选时 internal 被「要一次确认」拦下（是确认不是禁止）",
          blocked[1] == snap.CODE_INTERNAL_NEEDS_ACK and "确认而不是禁止" in blocked[2], blocked[2])
    ok_internal = reg.confirm(internal.draft_id, acknowledge_internal=True)
    check("显式勾选后 internal 可以上送", ok_internal[0] is not None and ok_internal[1] == "")
    check("internal 不要求本地通道（载荷里 forceLocal 为假）", ok_internal[0]["forceLocal"] is False)
    check("拦下与放行两句话不同（不许共用文案）", blocked[2] != (ok_internal[2] or "放行"))

    reg = fresh_snapshot()
    pii_confidential, _c, _m = reg.capture(text="手机 13800001234 的客户", privacy_runtime={"privacy": {"mode": "mask"}})
    check("PII 命中即机密：即使档位是脱敏，也先被机密闸拦下而不是脱敏后放行",
          pii_confidential.classification.sensitivity == snap.SENSITIVITY_CONFIDENTIAL
          and reg.confirm(pii_confidential.draft_id)[1] == snap.CODE_CONFIDENTIAL)

    plain, _c, _m = reg.capture(text="今天调了三个页面的间距", privacy_runtime={"privacy": {"mode": "mask"}})
    preview_plain = reg.preview(plain.draft_id)[0]
    payload_plain = reg.confirm(plain.draft_id)[0]
    check("无敏感命中的正文在 mask 档位下照样上送", payload_plain is not None)
    check("mask 档位的预览与上送仍相等", preview_plain["preview"] == payload_plain["contextBlocks"][0]["text"])
    check("预览回话里带出脱敏档位读数（界面不必自己猜）", preview_plain["redactionMode"] == "mask")

    secret_only, _c, _m = reg.capture(text="贴一段配置 sk-abcdefghijklmnop1234", privacy_runtime={"privacy": {"mode": "block"}})
    check("明文密钥走的是机密那一句（两族闸不重复拦同一件事）",
          reg.confirm(secret_only.draft_id)[1] == snap.CODE_CONFIDENTIAL)

    blocked_mode, _c, _m = reg.capture(text="内部标注 + 手机 13800001234", privacy_runtime={"privacy": {"mode": "block"}})
    check("档位 block 且命中 PII 的机密件仍按机密拒（判定顺序稳定）",
          reg.confirm(blocked_mode.draft_id)[1] == snap.CODE_CONFIDENTIAL)
    check("机密那一句与「需要显式勾选」那一句不是同一句",
          reg.confirm(blocked_mode.draft_id)[2] != blocked[2])

    reg = fresh_snapshot()
    phone, _c, _m = reg.capture(text="联系电话见下 13800001234")
    force = reg.confirm(phone.draft_id)
    check("纯 PII 件被判机密，载荷这一路根本走不到 forceLocal（如实记录）",
          force[1] == snap.CODE_CONFIDENTIAL and phone.classification.force_local is True)
    check("状态回话里点名「上送只产出 contextBlocks，通道选择不经过本模块」",
          any("contextBlocks" in item for item in reg.status()["unverified"]), str(reg.status()["unverified"])[:160])


def lifecycle_checks() -> None:
    section("6. 状态机：过期 / 取消 / 重复确认 / 超上限")
    clock = {"now": 5000.0}
    reg = fresh_snapshot(clock=lambda: clock["now"], ttl_minutes=1)
    draft, _c, _m = reg.capture(text="待确认的一份")
    clock["now"] += 61.0
    expired = reg.confirm(draft.draft_id)
    check("超过存活期后确认被拒（410 + 要求重新触发）",
          expired[1] == snap.CODE_EXPIRED and expired[3] == 410 and "重新触发" in expired[2], expired[2])
    check("过期后正文已销毁（草稿里不再留屏幕内容）", draft.text == "" and draft.image_bytes == b"")
    check("过期后预览同样回 410", reg.preview(draft.draft_id)[1] == snap.CODE_EXPIRED)

    clock = {"now": 5000.0}
    reg = fresh_snapshot(clock=lambda: clock["now"], ttl_minutes=1)
    pending, _c, _m = reg.capture(text="还没决定要不要给")
    check("未确认的草稿可以取消", reg.cancel(pending.draft_id)[1] == "")
    again = reg.cancel(pending.draft_id)
    check("取消第二次回「没有这份草稿」而不是静默成功", again[1] == snap.CODE_NOT_FOUND and again[3] == 404, again[1])
    revived = reg.confirm(pending.draft_id)
    check("取消之后确认不会把它捞回来", revived[1] == snap.CODE_NOT_FOUND, revived[1])

    reg = fresh_snapshot()
    keep, _c, _m = reg.capture(text="可以上送的一份")
    first = reg.confirm(keep.draft_id)[0]
    second = reg.confirm(keep.draft_id)[0]
    check("重复确认返回同一份载荷且标出 repeatConfirm（不二次上送）",
          first["contextBlocks"] == second["contextBlocks"] and second["repeatConfirm"] is True)
    check("确认后的草稿状态是 confirmed 而不是继续 pending", keep.status == snap.STATUS_CONFIRMED)

    reg = fresh_snapshot()
    ids = []
    for index in range(snap.MAX_PENDING_DRAFTS + 3):
        made, _c, _m = reg.capture(text=f"第 {index} 份")
        if made is not None:
            ids.append(made.draft_id)
    statuses = [reg.get(item).status for item in ids]
    check("待确认份数有上限，超出按最旧的过期处理",
          statuses.count(snap.STATUS_PENDING) == snap.MAX_PENDING_DRAFTS, str(statuses))
    check("被顶掉的那份正文已销毁且状态是 expired", reg.get(ids[0]).status == snap.STATUS_EXPIRED and reg.get(ids[0]).text == "")


# ── 5. 不落盘与审计 ─────────────────────────────────────────────


def audit_checks() -> None:
    section("7. 正文只留内存；审计只有元数据")
    reg = fresh_snapshot()
    marker = "绝不要出现在磁盘上的句子"
    secret_value = "sk-abcdefghijklmnop1234"
    draft, _c, _m = reg.capture(text=marker + " 与 " + secret_value, app_label="记事本", image_b64="aGk=", image_mime="image/png")
    reg.preview(draft.draft_id)
    reg.confirm(draft.draft_id, include_image=False)
    reg.cancel(draft.draft_id)

    files = sorted((Path(os.environ["TONGTIANWORK_HOME"])).rglob("*"))
    dumped = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in files
        if path.is_file() and path.suffix in {".jsonl", ".json"}
    )
    check("确实落了决策审计文件", any(path.name == snap.DECISIONS_FILENAME for path in files), str([p.name for p in files])[:160])
    check("审计里不含采集正文（那句标记不该出现在任何数据文件里）", marker not in dumped)
    check("审计里不含明文密钥原值", secret_value not in dumped)
    check("审计里不含图片 base64", "aGk=" not in dumped)

    rows = reg.decisions()["items"]
    actions = [row["action"] for row in rows]
    check("含明文密钥的那份走的是「拦下」，账上是 withheld 而不是 confirm_uplink（不粉饰）",
          "withheld" in actions and "confirm_uplink" not in actions, str(actions))

    # 动作齐全要用一份真正能上送的正经草稿来验：上面那份是机密件，永远走不到 confirm_uplink
    reg_flow = fresh_snapshot()
    plain, _c, _m = reg_flow.capture(text="今天调了三个页面的间距", app_label="编辑器")
    reg_flow.preview(plain.draft_id)
    reg_flow.confirm(plain.draft_id)
    reg_flow.cancel(plain.draft_id)
    flow_rows = reg_flow.decisions()["items"]
    flow_actions = [row["action"] for row in flow_rows]
    check("触发/确认/取消三类都在账上", {"capture", "confirm_uplink", "retire"} <= set(flow_actions), str(flow_actions))
    check("每条审计都带分级与命中模式名（够归因，不够还原正文）",
          all({"sensitivity", "hitPatterns", "textFingerprint", "textBytes"} <= set(row) for row in flow_rows))
    check("写条目记字节数与指纹而不是正文",
          all(isinstance(row["textBytes"], int) and len(row["textFingerprint"]) == 16 for row in flow_rows))
    retired = [row for row in flow_rows if row["action"] == "retire"][-1]
    captured = [row for row in flow_rows if row["action"] == "capture"][0]
    check("销毁后的审计仍能指回被销毁的那份内容（先清正文再记账会把归因写成 0 字节）",
          retired["destroyedTextBytes"] == captured["textBytes"] == len("今天调了三个页面的间距".encode("utf-8"))
          and retired["destroyedTextFingerprint"] == captured["textFingerprint"]
          and retired["textBytes"] == 0, str(retired)[:200])
    check("销毁条目记了图片字节数归零（本次没图）", retired["destroyedImageBytes"] == 0)

    reg2 = fresh_snapshot()
    page = reg2.decisions(limit=2, offset=0)
    check("空库的分页回话说清「这一页没有条目」", page["total"] == 0 and page["note"] != "")
    for index in range(4):
        reg2.capture(text=f"第 {index} 份正文")
    paged = reg2.decisions(limit=2, offset=0)
    check("审计分页给 total/hasMore", paged["total"] >= 4 and paged["hasMore"] is True and len(paged["items"]) == 2)


# ── 6. 接力边界 ─────────────────────────────────────────────────


def relay_checks() -> None:
    section("8. 接力边界：受控会话内才谈复用，四类拒绝各说一句")
    reg = fresh_relay()
    runtime = controlled_runtime()

    daily = {"browserUse": {"running": True, "policy": "auto", "profileDir": "C:/Users/dev/AppData/Local/Google/Chrome/User Data/Default"}}
    decision, _row = reg.grant(runtime=daily, workspace_id="ws-a", hosts=["example.com"])
    check("指向日常浏览器目录的授权开不出来（点名「不碰用户日常浏览器的 Cookie」）",
          decision.allowed is False and decision.code == relay.CODE_DAILY_PROFILE
          and "不碰用户日常浏览器的" in decision.reason, decision.reason)
    check("日常目录这一句归「受控浏览器边界」这一来源", decision.source == relay.SOURCE_CONTROLLED_BOUNDARY)

    no_session = reg.grant(runtime={}, workspace_id="ws-a", hosts=["example.com"])
    check("没有受控会话时授权不成立（来源是会话，不是边界也不是档位）",
          no_session[0].allowed is False and no_session[0].code == relay.CODE_NO_SESSION
          and no_session[0].source == relay.SOURCE_SESSION, no_session[0].reason)

    disabled = reg.grant(runtime=controlled_runtime(policy="disabled"), workspace_id="ws-a", hosts=["example.com"])
    check("浏览器档位禁用时授权不成立（来源是档位，另一句话）",
          disabled[0].allowed is False and disabled[0].code == relay.CODE_POLICY_DISABLED
          and disabled[0].source == relay.SOURCE_BROWSER_POLICY)

    no_hosts = reg.grant(runtime=runtime, workspace_id="ws-a", hosts=[])
    check("不给域名的授权被拒（不给域就等于不授权）",
          no_hosts[0].allowed is False and no_hosts[0].code == relay.CODE_NOT_GRANTED)

    bad_host = reg.grant(runtime=runtime, workspace_id="ws-a", hosts=["javascript:alert(1)"])
    check("授权项里的非法协议被 URL 白名单拒（复用 browser_use 那一份）",
          bad_host[0].allowed is False and bad_host[0].code == relay.CODE_BAD_URL, bad_host[0].reason)

    granted, row = reg.grant(runtime=runtime, workspace_id="ws-a", hosts=["example.com", "https://shop.test/cart?x=1"], task_id="t-1")
    check("正常授权成功并只留域名（URL 的查询串不进授权）",
          granted.allowed and sorted(row["hosts"]) == ["example.com", "shop.test"], str(row["hosts"]))
    row_dump = json.dumps(row, ensure_ascii=False)
    check("授权回话里没有 Cookie、没有完整路径（只给配置目录指纹）",
          "profileDir" not in row and "C:/Users/dev/AppData" not in row_dump
          and not any(key.lower().startswith("cookie") for key in row)
          and row["profileFingerprint"] != "", str(list(row))[:160])
    check("note 里提「Cookie」是说「不记它」，不是真带了 Cookie",
          "Cookie" in row["note"] and "cookie" not in row_dump.replace(row["note"], ""))

    other_ws = reg.decide(url="https://example.com/page", grant_id=granted.grant_id, workspace_id="ws-b", runtime=runtime)
    check("跨工作区不复用（这一句点名要重新授权、不静默放大）",
          other_ws.allowed is False and other_ws.code == relay.CODE_WORKSPACE_MISMATCH
          and "重新" in other_ws.reason or "不做静默放大" in other_ws.reason, other_ws.reason)
    other_host = reg.decide(url="https://other.test/", grant_id=granted.grant_id, workspace_id="ws-a", runtime=runtime)
    check("不在授权域集合里被拒（第三句）",
          other_host.allowed is False and other_host.code == relay.CODE_HOST_NOT_GRANTED)
    unsafe = reg.decide(url="file:///C:/secrets", grant_id=granted.grant_id, workspace_id="ws-a", runtime=runtime)
    check("file 协议在判定这一层也被拒（同一份白名单）", unsafe.allowed is False and unsafe.code == relay.CODE_BAD_URL)
    unknown = reg.decide(url="https://example.com", grant_id="nope", workspace_id="ws-a", runtime=runtime)
    check("拿不存在的授权 id 来问被拒，且不因为「以前授权过」放行",
          unknown.allowed is False and unknown.code == relay.CODE_GRANT_NOT_FOUND)

    allowed = reg.decide(url="https://example.com/page", grant_id=granted.grant_id, workspace_id="ws-a", runtime=runtime)
    check("同工作区同域 + 会话在线 ⇒ 放行", allowed.allowed is True, allowed.reason)
    login = reg.decide(url="https://shop.test/account", grant_id=granted.grant_id, workspace_id="ws-a", runtime=runtime, want="login")
    check("登录态复用按同一份授权判定（不另记凭据）", login.allowed is True)

    offline = reg.decide(url="https://example.com", grant_id=granted.grant_id, workspace_id="ws-a",
                         runtime=controlled_runtime(running=False))
    check("受控浏览器不在运行时被拒，且说明「授权本身仍然有效」",
          offline.allowed is False and offline.code == relay.CODE_NO_SESSION and "授权本身仍然有效" in offline.reason, offline.reason)
    moved = reg.decide(url="https://example.com", grant_id=granted.grant_id, workspace_id="ws-a",
                       runtime=controlled_runtime(profileDir="C:/Users/dev/other/profile"))
    check("本轮会话的目录与被授权那份对不上 ⇒ 拒（授权不跟着新目录走）",
          moved.allowed is False and moved.code == relay.CODE_PROFILE_UNKNOWN)
    policy_off = reg.decide(url="https://example.com", grant_id=granted.grant_id, workspace_id="ws-a",
                           runtime=controlled_runtime(policy="disabled"))
    check("授权成立但档位后来被关 ⇒ 按档位那一句拒",
          policy_off.allowed is False and policy_off.code == relay.CODE_POLICY_DISABLED)

    sentences = {row_item.reason for row_item in (other_ws, other_host, unsafe, unknown, offline, moved, policy_off, decision, no_session[0], disabled[0])}
    check("十类拒绝是十句不同的话（不许合并文案）", len(sentences) == 10, f"{len(sentences)} 句")
    labels = {relay.SOURCE_LABELS[item.source] for item in (other_ws, unsafe, offline, moved, policy_off, decision, no_session[0], disabled[0])}
    check("四个来源标签互不相同（界面按来源选文案，四类不许并成三类）", len(labels) == 4, str(labels))
    check("开授权时被档位拒与判定时被档位拒是两句话（同一 code，不同处境）",
          disabled[0].reason != policy_off.reason and disabled[0].code == policy_off.code)
    check("放行这一条不带来源标签（deniedByLabel 只在拒时有值）",
          granted.source == "" and granted.to_dict()["deniedByLabel"] == "")

    revoked, _ = reg.revoke(granted.grant_id, reason="用户点了「退出接力」")
    check("撤销成功且回话说「立即生效，不看剩余有效期」",
          revoked.allowed and "立即生效" in revoked.reason, revoked.reason)
    after_revoke = reg.decide(url="https://example.com", grant_id=granted.grant_id, workspace_id="ws-a", runtime=runtime)
    check("撤销后再问被拒（第四类：授权已撤销）", after_revoke.allowed is False and after_revoke.code == relay.CODE_GRANT_REVOKED)
    missing_revoke = reg.revoke("nope")
    check("撤销不存在的授权不回成功", missing_revoke[0].allowed is False and missing_revoke[0].code == relay.CODE_GRANT_NOT_FOUND)

    clock = {"now": 100.0}
    timed = fresh_relay(clock=lambda: clock["now"], ttl_minutes=2)
    granted2, _row2 = timed.grant(runtime=controlled_runtime(), workspace_id="ws-c", hosts=["a.test"])
    clock["now"] += 121.0
    expired = timed.decide(url="https://a.test", grant_id=granted2.grant_id, workspace_id="ws-c", runtime=controlled_runtime())
    check("超过存活期的授权按过期拒（第五句，与撤销不同）",
          expired.allowed is False and expired.code == relay.CODE_GRANT_EXPIRED)
    check("过期这一句与撤销这一句不是同一句", expired.reason != after_revoke.reason)


def relay_tabs_checks() -> None:
    section("9. 标签页登记：只登记，不枚举；登记也按授权走")
    reg = fresh_relay()
    granted, _row = reg.grant(runtime=controlled_runtime(), workspace_id="ws-t", hosts=["example.com"])
    ok, row = reg.register_tabs(granted.grant_id, [
        {"tabId": "1", "url": "https://example.com/a?token=secret-token", "title": "示例页", "active": True},
        {"tabId": "2", "url": "https://example.com/b", "title": "长" * 200},
    ])
    check("合法登记成功并留下活动标签", ok.allowed and row["tabCount"] == 2 and row["tabs"][0]["active"] is True)
    dumped = json.dumps(row, ensure_ascii=False)
    check("登记不留 URL 查询串（令牌不进接力现场）", "secret-token" not in dumped and "?token=" not in dumped)
    check("标题按长度截断（不把整页文案抄进清单）", len(row["tabs"][1]["title"]) <= relay.MAX_TAB_TITLE_CHARS)
    off_scope, _ = reg.register_tabs(granted.grant_id, [{"tabId": "3", "url": "https://other.test/x"}])
    check("登记越出授权域被拒（不然登记本身就成越界）",
          off_scope.allowed is False and off_scope.code == relay.CODE_HOST_NOT_GRANTED)
    bad_url, _ = reg.register_tabs(granted.grant_id, [{"tabId": "4", "url": "javascript:alert(1)"}])
    check("登记里的非法协议被拒", bad_url.allowed is False and bad_url.code == relay.CODE_BAD_URL)
    overflow, _ = reg.register_tabs(granted.grant_id, [{"tabId": str(i), "url": "https://example.com"} for i in range(relay.MAX_TABS_PER_GRANT + 1)])
    check("标签页数量超上限被拒（不是截断后回成功）", overflow.allowed is False and overflow.code == relay.CODE_TABS_OVERFLOW)
    ghost, _ = reg.register_tabs("nope", [{"tabId": "5", "url": "https://example.com"}])
    check("没有授权就无处登记", ghost.allowed is False and ghost.code == relay.CODE_GRANT_NOT_FOUND)
    check("非字典条目被静默丢掉而不是当成一个空标签",
          reg.register_tabs(granted.grant_id, ["不是对象", {"tabId": "6", "url": "https://example.com"}])[0].allowed)

    decided = reg.decide(url="https://example.com/a", grant_id=granted.grant_id, workspace_id="ws-t",
                         runtime=controlled_runtime())
    check("登记过的标签页所在域可复用（判定与登记一致）", decided.allowed is True)
    revoker = reg.revoke_workspace("ws-t", reason="按工作区整体撤销")
    check("按工作区整体撤销能覆盖这个工作区的授权", granted.grant_id in revoker)
    check("整体撤销后再判定按撤销拒",
          reg.decide(url="https://example.com", grant_id=granted.grant_id, workspace_id="ws-t", runtime=controlled_runtime()).code == relay.CODE_GRANT_REVOKED)

    audit = reg.decisions()["items"]
    blob = json.dumps(audit, ensure_ascii=False)
    check("接力审计不含 Cookie / 令牌 / 完整配置目录路径",
          all(needle not in blob for needle in ("cookie", "secret-token", "AppData")))
    check("审计里的路径只有指纹", all(len(item.get("profileFingerprint") or "") in (0, 12) for item in audit))


# ── 10. 状态回话与未验收 ────────────────────────────────────────


def surface_checks() -> None:
    section("10. 状态回话：口径、上限与未验收都点名")
    reg = fresh_snapshot()
    fresh_relay()
    status = reg.status()
    check("状态给出三档分级与五种状态", len(status["sensitivityLevels"]) == 3 and len(status["statuses"]) == 5)
    check("状态点名「本模块不截屏 ⇒ 真机采集未验收」",
          any("不截屏" in item and "未验收" in item for item in status["unverified"]), str(status["unverified"])[:120])
    check("状态点名「图片无法本地检测」这条边界",
          any("无法本地检测" in item for item in status["unverified"]), str(status["unverified"])[:160])
    check("状态说明预览与上送由同一个函数产出", "同一个函数" in status["note"])
    check("结论码清单不含后端那族的码（两族不混）",
          not any(code.startswith("WS_") for code in status["codes"]))

    relay_status = relay.REGISTRY.status()
    check("接力状态点名浏览器动作归 W5", "/api/browser" in relay_status["note"])
    check("接力状态给出四个拒绝来源", len(relay_status["denialSources"]) == 4)
    def _keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return {str(key) for key in node} | set().union(*(_keys(value) for value in node.values())) if node else set()
        if isinstance(node, list):
            return set().union(*(_keys(value) for value in node)) if node else set()
        return set()

    relay_keys = _keys(relay_status)
    check("接力状态不含 Cookie/令牌字段（说明句里提「Cookie」是讲不记它，字段才是事实）",
          not any(any(needle in key.lower() for needle in ("cookie", "token", "password", "credential"))
                  for key in relay_keys), str(sorted(relay_keys))[:200])
    check("接力状态不含完整配置目录路径",
          "AppData" not in json.dumps(relay_status, ensure_ascii=False))
    check("接力未验收点名「真实受控浏览器会话未验收」",
          any("未验收" in item and "受控浏览器" in item for item in relay_status["unverified"]))

    source = (PROJECT_ROOT / "engine" / "snapshot_runtime.py").read_text(encoding="utf-8")
    check("快照模块不 import server（车道口径：现场经 routes_lanes.engine() 取）", "import server" not in source)
    check("快照模块不自己发 HTTP（不截屏也不联网）",
          not any(needle in source for needle in ("urllib.request", "aiohttp", "socket.")))
    check("接力模块不发起 CDP（没有 WebSocket 连接件）",
          "websocket" not in (PROJECT_ROOT / "engine" / "snapshot_relay.py").read_text(encoding="utf-8").lower())
    def audit_slice(text: str) -> str:
        """只看 `_record` 这一段：整模块里出现 "text": 是上送载荷的 contextBlocks，不是审计。"""
        start = text.index("def _record(")
        rest = text[start + len("def _record("):]
        end = rest.index("\n    def ")
        return text[start : start + len("def _record(") + end]

    snap_audit = audit_slice(source)
    relay_audit = audit_slice((PROJECT_ROOT / "engine" / "snapshot_relay.py").read_text(encoding="utf-8"))
    check("两个模块的审计函数里都没有正文键（只有字节数与指纹）",
          '"text":' not in snap_audit and '"body":' not in snap_audit and '"text":' not in relay_audit
          and "textBytes" in snap_audit and "textFingerprint" in snap_audit)
    check("审计函数里明确写了「不含正文」这条口径", "正文" in snap_audit)


def main() -> int:
    print("=== 应用快照与浏览器上下文接力自测（离线 · 假现场 · 零真机）===")
    classification_parity_checks()
    secret_divergence_checks()
    capture_checks()
    preview_checks()
    uplink_gate_checks()
    lifecycle_checks()
    audit_checks()
    relay_checks()
    relay_tabs_checks()
    surface_checks()
    print("\n=== 汇总 ===")
    if FAILURES:
        print(f"[FAIL] {len(FAILURES)} 项未过：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("[PASS] ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
