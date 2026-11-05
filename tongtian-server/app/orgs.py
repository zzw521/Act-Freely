"""组织与部门查询：成员关系按 (uid, org_id, dept_id) 唯一。"""

from __future__ import annotations

import sqlite3

from .db import DEFAULT_DEPT_ID, DEFAULT_DEPT_NAME, now_ms


def ensure_org(conn: sqlite3.Connection, org_id: str, name: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO orgs (id, name, created_at_ms) VALUES (?1, ?2, ?3)",
        (org_id, name.strip() or org_id, now_ms()),
    )


def ensure_department(
    conn: sqlite3.Connection, org_id: str, dept_id: str, name: str = ""
) -> None:
    ensure_org(conn, org_id)
    conn.execute(
        "INSERT OR IGNORE INTO departments (org_id, id, name, created_at_ms) "
        "VALUES (?1, ?2, ?3, ?4)",
        (org_id, dept_id, name.strip() or dept_id, now_ms()),
    )


def ensure_default_department(conn: sqlite3.Connection, org_id: str) -> None:
    ensure_department(conn, org_id, DEFAULT_DEPT_ID, DEFAULT_DEPT_NAME)


def department_ids(conn: sqlite3.Connection, org_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT id FROM departments WHERE org_id = ?1", (org_id,)
    ).fetchall()
    return {str(row["id"]) for row in rows}


def memberships(conn: sqlite3.Connection, uid: str, org_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT d.id AS id, d.name AS name, ud.is_primary AS is_primary "
        "FROM user_departments ud JOIN departments d "
        "ON d.org_id = ud.org_id AND d.id = ud.dept_id "
        "WHERE ud.uid = ?1 AND ud.org_id = ?2 "
        "ORDER BY ud.is_primary DESC, d.name ASC, d.id ASC",
        (uid, org_id),
    ).fetchall()
    return [
        {"id": str(row["id"]), "name": str(row["name"]), "primary": bool(row["is_primary"])}
        for row in rows
    ]


def department_set(conn: sqlite3.Connection, uid: str, org_id: str) -> list[str]:
    return [item["id"] for item in memberships(conn, uid, org_id)]


def primary_department(conn: sqlite3.Connection, uid: str, org_id: str) -> str:
    row = conn.execute(
        "SELECT dept_id FROM user_departments WHERE uid = ?1 AND org_id = ?2 "
        "ORDER BY is_primary DESC, created_at_ms ASC, dept_id ASC LIMIT 1",
        (uid, org_id),
    ).fetchone()
    return str(row["dept_id"]) if row else ""


def replace_memberships(
    conn: sqlite3.Connection, uid: str, org_id: str, dept_ids: list[str], primary: str
) -> None:
    conn.execute(
        "DELETE FROM user_departments WHERE uid = ?1 AND org_id = ?2", (uid, org_id)
    )
    stamp = now_ms()
    for dept_id in dept_ids:
        conn.execute(
            "INSERT INTO user_departments (uid, org_id, dept_id, is_primary, created_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5)",
            (uid, org_id, dept_id, 1 if dept_id == primary else 0, stamp),
        )
