"""审计签名锚点与可选远端归档（缺陷 D-27 防篡改那一半）。

哈希链只能证明「行与行相连」：本机用户若把整段链重算一遍，逐行判定会全部通过。
锚点补的就是这一层——把 `(首行, 末行, 条目数, 链头)` 用服务端 RS256 私钥签掉，
签名摘要存进库里、公钥走 `/.well-known/jwks.json`。改了正文或删了行 ⇒ 链头变了 ⇒ 与
已签锚点对不上；重算了链 ⇒ 与更早的锚点对不上。锚点本身被删则「从未归档」也是可观察的
状态，`GET …/anchor/verify` 会把「一条锚点都没有」如实报出来。

远端归档是**可选**的：配了 `TT_AUDIT_ARCHIVE_URL` 才推，推送失败把原因写进行里
（`archive_state='failed'`），不谎报成已归档。归档地址必须 HTTPS，本机回环允许 HTTP，
与登录回调那条既有口径一致。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from .audit import BREAK_REASONS, verify_prefix, verify_report, write_audit
from .audit_chain import canonical
from .auth import require_gateway_staff
from .config import settings
from .db import now_ms, tx
from .errors import ApiError
from .keys import key_store
from .roles import can_write_gateway, role_of
from .security import sha256_hex

router = APIRouter()

ANCHOR_AUDIENCE = "tongtian-audit"
JWKS_PATH = "/.well-known/jwks.json"
MAX_ANCHORS_PAGE = 200
MAX_ARCHIVE_ROWS = 2000


def require_anchor_writer(request: Request) -> dict:
    """签发与归档是「写下可信结论」的动作：只放行平台管理员与网关运维，审计员保留全部读权。

    审计员能出合规报告、能验锚点，但不能造锚点——否则「谁都能盖章」和「盖章有意义」冲突。
    """
    user = require_gateway_staff(request)
    if not can_write_gateway(role_of(user)):
        raise ApiError(403, "FORBIDDEN", "签发或归档审计锚点需要管理员或网关运维权限")
    return user


# ── 签名与存储 ──────────────────────────────────────────────


def anchor_digest(fields: dict[str, Any]) -> str:
    return sha256_hex(canonical(fields).encode("utf-8"))


def _sign_anchor(fields: dict[str, Any]) -> tuple[str, str]:
    claims = {
        "iss": settings.jwt_issuer,
        "aud": ANCHOR_AUDIENCE,
        "typ": "audit-anchor",
        **fields,
        "digest": anchor_digest(fields),
        "iat": int(now_ms() // 1000),
    }
    store = key_store()
    return store.sign(claims), store.current_kid


def _view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "atMs": int(row["at_ms"]),
        "fromId": int(row["from_id"]),
        "toId": int(row["to_id"]),
        "entries": int(row["entries"]),
        "headHash": str(row["head_hash"]),
        "signingKid": str(row["signing_kid"]),
        "actor": str(row["actor"]),
        "archiveState": str(row["archive_state"]),
        "archiveRef": str(row["archive_ref"]),
        "archiveError": str(row["archive_error"]),
    }


def _row_by_seq(conn: sqlite3.Connection, seq: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM audit_anchors WHERE seq = ?1", (int(seq),)
    ).fetchone()


def create_anchor(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
    """给当前全链打一个签名锚点；链本身有断点时拒绝归档，不给篡改盖可信章。"""
    report = verify_report(conn)
    if not report["ok"]:
        if report["truncated"]:
            raise ApiError(
                507,
                "AUDIT_TOO_LARGE",
                f"审计行数超过单次巡检上限 {report['scanLimit']}，请先归档后再打锚点",
            )
        first = report["breaks"][0] if report["breaks"] else {}
        reason = BREAK_REASONS.get(str(first.get("reason")), "未知原因")
        raise ApiError(
            409,
            "AUDIT_CHAIN_BROKEN",
            f"审计链在行 {first.get('id')} 处已断开（{reason}），拒绝出具可信锚点",
        )
    if report["entries"] == 0:
        raise ApiError(409, "AUDIT_EMPTY", "审计链还没有任何行，无需归档")
    fields = {
        "fromId": int(report["firstId"]),
        "toId": int(report["lastId"]),
        "entries": int(report["entries"]),
        "headHash": str(report["headHash"]),
    }
    signature, kid = _sign_anchor(fields)
    stamp = now_ms()
    conn.execute(
        "INSERT INTO audit_anchors (at_ms, from_id, to_id, entries, head_hash, signature, "
        "signing_kid, actor, archive_state, archive_ref, archive_error) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'local', '', '')",
        (
            stamp,
            fields["fromId"],
            fields["toId"],
            fields["entries"],
            fields["headHash"],
            signature,
            kid,
            actor,
        ),
    )
    seq = int(conn.execute("SELECT last_insert_rowid() AS n").fetchone()["n"])
    row = _row_by_seq(conn, seq)
    assert row is not None
    result = _view(row)
    result["digest"] = anchor_digest(fields)
    # 签名只在签发的这一条响应里回显一次；列表接口不带它，避免被当成可读凭证到处转发。
    result["signature"] = signature
    return result


def verify_anchor_signature(row: sqlite3.Row, store: Any = None) -> tuple[bool, str]:
    """验一枚锚点签名；`store` 缺省用进程单例密钥库。

    自检与恢复演练要能指定「用哪一目录里的公钥验」（`app.cli doctor` / `backup restore`），
    而且那条路径必须不许生成密钥——所以这里允许外部传一个已经装载好的 KeyStore。
    """
    fields = {
        "fromId": int(row["from_id"]),
        "toId": int(row["to_id"]),
        "entries": int(row["entries"]),
        "headHash": str(row["head_hash"]),
    }
    claims, reason = (store or key_store()).verify(
        str(row["signature"] or ""), expected_audience=ANCHOR_AUDIENCE
    )
    if not claims:
        return False, reason
    if claims.get("typ") != "audit-anchor":
        return False, "签名载荷不是审计锚点类型"
    for key, value in fields.items():
        if claims.get(key) != value:
            return False, f"锚点字段与签名不符：{key}"
    if str(claims.get("digest") or "") and str(claims.get("digest")) != anchor_digest(fields):
        return False, "锚点摘要与签名不符"
    return True, "可信"


def verify_all(
    conn: sqlite3.Connection, *, limit: int = MAX_ANCHORS_PAGE, store: Any = None
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM audit_anchors ORDER BY seq DESC LIMIT ?1", (int(limit),)
    ).fetchall()
    items = []
    for row in rows:
        signature_ok, signature_reason = verify_anchor_signature(row, store)
        if signature_ok:
            prefix_ok, reason = verify_prefix(conn, _view(row))
        else:
            # 签名先不过就不再比对链：把「没比对」这件事写进原因里，
            # 否则运维只看到一句验签失败，不知道链条到底有没有核过。
            prefix_ok, reason = False, f"{signature_reason}（未比对链）"
        items.append(
            {
                **_view(row),
                "signatureTrusted": signature_ok,
                "chainTrusted": bool(signature_ok and prefix_ok),
                "reason": reason,
            }
        )
    bad = [item for item in items if not item["chainTrusted"]]
    return {
        "anchors": len(items),
        "untrusted": len(bad),
        "items": items,
        # 「一条锚点都没有」既不是坏消息也不是好消息：它意味着没有防整体重算的证据。
        "statement": (
            "尚无锚点，历史链无法证明未被整体重算"
            if not items
            else ("全部锚点可信" if not bad else f"{len(bad)} 个锚点与当前链不一致")
        ),
    }


# ── 远端归档 ────────────────────────────────────────────────


def _target_allowed(url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return True, ""
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return True, ""
    if parsed.scheme not in ("http", "https"):
        return False, "归档地址必须是 http(s)"
    return False, "归档地址必须使用 HTTPS（本机回环除外）"


def configured() -> dict[str, Any]:
    url = (settings.audit_archive_url or "").strip().rstrip("/")
    return {
        "enabled": bool(url),
        "endpoint": (url + "/v1/audit-anchors") if url else "",
        "urlConfigured": bool(url),
        "tokenConfigured": bool((settings.audit_archive_token or "").strip()),
        "timeoutSeconds": int(settings.audit_archive_timeout_seconds),
    }


def push_anchor(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    """把一个锚点连同它覆盖的行推给远端；失败只记录原因，不改锚点的可信性判定。"""
    row = _row_by_seq(conn, seq)
    if row is None:
        raise ApiError(404, "ANCHOR_NOT_FOUND", "锚点不存在")
    status = configured()
    if not status["enabled"]:
        result = _view(row)
        result.update(
            {
                "ok": True,
                "pushed": False,
                "reason": "未配置 TT_AUDIT_ARCHIVE_URL：只出本地锚点，不算已归档",
            }
        )
        return result
    allowed, reason = _target_allowed(status["endpoint"])
    if not allowed:
        raise ApiError(400, "INVALID_ARCHIVE_URL", reason)
    fields = {
        "fromId": int(row["from_id"]),
        "toId": int(row["to_id"]),
        "entries": int(row["entries"]),
        "headHash": str(row["head_hash"]),
    }
    rows = conn.execute(
        "SELECT id, at_ms, actor_uid, actor_role, action, object, payload_json, "
        "before_json, after_json, status, prev_hash, entry_hash "
        "FROM admin_audit WHERE id >= ?1 AND id <= ?2 ORDER BY id ASC LIMIT ?3",
        (fields["fromId"], fields["toId"], MAX_ARCHIVE_ROWS),
    ).fetchall()
    if len(rows) != fields["entries"]:
        # 只推半条链会让对端算不出锚点里的链头，那比不推更像造假：如实标 skipped 并拒推。
        detail = (
            f"锚点覆盖 {fields['entries']} 行，超过单次推送上限 {MAX_ARCHIVE_ROWS}，未推送"
        )
        conn.execute(
            "UPDATE audit_anchors SET archive_state='skipped', archive_ref='', "
            "archive_error=?1 WHERE seq=?2",
            (detail, int(row["seq"])),
        )
        updated = _row_by_seq(conn, int(row["seq"]))
        assert updated is not None
        result = _view(updated)
        result.update({"ok": True, "pushed": False, "reason": detail})
        return result
    body = {
        "anchor": {
            "seq": int(row["seq"]),
            **fields,
            "signature": str(row["signature"]),
            "signingKid": str(row["signing_kid"]),
            "actor": str(row["actor"]),
            "atMs": int(row["at_ms"]),
        },
        "jwksPath": JWKS_PATH,
        "rows": [
            {
                "id": int(item["id"]),
                "atMs": int(item["at_ms"]),
                "actor": str(item["actor_uid"]),
                "actorRole": str(item["actor_role"]),
                "action": str(item["action"]),
                "object": str(item["object"]),
                "status": int(item["status"]),
                "before": json.loads(item["before_json"] or "{}"),
                "after": json.loads(item["after_json"] or "{}"),
                "entryHash": str(item["entry_hash"]),
            }
            for item in rows
        ],
        "coverage": "full",
    }
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = (settings.audit_archive_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        status["endpoint"], data=data, headers=headers, method="POST"
    )
    remote_ref = ""
    failure = ""
    try:
        with urllib.request.urlopen(request, timeout=status["timeoutSeconds"]) as response:
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(parsed, dict):
                raise ValueError("归档端返回的不是对象")
            remote_ref = str(parsed.get("ref") or parsed.get("id") or "").strip()[:120]
            if not remote_ref:
                raise ValueError(f"归档端未返回 ref（HTTP {response.status}）")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        failure = (str(exc) or exc.__class__.__name__)[:200]
    if failure:
        # 失败状态必须在事务里落库：端点随后才按 ok=False 转成 502，
        # 若在事务内抛异常，这条「推过但没成」的证据会被回滚掉。
        conn.execute(
            "UPDATE audit_anchors SET archive_state='failed', archive_ref='', "
            "archive_error=?1 WHERE seq=?2",
            (failure, int(row["seq"])),
        )
        updated = _row_by_seq(conn, int(row["seq"]))
        assert updated is not None
        result = _view(updated)
        result.update({"ok": False, "pushed": False, "error": failure})
        return result
    conn.execute(
        "UPDATE audit_anchors SET archive_state='remote', archive_ref=?1, "
        "archive_error='' WHERE seq=?2",
        (remote_ref, int(row["seq"])),
    )
    updated = _row_by_seq(conn, int(row["seq"]))
    assert updated is not None
    result = _view(updated)
    result.update(
        {
            "ok": True,
            "pushed": True,
            "coverage": "full",
            "rowsPushed": len(body["rows"]),
        }
    )
    return result


# ── 端点 ────────────────────────────────────────────────────


@router.post("/api/v1/admin/audit/anchor")
def api_audit_anchor_create(staff: dict = Depends(require_anchor_writer)):
    actor = str(staff.get("uid") or "staff")
    with tx() as conn:
        result = create_anchor(conn, actor)
        write_audit(
            conn,
            actor=actor,
            actor_role=role_of(staff),
            action="audit.anchor",
            object=f"{result['fromId']}-{result['toId']}",
            payload={
                "entries": result["entries"],
                "headHash": result["headHash"],
                "digest": result["digest"],
                "signingKid": result["signingKid"],
            },
            after={"entries": result["entries"], "digest": result["digest"]},
        )
    return {"ok": True, "archive": configured(), "anchor": result}


@router.get("/api/v1/admin/audit/anchors")
def api_audit_anchor_list(
    limit: int = Query(20, ge=1, le=MAX_ANCHORS_PAGE),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_anchors ORDER BY seq DESC LIMIT ?1", (int(limit),)
        ).fetchall()
    return {
        "ok": True,
        "anchors": [_view(row) for row in rows],
        "archive": configured(),
    }


@router.get("/api/v1/admin/audit/anchor/verify")
def api_audit_anchor_verify(
    limit: int = Query(MAX_ANCHORS_PAGE, ge=1, le=MAX_ANCHORS_PAGE),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        result = verify_all(conn, limit=int(limit))
        report = verify_report(conn)
    return {"ok": True, "chain": report, **result}


class ArchiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int | None = None


@router.post("/api/v1/admin/audit/anchor/archive")
def api_audit_anchor_archive(
    payload: ArchiveIn | None = Body(default=None),
    staff: dict = Depends(require_anchor_writer),
):
    seq = payload.seq if payload is not None else None
    actor = str(staff.get("uid") or "staff")
    with tx() as conn:
        if seq is None:
            row = conn.execute(
                "SELECT seq FROM audit_anchors ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ApiError(409, "NO_ANCHOR", "还没有任何锚点可归档")
            seq = int(row["seq"])
        result = push_anchor(conn, int(seq))
        write_audit(
            conn,
            actor=actor,
            actor_role=role_of(staff),
            action="audit.archive",
            object=str(seq),
            payload={
                "state": result["archiveState"],
                "ref": result.get("archiveRef", ""),
                "error": result.get("error", ""),
            },
            after={
                "state": result["archiveState"],
                "ref": result.get("archiveRef", ""),
                "pushed": bool(result.get("pushed")),
            },
        )
    if not result.get("ok"):
        raise ApiError(
            502, "AUDIT_ARCHIVE_FAILED", f"远端归档失败：{result.get('error', '')}".rstrip("：")
        )
    return {"ok": True, **result}


@router.get("/api/v1/admin/audit/archive/status")
def api_audit_archive_status(
    limit: int = Query(20, ge=1, le=MAX_ANCHORS_PAGE),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        rows = conn.execute(
            "SELECT seq, archive_state, archive_ref, archive_error, at_ms "
            "FROM audit_anchors ORDER BY seq DESC LIMIT ?1",
            (int(limit),),
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            counts[str(row["archive_state"])] = counts.get(str(row["archive_state"]), 0) + 1
    return {
        "ok": True,
        "archive": configured(),
        "byState": counts,
        "recent": [
            {
                "seq": int(row["seq"]),
                "state": str(row["archive_state"]),
                "ref": str(row["archive_ref"]),
                "error": str(row["archive_error"]),
                "atMs": int(row["at_ms"]),
            }
            for row in rows
        ],
    }
