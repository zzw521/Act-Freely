"""在线市场：技能、专家套件、MCP 插件的发布、检索、详情、下载与收藏。"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .auth import bearer_user, require_admin, require_user
from .catalog import TYPE_SPECS, categories_of, normalize_category, type_of_plural
from .config import settings
from .db import now_ms, tx
from .errors import ApiError
from .packages import CONTROL_RE, inspect_skill_package, inspect_suite_package
from .security import (
    ENV_KEY_RE,
    ITEM_ID_RE,
    item_id_of,
    new_upload_token,
    normalize_version,
    sha256_hex,
)

logger = logging.getLogger("tongtian.market")
router = APIRouter()
_publish_lock = threading.Lock()

MAX_SUMMARY = 200
MAX_DESCRIPTION = 5000
MAX_README = 512 * 1024
MAX_CHANGELOG = 5000
MAX_TAGS = 8
MAX_TAG_LEN = 24
MAX_ICON_LEN = 64


# ── 通用校验 ─────────────────────────────────────────────────


def _clean_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise ApiError(400, "INVALID_FIELD", f"{field}不能为空")
    if CONTROL_RE.search(text):
        raise ApiError(400, "INVALID_FIELD", f"{field}含不可见控制字符")
    if len(text) > limit:
        raise ApiError(400, "INVALID_FIELD", f"{field}超过 {limit} 字上限")
    return text


def _clean_readme(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) > MAX_README:
        raise ApiError(400, "INVALID_FIELD", "readme 超过 512KB 字符上限")
    return text


def _normalize_tags(raw: Any, *, strict: bool) -> list[str] | None:
    if raw is None:
        return None
    values: list[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                raise ApiError(400, "INVALID_TAGS", "tags 不是合法的 JSON 数组") from None
            if not isinstance(parsed, list):
                raise ApiError(400, "INVALID_TAGS", "tags 必须是数组")
            values = parsed
        else:
            values = re.split(r"[,，]", text)
    elif isinstance(raw, list):
        values = raw
    else:
        if strict:
            raise ApiError(400, "INVALID_TAGS", "tags 必须是数组或逗号分隔字符串")
        return []

    tags: list[str] = []
    for item in values:
        if not isinstance(item, str):
            if strict:
                raise ApiError(400, "INVALID_TAGS", "每个标签都必须是字符串")
            continue
        tag = item.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN or CONTROL_RE.search(tag):
            if strict:
                raise ApiError(400, "INVALID_TAGS", f"标签需为 1~{MAX_TAG_LEN} 个可见字符")
            continue
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        if strict:
            raise ApiError(400, "INVALID_TAGS", f"标签最多 {MAX_TAGS} 个")
        tags = tags[:MAX_TAGS]
    return tags


def _resolve_category(item_type: str, raw: Any, existing: str | None, *, strict: bool) -> str:
    value = raw.strip() if isinstance(raw, str) else ""
    if value:
        if value in categories_of(item_type):
            return value
        if strict:
            raise ApiError(
                400,
                "INVALID_CATEGORY",
                f"分类「{value}」不在允许范围内：{'、'.join(categories_of(item_type))}",
            )
    if existing:
        return existing
    return "其他"


def _resolve_version(
    declared: str | None, requested: str | None, *, creating: bool
) -> str:
    explicit = None
    if requested:
        explicit = normalize_version(requested)
        if explicit is None:
            raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")
    if explicit and declared and explicit != declared:
        raise ApiError(
            400,
            "VERSION_MISMATCH",
            f"提交版本 {explicit} 与包内声明版本 {declared} 不一致",
        )
    final = explicit or declared
    if final is None:
        if creating:
            final = "1.0.0"
        else:
            raise ApiError(400, "VERSION_REQUIRED", "发布新版本必须提供版本号或由包内声明")
    return final


def _tags_from_json(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
        return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _tags_json(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False)


def _like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _require_plural(plural: str) -> str:
    item_type = type_of_plural(plural)
    if item_type is None:
        raise ApiError(404, "MARKET_NOT_FOUND", f"市场类型不存在：{plural}")
    return item_type


def _require_item_type(raw: str) -> str:
    item_type = raw.strip().lower()
    if item_type not in TYPE_SPECS:
        raise ApiError(400, "INVALID_TYPE", "type 仅支持 skill / suite / mcp")
    return item_type


def _validate_item_id(item_id: str) -> str:
    value = item_id.strip()
    if not ITEM_ID_RE.match(value):
        raise ApiError(400, "INVALID_ITEM_ID", "条目标识不合法")
    return value


# ── 查询视图 ─────────────────────────────────────────────────


_ITEM_SELECT = """
SELECT i.item_type, i.item_id, i.name, i.summary, i.description, i.category,
       i.tags_json, i.icon, i.author_uid, i.featured, i.created_at_ms, i.updated_at_ms,
       u.nickname AS author_nickname,
       (SELECT COALESCE(SUM(v.download_count), 0) FROM item_versions v
          WHERE v.item_type = i.item_type AND v.item_id = i.item_id) AS downloads,
       (SELECT COUNT(*) FROM stars s
          WHERE s.item_type = i.item_type AND s.item_id = i.item_id AND s.active = 1) AS stars,
       (SELECT COUNT(*) FROM item_versions v
          WHERE v.item_type = i.item_type AND v.item_id = i.item_id) AS version_count,
       (SELECT v.version FROM item_versions v
          WHERE v.item_type = i.item_type AND v.item_id = i.item_id
            AND v.revoked_at_ms = 0
          ORDER BY v.published_at_ms DESC, v.rowid DESC LIMIT 1) AS latest_version,
       (SELECT v.published_at_ms FROM item_versions v
          WHERE v.item_type = i.item_type AND v.item_id = i.item_id
            AND v.revoked_at_ms = 0
          ORDER BY v.published_at_ms DESC, v.rowid DESC LIMIT 1) AS latest_published_at_ms,
       EXISTS(SELECT 1 FROM stars s2
          WHERE s2.uid = ?1 AND s2.item_type = i.item_type AND s2.item_id = i.item_id
            AND s2.active = 1) AS starred
