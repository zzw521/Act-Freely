"""桌面端市场索引适配层（D-07.4）

桌面客户端（`src-tauri/src/market`）读的不是控制面那套分页接口，而是：

    GET /api/v1/desktop/skills.json → {"skills":[{…,"skillMdUrl","sha256"}],
                                        "signaturePolicy","publishers","revokedVersions","revokedUrl"}
    GET /api/v1/desktop/suites.json → 同形，条目上给的是 "zipUrl"
    GET {skillMdUrl}                → 一份 SKILL.md 正文（技能条目在桌面端就是**一个 Markdown 文件**）
    GET {zipUrl}                    → 套件 zip（与 Web 下载复用同一条路由、同一只包）
    GET /api/v1/desktop/revoked.json→ 裸数组的撤销清单（桌面端只会拉一次，不分页）

两处粒度不同，需要适配而不是改存储：

1. 控制面 `/api/v1/{plural}` 返回 `{ok, items[], total, page…}`，桌面端要的是**一次给全**
   的扁平清单（上限 500 条），且每条必须自带产物地址与 64 位十六进制 SHA-256；
2. 服务端技能存的是 zip 包，桌面端技能装的是 zip 里那份 SKILL.md 的**字节本身**，
   索引里的 sha256 必须是那份字节的摘要——不是 zip 的摘要。给错哈希会让每一次安装都
   在 `MARKET_HASH_MISMATCH` 上失败，所以这里在索引时真实解包取文件，不猜。

版本选取：每个条目取**最新发布的一版**（撤销的不算「最新」，但仍在 `revokedVersions`
里如实声明，让桌面端能把本机已装的旧版本禁用掉）。产物已损坏/取不到的条目**不进索引**：
留在清单里只会让用户点一次必然失败的下载，比不列出来更糟。

签名：桌面端 `signaturePolicy` 支持 required/optional，服务端目前**没有发布方密钥**
（`keys.py` 那对是网关 RS256 登录令牌用的，不是内容签名密钥），所以这里固定
`optional` 且不写 `publishers`，属于如实降级，不是「已验签」。
"""

from __future__ import annotations

import logging
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .config import settings
from .db import tx
from .errors import ApiError
from .market import _validate_item_id
from .security import normalize_version, sha256_hex

logger = logging.getLogger("tongtian.desktop_market")
router = APIRouter()

#: 桌面端 INDEX_MAX_ITEMS / 索引体积上限，超了整份索引会被拒收，宁可不给
DESKTOP_MAX_ITEMS = 500
DESKTOP_MAX_REVOKED = 500
#: 条目名/摘要在索引里的截断长度（客户端也会截，这里先按它看得懂的长度给）
DESKTOP_NAME_LIMIT = 120
DESKTOP_TEXT_LIMIT = 500

#: (item_id, version, 包 sha256) → (SKILL.md 字节, 其 sha256)
#: 包文件按内容寻址且发布后不可变，用包摘要做键就是不可变缓存键
_skill_md_cache: dict[tuple[str, str, str], tuple[bytes, str]] = {}
_SKILL_MD_CACHE_MAX = 256


def _base_url(request: Request) -> str:
    """索引里所有地址的基准：回环演练给 http://127.0.0.1:端口，公网部署给 https 域名。

    取的是请求自身的 scheme+host，因此反向代理后面要正确设置 X-Forwarded-Proto/Host，
    否则桌面端会拿到一个 http 的远程地址并在自己的规则下被拒（远程必须 HTTPS）。
    """
    return str(request.base_url).rstrip("/")


def _text(value: Any, limit: int) -> str | None:
    text = (str(value) if value is not None else "").strip()
    if not text:
        return None
    return text[:limit]


