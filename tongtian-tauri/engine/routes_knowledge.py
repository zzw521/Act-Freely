"""knowledge 车道（W2）：知识库混合检索 / 技能统一注册表 / 产物版本历史。

新的引擎端点写在这里，不要再打开 engine/server.py（#164 接缝，见
docs/并行实施/交棒清单.md）。既有 /api/knowledge/search 仍留在 server.py 里
——它被 tests/unit/bridge/knowledge-retrieval-contract.test.ts 逐字钉住，
改动请提分片给 W4。

规则：
- 只许占用 routes_lanes.LANE_PREFIXES["knowledge"] 里的前缀，越界会在挂载时抛。
- 不许 import server；引擎主模块用 ``rl.engine()`` 取（会话表、鉴权、WORK_DIR 都在里面）。
- 所有 /api/ 路径自动过 server.py 的令牌守卫与审计，本文件不需要再写鉴权。
- 工具面（不是端点面）请写 engine/toolkit_knowledge.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import artifact_history_runtime as ah
import data_paths
import knowledge_runtime as kr
import knowledge_vector_runtime as kv
import routes_lanes as rl
import skill_registry_runtime as sr

LANE = "knowledge"
OWNER = rl.LANE_OWNERS["knowledge"]

router = APIRouter()


def _denied(code: str, message: str, status: int = 403) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status)


def _engine() -> Any:
    return rl.engine()


def _in_engine_roots(target: Path) -> bool:
    """产物历史四个端点共用的路径闸。

    比 `/api/files/content` 的读口径**更严**：那个端点允许桌面壳读本机任意文件，
    这里不管调用方是不是桌面壳，一律只认引擎工作目录与数据根。理由是历史目录本身
    会把内容副本写进数据根——范围放宽就等于让任意路径都能往数据根里灌一份拷贝，
    而「能查不能存」这种口径不一致的死角，正是历史上反复出事的地方。
    """
    eng = _engine()
    roots = eng._engine_data_roots()
    return bool(eng._path_within(target, roots))


def _resolved(raw: str) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


@router.get("/api/artifacts/history")
async def artifacts_history(request: Request, path: str = ""):
    """列出某个产物的历史版本（从新到旧）；没留过档返回空列表而不是报错。"""
    target = _resolved(path)
    if target is None:
        return _denied("ARTIFACT_PATH_REQUIRED", "缺少 path 参数", 400)
    if not _in_engine_roots(target):
        return _denied("ARTIFACT_PATH_DENIED", "该路径不在引擎工作目录或数据根内，产物历史不覆盖它")
    return JSONResponse(ah.history_view(target))


@router.get("/api/artifacts/history/diff")
async def artifacts_history_diff(request: Request, path: str = "", version: str = ""):
    """某一版相对前一版的差异；二进制、差异过大、前一版已被清理都如实说明。"""
    target = _resolved(path)
    if target is None or not str(version or "").strip():
        return _denied("ARTIFACT_ARGS_REQUIRED", "需要 path 与 version 两个参数", 400)
    if not _in_engine_roots(target):
        return _denied("ARTIFACT_PATH_DENIED", "该路径不在引擎工作目录或数据根内，产物历史不覆盖它")
    return JSONResponse(ah.diff_version(target, str(version).strip()))


@router.get("/api/artifacts/history/stats")
async def artifacts_history_stats(request: Request):
    """历史目录占用与版本数：界面显示「留档花了多少盘」，也作为配额清理的输入。"""
    payload = ah.stats()
    payload["ok"] = True
    return JSONResponse(payload)


@router.post("/api/artifacts/history/capture")
async def artifacts_history_capture(request: Request):
    """覆盖前留档。写入方（Rust 工具口或引擎闭包）在真正落盘前调用一次。

    留的是**即将被盖掉的那份**；文件还不存在时不算错误，回执里写清「首次创建，
    没有旧内容需要留档」——调用方不需要自己判断该不该调。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001  没有请求体就是没参数，按缺参处理
        payload = {}
    body = payload if isinstance(payload, dict) else {}
    target = _resolved(str(body.get("path") or ""))
    if target is None:
        return _denied("ARTIFACT_PATH_REQUIRED", "缺少 path 参数", 400)
    if not _in_engine_roots(target):
        return _denied("ARTIFACT_PATH_DENIED", "该路径不在引擎工作目录或数据根内，产物历史不覆盖它")
    result = ah.record_current(
        target,
        task_id=str(body.get("task") or body.get("taskId") or ""),
        tool=str(body.get("tool") or ""),
        reason=str(body.get("reason") or ""),
    )
    status = 200 if result.get("ok") else 422
    return JSONResponse(result, status_code=status)


