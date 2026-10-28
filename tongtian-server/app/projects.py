"""项目协作：项目、成员与角色、邀请、共享任务、评论、结果回报与快照同步。

共享边界：只同步项目元信息、任务标题/说明/状态/负责人、评论、结果摘要与产物文件名。
不同步对话正文、产物文件内容、技能、模型凭据、运行状态与检查点；每台机器用自己的
账号、模型与工作目录执行，回报结果由负责人本人在其桌面端触发。

协作是「人工触发的跨机交接」，不是自动 A2A 中继：任务派给成员后，需要该成员在
自己的桌面端点击「在本机执行」，本地 Agent 完成后才把摘要与产物文件名回传。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .auth import require_user
from .db import now_ms, tx
from .errors import ApiError
from .security import EMAIL_RE, RateLimiter, token_hash

router = APIRouter()

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_VIEWER = "viewer"
ROLE_RANK = {ROLE_VIEWER: 1, ROLE_MEMBER: 2, ROLE_ADMIN: 3, ROLE_OWNER: 4}
INVITABLE_ROLES = {ROLE_ADMIN, ROLE_MEMBER, ROLE_VIEWER}

TASK_STATUSES = {"todo", "doing", "review", "done"}
RESULT_STATUSES = {"review", "done"}

MAX_NAME = 60
MAX_DESCRIPTION = 2000
MAX_TASK_TITLE = 120
MAX_TASK_DETAIL = 4000
MAX_COMMENT = 2000
MAX_RESULT_SUMMARY = 4000
MAX_ARTIFACTS = 20
MAX_ARTIFACT_NAME = 200
MAX_MACHINE = 60
INVITE_TTL_MS = 7 * 24 * 3600 * 1000
MAX_INVITE_TOKEN = 256
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 事件流：单次补拉上限与轮询节奏。协作验收要求 1 秒内可见，500ms 轮询
# 留出一半余量；心跳 15 秒，避免反代把空闲连接当作僵死连接掐断。
EVENT_PAGE_LIMIT = 500
EVENT_DEFAULT_LIMIT = 100
EVENT_STREAM_BATCH = 200
EVENT_STREAM_POLL_SECONDS = 0.5
EVENT_STREAM_PING_SECONDS = 15.0

_limits = RateLimiter()


# ── 入参 ──────────────────────────────────────────────────────


class CamelIn(BaseModel):
    """JSON 入参用 camelCase；同时接受 snake_case，便于脚本与测试调用。"""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class ProjectCreateIn(CamelIn):
    name: str
    description: str = ""


class InviteIn(CamelIn):
    email: str
    role: str = ROLE_MEMBER


class AcceptInviteIn(CamelIn):
    token: str


class MemberRoleIn(CamelIn):
    role: str


class TransferIn(CamelIn):
    uid: str


class TaskCreateIn(CamelIn):
    title: str
    detail: str = ""
    assignee_uid: str | None = None


class TaskPatchIn(CamelIn):
    version: int
    title: str | None = None
    detail: str | None = None
    status: str | None = None
    assignee_uid: str | None = None


class CommentIn(CamelIn):
    body: str
    task_id: str | None = None


class ResultIn(CamelIn):
    version: int
    summary: str
    artifacts: list[str] = []
    machine: str = ""
    status: str = "review"


# ── 清洗与权限 ────────────────────────────────────────────────


def _clean_text(raw: Any, field: str, limit: int, *, required: bool = False) -> str:
    value = str(raw or "").strip()
    if required and not value:
        raise ApiError(400, "PROJECT_INVALID_INPUT", f"{field}不能为空")
    if len(value) > limit:
        raise ApiError(400, "PROJECT_INVALID_INPUT", f"{field}不能超过 {limit} 个字符")
    if CONTROL_RE.search(value):
        raise ApiError(400, "PROJECT_INVALID_INPUT", f"{field}含不可见控制字符")
    return value


def _clean_single_line(raw: Any, field: str, limit: int, *, required: bool = False) -> str:
    value = _clean_text(raw, field, limit, required=required)
    return " ".join(value.split())


def _clean_email(raw: Any) -> str:
    email = str(raw or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ApiError(400, "PROJECT_INVALID_INPUT", "邀请邮箱格式不正确")
    return email


def _check_project_id(raw: str) -> str:
    project_id = str(raw or "").strip()
    if not re.fullmatch(r"prj-[0-9a-f]{16}", project_id):
        raise ApiError(404, "PROJECT_NOT_FOUND", "项目不存在或你不是成员")
    return project_id


def _check_task_id(raw: str) -> str:
    task_id = str(raw or "").strip()
    if not re.fullmatch(r"pt-[0-9a-f]{16}", task_id):
        raise ApiError(404, "PROJECT_TASK_NOT_FOUND", "任务不存在")
    return task_id


def _clean_artifacts(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ARTIFACTS:
        raise ApiError(400, "PROJECT_INVALID_INPUT", f"产物文件名不能超过 {MAX_ARTIFACTS} 个")
    out: list[str] = []
    for item in raw:
        name = str(item or "").replace("\\", "/").split("/")[-1].strip()
        if not name or name in (".", ".."):
            continue
        if len(name) > MAX_ARTIFACT_NAME or CONTROL_RE.search(name):
            raise ApiError(400, "PROJECT_INVALID_INPUT", "产物文件名不合法")
        if name not in out:
            out.append(name)
    return out


def _require_project(
    conn: sqlite3.Connection, project_id: str, uid: str, minimum: str = ROLE_VIEWER
) -> sqlite3.Row:
    project_id = _check_project_id(project_id)
    row = conn.execute(
        _summary_query("WHERE p.id = ?2"),
        (uid, project_id),
    ).fetchone()
    # 非成员统一按不存在处理，避免探测项目 id 是否存在
    if row is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", "项目不存在或你不是成员")
    if ROLE_RANK.get(row["my_role"], 0) < ROLE_RANK[minimum]:
        raise ApiError(403, "PROJECT_FORBIDDEN", "当前项目角色没有该操作权限")
    return row


def _active_member(conn: sqlite3.Connection, project_id: str, uid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT pm.*, u.nickname FROM project_members pm JOIN users u ON u.uid = pm.uid "
        "WHERE pm.project_id = ?1 AND pm.uid = ?2 AND pm.removed = 0",
        (project_id, uid),
    ).fetchone()


def _emit(
    conn: sqlite3.Connection,
    project_id: str,
    actor_uid: str,
    kind: str,
    payload: dict[str, Any],
) -> int:
    ts = now_ms()
    seq = conn.execute(
        "INSERT INTO project_events (project_id, actor_uid, kind, payload_json, created_at_ms) "
        "VALUES (?1, ?2, ?3, ?4, ?5)",
        (project_id, actor_uid, kind, json.dumps(payload, ensure_ascii=False), ts),
    ).lastrowid
    conn.execute(
        "UPDATE projects SET updated_at_ms = ?1 WHERE id = ?2",
        (ts, project_id),
    )
    return int(seq)


def _event_view(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "actorUid": row["actor_uid"],
        "payload": payload,
        "createdAtMs": row["created_at_ms"],
    }


def _event_rows(
    conn: sqlite3.Connection, project_id: str, since: int, limit: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT seq, actor_uid, kind, payload_json, created_at_ms FROM project_events "
        "WHERE project_id = ?1 AND seq > ?2 ORDER BY seq ASC LIMIT ?3",
        (project_id, since, limit),
    ).fetchall()


def _read_events(
    project_id: str, uid: str, since: int, limit: int
) -> list[dict[str, Any]] | None:
    """读取游标之后的事件；返回 None 表示已不是项目成员，流应关闭。"""
    try:
        with tx() as conn:
            _require_project(conn, project_id, uid)
            return [_event_view(row) for row in _event_rows(conn, project_id, since, limit)]
    except ApiError:
        return None


# ── 视图 ──────────────────────────────────────────────────────


def _project_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "ownerUid": row["owner_uid"],
        "myRole": row["my_role"],
        "memberCount": row["member_count"],
        "taskCounts": {
            "todo": row["todo_count"],
            "doing": row["doing_count"],
            "review": row["review_count"],
            "done": row["done_count"],
        },
        "seq": row["seq"],
        "createdAtMs": row["created_at_ms"],
        "updatedAtMs": row["updated_at_ms"],
    }


def _member_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "uid": row["uid"],
        "nickname": row["nickname"],
        "role": row["role"],
        "joinedAtMs": row["joined_at_ms"],
    }


def _task_view(row: sqlite3.Row) -> dict[str, Any]:
    try:
        artifacts = json.loads(row["result_artifacts_json"] or "[]")
    except Exception:
        artifacts = []
    if not isinstance(artifacts, list):
        artifacts = []
    result = None
    if row["result_at_ms"]:
        result = {
            "summary": row["result_summary"],
            "artifacts": [str(item) for item in artifacts],
            "machine": row["result_machine"],
            "by": row["result_by"],
            "byNickname": row["result_nickname"] or "",
            "atMs": row["result_at_ms"],
        }
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "title": row["title"],
        "detail": row["detail"],
        "status": row["status"],
        "assignee": (
            {"uid": row["assignee_uid"], "nickname": row["assignee_nickname"] or ""}
            if row["assignee_uid"]
            else None
        ),
        "createdBy": {"uid": row["created_by"], "nickname": row["creator_nickname"] or ""},
        "version": row["version"],
        "result": result,
        "createdAtMs": row["created_at_ms"],
        "updatedAtMs": row["updated_at_ms"],
    }


def _comment_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "author": {"uid": row["author_uid"], "nickname": row["author_nickname"] or ""},
        "body": row["body"],
        "createdAtMs": row["created_at_ms"],
    }


def _summary_query(where: str) -> str:
    return (
        "SELECT p.*, pm.role AS my_role, "
        "(SELECT COUNT(*) FROM project_members m WHERE m.project_id = p.id AND m.removed = 0) "
        "AS member_count, "
        "(SELECT COALESCE(MAX(e.seq), 0) FROM project_events e WHERE e.project_id = p.id) AS seq, "
        "(SELECT COUNT(*) FROM project_tasks t WHERE t.project_id = p.id AND t.status = 'todo') "
        "AS todo_count, "
        "(SELECT COUNT(*) FROM project_tasks t WHERE t.project_id = p.id AND t.status = 'doing') "
        "AS doing_count, "
        "(SELECT COUNT(*) FROM project_tasks t WHERE t.project_id = p.id AND t.status = 'review') "
        "AS review_count, "
        "(SELECT COUNT(*) FROM project_tasks t WHERE t.project_id = p.id AND t.status = 'done') "
        "AS done_count "
        "FROM projects p JOIN project_members pm ON pm.project_id = p.id "
        "AND pm.uid = ?1 AND pm.removed = 0 " + where
    )


def _task_query(where: str) -> str:
    return (
        "SELECT t.*, ua.nickname AS assignee_nickname, uc.nickname AS creator_nickname, "
        "ur.nickname AS result_nickname FROM project_tasks t "
        "LEFT JOIN users ua ON ua.uid = t.assignee_uid "
        "LEFT JOIN users uc ON uc.uid = t.created_by "
        "LEFT JOIN users ur ON ur.uid = t.result_by " + where
    )


def _snapshot(conn: sqlite3.Connection, project_id: str, uid: str) -> dict[str, Any]:
    project = _require_project(conn, project_id, uid, ROLE_VIEWER)
    members = [
        _member_view(row)
        for row in conn.execute(
            "SELECT pm.*, u.nickname FROM project_members pm "
            "JOIN users u ON u.uid = pm.uid "
            "WHERE pm.project_id = ?1 AND pm.removed = 0 "
            "ORDER BY CASE pm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 "
            "WHEN 'member' THEN 2 ELSE 3 END, pm.joined_at_ms",
            (project["id"],),
        )
    ]
    tasks = [
        _task_view(row)
        for row in conn.execute(
            _task_query("WHERE t.project_id = ?1 ORDER BY t.created_at_ms ASC"),
            (project["id"],),
        )
    ]
    comments = [
        _comment_view(row)
        for row in conn.execute(
            "SELECT c.*, u.nickname AS author_nickname FROM project_comments c "
            "LEFT JOIN users u ON u.uid = c.author_uid "
            "WHERE c.project_id = ?1 ORDER BY c.created_at_ms ASC LIMIT 300",
            (project["id"],),
        )
    ]
    invites: list[dict[str, Any]] = []
    if ROLE_RANK[project["my_role"]] >= ROLE_RANK[ROLE_ADMIN]:
        invites = [
            _invite_view(row)
            for row in conn.execute(
                "SELECT i.*, u.nickname AS inviter_nickname FROM project_invites i "
                "LEFT JOIN users u ON u.uid = i.invited_by "
                "WHERE i.project_id = ?1 AND i.accepted_at_ms IS NULL AND i.expires_at_ms > ?2 "
                "ORDER BY i.created_at_ms DESC LIMIT 50",
                (project["id"], now_ms()),
            )
        ]
    return {
        "ok": True,
        "project": _project_view(project),
        "members": members,
        "tasks": tasks,
        "comments": comments,
        "invites": invites,
        "seq": project["seq"],
    }


def _invite_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "invitedBy": {"uid": row["invited_by"], "nickname": row["inviter_nickname"] or ""},
        "expiresAtMs": row["expires_at_ms"],
        "createdAtMs": row["created_at_ms"],
    }


# ── 项目 ──────────────────────────────────────────────────────


@router.get("/api/v1/projects")
def api_list_projects(user: dict = Depends(require_user)):
    with tx() as conn:
        rows = conn.execute(
            _summary_query("ORDER BY p.updated_at_ms DESC LIMIT 200"),
            (user["uid"],),
        ).fetchall()
    return {"ok": True, "projects": [_project_view(row) for row in rows]}


@router.post("/api/v1/projects", status_code=201)
def api_create_project(payload: ProjectCreateIn, user: dict = Depends(require_user)):
    allowed, retry = _limits.check(f"project-create:{user['uid']}", 20, 3600)
    if not allowed:
        raise ApiError(
            429,
            "RATE_LIMITED",
            f"创建项目过于频繁，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    name = _clean_single_line(payload.name, "项目名称", MAX_NAME, required=True)
    description = _clean_text(payload.description, "项目说明", MAX_DESCRIPTION)
    project_id = f"prj-{secrets.token_hex(8)}"
    ts = now_ms()
    with tx() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, description, owner_uid, created_at_ms, updated_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?5)",
            (project_id, name, description, user["uid"], ts),
        )
        conn.execute(
            "INSERT INTO project_members (project_id, uid, role, removed, joined_at_ms, updated_at_ms) "
            "VALUES (?1, ?2, 'owner', 0, ?3, ?3)",
            (project_id, user["uid"], ts),
        )
        _emit(conn, project_id, user["uid"], "project.created", {"projectId": project_id})
        return _snapshot(conn, project_id, user["uid"])


@router.get("/api/v1/projects/{project_id}")
def api_project_detail(project_id: str, user: dict = Depends(require_user)):
    with tx() as conn:
        return _snapshot(conn, project_id, user["uid"])


# ── 邀请与成员 ────────────────────────────────────────────────


@router.post("/api/v1/projects/{project_id}/invites", status_code=201)
def api_create_invite(
    project_id: str, payload: InviteIn, user: dict = Depends(require_user)
):
    allowed, retry = _limits.check(f"project-invite:{user['uid']}", 60, 3600)
    if not allowed:
        raise ApiError(
            429,
            "RATE_LIMITED",
            f"邀请过于频繁，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    email = _clean_email(payload.email)
    role = str(payload.role or "").strip().lower()
    if role not in INVITABLE_ROLES:
        raise ApiError(400, "PROJECT_INVALID_INPUT", "邀请角色只能是 admin / member / viewer")
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_ADMIN)
        if project["my_role"] != ROLE_OWNER and role == ROLE_ADMIN:
            raise ApiError(403, "PROJECT_FORBIDDEN", "只有项目所有者可以邀请管理员")
        existing = conn.execute("SELECT uid FROM users WHERE email = ?1", (email,)).fetchone()
        if existing is not None:
            member = _active_member(conn, project["id"], existing["uid"])
            if member is not None:
                raise ApiError(409, "PROJECT_ALREADY_MEMBER", "该邮箱已是项目成员")
        token = secrets.token_urlsafe(24)
        invite_id = f"pi-{secrets.token_hex(8)}"
        ts = now_ms()
        expires = ts + INVITE_TTL_MS
        conn.execute(
            "INSERT INTO project_invites "
            "(id, project_id, email, role, token_hash, invited_by, expires_at_ms, created_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            (invite_id, project["id"], email, role, token_hash(token), user["uid"], expires, ts),
        )
        _emit(
            conn,
            project["id"],
            user["uid"],
            "invite.created",
            {"inviteId": invite_id, "email": email, "role": role},
        )
        row = conn.execute(
            "SELECT i.*, u.nickname AS inviter_nickname FROM project_invites i "
            "LEFT JOIN users u ON u.uid = i.invited_by WHERE i.id = ?1",
            (invite_id,),
        ).fetchone()
        return {
            "ok": True,
            "invite": _invite_view(row),
            # 明文邀请码只在此次响应返回一次；服务端只存 SHA-256 摘要
            "inviteCode": token,
            "project": _project_view(project),
        }


@router.get("/api/v1/projects/{project_id}/invites")
def api_list_invites(project_id: str, user: dict = Depends(require_user)):
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_ADMIN)
        rows = conn.execute(
            "SELECT i.*, u.nickname AS inviter_nickname FROM project_invites i "
            "LEFT JOIN users u ON u.uid = i.invited_by "
            "WHERE i.project_id = ?1 AND i.accepted_at_ms IS NULL AND i.expires_at_ms > ?2 "
            "ORDER BY i.created_at_ms DESC LIMIT 100",
            (project["id"], now_ms()),
        ).fetchall()
    return {"ok": True, "invites": [_invite_view(row) for row in rows]}


@router.post("/api/v1/projects/invites/accept")
def api_accept_invite(payload: AcceptInviteIn, user: dict = Depends(require_user)):
    token = str(payload.token or "").strip()
    if not token or len(token) > MAX_INVITE_TOKEN or CONTROL_RE.search(token):
        raise ApiError(400, "PROJECT_INVITE_INVALID", "邀请码无效或已过期")
    now = now_ms()
    with tx() as conn:
        invite = conn.execute(
            "SELECT * FROM project_invites WHERE token_hash = ?1", (token_hash(token),)
        ).fetchone()
        if invite is None or invite["accepted_at_ms"] is not None or invite["expires_at_ms"] <= now:
            raise ApiError(400, "PROJECT_INVITE_INVALID", "邀请码无效或已过期")
        if str(user.get("email") or "").strip().lower() != invite["email"]:
            raise ApiError(
                403,
                "PROJECT_INVITE_EMAIL_MISMATCH",
                "该邀请码属于其他邮箱，请使用被邀请的账号登录",
            )
        cur = conn.execute(
            "UPDATE project_invites SET accepted_at_ms = ?1, accepted_by = ?2 "
            "WHERE id = ?3 AND accepted_at_ms IS NULL",
            (now, user["uid"], invite["id"]),
        )
        if cur.rowcount != 1:
            raise ApiError(400, "PROJECT_INVITE_INVALID", "邀请码已被使用")
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?1", (invite["project_id"],)
        ).fetchone()
        if project is None:
            raise ApiError(400, "PROJECT_INVITE_INVALID", "邀请码对应的项目已不存在")
        member = conn.execute(
            "SELECT * FROM project_members WHERE project_id = ?1 AND uid = ?2",
            (project["id"], user["uid"]),
        ).fetchone()
        if member is not None and not member["removed"]:
            raise ApiError(409, "PROJECT_ALREADY_MEMBER", "你已经是该项目的成员")
        if member is None:
            conn.execute(
                "INSERT INTO project_members "
                "(project_id, uid, role, removed, joined_at_ms, updated_at_ms) "
                "VALUES (?1, ?2, ?3, 0, ?4, ?4)",
                (project["id"], user["uid"], invite["role"], now),
            )
        else:
            conn.execute(
                "UPDATE project_members SET role = ?1, removed = 0, joined_at_ms = ?2, "
                "updated_at_ms = ?2 WHERE project_id = ?3 AND uid = ?4",
                (invite["role"], now, project["id"], user["uid"]),
            )
        _emit(
            conn,
            project["id"],
            user["uid"],
            "member.joined",
            {"uid": user["uid"], "nickname": user.get("nickname") or "", "role": invite["role"]},
        )
        return _snapshot(conn, project["id"], user["uid"])


@router.post("/api/v1/projects/{project_id}/members/{uid}/role")
def api_update_member_role(
    project_id: str, uid: str, payload: MemberRoleIn, user: dict = Depends(require_user)
):
    role = str(payload.role or "").strip().lower()
    if role not in INVITABLE_ROLES:
        raise ApiError(400, "PROJECT_INVALID_INPUT", "成员角色只能是 admin / member / viewer")
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_OWNER)
        target = _active_member(conn, project["id"], uid)
        if target is None:
            raise ApiError(404, "PROJECT_MEMBER_NOT_FOUND", "成员不存在")
        if target["role"] == ROLE_OWNER or uid == project["owner_uid"]:
            raise ApiError(400, "PROJECT_OWNER_IMMUTABLE", "项目所有者角色不能直接修改")
        conn.execute(
            "UPDATE project_members SET role = ?1, updated_at_ms = ?2 "
            "WHERE project_id = ?3 AND uid = ?4",
            (role, now_ms(), project["id"], uid),
        )
        _emit(
            conn,
            project["id"],
            user["uid"],
            "member.role_changed",
            {"uid": uid, "nickname": target["nickname"], "role": role},
        )
    return {"ok": True}


@router.post("/api/v1/projects/{project_id}/members/{uid}/remove")
def api_remove_member(project_id: str, uid: str, user: dict = Depends(require_user)):
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_ADMIN)
        target = _active_member(conn, project["id"], uid)
        if target is None:
            raise ApiError(404, "PROJECT_MEMBER_NOT_FOUND", "成员不存在")
        if target["role"] == ROLE_OWNER or uid == project["owner_uid"]:
            raise ApiError(400, "PROJECT_OWNER_IMMUTABLE", "不能移除项目所有者")
        if project["my_role"] == ROLE_ADMIN and target["role"] == ROLE_ADMIN:
            raise ApiError(403, "PROJECT_FORBIDDEN", "管理员不能移除其他管理员")
        ts = now_ms()
        conn.execute(
            "UPDATE project_members SET removed = 1, updated_at_ms = ?1 "
            "WHERE project_id = ?2 AND uid = ?3",
            (ts, project["id"], uid),
        )
        # 未完成任务收回给项目重新分配，避免成员列表里出现已移除的负责人
        unassigned = conn.execute(
            "SELECT id FROM project_tasks WHERE project_id = ?1 AND assignee_uid = ?2 "
            "AND status != 'done'",
            (project["id"], uid),
        ).fetchall()
        for row in unassigned:
            conn.execute(
                "UPDATE project_tasks SET assignee_uid = NULL, version = version + 1, "
                "updated_at_ms = ?1 WHERE id = ?2",
                (ts, row["id"]),
            )
        _emit(
            conn,
            project["id"],
            user["uid"],
            "member.removed",
            {
                "uid": uid,
                "nickname": target["nickname"],
                "unassignedTaskIds": [row["id"] for row in unassigned],
            },
        )
    return {"ok": True, "unassignedTaskIds": [row["id"] for row in unassigned]}


@router.post("/api/v1/projects/{project_id}/transfer")
def api_transfer_owner(project_id: str, payload: TransferIn, user: dict = Depends(require_user)):
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_OWNER)
        target = _active_member(conn, project["id"], payload.uid)
        if target is None:
            raise ApiError(404, "PROJECT_MEMBER_NOT_FOUND", "成员不存在")
        if target["uid"] == project["owner_uid"]:
            raise ApiError(400, "PROJECT_INVALID_INPUT", "该成员已经是项目所有者")
        ts = now_ms()
        conn.execute(
            "UPDATE project_members SET role = 'member', updated_at_ms = ?1 "
            "WHERE project_id = ?2 AND uid = ?3",
            (ts, project["id"], project["owner_uid"]),
        )
        conn.execute(
            "UPDATE project_members SET role = 'owner', updated_at_ms = ?1 "
            "WHERE project_id = ?2 AND uid = ?3",
            (ts, project["id"], target["uid"]),
        )
        conn.execute(
            "UPDATE projects SET owner_uid = ?1, updated_at_ms = ?2 WHERE id = ?3",
            (target["uid"], ts, project["id"]),
        )
        _emit(
            conn,
            project["id"],
            user["uid"],
            "project.owner_transferred",
            {"from": user["uid"], "to": target["uid"], "toNickname": target["nickname"]},
        )
    return {"ok": True}


@router.post("/api/v1/projects/{project_id}/leave")
def api_leave_project(project_id: str, user: dict = Depends(require_user)):
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        if user["uid"] == project["owner_uid"]:
            raise ApiError(
                400, "PROJECT_OWNER_IMMUTABLE", "所有者不能退出项目，请先转移所有权"
            )
        ts = now_ms()
        conn.execute(
            "UPDATE project_members SET removed = 1, updated_at_ms = ?1 "
            "WHERE project_id = ?2 AND uid = ?3",
            (ts, project["id"], user["uid"]),
        )
        _emit(conn, project["id"], user["uid"], "member.left", {"uid": user["uid"]})
    return {"ok": True}


# ── 任务与评论 ────────────────────────────────────────────────


def _require_task(conn: sqlite3.Connection, project_id: str, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        _task_query("WHERE t.project_id = ?1 AND t.id = ?2"),
        (project_id, _check_task_id(task_id)),
    ).fetchone()
    if row is None:
        raise ApiError(404, "PROJECT_TASK_NOT_FOUND", "任务不存在")
    return row


def _require_assignee(conn: sqlite3.Connection, project_id: str, uid: str) -> sqlite3.Row:
    member = _active_member(conn, project_id, uid)
    if member is None or member["role"] == ROLE_VIEWER:
        raise ApiError(400, "PROJECT_ASSIGNEE_INVALID", "负责人必须是项目成员且不是只读成员")
    return member


@router.post("/api/v1/projects/{project_id}/tasks", status_code=201)
def api_create_task(
    project_id: str, payload: TaskCreateIn, user: dict = Depends(require_user)
):
    title = _clean_single_line(payload.title, "任务标题", MAX_TASK_TITLE, required=True)
    detail = _clean_text(payload.detail, "任务说明", MAX_TASK_DETAIL)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        assignee_uid = str(payload.assignee_uid or "").strip() or None
        if assignee_uid is not None:
            _require_assignee(conn, project["id"], assignee_uid)
        task_id = f"pt-{secrets.token_hex(8)}"
        ts = now_ms()
        conn.execute(
            "INSERT INTO project_tasks "
            "(id, project_id, title, detail, status, assignee_uid, created_by, version, "
            "created_at_ms, updated_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, 'todo', ?5, ?6, 1, ?7, ?7)",
            (task_id, project["id"], title, detail, assignee_uid, user["uid"], ts),
        )
        task = _require_task(conn, project["id"], task_id)
        seq = _emit(conn, project["id"], user["uid"], "task.created", {"task": _task_view(task)})
        return {"ok": True, "task": _task_view(task), "seq": seq}


@router.patch("/api/v1/projects/{project_id}/tasks/{task_id}")
def api_update_task(
    project_id: str,
    task_id: str,
    payload: TaskPatchIn,
    user: dict = Depends(require_user),
):
    if payload.version < 1:
        raise ApiError(400, "PROJECT_INVALID_INPUT", "任务版本号不合法")
    fields = payload.model_fields_set
    content_fields = fields & {"title", "detail", "assignee_uid"}
    if not fields - {"version"}:
        raise ApiError(400, "PROJECT_EMPTY_PATCH", "没有需要修改的字段")
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        task = _require_task(conn, project["id"], task_id)
        role = project["my_role"]
        privileged = role in (ROLE_OWNER, ROLE_ADMIN) or task["created_by"] == user["uid"]
        is_assignee = task["assignee_uid"] == user["uid"]
        if content_fields and not privileged:
            raise ApiError(403, "PROJECT_FORBIDDEN", "只有项目管理员或任务创建者可以修改标题、说明与负责人")
        if "status" in fields and not (privileged or is_assignee):
            raise ApiError(403, "PROJECT_FORBIDDEN", "只有项目管理员、任务创建者或负责人可以修改状态")
        if task["version"] != payload.version:
            raise ApiError(
                409,
                "PROJECT_CONFLICT",
                f"任务已被其他人修改（当前版本 {task['version']}），请刷新后重试",
            )
        updates: dict[str, Any] = {}
        if "title" in fields:
            updates["title"] = _clean_single_line(
                payload.title, "任务标题", MAX_TASK_TITLE, required=True
            )
        if "detail" in fields:
            updates["detail"] = _clean_text(payload.detail, "任务说明", MAX_TASK_DETAIL)
        if "status" in fields:
            status = str(payload.status or "").strip().lower()
            if status not in TASK_STATUSES:
                raise ApiError(400, "PROJECT_INVALID_INPUT", "任务状态不合法")
            updates["status"] = status
        if "assignee_uid" in fields:
            assignee_uid = str(payload.assignee_uid or "").strip() or None
            if assignee_uid is not None:
                _require_assignee(conn, project["id"], assignee_uid)
            updates["assignee_uid"] = assignee_uid
        ts = now_ms()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE project_tasks SET {assignments}, version = version + 1, "
            "updated_at_ms = ? WHERE id = ?",
            list(updates.values()) + [ts, task["id"]],
        )
        updated = _require_task(conn, project["id"], task_id)
        seq = _emit(
            conn, project["id"], user["uid"], "task.updated", {"task": _task_view(updated)}
        )
        return {"ok": True, "task": _task_view(updated), "seq": seq}


@router.post("/api/v1/projects/{project_id}/tasks/{task_id}/comments", status_code=201)
def api_add_comment(
    project_id: str,
    task_id: str,
    payload: CommentIn,
    user: dict = Depends(require_user),
):
    body_text = _clean_text(payload.body, "评论", MAX_COMMENT, required=True)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        _require_task(conn, project["id"], task_id)
        comment_id = f"pc-{secrets.token_hex(8)}"
        ts = now_ms()
        conn.execute(
            "INSERT INTO project_comments "
            "(id, project_id, task_id, author_uid, body, created_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            (comment_id, project["id"], task_id, user["uid"], body_text, ts),
        )
        row = conn.execute(
            "SELECT c.*, u.nickname AS author_nickname FROM project_comments c "
            "LEFT JOIN users u ON u.uid = c.author_uid WHERE c.id = ?1",
            (comment_id,),
        ).fetchone()
        seq = _emit(
            conn, project["id"], user["uid"], "task.commented", {"comment": _comment_view(row)}
        )
        return {"ok": True, "comment": _comment_view(row), "seq": seq}


@router.post("/api/v1/projects/{project_id}/tasks/{task_id}/result")
def api_report_result(
    project_id: str,
    task_id: str,
    payload: ResultIn,
    user: dict = Depends(require_user),
):
    if payload.version < 1:
        raise ApiError(400, "PROJECT_INVALID_INPUT", "任务版本号不合法")
    status = str(payload.status or "").strip().lower()
    if status not in RESULT_STATUSES:
        raise ApiError(400, "PROJECT_INVALID_INPUT", "结果状态只能是 review / done")
    summary = _clean_text(payload.summary, "结果摘要", MAX_RESULT_SUMMARY, required=True)
    artifacts = _clean_artifacts(payload.artifacts)
    machine = _clean_single_line(payload.machine, "机器标识", MAX_MACHINE)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        task = _require_task(conn, project["id"], task_id)
        # 只允许负责人回报，避免旁观者用自己的机器结果覆盖执行者的真实产出
        if task["assignee_uid"] != user["uid"]:
            raise ApiError(403, "PROJECT_FORBIDDEN", "只有任务负责人可以回报结果")
        if task["version"] != payload.version:
            raise ApiError(
                409,
                "PROJECT_CONFLICT",
                f"任务已被其他人修改（当前版本 {task['version']}），请刷新后重试",
            )
        ts = now_ms()
        conn.execute(
            "UPDATE project_tasks SET status = ?1, result_summary = ?2, "
            "result_artifacts_json = ?3, result_machine = ?4, result_by = ?5, "
            "result_at_ms = ?6, version = version + 1, updated_at_ms = ?6 WHERE id = ?7",
            (
                status,
                summary,
                json.dumps(artifacts, ensure_ascii=False),
                machine,
                user["uid"],
                ts,
                task["id"],
            ),
        )
        updated = _require_task(conn, project["id"], task_id)
        seq = _emit(
            conn, project["id"], user["uid"], "task.result", {"task": _task_view(updated)}
        )
        return {"ok": True, "task": _task_view(updated), "seq": seq}


# ── 事件增量拉取与实时流 ─────────────────────────────────────


@router.get("/api/v1/projects/{project_id}/events")
def list_project_events(
    project_id: str,
    since: int = Query(0, ge=0),
    limit: int = Query(EVENT_DEFAULT_LIMIT, ge=1, le=EVENT_PAGE_LIMIT),
    user: dict = Depends(require_user),
):
    """按游标补拉项目事件；客户端断线重连或降级轮询都用它。"""
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        events = [_event_view(row) for row in _event_rows(conn, project["id"], since, limit)]
    next_cursor = events[-1]["seq"] if events else since
    return {
        "ok": True,
        "events": events,
        "cursor": next_cursor,
        "hasMore": len(events) == limit,
    }


@router.get("/api/v1/projects/{project_id}/events/stream")
async def stream_project_events(
    project_id: str,
    request: Request,
    since: int = Query(0, ge=0),
    user: dict = Depends(require_user),
):
    """订阅项目事件（SSE）。

    鉴权走 Bearer 头；浏览器 EventSource 不能自定义请求头时，客户端应使用
    fetch 流式读取，或先走增量接口降级。游标来自 `since` 或 `Last-Event-ID`。
    """
    project_id = _check_project_id(project_id)
    with tx() as conn:
        _require_project(conn, project_id, user["uid"], ROLE_VIEWER)

    header_cursor = request.headers.get("last-event-id", "").strip()
    cursor = since
    if header_cursor.isdigit():
        cursor = max(cursor, int(header_cursor))

    async def generate():
        nonlocal cursor
        last_ping = time.monotonic()
        yield (
            "event: ready\n"
            f"data: {json.dumps({'cursor': cursor}, ensure_ascii=False)}\n\n"
        )
        while True:
            if await request.is_disconnected():
                return
            batch = await asyncio.to_thread(
                _read_events, project_id, user["uid"], cursor, EVENT_STREAM_BATCH
            )
            if batch is None:
                # 被移出项目或项目已删除：明确结束，客户端不再重连
                yield 'event: closed\ndata: {"reason":"membership"}\n\n'
                return
            if batch:
                for event in batch:
                    cursor = event["seq"]
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: message\ndata: {data}\n\n"
                last_ping = time.monotonic()
                continue
            if time.monotonic() - last_ping >= EVENT_STREAM_PING_SECONDS:
                yield ": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(EVENT_STREAM_POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
