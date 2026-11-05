"""写侧幂等键（缺陷 D-19 / 卡 #154 刀 B）：让「重试」不再等于「再做一次」。

为什么需要：第三方在不可靠网络上发一个写请求，响应丢了它只有两个选择——要么放弃（数据没上报），
要么重试（可能做了两遍）。`Idempotency-Key` 把选择变成服务端的责任：**同一把键 + 同一份请求体
= 只执行一次，重试拿到的是第一次那份回执**。

四条口径先钉死（都在 `tests/test_idempotency.py` 里真打钉住）：

1. **只有完成态可重放**。执行中（`state='in_progress'`）撞同一把键直接 409
   `IDEMPOTENCY_IN_PROGRESS`，不排队、不并发跑第二遍——排队会让两个请求都以为自己成功了。
   这把「进行中」的行是**租约**（`LEASE_MS` 很短），进程被杀也不会永久占住这把键。
2. **同一把键 + 不同请求体 = 冲突**，不是「后一次覆盖前一次」。409 `IDEMPOTENCY_CONFLICT`
   并如实说明差异只在摘要上（不回显对方的请求体）。键是「我打算做的这一件事」的名字，
   拿它做两件不同的事是调用方的 bug。
3. **失败不占键**。业务异常（`ApiError` 等）不落缓存、当场释放预留 ⇒ 重试真的重跑。
   缓存里只有成功回执，别把「上次也 400」当成幂等保证。
4. **回执原文落库**（`response_json` 列，明文 SQLite），到期删除。因此
   **签发令牌的路径（register / login / refresh / logout）有意不接这里**：
   不拿凭据换重放便利。需要重放保护的是上报与管理写，不是登录。

不带 `Idempotency-Key` 头的请求**逐字节走原路径**（本模块一行不执行），所以对现有桌面端
与 CLI 零行为变化；这条是「加表不加风险」的前提，写在契约的 `idempotency.semantics` 里。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

from fastapi import Request, Response

from .db import now_ms, tx
from .errors import ApiError

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-TT-Idempotent-Replay"

#: 完成态回执的保留时长：超过就不再有「重放」可言，键可被复用。
DEFAULT_TTL_MS = 24 * 60 * 1000
#: 预留（执行中）的租约：进程崩溃后最长占用时间，故意远短于 TTL。
LEASE_MS = 60 * 1000
#: 单次清理最多删除的行数（懒清，不引入定时任务与新依赖）。
SWEEP_BATCH = 500
#: 回执超过这个字节数就不缓存（重试改为真的重跑，而不是把大对象塞进 SQLite）。
MAX_STORED_RESPONSE_BYTES = 64 * 1024

KEY_RE = re.compile(r"^[A-Za-z0-9._-]{8,200}$")

#: 已接入幂等键的稳定操作，**键就是契约里的 op id**。两条硬约束由 `find_problems()` 双向核对：
#: ① 清单里声明 `"idempotency": "supported"` 的 op 集合 == 本表键集合（说支持而没接、
#:    或接了却没写进清单，都算契约漂移）；② 本表每行的 method/path 与清单条目一字不差。
#: `multipart=True` 的行由调用方在**读干请求体之后**再进 `guarded()`（摘要要含包内容）。
SUPPORTED_SCOPES: dict[str, dict[str, Any]] = {
    "market.publishSkill": {
        "method": "POST",
        "path": "/api/v1/skills",
        "successStatus": 201,
        "multipart": True,
        "actor": "user",
    },
    "market.publishSuite": {
        "method": "POST",
        "path": "/api/v1/suites",
        "successStatus": 201,
        "multipart": True,
        "actor": "user",
    },
    "usage.report": {
        "method": "POST",
        "path": "/api/v1/usage/report",
        "successStatus": 200,
        "multipart": False,
        "actor": "user",
    },
    "orgs.create": {
        "method": "POST",
        "path": "/api/v1/admin/orgs",
        "successStatus": 201,
        "multipart": False,
        "actor": "admin",
    },
    "orgs.quota.put": {
        "method": "PUT",
        "path": "/api/v1/admin/orgs/{org}/quota",
        "successStatus": 200,
        "multipart": False,
        "actor": "admin",
    },
}


def digest_of(op_id: str, binding: str, source: Any) -> str:
    """把「这一次打算做的事」压成一把摘要：操作 + 具体路径 + 归一化后的请求体。

    `sort_keys=True` 是关键：字典顺序不同而内容相同必须算同一件事，否则重放判不出来。
    """
    blob = json.dumps(
        {"op": op_id, "binding": binding, "body": source},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sweep(conn, now: int) -> int:
    """删掉已过期的行（完成态与租约都按 `expires_at_ms` 走），返回删除条数。"""
    rows = conn.execute(
        "SELECT scope, actor, idem_key FROM idempotency_keys "
        "WHERE expires_at_ms > 0 AND expires_at_ms <= ?1 LIMIT ?2",
        (now, SWEEP_BATCH),
    ).fetchall()
    for row in rows:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE scope = ?1 AND actor = ?2 AND idem_key = ?3",
            (str(row["scope"]), str(row["actor"]), str(row["idem_key"])),
        )
    return len(rows)


def _read_key(request: Request) -> str:
    raw = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if not raw:
        return ""
    if not KEY_RE.match(raw):
        raise ApiError(
            400,
            "IDEMPOTENCY_KEY_INVALID",
            f"{IDEMPOTENCY_HEADER} 需要 8–200 位字母、数字、点、下划线或短横线",
        )
    return raw


def _reserve(op_id: str, actor: str, key: str, digest: str) -> tuple[str, str]:
    """占位。返回 ('run', '') / ('replay', 回执原文)；冲突与执行中直接抛错。"""
    now = now_ms()
    with tx() as conn:
        sweep(conn, now)
        existing = conn.execute(
            "SELECT state, request_digest, expires_at_ms, response_json FROM idempotency_keys "
            "WHERE scope = ?1 AND actor = ?2 AND idem_key = ?3",
            (op_id, actor, key),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO idempotency_keys "
                "(scope, actor, idem_key, request_digest, state, status, response_json, "
                " created_at_ms, expires_at_ms) "
                "VALUES (?1, ?2, ?3, ?4, 'in_progress', 0, '', ?5, ?6)",
                (op_id, actor, key, digest, now, now + LEASE_MS),
            )
            return ("run", "")
        state = str(existing["state"])
        if state == "complete":
            if str(existing["request_digest"]) != digest:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    f"这把 {IDEMPOTENCY_HEADER} 已经用于另一次请求（请求体摘要不同）；"
                    "换一把键才是「另一件事」",
                )
            body = str(existing["response_json"])
            if not body:
                # 回执太大没存下来 ⇒ 重放拿不到原件，只能真的重跑（不装作重放）。
                return ("run", "")
            return ("replay", body)
        # 执行中：租约没到期就是「别人正在做这件事」，租约到期说明那次已经死了。
        if int(existing["expires_at_ms"]) > now:
            raise ApiError(
                409,
                "IDEMPOTENCY_IN_PROGRESS",
                f"同一把 {IDEMPOTENCY_HEADER} 的上一次请求还在执行中；"
                f"租约最长 {LEASE_MS // 1000} 秒，稍后重试",
                headers={"Retry-After": "2"},
            )
        conn.execute(
            "UPDATE idempotency_keys SET state = 'in_progress', request_digest = ?1, "
            "status = 0, response_json = '', created_at_ms = ?2, expires_at_ms = ?3 "
            "WHERE scope = ?4 AND actor = ?5 AND idem_key = ?6",
            (digest, now, now + LEASE_MS, op_id, actor, key),
        )
        return ("run", "")


def _complete(op_id: str, actor: str, key: str, digest: str, status: int, body: str, ttl_ms: int) -> None:
    now = now_ms()
    with tx() as conn:
        conn.execute(
            "UPDATE idempotency_keys SET state = 'complete', status = ?1, response_json = ?2, "
            "expires_at_ms = ?3 WHERE scope = ?4 AND actor = ?5 AND idem_key = ?6 "
            "AND request_digest = ?7",
            (status, body, now + ttl_ms, op_id, actor, key, digest),
        )


def _release(op_id: str, actor: str, key: str) -> None:
    """业务失败 ⇒ 释放预留，让重试真的重跑（失败不进缓存）。"""
    with tx() as conn:
        conn.execute(
            "DELETE FROM idempotency_keys "
            "WHERE scope = ?1 AND actor = ?2 AND idem_key = ?3 AND state = 'in_progress'",
            (op_id, actor, key),
        )


def guarded(
    op_id: str,
    request: Request,
    response: Response,
    *,
    actor: str,
    digest_source: Any,
    binding: str = "",
    handler: Callable[[], dict],
    ttl_ms: int = DEFAULT_TTL_MS,
) -> dict:
    """包住一次写操作：没带键就直接跑，带键就按「预留—执行—落缓存」三段走。

    `handler` 必须是**已经做完全部校验、只剩副作用**的那段代码；校验放在外面意味着
    「参数错误」根本不占键（也就不会被缓存成错误回执）。
    """
    key = _read_key(request)
    if not key:
        return handler()
    scope = SUPPORTED_SCOPES.get(op_id)
    if scope is None:  # 清单没承诺过重放的，这里也不接
        raise ApiError(
            400,
            "IDEMPOTENCY_SCOPE_UNSUPPORTED",
            f"该操作未接入 {IDEMPOTENCY_HEADER}（清单里没承诺过重放的就是没做）",
        )
    digest = digest_of(op_id, binding, digest_source)
    state, cached = _reserve(op_id, actor or "", key, digest)
    if state == "replay":
        response.headers[REPLAY_HEADER] = "true"
        return json.loads(cached)
    response.headers[REPLAY_HEADER] = "false"
    try:
        result = handler()
    except Exception:
        _release(op_id, actor or "", key)
        raise
    body = json.dumps(result, ensure_ascii=False)
    if len(body.encode("utf-8")) <= MAX_STORED_RESPONSE_BYTES:
        _complete(op_id, actor or "", key, digest, int(scope["successStatus"]), body, ttl_ms)
    else:
        # 存不下的回执直接标完成但留空原文：下次同键同体会「真的重跑」，不会假装重放。
        _complete(op_id, actor or "", key, digest, int(scope["successStatus"]), "", ttl_ms)
    return result


def rows(op_id: str = "") -> list[dict[str, Any]]:
    """给自测与 `verify` 看的第一手状态（不对外暴露，只在进程内使用）。"""
    with tx() as conn:
        if op_id:
            found = conn.execute(
                "SELECT scope, actor, idem_key, state, status, expires_at_ms FROM idempotency_keys "
                "WHERE scope = ?1 ORDER BY created_at_ms ASC",
                (op_id,),
            ).fetchall()
        else:
            found = conn.execute(
                "SELECT scope, actor, idem_key, state, status, expires_at_ms FROM idempotency_keys "
                "ORDER BY created_at_ms ASC"
            ).fetchall()
    return [
        {
            "scope": str(row["scope"]),
            "actor": str(row["actor"]),
            "key": str(row["idem_key"]),
            "state": str(row["state"]),
            "status": int(row["status"]),
            "expiresAtMs": int(row["expires_at_ms"]),
        }
        for row in found
    ]
