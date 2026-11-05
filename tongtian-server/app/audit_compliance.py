"""合规报告：把审计链与锚点的真实状态汇成一份可导出的周期报告（缺陷 D-27）。

这份报告只回答三件事：这段时间里管理面改了什么、链条可不可信、哪些事情本控制面
**证明不了**。第三件事必须写进来——高安全客户看的不是绿灯，而是绿灯的边界：
未配置远端归档时，同时持有数据库文件和签名私钥的人可以把链和锚点一起重造，
本机没有任何证据能反驳他；引擎与桌面的本地审计文件也不在这条链里。

只读接口，查询本身不写审计（与既有的审计查询口径一致）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from .audit import BREAK_REASONS, MAX_PAYLOAD_BYTES, SENSITIVE_KEYS, verify_report
from .audit_anchor import MAX_ANCHORS_PAGE, configured as archive_configured, verify_all
from .auth import require_gateway_staff
from .config import settings
from .db import now_ms, tx
from .errors import ApiError

router = APIRouter()

REPORT_CONTRACT_VERSION = "audit-compliance.v1"
DEFAULT_PERIOD_DAYS = 30
MIN_PERIOD_DAYS = 1
MAX_PERIOD_DAYS = 366
MAX_BREAKS_SHOWN = 10
TOP_ACTIONS = 20
TOP_ACTORS = 20
RECENT_SENSITIVE = 50

# 管理面里「改动了权力或钱」的动作。未列出的动作按常规活动统计，不进敏感清单。
SENSITIVE_ACTIONS = {
    "user.role": "账号管理角色变更",
    "gateway.admin": "网关管理面写操作",
    "model_tier.publish": "模型档位表发布",
    "model_tier.revert": "模型档位表回滚",
    "model_tier.withdraw": "模型档位表撤回",
    "audit.anchor": "审计锚点签发",
    "audit.archive": "审计锚点远端归档",
}


def _days(value: int) -> int:
    if value < MIN_PERIOD_DAYS or value > MAX_PERIOD_DAYS:
        raise ApiError(
            400,
            "INVALID_REQUEST",
            f"days 必须在 {MIN_PERIOD_DAYS}–{MAX_PERIOD_DAYS} 之间",
        )
    return int(value)


def _period(days: int) -> tuple[int, int]:
    size = _days(days)
    to_ms = now_ms()
    return to_ms - size * 86400_000, to_ms


def _activity(conn: sqlite3.Connection, from_ms: int, to_ms: int) -> dict[str, Any]:
    total = conn.execute(
        "SELECT COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id FROM admin_audit "
        "WHERE at_ms >= ?1 AND at_ms <= ?2",
        (from_ms, to_ms),
    ).fetchone()
    actions = conn.execute(
        "SELECT action, COUNT(*) AS n FROM admin_audit "
        "WHERE at_ms >= ?1 AND at_ms <= ?2 GROUP BY action ORDER BY n DESC, action ASC "
        "LIMIT ?3",
        (from_ms, to_ms, TOP_ACTIONS),
    ).fetchall()
    actors = conn.execute(
        "SELECT actor_uid, actor_role, COUNT(*) AS n FROM admin_audit "
        "WHERE at_ms >= ?1 AND at_ms <= ?2 GROUP BY actor_uid, actor_role "
        "ORDER BY n DESC, actor_uid ASC LIMIT ?3",
        (from_ms, to_ms, TOP_ACTORS),
    ).fetchall()
    days = conn.execute(
        "SELECT strftime('%Y-%m-%d', at_ms / 1000, 'unixepoch') AS day, COUNT(*) AS n "
        "FROM admin_audit WHERE at_ms >= ?1 AND at_ms <= ?2 GROUP BY day ORDER BY day ASC",
        (from_ms, to_ms),
    ).fetchall()
    failures = conn.execute(
        "SELECT COUNT(*) AS n FROM admin_audit "
        "WHERE at_ms >= ?1 AND at_ms <= ?2 AND status >= 400",
        (from_ms, to_ms),
    ).fetchone()
    entries = int(total["n"] or 0)
    id_span = 0
    if entries:
        id_span = int(total["last_id"]) - int(total["first_id"]) + 1
    return {
        "entries": entries,
        "firstId": int(total["first_id"] or 0),
        "lastId": int(total["last_id"] or 0),
        # 期间内行数 != id 跨度 ⇒ 期间里有行被删除（哈希链会断，这里给出同一事实的两个视角）
        "idSpanCoversPeriod": bool(entries) and id_span == entries,
        "nonOkRows": int(failures["n"] or 0),
        "byAction": [{"action": str(r["action"]), "count": int(r["n"])} for r in actions],
        "byActor": [
            {
                "actor": str(r["actor_uid"]),
                "role": str(r["actor_role"]),
                "count": int(r["n"]),
            }
            for r in actors
        ],
        "byDay": [{"day": str(r["day"]), "count": int(r["n"])} for r in days],
        "dayBasis": "UTC",
    }


def _sensitive(conn: sqlite3.Connection, from_ms: int, to_ms: int) -> dict[str, Any]:
    names = tuple(SENSITIVE_ACTIONS)
    # 动态 IN 列表用 `?3…?N` 显式编号：混用裸 `?` 会与 ?1/?2 的编号抢同一个槽位
    placeholders = ",".join(f"?{index}" for index in range(3, 3 + len(names)))
    limit_slot = 3 + len(names)
    window = f"WHERE at_ms >= ?1 AND at_ms <= ?2 AND action IN ({placeholders}) "
    rows = conn.execute(
        f"SELECT action, COUNT(*) AS n FROM admin_audit {window}"
        "GROUP BY action ORDER BY n DESC, action ASC",
        (from_ms, to_ms, *names),
    ).fetchall()
    recent = conn.execute(
        f"SELECT id, at_ms, actor_uid, actor_role, action, object, status, entry_hash "
        f"FROM admin_audit {window}ORDER BY id DESC LIMIT ?{limit_slot}",
        (from_ms, to_ms, *names, RECENT_SENSITIVE),
    ).fetchall()
    return {
        "byAction": [
            {
                "action": str(r["action"]),
                "label": SENSITIVE_ACTIONS.get(str(r["action"]), ""),
                "count": int(r["n"]),
            }
            for r in rows
        ],
        "recent": [
            {
                "id": int(r["id"]),
                "atMs": int(r["at_ms"]),
                "actor": str(r["actor_uid"]),
                "role": str(r["actor_role"]),
                "action": str(r["action"]),
                "object": str(r["object"]),
                "status": int(r["status"]),
                "entryHash": str(r["entry_hash"]),
            }
            for r in recent
        ],
        "note": "敏感清单按动作名归类；正文里的密钥类字段在写入前已替换成 ***",
    }


def _anchor_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """锚点的「可信」按当前链判定：签名可信但链头对不上同样算不可信。"""
    checked = verify_all(conn, limit=MAX_ANCHORS_PAGE)
    total = conn.execute("SELECT COUNT(*) AS n FROM audit_anchors").fetchone()
    archive = conn.execute(
        "SELECT archive_state, COUNT(*) AS n FROM audit_anchors GROUP BY archive_state"
    ).fetchall()
    newest = checked["items"][0] if checked["items"] else {}
    latest = {key: newest[key] for key in _LATEST_KEYS if key in newest} if newest else {}
    mismatches = [
        {"seq": item["seq"], "toId": item["toId"], "reason": item["reason"]}
        for item in checked["items"]
        if not item["chainTrusted"]
    ][:MAX_BREAKS_SHOWN]
    return {
        "anchors": int(total["n"] or 0),
        # 巡检只看最近 MAX_ANCHORS_PAGE 个；库里更多时如实标出这个口径差
        "checked": checked["anchors"],
        "untrusted": checked["untrusted"],
        "mismatches": mismatches,
        "statement": checked["statement"],
        "latest": latest,
        "archiveByState": {str(r["archive_state"]): int(r["n"]) for r in archive},
    }


_LATEST_KEYS = (
    "seq",
    "atMs",
    "fromId",
    "toId",
    "entries",
    "headHash",
    "signingKid",
    "actor",
    "signatureTrusted",
    "chainTrusted",
    "reason",
    "archiveState",
    "archiveRef",
    "archiveError",
)


def _findings(
    chain: dict[str, Any], anchors: dict[str, Any], archive: dict[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if chain["truncated"]:
        findings.append(
            {
                "level": "error",
                "code": "SCAN_TRUNCATED",
                "detail": (
                    f"审计行数超过单次巡检上限 {chain['scanLimit']}，"
                    "本次只检查了前缀，结论不覆盖全链"
                ),
            }
        )
    if chain["legacyRows"]:
        findings.append(
            {
                "level": "error",
                "code": "LEGACY_ROWS",
                "detail": f"{chain['legacyRows']} 行没有链摘要（存量未回填）",
            }
        )
    if chain["entries"] and not chain["contiguous"]:
        findings.append(
            {
                "level": "error",
                "code": "ID_GAP",
                "detail": (
                    f"行号 {chain['firstId']}—{chain['lastId']} 区间里有 "
                    f"{chain['lastId'] - chain['firstId'] + 1 - chain['entries']} 个缺口："
                    "有审计行被删除（追加式审计不应出现缺口）"
                ),
            }
        )
    for break_item in chain["breaks"][:MAX_BREAKS_SHOWN]:
        findings.append(
            {
                "level": "error",
                "code": "CHAIN_BREAK",
                "detail": (
                    f"行 {break_item['id']}：{BREAK_REASONS.get(break_item['reason'], '未知原因')}"
                ),
                "rowId": int(break_item["id"]),
                "reason": str(break_item["reason"]),
            }
        )
    if anchors["anchors"] == 0:
        findings.append(
            {
                "level": "warn",
                "code": "NO_ANCHOR",
                "detail": "还没有签名锚点：无法证明历史链未被整体重算",
            }
        )
    for item in anchors["mismatches"]:
        findings.append(
            {
                "level": "error",
                "code": "ANCHOR_MISMATCH",
                "detail": f"锚点 #{item['seq']}（覆盖到行 {item['toId']}）与当前链不一致：{item['reason']}",
                "anchorSeq": int(item["seq"]),
            }
        )
    if int(anchors["anchors"]) > int(anchors["checked"]):
        findings.append(
            {
                "level": "warn",
                "code": "ANCHORS_NOT_FULLY_CHECKED",
                "detail": (
                    f"库里有 {anchors['anchors']} 个锚点，本次只比对最近 {anchors['checked']} 个，"
                    "更早的锚点未纳入结论"
                ),
            }
        )
    if not archive["enabled"]:
        findings.append(
            {
                "level": "warn",
                "code": "NO_REMOTE_ARCHIVE",
                "detail": (
                    "未配置 TT_AUDIT_ARCHIVE_URL：锚点只存在本机，"
                    "同时持有数据库文件与签名私钥的人可以连锚点一起重造"
                ),
            }
        )
    remote = anchors["archiveByState"].get("remote", 0)
    if archive["enabled"] and anchors["anchors"] and remote == 0:
        findings.append(
            {
                "level": "warn",
                "code": "NOT_ARCHIVED",
                "detail": "已配置远端归档，但还没有任何锚点成功推出去",
            }
        )
    failed = anchors["archiveByState"].get("failed", 0)
    if failed:
        findings.append(
            {
                "level": "warn",
                "code": "ARCHIVE_FAILED_ROWS",
                "detail": f"{failed} 个锚点的远端归档处于失败状态，原因记在锚点行里",
            }
        )
    skipped = anchors["archiveByState"].get("skipped", 0)
    if skipped:
        findings.append(
            {
                "level": "warn",
                "code": "ARCHIVE_SKIPPED",
                "detail": (
                    f"{skipped} 个锚点因覆盖行数超过单次推送上限而没有推出去，"
                    "这些锚点目前没有外部存证"
                ),
            }
        )
    return findings


def _coverage() -> dict[str, Any]:
    """本报告的边界：写清楚哪些东西不在这条链的保护范围内。"""
    return {
        "scope": "tongtian-server 控制面管理审计（admin_audit）",
        "notCovered": [
            "引擎与桌面的本地审计文件：不在控制面库里，本表不声称覆盖",
            "业务用量与账务：见网关用量接口，不属于防篡改链",
            "密钥轮换的历史：由签名 kid 与 JWKS 体现，本表只引用不校验",
        ],
        "redaction": {
            "appliedOnWrite": True,
            "keys": sorted(SENSITIVE_KEYS),
            "maxPayloadBytes": MAX_PAYLOAD_BYTES,
        },
        "usageRetentionDays": int(settings.gateway_usage_retention_days),
    }


def build_report(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    from_ms, to_ms = _period(days)
    chain = verify_report(conn)
    summary = {
        "entries": chain["entries"],
        "firstId": chain["firstId"],
        "lastId": chain["lastId"],
        "contiguous": chain["contiguous"],
        "headHash": chain["headHash"],
        "brokenAt": chain["brokenAt"],
        "breakCount": chain["breakCount"],
        "breaks": chain["breaks"][:MAX_BREAKS_SHOWN],
        "legacyRows": chain["legacyRows"],
        "scanLimit": chain["scanLimit"],
        "truncated": chain["truncated"],
        "ok": chain["ok"],
    }
    anchors = _anchor_state(conn)
    archive = archive_configured()
    findings = _findings(summary, anchors, archive)
    errors = [item for item in findings if item["level"] == "error"]
    period_days = int(days)
    return {
        "contractVersion": REPORT_CONTRACT_VERSION,
        "generatedAtMs": to_ms,
        "period": {
            "days": period_days,
            "fromMs": from_ms,
            "toMs": to_ms,
            "basis": "at_ms（写入时刻，UTC 分桶）",
        },
        "verdict": {
            # trustworthy 只在「链自洽 + 有锚点 + 锚点签名可信」三者同时成立时为真。
            "trustworthy": bool(summary["ok"] and anchors["anchors"] and not anchors["untrusted"]),
            "chainOk": bool(summary["ok"]),
            "errorCount": len(errors),
            "warnCount": len(findings) - len(errors),
            "statement": _statement(summary, anchors, archive, errors),
        },
        "integrity": summary,
        "anchors": anchors,
        "archive": archive,
        "activity": _activity(conn, from_ms, to_ms),
        "sensitive": _sensitive(conn, from_ms, to_ms),
        "coverage": _coverage(),
        "findings": findings,
    }


def _statement(
    chain: dict[str, Any], anchors: dict[str, Any], archive: dict[str, Any], errors: list
) -> str:
    if errors:
        return f"本次巡检发现 {len(errors)} 项错误级问题，链条不可信，先处理再看统计"
    if not chain["ok"]:
        return "链有断点，暂不出具可信结论"
    if not anchors["anchors"]:
        return "链自洽但没有锚点：只能证明行与行相连，不能证明整段未被重算"
    where = "已推送到远端归档" if archive["enabled"] else "仅存在本机"
    return f"链自洽，{anchors['checked']} 个锚点全部与当前链一致（{where}）"


def _cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(report: dict[str, Any]) -> str:
    period = report["period"]
    verdict = report["verdict"]
    chain = report["integrity"]
    anchors = report["anchors"]
    activity = report["activity"]
    sensitive = report["sensitive"]
    coverage = report["coverage"]
    lines: list[str] = []
    push = lines.append

    push("# 管理审计合规报告")
    push("")
    push(f"- 报告口径：`{report['contractVersion']}`")
    push(f"- 生成时刻：{_format_ms(period['toMs'])}")
    push(f"- 覆盖区间：{_format_ms(period['fromMs'])} — {_format_ms(period['toMs'])}"
         f"（{period['days']} 天，按 {period['basis']}）")
    push(f"- 结论：**{verdict['statement']}**")
    push("")

    push("## 一、链条完整性")
    push("")
    push("| 指标 | 值 |")
    push("| --- | --- |")
    push(f"| 全链行数（本次巡检） | {chain['entries']} |")
    push(f"| 行号区间 | {chain['firstId']} — {chain['lastId']} |")
    push(f"| 行号连续 | {'是' if chain['contiguous'] else '否（中间有行被删除）'} |")
    push(f"| 链头摘要 | `{chain['headHash']}` |")
    push(f"| 断点数量 | {chain['breakCount']} |")
    push(f"| 未回填摘要的存量行 | {chain['legacyRows']} |")
    push(f"| 是否超出单次巡检上限 | {'是' if chain['truncated'] else '否'}（上限 {chain['scanLimit']}） |")
    push("")
    if chain["breaks"]:
        push("断点明细（最多列 " + str(MAX_BREAKS_SHOWN) + " 条）：")
        push("")
        push("| 行号 | 原因 |")
        push("| --- | --- |")
        for item in chain["breaks"]:
            push(f"| {item['id']} | {_cell(BREAK_REASONS.get(item['reason'], '未知原因'))} |")
        push("")

    push("## 二、签名锚点与归档")
    push("")
    push(f"- 本次比对最近 {anchors['checked']} 个锚点（库内共 {anchors['anchors']} 个），"
         f"与当前链不一致 {anchors['untrusted']} 个")
    latest = anchors["latest"]
    if latest:
        push(f"- 最新锚点：#{latest['seq']}，覆盖行 {latest['fromId']} — {latest['toId']}"
             f"（{latest['entries']} 条），签名{'可信' if latest['signatureTrusted'] else '不可信'}，"
             f"与当前链{'一致' if latest['chainTrusted'] else '不一致'}")
        if not latest["chainTrusted"]:
            push(f"- 不一致原因：{_cell(latest['reason'])}")
        push(f"- 最新锚点链头：`{latest['headHash']}`")
        push(f"- 最新锚点归档：{latest['archiveState']}"
             + (f"，引用 {latest['archiveRef']}" if latest["archiveRef"] else "")
             + (f"，失败原因 {latest['archiveError']}" if latest["archiveError"] else ""))
    else:
        push("- 最新锚点：无")
    push(f"- 远端归档：{'已配置' if report['archive']['enabled'] else '未配置（只出本地锚点）'}")
    push(f"- 归档状态分布：{_cell(json.dumps(anchors['archiveByState'], ensure_ascii=False))}")
    push("")

    push("## 三、期内活动")
    push("")
    push(f"- 审计行数：{activity['entries']}（行号 {activity['firstId']} — {activity['lastId']}）")
    push(f"- 区间行号连续：{'是' if activity['idSpanCoversPeriod'] else '否或期间无记录'}")
    push(f"- 非 2xx 记录：{activity['nonOkRows']}")
    push("")
    push("### 按动作")
    push("")
    push("| 动作 | 次数 |")
    push("| --- | --- |")
    if activity["byAction"]:
        for item in activity["byAction"]:
            push(f"| {_cell(item['action'])} | {item['count']} |")
    else:
        push("| （期间无管理审计记录） | 0 |")
    push("")
    push("### 按操作人")
    push("")
    push("| 操作人 | 角色 | 次数 |")
    push("| --- | --- | --- |")
    if activity["byActor"]:
        for item in activity["byActor"]:
            push(f"| {_cell(item['actor'])} | {_cell(item['role'])} | {item['count']} |")
    else:
        push("| （无） | （无） | 0 |")
    push("")

    push("## 四、敏感动作")
    push("")
    push(sensitive["note"])
    push("")
    push("| 动作 | 说明 | 次数 |")
    push("| --- | --- | --- |")
    if sensitive["byAction"]:
        for item in sensitive["byAction"]:
            push(f"| {_cell(item['action'])} | {_cell(item['label'])} | {item['count']} |")
    else:
        push("| （无） | （期间没有敏感动作记录） | 0 |")
    push("")

    push("## 五、问题清单")
    push("")
    if report["findings"]:
        push("| 级别 | 编码 | 说明 |")
        push("| --- | --- | --- |")
        for item in report["findings"]:
            push(f"| {item['level']} | {item['code']} | {_cell(item['detail'])} |")
    else:
        push("本次巡检没有发现问题。")
    push("")

    push("## 六、本报告证明不了什么")
    push("")
    push(f"- 范围：{coverage['scope']}")
    for item in coverage["notCovered"]:
        push(f"- 不覆盖：{item}")
    push("- 未配置远端归档时，锚点与链同库存放，无法抵御「连锚点一起重造」；"
         "配了远端归档，也只有状态为 remote 的锚点算外部证据。")
    push(f"- 正文脱敏在写入前完成（密钥类字段替换为 `***`，单条超过 "
         f"{coverage['redaction']['maxPayloadBytes']} 字节截断），"
         "因此报告无法还原被脱敏的内容。")
    push("")
    push("—— 报告结束 ——")
    push("")
    return "\n".join(lines)


def _format_ms(value: int) -> str:
    moment = datetime.datetime.fromtimestamp(int(value) / 1000, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/api/v1/admin/audit/compliance")
def api_audit_compliance(
    days: int = Query(DEFAULT_PERIOD_DAYS),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        report = build_report(conn, int(days))
    return {"ok": True, **report}


@router.get("/api/v1/admin/audit/compliance/export")
def api_audit_compliance_export(
    days: int = Query(DEFAULT_PERIOD_DAYS),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        report = build_report(conn, int(days))
    text = to_markdown(report)
    stamp = _format_ms(report["period"]["toMs"]).replace(":", "-").replace("Z", "")
    filename = f"audit-compliance-{stamp}-{int(days)}d.md"
    return Response(
        content=text.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-TT-Audit-Head": str(report["integrity"]["headHash"]),
            "X-TT-Audit-Trustworthy": "true" if report["verdict"]["trustworthy"] else "false",
        },
    )
