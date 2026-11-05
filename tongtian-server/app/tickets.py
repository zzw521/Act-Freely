"""工单层（缺陷 D-47）：授权工作单元与待人工状态机。

对标 DeepSeek Harness 的 Super-agent：把「一件被授权的工作」做成一等公民，而不是
聊天流里靠人推断的一段话。六个实体分别是

- `agent_tickets`：工单本体，七级 stage + 一个终态；
- `agent_mandates`：授权书，正文不可变，只有人的「停止」能吊销它；
- `agent_assignments`：执行体绑定，一次「谁在哪台机器的哪个目录里跑这份授权」；
- `agent_reports`：结构化报告六态（progress / waiting / ready / blocked / failed / dropped）；
- `agent_attentions`：待人工的结构化一问一答；
- `agent_subscriptions`：工单订阅某条会话/任务，后续讨论作为上下文扇出给在跑的执行体。

三条硬口径，都写在服务端而不是靠客户端自觉：

1. **「等人」只由结构化字段判定**。`waiting` 必须带 `question` 才能成立，`blocked / failed`
   直接把工单放进「等你的工单」，`ready` 才让工单进入「待评审」。服务端从不读报告正文
   去猜情绪，也从聊天流里推断是否卡住。
2. **停止是唯一撤权动作**。撤销工单只有人的 `POST .../stop` 一条路；执行体自己不能撤权，
   报告里也没有「我把授权删了」这种状态。撤权之后这张工单的一切写入都被拒。
3. **扇出只补视野，不给授权**。订阅带来的后续消息是上下文，绕过准入、不产生新的 Mandate，
   也不改变 stage——要扩大范围必须由人改授权（`mandate/amend`）。

状态变化一律写 `project_events` 的 `agent.*` 事件，复用缺陷 D-06 已验证的 SSE、游标与
成员退出即关闭的语义；不新开一条流，也不新存一份凭据。授权书正文只在本机与账号服务
之间流转，事件负载里带的是工单视图（标题、stage、摘要），不把 brief 全文广播给每个订阅者。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from .auth import require_user
from .db import now_ms, tx
from .errors import ApiError
from .projects import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    ROLE_VIEWER,
    _clean_single_line,
    _clean_text,
    _emit,
    _require_project,
)

router = APIRouter()

# 需要人动手的三种 stage：其余（未交付输入 / 排队 / 在跑 / 空闲 / 已收口）都是健康态，
# 不进「等你的工单」，不给版面。
ATTENTION_STAGES = ("awaiting_answer", "awaiting_review", "blocked")
TERMINAL_STAGE = "closed"
REPORT_KINDS = ("progress", "waiting", "ready", "blocked", "failed", "dropped")
# 六态报告各自把工单推到哪一级 stage——状态机只有这一张表，客户端改不动。
REPORT_STAGE = {
    "progress": "working",
    "waiting": "awaiting_answer",
    "ready": "awaiting_review",
    "blocked": "blocked",
    "failed": "blocked",
    "dropped": "idle",
}
INTENSITIES = ("explicit", "inferred")
ASSIGNMENT_END_STATUS = ("finished", "dropped", "lost")

MAX_TITLE = 120
MAX_DETAIL = 4000
MAX_BRIEF = 4000
MAX_SCOPE = 2000
MAX_SUMMARY = 2000
MAX_QUESTION = 1000
MAX_ANSWER = 4000
MAX_REASON = 1000
MAX_WORKER = 60
MAX_REF = 200
MAX_MACHINE = 60
MAX_ARTIFACTS = 20
MAX_ARTIFACT_NAME = 200
MAX_OPTIONS = 8
MAX_OPTION_LEN = 200
MAX_NOTE = 2000
LIST_DEFAULT = 50
LIST_MAX = 200
CONTEXT_LIMIT = 20

TICKET_ID_RE = re.compile(r"atk-[0-9a-f]{16}")


# ── 入参 ──────────────────────────────────────────────────────


class CamelIn(BaseModel):
    """JSON 入参用 camelCase；同时接受 snake_case，便于脚本与测试调用。"""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class TicketCreateIn(CamelIn):
    title: str
    detail: str = ""
    brief: str
    scope: str = ""
    out_of_scope: str = ""
    intensity: str = "explicit"
    source_kind: str = ""
    source_ref: str = ""
    policy_version: int = 0
    subscribe_task_id: str = ""


class AssignmentIn(CamelIn):
    mandate_id: str
    worker_id: str = ""
    workspace_kind: str = "isolated"
    workspace_ref: str = ""
    checkpoint_ref: str = ""
    machine: str = ""
    recovery_note: str = ""
    expected_mandate_digest: str = ""


class AssignmentEndIn(CamelIn):
    status: str
    exit_reason: str = ""


class ReportIn(CamelIn):
    # 可以不带：带了就校验"还是不是这单当前在跑的那个执行体"，不带就归给当前活着的那一个，
    # 一个都没有时如实回 409，而不是让人对着"缺字段"猜这单到底有没有在跑
    assignment_id: str = ""
    kind: str
    summary: str
    detail: str = ""
    artifacts: list[str] = []
    question: str = ""
    reason: str = ""
    options: list[str] = []
    context_seq: int = 0


class HandbackIn(CamelIn):
    assignment_id: str
    reason: str
    requested: str


class AnswerIn(CamelIn):
    answer: str


class StopIn(CamelIn):
    reason: str = ""


class AmendIn(CamelIn):
    brief: str
    scope: str = ""
    out_of_scope: str = ""
    reason: str = ""


class AcceptIn(CamelIn):
    accept: bool
    note: str = ""


class SubscribeIn(CamelIn):
    target_kind: str = "task"
    target_ref: str


# ── 小工具 ────────────────────────────────────────────────────


def _check_ticket_id(raw: str) -> str:
    ticket_id = str(raw or "").strip()
    if not TICKET_ID_RE.fullmatch(ticket_id):
        raise ApiError(404, "AGENT_TICKET_NOT_FOUND", "工单不存在或你不是项目成员")
    return ticket_id


def _clean_options(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    if len(raw) > MAX_OPTIONS:
        raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", f"候选项不能超过 {MAX_OPTIONS} 个")
    out: list[str] = []
    for item in raw:
        text = _clean_single_line(item, "候选项", MAX_OPTION_LEN)
        if text and text not in out:
            out.append(text)
    return out


def _clean_artifacts(raw: list[str] | None) -> list[str]:
    """只收产物文件名，路径与不可见字符一律拒——与共享任务同一口径。"""
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ARTIFACTS:
        raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", f"产物文件名不能超过 {MAX_ARTIFACTS} 个")
    out: list[str] = []
    for item in raw:
        name = str(item or "").replace("\\", "/").split("/")[-1].strip()
        if not name or name in (".", ".."):
            continue
        if len(name) > MAX_ARTIFACT_NAME or "\n" in name or "\x00" in name:
            raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", "产物文件名不合法")
        if name not in out:
            out.append(name)
    return out


def _mandate_digest(brief: str, scope: str, out_of_scope: str, intensity: str) -> str:
    """授权正文的规范化摘要：换行归一、空白折叠，改一个字就对不上。"""
    parts = [
        " ".join((brief or "").split()),
        " ".join((scope or "").split()),
        " ".join((out_of_scope or "").split()),
        intensity,
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _manager(project: sqlite3.Row, ticket: sqlite3.Row, uid: str) -> bool:
    """能撤权、能验收的人：项目所有者/管理员，或工单创建者本人。

    角色来自 `_require_project` 的那一行（`my_role` 只在项目汇总查询里有），
    工单行本身没有角色列——把两行分开传，别拿错行。
    """
    return project["my_role"] in (ROLE_OWNER, ROLE_ADMIN) or ticket["created_by"] == uid


def _require_open_ticket(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> sqlite3.Row:
    # 带上 creator_nickname：这行会直接进 `_ticket_view`，少一列就炸
    ticket = conn.execute(
        "SELECT t.*, p.name AS project_name, u.nickname AS creator_nickname "
        "FROM agent_tickets t "
        "JOIN projects p ON p.id = t.project_id "
        "LEFT JOIN users u ON u.uid = t.created_by "
        "WHERE t.project_id = ?1 AND t.id = ?2",
        (project_id, ticket_id),
    ).fetchone()
    if ticket is None:
        raise ApiError(404, "AGENT_TICKET_NOT_FOUND", "工单不存在或你不是项目成员")
    return ticket


def _active_mandate(conn: sqlite3.Connection, ticket_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_mandates WHERE ticket_id = ?1 AND status = 'active' "
        "ORDER BY seq DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()


def _live_assignment(conn: sqlite3.Connection, ticket_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_assignments WHERE ticket_id = ?1 AND status = 'running' "
        "ORDER BY seq DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()


def _set_stage(
    conn: sqlite3.Connection, ticket_id: str, stage: str, ts: int | None = None
) -> None:
    stamp = ts if ts is not None else now_ms()
    conn.execute(
        "UPDATE agent_tickets SET stage = ?2, updated_at_ms = ?3, version = version + 1 "
        "WHERE id = ?1",
        (ticket_id, stage, stamp),
    )


# ── 视图 ──────────────────────────────────────────────────────


def _mandate_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "ticketId": row["ticket_id"],
        "seq": row["seq"],
        "brief": row["brief"],
        "scope": row["scope"],
        "outOfScope": row["out_of_scope"],
        "intensity": row["intensity"],
        "intensityLabel": "明确授权" if row["intensity"] == "explicit" else "推断授权",
        "sourceKind": row["source_kind"],
        "sourceRef": row["source_ref"],
        "policyVersion": row["policy_version"],
        "digest": row["digest"],
        "status": row["status"],
        "statusLabel": "生效中" if row["status"] == "active" else "已吊销",
        "createdBy": row["created_by"],
        "createdAtMs": row["created_at_ms"],
        "revokedAtMs": row["revoked_at_ms"],
        "revokedBy": row["revoked_by"],
        "revokeReason": row["revoke_reason"],
    }


def _assignment_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticketId": row["ticket_id"],
        "mandateId": row["mandate_id"],
        "seq": row["seq"],
        "workerId": row["worker_id"],
        "holderUid": row["holder_uid"],
        "workspaceKind": row["workspace_kind"],
        "workspaceRef": row["workspace_ref"],
        "checkpointRef": row["checkpoint_ref"],
        "machine": row["machine"],
        "status": row["status"],
        "recoveryNote": row["recovery_note"],
        "readSteps": _json_list(row["read_steps_json"]),
        "startedAtMs": row["started_at_ms"],
        "endedAtMs": row["ended_at_ms"],
        "exitReason": row["exit_reason"],
    }


def _report_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticketId": row["ticket_id"],
        "assignmentId": row["assignment_id"],
        "kind": row["kind"],
        "kindLabel": REPORT_KIND_LABELS.get(row["kind"], row["kind"]),
        "summary": row["summary"],
        "detail": row["detail"],
        "artifacts": [str(item) for item in _json_list(row["artifacts_json"])],
        "handedBack": bool(row["handed_back"]),
        "contextSeq": row["context_seq"],
        "createdBy": row["created_by"],
        "createdAtMs": row["created_at_ms"],
    }


REPORT_KIND_LABELS = {
    "progress": "进展",
    "waiting": "等人回答",
    "ready": "待验收",
    "blocked": "受阻",
    "failed": "失败",
    "dropped": "回合结束",
}

STAGE_LABELS = {
    "input_undelivered": "输入还没交付",
    "queued": "排队中",
    "working": "在跑",
    "awaiting_answer": "等你回答",
    "awaiting_review": "等你验收",
    "idle": "空闲",
    "blocked": "等人处理",
    "closed": "已收口",
}


def _attention_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticketId": row["ticket_id"],
        "reportId": row["report_id"],
        "kind": row["kind"],
        "kindLabel": "要个答案" if row["kind"] == "answer" else "要个验收",
        "question": row["question"],
        "reason": row["reason"],
        "options": [str(item) for item in _json_list(row["options_json"])],
        "status": row["status"],
        "answer": row["answer"],
        "answeredBy": row["answered_by"],
        "answeredAtMs": row["answered_at_ms"],
        "createdAtMs": row["created_at_ms"],
    }


def _subscription_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticketId": row["ticket_id"],
        "targetKind": row["target_kind"],
        "targetRef": row["target_ref"],
        "lastFannedSeq": row["last_fanned_seq"],
        "createdAtMs": row["created_at_ms"],
        "closedAtMs": row["closed_at_ms"],
    }


def _ticket_row(conn: sqlite3.Connection, project_id: str, ticket_id: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT t.*, p.name AS project_name, u.nickname AS creator_nickname "
        "FROM agent_tickets t JOIN projects p ON p.id = t.project_id "
        "LEFT JOIN users u ON u.uid = t.created_by "
        "WHERE t.project_id = ?1 AND t.id = ?2",
        (project_id, ticket_id),
    ).fetchone()


def _ticket_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "projectName": row["project_name"],
        "title": row["title"],
        "detail": row["detail"],
        "stage": row["stage"],
        "stageLabel": STAGE_LABELS.get(row["stage"], row["stage"]),
        "needsHuman": row["stage"] in ATTENTION_STAGES,
        "intensity": row["intensity"],
        "createdBy": row["created_by"],
        "createdByNickname": row["creator_nickname"] or "",
        "createdFrom": row["created_from"],
        "version": row["version"],
        "closedAtMs": row["closed_at_ms"],
        "closedReason": row["closed_reason"],
        "createdAtMs": row["created_at_ms"],
        "updatedAtMs": row["updated_at_ms"],
    }


# ── 建单：工单与首份授权一起落 ─────────────────────────────────


def _project_prefix(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}"


@router.post("/api/v1/projects/{project_id}/tickets", status_code=201)
def api_create_ticket(
    project_id: str, payload: TicketCreateIn, user: dict = Depends(require_user)
):
    """开一张工单并写下首份授权。

    强度只有 `explicit / inferred` 两种，来自 D-46 的确定性 gate；`inferred` 必须写明
    命中依据（`sourceKind` / `sourceRef`），否则授权无从追溯，直接拒。
    """
    title = _clean_single_line(payload.title, "工单标题", MAX_TITLE, required=True)
    detail = _clean_text(payload.detail, "工单说明", MAX_DETAIL)
    brief = _clean_text(payload.brief, "授权范围说明", MAX_BRIEF, required=True)
    scope = _clean_text(payload.scope, "范围内", MAX_SCOPE)
    out_of_scope = _clean_text(payload.out_of_scope, "范围外", MAX_SCOPE)
    intensity = str(payload.intensity or "").strip().lower()
    if intensity not in INTENSITIES:
        raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", "授权强度只能是 explicit / inferred")
    source_kind = _clean_single_line(payload.source_kind, "来源类型", MAX_REF)
    source_ref = _clean_single_line(payload.source_ref, "来源引用", MAX_REF)
    if intensity == "inferred" and not source_ref:
        raise ApiError(
            400,
            "AGENT_TICKET_INVALID_INPUT",
            "推断授权必须写明依据（哪个候选/哪条判定），否则无法追溯这张工单凭什么开工",
        )
    policy_version = max(0, int(payload.policy_version or 0))

    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ts = now_ms()
        ticket_id = f"atk-{secrets.token_hex(8)}"
        mandate_id = f"mdt-{secrets.token_hex(8)}"
        conn.execute(
            "INSERT INTO agent_tickets "
            "(id, project_id, title, detail, stage, intensity, created_by, created_from, "
            "version, created_at_ms, updated_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, 'input_undelivered', ?5, ?6, ?7, 1, ?8, ?8)",
            (
                ticket_id,
                project["id"],
                title,
                detail,
                intensity,
                user["uid"],
                "candidate" if source_kind == "sa_decision" else "manual",
                ts,
            ),
        )
        conn.execute(
            "INSERT INTO agent_mandates "
            "(id, ticket_id, project_id, seq, brief, scope, out_of_scope, intensity, "
            "source_kind, source_ref, policy_version, digest, status, created_by, created_at_ms) "
            "VALUES (?1, ?2, ?3, 1, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'active', ?12, ?13)",
            (
                mandate_id,
                ticket_id,
                project["id"],
                brief,
                scope,
                out_of_scope,
                intensity,
                source_kind,
                source_ref,
                policy_version,
                _mandate_digest(brief, scope, out_of_scope, intensity),
                user["uid"],
                ts,
            ),
        )
        subscription = None
        if str(payload.subscribe_task_id or "").strip():
            subscription = _insert_subscription(
                conn,
                project["id"],
                ticket_id,
                "task",
                str(payload.subscribe_task_id).strip(),
                user["uid"],
                ts,
            )
        ticket = _ticket_row(conn, project["id"], ticket_id)
        mandate = _active_mandate(conn, ticket_id)
        seq = _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.ticket.created",
            {"ticket": _ticket_view(ticket), "mandate": _mandate_view(mandate)},
        )
        return {
            "ok": True,
            "ticket": _ticket_view(ticket),
            "mandate": _mandate_view(mandate),
            "subscription": _subscription_view(subscription) if subscription else None,
            "seq": seq,
        }


# ── 列表与详情 ────────────────────────────────────────────────


@router.get("/api/v1/agent/tickets")
def api_my_tickets(
    user: dict = Depends(require_user),
    needs_human: bool = Query(True, alias="needsHuman"),
    limit: int = Query(LIST_DEFAULT, ge=1, le=LIST_MAX),
):
    """跨项目的「等你的工单」。

    默认只列需要人动手的三种 stage：等你回答、等你验收、等人处理。健康态不占版面——
    这一条是本层的核心取舍：Agent 越多，人越不该盯着看。
    """
    stages = ATTENTION_STAGES if needs_human else tuple(STAGE_LABELS)
    placeholders = ", ".join("?" for _ in stages)
    with tx() as conn:
        rows = conn.execute(
            f"SELECT t.*, p.name AS project_name, u.nickname AS creator_nickname, "
            f"pm.role AS my_role "
            f"FROM agent_tickets t "
            f"JOIN project_members pm ON pm.project_id = t.project_id AND pm.uid = ?1 "
            f"AND pm.removed = 0 "
            f"JOIN projects p ON p.id = t.project_id "
            f"LEFT JOIN users u ON u.uid = t.created_by "
            f"WHERE t.stage IN ({placeholders}) "
            f"ORDER BY t.updated_at_ms DESC LIMIT ?{len(stages) + 2}",
            (user["uid"], *stages, limit),
        ).fetchall()
        tickets = [_ticket_view(row) for row in rows]
        for item in tickets:
            att = conn.execute(
                "SELECT * FROM agent_attentions WHERE ticket_id = ?1 AND status = 'open' "
                "ORDER BY created_at_ms DESC LIMIT 1",
                (item["id"],),
            ).fetchone()
            item["attention"] = _attention_view(att) if att else None
            mdt = _active_mandate(conn, item["id"])
            item["mandateBrief"] = mdt["brief"] if mdt else ""
            item["mandateSeq"] = mdt["seq"] if mdt else 0
    return {"ok": True, "tickets": tickets, "count": len(tickets)}


@router.get("/api/v1/projects/{project_id}/tickets")
def api_list_tickets(
    project_id: str,
    user: dict = Depends(require_user),
    stage: str = Query("", max_length=32),
    limit: int = Query(LIST_DEFAULT, ge=1, le=LIST_MAX),
):
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        where = "t.project_id = ?1"
        args: list[Any] = [project["id"]]
        if stage:
            if stage not in STAGE_LABELS:
                raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", "工单状态不合法")
            where += " AND t.stage = ?2"
            args.append(stage)
        args.append(limit)
        rows = conn.execute(
            f"SELECT t.*, p.name AS project_name, u.nickname AS creator_nickname "
            f"FROM agent_tickets t JOIN projects p ON p.id = t.project_id "
            f"LEFT JOIN users u ON u.uid = t.created_by WHERE {where} "
            f"ORDER BY t.updated_at_ms DESC LIMIT ?{len(args)}",
            args,
        ).fetchall()
        tickets = [_ticket_view(row) for row in rows]
    return {"ok": True, "tickets": tickets, "count": len(tickets)}


@router.get("/api/v1/projects/{project_id}/tickets/{ticket_id}")
def api_ticket_detail(
    project_id: str, ticket_id: str, user: dict = Depends(require_user)
):
    """工单全貌；顺带把订阅线上新到的事件作为上下文扇出给在跑的执行体。

    扇出只补视野：它绕过准入、不产生新授权，也不改变 stage。
    """
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        contexts = _fan_out(conn, project["id"], ticket["id"])
        mandate = _active_mandate(conn, ticket["id"])
        assignments = [
            _assignment_view(row)
            for row in conn.execute(
                "SELECT * FROM agent_assignments WHERE ticket_id = ?1 ORDER BY seq DESC",
                (ticket["id"],),
            ).fetchall()
        ]
        reports = [
            _report_view(row)
            for row in conn.execute(
                "SELECT * FROM agent_reports WHERE ticket_id = ?1 "
                "ORDER BY created_at_ms ASC, id ASC",
                (ticket["id"],),
            ).fetchall()
        ]
        attentions = [
            _attention_view(row)
            for row in conn.execute(
                "SELECT * FROM agent_attentions WHERE ticket_id = ?1 "
                "ORDER BY created_at_ms ASC, id ASC",
                (ticket["id"],),
            ).fetchall()
        ]
        subscriptions = [
            _subscription_view(row)
            for row in conn.execute(
                "SELECT * FROM agent_subscriptions WHERE ticket_id = ?1 ORDER BY created_at_ms ASC",
                (ticket["id"],),
            ).fetchall()
        ]
        return {
            "ok": True,
            "ticket": _ticket_view(ticket),
            "mandate": _mandate_view(mandate),
            "assignments": assignments,
            "reports": reports,
            "attentions": attentions,
            "subscriptions": subscriptions,
            "contexts": contexts,
        }


# ── 扇出：订阅线上新到的事件 ─────────────────────────────────


def _fan_out(
    conn: sqlite3.Connection, project_id: str, ticket_id: str
) -> list[dict[str, Any]]:
    """把每条订阅游标之后的项目事件交给工单，并把游标只前进地推上去。"""
    delivered: list[dict[str, Any]] = []
    subs = conn.execute(
        "SELECT * FROM agent_subscriptions WHERE ticket_id = ?1 AND closed_at_ms = 0 "
        "ORDER BY created_at_ms ASC",
        (ticket_id,),
    ).fetchall()
    for sub in subs:
        rows = conn.execute(
            "SELECT seq, actor_uid, kind, payload_json, created_at_ms FROM project_events "
            "WHERE project_id = ?1 AND seq > ?2 AND kind NOT LIKE 'agent.%' "
            "ORDER BY seq ASC LIMIT ?3",
            (project_id, sub["last_fanned_seq"], CONTEXT_LIMIT),
        ).fetchall()
        if not rows:
            continue
        items = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            items.append(
                {
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "actorUid": row["actor_uid"],
                    "payload": payload,
                    "createdAtMs": row["created_at_ms"],
                    "subscriptionId": sub["id"],
                }
            )
        top = int(rows[-1]["seq"])
        # 只前进：并发读到同一段时，落后的那一次不会把游标拽回去
        conn.execute(
            "UPDATE agent_subscriptions SET last_fanned_seq = ?2 "
            "WHERE id = ?1 AND last_fanned_seq < ?2",
            (sub["id"], top),
        )
        _emit(
            conn,
            project_id,
            sub["created_by"],
            "agent.context.fanned",
            {"ticketId": ticket_id, "subscriptionId": sub["id"], "uptoSeq": top, "count": len(items)},
        )
        delivered.extend(items)
    return delivered


def _insert_subscription(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    target_kind: str,
    target_ref: str,
    uid: str,
    ts: int,
) -> sqlite3.Row:
    if target_kind not in ("thread", "task"):
        raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", "订阅目标只能是 thread / task")
    ref = _clean_single_line(target_ref, "订阅目标", MAX_REF, required=True)
    if target_kind == "task":
        task = conn.execute(
            "SELECT id FROM project_tasks WHERE project_id = ?1 AND id = ?2",
            (project_id, ref),
        ).fetchone()
        if task is None:
            raise ApiError(404, "PROJECT_TASK_NOT_FOUND", "要订阅的任务不存在")
    # 起点用当前游标：订阅之前的历史不重放，之后的新消息才是上下文
    head = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS seq FROM project_events WHERE project_id = ?1",
        (project_id,),
    ).fetchone()["seq"]
    sub_id = f"sb-{secrets.token_hex(8)}"
    conn.execute(
        "INSERT INTO agent_subscriptions "
        "(id, ticket_id, project_id, target_kind, target_ref, last_fanned_seq, created_by, "
        "created_at_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8) "
        "ON CONFLICT(ticket_id, target_kind, target_ref) DO NOTHING",
        (sub_id, ticket_id, project_id, target_kind, ref, head, uid, ts),
    )
    row = conn.execute(
        "SELECT * FROM agent_subscriptions WHERE ticket_id = ?1 AND target_kind = ?2 "
        "AND target_ref = ?3",
        (ticket_id, target_kind, ref),
    ).fetchone()
    return row


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/subscriptions", status_code=201)
def api_subscribe(
    project_id: str, ticket_id: str, payload: SubscribeIn, user: dict = Depends(require_user)
):
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(409, "AGENT_TICKET_CLOSED", "工单已收口，不再接受新的订阅")
        ts = now_ms()
        row = _insert_subscription(
            conn, project["id"], ticket["id"], payload.target_kind, payload.target_ref, user["uid"], ts
        )
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.subscription.created",
            {"ticketId": ticket["id"], "subscription": _subscription_view(row)},
        )
        return {"ok": True, "subscription": _subscription_view(row)}


# ── 执行体领取与交回 ─────────────────────────────────────────


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments", status_code=201)
def api_claim(
    project_id: str, ticket_id: str, payload: AssignmentIn, user: dict = Depends(require_user)
):
    """执行体领取一份授权开始跑——续跑前重新校验授权，这是 DSH 的纪律。

    带 `mandateId` 时校验「还是不是当前生效的那一份」；带 `expectedMandateDigest` 时再校验
    「正文一个字都没变」。任何一项对不上就说明授权已经变了，旧的运行上下文不能接着跑。
    """
    worker_id = _clean_single_line(payload.worker_id, "执行体标识", MAX_WORKER)
    workspace_kind = str(payload.workspace_kind or "isolated").strip().lower()
    if workspace_kind not in ("shared", "isolated"):
        raise ApiError(400, "AGENT_TICKET_INVALID_INPUT", "工作区类型只能是 isolated / shared")
    workspace_ref = _clean_single_line(payload.workspace_ref, "工作区引用", MAX_REF)
    checkpoint_ref = _clean_single_line(payload.checkpoint_ref, "检查点引用", MAX_REF)
    machine = _clean_single_line(payload.machine, "机器标识", MAX_MACHINE)
    recovery_note = _clean_text(payload.recovery_note, "恢复注记", MAX_NOTE)

    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(
                409,
                "AGENT_TICKET_CLOSED",
                "工单已收口：人的停止是唯一能撤销授权的动作，撤了之后这张工单不再接任何输入",
            )
        mandate = _active_mandate(conn, ticket["id"])
        if mandate is None:
            raise ApiError(
                409,
                "AGENT_MANDATE_REVOKED",
                "这张工单当前没有生效授权，执行体不能开工；要开工请由人重新交代范围",
            )
        wanted = str(payload.mandate_id or "").strip()
        if wanted and wanted != mandate["id"]:
            raise ApiError(
                409,
                "AGENT_MANDATE_SUPERSEDED",
                "你按的那份授权已经被更新的一版取代（现在生效的是第 "
                f"{mandate['seq']} 份），先重读授权再开工",
            )
        expected_digest = str(payload.expected_mandate_digest or "").strip()
        if expected_digest and expected_digest != mandate["digest"]:
            raise ApiError(
                409,
                "AGENT_MANDATE_CHANGED",
                "授权正文已经改过（摘要对不上），续跑前必须按新正文重新校验",
            )
        live = _live_assignment(conn, ticket["id"])
        if live is not None and live["mandate_id"] == mandate["id"]:
            raise ApiError(
                409,
                "AGENT_ASSIGNMENT_RUNNING",
                f"这份授权已经在跑（执行体 {live['worker_id'] or live['id']}），不能再开一个",
            )
        ts = now_ms()
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM agent_assignments WHERE ticket_id = ?1",
            (ticket["id"],),
        ).fetchone()
        assignment_id = f"asg-{secrets.token_hex(8)}"
        conn.execute(
            "INSERT INTO agent_assignments "
            "(id, ticket_id, project_id, mandate_id, seq, worker_id, holder_uid, workspace_kind, "
            "workspace_ref, checkpoint_ref, machine, status, recovery_note, started_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'running', ?12, ?13)",
            (
                assignment_id,
                ticket["id"],
                project["id"],
                mandate["id"],
                int(seq_row["next"]),
                worker_id,
                user["uid"],
                workspace_kind,
                workspace_ref,
                checkpoint_ref,
                machine,
                recovery_note,
                ts,
            ),
        )
        # 输入交付了才算真的开工：input_undelivered → working
        _set_stage(conn, ticket["id"], "working", ts)
        assignment = conn.execute(
            "SELECT * FROM agent_assignments WHERE id = ?1", (assignment_id,)
        ).fetchone()
        ticket = _ticket_row(conn, project["id"], ticket["id"])
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.assignment.started",
            {"ticket": _ticket_view(ticket), "assignment": _assignment_view(assignment)},
        )
        return {"ok": True, "assignment": _assignment_view(assignment), "ticket": _ticket_view(ticket)}


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments/{assignment_id}/end")
def api_end_assignment(
    project_id: str,
    ticket_id: str,
    assignment_id: str,
    payload: AssignmentEndIn,
    user: dict = Depends(require_user),
):
    """执行体收手：只有跑这一单的持有人（或项目管理员）能宣布它结束了。"""
    status = str(payload.status or "").strip().lower()
    if status not in ASSIGNMENT_END_STATUS:
        raise ApiError(
            400, "AGENT_TICKET_INVALID_INPUT", "执行体结束状态只能是 finished / dropped / lost"
        )
    exit_reason = _clean_text(payload.exit_reason, "结束原因", MAX_REASON)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        row = conn.execute(
            "SELECT * FROM agent_assignments WHERE id = ?1 AND ticket_id = ?2",
            (assignment_id, ticket["id"]),
        ).fetchone()
        if row is None:
            raise ApiError(404, "AGENT_ASSIGNMENT_NOT_FOUND", "这次执行体绑定不存在")
        if row["status"] != "running":
            return {"ok": True, "assignment": _assignment_view(row), "alreadyEnded": True}
        if row["holder_uid"] != user["uid"] and not _manager(project, ticket, user["uid"]):
            raise ApiError(403, "AGENT_TICKET_FORBIDDEN", "只有领取这单的人或项目管理员能结束它")
        ts = now_ms()
        conn.execute(
            "UPDATE agent_assignments SET status = ?2, ended_at_ms = ?3, exit_reason = ?4 "
            "WHERE id = ?1",
            (row["id"], status, ts, exit_reason),
        )
        # 工单已经收口时不复活 stage；否则没有活的执行体就是排队等下一次领取
        if ticket["stage"] != TERMINAL_STAGE and _live_assignment(conn, ticket["id"]) is None:
            _set_stage(conn, ticket["id"], "queued", ts)
        updated = conn.execute(
            "SELECT * FROM agent_assignments WHERE id = ?1", (row["id"],)
        ).fetchone()
        ticket = _ticket_row(conn, project["id"], ticket["id"])
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.assignment.ended",
            {"ticket": _ticket_view(ticket), "assignment": _assignment_view(updated)},
        )
        return {"ok": True, "assignment": _assignment_view(updated), "ticket": _ticket_view(ticket)}


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/handback", status_code=201)
def api_handback(
    project_id: str, ticket_id: str, payload: HandbackIn, user: dict = Depends(require_user)
):
    """执行体把超出范围的诉求交回给人，而不是自己顺手做掉。

    一张工单只交回一次（由库里的部分唯一索引保证）：交回之后球在人这边——要么改授权
    扩大范围，要么收口。反复交回等于把同一件事问两遍。
    """
    reason = _clean_text(payload.reason, "交回理由", MAX_REASON, required=True)
    requested = _clean_text(payload.requested, "希望扩大的范围", MAX_SCOPE, required=True)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(409, "AGENT_TICKET_CLOSED", "工单已收口，交回也没有人会看到")
        assignment = _require_live_assignment(conn, ticket["id"], payload.assignment_id, user["uid"])
        done = conn.execute(
            "SELECT id FROM agent_reports WHERE ticket_id = ?1 AND handed_back = 1",
            (ticket["id"],),
        ).fetchone()
        if done is not None:
            raise ApiError(
                409,
                "AGENT_HANDBACK_DONE",
                "这单已经交回过一次了：现在等人改授权或收口，重复交回只会让人重复处理同一件事",
            )
        ts = now_ms()
        report_id = _insert_report(
            conn,
            project["id"],
            ticket["id"],
            assignment["id"],
            "blocked",
            f"超出授权范围，交回给人：{reason}",
            requested,
            [],
            0,
            user["uid"],
            ts,
            handed_back=1,
        )
        attention_id = _insert_attention(
            conn,
            project["id"],
            ticket["id"],
            report_id,
            "answer",
            "这件事超出你给我的授权范围，要不要把范围扩成这样？",
            reason,
            [f"按这个范围扩大授权：{requested}", "不做，按原范围收尾"],
            user["uid"],
            ts,
        )
        _set_stage(conn, ticket["id"], "awaiting_answer", ts)
        fresh = _ticket_row(conn, project["id"], ticket["id"])
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.ticket.handed_back",
            {
                "ticket": _ticket_view(fresh),
                "reportId": report_id,
                "attentionId": attention_id,
                "requested": requested,
            },
        )
        return {
            "ok": True,
            "ticket": _ticket_view(fresh),
            "reportId": report_id,
            "attentionId": attention_id,
        }


# ── 结构化报告与状态机 ───────────────────────────────────────


def _require_live_assignment(
    conn: sqlite3.Connection, ticket_id: str, assignment_id: str, uid: str
) -> sqlite3.Row:
    wanted = str(assignment_id or "").strip()
    row = None
    if wanted:
        row = conn.execute(
            "SELECT * FROM agent_assignments WHERE id = ?1 AND ticket_id = ?2",
            (wanted, ticket_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "AGENT_ASSIGNMENT_NOT_FOUND", "这次执行体绑定不存在")
        if row["status"] != "running":
            raise ApiError(
                409,
                "AGENT_ASSIGNMENT_STALE",
                "这单已经由其它执行体接手，旧执行体的报告不再改变工单状态",
            )
    else:
        row = _live_assignment(conn, ticket_id)
        if row is None:
            raise ApiError(409, "AGENT_ASSIGNMENT_NOT_FOUND", "现在没有在跑的执行体，报告无处可归")
    if row["holder_uid"] != uid:
        raise ApiError(403, "AGENT_TICKET_FORBIDDEN", "只有领取这单的执行体能替它报告")
    return row


def _insert_report(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    assignment_id: str,
    kind: str,
    summary: str,
    detail: str,
    artifacts: list[str],
    context_seq: int,
    uid: str,
    ts: int,
    handed_back: int = 0,
) -> str:
    report_id = f"arp-{secrets.token_hex(8)}"
    conn.execute(
        "INSERT INTO agent_reports "
        "(id, ticket_id, project_id, assignment_id, kind, summary, detail, artifacts_json, "
        "handed_back, context_seq, created_by, created_at_ms) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        (
            report_id,
            ticket_id,
            project_id,
            assignment_id,
            kind,
            summary,
            detail,
            json.dumps(artifacts, ensure_ascii=False),
            handed_back,
            context_seq,
            uid,
            ts,
        ),
    )
    return report_id


def _insert_attention(
    conn: sqlite3.Connection,
    project_id: str,
    ticket_id: str,
    report_id: str,
    kind: str,
    question: str,
    reason: str,
    options: list[str],
    uid: str,
    ts: int,
) -> str:
    """开一个新的待人工问题；同单已有开放问题时先撤旧的。

    「同时只留一个」是版面纪律：人可以回答，但界面上不该同时挂着三问。
    """
    conn.execute(
        "UPDATE agent_attentions SET status = 'withdrawn' WHERE ticket_id = ?1 AND status = 'open'",
        (ticket_id,),
    )
    attention_id = f"atn-{secrets.token_hex(8)}"
    conn.execute(
        "INSERT INTO agent_attentions "
        "(id, ticket_id, project_id, report_id, kind, question, reason, options_json, "
        "status, created_at_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'open', ?9)",
        (
            attention_id,
            ticket_id,
            project_id,
            report_id,
            kind,
            question,
            reason,
            json.dumps(options, ensure_ascii=False),
            ts,
        ),
    )
    return attention_id


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/reports", status_code=201)
def api_report(
    project_id: str, ticket_id: str, payload: ReportIn, user: dict = Depends(require_user)
):
    """执行体报告进展。工单去哪一级，只由 `kind` 这一个结构化字段决定。

    `waiting` 必须带 `question`——「等人」这件事得说清楚等什么，不让人去读正文猜。
    `ready` 才把工单推进「等你验收」；`blocked / failed` 落到「等人处理」；
    `dropped` 只表示这一回合结束（后台作业或子执行体还在跑），不是等人。
    """
    kind = str(payload.kind or "").strip().lower()
    if kind not in REPORT_KINDS:
        raise ApiError(
            400,
            "AGENT_REPORT_KIND_INVALID",
            "报告状态只能是 progress / waiting / ready / blocked / failed / dropped",
        )
    summary = _clean_text(payload.summary, "报告摘要", MAX_SUMMARY, required=True)
    detail = _clean_text(payload.detail, "报告正文", MAX_DETAIL)
    artifacts = _clean_artifacts(payload.artifacts)
    question = _clean_text(payload.question, "问题", MAX_QUESTION)
    reason = _clean_text(payload.reason, "理由", MAX_REASON)
    options = _clean_options(payload.options)
    if kind == "waiting" and not question:
        raise ApiError(
            400,
            "AGENT_ATTENTION_QUESTION_REQUIRED",
            "等人回答必须写明等什么（question），否则工单只能按受阻处理",
        )
    context_seq = max(0, int(payload.context_seq or 0))

    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(409, "AGENT_TICKET_CLOSED", "工单已收口，报告不会有人看到")
        assignment = _require_live_assignment(conn, ticket["id"], payload.assignment_id, user["uid"])
        ts = now_ms()
        report_id = _insert_report(
            conn,
            project["id"],
            ticket["id"],
            assignment["id"],
            kind,
            summary,
            detail,
            artifacts,
            context_seq,
            user["uid"],
            ts,
        )
        attention_id = ""
        if kind == "waiting":
            attention_id = _insert_attention(
                conn,
                project["id"],
                ticket["id"],
                report_id,
                "answer",
                question,
                reason,
                options,
                user["uid"],
                ts,
            )
        elif kind == "ready":
            attention_id = _insert_attention(
                conn,
                project["id"],
                ticket["id"],
                report_id,
                "review",
                question or "结果已就绪，是否验收？",
                reason,
                ["验收通过，收口", "不合格，按原范围重做"],
                user["uid"],
                ts,
            )
        elif kind in ("blocked", "failed"):
            # 受阻也是「等人」：有开放问题就按问题回答，没有就把摘要当问题问一遍
            attention_id = _insert_attention(
                conn,
                project["id"],
                ticket["id"],
                report_id,
                "answer",
                question or f"这单卡在：{summary}",
                reason,
                options,
                user["uid"],
                ts,
            )
        _set_stage(conn, ticket["id"], REPORT_STAGE[kind], ts)
        fresh = _ticket_row(conn, project["id"], ticket["id"])
        report_row = conn.execute(
            "SELECT * FROM agent_reports WHERE id = ?1", (report_id,)
        ).fetchone()
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.report.created",
            {
                "ticket": _ticket_view(fresh),
                "report": _report_view(report_row),
                "attentionId": attention_id,
            },
        )
        return {
            "ok": True,
            "report": _report_view(report_row),
            "ticket": _ticket_view(fresh),
            "attentionId": attention_id or None,
        }


# ── 人这一侧：回答、验收、改授权、停止 ───────────────────────


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/attentions/{attention_id}/answer")
def api_answer_attention(
    project_id: str,
    ticket_id: str,
    attention_id: str,
    payload: AnswerIn,
    user: dict = Depends(require_user),
):
    """人回答一个待人工问题：答案落账，工单重新排队或在跑的执行体继续。"""
    answer = _clean_text(payload.answer, "回答", MAX_ANSWER, required=True)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        row = conn.execute(
            "SELECT * FROM agent_attentions WHERE id = ?1 AND ticket_id = ?2",
            (attention_id, ticket["id"]),
        ).fetchone()
        if row is None:
            raise ApiError(404, "AGENT_ATTENTION_NOT_FOUND", "这个问题不存在")
        if row["status"] != "open":
            raise ApiError(
                409,
                "AGENT_ATTENTION_NOT_OPEN",
                f"这个问题已经是 {row['status']} 状态，不能重复回答",
            )
        ts = now_ms()
        conn.execute(
            "UPDATE agent_attentions SET status = 'answered', answer = ?2, answered_by = ?3, "
            "answered_at_ms = ?4 WHERE id = ?1",
            (row["id"], answer, user["uid"], ts),
        )
        if ticket["stage"] != TERMINAL_STAGE:
            live = _live_assignment(conn, ticket["id"])
            # 还有活的执行体就继续在跑，没有就退回排队等下一次领取
            _set_stage(conn, ticket["id"], "working" if live else "queued", ts)
        updated = conn.execute(
            "SELECT * FROM agent_attentions WHERE id = ?1", (row["id"],)
        ).fetchone()
        fresh = _ticket_row(conn, project["id"], ticket["id"])
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.attention.answered",
            {"ticket": _ticket_view(fresh), "attention": _attention_view(updated)},
        )
        return {"ok": True, "attention": _attention_view(updated), "ticket": _ticket_view(fresh)}


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/accept")
def api_accept(
    project_id: str, ticket_id: str, payload: AcceptIn, user: dict = Depends(require_user)
):
    """人验收这一单：通过就收口，不通过就退回排队（授权不变，范围照旧）。"""
    note = _clean_text(payload.note, "验收意见", MAX_REASON)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(409, "AGENT_TICKET_CLOSED", "工单已经收口，不能再验收一次")
        review = conn.execute(
            "SELECT * FROM agent_attentions WHERE ticket_id = ?1 AND status = 'open' "
            "AND kind = 'review' ORDER BY created_at_ms DESC LIMIT 1",
            (ticket["id"],),
        ).fetchone()
        if review is not None:
            ts = now_ms()
            conn.execute(
                "UPDATE agent_attentions SET status = 'answered', answer = ?2, answered_by = ?3, "
                "answered_at_ms = ?4 WHERE id = ?1",
                (review["id"], ("验收通过 " + note).strip(), user["uid"], ts),
            )
        ts = now_ms()
        if payload.accept:
            conn.execute(
                "UPDATE agent_tickets SET stage = 'closed', closed_at_ms = ?2, "
                "closed_reason = ?3, version = version + 1, updated_at_ms = ?2 WHERE id = ?1",
                (ticket["id"], ts, "accepted" + (f"：{note}" if note else "")),
            )
        else:
            # 退回重做：不吊销授权（范围没变），只把 stage 退回排队并留一条验收意见
            conn.execute(
                "UPDATE agent_tickets SET stage = 'queued', version = version + 1, "
                "updated_at_ms = ?2 WHERE id = ?1",
                (ticket["id"], ts),
            )
            live = _live_assignment(conn, ticket["id"])
            if live is not None:
                conn.execute(
                    "UPDATE agent_assignments SET status = 'dropped', ended_at_ms = ?2, "
                    "exit_reason = ?3 WHERE id = ?1",
                    (live["id"], ts, f"人退回重做：{note or '未写明原因'}"),
                )
        fresh = _ticket_row(conn, project["id"], ticket["id"])
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.ticket.accepted",
            {"ticket": _ticket_view(fresh), "accept": bool(payload.accept), "note": note},
        )
        return {"ok": True, "ticket": _ticket_view(fresh)}


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/mandate/amend")
def api_amend_mandate(
    project_id: str, ticket_id: str, payload: AmendIn, user: dict = Depends(require_user)
):
    """人改授权范围：吊销旧的那一份，插一版新的。授权正文本身永远不被改写。"""
    brief = _clean_text(payload.brief, "授权范围说明", MAX_BRIEF, required=True)
    scope = _clean_text(payload.scope, "范围内", MAX_SCOPE)
    out_of_scope = _clean_text(payload.out_of_scope, "范围外", MAX_SCOPE)
    reason = _clean_text(payload.reason, "改授权理由", MAX_REASON)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if not _manager(project, ticket, user["uid"]):
            raise ApiError(
                403, "AGENT_TICKET_FORBIDDEN", "只有工单创建者或项目管理员能改这份授权的范围"
            )
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(409, "AGENT_TICKET_CLOSED", "工单已收口，改授权也不会开工")
        mandate = _active_mandate(conn, ticket["id"])
        if mandate is None:
            raise ApiError(409, "AGENT_MANDATE_REVOKED", "这份授权已被吊销，要改范围请重开一单")
        ts = now_ms()
        conn.execute(
            "UPDATE agent_mandates SET status = 'revoked', revoked_at_ms = ?2, revoked_by = ?3, "
            "revoke_reason = ?4 WHERE id = ?1",
            (mandate["id"], ts, user["uid"], reason or "人改动了授权范围"),
        )
        next_seq = int(conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM agent_mandates WHERE ticket_id = ?1",
            (ticket["id"],),
        ).fetchone()["next"])
        new_id = f"mdt-{secrets.token_hex(8)}"
        conn.execute(
            "INSERT INTO agent_mandates "
            "(id, ticket_id, project_id, seq, brief, scope, out_of_scope, intensity, "
            "source_kind, source_ref, policy_version, digest, status, created_by, created_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, 'active', ?13, ?14)",
            (
                new_id,
                ticket["id"],
                project["id"],
                next_seq,
                brief,
                scope,
                out_of_scope,
                mandate["intensity"],
                mandate["source_kind"],
                mandate["source_ref"],
                mandate["policy_version"],
                _mandate_digest(brief, scope, out_of_scope, mandate["intensity"]),
                user["uid"],
                ts,
            ),
        )
        # 范围变了，正在跑的上下文不能再沿用：把活着的执行体结束，退回排队
        live = _live_assignment(conn, ticket["id"])
        if live is not None:
            conn.execute(
                "UPDATE agent_assignments SET status = 'dropped', ended_at_ms = ?2, "
                "exit_reason = ?3 WHERE id = ?1",
                (live["id"], ts, "授权范围被改动，旧执行体不再沿用"),
            )
        _set_stage(conn, ticket["id"], "queued", ts)
        fresh = _ticket_row(conn, project["id"], ticket["id"])
        new_mandate = _active_mandate(conn, ticket["id"])
        _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.mandate.amended",
            {
                "ticket": _ticket_view(fresh),
                "mandate": _mandate_view(new_mandate),
                "supersededMandateId": mandate["id"],
            },
        )
        return {"ok": True, "mandate": _mandate_view(new_mandate), "ticket": _ticket_view(fresh)}


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/stop")
def api_stop_ticket(
    project_id: str, ticket_id: str, payload: StopIn, user: dict = Depends(require_user)
):
    """停止：唯一能撤销授权的动作，也是这张工单的最后一次写入。

    停完之后：授权吊销、工单收口、活着的执行体结束、开放问题撤回；此后一切写入
    （报告、交回、回答、改授权、再领取）都被 `AGENT_TICKET_CLOSED` 拒掉。
    """
    reason = _clean_text(payload.reason, "停止理由", MAX_REASON)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        if not _manager(project, ticket, user["uid"]):
            raise ApiError(
                403,
                "AGENT_TICKET_FORBIDDEN",
                "只有工单创建者或项目管理员能停止这单——停止就是撤权",
            )
        if ticket["stage"] == TERMINAL_STAGE:
            raise ApiError(409, "AGENT_TICKET_CLOSED", "工单已经收口，不用再停一次")
        ts = now_ms()
        mandate = _active_mandate(conn, ticket["id"])
        if mandate is not None:
            conn.execute(
                "UPDATE agent_mandates SET status = 'revoked', revoked_at_ms = ?2, revoked_by = ?3, "
                "revoke_reason = ?4 WHERE id = ?1",
                (mandate["id"], ts, user["uid"], reason or "人停止了这单"),
            )
        live = _live_assignment(conn, ticket["id"])
        if live is not None:
            conn.execute(
                "UPDATE agent_assignments SET status = 'lost', ended_at_ms = ?2, exit_reason = ?3 "
                "WHERE id = ?1",
                (live["id"], ts, "人被停止"),
            )
        conn.execute(
            "UPDATE agent_attentions SET status = 'withdrawn' WHERE ticket_id = ?1 "
            "AND status = 'open'",
            (ticket["id"],),
        )
        conn.execute(
            "UPDATE agent_tickets SET stage = 'closed', closed_at_ms = ?2, closed_reason = ?3, "
            "version = version + 1, updated_at_ms = ?2 WHERE id = ?1",
            (ticket["id"], ts, "stopped" + (f"：{reason}" if reason else "")),
        )
        conn.execute(
            "UPDATE agent_subscriptions SET closed_at_ms = ?2 WHERE ticket_id = ?1 "
            "AND closed_at_ms = 0",
            (ticket["id"], ts),
        )
        fresh = _ticket_row(conn, project["id"], ticket["id"])
        seq = _emit(
            conn,
            project["id"],
            user["uid"],
            "agent.ticket.stopped",
            {
                "ticket": _ticket_view(fresh),
                "revokedMandateId": mandate["id"] if mandate else "",
                "reason": reason,
            },
        )
        return {"ok": True, "ticket": _ticket_view(fresh), "seq": seq}


# ── 已读事实落账（D-48.3 的服务端一侧）─────────────────────


@router.post("/api/v1/projects/{project_id}/tickets/{ticket_id}/assignments/{assignment_id}/read-step")
def api_mark_read(
    project_id: str,
    ticket_id: str,
    assignment_id: str,
    user: dict = Depends(require_user),
    event_seq: int = Query(..., alias="eventSeq", ge=1),
):
    """按步骤落账「这一号会话消息已经被执行体读进历史」。

    重启之后先查这张账再决定要不要重做：已经读过就不再重放，这是 D-48 验收①的依据。
    """
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        ticket = _require_open_ticket(conn, project["id"], _check_ticket_id(ticket_id))
        row = conn.execute(
            "SELECT * FROM agent_assignments WHERE id = ?1 AND ticket_id = ?2",
            (assignment_id, ticket["id"]),
        ).fetchone()
        if row is None:
            raise ApiError(404, "AGENT_ASSIGNMENT_NOT_FOUND", "这次执行体绑定不存在")
        if row["holder_uid"] != user["uid"]:
            raise ApiError(403, "AGENT_TICKET_FORBIDDEN", "只有领取这单的执行体能替它记已读")
        steps = [item for item in _json_list(row["read_steps_json"]) if isinstance(item, dict)]
        already = any(int(item.get("seq") or 0) == event_seq for item in steps)
        if not already:
            steps.append({"seq": event_seq, "atMs": now_ms(), "by": user["uid"]})
            conn.execute(
                "UPDATE agent_assignments SET read_steps_json = ?2 WHERE id = ?1",
                (row["id"], json.dumps(steps, ensure_ascii=False)),
            )
        updated = conn.execute(
            "SELECT * FROM agent_assignments WHERE id = ?1", (row["id"],)
        ).fetchone()
        return {"ok": True, "assignment": _assignment_view(updated), "already": already}
