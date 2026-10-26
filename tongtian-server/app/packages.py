"""技能包（SKILL.md）与专家套件包（plugin.json）的服务端校验与元数据解析。

校验口径对齐桌面端：
- 只接受 ZIP，限制包体积、条目数、单条目与解压总量；
- 拒绝绝对路径、盘符、`..` 穿越、软链接、加密条目与重复路径；
- 技能包的 SKILL.md、套件包的 plugin.json 必须位于根目录或单层目录内；
- 套件标识用与 Rust `suite_id_of` 相同的 slug / CRC-32 规则。
"""

from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath
from typing import Any

from .config import settings
from .errors import ApiError
from .security import normalize_version, suite_id_of

SKILL_ENTRY = "SKILL.md"
SUITE_ENTRY = "plugin.json"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PackageError(ApiError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(status, code, message)


def _fail(code: str, message: str, status: int = 400) -> None:
    raise PackageError(code, message, status)


def _clean_text(value: Any, *, limit: int, field: str, code: str, required: bool = False) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        _fail(code, f"缺少必填字段：{field}")
    if CONTROL_RE.search(text):
        _fail(code, f"{field} 含不可见控制字符")
    if len(text) > limit:
        _fail(code, f"{field} 超过 {limit} 字上限")
    return text


def safe_relative_name(raw: str) -> str | None:
    name = raw.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or ":" in name or "\x00" in name:
        return None
    segments = [seg for seg in name.split("/") if seg not in ("", ".")]
    if not segments or any(seg == ".." for seg in segments):
        return None
    normalized = "/".join(segments)
    if len(normalized) > 512 or CONTROL_RE.search(normalized):
        return None
    return normalized


def _open_zip(data: bytes, code: str) -> tuple[zipfile.ZipFile, list[tuple[zipfile.ZipInfo, str]]]:
    if not data:
        _fail(code, "上传内容为空")
    if len(data) > settings.max_zip_bytes:
        _fail(
            code,
            f"压缩包超过 {settings.max_zip_bytes // 1024 // 1024}MB 上限",
            status=413,
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        _fail(code, "文件不是有效的 ZIP 压缩包")

    infos = zf.infolist()
    if len(infos) > settings.max_entries:
        _fail(code, f"压缩包条目过多（{len(infos)}，上限 {settings.max_entries}）")
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            _fail(code, f"不支持加密条目：{info.filename}")
        normalized = safe_relative_name(info.filename)
        if normalized is None:
            _fail(code, f"压缩包内含不安全路径：{info.filename}")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            _fail(code, f"压缩包内含软链接：{info.filename}")
        if info.is_dir():
            continue
        if info.file_size > settings.max_entry_bytes:
            _fail(code, f"条目 {normalized} 解压后超过单文件上限")
        total += info.file_size
        if total > settings.max_total_uncompressed:
            _fail(code, "压缩包解压后的总大小超过上限")
        if normalized in seen:
            _fail(code, f"压缩包内含重复路径：{normalized}")
        seen.add(normalized)
        entries.append((info, normalized))
    if not entries:
        _fail(code, "压缩包内没有任何文件")
    return zf, entries


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int, code: str) -> bytes:
    if info.file_size > limit:
        _fail(code, f"条目 {info.filename} 超过 {limit // 1024}KB 上限")
    try:
        data = zf.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        _fail(code, f"读取条目 {info.filename} 失败：{exc}")
    if len(data) > limit:
        _fail(code, f"条目 {info.filename} 解压后超过上限")
    return data


def _find_entry_at_depth(
    entries: list[tuple[zipfile.ZipInfo, str]], basename: str, code: str
) -> tuple[zipfile.ZipInfo, str] | None:
    matches = [
        (info, name)
        for info, name in entries
        if len(PurePosixPath(name).parts) <= 2
        and PurePosixPath(name).name.lower() == basename.lower()
    ]
    if len(matches) > 1:
        _fail(code, f"压缩包内含多个 {basename}，无法确定入口文件")
    return matches[0] if matches else None


def _decode_text(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md 的 YAML frontmatter 子集（标量、块标量、行内列表）。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            end = idx
            break
    if end < 0:
        return {}, text

    meta: dict[str, str] = {}
    idx = 1
    while idx < end:
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            idx += 1
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip().lower()
        value = raw_value.strip()
        idx += 1
        if value in ("|", ">", "|-", ">-"):
            block: list[str] = []
            while idx < end and (lines[idx].startswith((" ", "\t")) or not lines[idx].strip()):
                block.append(lines[idx].strip())
                idx += 1
            joined = "\n".join(block).strip() if value.startswith("|") else " ".join(
                part for part in block if part
            )
            meta[key] = joined
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        meta[key] = value
    body = "\n".join(lines[end + 1 :]).lstrip("\r\n")
    return meta, body


def _parse_tag_list(raw: str) -> list[str]:
    value = raw.strip().strip("[]")
    parts = re.split(r"[,，]", value)
    tags: list[str] = []
    for part in parts:
        tag = part.strip().strip("'\"").strip()
        if tag and tag not in tags and len(tag) <= 24:
            tags.append(tag)
        if len(tags) >= 8:
            break
    return tags


def _first_paragraph(body: str) -> str:
    paragraphs: list[str] = []
    for line in body.splitlines():
        if line.strip():
            paragraphs.append(line.strip())
        elif paragraphs:
            break
    text = " ".join(paragraphs).strip().lstrip("#").strip()
    return text[:200]


def _file_list(names: list[str]) -> list[str]:
    return sorted(names)[:200]


def _package_summary(meta: dict[str, str], body: str) -> str:
    desc = (meta.get("description") or "").strip()
    if desc:
        return desc[:200]
    return _first_paragraph(body)


def _parse_declared_list(raw: Any) -> int:
    if not isinstance(raw, list):
        return 0
    count = 0
    for item in raw:
        if isinstance(item, str) and item.strip():
            count += 1
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            count += 1
    return count


def parse_commands(raw: Any) -> list[dict[str, str | None]]:
    out: list[dict[str, str | None]] = []
    seen: set[str] = set()

    def add(name: Any, description: Any = None) -> None:
        text = name.strip() if isinstance(name, str) else ""
        if not text or len(text) > 64 or CONTROL_RE.search(text) or text in seen:
            return
        seen.add(text)
        desc = description.strip() if isinstance(description, str) else ""
        out.append({"name": text, "description": desc[:200] or None})

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("name"), item.get("description"))
    elif isinstance(raw, dict):
        for name, desc in raw.items():
            add(name, desc)
    return out[:100]


def _find_readme(
    zf: zipfile.ZipFile,
    entries: list[tuple[zipfile.ZipInfo, str]],
    code: str,
) -> str:
    matches = [
        (info, name)
        for info, name in entries
        if len(PurePosixPath(name).parts) <= 2
        and PurePosixPath(name).name.lower() == "readme.md"
    ]
    if not matches:
        return ""
    info, _name = sorted(matches, key=lambda pair: len(PurePosixPath(pair[1]).parts))[0]
    data = _read_entry(
        zf, info, limit=settings.max_skill_md_bytes, code=code
    )
    return _decode_text(data)


def inspect_skill_package(data: bytes, fallback_name: str = "") -> dict:
    """解析技能包，返回发布所需的归一化元数据。"""
    code = "INVALID_SKILL_PACKAGE"
    zf, entries = _open_zip(data, code)
    try:
        found = _find_entry_at_depth(entries, SKILL_ENTRY, code)
        if found is None:
            _fail(code, "技能包缺少 SKILL.md（应位于根目录或单层目录内）")
        info, entry_name = found
        raw = _read_entry(zf, info, limit=settings.max_skill_md_bytes, code=code)
    finally:
        zf.close()

    text = _decode_text(raw)
    meta, body = split_frontmatter(text)
    name = (meta.get("name") or "").strip()
    if not name:
        parts = PurePosixPath(entry_name).parts
        name = parts[0] if len(parts) == 2 else fallback_name.strip()
    if not name:
        _fail(code, "技能包未声明 name，且无法从目录名/文件名推断")
    name = _clean_text(name, limit=80, field="技能名称", code=code, required=True)

    declared_version = normalize_version(meta.get("version"))
    category = (meta.get("category") or "").strip() or None
    tags = _parse_tag_list(meta.get("tags", "")) if meta.get("tags") else []
    summary = _package_summary(meta, body)
    readme = body.strip()
    manifest = {
        "id": entry_name,
        "name": name,
        "entry": "SKILL.md",
        "fileCount": len(entries),
        "files": _file_list([name for _, name in entries]),
    }
    return {
        "name": name,
        "summary": summary,
        "readme": readme,
        "declared_version": declared_version,
        "category": category,
        "tags": tags,
        "manifest": manifest,
        "file_count": len(entries),
    }


def inspect_suite_package(data: bytes) -> dict:
    """解析专家套件包（plugin.json 必填），返回发布所需的归一化元数据。"""
    code = "INVALID_SUITE_PACKAGE"
    zf, entries = _open_zip(data, code)
    try:
        found = _find_entry_at_depth(entries, SUITE_ENTRY, code)
        if found is None:
            _fail(code, "套件包缺少 plugin.json（应位于根目录或单层目录内）")
        info, _manifest_name = found
        raw = _read_entry(zf, info, limit=settings.max_manifest_bytes, code=code)
        readme = _find_readme(zf, entries, code)
    finally:
        zf.close()

    try:
        manifest = json.loads(_decode_text(raw))
    except json.JSONDecodeError as exc:
        _fail(code, f"plugin.json 不是合法 JSON：{exc.msg}")
    if not isinstance(manifest, dict):
        _fail(code, "plugin.json 顶层必须是对象")

    name = _clean_text(
        manifest.get("name"), limit=80, field="套件名称", code=code, required=True
    )
    raw_description = manifest.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""
    if len(description) > 5000:
        _fail(code, "套件 description 超过 5000 字上限")
    commands = parse_commands(manifest.get("commands"))
    skill_files = sum(1 for _, entry in entries if PurePosixPath(entry).name == "SKILL.md")
    skill_count = max(_parse_declared_list(manifest.get("skills")), skill_files)
    data_link_count = _parse_declared_list(manifest.get("dataLinks"))
    declared_version = normalize_version(
        manifest.get("version") if isinstance(manifest.get("version"), str) else None
    )
    category = manifest.get("category") if isinstance(manifest.get("category"), str) else None
    raw_tags = manifest.get("tags")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for item in raw_tags:
            if isinstance(item, str):
                tag = item.strip()
                if tag and tag not in tags and len(tag) <= 24:
                    tags.append(tag)
            if len(tags) >= 8:
                break

    suite_id = suite_id_of(name)
    summary = re.sub(r"\s+", " ", description).strip()[:200]
    manifest_out = {
        "id": suite_id,
        "name": name,
        "commands": commands,
        "skillCount": skill_count,
        "dataLinkCount": data_link_count,
        "fileCount": len(entries),
        "files": _file_list([entry for _, entry in entries]),
    }
    return {
        "id": suite_id,
        "name": name,
        "summary": summary,
        "description": description,
        "readme": readme.strip(),
        "declared_version": declared_version,
        "category": (category or "").strip() or None,
        "tags": tags,
        "commands": commands,
        "skill_count": skill_count,
        "data_link_count": data_link_count,
        "manifest": manifest_out,
        "file_count": len(entries),
    }