def _skill_md_of(package_path: Path, package_sha256: str, item_id: str, version: str) -> tuple[bytes, str]:
    """从技能包里取出 SKILL.md 的字节与其摘要（与发布时同一套入口规则：根目录或单层目录）。"""
    key = (item_id, version, package_sha256)
    hit = _skill_md_cache.get(key)
    if hit is not None:
        return hit
    try:
        with zipfile.ZipFile(package_path) as zf:
            names = zf.namelist()
            matches = [
                info
                for info in zf.infolist()
                if len(Path(info.filename).parts) <= 2
                and Path(info.filename).name.lower() == "skill.md"
                and not info.is_dir()
            ]
            if len(matches) != 1:
                raise ApiError(
                    410,
                    "PACKAGE_CORRUPTED",
                    f"技能包里的 SKILL.md 入口不唯一（{len(matches)} 个，共 {len(names)} 项）",
                )
            raw = zf.read(matches[0])
    except zipfile.BadZipFile as exc:
        raise ApiError(410, "PACKAGE_CORRUPTED", "技能包不是合法 zip，无法为桌面端提供正文") from exc
    if len(raw) > settings.max_skill_md_bytes:
        raise ApiError(410, "PACKAGE_CORRUPTED", "技能正文超过桌面端上限，不下发")
    value = (raw, sha256_hex(raw))
    if len(_skill_md_cache) >= _SKILL_MD_CACHE_MAX:
        _skill_md_cache.clear()
    _skill_md_cache[key] = value
    return value


def _package_file(item_type: str, item_id: str, version_value: str, package_path: str | None) -> Path:
    if not package_path:
        raise ApiError(404, "NO_PACKAGE", "该版本没有可下载的包")
    full = (settings.data_dir / package_path).resolve()
    if not full.is_relative_to(settings.data_dir.resolve()) or not full.is_file():
        raise ApiError(410, "PACKAGE_MISSING", "包文件缺失，请联系管理员")
    return full


def _rows(conn, item_type: str, limit: int) -> list:
    """每个条目取最新发布的一版（撤销版本不参与「最新」的选取）。"""
    return conn.execute(
        "SELECT i.item_id, i.name, i.summary, i.category, "
        "       v.version, v.manifest_json, v.package_path, v.package_sha256, "
        "       v.package_size, v.published_at_ms, v.revoked_at_ms, v.revoked_reason "
        "FROM items i "
        "JOIN item_versions v ON v.item_type = i.item_type AND v.item_id = i.item_id "
        "WHERE i.item_type = ?1 AND v.revoked_at_ms = 0 AND v.package_path IS NOT NULL "
        "  AND v.rowid = ("
        "        SELECT v2.rowid FROM item_versions v2 "
        "        WHERE v2.item_type = i.item_type AND v2.item_id = i.item_id "
        "          AND v2.revoked_at_ms = 0 "
        "        ORDER BY v2.published_at_ms DESC, v2.rowid DESC LIMIT 1"
        "      ) "
        "ORDER BY v.published_at_ms DESC LIMIT ?2",
        (item_type, limit),
    ).fetchall()


def _revoked_rows(conn, item_type: str) -> list:
    return conn.execute(
        "SELECT item_id, version, revoked_reason FROM item_versions "
        "WHERE item_type = ?1 AND revoked_at_ms > 0 "
        "ORDER BY revoked_at_ms DESC, rowid DESC LIMIT ?2",
        (item_type, DESKTOP_MAX_REVOKED),
    ).fetchall()