@router.post("/api/artifacts/history/restore")
async def artifacts_history_restore(request: Request):
    """把某一版写回原路径（回滚前自动把当前内容留成新版本）。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    body = payload if isinstance(payload, dict) else {}
    version = str(body.get("version") or "").strip()
    target = _resolved(str(body.get("path") or ""))
    if target is None or not version:
        return _denied("ARTIFACT_ARGS_REQUIRED", "需要 path 与 version 两个参数", 400)
    if not _in_engine_roots(target):
        return _denied("ARTIFACT_PATH_DENIED", "该路径不在引擎工作目录或数据根内，产物历史不覆盖它")
    result = ah.restore(target, version, task_id=str(body.get("task") or body.get("taskId") or ""))
    status = 200 if result.get("ok") else 422
    return JSONResponse(result, status_code=status)


# ── 检索口径（D-09）：只自述，不改变 ───────────────────────────


@router.get("/api/knowledge/retrieval")
async def knowledge_retrieval(request: Request, prefer: str = ""):
    """当前检索口径的**如实自述**：模式、选中的嵌入来源、是不是语义召回、为什么。

    桌面「选哪种检索」缺的第一段就是这一段（界面得先能说清「本机没有可用的语义嵌入模型」，
    否则用户以为开了向量就有语义）。**这条端点不改任何配置**：模式现在只由
    `TT_KNOWLEDGE_RETRIEVAL` 决定，落到可持久化的用户设置需要一个配置归属，见
    `docs/并行实施/137.md` §5 的请求。
    `prefer` 只做一次**试运行**（`builtin` / `local` / `remote` / `lexical`），
    不改选中的结果，用来回答「如果我指定本地模型会怎样」。
    """
    mode = kr.retrieval_mode()
    provider, notes = kv.resolve_provider(prefer)
    if mode != "hybrid":
        notes = [
            f"{kr.RETRIEVAL_ENV}={mode}：本轮只走词法召回，下面列出的嵌入来源并没有被用上"
        ] + notes
    return JSONResponse({
        "ok": True,
        "mode": mode,
        "modes": list(kr.RETRIEVAL_MODES),
        "envVar": kr.RETRIEVAL_ENV,
        "vectorRecall": provider is not None and mode == "hybrid",
        "provider": "" if provider is None else provider.key,
        "providerLabel": "" if provider is None else provider.label,
        "semantic": bool(provider.semantic) if provider is not None else False,
        "dimension": provider.dimension if provider is not None else 0,
        "source": "" if provider is None else provider.source,
        "notes": notes,
    })


# ── 技能共享池（D-38）：只读视图 + 核对视图 + 刷新 ─────────────────
#
# 这三条**故意不接**「启用/停用给某个专家」：Rust 侧有全局停用位（设置项 `skill.disabled`），
# 池里是按专家的启停映射，两份还没定谁是权威。在定下来之前开写面就是双写。


@router.get("/api/skills/pool")
async def skills_pool(request: Request):
    """共享池只读视图：池里有什么、来源还在不在、生效给哪些专家。**不写盘**。

    数据根只取 `data_paths.engine_home()`，**不接受调用方传目录**——池里的条目带着
    正文文件的绝对路径，允许外部指定 home 等于开一条任意文件读。
    """
    view = sr.pool_view(data_paths.engine_home())
    view["ok"] = True
    return JSONResponse(view)


@router.get("/api/skills/pool/resolved")
async def skills_pool_resolved(request: Request, expert: str = ""):
    """某个专家本轮实际会拿到哪些技能（含正文）与「为什么少了一条」的说明。

    这是**核对视图**不是装配现场：这里没有装配现场的可用工具/连接器清单，
    所以依赖检查走「未检查」那条口径——照样放行，但 notes 会说明没检查。
    `expert` 留空表示只看缺省启用位（没单独设置过的专家走的那一档）。
    """
    pool = sr.load_pool(data_paths.engine_home())
    key = str(expert or "").strip()
    skills, notes = pool.resolve_for_expert(key)
    if not pool.writable():
        notes.insert(0, pool.load_error)
    return JSONResponse({"ok": True, "expert": key, "skills": skills, "notes": notes})


@router.post("/api/skills/pool/sync")
async def skills_pool_sync(request: Request):
    """把磁盘上新装/改过的技能并入池并落盘（页面「刷新」按钮点这个）。

    只并清单：已有条目的启用态保留，同 id 换来源时已安装的那份优先。
    `pool.json` 读坏时 `sync_pool` 内部那次 `save()` 会拒绝覆盖，原因原样在 notes 里，
    端点不猜「有没有落盘成功」——`ok` 只报池子当前可不可写（读坏了就是不可写）。
    """
    home = data_paths.engine_home()
    pool, notes = sr.sync_pool(home)
    payload: dict[str, Any] = {
        "ok": pool.writable(),
        "writable": pool.writable(),
        "loadError": pool.load_error,
        "count": len(pool.entries),
        "notes": notes,
    }
    return JSONResponse(payload, status_code=200 if payload["ok"] else 422)
