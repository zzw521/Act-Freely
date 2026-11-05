# -*- coding: utf-8 -*-
"""产物版本历史与回滚（缺陷 D-26）。

现状差距：`write_text_file` / `insert_text_file` 与办公产物生成都是**覆盖写**，
旧内容当场消失；任务跑歪了、模型把一份好稿子改坏了，用户唯一的办法是重新生成。

本模块把「覆盖之前那份」留在盘上，并提供回滚：

1. **留档点在写入之前**（`record_current`）：留的是**即将被盖掉的那份内容**，
   不是刚写进去的新内容。这样任何一次覆盖都留下一版可回退的事实。
2. **文件系统就是台账**：每个产物一个目录，版本各自一个 blob + 一个 meta，
   没有中心清单。本仓库不是 git 仓库、七个窗口并行跑，中心 `manifest.json`
   会是第二个「谁后写谁覆盖」的现场；列版本靠扫目录，天然并发安全。
3. **文本留 diff，二进制不装**：能按文本读出来的两版之间生成 unified diff；
   读不出来的（含 `\x00`、解不开编码）只留副本并写明「二进制未生成 diff」。
   副本本身照留——用户要的是「别把内容弄丢」，不是「假装能比」。
4. **回滚先留后写**：`restore` 把当前内容记成新版本，再把选定的旧版本原子写回
   （临时文件 + `os.replace`）。回滚因此也是可回滚的，不会变成第二次不可逆覆盖。
5. **配额清理有下限**：每个产物默认留最新 `DEFAULT_KEEP_VERSIONS` 版，
   超出总量预算时按「最旧先删」清，但**最新一版任何情况下都不删**——
   清理策略把最后一份内容删掉，等于用「省盘」的名义重新制造本卡要修的缺陷。

铁律与 `data_paths` 同源：数据根只从 `data_paths.engine_home()` 取，本模块不解析
环境变量；所有写盘都原子；任何一步失败都交出**中文原因**，不静默少一版。
"""

from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import os
import re
from pathlib import Path

import data_paths

VERSIONS_DIRNAME = "artifact-history"

#: 单份内容的留档上限：超过它就不是「产物」而是数据文件，留档会把盘吃光
MAX_TRACK_BYTES = 4 * 1024 * 1024
#: diff 的行数上限：再大的差异生成出来也没人读，不如写明没生成
MAX_DIFF_LINES = 4000
#: 每个产物默认保留的版本数
DEFAULT_KEEP_VERSIONS = 20
#: 整个历史目录的总量预算（超出后按最旧先删）
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024

META_SUFFIX = ".meta.json"
DIFF_SUFFIX = ".diff"
_VERSION_NAME = re.compile(r"^(\d{4})-([0-9A-Za-z._-]+?)(\.[0-9A-Za-z._-]+)*$")


def history_home() -> Path:
    return data_paths.engine_home() / VERSIONS_DIRNAME


def _abs(path: object) -> Path:
    return Path(str(path or "")).expanduser().resolve()


def artifact_key(path: object) -> str:
    """产物的目录名：绝对路径规范化后的摘要。

    Windows 大小写不敏感 ⇒ 先 `normcase`，否则 `A.md` 与 `a.md` 会记成两个产物。
    """
    target = _abs(path)
    return hashlib.sha256(os.path.normcase(str(target)).encode("utf-8", "replace")).hexdigest()[:16]


def artifact_dir(path: object) -> Path:
    return history_home() / artifact_key(path)


def _now() -> float:
    return _dt.datetime.now().timestamp()


def _stamp(now: float) -> str:
    return _dt.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _brief(text: object, limit: int = 200) -> str:
    clean = " ".join(str(text or "").split())
    return clean[:limit]


def _decode(data: bytes) -> tuple[str | None, str]:
    """按文本解一次；解不出来就是二进制，不猜。"""
    if b"\x00" in data[:8192]:
        return None, "含空字节，按二进制处理"
    for codec in ("utf-8", "gbk"):
        try:
            return data.decode(codec), codec
        except (UnicodeDecodeError, LookupError):
            continue
    return None, "无法按文本解码，按二进制处理"


def _meta_file(blob: Path) -> Path:
    return blob.with_name(blob.name + META_SUFFIX)


