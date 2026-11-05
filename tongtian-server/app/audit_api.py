"""控制面审计查询：哈希链分页、验链、NDJSON 导出，外加签名锚点与合规报告。

审计员及以上角色可读；查询本身不写审计，避免「看审计」把审计刷满。
锚点与合规报告挂在同一个 router 下（`include_router`），为的是避开市场通配路由的
注册顺序问题——`main.py` 只需要在 `market_router` 之前挂载这一个 router。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from .audit import export_ndjson, list_entries, verify_report
from .audit_anchor import router as anchor_router
from .audit_compliance import router as compliance_router
from .auth import require_gateway_staff
from .db import tx
from .errors import ApiError

router = APIRouter()

MAX_LIMIT = 1000


def _limit(value: int) -> int:
    if value < 1 or value > MAX_LIMIT:
        raise ApiError(400, "INVALID_REQUEST", f"limit 必须在 1–{MAX_LIMIT} 之间")
    return value


@router.get("/api/v1/admin/audit")
def api_admin_audit(
    after: int = Query(0, ge=0),
    limit: int = Query(100),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        page = list_entries(conn, after=after, limit=_limit(limit))
    return {"ok": True, **page}


@router.get("/api/v1/admin/audit/verify")
def api_admin_audit_verify(_staff: dict = Depends(require_gateway_staff)):
    with tx() as conn:
        report = verify_report(conn)
    return report


@router.get("/api/v1/admin/audit/export")
def api_admin_audit_export(
    after: int = Query(0, ge=0),
    limit: int = Query(MAX_LIMIT),
    _staff: dict = Depends(require_gateway_staff),
):
    with tx() as conn:
        text, page = export_ndjson(conn, after=after, limit=_limit(limit))
    return Response(
        content=text.encode("utf-8"),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-TT-Audit-Head": str(page["headHash"]),
            "X-TT-Audit-Next": str(page["nextAfter"]),
        },
    )


# 锚点与合规报告的端点各自带完整路径，挂在同一个 router 下即随本 router 一起注册。
router.include_router(anchor_router)
router.include_router(compliance_router)
