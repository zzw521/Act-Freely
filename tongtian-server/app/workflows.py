"""工作流层（缺陷 D-10 的服务端半边）：团队共用的定义仓库 + 每次运行的留证。

桌面引擎（`tongtian-tauri/engine/workflow_runtime.py`）是**唯一执行者**：触发→动作→条件→
人工卡点那四步在本地推进，唤醒投进的是 D-48 那张工单表。本层不执行任何一步，只做三件
桌面做不了的事：

1. **共享定义**：一本流程在项目里只有一份，成员之间看得见、改得动，且改法是**升版**
   而不是覆盖——`expectedVersion` 对不上就 409，谁也不会把自己的版本静默盖到别人头上
   （桌面本地没有这个问题：一台机器只有一个写者；跨设备才有）。
2. **每次运行的留证**：谁起的、钉的是哪一版（`workflowVersion` + `workflowDigest`）、
   现在停在哪儿、每一步的状态与尝试次数。换一台设备也要答得出「上次那本跑到哪了」。
3. **人工卡点的结论是独立一行**：`agent_workflow_approvals` 只记**人真给过的**结论
   （`allow` / `deny` + 谁点的 + 说了什么）。桌面上档位在 off 时那种自动放行**不进这张表**，
   它只在运行行的 `autoPassed` 计数里——把没人问过的事写成有人批准过，是这一层最坏的谎。

三条硬口径：

1. **服务端不推进运行**。`report` 收的是引擎已经算好的视图，`approve` 收的是人已给的结论；
   本层既不判定条件、也不决定下一步走哪条分支（那需要定义里的步骤形状，判定权在引擎）。
   于是「两处都能跑」这种最难查的分歧在这里根本不存在。
2. **定义的语义校验与桌面同一套规则**。四种节点、三种动作、九个算子这三张表在本文件与
   `engine/workflow_runtime.py` 各存一份（一个跑在容器、一个跑在桌面，没法 import 对方）。
   跨树一致性由桌面侧自测钉住：`tests/integration/engine_workflow_runtime_selftest.py`
   的 `group_crosswire` 会读本文件的常量并与引擎那份逐条比对（文件不在就 SKIP 并说明）。
   **改这三张表必须同时改两处**，否则桌面存得下的定义服务端会说非法，共享当场断。
3. **没有硬删**。归档只是 `archived = 1`：还有未收口运行挂在上面时默认 409，带 `force`
   也只是如实归档——运行历史不跟着蒸发（`ON DELETE CASCADE` 只在项目整体删除时生效，
   那一刀本来就该带走整个项目的东西）。

状态变化写 `project_events` 的 `workflow.*`，复用 D-06 已验证的 SSE、游标与成员退出即关闭
的语义；事件负载里带的是**视图与摘要**（步数、是否有卡点、钉的是哪一版），不广播步骤正文。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
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

# ── 三张与桌面同源的表（见模块头第 2 条硬口径）──────────────────

WORKFLOW_KINDS = ("trigger", "action", "condition", "approval")
ACTION_KINDS = ("team_dispatch", "ticket_wake", "notify_host")
CONDITION_OPS = (
    "equals",
    "notEquals",
    "contains",
    "nonEmpty",
    "empty",
    "greaterThan",
    "lessThan",
    "isTrue",
    "isFalse",
)
ON_ERROR = ("stop", "retry", "continue")
APPROVAL_DECISIONS = ("allow", "deny")

#: 运行状态：与桌面 `workflow_runtime.RUN_STATES` 逐字一致。
RUN_STATES = (
    "running",
    "awaiting_step",
    "awaiting_approval",
    "blocked",
    "failed",
    "succeeded",
    "cancelled",
)
#: 还没收口的三种 + 装配缺件那一种：归档护栏与「等人的运行」清单都读这张表。
OPEN_RUN_STATES = ("running", "awaiting_step", "awaiting_approval", "blocked")
#: 需要人动手的两种：`blocked` 等人补装配，不等人点确认，所以不进「等你」那一屏。
ATTENTION_RUN_STATES = ("awaiting_approval",)

MAX_PER_PROJECT = 64
MAX_STEPS = 40
MAX_VERSIONS = 20
MAX_RUNS_PER_PROJECT = 2000
#: 重试次数上限：与桌面 `MAX_ATTEMPTS` 同值。服务端收下一本 attempts=99 的定义，
#: 桌面会说「超过上限」而拒绝它——共享当场断，所以这里也不能当合法。
MAX_ATTEMPTS = 5
#: 名字与运行标题的上限：**照桌面 `MAX_NAME_CHARS`（60）取齐**，不是按数据库列能放多少取。
#: 桌面把名字当「复用」时唯一认得出的那一行，超长是拒掉而不是截断（见 `_check_len`）；
#: 服务端要是收 120 字，就会出现「云端存得下一本桌面存不下的定义」，共享当场断。
MAX_NAME = 60
MAX_DESCRIPTION = 2000
#: 单个文本字段的上限：与桌面 `MAX_TEXT_CHARS` 同值（比它小会拒掉桌面存得下的定义，
#: 比它大则会让桌面把正文截了、两边存的不是同一本）。
MAX_TEXT = 2000
#: 步 id 长度上限：桌面 `_slug` 截到 64，服务端按同一长度拒（不改写内容，见 `_step_id`）。
MAX_STEP_ID = 64
MAX_TITLE = 60
#: 问题原文上限：与桌面 `MAX_QUESTION_CHARS` 同值——人要签字的那句话，两边都得是同一句。
MAX_QUESTION = 1000
MAX_NOTE = 2000
MAX_REF = 120
MAX_DEFINITION_JSON = 200_000
MAX_MEMBERS_PER_STEP = 16
LIST_DEFAULT = 50
LIST_MAX = 200

WORKFLOW_ID_RE = re.compile(r"awf-[0-9a-f]{16}")
RUN_ID_RE = re.compile(r"awr-[0-9a-f]{16}")
KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


# ── 建表：本层自带，不挤进 db.SCHEMA ────────────────────────────
#
# `db.py` 与 `main.py` 是并发共享地（别的窗口此刻正在里面写）。这里用
# `CREATE TABLE IF NOT EXISTS` 自成一段，运行期第一次用到时建一次；W4 要把它们折进
# `db.SCHEMA` 时，请**把下面这段原样搬过去**并删掉 `_schema_once`，别照着一份改写一遍
# ——两份 DDL 长得不一样时，旧卷升级那条「索引早于补列」的坑会重新出现。

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_workflows (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    definition_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    has_approval INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_workflows_key
    ON agent_workflows(project_id, key);
CREATE INDEX IF NOT EXISTS idx_agent_workflows_project
    ON agent_workflows(project_id, updated_at_ms DESC);

CREATE TABLE IF NOT EXISTS agent_workflow_revisions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES agent_workflows(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    definition_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    has_approval INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_workflow_revisions
    ON agent_workflow_revisions(workflow_id, version);
CREATE INDEX IF NOT EXISTS idx_agent_workflow_revisions_list
    ON agent_workflow_revisions(workflow_id, version DESC);

CREATE TABLE IF NOT EXISTS agent_workflow_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL REFERENCES agent_workflows(id) ON DELETE CASCADE,
    workflow_version INTEGER NOT NULL DEFAULT 1,
    workflow_digest TEXT NOT NULL DEFAULT '',
    room_ref TEXT NOT NULL DEFAULT '',
    ticket_ref TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'running',
    note TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    executed INTEGER NOT NULL DEFAULT 0,
    auto_passed INTEGER NOT NULL DEFAULT 0,
    started_by TEXT NOT NULL DEFAULT '',
    started_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_workflow_runs_project
    ON agent_workflow_runs(project_id, updated_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_agent_workflow_runs_workflow
    ON agent_workflow_runs(workflow_id, started_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_agent_workflow_runs_open
    ON agent_workflow_runs(state, updated_at_ms DESC);

CREATE TABLE IF NOT EXISTS agent_workflow_approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_workflow_runs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at_ms INTEGER NOT NULL
);
-- 同一步只能有一个结论：重复点确认在这里撞唯一索引，而不是靠客户端自觉
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_workflow_approvals
    ON agent_workflow_approvals(run_id, step_id);
CREATE INDEX IF NOT EXISTS idx_agent_workflow_approvals_run
    ON agent_workflow_approvals(run_id, decided_at_ms);
"""