def _iter_blobs(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    out: list[Path] = []
    for entry in directory.iterdir():
        name = entry.name
        if not entry.is_file() or name.endswith(META_SUFFIX) or name.endswith(DIFF_SUFFIX):
            continue
        if _VERSION_NAME.match(name):
            out.append(entry)
    return sorted(out, key=lambda p: p.name)


# ── 留档 ─────────────────────────────────────────────────────


def _latest_blob(directory: Path) -> Path | None:
    blobs = _iter_blobs(directory)
    return blobs[-1] if blobs else None


def _load_meta(blob: Path) -> dict:
    meta_path = _meta_file(blob)
    if not meta_path.is_file():
        return {}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001  meta 坏了就按没有 meta 处理，不影响副本本身
        return {}


def _atomic_write(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    tmp.write_bytes(payload)
    os.replace(tmp, target)


def record_current(
    path: object, *, task_id: str = "", tool: str = "", reason: str = ""
) -> dict:
    """把**当前**内容留成一版（在覆盖它之前调用）。

    返回 `{"ok", "versionId", "deduped", "note"}`；任何不留档的情况都带中文原因，
    调用方可以直接把这句话播给用户，不需要再猜为什么少一版。
    """
    target = _abs(path)
    result = {"ok": False, "versionId": "", "deduped": False, "note": "", "path": str(target)}
    if not str(target):
        result["note"] = "没有给出文件路径，未留档"
        return result
    try:
        if not target.is_file():
            # 首次创建：盘上没有即将消失的内容，本来就没有可留的版本
            result["ok"] = True
            result["note"] = f"「{target.name}」当前不在盘上，这是首次创建，没有旧内容需要留档"
            return result
        size = target.stat().st_size
        if size > MAX_TRACK_BYTES:
            result["note"] = (
                f"「{target.name}」有 {size:,} 字节，超过单份留档上限 {MAX_TRACK_BYTES:,} 字节；"
                "本次未留档，覆盖照常进行"
            )
            return result
        data = target.read_bytes()
    except OSError as exc:
        result["note"] = f"读取「{target.name}」失败，未留档：{_brief(exc)}"
        return result

    directory = artifact_dir(target)
    digest = _sha(data)
    latest = _latest_blob(directory)
    if latest is not None:
        previous_meta = _load_meta(latest)
        if previous_meta.get("sha256") == digest:
            result["ok"] = True
            result["deduped"] = True
            result["versionId"] = _version_id(latest)
            result["note"] = (
                f"内容与最近一版（{result['versionId']}）逐字节相同，不重复留档"
            )
            return result

    mtime = target.stat().st_mtime
    blobs = _iter_blobs(directory)
    seq = 1
    if blobs:
        matched = _VERSION_NAME.match(blobs[-1].name)
        seq = (int(matched.group(1)) + 1) if matched else 1
    blob_name = f"{seq:04d}-{_stamp(_now())}{target.suffix[:24]}"
    blob = directory / blob_name
    text, codec = _decode(data)
    payload = {
        "versionId": f"{seq:04d}",
        "origin": str(target),
        "name": target.name,
        "sha256": digest,
        "bytes": len(data),
        "mtimeMs": int(mtime * 1000),
        "capturedAtMs": int(_now() * 1000),
        "text": text is not None,
        "codec": codec if text is not None else "",
        "task": str(task_id or ""),
        "tool": str(tool or ""),
        "reason": str(reason or ""),
        "previousVersion": _version_id(latest) if latest is not None else "",
        "blob": blob_name,
        "diff": "",
    }
    if latest is not None and text is not None:
        prev_data = _read_blob(latest)
        prev_text = _decode(prev_data)[0] if prev_data is not None else None
        if prev_text is not None:
            diff_text, diff_note = unified_diff(prev_text, text, label_prev=latest.name,
                                                label_new=blob_name)
            if diff_text:
                diff_path = blob.with_name(blob_name + DIFF_SUFFIX)
                try:
                    _atomic_write(diff_path, diff_text.encode("utf-8"))
                    payload["diff"] = diff_path.name
                except OSError as exc:
                    payload["diffNote"] = f"差异文件写入失败：{_brief(exc)}"
            else:
                # 字节不同但行级相同（多是行尾符换了）：不写空 diff 文件，只把原因留下
                payload["diffNote"] = diff_note or "两版行级内容相同，未生成差异文件"
    try:
        _atomic_write(blob, data)
    except OSError as exc:
        result["note"] = f"留档写入失败，未记版本：{_brief(exc)}"
        return result
    try:
        _atomic_write(_meta_file(blob),
                      json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError as exc:
        # 副本已经在盘上，只是账没记上：如实说，别让调用方以为整版丢了
        result["note"] = f"版本 {payload['versionId']} 的副本已留档，但元数据写入失败：{_brief(exc)}"
        result["ok"] = True
        result["versionId"] = payload["versionId"]
        return result
    result["ok"] = True
    result["versionId"] = payload["versionId"]
    result["meta"] = payload
    result["note"] = (
        f"已把覆盖前的「{target.name}」留成版本 {payload['versionId']}"
        f"（{len(data):,} 字节，{'文本' if text is not None else '二进制'}）"
    )
    return result


def _read_blob(blob: Path) -> bytes | None:
    try:
        return blob.read_bytes()
    except OSError:
        return None


def _version_id(blob: Path) -> str:
    matched = _VERSION_NAME.match(blob.name)
    return matched.group(1) if matched else blob.name


# ── 列表 / 读取 / 差异 ──────────────────────────────────────


def list_versions(path: object) -> tuple[list[dict], list[str]]:
    """按版本从新到旧列出这个产物的历史；目录不存在就是空列表（不是错误）。"""
    target = _abs(path)
    directory = artifact_dir(target)
    notes: list[str] = []
    rows: list[dict] = []
    blobs = _iter_blobs(directory)
    for blob in reversed(blobs):
        meta = _load_meta(blob)
        if not meta:
            notes.append(
                f"版本 {blob.name} 的元数据读不到（可能上次留档中途断电）；"
                "副本仍在，可照常回滚，只是没有大小与来源可显示"
            )
            meta = {"versionId": _version_id(blob), "blob": blob.name, "origin": str(target)}
        try:
            meta["bytes"] = int(meta.get("bytes") or blob.stat().st_size)
        except OSError:
            meta["bytes"] = int(meta.get("bytes") or 0)
        meta.setdefault("versionId", _version_id(blob))
        meta["fileExists"] = target.is_file()
        rows.append(meta)
    return rows, notes


def find_version(path: object, version_id: str) -> tuple[Path | None, dict]:
    directory = artifact_dir(path)
    wanted = str(version_id or "").strip()
    if not wanted:
        return None, {}
    for blob in _iter_blobs(directory):
        if _version_id(blob) == wanted or blob.name == wanted:
            return blob, _load_meta(blob)
    return None, {}


def version_text(path: object, version_id: str) -> tuple[str, str]:
    """读出某一版正文；返回 (正文, 原因)。非文本不猜内容。"""
    blob, meta = find_version(path, version_id)
    if blob is None:
        return "", "找不到这一版，可能已被配额清理或从未留档"
    data = _read_blob(blob)
    if data is None:
        return "", f"版本副本读不到：{blob.name}"
    if not meta.get("text", True):
        return "", "这一版是二进制文件，没有可比对的文本正文"
    text, _ = _decode(data)
    if text is None:
        return "", "这一版无法按文本解码，未强行显示"
    return text, ""


def unified_diff(old: str, new: str, *, label_prev: str = "旧版", label_new: str = "新版",
                 context: int = 3) -> tuple[str | None, str]:
    """两版文本的 unified diff；过长就不生成并说明，不返回半截冒充完整差异。"""
    old_lines = str(old or "").splitlines()
    new_lines = str(new or "").splitlines()
    changed = sum(1 for a, b in zip(old_lines, new_lines) if a != b) + abs(len(old_lines) - len(new_lines))
    if changed > MAX_DIFF_LINES:
        return None, (
            f"差异行数（约 {changed} 行）超过 {MAX_DIFF_LINES} 上限，未生成逐行差异；"
            "两版正文都完整留档，可整版回滚"
        )
    out = list(difflib.unified_diff(old_lines, new_lines, fromfile=label_prev,
                                    tofile=label_new, lineterm="", n=context))
    if not out:
        return "", "两版文本没有差异"
    return "\n".join(out), ""


def diff_version(path: object, version_id: str) -> dict:
    """某一版相对**它前一版**的差异；前一版已被清理时如实说明。"""
    blob, meta = find_version(path, version_id)
    result = {"ok": False, "versionId": str(version_id or ""), "diff": "", "note": "", "truncated": False}
    if blob is None:
        result["note"] = "找不到这一版，无法给出差异"
        return result
    result["ok"] = True
    if meta.get("diff"):
        diff_blob = blob.with_name(str(meta["diff"]))
        text = None
        if diff_blob.is_file():
            raw = _read_blob(diff_blob)
            text = raw.decode("utf-8", "replace") if raw is not None else None
        if text is not None:
            result["diff"] = text
            result["note"] = "这一版相对上一版的差异（留档时已生成）"
            return result
        result["note"] = "差异文件不在了（可能被配额清理），改为现算"
    prev_id = str(meta.get("previousVersion") or "")
    if not prev_id:
        result["note"] = "这是该产物留档的第一版，前面没有可比的一版"
        result["isFirst"] = True
        return result
    prev_blob, _prev_meta = find_version(path, prev_id)
    if prev_blob is None:
        result["note"] = f"前一版 {prev_id} 已被清理，无法现算差异；这一版本身可以整版回滚"
        return result
    old_text, old_note = version_text(path, prev_id)
    new_text, new_note = version_text(path, str(version_id))
    if not old_text and old_note:
        result["note"] = f"前一版取不出正文：{old_note}"
        return result
    if not new_text and new_note:
        result["note"] = f"这一版取不出正文：{new_note}"
        return result
    diff_text, note = unified_diff(old_text, new_text, label_prev=f"{prev_id}（前一版）",
                                   label_new=f"{version_id}（这一版）")
    if diff_text is None:
        result["note"] = note
        result["truncated"] = True
        return result
    result["diff"] = diff_text
    result["note"] = note or "这一版相对上一版的差异（现算）"
    return result


# ── 回滚 ────────────────────────────────────────────────────


def restore(path: object, version_id: str, *, task_id: str = "") -> dict:
    """把某一版写回原路径。

    三条硬规矩：
    - 只写回它**留档时记录的那个路径**（绝不拿 A 的历史去盖 B）；
    - 写回前先把当前内容留成一版 ⇒ 回滚本身可回滚；
    - 原子替换（临时文件 + `os.replace`），不留半截文件。
    """
    result = {"ok": False, "restored": "", "note": "", "capturedVersion": ""}
    blob, meta = find_version(path, version_id)
    if blob is None:
        result["note"] = f"找不到版本「{version_id}」，没有执行回滚"
        return result
    origin = str(meta.get("origin") or "").strip()
    target = _abs(path)
    if not origin or os.path.normcase(str(_abs(origin))) != os.path.normcase(str(target)):
        result["note"] = (
            f"这一版记录的原路径是「{origin or '（未记录）'}」，与要回滚的「{target}」不是同一个文件；"
            "拒绝把别的产物的历史写到这里"
        )
        return result
    data = _read_blob(blob)
    if data is None:
        result["note"] = f"版本副本 {blob.name} 读不到，没有执行回滚"
        return result
    if target.is_file():
        captured = record_current(target, task_id=task_id, tool="artifact_history_runtime.restore",
                                  reason=f"回滚到版本 {version_id} 之前自动留档")
        result["capturedVersion"] = str(captured.get("versionId") or "")
        if not captured.get("ok"):
            # 当前内容留不住就不能覆盖：宁可回滚失败，也不做第二次不可逆写入
            result["note"] = f"回滚已取消：{captured.get('note') or '当前内容无法留档'}"
            return result
    try:
        _atomic_write(target, data)
    except OSError as exc:
        result["note"] = f"回滚写入失败：{_brief(exc)}"
        return result
    result["ok"] = True
    result["restored"] = str(version_id)
    result["note"] = (
        f"已把「{target.name}」回滚到版本 {version_id}（{len(data):,} 字节）"
        + (f"；回滚前的内容留成版本 {result['capturedVersion']}" if result["capturedVersion"]
           else "；该路径此前没有文件，无需留档")
    )
    return result


# ── 配额清理 ────────────────────────────────────────────────


def _artifact_dirs() -> list[Path]:
    root = history_home()
    if not root.is_dir():
        return []
    return [entry for entry in sorted(root.iterdir()) if entry.is_dir()]


def prune(keep: int = DEFAULT_KEEP_VERSIONS, max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict:
    """按「每个产物留最新 N 版 + 总量预算」清理旧版本。

    两刀分先后：先按版本数清掉各产物第 N 版之前的，再看总量；仍超预算就继续从最旧删，
    但**每个产物最新那一版永远不动**——清理把最后一份内容删掉，
    等于用「省盘」的名义重新制造本卡要修的缺陷。
    """
    keep = max(1, int(keep or 1))
    directories = [d for d in _artifact_dirs() if _iter_blobs(d)]
    total = sum(_blob_bytes(b) for d in directories for b in _iter_blobs(d))
    removed = 0
    freed = 0

    def drop(blob: Path) -> int:
        nonlocal removed, freed
        size = _blob_bytes(blob)
        if _drop_version(blob):
            removed += 1
            freed += size
            return size
        return 0

    # 第一刀：超出「每个产物 keep 版」的旧版本
    for directory in directories:
        blobs = _iter_blobs(directory)  # 旧 → 新
        for blob in blobs[:max(0, len(blobs) - keep)]:
            total -= drop(blob)

    # 第二刀：总量仍超预算 ⇒ 从最旧继续删，各产物最新一版除外
    if total > max_total_bytes:
        for directory in directories:
            if total <= max_total_bytes:
                break
            # `[:-1]` 就是「除了最新一版」——这一版永远留着，见上面口径
            for blob in _iter_blobs(directory)[:-1]:
                if total <= max_total_bytes:
                    break
                total -= drop(blob)

    notes: list[str] = []
    if total > max_total_bytes:
        notes.append(
            f"已删到「每个产物只剩最新一版」，仍有 {total:,} 字节、超过 {max_total_bytes:,} 字节预算："
            "剩下的都是各产物唯一可回滚的那一版，不再删"
        )
    return {
        "removed": removed,
        "freedBytes": freed,
        "keepPerArtifact": keep,
        "maxTotalBytes": max_total_bytes,
        "totalBytesAfter": total,
        "notes": notes,
        "note": (
            f"清理掉 {removed} 个旧版本（释放 {freed:,} 字节），每个产物保留最新 {keep} 版；"
            "各产物最新那一版永不删除"
        ),
    }


def _blob_bytes(blob: Path) -> int:
    try:
        return blob.stat().st_size
    except OSError:
        return 0


def _drop_version(blob: Path) -> bool:
    """删一版：副本 + 元数据 + 差异文件一起走，不留孤儿。"""
    gone = False
    for partner in (blob, _meta_file(blob), blob.with_name(blob.name + DIFF_SUFFIX)):
        try:
            if partner.is_file():
                partner.unlink()
                gone = True
        except OSError:
            continue
    return gone


# ── 一次给齐的视图（端点与界面直接用这一份）─────────────────


def history_view(path: object, *, versions_limit: int = 50) -> dict:
    target = _abs(path)
    rows, notes = list_versions(target)
    total_bytes = sum(int(r.get("bytes") or 0) for r in rows)
    return {
        "ok": True,
        "path": str(target),
        "name": target.name,
        "exists": target.is_file(),
        "tracked": bool(rows),
        "historyDir": str(artifact_dir(target)),
        "versions": rows[:max(1, int(versions_limit or 50))],
        "versionCount": len(rows),
        "totalBytes": total_bytes,
        "truncated": len(rows) > versions_limit,
        "notes": notes,
    }


def stats() -> dict:
    """历史目录的体量：产物数、版本数、占用字节，供界面显示与配额决策。"""
    artifacts = 0
    versions = 0
    total = 0
    for directory in _artifact_dirs():
        blobs = _iter_blobs(directory)
        if not blobs:
            continue
        artifacts += 1
        versions += len(blobs)
        total += sum(_blob_bytes(b) for b in blobs)
    return {
        "home": str(history_home()),
        "artifacts": artifacts,
        "versions": versions,
        "totalBytes": total,
        "maxTotalBytes": DEFAULT_MAX_TOTAL_BYTES,
        "overBudget": total > DEFAULT_MAX_TOTAL_BYTES,
    }
