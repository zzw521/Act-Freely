# -*- coding: utf-8 -*-
"""写暂存与 diff 审批（缺陷 D-03.5，引擎侧半边）。

差距（台账 D-03「未完成」第三条、D-45 之后的最后一块）：模型说「我把这个文件改成这样」时，
**盘上已经改了**。文件访问档位（每次询问 / 自动审批 / 完全访问）解决的是「让不让写」，
解决不了「写完之后人看不到改了什么就生效了」——逐工具审批弹的是一个「是 / 否」的框，
用户在没有 diff 的情况下点「是」，等于签一张空白支票。

本模块把文本写入拆成两步：

1. **先落暂存区**（`stage`）：新内容写进数据根下的 `write-staging/`，目标文件**一个字也不动**，
   同时对着盘上那一份算出 unified diff。暂存目录**不在工作目录里**——工作目录是模型能写的地方，
   把「待审批的内容」放在那儿等于让被审批者改自己的审批对象。
2. **批准才合并**（`apply`）：合并前重新算一次目标文件的指纹。目标在暂存之后又被改过
   （另一条通道直接写盘、用户在编辑器里改了、文件被删了）就**拒绝合并并标成过期**，
   绝不拿一份对不上事实的 diff 去盖现在的内容——这一步是整个功能的意义所在，
   少了它，「审批」只是审批了一份已经不存在的改动。

口径与相邻模块一致：

- 数据根只从 `data_paths.write_staging_dir()` 取，本模块不解析环境变量；
- 所有写盘原子（临时文件 + `os.replace`），坏 JSON 改名让路并如实报 `loadErrors`，不静默当成没有；
- 每一次决定（合并 / 拒绝 / 过期）追加一行 `decisions.jsonl`，**只记指纹与字节数，不记内容本体**；
- 上限一律如实拒绝，不「退化成直接写」：暂存开着的时候把内容原样落盘，
  就是当着用户的面把承诺撤回。
- 未开启（缺省 `off`）时 `stage` 返回 `staged=False`，调用方按改造前的路径原样写盘，
  回执逐字不变；直接调用 `build_toolkit` 的既有自测因此完全不受影响。

只管文本写：`generate_xlsx` / `generate_docx` / `generate_pptx` 这类产物生成件与
`delete_text_file` 仍然直接落盘（删除件的兜底是回收目录，见 `server.py::_to_trash`），
本模块不冒充已经把它们一起纳管。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import data_paths

MODES = ("off", "staged")
DEFAULT_MODE = "off"
MODE_LABELS = {
    "off": "直接写盘",
    "staged": "先落暂存、看过 diff 再合并",
}

#: 单份暂存内容的上限：与产物留档同一档（超过它的是数据文件，不是「一处改动」）
MAX_CONTENT_BYTES = 4 * 1024 * 1024
#: diff 行数上限：再大的差异生成出来也没人读，不如写明没生成
MAX_DIFF_LINES = 4000
#: 同一任务待审阅条数上限：满了就拒新暂存，让用户先处理存量
MAX_PENDING_PER_TASK = 200
#: 列表缺省 / 上限条数
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500
#: 扫描变更目录时的兜底（目录结构损坏、被外部塞了别的东西时不至于卡死请求）
MAX_SCAN_DIRS = 5000

META_NAME = "meta.json"
CONTENT_NAME = "new.txt"
DECISIONS_FILENAME = "decisions.jsonl"
_CORRUPT_SUFFIX = ".corrupt-"
_TMP_PREFIX = ".tt-staging-"

_lock = threading.RLock()

STATUS_PENDING = "pending"
STATUS_STALE = "stale"

#: 会被这一档接管的两件文本写入工具（**唯一来源**：`server.build_toolkit` 与
#: `tool_approval.prompts_for` 都从这里读，避免「暂存接管了 A、审批层以为接管了 B」）
STAGED_TOOLS = ("write_text_file", "insert_text_file")


# ── 配置（运行时开关的载体是桌面推给引擎的 runtime 载荷）────────────

def normalize_mode(raw: object) -> str:
    """把 runtime 里的档位归一成 `off` / `staged`；认不出来按缺省关闭。"""
    text = str(raw or "").strip().lower()
    if text in ("staged", "stage", "review", "审批", "暂存"):
        return "staged"
    if text in ("off", "", "direct", "none"):
        return "off"
    return "off"


def _raw_setting(runtime: dict | None):
    """取 runtime 里的写暂存配置（允许 `writeStaging` / `write_staging`、裸字符串或对象）。"""
    if not isinstance(runtime, dict):
        return None
    block = runtime.get("writeStaging")
    if block is None:
        block = runtime.get("write_staging")
    if isinstance(block, dict):
        return block.get("mode", "")
    return block


def mode_error(runtime: dict | None) -> str:
    """只配了非法值时给一句中文原因；合法值与缺省都返回空串。"""
    text = str(_raw_setting(runtime) or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if normalize_mode(text) == "off" and lowered not in ("off", "direct", "none", "关闭", "false"):
        return f"写暂存档位「{_brief(text, 40)}」不认识，本轮按「直接写盘」执行"
    return ""


def staging_enabled(runtime: dict | None) -> bool:
    """这一轮是否开启写暂存（装配现场两处判定共用：`build_agent` 与逐工具审批网关）。

    留一个函数而不是两边各写一遍 `from_runtime(...).staged`：接管范围一旦分叉，
    就会出现「暂存没开、审批却少弹一次」——那是少一道审批，不是少一次打扰。
    """
    return WriteStaging.from_runtime(runtime).staged


class WriteStaging:
    """一次任务的写暂存策略（只读判定，不产生副作用）。"""

    __slots__ = ("mode", "error")

    def __init__(self, mode: str = DEFAULT_MODE, error: str = "") -> None:
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.error = error

    @classmethod
    def from_runtime(cls, runtime: dict | None) -> "WriteStaging":
        return cls(normalize_mode(_raw_setting(runtime)), mode_error(runtime))

    @property
    def staged(self) -> bool:
        return self.mode == "staged"

    def note(self) -> str:
        """给用户看得见的一句说明（含非法档位那次）。"""
        lines = [f"写暂存：{MODE_LABELS.get(self.mode, self.mode)}"]
        if self.error:
            lines.append(self.error)
        if self.mode == "staged":
            lines.append(
                "本轮文本写入先进暂存区，界面批准 diff 后才合并进目标文件；"
                "未批准的改动不会落到盘上。"
            )
        return "；".join(lines)

    def system_hint(self) -> str:
        """给模型的提示词：不开启就一个字也不加，开启后必须防止它把「待审阅」说成「已完成」。"""
        if self.mode != "staged":
            return ""
        return (
            "\n\n【写暂存已开启】本轮 `write_text_file` / `insert_text_file` **不直接写盘**："
            "内容进入暂存区并生成 diff，等用户在界面批准后才合并到目标文件。"
            "工具回执里给的是变更号（chg-…）而不是「已写入」，"
            "所以不要把文件说成已经更新，要说「已提交待审阅（变更号 …）」；"
            "也不要为了「确认写入成功」去重复调用同一文件的写入——重复调用只会覆盖同一条待审阅。"
            "需要看现在盘上的内容请照旧用 `view_text_file`。"
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _brief(text: object, limit: int = 200) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def staging_home() -> Path:
    return data_paths.write_staging_dir()


def decisions_file() -> Path:
    return staging_home() / DECISIONS_FILENAME


def _task_slug(task_id: str) -> str:
    """任务 id 变成目录名：只留安全字符，别的换成 `-`，空值归到一个明确命名的缺省桶。"""
    raw = str(task_id or "").strip()
    if not raw:
        return "_no-task"
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "-", raw)[:64].strip("-.")
    return cleaned or "_no-task"


def _read_bytes(target: Path) -> tuple[bytes, str]:
    """返回 (字节, 错误原因)；不存在时返回 (b"", "")，由调用方按「新建」处理。"""
    try:
        if not target.exists():
            return b"", ""
        if target.is_dir():
            return b"", "目标是目录，不能当文件写"
        return target.read_bytes(), ""
    except OSError as exc:
        return b"", f"读目标文件失败：{exc}"


def _as_text(raw: bytes) -> tuple[str | None, str]:
    """能按文本解就返回 (文本, "")，否则 (None, 原因)——不假装能比。"""
    if b"\x00" in raw[:8192]:
        return None, "目标是二进制内容（含 NUL），无法生成行级 diff"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "目标不是 UTF-8 文本，无法生成行级 diff"


def _unified_diff(old_text: str, new_text: str, label_old: str, label_new: str) -> dict:
    a = old_text.splitlines()
    b = new_text.splitlines()
    lines = list(difflib.unified_diff(a, b, fromfile=label_old, tofile=label_new, lineterm=""))
    truncated = len(lines) > MAX_DIFF_LINES
    kept = lines[:MAX_DIFF_LINES]
    added = sum(1 for x in kept if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in kept if x.startswith("-") and not x.startswith("---"))
    note = ""
    if truncated:
        note = f"差异超过 {MAX_DIFF_LINES} 行，diff 已截断，请对照暂存文件全文审阅"
    return {
        "text": "\n".join(kept),
        "diffLines": len(kept),
        "diffAdded": added,
        "diffRemoved": removed,
        "diffTruncated": truncated,
        "diffNote": note,
    }


def _meta_path(change_dir: Path) -> Path:
    return change_dir / META_NAME


def _write_json(path: Path, payload: dict) -> None:
    """原子写 JSON：先写 tmp 再 replace，读侧永远只会看到完整的一份或没有。"""
    tmp = path.with_name(f"{path.name}{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _walk_changes(task_filter: str = "") -> list[Path]:
    """列出变更目录（新→旧由调用方按 meta 排）。目录不存在时给空表，不抛。"""
    home = staging_home()
    if not home.is_dir():
        return []
    out: list[Path] = []
    scanned = 0
    task_dirs = [home / task_filter] if task_filter else sorted(p for p in home.iterdir() if p.is_dir())
    for task_dir in task_dirs:
        try:
            children = sorted(p for p in task_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for change_dir in children:
            scanned += 1
            if scanned > MAX_SCAN_DIRS:
                return out
            out.append(change_dir)
    return out


def _find_change(change_id: str) -> tuple[Path | None, str]:
    cid = str(change_id or "").strip()
    if not cid:
        return None, "没有给出变更号"
    for change_dir in _walk_changes():
        if change_dir.name == cid:
            return change_dir, ""
    return None, f"找不到变更号「{_brief(cid, 60)}」的待审阅记录（可能已经合并或被拒绝）"


def _load_meta(change_dir: Path, errors: list[str]) -> dict | None:
    path = _meta_path(change_dir)
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001  坏文件改名让路，别把一次读失败变成整张列表空白
        aside = path.with_name(f"{path.name}{_CORRUPT_SUFFIX}{_stamp()}")
        try:
            path.rename(aside)
            errors.append(
                f"暂存记录读不出（不是合法 JSON），已改名让路到 {aside.name}：{_brief(exc)}"
            )
        except OSError:
            errors.append(f"暂存记录读不出且无法改名：{_brief(exc)}")
        return None
    if not isinstance(meta, dict) or not str(meta.get("changeId") or ""):
        errors.append(f"暂存记录缺必要字段（changeId），已忽略：{change_dir.name}")
        return None
    return meta


def _refresh_state(meta: dict) -> dict:
    """把盘上事实并进来：过期与合并资格每次读都重算，不信任 meta 里写过的 status。"""
    target = Path(str(meta.get("targetPath") or ""))
    item = dict(meta)
    item["targetChanged"] = False
    item["targetChangeNote"] = ""
    if str(meta.get("status")) != STATUS_PENDING:
        item["approvable"] = False
        return item
    if not str(meta.get("targetPath") or ""):
        item["approvable"] = False
        item["targetChangeNote"] = "记录里没有目标路径"
        return item
    raw, read_error = _read_bytes(target)
    if read_error:
        item["approvable"] = False
        item["targetChangeNote"] = read_error
        return item
    base_exists = bool(meta.get("baseExists"))
    base_sha = str(meta.get("baseSha") or "")
    if base_exists and not raw:
        item["targetChanged"] = True
        item["targetChangeNote"] = "目标文件在提交暂存后被删除或已不存在"
    elif not base_exists and raw:
        item["targetChanged"] = True
        item["targetChangeNote"] = "目标文件在提交暂存后被别人创建了"
    elif base_exists and raw and _sha(raw) != base_sha:
        item["targetChanged"] = True
        item["targetChangeNote"] = "目标文件在提交暂存后被改过"
    if not target.parent.is_dir():
        item["targetChangeNote"] = (
            (item["targetChangeNote"] + "；" if item["targetChangeNote"] else "")
            + "目标目录当前不存在（合并时会自动创建）"
        )
    item["approvable"] = not item["targetChanged"]
    return item


def _append_decision(entry: dict) -> dict:
    """追加一行决定日志：只带指纹与字节数，绝不含内容本体。"""
    safe = {k: v for k, v in entry.items() if v is not None}
    safe["loggedAtMs"] = _now_ms()
    try:
        target = decisions_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
        safe["persisted"] = True
    except Exception as exc:  # noqa: BLE001  日志写失败要说出来，不能假装这条决定没发生过
        safe["persisted"] = False
        safe["persistError"] = _brief(exc)
    return safe


def _base_drifted(meta: dict, current_raw: bytes) -> bool:
    """记录里的基线与此刻盘上是否已经不一致（不一致就不能再当基线用）。

    「该有内容却没了」也算不一致：基线记的是那份旧内容，文件被删掉后拿它算 diff
    会把「删除」混进用户的批准对象里。
    """
    if bool(meta.get("baseExists")):
        return (not current_raw) or _sha(current_raw) != str(meta.get("baseSha") or "")
    return bool(current_raw)


def _recorded_work_dir(meta: dict) -> Path | None:
    """取记录里当初绑定的工作目录；没有或读不出来就返回 None（交给硬拒清单判）。"""
    raw = str(meta.get("workDir") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw)
    except (OSError, ValueError):  # 路径里带空字节等非法字符时 Path 会抛 ValueError
        return None


def _hard_deny(target: Path) -> str:
    """越出绑定工作目录时的硬拒清单判定；判不了就不写（返回原因即拒绝）。"""
    try:
        import sandbox_runtime as sr  # noqa: PLC0415

        return sr.hard_deny_hit(target)
    except Exception as exc:  # noqa: BLE001
        return f"安全判定模块读不出来（{type(exc).__name__}：{_brief(exc)}），不合并"


# ── 第一步：暂存 ────────────────────────────────────────────────

def stage(
    new_text: str,
    *,
    target: object,
    task_id: str = "",
    tool: str = "write_text_file",
    work_dir: Path | None = None,
) -> dict:
    """把「即将写进去的内容」收进暂存区；**不碰目标文件**。

    返回 `{"ok", "staged", ...}`：`staged=False` 表示本轮没开暂存，调用方照旧直接写盘；
    `ok=False` 表示暂存没成功**且内容没有落盘**，调用方必须把 `reason` 原样交给用户。
    """
    result = {"ok": False, "staged": True, "changeId": "", "reason": "", "note": ""}
    raw_target = str(target or "").strip()
    if not raw_target:
        # 必须在 Path 构造之前判：Path("") 在 Windows 上会变成 "."，
        # 「没给目标」就悄悄变成了「暂存一份要写进当前目录的东西」——那正是这条底线最难的漏法
        result["reason"] = "没有给出目标路径，未暂存"
        return result
    target_path = Path(raw_target).expanduser()
    if not str(target_path):
        result["reason"] = "没有给出目标路径，未暂存"
        return result
    try:
        target_path = target_path.resolve()
    except OSError as exc:
        result["reason"] = f"目标路径解析失败：{exc}"
        return result

    content = new_text if isinstance(new_text, str) else str(new_text or "")
    raw_new = content.encode("utf-8")
    if len(raw_new) > MAX_CONTENT_BYTES:
        result["reason"] = (
            f"新内容有 {len(raw_new):,} 字节，超过暂存上限 {MAX_CONTENT_BYTES:,} 字节；"
            "未暂存也未写盘——这么大的文本请直接生成为文件产物"
        )
        return result

    slug = _task_slug(task_id)
    with _lock:
        task_dir = staging_home() / slug
        pending_dirs = [p for p in _walk_changes(slug) if (p / META_NAME).is_file()] \
            if task_dir.is_dir() else []
        live = 0
        for change_dir in pending_dirs:
            try:
                meta = json.loads(_meta_path(change_dir).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001  坏记录走 _load_meta 的改名分支，这里只统计
                continue
            if isinstance(meta, dict) and str(meta.get("status")) == STATUS_PENDING:
                live += 1
        if live >= MAX_PENDING_PER_TASK:
            result["reason"] = (
                f"本任务已有 {live} 条待审阅改动（上限 {MAX_PENDING_PER_TASK}）；"
                "未暂存也未写盘——请先在界面处理存量"
            )
            return result

        # 同一个目标只留一条记录：第二次写覆盖第一次，而不是叠成两条互相矛盾的改动。
        # 已过期的那条也一起被替换——它永远合并不了，留着只会让用户对着两条同样的目标猜哪条算数。
        existing_dir: Path | None = None
        existing_meta: dict | None = None
        for change_dir in pending_dirs:
            try:
                meta = json.loads(_meta_path(change_dir).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(meta, dict):
                continue
            if str(meta.get("status")) not in (STATUS_PENDING, STATUS_STALE):
                continue
            if str(meta.get("targetPath")) == str(target_path):
                existing_dir, existing_meta = change_dir, meta
                break

        base_raw, read_error = _read_bytes(target_path)
        supersede = existing_dir is not None and existing_meta is not None
        if supersede and read_error:
            # 目标在两次暂存之间坏了（被删成目录 / 读不动）：老记录的 base 已失去意义，
            # 不能继续拿它当基准，改用当前事实重建一条。
            supersede = False
        if read_error and not supersede:
            result["reason"] = f"目标当前不可写：{read_error}；未暂存也未写盘"
            return result

        change_id = existing_meta.get("changeId") if supersede and existing_meta else \
            f"chg-{_now_ms()}-{os.urandom(3).hex()}"
        change_dir = existing_dir if supersede else staging_home() / slug / change_id
        change_dir.mkdir(parents=True, exist_ok=True)

        reuse_base = bool(
            supersede
            and existing_meta
            and str(existing_meta.get("status")) == STATUS_PENDING
            and not _base_drifted(existing_meta, base_raw)
        )
        if reuse_base and existing_meta:
            base_sha = str(existing_meta.get("baseSha") or "")
            base_exists = bool(existing_meta.get("baseExists"))
            base_size = int(existing_meta.get("baseSize") or 0)
            base_mtime_ms = int(existing_meta.get("baseMtimeMs") or 0)
            created_at_ms = int(existing_meta.get("createdAtMs") or _now_ms())
            superseded = int(existing_meta.get("supersededCount") or 0) + 1
        else:
            # 新记录，或替换一条已过期的：基线一律取**此刻**的盘上内容——
            # 用户批准的是「现在这份 → 新内容」，不是上一次那份对不上的差异
            base_sha = _sha(base_raw) if base_raw else ""
            base_exists = bool(base_raw)
            base_size = len(base_raw)
            try:
                base_mtime_ms = int(target_path.stat().st_mtime * 1000) if base_raw else 0
            except OSError:
                base_mtime_ms = 0
            created_at_ms = int((existing_meta or {}).get("createdAtMs") or _now_ms())
            superseded = int((existing_meta or {}).get("supersededCount") or 0) + (1 if supersede else 0)

        old_text = ""
        diff_available = True
        diff_note = ""
        if base_exists:
            decoded_old, why_old = _as_text(base_raw)
            if decoded_old is None:
                diff_available = False
                diff_note = why_old
            else:
                old_text = decoded_old
        decoded_new, why_new = _as_text(raw_new)
        if decoded_new is None:
            diff_available = False
            diff_note = diff_note or why_new

        display = _display_path(target_path, work_dir)
        diff = (
            _unified_diff(old_text, content, display, f"{display}（待审阅）")
            if diff_available
            else {"text": "", "diffLines": 0, "diffAdded": 0, "diffRemoved": 0,
                  "diffTruncated": False, "diffNote": ""}
        )

        meta = {
            "changeId": change_id,
            "taskId": str(task_id or ""),
            "taskSlug": slug,
            "targetPath": str(target_path),
            "displayPath": display,
            "workDir": str(Path(work_dir).resolve()) if work_dir is not None else "",
            "tool": str(tool or ""),
            "status": STATUS_PENDING,
            "createdAtMs": created_at_ms,
            "updatedAtMs": _now_ms(),
            "supersededCount": superseded,
            "baseExists": base_exists,
            "baseSha": base_sha,
            "baseSize": base_size,
            "baseMtimeMs": base_mtime_ms,
            "newSize": len(raw_new),
            "newSha": _sha(raw_new),
            "diffAvailable": diff_available,
            "diffNote": diff_note or diff["diffNote"],
            "diffLines": diff["diffLines"],
            "diffAdded": diff["diffAdded"],
            "diffRemoved": diff["diffRemoved"],
            "diffTruncated": diff["diffTruncated"],
        }
        try:
            (change_dir / CONTENT_NAME).write_bytes(raw_new)
            _write_json(_meta_path(change_dir), meta)
        except OSError as exc:
            # 暂存区写不下去就什么都没成立：清掉半成品，把原因交出去，绝不回退成直接写盘
            try:
                if not supersede:
                    shutil_rmtree(change_dir)
            except Exception:  # noqa: BLE001
                pass
            result["reason"] = f"暂存区写入失败：{exc}"
            return result

    result["ok"] = True
    result["changeId"] = change_id
    result["targetPath"] = str(target_path)
    result["displayPath"] = display
    result["diffAdded"] = diff["diffAdded"]
    result["diffRemoved"] = diff["diffRemoved"]
    result["diffAvailable"] = diff_available
    result["supersededCount"] = superseded
    result["pendingNote"] = (
        f"同一目标的旧待审阅已被这条替换（累计替换 {superseded} 次），原先那份不会生效"
        if superseded
        else ""
    )
    result["note"] = _receipt(result)
    return result


def _receipt(item: dict) -> str:
    """给模型的回执：三件事必须说全——没写盘、变更号是什么、怎么合并。"""
    size = int(item.get("diffAdded") or 0)
    gone = int(item.get("diffRemoved") or 0)
    parts = [
        "【写暂存】内容**没有写进目标文件**，已存入暂存区等用户在界面批准 diff 后合并",
        f"变更号 {item.get('changeId', '')}",
        f"目标 {item.get('displayPath', '')}",
    ]
    if item.get("diffAvailable"):
        parts.append(f"差异 +{size} / −{gone} 行")
    else:
        parts.append("无法生成行级差异，界面按整份替换审阅")
    if item.get("supersededCount"):
        parts.append(str(item.get("pendingNote", "")))
    parts.append("同一文件重复写会替换这条待审阅，不会叠成两条")
    return "；".join(p for p in parts if p)


def _display_path(target: Path, work_dir: Path | None) -> str:
    if work_dir is not None:
        try:
            return str(target.relative_to(Path(work_dir).resolve()))
        except (ValueError, OSError):
            pass
    return str(target)


# ── 读侧 ────────────────────────────────────────────────────────

def pending(task_id: str = "", limit: int = DEFAULT_LIST_LIMIT) -> dict:
    """列待审阅（含过期标记）。只读，不改任何状态。"""
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = DEFAULT_LIST_LIMIT
    size = max(1, min(size, MAX_LIST_LIMIT))
    errors: list[str] = []
    items: list[dict] = []
    for change_dir in _walk_changes(_task_slug(task_id) if task_id else ""):
        meta = _load_meta(change_dir, errors)
        if meta is None:
            continue
        items.append(_refresh_state(meta))
    items.sort(key=lambda m: int(m.get("updatedAtMs") or 0), reverse=True)
    stale = sum(1 for m in items if m.get("targetChanged"))
    return {
        "ok": True,
        "observed": bool(items),
        "items": items[:size],
        "total": len(items),
        "truncated": len(items) > size,
        "staleCount": stale,
        "loadErrors": errors,
        "stagingDir": str(staging_home()),
        "note": (
            f"{len(items)} 条待审阅，其中 {stale} 条已过期（目标文件在暂存之后又变了，不能合并）"
            if items
            else "没有待审阅的暂存改动"
        ),
    }


def detail(change_id: str) -> dict:
    """单条详情 + diff 正文 + 暂存的新内容（界面点开用）。"""
    change_dir, why = _find_change(change_id)
    if change_dir is None:
        return {"ok": False, "reason": why}
    errors: list[str] = []
    meta = _load_meta(change_dir, errors)
    if meta is None:
        return {"ok": False, "reason": errors[0] if errors else "暂存记录读不出来", "loadErrors": errors}
    item = _refresh_state(meta)
    try:
        item["diffText"] = _unified_diff_text(change_dir, meta)
    except OSError as exc:
        item["diffText"] = ""
        item["diffReadError"] = _brief(exc)
    try:
        raw = (change_dir / CONTENT_NAME).read_bytes()
        decoded, why_text = _as_text(raw)
        item["newContent"] = decoded if decoded is not None else ""
        item["newContentTextual"] = decoded is not None
        if decoded is None:
            item["newContentNote"] = why_text
        item["newSizeOnDisk"] = len(raw)
    except OSError as exc:
        item["newContent"] = ""
        item["newContentNote"] = f"暂存内容读不出来：{exc}"
    item["ok"] = True
    return item


def _unified_diff_text(change_dir: Path, meta: dict) -> str:
    """diff 现算不缓存：缓存的那份一旦与盘上不同步就会骗人。

    基线用的是**记录里那份指纹对应的内容**：目标在暂存之后又被改过时，这里算出来的
    仍然是「用户批准时该看到的那份差异」，而过期判定在 `_refresh_state` 里另说——
    界面看 diff、`apply` 拦合并，两件事不能互相冒充。
    """
    if not meta.get("diffAvailable"):
        return ""
    try:
        new_text = (change_dir / CONTENT_NAME).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    old_text = ""
    if bool(meta.get("baseExists")):
        try:
            base_raw = Path(str(meta.get("targetPath") or "")).read_bytes()
        except OSError:
            # 基线已经不在了：差异只能按「新建」呈现，界面上另有 targetChanged 说明原因
            base_raw = b""
        decoded, _ = _as_text(base_raw)
        if decoded is None:
            return ""
        old_text = decoded
    display = str(meta.get("displayPath") or Path(str(meta.get("targetPath") or "")).name)
    return _unified_diff(old_text, new_text, display, f"{display}（待审阅）")["text"]


def decisions(limit: int = 50, change_id: str = "") -> dict:
    """倒序读决定日志（合并 / 拒绝 / 过期）。只读，缺文件给空表。"""
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 50
    size = max(1, min(size, MAX_LIST_LIMIT))
    path = decisions_file()
    rows: list[dict] = []
    error = ""
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001  单行坏了不影响其余
                    continue
                if isinstance(row, dict) and (not change_id or row.get("changeId") == change_id):
                    # 能从这个文件里读出来就说明这一行已经落盘；把「有没有落盘」统一成读面的一个字段，
                    # 界面不必为「日志行」和「刚写入并返回的那条」写两套判定
                    row.setdefault("persisted", True)
                    rows.append(row)
        except OSError as exc:
            error = _brief(exc)
    rows.reverse()
    out = {"ok": not error, "items": rows[:size], "total": len(rows), "truncated": len(rows) > size}
    if error:
        out["reason"] = f"决定日志读失败：{error}"
    return out


# ── 第二步：合并 / 拒绝 ─────────────────────────────────────────

def apply(
    change_id: str,
    *,
    history: Any = None,
    work_dir: Path | None = None,
) -> dict:
    """批准后合并：重算目标指纹 → 原子写 → 销掉暂存记录。

    `history` 是产物版本历史句柄（`artifact_history_runtime` 模块本体，与 `build_toolkit`
    的开关同源）：给了就照普通写入那样先留旧版再盖，没给就在回执里**明说这次没有可回滚旧版**，
    不装作留过档。
    """
    result = {"ok": False, "changeId": change_id, "written": False, "reason": "", "note": ""}
    change_dir, why = _find_change(change_id)
    if change_dir is None:
        result["reason"] = why
        return result
    errors: list[str] = []
    meta = _load_meta(change_dir, errors)
    if meta is None:
        result["reason"] = errors[0] if errors else "暂存记录读不出来"
        return result
    item = _refresh_state(meta)
    target = Path(str(item.get("targetPath") or ""))

    with _lock:
        if str(item.get("status")) != STATUS_PENDING:
            result["reason"] = (
                f"这条记录当前状态是「{item.get('status')}」，不是待审阅，未做任何写入"
            )
            return result
        # 位置再判一次：暂存之后设置可能被改过，记录也可能来自别处。
        # 底线与文件访问档位一致——**绑定工作目录内**照旧允许（这正是当初暂存能通过的原因），
        # 越出它才进硬拒清单（家目录 / 引擎数据根 / 系统关键目录）。
        # 直接拿 `hard_deny_hit` 判所有路径会连用户自己的工作区一起拒掉：
        # Windows 的临时目录本来就在家目录里，那不是「越权」，是把功能自己关死。
        # 工作目录优先用调用方当下传的（设置改了就按新的算），没传才退回记录里那份——
        # 界面上的「批准」按钮是一次独立 HTTP 调用，它不该被要求先猜出当初的会话现场。
        effective_work_dir = work_dir if work_dir is not None else _recorded_work_dir(item)
        if effective_work_dir is not None:
            try:
                target.relative_to(Path(effective_work_dir).resolve())
                hit = ""
            except (ValueError, OSError):
                hit = _hard_deny(target)
        else:
            hit = _hard_deny(target)
        if hit:
            result["reason"] = f"目标落在禁止写入的位置，未合并：{hit}"
            _append_decision({
                "outcome": "denied", "changeId": change_id, "targetPath": str(target),
                "taskId": item.get("taskId", ""), "reason": hit,
            })
            return result

        if item.get("targetChanged"):
            # 过期：把状态落到盘上，界面上它就从「可批准」变成「需重做」
            item["status"] = STATUS_STALE
            item["updatedAtMs"] = _now_ms()
            try:
                _write_json(_meta_path(change_dir), item)
            except OSError as exc:
                result["note"] = f"过期状态没落进记录（内容未改）：{exc}"
            result["reason"] = (
                f"不能合并：{item.get('targetChangeNote', '目标文件已变')}。"
                "请让模型重新写一次（新的 diff 会重新等审批）——"
                "拿旧 diff 盖新内容，盖掉的正是用户批准时没看过的那部分"
            )
            _append_decision({
                "outcome": "stale", "changeId": change_id, "targetPath": str(target),
                "taskId": item.get("taskId", ""), "baseSha": item.get("baseSha", ""),
                "reason": item.get("targetChangeNote", ""),
            })
            return result

        try:
            raw_new = (change_dir / CONTENT_NAME).read_bytes()
        except OSError as exc:
            result["reason"] = f"暂存内容读不出来，未合并：{exc}"
            return result
        if _sha(raw_new) != str(item.get("newSha") or ""):
            result["reason"] = "暂存内容与记录里的指纹不符（暂存区被动过），未合并"
            _append_decision({
                "outcome": "denied", "changeId": change_id, "targetPath": str(target),
                "taskId": item.get("taskId", ""), "reason": "暂存内容指纹不符",
            })
            return result

        history_note = ""
        if history is not None:
            try:
                rec = history.record_current(target, task_id=str(item.get("taskId") or ""),
                                             tool="write_staging_apply")
                history_note = str(rec.get("note") or "").strip()
            except Exception as exc:  # noqa: BLE001  留档失败不挡用户已经批准的合并
                history_note = f"覆盖前留档失败（这一次没有可回滚的旧版本）：{exc}"
        else:
            history_note = "本次合并没有留旧版本（产物版本历史未点亮）"

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f"{_TMP_PREFIX}{change_id}.tmp"
        try:
            tmp.write_bytes(raw_new)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            result["reason"] = f"写目标文件失败，未合并：{exc}"
            return result

        try:
            shutil_rmtree(change_dir)
        except OSError as exc:
            result["note"] = (result["note"] + f"；暂存记录清理失败：{exc}").strip("；")
        result["ok"] = True
        result["written"] = True
        result["targetPath"] = str(target)
        result["displayPath"] = _display_path(target, effective_work_dir)
        result["writtenBytes"] = len(raw_new)
        result["historyNote"] = history_note
        result["note"] = f"已合并到 {result['displayPath']}" + (
            f"（{history_note}）" if history_note else ""
        )
    _append_decision({
        "outcome": "applied", "changeId": change_id, "targetPath": str(target),
        "taskId": item.get("taskId", ""), "newSha": item.get("newSha", ""),
        "baseSha": item.get("baseSha", ""), "writtenBytes": len(raw_new),
    })
    return result


def reject(change_id: str, reason: str = "") -> dict:
    """拒绝并丢掉暂存内容：目标文件从未被改过，这里只处理暂存区。"""
    result = {"ok": False, "changeId": change_id, "reason": ""}
    change_dir, why = _find_change(change_id)
    if change_dir is None:
        result["reason"] = why
        return result
    errors: list[str] = []
    meta = _load_meta(change_dir, errors)
    if meta is None:
        result["reason"] = errors[0] if errors else "暂存记录读不出来"
        return result
    target = Path(str(meta.get("targetPath") or ""))
    existed = target.is_file()
    with _lock:
        try:
            shutil_rmtree(change_dir)
        except OSError as exc:
            result["reason"] = f"暂存记录删不掉：{exc}"
            return result
    result["ok"] = True
    result["targetUntouched"] = True
    result["targetExistedBefore"] = existed
    result["note"] = (
        f"已拒绝变更 {change_id}：目标文件没被改过，暂存内容已丢掉"
        + (f"（原因：{_brief(reason, 120)}）" if str(reason or "").strip() else "")
    )
    _append_decision({
        "outcome": "rejected", "changeId": change_id, "targetPath": str(target),
        "taskId": meta.get("taskId", ""), "newSha": meta.get("newSha", ""),
        "reason": _brief(reason, 200),
    })
    return result


def discard_all(task_id: str = "") -> dict:
    """任务收尾时丢掉全部待审阅（不碰任何目标文件）。"""
    slug = _task_slug(task_id) if task_id else ""
    dropped: list[str] = []
    failed: list[str] = []
    with _lock:
        for change_dir in _walk_changes(slug):
            if not _meta_path(change_dir).is_file():
                continue
            try:
                shutil_rmtree(change_dir)
                dropped.append(change_dir.name)
            except OSError as exc:
                failed.append(f"{change_dir.name}：{exc}")
    return {
        "ok": not failed,
        "dropped": len(dropped),
        "changeIds": dropped,
        "failed": failed,
        "note": (
            f"丢掉 {len(dropped)} 条待审阅（目标文件未被改动）"
            + (f"，{len(failed)} 条没删掉：{failed[0]}" if failed else "")
        ),
    }


def shutil_rmtree(path: Path) -> None:
    """删目录树（不用 shutil 是为了把这个动作的名字留在本模块里，出问题好定位）。"""
    import shutil

    shutil.rmtree(path, ignore_errors=False)


def reset_for_tests() -> None:
    """自测用：整只暂存目录清空，避免用例之间串味（只在临时数据根下调用）。"""
    with _lock:
        home = staging_home()
        if home.is_dir():
            shutil_rmtree(home)
        home.mkdir(parents=True, exist_ok=True)


def report(policy: "WriteStaging", task_id: str = "") -> dict:
    """装配现场给界面的一份读数：档位 + 待审阅数量 + 说明，一次调用拿全。"""
    listing = pending(task_id=task_id)
    return {
        "mode": policy.mode,
        "modeLabel": MODE_LABELS.get(policy.mode, policy.mode),
        "staged": policy.staged,
        "note": policy.note(),
        "error": policy.error,
        "pendingTotal": listing.get("total", 0),
        "staleCount": listing.get("staleCount", 0),
        "limits": {
            "contentBytes": MAX_CONTENT_BYTES,
            "diffLines": MAX_DIFF_LINES,
            "pendingPerTask": MAX_PENDING_PER_TASK,
        },
    }
