# -*- coding: utf-8 -*-
"""可选遥测（缺陷 D-28 的前半，引擎侧）：**默认关闭、显式同意、看得见将发送什么**。

差距（台账 D-28 那句「线上问题不可见」）：引擎跑在用户机器上，崩了、卡了、某个工具反复被拒，
我们这边一个字都不知道。可这件事一旦做成「先收了再说」就变成把用户数据往外搬——
所以本模块的默认排序是**什么都不收**，且每一层都留了能被自测钉住的事实：

1. **默认关，且关这件事不需要理由**。本机没有同意文件 = 没同意：`enabled()` 回 False，
   `record()` 直接不落盘。没有「先进缓冲区、等用户改主意」这种中间态——缓冲区本身就是数据。
2. **同意要确认词**。开启必须在调用方带上 `CONFIRM_WORD`（与 `src-tauri/src/align/archive.rs`
   里清空记录用 `DELETE` 同一套路：把「顺手点一下」和「知道自己在同意什么」分开）。
   **关闭永远不需要确认词**，也不许设任何门槛——退出面比进入面难走是遥测最常见的耍赖。
   关闭同时**删掉本地缓冲**并**换掉安装标识**（跨同意期关联不了，这是能承诺的最强一句）。
3. **字段白名单，逐类**。`ALLOWED_FIELDS` 每一类事件只列几个纯量键；不在表里的键丢掉并计数。
   不记的类别（正文、提示词、文件路径、命令原文、模型名、任何凭据形状）连键名都不出现在表里——
   表就是契约，加字段必须同时改这张表，改这张表必须同时过本文件的自测。
   同意开着也不豁免这一条：同意的是这几列，不是「我写什么你收什么」。
4. **看得见将发送什么**：`preview()` 返回的就是 `flush()` 真要交出去的那份结构（同一份 JSON，
   不是另写一遍的展示版）；`status()` 如实说明有没有出口。本仓库现在**没有**遥测接收端，
   所以缺省读数就是「只缓冲、未发送」——`flush()` 在没有出口时**不吞事件、不虚报已发**，
   这正是 #165 那条「被停下的任务不许交付『任务完成』」的同一条底线。

隐私任务（`privacy=True`）一律不进缓冲，连计数都不算：隐私档的语义是「这一轮的内容与轨迹
都不出本机」，遥测不是例外。

安装标识 `installationId` 是随机的本机 uuid，**不是**账号也不是设备指纹：它只用来把同一个
安装在一段时间内的几条事件串起来。每次重新同意都会换一个新的，所以它无法用来长期跟人。

路径只从 `data_paths` 取；写盘一律原子；坏 JSON 改名让路并如实报，不静默当成没有。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import crash_runtime
import data_paths

#: 开启动作必须带的确认词（关闭永远不需要——见模块文档第 2 条）
CONFIRM_WORD = "AGREE"
#: 同意文件的格式版本：将来加字段要能被识别出来，不靠「读不出来就当没同意」蒙混
CONSENT_SCHEMA = "tongtian.telemetry.consent.v1"

#: 事件类别（**唯一来源**：界面、路由与自测都从这里读；不在表里的类别不落账）
#: 只有三类——每一类都对应台账 D-28 那句「线上问题不可见」的一个具体问号：
#: 引擎起来过没有、任务是怎么结束的、崩过几次。不收集审批偏好、模型档位、内容统计。
KINDS = ("engine_started", "task_finished", "crash_recorded")

#: 逐类字段白名单：只放纯量、只放「能定位问题」的那几列。
#: 注意这张表里**没有**正文、路径、命令、模型名、密钥——它们不是「暂时没列」，是不该列。
ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "engine_started": ("engineVersion", "osFamily", "pythonFamily"),
    "task_finished": (
        "engineVersion", "endReason", "durationMs", "modelCallCount", "toolCallCount",
        "hadError", "privacyBlocked", "checkpointResumable", "secureEnv",
    ),
    "crash_recorded": ("engineVersion", "category", "pendingReports"),
}

#: 字段来源逐条对得上装配现场（`server.py::_telemetry_task_finished`），不是设想出来的字段：
#: `endReason` = `run_agent` 的 `end_reason`（全文只有四处赋值）、
#: `modelCallCount` / `toolCallCount` = `checkpoint.last_summary` 的 `modelCalls` / `executedTools`、
#: `checkpointResumable` = `checkpoint.resumable`、`secureEnv` / `privacyBlocked` = 会话上的两个开关。
#: 计数字段是刻意留下的**唯一「量」类信号**：谁都能在不上报任何正文的前提下回答
#: 「它崩在第几次模型调用上」。版本、Python 细节一律只进本机现场（`crash_runtime.runtime_info`），
#: 不进这份要发出去的东西——多一列就多一条能把用户区分开的轴。

#: 类别专属的取值词表（不在词表里的值丢掉）：让「endReason」不会变成任意字符串的容身处。
#: 词表逐条对得上源码：`server.py` 里 `end_reason` 只有那四个赋值（2068/2339/2356/2363），
#: 类别表来自 `crash_runtime.CATEGORIES`——**不在此处重抄**，否则两边会各自漂移。
FIELD_VOCAB: dict[tuple[str, str], tuple[str, ...]] = {
    ("task_finished", "endReason"): ("completed", "cancelled", "privacy_blocked", "error"),
    ("crash_recorded", "category"): crash_runtime.CATEGORIES + (crash_runtime.CATEGORY_UNKNOWN,),
}

#: 字符串标量的长度上限：白名单里没有一个字段需要长文本
MAX_VALUE_CHARS = 120
#: 本地缓冲上限（条）：满了丢最旧的并如实计数，绝不「静默覆盖」也绝不无限长
MAX_BUFFERED_EVENTS = 500
#: 一次 flush 至多交出去多少条
FLUSH_BATCH = 50
#: 预览/列表的条数上下限
DEFAULT_PREVIEW_LIMIT = 20
MAX_PREVIEW_LIMIT = 100
#: 「没有出口」这件事的**唯一一句话**：flush 的回话与计数器读数都用它，
#: 界面读到的理由和这一次调用的理由必须同字（两处各写一套，改一处漏一处）。
NO_SINK_SUMMARY = "没有出口：事件留在本机缓冲，未发送"

EVENTS_NAME = data_paths.TELEMETRY_EVENTS_FILENAME
STATE_NAME = "state.json"
DECISIONS_NAME = "decisions.jsonl"

_lock = threading.RLock()

#: 出口只能由**进程内装配处**登记（`server.py` 的启动路径），任何 HTTP 路由都不许调：
#: 能远程设定「发到哪」就等于让远程决定用户数据往哪走。这条口径由自测按源码扫描钉住。
SINK_SETTERS = ("configure_sink",)

_STATE: dict[str, Any] = {"sink": None, "endpoint": "", "sinkLabel": ""}


def telemetry_home() -> Path:
    return data_paths.telemetry_dir()


def events_file() -> Path:
    return data_paths.telemetry_events_file()


def consent_file() -> Path:
    return data_paths.telemetry_consent_file()


def state_file() -> Path:
    return telemetry_home() / STATE_NAME


def decisions_file() -> Path:
    return telemetry_home() / DECISIONS_NAME


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ── 同意状态（本机唯一权威）────────────────────────────────────

def consent_state() -> dict[str, Any]:
    """读同意文件；**没有文件就是关闭**，读坏了也是关闭（并如实留一条 loadError）。

    四个分支返回的**键集合必须一致**（`on/atMs/via/schema/installationId/source/reason/loadError/movedTo`）：
    状态面与路由都按同一张表取值，缺一个键就是 `status()` 当场抛 KeyError——
    那是「查遥测状态」这个只读动作自己崩了，正是要治的病，不能反过来制造新的崩溃。
    所以这里用同一个 `blank` 起步，每个分支只覆盖自己那几项；`loadError`/`movedTo`
    在没有发生时是空串，不是「字段不存在」（界面写代码时不必再猜哪一档会有）。
    """
    path = consent_file()
    blank = {
        "on": False, "atMs": 0, "via": "", "schema": "", "installationId": "",
        "reason": "", "loadError": "", "movedTo": "",
    }
    if not path.exists():
        return {**blank, "reason": "本机没有同意文件：按默认关闭处理", "source": "absent"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        mover = path.with_name(f"{path.name}.corrupt-{_now_ms()}")
        try:
            path.rename(mover)
        except OSError:
            mover = path
        return {
            **blank,
            "reason": f"同意文件读不出来（{type(exc).__name__}），已改名让路并按默认关闭处理",
            "source": "corrupt",
            "loadError": f"{type(exc).__name__}: {str(exc)[:160]}",
            "movedTo": mover.name,
        }
    if not isinstance(data, dict):
        return {**blank, "reason": "同意文件不是一条对象，按默认关闭处理", "source": "invalid"}
    return {
        **blank,
        "on": bool(data.get("on")),
        "atMs": int(data.get("atMs") or 0),
        "via": str(data.get("via") or ""),
        "schema": str(data.get("schema") or ""),
        "installationId": str(data.get("installationId") or ""),
        "source": "file",
    }


def enabled() -> bool:
    return consent_state()["on"]


def _write_consent(on: bool, *, via: str, installation_id: str, at_ms: int) -> dict[str, Any]:
    payload = {
        "schema": CONSENT_SCHEMA,
        "on": bool(on),
        "atMs": int(at_ms),
        "atText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at_ms / 1000.0)),
        # 记「确认词给过了」这个事实，不记确认词本身（那是一句口令，不该留在盘上）
        "confirmWordSeen": bool(on),
        "via": str(via or "")[:60],
        "installationId": installation_id if on else "",
    }
    _atomic_write(consent_file(), json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _log_decision(entry: dict[str, Any]) -> None:
    """同意侧的每一次动作都追加一行本地审计（含被拒的尝试）——只在本机，不外发。"""
    try:
        _append_jsonl(decisions_file(), entry)
    except Exception:  # noqa: BLE001  审计写不上不影响主流程，但也别静默：状态面会显示读不出来
        pass


def set_consent(
    on: Any,
    *,
    word: str = "",
    via: str = "",
    at_ms: int = 0,
) -> dict[str, Any]:
    """改同意状态。开 = 必须带确认词；关 = 无条件允许，并清掉缓冲与安装标识。

    返回里 `changed` 说明有没有真的改；被拒时 `reason` 是给界面直接念的一句话。
    """
    want = bool(on)
    stamp = int(at_ms or _now_ms())
    before = consent_state()
    if want:
        given = str(word or "").strip().upper()
        if given != CONFIRM_WORD:
            _log_decision({
                "atMs": stamp, "action": "opt_in_refused", "via": str(via or "")[:60],
                "reason": "没带确认词或确认词不对：本次没有开启遥测",
                "wordLength": len(str(word or "")),
            })
            return {
                "ok": False,
                "changed": False,
                "on": before["on"],
                "reason": (
                    f"开启可选遥测需要你明确输入确认词「{CONFIRM_WORD}」：这表示同意把本文件"
                    f"字段白名单里那几列（不含任何正文、路径、命令与模型名）发送到已登记的出口。"
                    "没有收到确认词，所以什么都没改。"
                ),
            }
        payload = _write_consent(True, via=via, installation_id=uuid.uuid4().hex, at_ms=stamp)
        _log_decision({
            "atMs": stamp, "action": "opt_in", "via": str(via or "")[:60],
            "installationIdTail": payload["installationId"][-8:],
        })
        return {
            "ok": True, "changed": not before["on"], "on": True, "atMs": stamp,
            "installationId": payload["installationId"],
            "note": (
                f"已开启：只记「{'、'.join(KINDS)}」这几类事件的白名单字段。"
                "开启前请先用 preview 看明白将发送什么。"
                + (
                    f"当前登记的出口是 {sink_state()['label'] or sink_state()['endpoint'] or '进程内对象'}。"
                    if sink_state()["configured"]
                    else "本仓库目前没有遥测接收端，所以开启后也仍然是「只缓冲、未发送」。"
                )
            ),
        }
    # 关闭：先落同意状态，再删缓冲，最后换掉安装标识（不保留跨期可关联的任何东西）
    removed = 0
    if events_file().exists():
        try:
            removed = sum(1 for line in events_file().read_text(encoding="utf-8").splitlines() if line.strip())
            events_file().unlink()
        except OSError:
            removed = -1
    payload = _write_consent(False, via=via, installation_id="", at_ms=stamp)
    _log_decision({
        "atMs": stamp,
        "action": "opt_out",
        "via": str(via or "")[:60],
        "eventsRemoved": max(removed, 0),
        "bufferDeleteFailed": removed < 0,
    })
    return {
        "ok": True, "changed": before["on"], "on": False, "atMs": stamp,
        "eventsRemoved": max(removed, 0),
        "bufferDeleteFailed": removed < 0,
        "note": (
            "已关闭：同意文件写成 off，本地缓冲"
            + ("删除失败（见状态面 bufferDeleteFailed，可手动删）" if removed < 0 else "已整份删除")
            + "，安装标识同时作废——下次重新开启会换一个新的，关闭前后的事件关联不到同一个安装。"
        ),
    }


# ── 出口登记（只允许进程内）────────────────────────────────────

def configure_sink(sink: Any, *, endpoint: str = "", label: str = "") -> dict[str, Any]:
    """登记「发到哪」。**只由引擎启动路径调用**；HTTP 层没有任何入口能走到这里。

    `sink` 是一个 `callable(events: list[dict]) -> dict`。没有出口时一切外发动作都如实回
    「未发送」——这条是本模块存在的意义：宁可永远不发，也不能把「没发」说成「发了」。
    """
    if sink is not None and not callable(sink):
        return {"ok": False, "reason": "出口必须是一个可调用对象，收到的不是，本次没有登记"}
    _STATE["sink"] = sink
    _STATE["endpoint"] = str(endpoint or "")[:200]
    _STATE["sinkLabel"] = str(label or "")[:80]
    return {
        "ok": True,
        "configured": sink is not None,
        "endpoint": _STATE["endpoint"],
        "label": _STATE["sinkLabel"],
        "note": "出口是进程内登记：重启即失效，也不接受任何 HTTP 调用方设定",
    }


def sink_state() -> dict[str, Any]:
    return {
        "configured": _STATE["sink"] is not None,
        "endpoint": _STATE["endpoint"],
        "label": _STATE["sinkLabel"],
        "note": (
            "没有登记出口：事件只会留在本机缓冲里，flush 如实报「未发送」，不会假装发过"
            if _STATE["sink"] is None
            else f"已登记出口（{_STATE['sinkLabel'] or _STATE['endpoint'] or '进程内'}）：仅本机同意开启后才会用它"
        ),
    }


# ── 事件落缓冲 ─────────────────────────────────────────────────

def _clean_fields(kind: str, fields: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    """按该类的白名单收字段：不在表里的丢掉，词表外的值丢掉，非标量丢掉。"""
    allowed = ALLOWED_FIELDS.get(kind, ())
    raw = fields if isinstance(fields, dict) else {}
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    off_vocab: list[str] = []
    for key, value in raw.items():
        name = str(key)
        if name not in allowed:
            dropped.append(name[:60])
            continue
        if isinstance(value, bool):
            kept[name] = value
        elif isinstance(value, int):
            kept[name] = max(-2**53, min(value, 2**53))
        elif isinstance(value, float):
            kept[name] = round(float(value), 4)
        elif isinstance(value, str):
            vocab = FIELD_VOCAB.get((kind, name))
            text = value.strip()
            if vocab is not None and text not in vocab:
                off_vocab.append(f"{name}={text[:40]}")
                continue
            text = text[:MAX_VALUE_CHARS]
            # 白名单字段也可能是人写进去的自由文本（toolName、lane 名）：再过一次同一份脱敏，
            # 不再抄第二套正则——凭据形状的规则全仓库只有 crash_runtime 里那一份。
            kept[name] = str(crash_runtime.redact_secrets(text)["text"])
        else:
            dropped.append(f"{name}(非纯量)")
    return kept, dropped[:20], off_vocab[:20]


def record(kind: str, fields: dict[str, Any] | None = None, *, privacy: bool = False) -> dict[str, Any]:
    """按类别记一条事件；**没同意就一个字节也不写**，且任何失败都不抛（记事件不该弄坏任务）。"""
    if str(kind or "") not in KINDS:
        return {"ok": False, "buffered": False, "reason": f"事件类别「{str(kind)[:40]}」不在登记表里，没有落账"}
    if privacy:
        return {"ok": True, "buffered": False, "reason": "隐私任务：这一轮不记任何遥测事件（连计数都不算）"}
    consent = consent_state()
    if not consent["on"]:
        return {"ok": True, "buffered": False, "reason": "遥测未开启（默认关闭）：没有写入任何事件"}
    kept, dropped, off_vocab = _clean_fields(kind, fields)
    event = {
        "eventId": uuid.uuid4().hex,
        "kind": kind,
        "atMs": _now_ms(),
        "installationId": consent["installationId"],
        "fields": kept,
    }
    if dropped:
        event["droppedKeys"] = dropped
    if off_vocab:
        event["offVocab"] = off_vocab
    try:
        with _lock:
            _append_jsonl(events_file(), event)
            dropped_by_cap = _trim_buffer()
    except Exception as exc:  # noqa: BLE001  写不上就说写不上，不影响任务本身
        return {"ok": False, "buffered": False, "reason": f"事件写入本地缓冲失败：{type(exc).__name__}：{str(exc)[:160]}"}
    return {
        "ok": True,
        "buffered": True,
        "eventId": event["eventId"],
        "kind": kind,
        "keptFields": sorted(kept),
        "droppedKeys": dropped,
        "offVocab": off_vocab,
        "droppedByCap": dropped_by_cap,
    }


def _trim_buffer() -> int:
    """缓冲超过上限就删最旧的几条，并把累计数记进 state（不是静默丢）。"""
    rows = _read_events()
    if len(rows) <= MAX_BUFFERED_EVENTS:
        return 0
    victims = rows[: len(rows) - MAX_BUFFERED_EVENTS]
    keep = rows[len(rows) - MAX_BUFFERED_EVENTS:]
    _atomic_write(events_file(), "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in keep))
    bump_state(droppedByCap=len(victims))
    return len(victims)


def _read_events() -> list[dict[str, Any]]:
    path = events_file()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001  坏行跳过并计数，不整份作废
            rows.append({"_badLine": text[:120]})
            continue
        if isinstance(data, dict):
            rows.append(data)
        else:
            rows.append({"_badLine": str(data)[:120]})
    return rows


def read_state() -> dict[str, Any]:
    path = state_file()
    if not path.exists():
        return {"droppedByCap": 0, "flushedEvents": 0, "lastFlushAtMs": 0, "lastAttemptReason": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "droppedByCap": 0, "flushedEvents": 0, "lastFlushAtMs": 0,
            "lastAttemptReason": f"state.json 读不出来：{type(exc).__name__}",
            "loadError": True,
        }
    base = {"droppedByCap": 0, "flushedEvents": 0, "lastFlushAtMs": 0, "lastAttemptReason": ""}
    if not isinstance(data, dict):
        return base
    for key in base:
        if key in data:
            base[key] = data[key]
    return base


def bump_state(**changes: Any) -> dict[str, Any]:
    """计数器：`droppedByCap` / `flushedEvents` 累加，`lastFlushAtMs` 取新，`lastAttemptReason` 覆盖。

    键表仍以 `read_state()` 的骨架为准：不在骨架里的键一律不写，避免这份小文件变成第二个垃圾桶。
    """
    counters = ("droppedByCap", "flushedEvents")
    with _lock:
        data = read_state()
        data.pop("loadError", None)
        for key, value in changes.items():
            if key not in data:
                continue
            if key in counters:
                data[key] = int(data[key]) + int(value)
            elif key == "lastFlushAtMs":
                data[key] = max(int(data[key]), int(value))
            else:
                data[key] = str(value)[:200]
        _atomic_write(state_file(), json.dumps(data, ensure_ascii=False, indent=2))
        return data


# ── 看 / 发 ────────────────────────────────────────────────────

def pending_count() -> int:
    return sum(1 for row in _read_events() if not row.get("_badLine"))


def preview(limit: Any = DEFAULT_PREVIEW_LIMIT) -> dict[str, Any]:
    """**这就是真要发出去的那份东西**：结构与 `flush()` 交给出口的完全一致。

    同一段构造代码（`_rows_from`）两处共用，界面不可能显示一份温和的、实际发另一份。
    """
    size = _limit(limit)
    rows = _read_events()
    good = [row for row in rows if not row.get("_badLine")]
    payload = _rows_from(good, size)
    return {
        "ok": True,
        "enabled": enabled(),
        "count": len(good),
        "shown": len(payload),
        "badLines": len(rows) - len(good),
        "limit": size,
        "payload": payload,
        "destination": sink_state(),
        "kinds": list(KINDS),
        "allowedFields": {kind: list(fields) for kind, fields in ALLOWED_FIELDS.items()},
        "note": (
            "上面这份就是出口会收到的数组（不含任何正文、路径、命令与模型名；"
            "只有本文件字段白名单里那几列）。"
            + ("当前未开启，所以这份预览只是「如果开启会看到什么」，本机现在一个字节都没记。"
               if not enabled() else
               "当前已开启；没有登记出口时 flush 仍然如实报未发送。")
        ),
    }


def _limit(raw: Any) -> int:
    try:
        size = int(str(raw).strip())
    except (TypeError, ValueError):
        size = DEFAULT_PREVIEW_LIMIT
    return max(1, min(size, MAX_PREVIEW_LIMIT))


def _batch(raw: Any) -> int:
    """一次 flush 的条数：认不出来就按缺省批次，绝不因为参数脏而抛（这是任务收尾路径）。"""
    try:
        size = int(str(raw).strip())
    except (TypeError, ValueError):
        size = FLUSH_BATCH
    return max(1, min(size, FLUSH_BATCH))


def _rows_from(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """出口收到的行：只保留信封里那几个键 + 白名单字段；**按先进先出取最旧的 size 条**。

    缓冲区里额外记的 `droppedKeys` / `offVocab` 是**本机审计**用的（说明这条被削过），
    不外发——发出去只会让接收端多拿一份本地治理信息，没有排查价值。

    取「最旧的 size 条」而不是「最新的 size 条」：`flush` 承诺过的是把最旧的先送走。
    切片方向写反时，缓冲一旦长期大于批次，最旧那几条永远不会被发出——遥测在客户端
    积压十年，接收端看到的却全是最后几秒，这种「只发最近」的错觉比不发更坏。
    """
    out: list[dict[str, Any]] = []
    for row in rows[:size]:
        fields = row.get("fields")
        out.append({
            "eventId": str(row.get("eventId") or ""),
            "kind": str(row.get("kind") or ""),
            "atMs": int(row.get("atMs") or 0),
            "installationId": str(row.get("installationId") or ""),
            "fields": fields if isinstance(fields, dict) else {},
        })
    return out


def _payload_rows(size: int) -> list[dict[str, Any]]:
    return _rows_from([row for row in _read_events() if not row.get("_badLine")], size)


def flush(*, limit: Any = FLUSH_BATCH) -> dict[str, Any]:
    """把最旧的若干条交给出口；**没有出口就照实说没发**，事件一条也不删。"""
    size = _batch(limit)
    if not enabled():
        return {
            "ok": True, "sent": 0, "delivered": False, "remaining": pending_count(),
            "reason": "遥测未开启（默认关闭）：本次没有发送任何东西",
        }
    rows = _payload_rows(size)
    if not rows:
        return {"ok": True, "sent": 0, "delivered": True, "remaining": 0, "reason": "缓冲是空的，没有要发的事件"}
    sink = _STATE["sink"]
    if sink is None:
        bump_state(lastAttemptReason=NO_SINK_SUMMARY, lastFlushAtMs=_now_ms())
        return {
            "ok": True, "sent": 0, "delivered": False, "remaining": len(rows),
            "attempted": len(rows),
            "reason": (
                f"{NO_SINK_SUMMARY}——本仓库目前没有遥测接收端，所以这 "
                f"{len(rows)} 条仍然只在你自己的机器上（{events_file()}）。"
                "这里不会把「没发」写成「已发」，也不会因为发了就删——删了就没有可核对的东西了。"
            ),
            "preview": rows,
        }
    try:
        result = sink(rows)
    except Exception as exc:  # noqa: BLE001  出口炸了不该弄坏引擎
        bump_state(lastAttemptReason=f"出口抛出异常：{type(exc).__name__}", lastFlushAtMs=_now_ms())
        return {
            "ok": False, "sent": 0, "delivered": False, "remaining": len(rows),
            "reason": f"出口调用失败，事件全部留在本机：{type(exc).__name__}：{str(exc)[:160]}",
        }
    delivered = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    if not delivered:
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("reason") or result.get("message") or "")[:200]
        bump_state(lastAttemptReason=f"出口拒绝：{reason or '未说明原因'}", lastFlushAtMs=_now_ms())
        return {
            "ok": False, "sent": 0, "delivered": False, "remaining": len(rows),
            "reason": f"出口没有收下：{reason or '未说明原因'}；事件仍全部留在本机",
        }
    _drop_delivered(rows)
    bump_state(flushedEvents=len(rows), lastFlushAtMs=_now_ms(), lastAttemptReason="")
    return {
        "ok": True, "sent": len(rows), "delivered": True,
        "remaining": pending_count(),
        "endpoint": _STATE["endpoint"] or _STATE["sinkLabel"],
        "note": "出口确认收下后才从缓冲移除；没确认就一条也不动",
    }


def _drop_delivered(rows: list[dict[str, Any]]) -> None:
    """按 eventId 精确移除已送达的行——不按条数截断，避免并发新写入时被误删。"""
    delivered = {str(row.get("eventId") or "") for row in rows}
    with _lock:
        rest = [row for row in _read_events() if str(row.get("eventId") or "") not in delivered]
        if rest:
            _atomic_write(events_file(), "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rest))
        elif events_file().exists():
            try:
                events_file().unlink()
            except OSError:
                pass


def discard_all() -> dict[str, Any]:
    """清掉本地缓冲（同意与否都能用；这是「我先把攒下的删了」那条退出面）。"""
    path = events_file()
    if not path.exists():
        return {"ok": True, "removed": 0, "reason": "缓冲本来就是空的"}
    count = sum(1 for row in _read_events() if not row.get("_badLine"))
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "removed": 0, "reason": f"删不掉缓冲：{type(exc).__name__}：{str(exc)[:160]}"}
    return {"ok": True, "removed": count, "reason": f"本地缓冲已清空（{count} 条）；同意状态没有改动"}


# ── 状态面 ─────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    consent = consent_state()
    state = read_state()
    rows = _read_events()
    kinds_seen: dict[str, int] = {}
    for row in rows:
        if row.get("_badLine"):
            continue
        kinds_seen[str(row.get("kind") or "")] = kinds_seen.get(str(row.get("kind") or ""), 0) + 1
    return {
        "ok": True,
        "enabled": consent["on"],
        "consent": {key: value for key, value in consent.items() if key != "installationId"},
        "installationIdTail": (consent["installationId"] or "")[-8:],
        "buffered": len([row for row in rows if not row.get("_badLine")]),
        "badLines": len([row for row in rows if row.get("_badLine")]),
        "byKind": kinds_seen,
        "bufferPath": str(events_file()),
        "consentPath": str(consent_file()),
        "limits": {"maxBufferedEvents": MAX_BUFFERED_EVENTS, "flushBatch": FLUSH_BATCH, "maxValueChars": MAX_VALUE_CHARS},
        "counters": state,
        "sink": sink_state(),
        "confirmWord": CONFIRM_WORD,
        "note": (
            "遥测未开启（缺省）：本机没有记录任何事件。"
            if not consent["on"]
            else "遥测已开启：只记白名单字段；出口没登记时一切外发动作都如实报未发送。"
        ),
    }


def describe() -> dict[str, Any]:
    """界面开场一次拿全：类别、逐类字段、确认词、上限、有没有出口。"""
    return {
        "ok": True,
        "defaultOn": False,
        "kinds": list(KINDS),
        "allowedFields": {kind: list(fields) for kind, fields in ALLOWED_FIELDS.items()},
        "fieldVocab": {f"{kind}.{field}": list(values) for (kind, field), values in FIELD_VOCAB.items()},
        "neverCollected": [
            "对话与提示词正文", "文件路径与工作目录", "命令原文", "模型名与密钥",
            "崩溃 traceback", "工单与会话正文", "任何凭据形状（再过一次全仓库唯一那份脱敏）",
        ],
        "confirmWord": CONFIRM_WORD,
        "limits": {
            "maxBufferedEvents": MAX_BUFFERED_EVENTS,
            "flushBatch": FLUSH_BATCH,
            "maxValueChars": MAX_VALUE_CHARS,
            "previewLimit": MAX_PREVIEW_LIMIT,
        },
        "schema": CONSENT_SCHEMA,
        "paths": {
            "dir": str(telemetry_home()),
            "consent": str(consent_file()),
            "events": str(events_file()),
            "decisions": str(decisions_file()),
        },
        "sink": sink_state(),
        "note": (
            "默认关闭；开启需要确认词，关闭不需要且不设门槛。同意文件是本机唯一权威，"
            "runtime 与界面都只读它——HTTP 调用方没有任何路径能改出口或代用户同意。"
        ),
    }


def decisions(limit: Any = 30) -> dict[str, Any]:
    """同意侧的本地审计（开启 / 关闭 / 被拒的尝试）：只在本机读，不外发。"""
    path = decisions_file()
    size = max(1, min(int(str(limit).strip() or 30), MAX_PREVIEW_LIMIT))
    rows: list[dict[str, Any]] = []
    load_errors = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                load_errors += 1
                continue
            if isinstance(data, dict):
                rows.append(data)
    return {
        "ok": True,
        "count": len(rows),
        "returned": len(rows[-size:]),
        "decisions": rows[-size:],
        "loadErrors": load_errors,
        "note": "这份日志只在本机：它记的是「同不同意」这件事，不是内容本体",
    }


def reset_for_tests() -> None:
    """自测隔离用：清掉进程内出口登记（数据根由用例重定向到临时目录）。"""
    with _lock:
        _STATE["sink"] = None
        _STATE["endpoint"] = ""
        _STATE["sinkLabel"] = ""
