"""管理操作审计：只记动作与对象，敏感字段在写入前脱敏，并维护哈希链。

审计是给运营看「谁在什么时候改了什么」，不是凭据仓库：secret/token/password
一律替换成 `***`，请求体超过 4KB 截断并标注，避免把厂商密钥写进库。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .audit_chain import GENESIS, digest
from .db import now_ms

SENSITIVE_KEYS = frozenset(
    {
        "secret",
        "token",
        "password",
        "apikey",
        "api_key",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "admin_token",
        "service_token",
        "master_key",
    }
)
MAX_PAYLOAD_BYTES = 4096
MAX_LIMIT = 1000


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {
            key: (
                "***"
                if str(key).strip().lower() in SENSITIVE_KEYS
                else redact(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value[:200]]
    return value


def _payload_json(payload: Any) -> str:
    text = json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return text
    return json.dumps(
        {"truncated": True, "bytes": len(text.encode("utf-8"))},
        ensure_ascii=False,
    )


def write_audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    object: str,
    payload: Any = None,
    status: int = 200,
    actor_role: str = "admin",
    before: Any = None,
    after: Any = None,
) -> None:
    before_json = _payload_json(before if before is not None else {})
    after_json = _payload_json(after if after is not None else (payload or {}))
    row = conn.execute(
        "SELECT entry_hash FROM admin_audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = str(row["entry_hash"] or GENESIS) if row and row["entry_hash"] else GENESIS
    at = now_ms()
    value = digest(
        prev,
        at,
        (actor or "unknown").strip()[:120],
        actor_role or "admin",
        action.strip()[:32],
        object.strip()[:300],
        before_json,
        after_json,
    )
    conn.execute(
        "INSERT INTO admin_audit (at_ms, actor_uid, actor_role, action, object, "
        "payload_json, before_json, after_json, status, prev_hash, entry_hash) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        (
            at,
            (actor or "unknown").strip()[:120],
            (actor_role or "admin").strip()[:32],
            action.strip()[:32],
            object.strip()[:300],
            _payload_json(payload or {}),
            before_json,
            after_json,
            int(status),
            prev,
            value,
        ),
    )


def _entry(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "atMs": int(row["at_ms"]),
        "actor": row["actor_uid"],
        "actorRole": row["actor_role"],
        "action": row["action"],
        "object": row["object"],
        "status": int(row["status"]),
        "payload": json.loads(row["payload_json"] or "{}"),
        "before": json.loads(row["before_json"] or "{}"),
        "after": json.loads(row["after_json"] or "{}"),
        "prevHash": row["prev_hash"],
        "entryHash": row["entry_hash"],
    }


def _head(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT entry_hash FROM admin_audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return str(row["entry_hash"] or GENESIS) if row else GENESIS


def _legacy_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM admin_audit WHERE entry_hash = ''"
    ).fetchone()
    return int(row["n"]) if row else 0


def list_entries(
    conn: sqlite3.Connection, *, after: int = 0, limit: int = 100
) -> dict:
    size = max(1, min(MAX_LIMIT, int(limit)))
    rows = conn.execute(
        "SELECT * FROM admin_audit WHERE id > ? ORDER BY id ASC LIMIT ?",
        (max(0, int(after)), size),
    ).fetchall()
    entries = [_entry(row) for row in rows]
    return {
        "entries": entries,
        "nextAfter": entries[-1]["id"] if entries else max(0, int(after)),
        "headHash": _head(conn),
        "legacyRows": _legacy_count(conn),
    }


BREAK_REASONS = {
    "missing_hash": "本行没有链摘要（存量行未回填）",
    "prev_mismatch": "前驱摘要对不上：本行之前有行被删除、插入或整段重算",
    "entry_mismatch": "本行内容与自身摘要对不上：字段被改动",
}
MAX_BREAKS_REPORTED = 20


def verify_report(
    conn: sqlite3.Connection, *, up_to_id: int | None = None, scan_limit: int = 20000
) -> dict:
    """全量走一遍链并列出所有断点，不是停在第一个就返回。

    两类断点分开判：`entry_mismatch` 说明这一行的正文被改；`prev_mismatch` 说明
    它前面的行被删/插/整体重算。行被删后又把后续行重算成一条自洽新链时，逐行判定
    全都「正常」——那种情形只有签名锚点（`audit_anchor`）能抓，所以这里同时给出
    `firstId/lastId/entries/contiguous` 供锚点比对。
    """
    if up_to_id is None:
        rows = conn.execute(
            "SELECT * FROM admin_audit ORDER BY id ASC LIMIT ?1", (int(scan_limit),)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM admin_audit").fetchone()
    else:
        rows = conn.execute(
            "SELECT * FROM admin_audit WHERE id <= ?1 ORDER BY id ASC LIMIT ?2",
            (int(up_to_id), int(scan_limit)),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM admin_audit WHERE id <= ?1", (int(up_to_id),)
        ).fetchone()
    truncated = int(total["n"]) > int(scan_limit)
    prev = GENESIS
    breaks: list[dict] = []
    legacy = 0
    for row in rows:
        stored = str(row["entry_hash"] or "")
        declared_prev = str(row["prev_hash"] or "")
        row_id = int(row["id"])
        if not stored:
            legacy += 1
            if len(breaks) < MAX_BREAKS_REPORTED:
                breaks.append({"id": row_id, "reason": "missing_hash"})
            continue
        if declared_prev != prev:
            if len(breaks) < MAX_BREAKS_REPORTED:
                breaks.append({"id": row_id, "reason": "prev_mismatch"})
        expected = digest(
            declared_prev,
            int(row["at_ms"]),
            str(row["actor_uid"]),
            str(row["actor_role"]),
            str(row["action"]),
            str(row["object"]),
            str(row["before_json"] or "{}"),
            str(row["after_json"] or "{}"),
        )
        if expected != stored:
            if len(breaks) < MAX_BREAKS_REPORTED:
                breaks.append({"id": row_id, "reason": "entry_mismatch"})
        # 摘要指针按「库里存的那条」前进：伪造者重算后指针会跟着他的走，
        # 这样后面每一行仍能被单独判成「正文对不上」，而不是一路连锁报错。
        prev = stored
    ids = [int(row["id"]) for row in rows]
    contiguous = bool(ids) and ids[-1] - ids[0] + 1 == len(ids)
    return {
        "entries": len(rows),
        "firstId": ids[0] if ids else 0,
        "lastId": ids[-1] if ids else 0,
        "contiguous": contiguous,
        "headHash": prev,
        "brokenAt": breaks[0]["id"] if breaks else None,
        "breaks": breaks,
        "breakCount": len(breaks),
        "legacyRows": legacy,
        "scanLimit": int(scan_limit),
        "truncated": bool(truncated),
        "ok": not breaks and not truncated,
    }


def verify_prefix(conn: sqlite3.Connection, anchor: dict) -> tuple[bool, str]:
    """按锚点重放前缀：条目数、id 区间、链头三者任一不符即判定不可信。"""
    report = verify_report(conn, up_to_id=int(anchor["toId"]))
    if int(anchor["fromId"]) and report["firstId"] != int(anchor["fromId"]):
        return False, "首行编号与锚点不符：锚点之后的行被删除或重编号"
    if report["entries"] != int(anchor["entries"]):
        return False, (
            f"条目数与锚点不符：锚点记 {int(anchor['entries'])} 条，"
            f"当前前缀 {report['entries']} 条"
        )
    # 行号有缺口不单独判成「锚点不可信」：删行一定会让条目数或链头先对不上，
    # 而缺口本身是链的状态（锚点签发时就在），由合规报告的 ID_GAP 一条如实点名。
    if report["breaks"]:
        first = report["breaks"][0]
        return False, f"前缀链有断点（行 {first['id']}：{BREAK_REASONS[first['reason']]}）"
    if report["headHash"] != str(anchor["headHash"]):
        return False, "链头与锚点不符：整段链被重算过"
    return True, "可信"



def export_ndjson(
    conn: sqlite3.Connection, *, after: int = 0, limit: int = 1000
) -> tuple[str, dict]:
    page = list_entries(conn, after=after, limit=limit)
    text = "".join(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        for entry in page["entries"]
    )
    return text, page


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?1",
        (max(1, min(500, limit)),),
    ).fetchall()
    return [_entry(row) for row in rows]
