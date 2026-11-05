"""审计哈希链：与存储解耦的规范化、摘要与存量回填。

`entry_hash = sha256(prev_hash + "\\n" + at_ms + "\\n" + actor + "\\n" +
actor_role + "\\n" + action + "\\n" + object + "\\n" + before_json + "\\n" +
after_json)`；首条的 `prev_hash` 为 64 个 `0`。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

GENESIS = "0" * 64


def canonical(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(
    prev_hash: str,
    at_ms: int,
    actor: str,
    actor_role: str,
    action: str,
    object_id: str,
    before_json: str,
    after_json: str,
) -> str:
    material = "\n".join(
        (
            prev_hash,
            str(int(at_ms)),
            actor,
            actor_role,
            action,
            object_id,
            before_json,
            after_json,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def backfill(conn: sqlite3.Connection) -> int:
    """按 id 顺序为存量行补 actor_role 与前/后哈希；只处理缺哈希的行。"""
    rows = conn.execute(
        "SELECT id, at_ms, actor, action, object, before_json, after_json, "
        "actor_role, prev_hash, entry_hash FROM audit_log ORDER BY id"
    ).fetchall()
    prev = GENESIS
    filled = 0
    for row in rows:
        entry_hash = str(row["entry_hash"] or "")
        if entry_hash:
            prev = entry_hash
            continue
        role = str(row["actor_role"] or "") or "admin"
        value = digest(
            prev,
            int(row["at_ms"]),
            str(row["actor"]),
            role,
            str(row["action"]),
            str(row["object"]),
            str(row["before_json"] or "{}"),
            str(row["after_json"] or "{}"),
        )
        conn.execute(
            "UPDATE audit_log SET actor_role=?, prev_hash=?, entry_hash=? WHERE id=?",
            (role, prev, value, int(row["id"])),
        )
        prev = value
        filled += 1
    return filled