def _build_index(plural: str, item_type: str, request: Request) -> dict:
    base = _base_url(request)
    items: list[dict] = []
    skipped = 0
    with tx() as conn:
        for row in _rows(conn, item_type, DESKTOP_MAX_ITEMS + 1):
            if len(items) >= DESKTOP_MAX_ITEMS:
                skipped += 1
                continue
            item_id = row["item_id"]
            version_value = normalize_version(row["version"]) or row["version"]
            # 桌面端会把 sizeBytes 拿去和自己那份产物的上限比：技能的上限是正文大小、
            # 套件的上限是 zip 大小。给错尺寸会让合法条目被整份索引拒收。
            try:
                if item_type == "skill":
                    path = _package_file(item_type, item_id, version_value, row["package_path"])
                    raw, digest = _skill_md_of(path, row["package_sha256"], item_id, version_value)
                    artifact_url = (
                        f"{base}/api/v1/desktop/skills/{item_id}/{urllib.parse.quote(version_value)}/SKILL.md"
                    )
                    artifact_size = len(raw)
                else:
                    # 套件产物就是那只 zip，直接复用 Web 下载路由：同一只包、同一份计数
                    digest = (row["package_sha256"] or "").strip().lower()
                    artifact_url = (
                        f"{base}/api/v1/suites/{item_id}/download"
                        f"?version={urllib.parse.quote(version_value)}"
                    )
                    artifact_size = int(row["package_size"] or 0)
            except ApiError as exc:
                # 拿不到可靠正文/摘要的条目不进索引：列出来只会是一次必然失败的下载
                logger.warning("桌面索引跳过 %s/%s@%s：%s", item_type, item_id, version_value, exc.message)
                skipped += 1
                continue
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                logger.warning("桌面索引跳过 %s/%s：SHA-256 不是 64 位十六进制", item_type, item_id)
                skipped += 1
                continue
            items.append(
                {
                    "id": item_id,
                    "name": _text(row["name"], DESKTOP_NAME_LIMIT) or item_id,
                    "description": _text(row["summary"], DESKTOP_TEXT_LIMIT),
                    "version": version_value,
                    "category": _text(row["category"], 40),
                    "sizeBytes": artifact_size,
                    "publishedAtMs": int(row["published_at_ms"] or 0),
                    "sha256": digest,
                    ("skillMdUrl" if item_type == "skill" else "zipUrl"): artifact_url,
                }
            )

        revoked = [
            {
                "kind": item_type,
                "id": row["item_id"],
                "version": normalize_version(row["version"]) or row["version"],
                "reason": _text(row["revoked_reason"], 200) or "市场未给出原因",
            }
            for row in _revoked_rows(conn, item_type)
        ]

    if skipped:
        logger.info("桌面索引 %s：%s 条可下发，%s 条被跳过", plural, len(items), skipped)
    return {
        "signaturePolicy": "optional",
        "publishers": [],
        plural: items,
        # 顶层撤销声明：本机已装但已被撤销的版本靠这个被禁用
        "revokedVersions": revoked,
        "revokedUrl": f"{base}/api/v1/desktop/revoked.json",
    }


@router.get("/api/v1/desktop/skills.json")
def api_desktop_skills(request: Request):
    """桌面端技能索引：一次给全，每条自带正文地址与正文摘要。

    两类索引按显式路径分别声明而不是写成 `/desktop/{plural}.json` 通配：通配会把
    `revoked.json` 也吃掉（`{plural}` 收到 "revoked"），撤销清单就永远 404，
    而桌面端只拉一次清单——漏掉的已撤销版本再也禁不掉。
    """
    return _build_index("skills", "skill", request)


@router.get("/api/v1/desktop/suites.json")
def api_desktop_suites(request: Request):
    """桌面端套件索引：产物是 zip，摘要用包摘要。"""
    return _build_index("suites", "suite", request)


@router.get("/api/v1/desktop/skills/{item_id}/{version}/SKILL.md")
def api_desktop_skill_md(item_id: str, version: str):
    """桌面端技能产物：SKILL.md 的原始字节（索引里的 sha256 就是它的摘要）。"""
    item_id = _validate_item_id(item_id)
    requested = normalize_version(version)
    if requested is None:
        raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")
    with tx() as conn:
        row = conn.execute(
            "SELECT version, package_path, package_sha256, revoked_at_ms, revoked_reason "
            "FROM item_versions WHERE item_type = 'skill' AND item_id = ?1 AND version = ?2",
            (item_id, requested),
        ).fetchone()
        if row is None:
            raise ApiError(404, "VERSION_NOT_FOUND", "指定版本不存在")
        if row["revoked_at_ms"]:
            reason = _text(row["revoked_reason"], 200) or "已被市场撤销"
            raise ApiError(410, "ITEM_REVOKED", f"该版本已被撤销：{reason}")
        path = _package_file("skill", item_id, row["version"], row["package_path"])
        raw, digest = _skill_md_of(path, row["package_sha256"], item_id, row["version"])
    return Response(
        content=raw,
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Checksum-Sha256": digest,
            "ETag": f'"{digest}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@router.get("/api/v1/desktop/revoked.json")
def api_desktop_revoked():
    """撤销清单（桌面端口径）：裸数组、不分页、上限 500 条。

    控制面 `/api/v1/revoked` 是分页的，桌面端只会拉一次——把它直接指过去会只看得到
    前 20 条撤销记录，第 21 条之后的已撤销版本就再也禁不掉，所以这里单独给一条。
    """
    out: list[dict] = []
    with tx() as conn:
        for item_type in ("skill", "suite"):
            for row in _revoked_rows(conn, item_type):
                out.append(
                    {
                        "kind": item_type,
                        "id": row["item_id"],
                        "version": normalize_version(row["version"]) or row["version"],
                        "reason": _text(row["revoked_reason"], 200) or "市场未给出原因",
                    }
                )
    return out
