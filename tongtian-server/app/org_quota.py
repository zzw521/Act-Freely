"""组织与两级配额（D-23 的服务端半边）。

**两级**：组织总额（月度 token 预算 + 并发上限）在上，成员级在下
（成员级又是「个人显式覆盖 > 组织成员默认 > 角色缺省」三档，前两档就是「两级配额」的落点）。

四条口径先写在这里，因为它们决定了整个模块的行为：

1. **0 表示不限制**，不是表示「零额度」。这与引擎 `_quota_error` 的判定一致
   （`monthly > 0` 才拦）。所以「把成员上限收敛到剩余预算」时**绝不允许写出 0**——
   收敛出 0 等于把限制**取消**，方向正好反了。剩余为 0 时唯一的正确动作是**不发令牌**。
2. **组织已用按 `usage_rollup` 行上盖章的 `org_id` 归集**，不是按当前成员名单现算。
   上报时刻人在哪个组织，那批用量就记在哪个组织；成员移入移出不追溯改写历史。
   这条与部门报表（`/api/v1/admin/usage/departments`）完全同源，本模块直接复用它的
   行过滤，保证「逐部门相加 = 组织总量 = 个人明细之和」不是三套算法。
3. **服务端只知道已经报上来的用量**。引擎的月度判定读的是它自己的本地台账，
   桌面用量靠上报同步（D-43）；未上报的在途用量不在组织已用里，是**预算必然滞后**的一段，
   所以在响应里如实交出 `disclosed`，不装作实时。
4. **`enforce` 缺省关闭**：旧卷升级后行为与升级前逐字节相同（没有 `org_quotas` 行 = 不收敛不拦停），
   管理员显式打开才改变令牌签发结果。拦停是会把人关在门外的动作，不该由一次建表顺带完成。

**管理员豁免**：平台管理员不受组织拦停约束。否则「组织预算耗尽 ⇒ 唯一能改预算的人也进不来」
是一个自锁死循环；这件事不藏着——签发回执的 `quotaResolution.reasons` 里会写一条，
预算页的 `disclosed` 里也列一条，成员上限加总（`sumMemberCaps`）把他的行按 `exempt` 排除在外。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .audit import write_audit
from .auth import QUOTA_LIMITS, effective_quota, require_admin
from .db import now_ms, tx
from .errors import ApiError
from .gateway_usage import _is_deduped
from .idempotency import guarded
from .usage import _empty_bucket, _finish, _merge_into

router = APIRouter()

ORG_QUOTA_CONTRACT = "tt-org-quota.v1"
ORG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PERIODS = ("daily", "monthly", "total")
MAX_ORG_MEMBERS_IN_VIEW = 500
# 与个人配额同一上下界：两级用一套尺子，避免出现「组织能填出个人填不出的数」
ORG_TOKEN_MAX = int(QUOTA_LIMITS["monthlyTokens"][1])
ORG_CONCURRENCY_MAX = int(QUOTA_LIMITS["concurrentTasks"][1])


class OrgQuotaIn(BaseModel):
    """组织配额入参：五个字段都可省略，省略即「不改」，全给 0 即「全部不限制」。"""

    model_config = ConfigDict(extra="forbid")
    monthlyTokens: int | None = Field(default=None, ge=0, le=ORG_TOKEN_MAX)
    concurrentTasks: int | None = Field(default=None, ge=0, le=ORG_CONCURRENCY_MAX)
    memberMonthlyTokens: int | None = Field(default=None, ge=0, le=ORG_TOKEN_MAX)
    memberConcurrentTasks: int | None = Field(default=None, ge=0, le=ORG_CONCURRENCY_MAX)
    enforce: bool | None = None


class OrgIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org: str
    name: str = ""


# ── 行读写 ──────────────────────────────────────────────────


def normalize_org_id(raw: str | None) -> str:
    org = (raw or "").strip().lower()
    if not org or not ORG_RE.match(org):
        raise ApiError(
            400,
            "INVALID_ORG",
            "组织标识需为 1~64 位小写字母、数字、下划线或连字符，且以字母或数字开头",
        )
    return org


def _row_to_quota(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "org": str(row["org_id"]),
        "monthlyTokens": int(row["monthly_tokens"] or 0),
        "concurrentTasks": int(row["concurrent_tasks"] or 0),
        "memberMonthlyTokens": int(row["member_monthly_tokens"] or 0),
        "memberConcurrentTasks": int(row["member_concurrent_tasks"] or 0),
        "enforce": bool(int(row["enforce"] or 0)),
        "updatedAtMs": int(row["updated_at_ms"] or 0),
        "updatedBy": str(row["updated_by"] or ""),
    }


def get_org_quota(conn: sqlite3.Connection, org: str) -> dict[str, Any] | None:
    return _row_to_quota(
        conn.execute("SELECT * FROM org_quotas WHERE org_id = ?1", (org,)).fetchone()
    )


def org_members(conn: sqlite3.Connection, org: str) -> list[sqlite3.Row]:
    # 带上 org_id：resolve_member_quota 要按「这个人现在在哪个组织」去查组织配额，
    # 少这一列就会把「按组织筛出来的人」读成「没有组织的人」
    return conn.execute(
        "SELECT uid, nickname, email, role, org_id, quota_json FROM users WHERE org_id = ?1 "
        "ORDER BY created_at_ms ASC, uid ASC",
        (org,),
    ).fetchall()


# ── 组织用量（与个人明细、部门报表同一套行过滤）──────────────


def _period_days(period: str, now: int) -> tuple[str | None, str | None]:
    """返回 (day, monthPrefix)；与 usage.py / gateway_usage.py 同一算法，别在这儿另起一套。"""
    today = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
    if period == "daily":
        return today, None
    if period == "monthly":
        return None, today[:7]
    return None, None


def org_usage(conn: sqlite3.Connection, org: str, period: str = "monthly") -> dict[str, Any]:
    """组织当期已用：逐行套用与部门报表相同的去重口径。"""
    day_exact, month_prefix = _period_days(period, now_ms())
    rows = conn.execute("SELECT * FROM usage_rollup WHERE org_id = ?1", (org,)).fetchall()
    totals = _empty_bucket()
    per_member: dict[str, dict[str, Any]] = {}
    members_seen: set[str] = set()
    models: set[str] = set()
    deduped_rows = 0
    for raw in rows:
        day = str(raw["day"])
        if day_exact is not None and day != day_exact:
            continue
        if month_prefix is not None and not day.startswith(month_prefix):
            continue
        if _is_deduped(str(raw["source"]), str(raw["origin"])):
            deduped_rows += 1
            continue
        row = {
            "promptTokens": int(raw["prompt_tokens"] or 0),
            "completionTokens": int(raw["completion_tokens"] or 0),
            "tokensUsed": int(raw["prompt_tokens"] or 0) + int(raw["completion_tokens"] or 0),
            "calls": int(raw["calls"] or 0),
            "lastUsedAtMs": int(raw["last_used_at_ms"] or 0),
            "latencyMsTotal": int(raw["latency_ms_total"] or 0),
            "latencySamples": int(raw["latency_samples"] or 0),
            "pricedCalls": int(raw["priced_calls"] or 0),
            "unpricedCalls": int(raw["unpriced_calls"] or 0),
            "costs": json.loads(str(raw["costs_json"] or "{}")),
        }
        _merge_into(totals, row)
        models.add(str(raw["model_name"]))
        sub = str(raw["sub"])
        members_seen.add(sub)
        bucket = per_member.get(sub)
        if bucket is None:
            bucket = _empty_bucket()
            per_member[sub] = bucket
        _merge_into(bucket, row)
    used = _finish(totals)
    used["models"] = len(models)
    used["reportingMembers"] = len(members_seen)
    used["dedupedRows"] = deduped_rows
    per_member_out = [
        {**_finish(bucket), "uid": sub}
        for sub, bucket in sorted(
            per_member.items(), key=lambda item: (-item[1]["tokensUsed"], item[0])
        )
    ]
    return {"period": period, "used": used, "perMember": per_member_out}


# ── 两级裁决 ────────────────────────────────────────────────


def _value(user: sqlite3.Row | dict, key: str, default: Any = "") -> Any:
    """取字段但不假设调用方给的是整张 users 行。

    `org_members()` 为了少读列只取 uid/nickname/email/role/quota_json，
    这里要是直接 `user["org_id"]`，sqlite3.Row 会抛 IndexError——
    而且是在「查预算」这种只读动作里抛，最难查。
    """
    if isinstance(user, sqlite3.Row):
        return user[key] if key in user.keys() else default
    return user.get(key, default)


def _member_default(
    quota: dict[str, Any] | None, user: sqlite3.Row | dict, key: str
) -> tuple[int, str]:
    """返回 (值, 来源)。来源只有 personal / orgDefault / roleDefault 三种说法。"""
    override = _personal_override(user, key)
    if override is not None:
        return int(override), "personal"
    if quota:
        org_default = int(quota.get(_member_key(key)) or 0)
        if org_default > 0:
            return org_default, "orgDefault"
    personal = effective_quota(user)
    return int(personal.get(key) or 0), "roleDefault"


def _member_key(key: str) -> str:
    return {
        "monthlyTokens": "memberMonthlyTokens",
        "concurrentTasks": "memberConcurrentTasks",
    }[key]


def _personal_override(user: sqlite3.Row | dict, key: str) -> int | None:
    raw = _value(user, "quota_json", "{}")
    if isinstance(raw, memoryview):
        raw = bytes(raw).decode("utf-8", "replace")
    try:
        data = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    low, high = QUOTA_LIMITS[key]
    if not low <= value <= high:
        return None
    return int(value)


def resolve_member_quota(
    conn: sqlite3.Connection, user: sqlite3.Row | dict
) -> dict[str, Any]:
    """把「两级」算成一份要写进令牌声明的配额，并如实说明每个数字从哪一档来。

    返回的 `quota` 就是进令息的形状（`monthlyTokens` / `concurrentTasks`），
    其余键是给人看的：来源、是否被组织预算收敛、收敛前后。
    """
    org = str(_value(user, "org_id", "") or "").strip().lower()
    role = str(_value(user, "role", "user") or "user")
    quota = get_org_quota(conn, org) if org else None
    monthly, monthly_source = _member_default(quota, user, "monthlyTokens")
    concurrent, concurrent_source = _member_default(quota, user, "concurrentTasks")
    result: dict[str, Any] = {
        "org": org,
        "quota": {"monthlyTokens": monthly, "concurrentTasks": concurrent},
        "source": {"monthlyTokens": monthly_source, "concurrentTasks": concurrent_source},
        "orgQuota": quota,
        "enforced": False,
        "blocked": False,
        "clamped": False,
        "reasons": [],
    }
    if not quota or not quota["enforce"]:
        # 没配组织表 = 与升级前一模一样；这里连「已读组织表」都不声称
        return result
    result["enforced"] = True
    if role == "admin":
        result["reasons"].append("平台管理员不受组织拦停（否则会自锁：唯一能改预算的人进不来）")
        return result
    if int(quota["monthlyTokens"]) > 0:
        used = int(org_usage(conn, org, "monthly")["used"]["tokensUsed"])
        remaining = int(quota["monthlyTokens"]) - used
        if remaining <= 0:
            result["blocked"] = True
            result["reasons"].append(
                f"组织本月 token 预算已用尽（{used}/{quota['monthlyTokens']}），不签发令牌"
            )
        elif monthly <= 0 or remaining < monthly:
            # 0 是「不限制」的哨兵值，不是「额度为零」：`remaining < 0` 永远为假，
            # 直接用大小比较会让一个不限额成员在组织只剩 1 时照样拿到 0 = 无限。
            # 这里必须把 0 当作 +∞ 来比，并且收敛后的值下限取 1——写 0 等于取消限制。
            result["quota"]["monthlyTokens"] = max(1, remaining)
            result["clamped"] = True
            result["reasons"].append(
                f"成员月度上限原本不限制，收敛为组织剩余 {max(1, remaining)}"
                if monthly <= 0
                else f"成员月度上限由 {monthly} 收敛为组织剩余 {max(1, remaining)}"
            )
    org_concurrency = int(quota["concurrentTasks"])
    if org_concurrency > 0 and org_concurrency < concurrent:
        # 并发这一档只能收敛「一个人的声明」：组织同时任务数要由执行面计数，见模块文档
        result["quota"]["concurrentTasks"] = org_concurrency
        result["clamped"] = True
        result["reasons"].append(
            f"成员并发上限由 {concurrent} 收敛为组织并发上限 {org_concurrency}"
            "（组织同时任务总数需执行面计数，此处只收敛单人声明）"
        )
    return result


def budget_view(conn: sqlite3.Connection, org: str, period: str = "monthly") -> dict[str, Any]:
    """给管理面/CLI 看的一页裁决：额度、已用、剩余、超售、口径缺口。"""
    quota = get_org_quota(conn, org)
    usage = org_usage(conn, org, period)
    used_tokens = int(usage["used"]["tokensUsed"])
    members = org_members(conn, org)
    limit = int(quota["monthlyTokens"]) if quota else 0
    remaining = limit - used_tokens if limit > 0 else None
    caps: list[dict[str, Any]] = []
    sum_caps = 0
    for row in members:
        resolved = resolve_member_quota(conn, row)
        value = int(resolved["quota"]["monthlyTokens"])
        if value > 0:
            sum_caps += value
        caps.append(
            {
                "uid": str(row["uid"]),
                "nickname": str(row["nickname"]),
                "role": str(row["role"] or "user"),
                "monthlyTokens": value,
                "concurrentTasks": int(resolved["quota"]["concurrentTasks"]),
                "source": resolved["source"]["monthlyTokens"],
                "clamped": bool(resolved["clamped"]),
                # 管理员不受组织拦停：他的数字是「他会拿到什么」，不参与超售加总
                "exempt": str(row["role"] or "user") == "admin",
            }
        )
    stamped = {item["uid"] for item in usage["perMember"]}
    current = {str(row["uid"]) for row in members}
    # 「不限制」的成员没法进加总：0 不是一个可以相加的额度。
    # 只要组织设了预算而组里还有人不限额，承诺之和就已经大于任何有限总额 ⇒ 记为超售。
    # 管理员豁免拦停、走的不是这条签发路径，因此不计入不限额人数（在 exempt 里单列）。
    uncapped = sum(
        1
        for item in caps
        if not item["exempt"] and int(item["monthlyTokens"]) <= 0
    )
    verdict = "unlimited"
    if quota and quota["enforce"] and limit > 0:
        verdict = "exhausted" if remaining is not None and remaining <= 0 else "within"
    elif limit > 0:
        verdict = "reportedOnly"
    return {
        "ok": True,
        "contractVersion": ORG_QUOTA_CONTRACT,
        "org": org,
        "period": period,
        "quota": quota,
        "usage": usage["used"],
        "members": {
            "total": len(members),
            "shown": min(len(members), MAX_ORG_MEMBERS_IN_VIEW),
            "caps": caps[:MAX_ORG_MEMBERS_IN_VIEW],
        },
        "budget": {
            "limit": limit,
            "used": used_tokens,
            "remaining": remaining,
            "verdict": verdict,
            "enforce": bool(quota and quota["enforce"]),
            # 成员上限之和大于组织总额就是「超售」：不发令牌时没人超限，一起跑就会撞墙
            "sumMemberCaps": sum_caps,
            # 不限额成员单列：他们没有数字可加，但承诺已经超出任何有限预算
            "uncappedMembers": uncapped,
            "overCommitted": bool(limit > 0 and (sum_caps > limit or uncapped > 0)),
            "overCommitReason": (
                f"{uncapped} 名成员月度不限制：组织预算有限而承诺无上限，即视为超售"
                if limit > 0 and uncapped > 0
                else (
                    f"成员上限之和 {sum_caps} 已超过组织总额 {limit}"
                    if limit > 0 and sum_caps > limit
                    else ""
                )
            ),
        },
        "attribution": {
            "rule": "按 usage_rollup 行上盖章的 org_id 归集（上报时刻），成员移入不追溯改写历史",
            "usageFromNonMembers": len(stamped - current),
            "membersWithNoUsageThisPeriod": len(current - stamped),
        },
        "disclosed": [
            "组织已用只包含已经上报到服务端的用量；引擎侧未上报的在途用量不计入，"
            "因此预算判定天然滞后",
            "令牌里的成员额度是**签发时刻**的快照；组织预算在会话存续期间被耗尽时，"
            "已签发的令牌不会被服务端自动改小（改配额会吊销会话，用量增长不会）",
            "并发这一档收敛的是单人声明，不是组织同时任务总数",
            "平台管理员不受组织拦停",
        ],
        "generatedAtMs": now_ms(),
    }


# ── 写侧 ────────────────────────────────────────────────────


def set_org_quota(
    conn: sqlite3.Connection, org: str, payload: OrgQuotaIn, actor: str
) -> tuple[dict[str, Any], int]:
    """写入组织配额，返回 (生效后的配额, 因拦停翻转而被吊销的会话数)。"""
    before = get_org_quota(conn, org)
    fields = payload.model_fields_set
    if not fields:
        raise ApiError(400, "NOTHING_TO_UPDATE", "请至少提供 monthlyTokens、concurrentTasks、"
                    "memberMonthlyTokens、memberConcurrentTasks 或 enforce 之一")
    after = dict(before) if before else {
        "org": org,
        "monthlyTokens": 0,
        "concurrentTasks": 0,
        "memberMonthlyTokens": 0,
        "memberConcurrentTasks": 0,
        "enforce": False,
        "updatedBy": "",
    }
    mapping = {
        "monthlyTokens": "monthly_tokens",
        "concurrentTasks": "concurrent_tasks",
        "memberMonthlyTokens": "member_monthly_tokens",
        "memberConcurrentTasks": "member_concurrent_tasks",
    }
    for key, column in mapping.items():
        if key in fields:
            after[key] = int(getattr(payload, key) or 0)
    if "enforce" in fields:
        after["enforce"] = bool(payload.enforce)
    if not (conn.execute("SELECT id FROM orgs WHERE id = ?1", (org,)).fetchone()):
        raise ApiError(404, "ORG_NOT_FOUND", f"组织不存在：{org}（请先由管理员建立或指派成员）")
    stamp = now_ms()
    after["updatedAtMs"] = stamp
    after["updatedBy"] = actor
    conn.execute(
        """
        INSERT INTO org_quotas (org_id, monthly_tokens, concurrent_tasks, member_monthly_tokens,
            member_concurrent_tasks, enforce, updated_at_ms, updated_by)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
        ON CONFLICT(org_id) DO UPDATE SET
            monthly_tokens = excluded.monthly_tokens,
            concurrent_tasks = excluded.concurrent_tasks,
            member_monthly_tokens = excluded.member_monthly_tokens,
            member_concurrent_tasks = excluded.member_concurrent_tasks,
            enforce = excluded.enforce,
            updated_at_ms = excluded.updated_at_ms,
            updated_by = excluded.updated_by
        """,
        (
            org,
            int(after["monthlyTokens"]),
            int(after["concurrentTasks"]),
            int(after["memberMonthlyTokens"]),
            int(after["memberConcurrentTasks"]),
            1 if after["enforce"] else 0,
            stamp,
            actor,
        ),
    )
    write_audit(
        conn,
        actor=actor,
        action="org_quota.set",
        object=org,
        payload={"changed": sorted(fields)},
        before=before or {},
        after={
            key: after[key]
            for key in (
                "monthlyTokens",
                "concurrentTasks",
                "memberMonthlyTokens",
                "memberConcurrentTasks",
                "enforce",
            )
        },
    )
    # 打开或关闭拦停都会改变「下一次登录能不能成」，与改个人配额同性质：让旧令牌失效重签
    revoked = 0
    if "enforce" in fields and bool((before or {}).get("enforce")) != bool(after["enforce"]):
        cursor = conn.execute(
            "UPDATE sessions SET revoked = 1 WHERE revoked = 0 AND expires_at_ms > ?1 "
            "AND uid IN (SELECT uid FROM users WHERE org_id = ?2)",
            (now_ms(), org),
        )
        revoked = max(0, int(cursor.rowcount or 0))
    return after, revoked


# ── 路由 ────────────────────────────────────────────────────


@router.get("/api/v1/admin/orgs")
def api_admin_orgs(
    period: str = Query("monthly"), _admin: dict = Depends(require_admin)
) -> dict:
    """组织清单：每行的配额与当期已用一次给全，报表口径与部门报表同源。"""
    p = period if period in PERIODS else "monthly"
    with tx() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at_ms FROM orgs ORDER BY created_at_ms ASC, id ASC"
        ).fetchall()
        items = []
        for row in rows:
            org = str(row["id"])
            quota = get_org_quota(conn, org)
            usage = org_usage(conn, org, p)["used"]
            items.append(
                {
                    "org": org,
                    "name": str(row["name"]),
                    "createdAtMs": int(row["created_at_ms"] or 0),
                    "members": int(
                        conn.execute(
                            "SELECT COUNT(*) AS n FROM users WHERE org_id = ?1", (org,)
                        ).fetchone()["n"]
                    ),
                    "quota": quota,
                    "used": {
                        "tokensUsed": usage["tokensUsed"],
                        "calls": usage["calls"],
                        "reportingMembers": usage["reportingMembers"],
                    },
                }
            )
    return {
        "ok": True,
        "contractVersion": ORG_QUOTA_CONTRACT,
        "period": p,
        "total": len(items),
        "orgs": items,
    }


@router.post("/api/v1/admin/orgs", status_code=201)
def api_admin_create_org(
    payload: OrgIn, request: Request, response: Response, admin: dict = Depends(require_admin)
) -> dict:
    """建立/改名组织：只写 `orgs` 一行，不碰任何既有组织。

    带 `Idempotency-Key`（D-19 / #154 刀 B）时重试拿到的是第一次那份回执——`created` 这个
    「到底是我建的还是它本来就存在」的事实只在第一次执行时算得出来，重放不会把它翻成 false。
    """
    return guarded(
        "orgs.create",
        request,
        response,
        actor=str(admin.get("uid") or "admin"),
        digest_source=payload.model_dump(mode="json"),
        handler=lambda: _create_org(payload, admin),
    )


def _create_org(payload: OrgIn, admin: dict) -> dict:
    org = normalize_org_id(payload.org)
    name = (payload.name or "").strip() or org
    if len(name) > 64:
        raise ApiError(400, "INVALID_ORG_NAME", "组织名称不能超过 64 个字符")
    with tx() as conn:
        existing = conn.execute("SELECT id, name FROM orgs WHERE id = ?1", (org,)).fetchone()
        stamp = now_ms()
        if existing is None:
            conn.execute(
                "INSERT INTO orgs (id, name, created_at_ms) VALUES (?1, ?2, ?3)",
                (org, name, stamp),
            )
            write_audit(
                conn,
                actor=str(admin.get("uid") or "admin"),
                action="org.create",
                object=org,
                payload={"name": name},
                before={},
                after={"name": name},
            )
        else:
            conn.execute("UPDATE orgs SET name = ?1 WHERE id = ?2", (name, org))
            write_audit(
                conn,
                actor=str(admin.get("uid") or "admin"),
                action="org.rename",
                object=org,
                payload={"name": name},
                before={"name": str(existing["name"])},
                after={"name": name},
            )
        row = conn.execute("SELECT * FROM orgs WHERE id = ?1", (org,)).fetchone()
    return {
        "ok": True,
        "contractVersion": ORG_QUOTA_CONTRACT,
        "created": existing is None,
        "org": org,
        "name": str(row["name"]),
        "createdAtMs": int(row["created_at_ms"] or 0),
    }


@router.get("/api/v1/admin/orgs/{org}/quota")
def api_admin_org_quota(org: str, _admin: dict = Depends(require_admin)) -> dict:
    key = normalize_org_id(org)
    with tx() as conn:
        quota = get_org_quota(conn, key)
        if quota is None and not conn.execute(
            "SELECT id FROM orgs WHERE id = ?1", (key,)
        ).fetchone():
            raise ApiError(404, "ORG_NOT_FOUND", f"组织不存在：{key}")
    return {
        "ok": True,
        "contractVersion": ORG_QUOTA_CONTRACT,
        "org": key,
        "quota": quota,
        "statement": (
            "该组织还没有配额行：两级配额不生效，成员沿用个人覆盖或角色缺省"
            if quota is None
            else ("拦停已开启：令牌按组织预算收敛，用尽即拒发（管理员豁免）"
                  if quota["enforce"] else "拦停未开启：只报表，不改变任何签发结果")
        ),
    }


@router.put("/api/v1/admin/orgs/{org}/quota")
def api_admin_org_quota_put(
    org: str,
    payload: OrgQuotaIn,
    request: Request,
    response: Response,
    admin: dict = Depends(require_admin),
) -> dict:
    """写组织配额。带 `Idempotency-Key` 时同一次改动的重试不会二次吊销会话。

    摘要把**显式传了哪些字段**也算进去（`model_fields_set`）：PATCH 式的部分更新里
    「只改 enforce」和「四个数全改一遍再带上 enforce」是两件不同的事，不能共用一把键。
    """
    return guarded(
        "orgs.quota.put",
        request,
        response,
        actor=str(admin.get("uid") or "admin"),
        binding=normalize_org_id(org),
        digest_source={
            "fields": sorted(payload.model_fields_set),
            "values": payload.model_dump(mode="json", exclude_unset=True),
        },
        handler=lambda: _put_org_quota(org, payload, admin),
    )


def _put_org_quota(org: str, payload: OrgQuotaIn, admin: dict) -> dict:
    key = normalize_org_id(org)
    with tx() as conn:
        quota, revoked = set_org_quota(conn, key, payload, str(admin.get("uid") or "admin"))
        # 拦停翻转会把该组织在用的会话一并吊销（旧声明里的额度数字已经不作数了）。
        # 两个数都要给：只报「还剩几个在用」会让管理员以为没影响到任何人。
        active = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE revoked = 0 AND expires_at_ms > ?1 "
                "AND uid IN (SELECT uid FROM users WHERE org_id = ?2)",
                (now_ms(), key),
            ).fetchone()["n"]
        )
    return {
        "ok": True,
        "contractVersion": ORG_QUOTA_CONTRACT,
        "org": key,
        "quota": quota,
        "sessionsRevoked": revoked,
        "activeSessions": active,
        "statement": (
            f"本次拦停开关变更吊销了 {revoked} 个在用会话，组织内仍在用的 {active} 个"
            if revoked
            else f"本次未吊销会话（拦停开关没有翻转），组织内在用会话 {active} 个"
        ),
        "next": "改动已生效；被吊销会话的成员下次登录会拿到按新额度收敛的声明",
    }


@router.get("/api/v1/admin/orgs/{org}/budget")
def api_admin_org_budget(
    org: str, period: str = Query("monthly"), _admin: dict = Depends(require_admin)
) -> dict:
    key = normalize_org_id(org)
    p = period if period in PERIODS else "monthly"
    with tx() as conn:
        if not conn.execute("SELECT id FROM orgs WHERE id = ?1", (key,)).fetchone():
            raise ApiError(404, "ORG_NOT_FOUND", f"组织不存在：{key}")
        return budget_view(conn, key, p)


@router.get("/api/v1/admin/orgs/{org}/members")
def api_admin_org_members(
    org: str, _admin: dict = Depends(require_admin)
) -> dict:
    """成员的「两级配额落点」：每个人当前生效的两个数、来自哪一档、有没有被收敛。"""
    key = normalize_org_id(org)
    with tx() as conn:
        if not conn.execute("SELECT id FROM orgs WHERE id = ?1", (key,)).fetchone():
            raise ApiError(404, "ORG_NOT_FOUND", f"组织不存在：{key}")
        members = org_members(conn, key)
        items = []
        for row in members:
            resolved = resolve_member_quota(conn, row)
            items.append(
                {
                    "uid": str(row["uid"]),
                    "nickname": str(row["nickname"]),
                    "email": str(row["email"]),
                    "role": str(row["role"] or "user"),
                    "quota": resolved["quota"],
                    "source": resolved["source"],
                    "clamped": bool(resolved["clamped"]),
                    "blocked": bool(resolved["blocked"]),
                    "reasons": resolved["reasons"],
                }
            )
    return {
        "ok": True,
        "contractVersion": ORG_QUOTA_CONTRACT,
        "org": key,
        "total": len(items),
        "members": items,
        "note": "这里的数字是**下一次签发令牌时**会写进声明的值，不是当前会话正在用的值",
    }
