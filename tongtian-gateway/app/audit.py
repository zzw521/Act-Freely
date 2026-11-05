"""网关审计：追加带哈希链的条目，并提供读取、验链与 NDJSON 导出。"""

from __future__ import annotations

import json
from typing import Any

from .audit_chain import GENESIS, canonical, digest
from .audit_outbox import enqueue
from .config import settings
from .db import Connection, Row, lock_audit_chain, now_ms

MAX_LIMIT = 1000


def append(
    conn: Connection,
    *,
    actor: str,
    actor_role: str,
    action: str,
    object_id: str,
    before: Any = None,
    after: Any = None,
) -> dict:
    lock_audit_chain(conn)
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = str(row["entry_hash"] or GENESIS) if row else GENESIS
    before_json = canonical(before)
    after_json = canonical(after)
    at = now_ms()
    value = digest(
        prev, at, actor, actor_role, action, object_id, before_json, after_json
    )
    inserted = conn.execute(
        "INSERT INTO audit_log(at_ms, actor, actor_role, action, object, "
        "before_json, after_json, prev_hash, entry_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (at, actor, actor_role, action, object_id, before_json, after_json, prev, value),
    ).fetchone()
    entry = {
        "id": int(inserted["id"]),
        "atMs": at,
        "actor": actor,
        "actorRole": actor_role,
        "action": action,
        "object": object_id,
        "before": json.loads(before_json),
        "after": json.loads(after_json),
        "prevHash": prev,
        "entryHash": value,
    }
    if settings.audit_webhook_url:
        enqueue(conn, entry)
    return entry


def _entry(row: Row) -> dict:
    return {
        "id": int(row["id"]),
        "atMs": int(row["at_ms"]),
        "actor": row["actor"],
        "actorRole": row["actor_role"],
        "action": row["action"],
        "object": row["object"],
        "before": json.loads(row["before_json"] or "{}"),
        "after": json.loads(row["after_json"] or "{}"),
        "prevHash": row["prev_hash"],
        "entryHash": row["entry_hash"],
    }


def _head(conn: Connection) -> str:
    row = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return str(row["entry_hash"] or GENESIS) if row else GENESIS


def _legacy_count(conn: Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE entry_hash = ''"
    ).fetchone()
    return int(row["n"]) if row else 0


def list_entries(
    conn: Connection, *, after: int = 0, limit: int = 100
) -> dict:
    size = max(1, min(MAX_LIMIT, int(limit)))
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE id > ? ORDER BY id ASC LIMIT ?",
        (max(0, int(after)), size),
    ).fetchall()
    entries = [_entry(row) for row in rows]
    return {
        "entries": entries,
        "nextAfter": entries[-1]["id"] if entries else max(0, int(after)),
        "headHash": _head(conn),
        "legacyRows": _legacy_count(conn),
    }


def verify(conn: Connection) -> dict:
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id ASC"
    ).fetchall()
    prev = GENESIS
    broken_at = None
    legacy = 0
    for row in rows:
        entry_hash = str(row["entry_hash"] or "")
        if not entry_hash:
            legacy += 1
            broken_at = int(row["id"])
            break
        if str(row["prev_hash"] or "") != prev:
            broken_at = int(row["id"])
            break
        expected = digest(
            prev,
            int(row["at_ms"]),
            str(row["actor"]),
            str(row["actor_role"]),
            str(row["action"]),
            str(row["object"]),
            str(row["before_json"] or "{}"),
            str(row["after_json"] or "{}"),
        )
        if expected != entry_hash:
            broken_at = int(row["id"])
            break
        prev = entry_hash
    return {
        "entries": len(rows),
        "headHash": prev,
        "brokenAt": broken_at,
        "legacyRows": legacy,
    }


def export_ndjson(
    conn: Connection, *, after: int = 0, limit: int = 1000
) -> tuple[str, dict]:
    page = list_entries(conn, after=after, limit=limit)
    text = "".join(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        for entry in page["entries"]
    )
    return text, page