FROM items i
JOIN users u ON u.uid = i.author_uid
"""


def _summary_of(row: sqlite3.Row, item_type: str) -> dict:
    plural = TYPE_SPECS[item_type]["plural"]
    latest = row["latest_version"]
    revoked = bool(row["version_count"]) and latest is None
    download_url = None
    if item_type != "mcp" and latest:
        download_url = f"/api/v1/{plural}/{row['item_id']}/download?version={urllib.parse.quote(latest)}"
    return {
        "id": row["item_id"],
        "type": item_type,
        "name": row["name"],
        "summary": row["summary"],
        "category": row["category"],
        "tags": _tags_from_json(row["tags_json"]),
        "icon": row["icon"] or TYPE_SPECS[item_type]["default_icon"],
        "author": {"uid": row["author_uid"], "nickname": row["author_nickname"]},
        "featured": bool(row["featured"]),
        "revoked": revoked,
        "latestVersion": latest,
        "versionCount": row["version_count"],
        "downloads": row["downloads"],
        "stars": row["stars"],
        "starred": bool(row["starred"]),
        "publishedAtMs": row["latest_published_at_ms"],
        "createdAtMs": row["created_at_ms"],
        "updatedAtMs": row["updated_at_ms"],
        "downloadUrl": download_url,
    }


def _version_view(row: sqlite3.Row, item_type: str, item_id: str) -> dict:
    plural = TYPE_SPECS[item_type]["plural"]
    revoked = bool(row["revoked_at_ms"])
    has_package = bool(row["package_path"]) and not revoked
    download_url = None
    if item_type != "mcp" and has_package:
        download_url = (
            f"/api/v1/{plural}/{item_id}/download?version={urllib.parse.quote(row['version'])}"
        )
    return {
        "version": row["version"],
        "changelog": row["changelog"],
        "sha256": row["package_sha256"] or None,
        "sizeBytes": row["package_size"] or 0,
        "downloads": row["download_count"],
        "publishedAtMs": row["published_at_ms"],
        "revoked": revoked,
        "revokedAtMs": row["revoked_at_ms"] or None,
        "revokedReason": row["revoked_reason"] or None,
        "downloadUrl": download_url,
    }


def _install_hint(item_type: str, item_id: str) -> dict:
    if item_type == "skill":
        return {
            "kind": "skill",
            "packageFormat": "zip",
            "entry": "SKILL.md",
            "target": f"skills/{item_id}",
        }
    if item_type == "suite":
        return {
            "kind": "suite",
            "packageFormat": "zip",
            "entry": "plugin.json",
            "target": f"suites/{item_id}",
        }
    return {
        "kind": "mcp",
        "mode": "config",
        "configEndpoint": f"/api/v1/mcp/{item_id}/config",
    }


def get_item_detail(item_type: str, item_id: str, requester_uid: str | None = None) -> dict:
    with tx() as conn:
        row = conn.execute(
            _ITEM_SELECT + " WHERE i.item_type = ?2 AND i.item_id = ?3",
            (requester_uid or "", item_type, item_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
        versions = conn.execute(
            "SELECT version, readme, changelog, manifest_json, package_path, package_size, "
            "package_sha256, published_at_ms, download_count, "
            "revoked_at_ms, revoked_reason "
            "FROM item_versions WHERE item_type = ?1 AND item_id = ?2 "
            "ORDER BY published_at_ms DESC, rowid DESC",
            (item_type, item_id),
        ).fetchall()

    item = _summary_of(row, item_type)
    latest = next((version for version in versions if not version["revoked_at_ms"]), None)
    manifest: dict = {}
    if latest is not None:
        try:
            parsed = json.loads(latest["manifest_json"] or "{}")
            manifest = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            manifest = {}
    item.update(
        {
            "description": row["description"],
            "readme": latest["readme"] if latest else "",
            "versions": [_version_view(version, item_type, item_id) for version in versions],
            "install": _install_hint(item_type, item_id),
        }
    )
    if item_type == "suite":
        item["commands"] = manifest.get("commands") if isinstance(manifest.get("commands"), list) else []
        item["packageInfo"] = {
            "fileCount": manifest.get("fileCount", 0),
            "files": manifest.get("files", []),
            "skillCount": manifest.get("skillCount", 0),
            "dataLinkCount": manifest.get("dataLinkCount", 0),
        }
    elif item_type == "skill":
        item["packageInfo"] = {
            "fileCount": manifest.get("fileCount", 0),
            "files": manifest.get("files", []),
        }
    else:
        item["mcp"] = manifest
    return item


def _list_items(
    *,
    item_type: str | None,
    q: str,
    category: str,
    sort: str,
    featured: str | None,
    page: int,
    page_size: int,
    requester_uid: str | None,
    author_uid: str | None = None,
) -> dict:
    if sort not in ("recommended", "latest", "downloads", "stars", "name"):
        raise ApiError(400, "INVALID_SORT", "sort 仅支持 recommended / latest / downloads / stars / name")
    q = (q or "").strip()
    if len(q) > 100:
        raise ApiError(400, "INVALID_QUERY", "搜索词不能超过 100 字")
    category = (category or "").strip()
    if category and item_type is not None and category not in categories_of(item_type):
        raise ApiError(400, "INVALID_CATEGORY", f"分类不存在：{category}")
    featured_flag: bool | None = None
    if featured is not None and featured != "":
        lowered = featured.lower()
        if lowered in ("true", "1"):
            featured_flag = True
        elif lowered in ("false", "0"):
            featured_flag = False
        else:
            raise ApiError(400, "INVALID_QUERY", "featured 仅支持 true/false")
    page = max(1, page)
    page_size = min(100, max(1, page_size))

    conditions: list[str] = []
    params: list[Any] = []
    if item_type:
        conditions.append("i.item_type = ?")
        params.append(item_type)
    if author_uid:
        conditions.append("i.author_uid = ?")
        params.append(author_uid)
    if q:
        pattern = _like_pattern(q)
        conditions.append(
            "(i.name LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\' "
            "OR i.description LIKE ? ESCAPE '\\' OR i.tags_json LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])
    if category:
        conditions.append("i.category = ?")
        params.append(category)
    if featured_flag is not None:
        conditions.append("i.featured = ?")
        params.append(1 if featured_flag else 0)
    where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    order_sql = {
        "recommended": "i.featured DESC, downloads DESC, i.updated_at_ms DESC",
        "latest": "i.updated_at_ms DESC",
        "downloads": "downloads DESC, i.updated_at_ms DESC",
        "stars": "stars DESC, i.updated_at_ms DESC",
        "name": "i.name COLLATE NOCASE ASC",
    }[sort]

    with tx() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM items i" + where_sql, params
        ).fetchone()["n"]
        rows = conn.execute(
            _ITEM_SELECT
            + where_sql
            + f" ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [requester_uid or "", *params, page_size, (page - 1) * page_size],
        ).fetchall()

    type_label = TYPE_SPECS[item_type]["label"] if item_type else "全部"
    return {
        "ok": True,
        "type": item_type,
        "typeLabel": type_label,
        "items": [_summary_of(row, item_type or row["item_type"]) for row in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "hasMore": page * page_size < total,
    }


# ── 发布落盘 ─────────────────────────────────────────────────


def _store_package(item_type: str, item_id: str, version: str, data: bytes) -> tuple[Path, str]:
    directory = settings.packages_dir / item_type / item_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{version}-{new_upload_token()}.zip"
    temp = target.with_suffix(".tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return target, target.relative_to(settings.data_dir).as_posix()


def _insert_item(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    item_id: str,
    name: str,
    summary: str,
    description: str,
    category: str,
    tags: list[str],
    icon: str,
    author_uid: str,
    now: int,
) -> None:
    conn.execute(
        "INSERT INTO items (item_type, item_id, name, summary, description, category, "
        "tags_json, icon, author_uid, featured, created_at_ms, updated_at_ms) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 0, ?10, ?10)",
        (item_type, item_id, name, summary, description, category, _tags_json(tags), icon, author_uid, now),
    )


def _update_item(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    item_id: str,
    summary: str,
    description: str,
    category: str,
    tags: list[str],
    icon: str,
    now: int,
) -> None:
    conn.execute(
        "UPDATE items SET summary = ?3, description = ?4, category = ?5, tags_json = ?6, "
        "icon = ?7, updated_at_ms = ?8 WHERE item_type = ?1 AND item_id = ?2",
        (item_type, item_id, summary, description, category, _tags_json(tags), icon, now),
    )


def _shared_meta(
    *,
    item_type: str,
    existing: sqlite3.Row | None,
    summary: str | None,
    description: str | None,
    category: str | None,
    tags: list[str] | None,
    icon: str | None,
    package_summary: str,
    package_description: str,
    package_category: str | None,
    package_tags: list[str],
) -> tuple[str, str, str, list[str], str]:
    existing_summary = existing["summary"] if existing else ""
    existing_description = existing["description"] if existing else ""
    existing_category = existing["category"] if existing else "其他"
    existing_tags = _tags_from_json(existing["tags_json"]) if existing else []
    existing_icon = existing["icon"] if existing else ""

    final_summary = (
        _clean_text(summary, "摘要", MAX_SUMMARY, required=True)
        if summary is not None
        else (package_summary or existing_summary).strip()
    )
    if not final_summary:
        raise ApiError(400, "SUMMARY_REQUIRED", "缺少摘要（summary/description），无法发布")
    final_summary = final_summary[:MAX_SUMMARY]

    final_description = (
        _clean_text(description, "描述", MAX_DESCRIPTION)
        if description is not None
        else (package_description or existing_description or final_summary)
    )
    final_category = (
        _resolve_category(item_type, category, existing_category, strict=True)
        if category is not None
        else _resolve_category(item_type, package_category, existing_category, strict=False)
    )
    final_tags = tags if tags is not None else (package_tags or existing_tags)
    final_icon = (
        _clean_text(icon, "图标", MAX_ICON_LEN)
        if icon is not None
        else (existing_icon or TYPE_SPECS[item_type]["default_icon"])
    )
    return final_summary, final_description, final_category, final_tags, final_icon


def publish_package(
    user: dict,
    item_type: str,
    data: bytes,
    filename: str,
    *,
    summary: str | None = None,
    description: str | None = None,
    category: str | None = None,
    tags: str | None = None,
    readme: str | None = None,
    changelog: str | None = None,
    icon: str | None = None,
    version: str | None = None,
    expected_item_id: str | None = None,
) -> dict:
    if item_type not in ("skill", "suite"):
        raise ApiError(400, "INVALID_TYPE", "该接口仅支持 skill / suite")
    if item_type == "skill":
        meta = inspect_skill_package(data, fallback_name=Path(filename or "").stem)
    else:
        meta = inspect_suite_package(data)

    name = meta["name"]
    item_id = item_id_of(item_type, name)
    if expected_item_id is not None and expected_item_id != item_id:
        raise ApiError(
            400,
            "PACKAGE_ID_MISMATCH",
            f"包声明的条目标识是 {item_id}，与路径中的 {expected_item_id} 不一致",
        )
    explicit_tags = _normalize_tags(tags, strict=True)
    final_readme = _clean_readme(readme) if readme is not None else meta.get("readme", "")
    final_changelog = _clean_text(changelog, "更新说明", MAX_CHANGELOG) if changelog is not None else ""

    stored_path: Path | None = None
    try:
        with _publish_lock, tx() as conn:
            existing = conn.execute(
                "SELECT * FROM items WHERE item_type = ?1 AND item_id = ?2",
                (item_type, item_id),
            ).fetchone()
            creating = expected_item_id is None
            if creating and existing is not None:
                raise ApiError(
                    409,
                    "ITEM_EXISTS",
                    f"同名{item_type}已存在；发布新版本请调用 /api/v1/{TYPE_SPECS[item_type]['plural']}/{item_id}/versions",
                )
            if not creating:
                if existing is None:
                    raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
                if existing["author_uid"] != user["uid"] and user.get("role") != "admin":
                    raise ApiError(403, "FORBIDDEN", "只能发布自己条目的新版本")

            final_version = _resolve_version(
                meta.get("declared_version"), version, creating=creating
            )
            if conn.execute(
                "SELECT 1 FROM item_versions WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
                (item_type, item_id, final_version),
            ).fetchone():
                raise ApiError(409, "VERSION_EXISTS", f"版本 {final_version} 已存在，版本包不可覆盖")

            final_summary, final_description, final_category, final_tags, final_icon = _shared_meta(
                item_type=item_type,
                existing=existing,
                summary=summary,
                description=description,
                category=category,
                tags=explicit_tags,
                icon=icon,
                package_summary=meta.get("summary", ""),
                package_description=meta.get("description") or meta.get("summary", ""),
                package_category=meta.get("category"),
                package_tags=meta.get("tags") or [],
            )

            now = now_ms()
            manifest = dict(meta.get("manifest") or {})
            manifest.update({"id": item_id, "name": name, "version": final_version})
            if creating:
                _insert_item(
                    conn,
                    item_type=item_type,
                    item_id=item_id,
                    name=name,
                    summary=final_summary,
                    description=final_description,
                    category=final_category,
                    tags=final_tags,
                    icon=final_icon,
                    author_uid=user["uid"],
                    now=now,
                )
            else:
                _update_item(
                    conn,
                    item_type=item_type,
                    item_id=item_id,
                    summary=final_summary,
                    description=final_description,
                    category=final_category,
                    tags=final_tags,
                    icon=final_icon,
                    now=now,
                )

            stored_path, relative_path = _store_package(item_type, item_id, final_version, data)
            try:
                conn.execute(
                    "INSERT INTO item_versions (item_type, item_id, version, readme, changelog, "
                    "manifest_json, package_path, package_size, package_sha256, published_at_ms, "
                    "download_count) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0)",
                    (
                        item_type,
                        item_id,
                        final_version,
                        final_readme,
                        final_changelog,
                        json.dumps(manifest, ensure_ascii=False),
                        relative_path,
                        len(data),
                        sha256_hex(data),
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ApiError(409, "VERSION_EXISTS", f"版本 {final_version} 已存在") from None
    except Exception:
        if stored_path is not None:
            stored_path.unlink(missing_ok=True)
        raise

    return get_item_detail(item_type, item_id, user["uid"])


# ── MCP 入参与校验 ──────────────────────────────────────────


class KeyHintIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    description: str = ""
    required: bool = True
    sensitive: bool = True


class McpBaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    transport: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    envKeys: list[KeyHintIn] = Field(default_factory=list)
    headerKeys: list[KeyHintIn] = Field(default_factory=list)
    timeoutSecs: int = 30
    readme: str | None = None
    changelog: str | None = None


class McpCreateIn(McpBaseIn):
    name: str
    summary: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    icon: str | None = None


class McpVersionIn(McpBaseIn):
    summary: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    icon: str | None = None


def _validate_hints(items: list[KeyHintIn], field: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hint in items:
        key = hint.key.strip()
        if not ENV_KEY_RE.match(key):
            raise ApiError(400, "INVALID_MCP_CONFIG", f"{field} 中的键名不合法：{key or '(空)'}")
        if key in seen:
            raise ApiError(400, "INVALID_MCP_CONFIG", f"{field} 中存在重复键：{key}")
        seen.add(key)
        out.append(
            {
                "key": key,
                "description": _clean_text(hint.description, f"{field}.description", 200),
                "required": bool(hint.required),
                "sensitive": bool(hint.sensitive),
            }
        )
    return out


def _validate_mcp_config(payload: McpBaseIn) -> dict:
    transport_raw = (payload.transport or "").strip().lower()
    aliases = {
        "stdio": "stdio",
        "streamable_http": "streamable_http",
        "streamablehttp": "streamable_http",
        "http": "streamable_http",
        "sse": "sse",
    }
    transport = aliases.get(transport_raw)
    if transport is None:
        raise ApiError(
            400, "INVALID_MCP_CONFIG", "transport 仅支持 stdio / streamable_http / sse"
        )
    if not 1 <= payload.timeoutSecs <= 300:
        raise ApiError(400, "INVALID_MCP_CONFIG", "timeoutSecs 需在 1~300 之间")

    command = (payload.command or "").strip()
    url = (payload.url or "").strip()
    args = payload.args or []
    if len(args) > 64:
        raise ApiError(400, "INVALID_MCP_CONFIG", "args 最多 64 项")
    for arg in args:
        if not isinstance(arg, str) or len(arg) > 1000 or CONTROL_RE.search(arg):
            raise ApiError(400, "INVALID_MCP_CONFIG", "args 中存在非法参数")
    env_keys = _validate_hints(payload.envKeys or [], "envKeys")
    header_keys = _validate_hints(payload.headerKeys or [], "headerKeys")

    if transport == "stdio":
        if not command:
            raise ApiError(400, "INVALID_MCP_CONFIG", "stdio 连接器必须填写 command")
        if len(command) > 500 or CONTROL_RE.search(command):
            raise ApiError(400, "INVALID_MCP_CONFIG", "command 非法")
        if url:
            raise ApiError(400, "INVALID_MCP_CONFIG", "stdio 连接器不能填写 url")
        if header_keys:
            raise ApiError(400, "INVALID_MCP_CONFIG", "stdio 连接器不支持 headerKeys")
        clean_url = ""
    else:
        if command:
            raise ApiError(400, "INVALID_MCP_CONFIG", "远程连接器不能填写 command")
        if args:
            raise ApiError(400, "INVALID_MCP_CONFIG", "远程连接器不能填写 args")
        if env_keys:
            raise ApiError(400, "INVALID_MCP_CONFIG", "远程连接器不支持 envKeys")
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            raise ApiError(400, "INVALID_MCP_CONFIG", "url 不合法") from None
        if parts.scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
            raise ApiError(400, "INVALID_MCP_CONFIG", "url 必须是完整的 http/https 地址")
        if parts.username or parts.password:
            raise ApiError(400, "INVALID_MCP_CONFIG", "url 中不能内嵌账号密码")
        if CONTROL_RE.search(url) or len(url) > 2048:
            raise ApiError(400, "INVALID_MCP_CONFIG", "url 非法")
        clean_url = url

    return {
        "transport": transport,
        "command": command if transport == "stdio" else "",
        "args": args if transport == "stdio" else [],
        "url": clean_url,
        "envKeys": env_keys,
        "headerKeys": header_keys,
        "timeoutSecs": payload.timeoutSecs,
        "requiresLocalExecution": transport == "stdio",
        "riskLevel": "high" if transport == "stdio" else "medium",
    }


def publish_mcp(
    user: dict,
    payload: McpCreateIn | McpVersionIn,
    *,
    expected_item_id: str | None = None,
) -> dict:
    creating = expected_item_id is None
    name = ""
    item_id = ""
    if isinstance(payload, McpCreateIn):
        name = _clean_text(payload.name, "插件名称", 80, required=True)
        item_id = item_id_of("mcp", name)
    else:
        item_id = expected_item_id or ""

    config = _validate_mcp_config(payload)
    version = normalize_version(payload.version)
    if version is None:
        raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")
    explicit_tags = _normalize_tags(payload.tags, strict=True)
    explicit_readme = _clean_readme(payload.readme) if payload.readme is not None else None
    explicit_changelog = (
        _clean_text(payload.changelog, "更新说明", MAX_CHANGELOG)
        if payload.changelog is not None
        else ""
    )

    with _publish_lock, tx() as conn:
        existing = conn.execute(
            "SELECT * FROM items WHERE item_type = 'mcp' AND item_id = ?1", (item_id,)
        ).fetchone()
        if creating:
            if existing is not None:
                raise ApiError(
                    409,
                    "ITEM_EXISTS",
                    f"同名 MCP 插件已存在；发布新版本请调用 /api/v1/mcp/{item_id}/versions",
                )
        else:
            if existing is None:
                raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
            if existing["author_uid"] != user["uid"] and user.get("role") != "admin":
                raise ApiError(403, "FORBIDDEN", "只能发布自己条目的新版本")
        if conn.execute(
            "SELECT 1 FROM item_versions WHERE item_type = 'mcp' AND item_id = ?1 AND version = ?2",
            (item_id, version),
        ).fetchone():
            raise ApiError(409, "VERSION_EXISTS", f"版本 {version} 已存在，版本包不可覆盖")

        latest_readme = ""
        if existing is not None:
            row = conn.execute(
                "SELECT readme FROM item_versions WHERE item_type = 'mcp' AND item_id = ?1 "
                "AND revoked_at_ms = 0 "
                "ORDER BY published_at_ms DESC, rowid DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            latest_readme = row["readme"] if row else ""

        final_summary, final_description, final_category, final_tags, final_icon = _shared_meta(
            item_type="mcp",
            existing=existing,
            summary=payload.summary,
            description=payload.description,
            category=payload.category,
            tags=explicit_tags,
            icon=payload.icon,
            package_summary=(payload.description or "").strip()[:MAX_SUMMARY],
            package_description="",
            package_category=None,
            package_tags=[],
        )
        final_readme = explicit_readme if explicit_readme is not None else latest_readme
        now = now_ms()
        if creating:
            _insert_item(
                conn,
                item_type="mcp",
                item_id=item_id,
                name=name,
                summary=final_summary,
                description=final_description,
                category=final_category,
                tags=final_tags,
                icon=final_icon,
                author_uid=user["uid"],
                now=now,
            )
        else:
            _update_item(
                conn,
                item_type="mcp",
                item_id=item_id,
                summary=final_summary,
                description=final_description,
                category=final_category,
                tags=final_tags,
                icon=final_icon,
                now=now,
            )

        try:
            conn.execute(
                "INSERT INTO item_versions (item_type, item_id, version, readme, changelog, "
                "manifest_json, package_path, package_size, package_sha256, published_at_ms, "
                "download_count) VALUES ('mcp', ?1, ?2, ?3, ?4, ?5, NULL, 0, '', ?6, 0)",
                (
                    item_id,
                    version,
                    final_readme,
                    explicit_changelog,
                    json.dumps(config, ensure_ascii=False),
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, "VERSION_EXISTS", f"版本 {version} 已存在") from None

    return get_item_detail("mcp", item_id, user["uid"])


# ── 上传读取 ─────────────────────────────────────────────────


async def _read_package(upload: UploadFile) -> bytes:
    if upload is None:
        raise ApiError(400, "PACKAGE_REQUIRED", "请上传 ZIP 包（字段名 package）")
    buffer = bytearray()
    while True:
        chunk = await upload.read(256 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > settings.max_zip_bytes:
            raise ApiError(
                413,
                "PACKAGE_TOO_LARGE",
                f"压缩包超过 {settings.max_zip_bytes // 1024 // 1024}MB 上限",
            )
    if not buffer:
        raise ApiError(400, "PACKAGE_REQUIRED", "上传的压缩包为空")
    return bytes(buffer)


# ── 读取接口 ─────────────────────────────────────────────────


@router.get("/api/v1/categories")
def api_categories(type: str = Query(..., description="skill / suite / mcp")):
    item_type = _require_item_type(type)
    with tx() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n FROM items WHERE item_type = ?1 GROUP BY category",
            (item_type,),
        ).fetchall()
    counts = {row["category"]: row["n"] for row in rows}
    categories = [{"name": name, "count": counts.get(name, 0)} for name in categories_of(item_type)]
    for name, count in counts.items():
        if name not in categories_of(item_type):
            categories.append({"name": name, "count": count})
    return {"ok": True, "type": item_type, "categories": categories}


@router.get("/api/v1/me/items")
def api_my_items(
    request: Request,
    type: str | None = Query(None),
    page: int = Query(1),
    pageSize: int = Query(20),
    sort: str = Query("latest"),
    user: dict = Depends(require_user),
):
    item_type = _require_item_type(type) if type else None
    return _list_items(
        item_type=item_type,
        q="",
        category="",
        sort=sort,
        featured=None,
        page=page,
        page_size=pageSize,
        requester_uid=user["uid"],
        author_uid=user["uid"],
    )


@router.get("/api/v1/mcp/{item_id}/config")
def api_mcp_config(item_id: str, version: str | None = Query(None)):
    item_id = _validate_item_id(item_id)
    requested = None
    if version and version != "latest":
        requested = normalize_version(version)
        if requested is None:
            raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")
    all_revoked = False
    with tx() as conn:
        item = conn.execute(
            "SELECT name FROM items WHERE item_type = 'mcp' AND item_id = ?1", (item_id,)
        ).fetchone()
        if item is None:
            raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
        if requested:
            row = conn.execute(
                "SELECT version, manifest_json, revoked_at_ms FROM item_versions "
                "WHERE item_type = 'mcp' AND item_id = ?1 AND version = ?2",
                (item_id, requested),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT version, manifest_json, revoked_at_ms FROM item_versions "
                "WHERE item_type = 'mcp' AND item_id = ?1 AND revoked_at_ms = 0 "
                "ORDER BY published_at_ms DESC, rowid DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if row is None:
                all_revoked = conn.execute(
                    "SELECT 1 FROM item_versions WHERE item_type = 'mcp' AND item_id = ?1 LIMIT 1",
                    (item_id,),
                ).fetchone() is not None
    if row is None:
        if all_revoked:
            raise ApiError(410, "ITEM_REVOKED", "该条目的所有版本已被撤销")
        raise ApiError(404, "VERSION_NOT_FOUND", "指定版本不存在")
    if row["revoked_at_ms"]:
        raise ApiError(410, "ITEM_REVOKED", "该版本已被撤销")
    try:
        config = json.loads(row["manifest_json"] or "{}")
    except json.JSONDecodeError:
        config = {}
    return {
        "ok": True,
        "id": item_id,
        "name": item["name"],
        "version": row["version"],
        "mcp": config,
    }


@router.get("/api/v1/revoked")
def api_revoked(
    type: str | None = Query(None),
    page: int = Query(1),
    pageSize: int = Query(20),
):
    item_type = _require_item_type(type) if type else None
    page = max(1, page)
    page_size = min(100, max(1, pageSize))
    conditions = ["v.revoked_at_ms > 0"]
    params: list[Any] = []
    if item_type:
        conditions.append("v.item_type = ?")
        params.append(item_type)
    where_sql = " WHERE " + " AND ".join(conditions)
    with tx() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM item_versions v" + where_sql, params
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT v.item_type, v.item_id, v.version, v.revoked_reason, v.revoked_at_ms, i.name "
            "FROM item_versions v "
            "JOIN items i ON i.item_type = v.item_type AND i.item_id = v.item_id"
            + where_sql
            + " ORDER BY v.revoked_at_ms DESC, v.rowid DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "ok": True,
        "items": [
            {
                "type": row["item_type"],
                "id": row["item_id"],
                "name": row["name"],
                "version": row["version"],
                "reason": row["revoked_reason"],
                "revokedAtMs": row["revoked_at_ms"],
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "hasMore": page * page_size < total,
    }


@router.get("/api/v1/{plural}")
def api_list(
    plural: str,
    request: Request,
    q: str = Query(""),
    category: str = Query(""),
    sort: str = Query("recommended"),
    page: int = Query(1),
    pageSize: int = Query(20),
    featured: str | None = Query(None),
):
    item_type = _require_plural(plural)
    user = bearer_user(request)
    return _list_items(
        item_type=item_type,
        q=q,
        category=category,
        sort=sort,
        featured=featured,
        page=page,
        page_size=pageSize,
        requester_uid=user["uid"] if user else None,
    )


@router.get("/api/v1/{plural}/{item_id}/download")
def api_download(plural: str, item_id: str, version: str | None = Query(None)):
    item_type = _require_plural(plural)
    item_id = _validate_item_id(item_id)
    if item_type == "mcp":
        raise ApiError(404, "NO_PACKAGE", "MCP 插件以配置形式分发，没有可下载的压缩包")
    requested = None
    if version and version != "latest":
        requested = normalize_version(version)
        if requested is None:
            raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")

    all_revoked = False
    with tx() as conn:
        item = conn.execute(
            "SELECT name FROM items WHERE item_type = ?1 AND item_id = ?2",
            (item_type, item_id),
        ).fetchone()
        if item is None:
            raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
        if requested:
            row = conn.execute(
                "SELECT version, package_path, package_sha256, package_size, revoked_at_ms "
                "FROM item_versions "
                "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
                (item_type, item_id, requested),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT version, package_path, package_sha256, package_size, revoked_at_ms "
                "FROM item_versions "
                "WHERE item_type = ?1 AND item_id = ?2 AND revoked_at_ms = 0 "
                "ORDER BY published_at_ms DESC, rowid DESC LIMIT 1",
                (item_type, item_id),
            ).fetchone()
            if row is None:
                all_revoked = conn.execute(
                    "SELECT 1 FROM item_versions WHERE item_type = ?1 AND item_id = ?2 LIMIT 1",
                    (item_type, item_id),
                ).fetchone() is not None
        if row is None:
            if all_revoked:
                raise ApiError(410, "ITEM_REVOKED", "该条目的所有版本已被撤销")
            raise ApiError(404, "VERSION_NOT_FOUND", "指定版本不存在")
        if row["revoked_at_ms"]:
            raise ApiError(410, "ITEM_REVOKED", "该版本已被撤销")
        if not row["package_path"]:
            raise ApiError(404, "NO_PACKAGE", "该版本没有可下载的压缩包")
        full_path = (settings.data_dir / row["package_path"]).resolve()
        if not full_path.is_relative_to(settings.data_dir.resolve()) or not full_path.is_file():
            raise ApiError(410, "PACKAGE_MISSING", "包文件缺失，请联系管理员")
        actual_size = full_path.stat().st_size
        if actual_size != row["package_size"]:
            logger.error(
                "包文件大小与登记不一致：%s（登记 %s，实际 %s）",
                full_path, row["package_size"], actual_size,
            )
            raise ApiError(410, "PACKAGE_CORRUPTED", "包文件大小与登记不一致，请联系管理员")
        conn.execute(
            "UPDATE item_versions SET download_count = download_count + 1 "
            "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
            (item_type, item_id, row["version"]),
        )
        version_value = row["version"]
        digest = row["package_sha256"]

    return FileResponse(
        full_path,
        media_type="application/zip",
        filename=f"{item_id}-{version_value}.zip",
        headers={
            "X-Checksum-Sha256": digest,
            "X-Item-Version": version_value,
            "ETag": f'"{digest}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@router.get("/api/v1/{plural}/{item_id}")
def api_detail(plural: str, item_id: str, request: Request):
    item_type = _require_plural(plural)
    item_id = _validate_item_id(item_id)
    user = bearer_user(request)
    return {"ok": True, "item": get_item_detail(item_type, item_id, user["uid"] if user else None)}


@router.post("/api/v1/{plural}/{item_id}/star")
def api_star(plural: str, item_id: str, user: dict = Depends(require_user)):
    item_type = _require_plural(plural)
    item_id = _validate_item_id(item_id)
    with _publish_lock, tx() as conn:
        item = conn.execute(
            "SELECT 1 FROM items WHERE item_type = ?1 AND item_id = ?2", (item_type, item_id)
        ).fetchone()
        if item is None:
            raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
        existing = conn.execute(
            "SELECT active FROM stars WHERE uid = ?1 AND item_type = ?2 AND item_id = ?3",
            (user["uid"], item_type, item_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO stars (uid, item_type, item_id, active, created_at_ms) "
                "VALUES (?1, ?2, ?3, 1, ?4)",
                (user["uid"], item_type, item_id, now_ms()),
            )
            starred = True
        else:
            starred = not bool(existing["active"])
            conn.execute(
                "UPDATE stars SET active = ?4, created_at_ms = ?5 "
                "WHERE uid = ?1 AND item_type = ?2 AND item_id = ?3",
                (user["uid"], item_type, item_id, 1 if starred else 0, now_ms()),
            )
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM stars "
            "WHERE item_type = ?1 AND item_id = ?2 AND active = 1",
            (item_type, item_id),
        ).fetchone()["n"]
    return {"ok": True, "starred": starred, "stars": count}


# ── 发布接口 ─────────────────────────────────────────────────


@router.post("/api/v1/skills", status_code=201)
async def api_publish_skill(
    package: UploadFile = File(...),
    summary: str | None = Form(None),
    description: str | None = Form(None),
    category: str | None = Form(None),
    tags: str | None = Form(None),
    readme: str | None = Form(None),
    changelog: str | None = Form(None),
    icon: str | None = Form(None),
    version: str | None = Form(None),
    user: dict = Depends(require_user),
):
    data = await _read_package(package)
    item = publish_package(
        user,
        "skill",
        data,
        package.filename or "",
        summary=summary,
        description=description,
        category=category,
        tags=tags,
        readme=readme,
        changelog=changelog,
        icon=icon,
        version=version,
    )
    return {"ok": True, "item": item}


@router.post("/api/v1/skills/{item_id}/versions", status_code=201)
async def api_publish_skill_version(
    item_id: str,
    package: UploadFile = File(...),
    summary: str | None = Form(None),
    description: str | None = Form(None),
    category: str | None = Form(None),
    tags: str | None = Form(None),
    readme: str | None = Form(None),
    changelog: str | None = Form(None),
    icon: str | None = Form(None),
    version: str | None = Form(None),
    user: dict = Depends(require_user),
):
    item_id = _validate_item_id(item_id)
    data = await _read_package(package)
    item = publish_package(
        user,
        "skill",
        data,
        package.filename or "",
        summary=summary,
        description=description,
        category=category,
        tags=tags,
        readme=readme,
        changelog=changelog,
        icon=icon,
        version=version,
        expected_item_id=item_id,
    )
    return {"ok": True, "item": item}


@router.post("/api/v1/suites", status_code=201)
async def api_publish_suite(
    package: UploadFile = File(...),
    summary: str | None = Form(None),
    description: str | None = Form(None),
    category: str | None = Form(None),
    tags: str | None = Form(None),
    readme: str | None = Form(None),
    changelog: str | None = Form(None),
    icon: str | None = Form(None),
    version: str | None = Form(None),
    user: dict = Depends(require_user),
):
    data = await _read_package(package)
    item = publish_package(
        user,
        "suite",
        data,
        package.filename or "",
        summary=summary,
        description=description,
        category=category,
        tags=tags,
        readme=readme,
        changelog=changelog,
        icon=icon,
        version=version,
    )
    return {"ok": True, "item": item}


@router.post("/api/v1/suites/{item_id}/versions", status_code=201)
async def api_publish_suite_version(
    item_id: str,
    package: UploadFile = File(...),
    summary: str | None = Form(None),
    description: str | None = Form(None),
    category: str | None = Form(None),
    tags: str | None = Form(None),
    readme: str | None = Form(None),
    changelog: str | None = Form(None),
    icon: str | None = Form(None),
    version: str | None = Form(None),
    user: dict = Depends(require_user),
):
    item_id = _validate_item_id(item_id)
    data = await _read_package(package)
    item = publish_package(
        user,
        "suite",
        data,
        package.filename or "",
        summary=summary,
        description=description,
        category=category,
        tags=tags,
        readme=readme,
        changelog=changelog,
        icon=icon,
        version=version,
        expected_item_id=item_id,
    )
    return {"ok": True, "item": item}


@router.post("/api/v1/mcp", status_code=201)
def api_publish_mcp(payload: McpCreateIn, user: dict = Depends(require_user)):
    return {"ok": True, "item": publish_mcp(user, payload)}


@router.post("/api/v1/mcp/{item_id}/versions", status_code=201)
def api_publish_mcp_version(
    item_id: str, payload: McpVersionIn, user: dict = Depends(require_user)
):
    item_id = _validate_item_id(item_id)
    return {"ok": True, "item": publish_mcp(user, payload, expected_item_id=item_id)}


# ── 管理员 ───────────────────────────────────────────────────


MAX_REVOKE_REASON = 500


class FeatureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    featured: bool


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


def _set_version_revoked(
    item_type: str,
    item_id: str,
    version: str,
    *,
    revoked: bool,
    reason: str,
    admin_uid: str,
) -> dict:
    with _publish_lock, tx() as conn:
        if conn.execute(
            "SELECT 1 FROM items WHERE item_type = ?1 AND item_id = ?2", (item_type, item_id)
        ).fetchone() is None:
            raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
        row = conn.execute(
            "SELECT revoked_at_ms FROM item_versions "
            "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
            (item_type, item_id, version),
        ).fetchone()
        if row is None:
            raise ApiError(404, "VERSION_NOT_FOUND", "指定版本不存在")
        changed = bool(row["revoked_at_ms"]) != revoked
        if changed and revoked:
            conn.execute(
                "UPDATE item_versions SET revoked_at_ms = ?4, revoked_reason = ?5, revoked_by = ?6 "
                "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
                (item_type, item_id, version, now_ms(), reason, admin_uid),
            )
        elif changed:
            conn.execute(
                "UPDATE item_versions SET revoked_at_ms = 0, revoked_reason = '', revoked_by = '' "
                "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
                (item_type, item_id, version),
            )
        state = conn.execute(
            "SELECT revoked_at_ms, revoked_reason FROM item_versions "
            "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
            (item_type, item_id, version),
        ).fetchone()
    return {
        "changed": changed,
        "revokedAtMs": state["revoked_at_ms"],
        "reason": state["revoked_reason"],
    }


@router.post("/api/v1/admin/items/{item_type}/{item_id}/feature")
def api_feature(
    item_type: str, item_id: str, payload: FeatureIn, user: dict = Depends(require_admin)
):
    item_type = _require_item_type(item_type)
    item_id = _validate_item_id(item_id)
    with _publish_lock, tx() as conn:
        cur = conn.execute(
            "UPDATE items SET featured = ?3, updated_at_ms = ?4 "
            "WHERE item_type = ?1 AND item_id = ?2",
            (item_type, item_id, 1 if payload.featured else 0, now_ms()),
        )
        if cur.rowcount == 0:
            raise ApiError(404, "ITEM_NOT_FOUND", f"未找到条目：{item_id}")
    return {"ok": True, "item": get_item_detail(item_type, item_id, user["uid"])}


@router.post("/api/v1/admin/items/{item_type}/{item_id}/versions/{version}/revoke")
def api_revoke_version(
    item_type: str,
    item_id: str,
    version: str,
    payload: RevokeIn,
    user: dict = Depends(require_admin),
):
    item_type = _require_item_type(item_type)
    item_id = _validate_item_id(item_id)
    normalized = normalize_version(version)
    if normalized is None:
        raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")
    reason = _clean_text(payload.reason, "撤销原因", MAX_REVOKE_REASON, required=True)
    state = _set_version_revoked(
        item_type, item_id, normalized, revoked=True, reason=reason, admin_uid=user["uid"]
    )
    return {
        "ok": True,
        "changed": state["changed"],
        "revokedAtMs": state["revokedAtMs"],
        "item": get_item_detail(item_type, item_id, user["uid"]),
    }


@router.post("/api/v1/admin/items/{item_type}/{item_id}/versions/{version}/unrevoke")
def api_unrevoke_version(
    item_type: str,
    item_id: str,
    version: str,
    user: dict = Depends(require_admin),
):
    item_type = _require_item_type(item_type)
    item_id = _validate_item_id(item_id)
    normalized = normalize_version(version)
    if normalized is None:
        raise ApiError(400, "INVALID_VERSION", "版本号需形如 1.0.0")
    state = _set_version_revoked(
        item_type, item_id, normalized, revoked=False, reason="", admin_uid=user["uid"]
    )
    return {
        "ok": True,
        "changed": state["changed"],
        "item": get_item_detail(item_type, item_id, user["uid"]),
    }