_schema_ready = False


def _schema_once() -> None:
    """第一次用到本层时建一次表（`executescript` 自带提交，故单开一个事务）。"""
    global _schema_ready
    if _schema_ready:
        return
    with tx() as conn:
        conn.executescript(SCHEMA)
    _schema_ready = True


def reset_schema_flag() -> None:
    """测试换数据根后用：进程内建过表 ≠ 这个库里也有表。"""
    global _schema_ready
    _schema_ready = False


# ── 入参 ──────────────────────────────────────────────────────


class CamelIn(BaseModel):
    """JSON 入参用 camelCase；同时接受 snake_case，便于脚本与测试调用。"""

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class WorkflowIn(CamelIn):
    key: str = ""
    name: str
    description: str = ""
    steps: list[dict[str, Any]]
    expected_version: int = 0


class ArchiveIn(CamelIn):
    reason: str = ""
    force: bool = False


class RunIn(CamelIn):
    title: str = ""
    room_ref: str = ""
    ticket_ref: str = ""
    state: str = "running"
    note: str = ""
    steps: list[dict[str, Any]] = []
    executed: int = 0
    auto_passed: int = 0
    #: 引擎那份定义的摘要：留证要钉得住「跑的是哪一版内容」，光有版本号会被升版追过去
    definition_digest: str = ""


class RunReportIn(CamelIn):
    state: str
    note: str = ""
    steps: list[dict[str, Any]] = []
    executed: int = 0
    auto_passed: int = 0


class ApprovalIn(CamelIn):
    step_id: str
    decision: str
    note: str = ""
    question: str = ""


# ── 小工具 ────────────────────────────────────────────────────


@contextlib.contextmanager
def _payload_errors() -> Iterator[None]:
    """把本层清洗件的错误码收拢成 `WORKFLOW_INVALID`。

    `_clean_text` / `_clean_single_line` 是 projects 层的共享件（改它们会动别人的契约），
    抛的是 `PROJECT_INVALID_INPUT`。对交定义的人来说，这条 400 说的是「这本工作流不合法」，
    不是「项目参数错了」——客户端按 code 分支时不该被隔壁层的命名带偏。
    只包清洗段，不包 `_require_project`：项目 id 写错了确实该报 PROJECT_INVALID_INPUT。
    """
    try:
        yield
    except ApiError as exc:
        if exc.code != "PROJECT_INVALID_INPUT":
            raise
        raise ApiError(exc.status, "WORKFLOW_INVALID", exc.message, exc.headers) from exc


def _check_workflow_id(raw: str) -> str:
    workflow_id = str(raw or "").strip()
    if not WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise ApiError(404, "WORKFLOW_NOT_FOUND", "工作流不存在或你不是项目成员")
    return workflow_id


def _check_run_id(raw: str) -> str:
    run_id = str(raw or "").strip()
    if not RUN_ID_RE.fullmatch(run_id):
        raise ApiError(404, "WORKFLOW_RUN_NOT_FOUND", "运行记录不存在或你不是项目成员")
    return run_id


def _step_id(raw: Any, index: int, label: str = "") -> str:
    """步 id 折成单行后做键：与桌面 `_slug` 不同，这里**不改写**内容（改写会让两边
    对同一个 id 得出不同结论），只要求它本身能当键用。

    `label` 是给「不属于清单第 N 项」的那类调用（比如人给的卡点结论）换句人话：
    传 index=0 却报「第 1 步」会把人指到定义里没有的位置上。
    """
    who = label or f"第 {index + 1} 步"
    value = _clean_single_line(raw, f"{who}的 id", MAX_STEP_ID)
    if not value:
        raise ApiError(400, "WORKFLOW_INVALID", f"{who}没有 id：运行视图与条件引用都靠它寻址")
    if re.search(r"[./\s]", value):
        raise ApiError(
            400,
            "WORKFLOW_INVALID",
            f"步骤 id「{value[:40]}」含点号或空白：桌面会把它们归一成连字符，"
            "两边算出的 id 不一样，条件引用会指空",
        )
    return value


def _clean_string_list(raw: Any, field: str, limit: int) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > limit:
        raise ApiError(400, "WORKFLOW_INVALID", f"{field}要写成不超过 {limit} 项的数组")
    out: list[str] = []
    for item in raw:
        value = _clean_single_line(item, field, MAX_STEP_ID)
        if value and value not in out:
            out.append(value)
    if not out:
        raise ApiError(400, "WORKFLOW_INVALID", f"{field}不能全是空项")
    return out


def _edge(raw: Any, field: str) -> str:
    return _clean_single_line(raw, field, MAX_STEP_ID)


def _validate_steps(raw_steps: Any) -> tuple[list[dict[str, Any]], int, bool]:
    """结构校验：返回（归一后的 steps, 步数, 是否含人工卡点）。

    与桌面 `normalize_workflow` 同一套判据（ kinds / 动作 / 算子 / 分支指向 / 恰好一个触发）。
    这里不回显整张错误清单——服务端拒的就是「这本共享不出去」，一次说第一条就够，
    逐条体检请在桌面保存时看运行端的 errors。
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ApiError(400, "WORKFLOW_INVALID", "steps 要写成非空数组：没有节点的工作流共享出去只会是一条空运行")
    if len(raw_steps) > MAX_STEPS:
        raise ApiError(400, "WORKFLOW_INVALID", f"steps 有 {len(raw_steps)} 个、超过上限 {MAX_STEPS}：先拆成几本")

    steps: list[dict[str, Any]] = []
    has_approval = False
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise ApiError(400, "WORKFLOW_INVALID", f"第 {index + 1} 步不是对象")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in WORKFLOW_KINDS:
            raise ApiError(
                400, "WORKFLOW_INVALID",
                f"第 {index + 1} 步的 kind「{kind or '（空）'}」不在四种节点里（触发 / 动作 / 条件 / 人工卡点）",
            )
        sid = _step_id(item.get("id"), index)
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        raw_attempts = item.get("attempts")
        try:
            attempts = int(raw_attempts or 1)
        except (TypeError, ValueError):
            raise ApiError(
                400, "WORKFLOW_INVALID",
                f"步骤「{sid}」的 attempts「{str(raw_attempts)[:20]}」不是整数："
                "按 1 次处理意味着重试根本没开，桌面也拒同一本",
            ) from None
        if attempts > MAX_ATTEMPTS:
            raise ApiError(
                400, "WORKFLOW_INVALID",
                f"步骤「{sid}」要求重试 {attempts} 次、超过上限 {MAX_ATTEMPTS}："
                "这里不替你改小（改了就和桌面存的那本不是同一本），要重试更多请先拆步",
            )
        step: dict[str, Any] = {
            "id": sid,
            "kind": kind,
            "name": _clean_single_line(item.get("name"), f"步骤「{sid}」的名字", MAX_NAME) or sid,
            "next": _edge(item.get("next"), f"步骤「{sid}」的 next"),
            "onError": str(item.get("onError") or "stop").strip().lower() or "stop",
            "attempts": max(1, attempts),
            "spec": spec,
        }
        if step["onError"] not in ON_ERROR:
            raise ApiError(400, "WORKFLOW_INVALID", f"步骤「{sid}」的 onError 只能是 stop / retry / continue")
        if step["onError"] == "retry" and step["attempts"] < 2:
            raise ApiError(400, "WORKFLOW_INVALID", f"步骤「{sid}」开了 retry 却没给 attempts：重试一次等于没重试")

        if kind == "action":
            action = str(spec.get("action") or "").strip().lower()
            if action not in ACTION_KINDS:
                raise ApiError(
                    400, "WORKFLOW_INVALID",
                    f"步骤「{sid}」的动作「{action or '（空）'}」不在表里：动作只能是 "
                    f"{' / '.join(ACTION_KINDS)}（每一个都投进既有工单，本层不新造执行体）",
                )
            if action == "team_dispatch":
                _clean_string_list(spec.get("memberIds"), f"步骤「{sid}」的 memberIds",
                                   MAX_MEMBERS_PER_STEP)
                if not _clean_text(spec.get("brief"), f"步骤「{sid}」的 brief", MAX_TEXT):
                    raise ApiError(400, "WORKFLOW_INVALID", f"步骤「{sid}」是团队派工却没有 brief：成员看不到上游对话")
            else:
                if not _clean_text(spec.get("text"), f"步骤「{sid}」的正文", MAX_TEXT):
                    raise ApiError(400, "WORKFLOW_INVALID", f"步骤「{sid}」是 {action} 却没有正文：空消息连收件箱都进不去")
        elif kind == "condition":
            if step["next"]:
                raise ApiError(
                    400, "WORKFLOW_INVALID",
                    f"条件步骤「{sid}」同时给了 next 和两个分支：条件只按 nextTrue / nextFalse 走，"
                    "next 永远不会被用到",
                )
            for key in ("nextTrue", "nextFalse"):
                target = _edge(item.get(key), f"步骤「{sid}」的 {key}")
                if not target:
                    raise ApiError(400, "WORKFLOW_INVALID", f"条件步骤「{sid}」缺 {key}：两个分支必须各有去向")
                step[key] = target
            op = str(spec.get("op") or "").strip()
            canonical = next((name for name in CONDITION_OPS if name.lower() == op.lower()), "")
            if not canonical:
                raise ApiError(
                    400, "WORKFLOW_INVALID",
                    f"条件步骤「{sid}」的算子「{op[:30] or '（空）'}」不在九个里（{' / '.join(CONDITION_OPS)}）："
                    "这里只做数据比对，不接受表达式",
                )
            source = str(spec.get("source") or "").strip()
            if not source:
                raise ApiError(400, "WORKFLOW_INVALID", f"条件步骤「{sid}」没写 source：判断结果没有依据")
            if source != "payload" and not source.startswith("step:"):
                raise ApiError(400, "WORKFLOW_INVALID", f"条件步骤「{sid}」的 source 只认 payload 或 step:<步 id>.output")
            if source.startswith("step:") and not _edge(source[5:].removesuffix(".output"), "引用步 id"):
                raise ApiError(400, "WORKFLOW_INVALID", f"条件步骤「{sid}」的 source 里没有可用的步 id")
            step["spec"] = {**spec, "op": canonical}
        elif kind == "approval":
            has_approval = True
            deny = _edge(item.get("onDeny"), f"卡点「{sid}」的 onDeny")
            if deny:
                step["onDeny"] = deny
            # 问题原文按 `MAX_QUESTION`（= 桌面 `MAX_QUESTION_CHARS`）拒，不按正文的
            # `MAX_TEXT` 收：这一句是给人签字用的，桌面会拒的 lengths 云端不能当成合法，
            # 否则「共享」过去的那本卡点，桌面根本跑不了。
            step["question"] = _clean_text(
                item.get("question"), f"卡点「{sid}」的问题", MAX_QUESTION
            ) or "这一步需要有人确认后再继续"

        steps.append(step)

    ids = [s["id"] for s in steps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ApiError(400, "WORKFLOW_INVALID", f"步骤 id 重复：{'、'.join(dupes)}")
    triggers = [s for s in steps if s["kind"] == "trigger"]
    if len(triggers) != 1:
        raise ApiError(
            400, "WORKFLOW_INVALID",
            f"触发节点必须恰好一个，这里有 {len(triggers)} 个：运行要从哪儿起得只有一个答案",
        )
    known = set(ids)
    for step in steps:
        for key in ("next", "nextTrue", "nextFalse", "onDeny"):
            target = step.get(key) or ""
            if target and target not in known:
                raise ApiError(
                    400, "WORKFLOW_INVALID",
                    f"步骤「{step['id']}」的 {key} 指向「{target}」，定义里没有这一步：走到那儿就断了",
                )
    return steps, len(steps), has_approval


def _definition_digest(name: str, description: str, steps: list[dict[str, Any]]) -> str:
    """定义的规范化摘要：空白折叠后取 sha256，改一个字就对不上（与授权书摘要同一口径）。"""
    parts = [
        " ".join((name or "").split()),
        " ".join((description or "").split()),
        steps,
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _definition_bytes(steps: list[dict[str, Any]]) -> str:
    try:
        raw = json.dumps(steps, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "WORKFLOW_INVALID", f"steps 没法写成 JSON：{exc}") from exc
    if len(raw) > MAX_DEFINITION_JSON:
        raise ApiError(
            400, "WORKFLOW_INVALID",
            f"steps 有 {len(raw)} 字、超过 {MAX_DEFINITION_JSON}：一本流程不该靠往步骤里塞正文来长大",
        )
    return raw


def _key_of(payload: WorkflowIn, name: str) -> str:
    # 与 `_step_id` 同一个 stance：**不改写**交上来的键。静默小写会把桌面上两本不同的
    # 流程（"Daily" 与 "daily"）合成项目里的一本，那种合并没有任何人点头。
    raw = str(payload.key or "").strip() or None
    if raw is None:
        raw = "wf-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    if not KEY_RE.fullmatch(raw):
        raise ApiError(
            400, "WORKFLOW_INVALID",
            "key 只能是小写字母、数字、连字符与下划线（首字符要字母或数字），长度 ≤64："
            "本层不替你改写它（改写会把两本不同的流程合成一本），要大写就自己换成小写再交一次",
        )
    return raw


def _workflow_row(conn: sqlite3.Connection, project_id: str, workflow_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT w.*, u.nickname AS creator_nickname FROM agent_workflows w "
        "LEFT JOIN users u ON u.uid = w.created_by "
        "WHERE w.project_id = ?1 AND w.id = ?2",
        (project_id, workflow_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "WORKFLOW_NOT_FOUND", "工作流不存在或你不是项目成员")
    return row


def _run_row(conn: sqlite3.Connection, project_id: str, run_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT r.*, w.name AS workflow_name FROM agent_workflow_runs r "
        "JOIN agent_workflows w ON w.id = r.workflow_id "
        "WHERE r.project_id = ?1 AND r.id = ?2",
        (project_id, run_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "WORKFLOW_RUN_NOT_FOUND", "运行记录不存在或你不是项目成员")
    return row


def _steps_of(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        value = json.loads(row["definition_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _run_steps_of(row: sqlite3.Row) -> list[dict[str, Any]]:
    try:
        value = json.loads(row["steps_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _workflow_view(row: sqlite3.Row, runs: int = 0, open_runs: int = 0) -> dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "key": row["key"],
        "name": row["name"],
        "description": row["description"],
        "version": row["version"],
        "digest": row["digest"],
        "stepCount": row["step_count"],
        "hasApproval": bool(row["has_approval"]),
        "archived": bool(row["archived"]),
        "createdBy": row["created_by"],
        "creatorNickname": row["creator_nickname"] if "creator_nickname" in row.keys() else "",
        "updatedBy": row["updated_by"],
        "createdAtMs": row["created_at_ms"],
        "updatedAtMs": row["updated_at_ms"],
        "runCount": runs,
        "openRunCount": open_runs,
        "steps": _steps_of(row),
    }


def _revision_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workflowId": row["workflow_id"],
        "version": row["version"],
        "name": row["name"],
        "description": row["description"],
        "digest": row["digest"],
        "stepCount": row["step_count"],
        "hasApproval": bool(row["has_approval"]),
        "createdBy": row["created_by"],
        "createdAtMs": row["created_at_ms"],
        "steps": _steps_of(row),
        # 历史版是只读的：能看、能比对，不能拿它起新的运行
        "historic": True,
    }


def _run_view(row: sqlite3.Row, approvals: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "workflowId": row["workflow_id"],
        "workflowName": row["workflow_name"] if "workflow_name" in row.keys() else "",
        "workflowVersion": row["workflow_version"],
        "workflowDigest": row["workflow_digest"],
        "roomRef": row["room_ref"],
        "ticketRef": row["ticket_ref"],
        "title": row["title"],
        "state": row["state"],
        "stateLabel": STATE_LABELS.get(row["state"], row["state"]),
        "note": row["note"],
        "executed": row["executed"],
        "autoPassed": row["auto_passed"],
        "startedBy": row["started_by"],
        "startedAtMs": row["started_at_ms"],
        "updatedAtMs": row["updated_at_ms"],
        "endedAtMs": row["ended_at_ms"],
        "open": bool(row["state"] in OPEN_RUN_STATES),
        "needsHuman": bool(row["state"] in ATTENTION_RUN_STATES),
        "steps": _run_steps_of(row),
        "approvals": approvals if approvals is not None else [],
        "humanPassed": len([a for a in (approvals or []) if a["decision"] in APPROVAL_DECISIONS]),
    }


STATE_LABELS = {
    "running": "在跑",
    "awaiting_step": "等执行体回执",
    "awaiting_approval": "等人确认",
    "blocked": "停在装配缺件",
    "failed": "失败",
    "succeeded": "已跑完",
    "cancelled": "已取消",
}


def _approval_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "stepId": row["step_id"],
        "question": row["question"],
        "decision": row["decision"],
        "note": row["note"],
        "decidedBy": row["decided_by"],
        "decidedAtMs": row["decided_at_ms"],
        # 这张表里出现过的每一条都是人真按下的：自动放行根本不写这里
        "human": True,
    }


def _compact_view(row: sqlite3.Row) -> dict[str, Any]:
    """进事件负载的版本：只有身份与摘要，没有步骤正文。

    步骤正文里可能夹着内部链接、客户名与 brief 全文；项目事件会 SSE 推给每个在线成员，
    所以广播用视图，详情靠 GET。
    """
    return {
        "id": row["id"],
        "name": row["name"],
        "key": row["key"],
        "version": row["version"],
        "digest": row["digest"],
        "stepCount": row["step_count"],
        "hasApproval": bool(row["has_approval"]),
        "archived": bool(row["archived"]),
        "updatedAtMs": row["updated_at_ms"],
    }


def _counts(conn: sqlite3.Connection, project_id: str, workflow_ids: list[str]) -> dict[str, tuple[int, int]]:
    """一条 SQL 数完所有本子的运行数与未收口数：逐本查在项目大了以后会拖到秒级。

    占位符全部显式编号：`IN (…)` 里混用裸 `?` 时，SQLite 会把它排到已见最大编号之后
    （`?2` 之后的裸 `?` 变成 `?3`），参数元组按「先 id 后项目」排就错位——第一次踩到时
    表现为 `Incorrect number of bindings`，写成编号就没有这一步。
    """
    if not workflow_ids:
        return {}
    marks = ", ".join(f"?{i + 2}" for i in range(len(workflow_ids)))
    rows = conn.execute(
        f"SELECT workflow_id, COUNT(*) AS total, "
        f"SUM(CASE WHEN state IN {OPEN_RUN_STATES} THEN 1 ELSE 0 END) AS open_runs "
        f"FROM agent_workflow_runs WHERE project_id = ?1 AND workflow_id IN ({marks}) "
        f"GROUP BY workflow_id",
        (project_id, *workflow_ids),
    ).fetchall()
    return {r["workflow_id"]: (int(r["total"]), int(r["open_runs"] or 0)) for r in rows}


# ── 定义：存 / 列 / 取 / 归档 ──────────────────────────────────


@router.post("/api/v1/projects/{project_id}/workflows", status_code=201)
def api_save_workflow(
    project_id: str, payload: WorkflowIn, user: dict = Depends(require_user)
):
    """存一本定义：新 key 建第一版，已有 key 升一版。

    `expectedVersion` 是跨设备写冲突的唯一防线：改一本已存在的流程**必须**带它，对不上就
    409，谁也不会把自己的版本静默盖到别人头上。缺省 0 的语义是**「只在这本还不存在时才写」**
    ——第一次建本不必先读一遍。唯一的例外是内容摘要与当前版一字不差：那按重复提交处理，
    不升版也不发事件（超时重试不该长出一个新版本，把版本号变成不可信的东西）。
    """
    _schema_once()
    with _payload_errors():
        name = _clean_single_line(payload.name, "工作流名字", MAX_NAME, required=True)
        description = _clean_text(payload.description, "工作流说明", MAX_DESCRIPTION)
        steps, step_count, has_approval = _validate_steps(payload.steps)
        definition = _definition_bytes(steps)
        digest = _definition_digest(name, description, steps)
        key = _key_of(payload, name)

    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        current = conn.execute(
            "SELECT * FROM agent_workflows WHERE project_id = ?1 AND key = ?2",
            (project["id"], key),
        ).fetchone()
        ts = now_ms()
        if current is None:
            if payload.expected_version and payload.expected_version != 0:
                raise ApiError(409, "WORKFLOW_NOT_FOUND_EXPECTED",
                               f"你期望这本已经存在（expectedVersion={payload.expected_version}），"
                               f"但项目里没有 key=「{key}」：先确认是不是另一台设备把它归档或改名了")
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_workflows WHERE project_id = ?1", (project["id"],)
            ).fetchone()["n"]
            if int(total) >= MAX_PER_PROJECT:
                raise ApiError(
                    409, "WORKFLOW_LIMIT",
                    f"项目里已有 {total} 本工作流、到上限 {MAX_PER_PROJECT}：这一本没有落盘。"
                    "先把不再用的归档，别靠挤掉旧的腾地方——被挤掉的那本可能正有运行挂在某一步上",
                )
            workflow_id = f"awf-{_hex()}"
            conn.execute(
                "INSERT INTO agent_workflows "
                "(id, project_id, key, name, description, version, definition_json, digest, "
                "step_count, has_approval, archived, created_by, updated_by, created_at_ms, updated_at_ms) "
                "VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6, ?7, ?8, ?9, 0, ?10, ?10, ?11, ?11)",
                (workflow_id, project["id"], key, name, description, definition, digest,
                 step_count, 1 if has_approval else 0, user["uid"], ts),
            )
            event = "workflow.created"
        else:
            if digest == str(current["digest"]) and not bool(current["archived"]):
                # 内容一字未改的重复提交（最常见的是网络超时后的重试）：不升版、不发事件，
                # 把现状原样回回去——重试长出第二个版本，等于让客户端自己都不敢信版本号。
                same_counts = _counts(conn, project["id"], [current["id"]]).get(
                    current["id"], (0, 0))
                return {"ok": True, "workflow": _workflow_view(current, *same_counts),
                        "digest": digest, "seq": 0, "created": False, "unchanged": True}
            want = int(payload.expected_version or 0)
            if want == 0:
                raise ApiError(
                    409, "WORKFLOW_VERSION_REQUIRED",
                    f"这本在项目里已经是第 {current['version']} 版：改一本已存在的流程必须带 "
                    "expectedVersion（留空的语义是「只在这本还不存在时才写」）——"
                    "不带版本号盖下去，跨设备时没人知道自己覆盖了谁",
                )
            if want != int(current["version"]):
                raise ApiError(
                    409, "WORKFLOW_VERSION_CONFLICT",
                    f"这本定义在项目里已经是第 {current['version']} 版，你改的是第 {payload.expected_version} 版："
                    "没有覆盖任何人的版本。先读最新版再改，别把两台的编辑合成一版",
                )
            workflow_id = current["id"]
            version = int(current["version"]) + 1
            revision_id = f"awv-{_hex()}"
            # 旧版先进历史再改当前行：中途失败时事务回滚，不会出现「当前版没了、历史里也没有」
            conn.execute(
                "INSERT INTO agent_workflow_revisions "
                "(id, workflow_id, project_id, version, name, description, definition_json, digest, "
                "step_count, has_approval, created_by, created_at_ms) "
                "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
                (revision_id, workflow_id, project["id"], current["version"], current["name"],
                 current["description"], current["definition_json"], current["digest"],
                 current["step_count"], current["has_approval"], current["updated_by"] or current["created_by"],
                 current["updated_at_ms"]),
            )
            conn.execute(
                "UPDATE agent_workflows SET name = ?2, description = ?3, version = ?4, "
                "definition_json = ?5, digest = ?6, step_count = ?7, has_approval = ?8, "
                "archived = 0, updated_by = ?9, updated_at_ms = ?10 WHERE id = ?1",
                (workflow_id, name, description, version, definition, digest, step_count,
                 1 if has_approval else 0, user["uid"], ts),
            )
            _trim_revisions(conn, workflow_id)
            event = "workflow.versioned"
        row = _workflow_row(conn, project["id"], workflow_id)
        seq = _emit(conn, project["id"], user["uid"], event, {"workflow": _compact_view(row)})
        return {"ok": True, "workflow": _workflow_view(row), "digest": digest, "seq": seq,
                "created": event == "workflow.created"}


@router.get("/api/v1/projects/{project_id}/workflows")
def api_list_workflows(
    project_id: str,
    user: dict = Depends(require_user),
    include_archived: bool = Query(False, alias="includeArchived"),
    limit: int = Query(LIST_DEFAULT, ge=1, le=LIST_MAX),
):
    """项目里的工作流清单（界面画卡片用的那几样：步数、是否有卡点、跑过几次、还挂着几条）。"""
    _schema_once()
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        where = "w.project_id = ?1"
        if not include_archived:
            where += " AND w.archived = 0"
        rows = conn.execute(
            f"SELECT w.*, u.nickname AS creator_nickname FROM agent_workflows w "
            f"LEFT JOIN users u ON u.uid = w.created_by WHERE {where} "
            f"ORDER BY w.updated_at_ms DESC LIMIT ?2",
            (project["id"], limit),
        ).fetchall()
        counts = _counts(conn, project["id"], [r["id"] for r in rows])
        items = [
            _workflow_view(r, *(counts.get(r["id"]) or (0, 0))) for r in rows
        ]
    return {"ok": True, "workflows": items, "count": len(items)}


@router.get("/api/v1/projects/{project_id}/workflows/{workflow_id}")
def api_workflow_detail(
    project_id: str,
    workflow_id: str,
    user: dict = Depends(require_user),
    version: int = Query(0, ge=0, le=10_000),
):
    """一本定义；`version` 填了就是取那一版历史（只读，并如实标 historic）。"""
    _schema_once()
    workflow_id = _check_workflow_id(workflow_id)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        row = _workflow_row(conn, project["id"], workflow_id)
        counts = _counts(conn, project["id"], [workflow_id])
        if not version or int(version) == int(row["version"]):
            return {"ok": True, "historic": False,
                    "workflow": _workflow_view(row, *(counts.get(workflow_id) or (0, 0))),
                    "versions": _revision_list(conn, workflow_id)}
        rev = conn.execute(
            "SELECT * FROM agent_workflow_revisions WHERE workflow_id = ?1 AND version = ?2",
            (workflow_id, version),
        ).fetchone()
        if rev is None:
            raise ApiError(
                404, "WORKFLOW_VERSION_UNAVAILABLE",
                f"第 {version} 版取不到：只保留最近 {MAX_VERSIONS} 版历史，"
                "更早的那几版不在盘上了（不退回当前版糊过去——那会把人引到一份他没写过的定义上）",
            )
        return {"ok": True, "historic": True, "revision": _revision_view(rev),
                "workflow": _workflow_view(row, *(counts.get(workflow_id) or (0, 0)), )}


def _revision_list(conn: sqlite3.Connection, workflow_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_workflow_revisions WHERE workflow_id = ?1 ORDER BY version DESC",
        (workflow_id,),
    ).fetchall()
    return [
        {"version": r["version"], "digest": r["digest"], "name": r["name"],
         "stepCount": r["step_count"], "hasApproval": bool(r["has_approval"]),
         "createdAtMs": r["created_at_ms"], "createdBy": r["created_by"]}
        for r in rows
    ]


def _trim_revisions(conn: sqlite3.Connection, workflow_id: str) -> None:
    """历史窗口有界：只留最近 MAX_VERSIONS 版。"""
    conn.execute(
        "DELETE FROM agent_workflow_revisions WHERE workflow_id = ?1 AND version NOT IN ("
        "SELECT version FROM agent_workflow_revisions WHERE workflow_id = ?1 "
        "ORDER BY version DESC LIMIT ?2)",
        (workflow_id, MAX_VERSIONS),
    )


@router.post("/api/v1/projects/{project_id}/workflows/{workflow_id}/archive", status_code=201)
def api_archive_workflow(
    project_id: str, workflow_id: str, payload: ArchiveIn, user: dict = Depends(require_user)
):
    """归档一本定义。还挂着未收口运行时要 `force`，且 force 也只是归档——运行历史不跟着走。"""
    _schema_once()
    workflow_id = _check_workflow_id(workflow_id)
    with _payload_errors():
        reason = _clean_text(payload.reason, "归档原因", MAX_NOTE)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        row = _workflow_row(conn, project["id"], workflow_id)
        if row["created_by"] != user["uid"] and project["my_role"] not in (ROLE_OWNER, ROLE_ADMIN):
            # 归档是把别人正在复用的流程从清单里拿走：创建者或管理员点头才行
            raise ApiError(403, "WORKFLOW_FORBIDDEN", "只有创建者或项目管理员能归档这本工作流")
        open_runs = conn.execute(
            f"SELECT COUNT(*) AS n FROM agent_workflow_runs WHERE workflow_id = ?1 "
            f"AND state IN {OPEN_RUN_STATES}",
            (workflow_id,),
        ).fetchone()["n"]
        if int(open_runs) and not payload.force:
            raise ApiError(
                409, "WORKFLOW_RUNS_OPEN",
                f"这本还挂着 {open_runs} 条未收口的运行：归档后那些卡点没人认得了。"
                "要么先取消运行，要么带 force 明确承认它们留在盘上",
            )
        ts = now_ms()
        conn.execute(
            "UPDATE agent_workflows SET archived = 1, updated_by = ?2, updated_at_ms = ?3 WHERE id = ?1",
            (workflow_id, user["uid"], ts),
        )
        fresh = _workflow_row(conn, project["id"], workflow_id)
        seq = _emit(conn, project["id"], user["uid"], "workflow.archived",
                    {"workflow": _compact_view(fresh), "openRuns": int(open_runs),
                     "forced": bool(payload.force)})
        total_runs, still_open = _counts(conn, project["id"], [workflow_id]).get(
            workflow_id, (0, 0))
        return {"ok": True, "workflow": _workflow_view(fresh, total_runs, still_open),
                "openRuns": int(open_runs), "note": "定义归档，运行记录一条没删：「查看每次运行」还在",
                "seq": seq}


# ── 运行：留证 / 上报 / 人工卡点结论 ────────────────────────────


@router.post("/api/v1/projects/{project_id}/workflows/{workflow_id}/runs", status_code=201)
def api_start_run(
    project_id: str, workflow_id: str, payload: RunIn, user: dict = Depends(require_user)
):
    """记一次运行的开始。

    本层不执行、不判定，起运行的那台桌面把**它已经算好的**状态交上来留证：钉住哪一版
    （`workflowVersion` + `workflowDigest`）、在哪个房间、用哪张工单。
    """
    _schema_once()
    workflow_id = _check_workflow_id(workflow_id)
    with _payload_errors():
        title = _clean_single_line(payload.title, "运行标题", MAX_TITLE)
        room_ref = _clean_single_line(payload.room_ref, "房间引用", MAX_REF)
        ticket_ref = _clean_single_line(payload.ticket_ref, "工单引用", MAX_REF)
        note = _clean_text(payload.note, "说明", MAX_NOTE)
        state = str(payload.state or "running").strip().lower()
        if state not in RUN_STATES:
            raise ApiError(400, "WORKFLOW_INVALID", f"运行状态「{state[:30]}」不在表里（{' / '.join(RUN_STATES)}）")
        steps = _run_steps_payload(payload.steps)
        definition_digest = _clean_single_line(payload.definition_digest, "定义摘要", 80)

    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        row = _workflow_row(conn, project["id"], workflow_id)
        if bool(row["archived"]):
            raise ApiError(409, "WORKFLOW_ARCHIVED", "这本已经归档：不再起新的运行（已在跑的那些照常记账）")
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_workflow_runs WHERE project_id = ?1", (project["id"],)
        ).fetchone()["n"]
        if int(total) >= MAX_RUNS_PER_PROJECT:
            raise ApiError(
                409, "WORKFLOW_RUN_LIMIT",
                f"项目里已有 {total} 条运行记录、到上限 {MAX_RUNS_PER_PROJECT}：这条没有落盘。"
                "「查看每次运行」不能靠丢掉旧的那几次来腾地方",
            )
        digest = definition_digest or row["digest"]
        if digest != row["digest"]:
            # 允许把运行钉在历史版上（跑了一半才升版是常态），但那一版必须真在盘上
            rev = conn.execute(
                "SELECT * FROM agent_workflow_revisions WHERE workflow_id = ?1 AND digest = ?2",
                (workflow_id, digest),
            ).fetchone()
            if rev is None:
                raise ApiError(
                    409, "WORKFLOW_DIGEST_UNKNOWN",
                    "这份定义摘要在本项目里查不到（既不是当前版也不在保留的历史里）："
                    "运行得钉得住是哪一版内容，否则事后读账对不上",
                )
            version = int(rev["version"])
        else:
            version = int(row["version"])
        run_id = f"awr-{_hex()}"
        ts = now_ms()
        conn.execute(
            "INSERT INTO agent_workflow_runs "
            "(id, project_id, workflow_id, workflow_version, workflow_digest, room_ref, ticket_ref, "
            "title, state, note, steps_json, executed, auto_passed, started_by, started_at_ms, "
            "updated_at_ms, ended_at_ms) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?15, ?16)",
            (run_id, project["id"], workflow_id, version, digest, room_ref, ticket_ref,
             title or row["name"], state, note, steps, max(0, int(payload.executed or 0)),
             max(0, int(payload.auto_passed or 0)), user["uid"], ts,
             ts if state in RUN_STATES and state not in OPEN_RUN_STATES else 0),
        )
        created = _run_row(conn, project["id"], run_id)
        seq = _emit(conn, project["id"], user["uid"], "workflow.run.started",
                    {"runId": run_id, "workflow": _compact_view(row), "state": state,
                     "workflowVersion": version, "title": created["title"]})
        return {"ok": True, "run": _run_view(created), "seq": seq}


def _run_steps_payload(raw: list[dict[str, Any]]) -> str:
    """运行视图按条收下，只限体积不限形状：那是引擎算出来的东西，本层不重算。"""
    if not isinstance(raw, list):
        raise ApiError(400, "WORKFLOW_INVALID", "steps 要写成数组")
    if len(raw) > MAX_STEPS:
        raise ApiError(400, "WORKFLOW_INVALID", f"运行视图最多 {MAX_STEPS} 步")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "WORKFLOW_INVALID", f"运行视图没法写成 JSON：{exc}") from exc
    if len(encoded) > MAX_DEFINITION_JSON:
        raise ApiError(400, "WORKFLOW_INVALID", f"运行视图超过 {MAX_DEFINITION_JSON} 字：这一步的正文别往留证里灌")
    return encoded


@router.post("/api/v1/projects/{project_id}/workflow-runs/{run_id}/report")
def api_report_run(
    project_id: str, run_id: str, payload: RunReportIn, user: dict = Depends(require_user)
):
    """更新一条运行的视图（引擎每次推进之后上报一次）。

    收的是已经算好的状态，本层不判定下一步。重复上报同一份内容不重复发事件；
    已收口的运行不许再改状态——那会把「跑完了」改写成「没跑完」。
    """
    _schema_once()
    run_id = _check_run_id(run_id)
    with _payload_errors():
        state = str(payload.state or "").strip().lower()
        if state not in RUN_STATES:
            raise ApiError(400, "WORKFLOW_INVALID", f"运行状态「{state[:30]}」不在表里（{' / '.join(RUN_STATES)}）")
        note = _clean_text(payload.note, "说明", MAX_NOTE)
        steps = _run_steps_payload(payload.steps)

    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        row = _run_row(conn, project["id"], run_id)
        before = (row["state"], row["steps_json"], row["note"], int(row["executed"]), int(row["auto_passed"]))
        if row["state"] not in OPEN_RUN_STATES and state in OPEN_RUN_STATES:
            raise ApiError(
                409, "WORKFLOW_RUN_CLOSED",
                f"这条运行已经收口（{row['state']}），不能再改回未收口（{state}）："
                "跑完就是跑完，事后把账改回去的口子不能留",
            )
        ts = now_ms()
        closed = state not in OPEN_RUN_STATES
        conn.execute(
            "UPDATE agent_workflow_runs SET state = ?2, note = ?3, steps_json = ?4, "
            "executed = ?5, auto_passed = ?6, updated_at_ms = ?7, "
            "ended_at_ms = CASE WHEN ?8 = 1 THEN ?7 ELSE ended_at_ms END WHERE id = ?1",
            (run_id, state, note, steps, max(0, int(payload.executed or 0)),
             max(0, int(payload.auto_passed or 0)), ts, 1 if closed else 0),
        )
        after = _run_row(conn, project["id"], run_id)
        changed = before != (after["state"], after["steps_json"], after["note"],
                             int(after["executed"]), int(after["auto_passed"]))
        seq = 0
        if changed:
            seq = _emit(conn, project["id"], user["uid"], "workflow.run.updated",
                        {"runId": run_id, "state": state, "workflowId": after["workflow_id"],
                         "workflowVersion": after["workflow_version"], "note": note[:200]})
        return {"ok": True, "run": _run_view(after, _approvals_of(conn, run_id)),
                "changed": changed, "seq": seq}


def _approvals_of(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_workflow_approvals WHERE run_id = ?1 ORDER BY decided_at_ms",
        (run_id,),
    ).fetchall()
    return [_approval_view(r) for r in rows]


@router.get("/api/v1/projects/{project_id}/workflows/{workflow_id}/runs")
def api_list_runs(
    project_id: str,
    workflow_id: str,
    user: dict = Depends(require_user),
    state: str = Query("", max_length=32),
    limit: int = Query(LIST_DEFAULT, ge=1, le=LIST_MAX),
):
    """「查看每次运行」：跨设备也答得出（读的全是服务端这张表，不是某个进程的内存箱）。"""
    _schema_once()
    workflow_id = _check_workflow_id(workflow_id)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        _workflow_row(conn, project["id"], workflow_id)
        where = "r.workflow_id = ?1"
        args: list[Any] = [workflow_id]
        if state:
            if state not in RUN_STATES:
                raise ApiError(400, "WORKFLOW_INVALID", "运行状态不合法")
            where += " AND r.state = ?2"
            args.append(state)
        args.append(limit)
        rows = conn.execute(
            f"SELECT r.*, w.name AS workflow_name FROM agent_workflow_runs r "
            f"JOIN agent_workflows w ON w.id = r.workflow_id WHERE {where} "
            f"ORDER BY r.started_at_ms DESC LIMIT ?{len(args)}",
            args,
        ).fetchall()
        items = [_run_view(r, _approvals_of(conn, r["id"])) for r in rows]
    return {"ok": True, "runs": items, "count": len(items)}


@router.get("/api/v1/projects/{project_id}/workflow-runs/{run_id}")
def api_run_detail(project_id: str, run_id: str, user: dict = Depends(require_user)):
    """单条运行：步视图 + 人工卡点的结论（谁点的、说了什么，事后要认得出）。"""
    _schema_once()
    run_id = _check_run_id(run_id)
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_VIEWER)
        row = _run_row(conn, project["id"], run_id)
        return {"ok": True, "run": _run_view(row, _approvals_of(conn, run_id))}


@router.post("/api/v1/projects/{project_id}/workflow-runs/{run_id}/approve", status_code=201)
def api_approve_run(
    project_id: str, run_id: str, payload: ApprovalIn, user: dict = Depends(require_user)
):
    """人已给的人工卡点结论。

    三条口径：**只认人**（档位在 off 时那种自动放行由引擎写在 `autoPassed` 计数里，
    绝不进这张表）、**同一步只能一次**（撞唯一索引就是 409，不应用第二次）、
    **不推进运行**（下一步走哪条分支由引擎按定义算，这里只记账）。
    """
    _schema_once()
    run_id = _check_run_id(run_id)
    with _payload_errors():
        step_id = _step_id(payload.step_id, 0, "卡点步骤")
        decision = str(payload.decision or "").strip().lower()
        if decision not in APPROVAL_DECISIONS:
            raise ApiError(400, "WORKFLOW_INVALID", "结论只能是 allow 或 deny：这里没有「跳过」这种第三种答案")
        note = _clean_text(payload.note, "备注", MAX_NOTE)
        question = _clean_text(payload.question, "问题原文", MAX_QUESTION)

    deferred: ApiError | None = None
    with tx() as conn:
        project = _require_project(conn, project_id, user["uid"], ROLE_MEMBER)
        row = _run_row(conn, project["id"], run_id)
        if row["state"] != "awaiting_approval":
            raise ApiError(
                409, "WORKFLOW_RUN_NOT_AWAITING",
                f"这条运行现在不是「等人」状态（{row['state']}）：重复点确认不能再推一步，"
                "否则一份定义会跑出两条前进",
            )
        # 结论必须落在一个真卡点上：拿步骤名字蒙一步是桌面层最容易犯的错
        definition = _definition_for_run(conn, row)
        if definition is None:
            # 这一支只**记下**要抛的 409，不在事务里 raise：`with conn` 见到异常会回滚，
            # 那 "运行已按 blocked 记账" 就成了假话——状态没落盘，人还以为点坏了。
            deferred = _mark_blocked_without_definition(conn, project, user, run_id)
        else:
            step = next((s for s in _steps_of(definition) if s.get("kind") == "approval"
                         and str(s.get("id")) == step_id), None)
            if step is None:
                raise ApiError(
                    404, "WORKFLOW_STEP_NOT_APPROVAL",
                    f"步骤「{step_id}」在这一版定义里不是人工卡点：不能替别的步给结论",
                )
            existing = conn.execute(
                "SELECT * FROM agent_workflow_approvals WHERE run_id = ?1 AND step_id = ?2",
                (run_id, step_id),
            ).fetchone()
            if existing is not None:
                raise ApiError(
                    409, "WORKFLOW_ALREADY_DECIDED",
                    f"这一步人已经给过结论（{existing['decision']}，{existing['decided_by']}）："
                    "同一个卡点第二次点头不算第二次批准，也不许改口——要改只能重跑一次",
                )
            ts = now_ms()
            approval_id = f"awa-{_hex()}"
            conn.execute(
                "INSERT INTO agent_workflow_approvals "
                "(id, run_id, project_id, step_id, question, decision, note, decided_by, decided_at_ms) "
                "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                (approval_id, run_id, project["id"], step_id,
                 question or str(step.get("question") or ""), decision, note, user["uid"], ts),
            )
            after = _run_row(conn, project["id"], run_id)
            seq = _emit(conn, project["id"], user["uid"], "workflow.approval.decided",
                        {"runId": run_id, "workflowId": after["workflow_id"], "stepId": step_id,
                         "decision": decision, "by": user["uid"]})
            result = {"ok": True, "approval": _approval_view(conn.execute(
                "SELECT * FROM agent_workflow_approvals WHERE id = ?1", (approval_id,)).fetchone()),
                "run": _run_view(after, _approvals_of(conn, run_id)),
                "note": "结论已记账；运行仍在等人——下一步往哪儿走由起运行的那台桌面按定义推进",
                "seq": seq}
            return result
    if deferred is not None:
        raise deferred


def _mark_blocked_without_definition(
    conn: sqlite3.Connection, project: sqlite3.Row, user: dict, run_id: str
) -> ApiError:
    """把运行按 blocked 收口并记账，返回**待抛**的 409（不在这里 raise，见调用处注释）。

    状态改了、结论没应用：桌面层同口径（`workflow_runtime._orphaned`），两边都不许在
    「钉不住的那一版取不到」时退回当前版继续跑。
    """
    conn.execute(
        "UPDATE agent_workflow_runs SET state = 'blocked', note = ?2, updated_at_ms = ?3 "
        "WHERE id = ?1",
        (run_id, "钉的那一版定义取不到了（被归档清理或滚出保留窗口）：这条运行停在这儿，"
                 "不猜一份定义把它推完", now_ms()),
    )
    after = _run_row(conn, project["id"], run_id)
    _emit(conn, project["id"], user["uid"], "workflow.run.updated",
          {"runId": run_id, "state": "blocked", "reason": "definition_unavailable",
           "workflowId": after["workflow_id"]})
    return ApiError(
        409, "WORKFLOW_DEFINITION_UNAVAILABLE",
        "这条运行钉的那一版定义已经取不到（被清理或滚出保留窗口）："
        "你给的结论没有应用，运行已按 blocked 记账——不猜一份定义把它推完",
        headers={"X-Run-State": after["state"]},
    )


def _definition_for_run(conn: sqlite3.Connection, run: sqlite3.Row) -> sqlite3.Row | None:
    """这条运行当初钉的是哪一版（当前版或保留窗口里的历史版）。

    取不到就返回 None：调用方**不能退回当前版**——那等于一次升版把在跑的运行悄悄改道到
    新的步骤形状上，桌面层同口径（见 `workflow_runtime._definition_for_run`）。
    """
    digest = run["workflow_digest"]
    current = conn.execute(
        "SELECT * FROM agent_workflows WHERE id = ?1 AND digest = ?2",
        (run["workflow_id"], digest),
    ).fetchone()
    if current is not None:
        return current
    rev = conn.execute(
        "SELECT * FROM agent_workflow_revisions WHERE workflow_id = ?1 AND digest = ?2 LIMIT 1",
        (run["workflow_id"], digest),
    ).fetchone()
    if rev is not None:
        return rev
    # 摘要对不上时再按版本号兜一次：旧版客户端可能只报了版本没报摘要
    if int(run["workflow_version"] or 0) > 0:
        by_version = conn.execute(
            "SELECT * FROM agent_workflow_revisions WHERE workflow_id = ?1 AND version = ?2",
            (run["workflow_id"], run["workflow_version"]),
        ).fetchone()
        if by_version is not None:
            return by_version
    return None


@router.get("/api/v1/agent/workflow-runs")
def api_my_runs(
    user: dict = Depends(require_user),
    needs_human: bool = Query(True, alias="needsHuman"),
    limit: int = Query(LIST_DEFAULT, ge=1, le=LIST_MAX),
):
    """跨项目的「等工作流运行」。

    默认只列需要人动手那一种（等人确认）。`blocked`（装配缺件）不在内：它等人补的是
    装配，不是点一个确认按钮，混进这一屏会让人以为点一下就能走。
    """
    _schema_once()
    states = ATTENTION_RUN_STATES if needs_human else OPEN_RUN_STATES
    # 同 `_counts`：状态清单与 LIMIT 全部显式编号，不靠裸 `?` 的自动排位
    marks = ", ".join(f"?{i + 2}" for i in range(len(states)))
    with tx() as conn:
        rows = conn.execute(
            f"SELECT r.*, w.name AS workflow_name FROM agent_workflow_runs r "
            f"JOIN agent_workflows w ON w.id = r.workflow_id "
            f"JOIN project_members pm ON pm.project_id = r.project_id AND pm.uid = ?1 AND pm.removed = 0 "
            f"WHERE r.state IN ({marks}) ORDER BY r.updated_at_ms DESC LIMIT ?{len(states) + 2}",
            (user["uid"], *states, limit),
        ).fetchall()
        items = [_run_view(r, _approvals_of(conn, r["id"])) for r in rows]
    return {"ok": True, "runs": items, "count": len(items)}


def _hex() -> str:
    import secrets

    return secrets.token_hex(8)
