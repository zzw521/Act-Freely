# -*- coding: utf-8 -*-
"""崩溃现场的本地留存与导出（缺陷 D-28 的后半，引擎侧）。

差距（台账 D-28 那句「线上问题不可见」）在这里比遥测更硬：引擎是桌面壳起的子进程，
`src-tauri/src/bridge/agent.rs` 把它的 stdout/stderr 一路 `pump_logs()` 转成 GUI 进程的
`println!`，而 GUI 是 `windows_subsystem="windows"`（`src-tauri/src/main.rs:1`）——**那两条
管道没有目的地**。所以今天一条未捕获异常从引擎里抛出来，用户看见的只有「任务转圈然后失败」，
 traceback 一个字也没落到任何地方；重启之后连「刚才崩过」都无法证明。
本文件先把这半件事做出来：**崩了要在本机留下一份能读的现场，并明确提示人可以导出**。

四条口径，都是这份现场会被别人读到而定的：

1. **只留本机，绝不自动外发**。导出（`export`）是一次显式动作，产出一份文本副本并回给人
   路径、字节数与指纹；导出**不删原件**（删是另一条 `delete`，用户自己决定时机）。
   遥测那半边（`telemetry_runtime`）才有「发出去」的概念，而且默认关——这里没有出口。
2. **落盘之前先打掉凭据，不是导出时**。现场里最值钱也最危险的就是 traceback：里面可能
   原样印着 `headers={"Authorization": "Bearer sk-..."}`。写到磁盘之后它就可能被别的工具
   读走、被用户整目录打包上传，所以脱敏发生在**落盘之前**（`redact_secrets`），
   并把「打掉了哪几类」写进记录里，让人知道这份现场被改过、改在哪里。
3. **家目录与数据根换成占位符**。traceback 里的绝对路径会带出用户名；真实路径对排查几乎
   不需要（栈里有 `~` 前缀就够定位仓库内文件），所以默认换成 `~` 与 `<数据根>`。
   需要真路径的人可以在自己的机器上还原——那份文件本来就在他的机器上。
4. **有上限、会重复计数、handler 自己不许崩**。报告数超过 `MAX_REPORTS` 就丢最旧的并如实
   计数；同类同栈在 `DEDUPE_WINDOW_MS` 内合成一条并累加 `times`（一次崩溃往往在几个线程里
   各抛一次，五十条一样的东西既占盘又淹掉别的现场）； excepthook 里任何异常一律吞掉——
   处理崩溃的代码再崩一次，就把「有现场可看」这件事彻底输掉了。

只管**崩溃**。业务性失败（模型返回 4xx、路径越界被拒、审批没过）不走这里，那些有自己的
回话与审计；把可预期的失败堆进崩溃现场，等于把真崩溃埋进噪声里。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback as _traceback
from pathlib import Path
from typing import Any

import data_paths

#: 单条 traceback 的落盘上限：真炸的栈 rarely 超过它，超了的多半是被塞了正文
MAX_TRACEBACK_BYTES = 64 * 1024
#: 保留的报告条数上限：满了丢最旧的，并如实报 `droppedByCap`
MAX_REPORTS = 50
#: 列表缺省 / 上限
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 200
#: 同栈去重窗口（毫秒）：一次故障常在主线程 + 工作线程 + 循环回调里各抛一次
DEDUPE_WINDOW_MS = 60_000
#: 单条理由文本的展示上限（字符）——`record(note=...)` 用它，报告里那条「谁说的话」
MAX_NOTE_CHARS = 400
#: 去重时往回看几条：真炸时同一份栈会在几个钩子里连着抛，只比对最近这几条就够了
DEDUPE_LOOKBACK = 12

REPORT_DIRNAME = "reports"
EXPORT_DIRNAME = "exported"
REPORT_SUFFIX = ".json"
EXPORT_PREFIX = "crash-export-"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: 类别只认这几个：界面与自测都按这张表说话，跑出表外一律归 `unknown`
CATEGORIES = (
    "uncaught_exception",   # sys.excepthook：主线程没接住的异常
    "worker_thread",        # threading.excepthook：工作线程里抛出来的
    "event_loop",           # asyncio 循环回调里抛出来的
    "http_unhandled",       # FastAPI 兜底处理器接住的请求级异常
    "manual",               # 代码显式记录（例如启动自检失败）
)
CATEGORY_UNKNOWN = "unknown"

#: 现场里允许带的上下文字段（标量白名单，与遥测同一套理由：不在表里的东西进不了这份文件）
CONTEXT_FIELDS = ("engineVersion", "taskId", "lane", "endpoint", "provider")

_lock = threading.RLock()
_STATE: dict[str, Any] = {"installed": False, "previous": {}, "installError": ""}
#: 进程内观察者（装配现场登记，用来把「崩过一次」转成一条计数事件）；默认没有
_OBSERVER: dict[str, Any] = {"callback": None, "notified": 0, "error": ""}

#: 脱敏规则的名字（测试按名字断言「这一类确实被打掉了」，规则表与名字表同源）
REDACTION_KINDS = (
    "认证头",
    "键值凭据",
    "模型密钥",
    "JWT",
    "长随机串",
    "家目录",
    "数据根",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def crash_home() -> Path:
    """崩溃现场根目录：路径本身由 `data_paths.crash_dir()` 解析（D-43 单一来源口径）。"""
    return data_paths.crash_dir()


def reports_dir() -> Path:
    return crash_home() / REPORT_DIRNAME


def exports_dir() -> Path:
    return crash_home() / EXPORT_DIRNAME


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + `os.replace`：现场写到一半就断电，等于没有现场。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


# ── 脱敏 ────────────────────────────────────────────────────────

def _looks_like_secret_blob(matched: str) -> bool:
    """「长随机串」这条的判据：同串里既有数字又有字母才算密钥形状。

    没有这道判据时，一段 64 KB 的 `X`×N（压缩体、data URI，或者自测造的超长栈）会被整块
    打成一个 `***`——那是把崩溃现场弄丢，不是脱敏（现场的价值恰恰在栈本身）。真实凭据几乎
    总是字母数字混排，用这一条换来的保真度值得。
    """
    body = matched.rstrip("=")
    return any(char.isdigit() for char in body) and any(char.isalpha() for char in body)


#: 每条规则 = `(类名, 正则, 替换, 取值判据或 None)`；判据为 None 表示「匹配到就打」。
#: 判据走回调而不是往正则里塞三个 `(?=…)`：后者在 64 KB 的长串上是平方级扫描，
#: 一次脱敏能把引擎卡住几分钟——处理崩溃的代码自己变成故障源。
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str, Any], ...] = (
    # 值的那一半必须同时吃下两种写法：`Authorization: Bearer x` 与 `{"Authorization": "Bearer x"}`。
    # 早期只写了 `\S+`，于是字典 repr（栈里最常见的形态）整条漏掉，「已脱敏」只在裸写法上成立。
    # 现在：键名后允许一个引号再跟冒号，值要么是引号里的整串，要么是非分隔符的一截。
    ("认证头", re.compile(
        r"(?i)\b(proxy-authorization|authorization|cookie|set-cookie)\b(\s*[\"']?\s*[:=]\s*)"
        r"(?:[\"'][^\"']*[\"']|[^\s,;'\"]+)"), r"\1\2***", None),
    ("键值凭据", re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|"
        r"password|passwd|secret|token)\b(\s*[\"']?\s*[:=]\s*)"
        r"(?:[\"'][^\"']*[\"']|[^\s,;'\"]+)"), r"\1\2***", None),
    ("模型密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***", None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "***.***.***", None),
    # 长随机串放在最后：前四条已经把常见凭据形状处理掉了，这条兜的是「裸 token 直接印在栈里」。
    ("长随机串", re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"), "***", _looks_like_secret_blob),
)


def _home_prefixes() -> tuple[str, str]:
    home = ""
    root = ""
    try:
        home = str(Path.home())
    except Exception:  # noqa: BLE001  取不到家目录就不换，不许因此丢现场
        home = ""
    try:
        root = str(data_paths.engine_home())
    except Exception:  # noqa: BLE001
        root = ""
    return home, root


def redact_secrets(text: Any) -> dict[str, Any]:
    """把凭据形状从文本里打掉；回 `{text, kinds: [打掉了哪几类], hits: n}`。

    规则表在 `_SECRET_PATTERNS`，名字表在 `REDACTION_KINDS`——两条同源于本文件，
    界面与自测都从这里读，不在别处再抄一份正则。
    """
    raw = str(text or "")
    home, root = _home_prefixes()
    out = raw
    kinds: list[str] = []
    hits = 0
    for name, pattern, repl, accepts in _SECRET_PATTERNS:
        if accepts is None:
            out, n = pattern.subn(repl, out)
        else:
            # 有判据的那条走回调：匹配到的串先过一遍 `_looks_like_secret_blob`，
            # 不像凭据就原样留下（现场正文里 32 个以上的连续字母太常见了）。
            n = 0

            def _replace(match: re.Match[str]) -> str:
                nonlocal n
                if accepts(match.group(0)):
                    n += 1
                    return repl
                return match.group(0)

            out = pattern.sub(_replace, out)
        if n:
            kinds.append(name)
            hits += n
    for name, base in (("数据根", root), ("家目录", home)):
        for variant in _path_variants(base):
            if variant and variant in out:
                out = out.replace(variant, "<数据根>" if name == "数据根" else "~")
                if name not in kinds:
                    kinds.append(name)
    # 先换数据根再换家目录：数据根通常就在家目录里，反过来会把 `<数据根>` 又换成 `~/<...>`
    return {"text": out, "kinds": kinds, "hits": hits}


def _path_variants(base: str) -> list[str]:
    """把同一目录的常见写法都列出来（Windows 的大小写与 `/`\、混用是真会发生的）。"""
    if not base:
        return []
    variants = [base, base.rstrip("\\/")]
    for value in list(variants):
        if not value:
            continue
        variants.append(value.replace("\\", "/"))
        variants.append(value.replace("/", "\\"))
        variants.append(value.lower())
        variants.append(value.replace("\\", "/").lower())
    seen: list[str] = []
    for value in variants:
        if value and value not in seen:
            seen.append(value)
    # 长的先换：数据根比家目录长，先换长的才不会被短的截断成 `~/<数据根>`
    return sorted(seen, key=len, reverse=True)


# ── 报告读写 ────────────────────────────────────────────────────

def _report_path(report_id: str) -> Path | None:
    """按 id 找报告文件；id 不合法或解析出目录外一律回 None（不给路径穿越留口）。"""
    text = str(report_id or "").strip()
    if not _SAFE_ID.match(text):
        return None
    root = reports_dir().resolve()
    candidate = (root / f"{text}{REPORT_SUFFIX}").resolve()
    if not str(candidate).startswith(str(root) + os.sep) and candidate.parent != root:
        return None
    return candidate


def new_report_id(category: str, digest: str, at_ms: int) -> str:
    """编号里带**毫秒**，为的是「文件名排序就是时间排序」这件事真的成立。

    只到秒时，同一秒里崩两次得到的是 `crash-<同一秒>-<类别>-<摘要前 8 位>`，按名排序变成了
    按摘要排序——而 `_enforce_cap` 丢最旧与 `_merge_duplicate` 只回看最近若干份，两处都靠
    「名字序＝时间序」这个假设。假设一破，超上限时可能删掉最新的那份，去重也可能看不见
    窗口里那条。一次故障在几个钩子里各抛一次正好就是「同一秒内多条」这种形状。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(at_ms / 1000.0))
    return f"crash-{stamp}-{int(at_ms) % 1000:03d}-{category[:12]}-{digest[:8]}"


def record(
    category: str,
    *,
    exc_type: str = "",
    exc_text: str = "",
    traceback_text: str = "",
    context: dict[str, Any] | None = None,
    at_ms: int = 0,
    note: str = "",
) -> dict[str, Any]:
    """落一份崩溃现场；回 `{ok, reportId, path, bytes, sha256, deduped, ...}`。

    写失败**不抛**（这是崩溃路径，抛出去就没人接了）：回 `ok=False` 与中文 `reason`，
    调用方（excepthook）把它印到 stderr，至少人还能看见一次。
    """
    stamp = int(at_ms or _now_ms())
    kind = category if category in CATEGORIES else CATEGORY_UNKNOWN
    clean_tb = redact_secrets(traceback_text or "")
    body = str(clean_tb["text"])
    truncated = len(body.encode("utf-8", errors="replace")) > MAX_TRACEBACK_BYTES
    if truncated:
        body = body.encode("utf-8", errors="replace")[:MAX_TRACEBACK_BYTES].decode("utf-8", errors="replace")
    clean_exc = redact_secrets(exc_text or "")
    clean_note = redact_secrets(note or "")
    trace_digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
    safe_context = _clean_context(context)
    report_id = new_report_id(kind, trace_digest, stamp)
    try:
        with _lock:
            merged = _merge_duplicate(kind, trace_digest, stamp)
            if merged is not None:
                return {**merged, "ok": True, "deduped": True}
            payload = {
                "reportId": report_id,
                "atMs": stamp,
                "atText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp / 1000.0)),
                "category": kind,
                "categoryDeclared": str(category or ""),
                "times": 1,
                "excType": str(exc_type or "")[:120],
                "excText": str(clean_exc["text"])[:2000],
                "note": str(clean_note["text"])[:MAX_NOTE_CHARS],
                "traceBytes": len(body.encode("utf-8", errors="replace")),
                "traceSha256": trace_digest,
                "traceTruncated": bool(truncated),
                "redacted": {
                    "kinds": sorted(set(clean_tb["kinds"]) | set(clean_exc["kinds"]) | set(clean_note["kinds"])),
                    "hits": int(clean_tb["hits"]) + int(clean_exc["hits"]) + int(clean_note["hits"]),
                },
                "context": safe_context,
                "runtime": runtime_info(),
                "exportedAtMs": 0,
                "traceback": body,
            }
            path = _report_path(report_id)
            if path is None:
                return {"ok": False, "reason": "报告编号没能算成合法文件名，这条现场没落盘"}
            _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
            dropped = _enforce_cap()
    except Exception as exc:  # noqa: BLE001  崩溃路径不许再抛
        return {"ok": False, "reason": f"崩溃现场写入失败：{type(exc).__name__}：{str(exc)[:200]}"}
    # 新落一份才通知观察者（去重那一次不通知：合并本身就为了少吵一遍）
    _notify(kind, report_id)
    return {
        "ok": True,
        "reportId": report_id,
        "path": str(path),
        "bytes": payload["traceBytes"],
        "sha256": trace_digest[:16],
        "deduped": False,
        "droppedByCap": dropped,
        "redactedKinds": payload["redacted"]["kinds"],
    }


def _merge_duplicate(kind: str, trace_digest: str, stamp: int) -> dict[str, Any] | None:
    """同类别 + 同栈 + 在去重窗口内 → 累加次数而不是再写一份。

    只往回看最近 `DEDUPE_LOOKBACK` 份（按文件名倒序，编号带时间戳所以文件名序就是时间序）：
    一次故障在几个钩子里各抛一次是紧挨着发生的，翻全目录既慢又会把「一小时后同样的栈」
    误合成一条——那会抹掉两次独立崩溃之间的时间事实。
    """
    root = reports_dir()
    if not root.exists():
        return None
    for candidate in sorted(root.glob(f"*{REPORT_SUFFIX}"), key=lambda p: p.name, reverse=True)[:DEDUPE_LOOKBACK]:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  读不出来就当不是同一条，宁可多写一份也不丢现场
            continue
        if data.get("category") != kind or data.get("traceSha256") != trace_digest:
            continue
        if stamp - int(data.get("atMs") or 0) > DEDUPE_WINDOW_MS:
            continue
        data["times"] = int(data.get("times") or 1) + 1
        data["lastSeenMs"] = stamp
        _atomic_write(candidate, json.dumps(data, ensure_ascii=False, indent=2))
        return {"reportId": str(data.get("reportId") or candidate.stem), "atMs": stamp, "times": data["times"]}
    return None


def _enforce_cap() -> int:
    root = reports_dir()
    if not root.exists():
        return 0
    files = [p for p in root.glob(f"*{REPORT_SUFFIX}") if p.is_file()]
    if len(files) <= MAX_REPORTS:
        return 0
    ordered = sorted(files, key=lambda p: p.name)
    victims = ordered[: len(files) - MAX_REPORTS]
    for victim in victims:
        try:
            victim.unlink()
        except OSError:
            pass
    return len(victims)


def _clean_context(context: Any) -> dict[str, Any]:
    """上下文字段走白名单 + 标量：不在表里的键丢掉并计数（现场不是正文的容身处）。"""
    raw = context if isinstance(context, dict) else {}
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in raw.items():
        name = str(key)
        if name not in CONTEXT_FIELDS:
            dropped.append(name[:40])
            continue
        if isinstance(value, bool) or isinstance(value, int) or value is None:
            out[name] = value
        elif isinstance(value, str):
            out[name] = str(redact_secrets(value[:160])["text"])
        else:
            dropped.append(f"{name}(非纯量)")
    if dropped:
        out["_droppedKeys"] = dropped[:20]
    return out


def set_recorder(callback: Any) -> dict[str, Any]:
    """登记「谁想知道崩过」：**进程内**观察者，与遥测出口同一套口径（HTTP 调用方给不了）。

    本模块不认识 `telemetry_runtime`——崩溃留存不该依赖外发面。装配现场（`server.py`）
    在这里挂一个函数，把「新落了一份现场」转成一条计数事件；观察者抛出任何异常都只记
    一句话进 `_OBSERVER["error"]`，绝不反过来影响留存本身。
    """
    if callback is not None and not callable(callback):
        return {"ok": False, "registered": False, "reason": "观察者必须是可调用对象或 None，本次没有登记"}
    _OBSERVER["callback"] = callback
    return {"ok": True, "registered": callback is not None, "note": "只在进程内生效，重启即失效"}


def recorder_state() -> dict[str, Any]:
    return {
        "registered": _OBSERVER["callback"] is not None,
        "notified": int(_OBSERVER.get("notified") or 0),
        "error": str(_OBSERVER.get("error") or ""),
    }


def _notify(kind: str, report_id: str) -> None:
    """只在新落一份现场后调用（去重合并的那一次不重复通知，去重的意义就是别再吵一遍）。"""
    callback = _OBSERVER.get("callback")
    if callback is None:
        return
    try:
        callback({"category": kind, "reportId": report_id})
        _OBSERVER["notified"] = int(_OBSERVER.get("notified") or 0) + 1
    except Exception as exc:  # noqa: BLE001  观察者炸了不能把留存拖下水
        _OBSERVER["error"] = f"崩溃观察者抛出异常：{type(exc).__name__}：{str(exc)[:160]}"


def runtime_info() -> dict[str, Any]:
    """现场里的运行环境读数：只到「哪台机器什么版本」这一层，不含用户名与机器名。"""
    return {
        "python": sys.version.split()[0],
        "implementation": getattr(sys, "implementation", None) and sys.implementation.name or "",
        "platform": _platform_label(),
        "pid": os.getpid(),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _platform_label() -> str:
    try:
        import platform as _platform

        return f"{_platform.system()}-{_platform.machine()}-{_platform.release()}"[:120]
    except Exception:  # noqa: BLE001
        return ""


# ── 列表 / 详情 / 导出 / 删除 ───────────────────────────────────

def _read_report(path: Path) -> dict[str, Any]:
    """坏 JSON 改名让路并如实报，不静默当成没有（与 `write_staging_runtime` 同口径）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        mover = path.with_name(f"{path.name}.corrupt-{_now_ms()}")
        try:
            path.rename(mover)
        except OSError:
            mover = path
        return {"_loadError": f"{type(exc).__name__}：{str(exc)[:160]}", "_movedTo": mover.name}


def list_reports(limit: Any = DEFAULT_LIST_LIMIT) -> dict[str, Any]:
    try:
        size = int(str(limit).strip())
    except (TypeError, ValueError):
        size = DEFAULT_LIST_LIMIT
    size = max(1, min(size, MAX_LIST_LIMIT))
    root = reports_dir()
    rows: list[dict[str, Any]] = []
    load_errors = 0
    if root.exists():
        for path in sorted(root.glob(f"*{REPORT_SUFFIX}"), key=lambda p: p.name, reverse=True):
            data = _read_report(path)
            if data.get("_loadError"):
                load_errors += 1
                rows.append({
                    "reportId": path.stem,
                    "atMs": 0,
                    "category": "unreadable",
                    "reason": data["_loadError"],
                })
                continue
            rows.append(_brief_row(data))
    total = len(rows)
    return {
        "ok": True,
        "count": total,
        "returned": len(rows[:size]),
        "limit": size,
        "loadErrors": load_errors,
        "reports": rows[:size],
        "hasMore": len(rows) > size,
        "dir": str(root),
    }


def _brief_row(data: dict[str, Any]) -> dict[str, Any]:
    """列表只给索引信息：traceback 不进列表（一次几十条正文会把回话撑爆，也没人读）。"""
    return {
        "reportId": str(data.get("reportId") or ""),
        "atMs": int(data.get("atMs") or 0),
        "atText": str(data.get("atText") or ""),
        "category": str(data.get("category") or ""),
        "excType": str(data.get("excType") or ""),
        "excText": str(data.get("excText") or "")[:200],
        "note": str(data.get("note") or "")[:MAX_NOTE_CHARS],
        "times": int(data.get("times") or 1),
        "traceBytes": int(data.get("traceBytes") or 0),
        "traceSha256": str(data.get("traceSha256") or ""),
        "truncated": bool(data.get("traceTruncated")),
        "redactedKinds": (data.get("redacted") or {}).get("kinds") or [],
        "context": data.get("context") or {},
        "exportedAtMs": int(data.get("exportedAtMs") or 0),
    }


def detail(report_id: str, *, include_traceback: bool = True) -> dict[str, Any]:
    path = _report_path(report_id)
    if path is None or not path.exists():
        return {"ok": False, "reason": f"没有这份崩溃现场：{str(report_id)[:80]}（可能被清理掉了，或编号不完整）"}
    data = _read_report(path)
    if data.get("_loadError"):
        return {"ok": False, "reason": f"这份现场读不出来了：{data['_loadError']}", "reportId": path.stem}
    row = _brief_row(data)
    row["ok"] = True
    row["runtime"] = data.get("runtime") or {}
    row["categoryDeclared"] = data.get("categoryDeclared") or ""
    if include_traceback:
        row["traceback"] = str(data.get("traceback") or "")
    return row


def export(report_id: str = "*") -> dict[str, Any]:
    """把现场导出成一份可附到问题报告里的文本；**不删原件**，导出物里已脱敏。

    `report_id` 给 `*`（缺省）导出全部；给具体编号只导出那一条。
    """
    root = reports_dir()
    wanted: list[dict[str, Any]] = []
    unreadable: list[str] = []
    if not root.exists():
        return {"ok": False, "reason": "本机还没有任何崩溃现场，没有可导出的东西"}
    if str(report_id or "*").strip() == "*":
        for path in sorted(root.glob(f"*{REPORT_SUFFIX}"), key=lambda p: p.name):
            data = _read_report(path)
            if data.get("_loadError"):
                unreadable.append(path.stem)
                continue
            wanted.append(data)
        if not wanted:
            return {
                "ok": False,
                "reason": f"现场目录里有 {len(unreadable)} 个文件但都读不出来（已改名让路，见崩溃列表的 loadErrors）",
                "unreadable": unreadable,
            }
    else:
        path = _report_path(report_id)
        if path is None or not path.exists():
            return {"ok": False, "reason": f"没有这份崩溃现场：{str(report_id)[:80]}"}
        data = _read_report(path)
        if data.get("_loadError"):
            return {"ok": False, "reason": f"这份现场读不出来：{data['_loadError']}"}
        wanted = [data]
    stamp = _now_ms()
    text = _bundle_text(wanted, stamp)
    # 名字带到毫秒：连点两次「导出」如果落在同一秒里，只到秒的名字会撞车，
    # 后一份把前一份覆盖掉——用户以为导出了两次，盘上只剩一份。
    name = (
        f"{EXPORT_PREFIX}{time.strftime('%Y%m%d-%H%M%S', time.gmtime(stamp / 1000.0))}"
        f"-{stamp % 1000:03d}.txt"
    )
    target = exports_dir() / name
    if target.exists():
        target = exports_dir() / f"{EXPORT_PREFIX}{stamp}.txt"
    _atomic_write(target, text)
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    with _lock:
        for item in wanted:
            rid = str(item.get("reportId") or "")
            path = _report_path(rid)
            if path is None or not path.exists():
                continue
            data = _read_report(path)
            if data.get("_loadError"):
                continue
            data["exportedAtMs"] = stamp
            _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return {
        "ok": True,
        "path": str(target),
        "bytes": len(encoded),
        "sha256": digest[:16],
        "reports": [str(item.get("reportId") or "") for item in wanted],
        "count": len(wanted),
        "note": (
            "导出的是**副本**：原始现场仍在本地，要清掉请单独走删除。这份文本里的凭据形状"
            "在落盘前就已经打掉（每条记录的 redacted.kinds 写了打掉哪几类），"
            "发出去之前仍建议自己看一遍——只有你能判断某个路径是不是敏感。"
        ),
        "unreadable": unreadable,
    }


def _bundle_text(reports: list[dict[str, Any]], stamp: int) -> str:
    head = [
        "# 通天府引擎崩溃现场导出",
        f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp / 1000.0))}",
        f"# 现场条数：{len(reports)}",
        "# 说明：以下 traceback 已在写盘时打掉凭据形状（密钥、令牌、认证头、JWT、长随机串），"
        "并把家目录换成 ~、数据根换成 <数据根>。这不是原文，是一份改过的现场。",
        "",
    ]
    blocks: list[str] = []
    for item in reports:
        blocks.append(
            "\n".join(
                [
                    "=" * 72,
                    f"编号：{item.get('reportId')}",
                    f"时间：{item.get('atText')}（{item.get('atMs')}）",
                    f"类别：{item.get('category')}"
                    + (f"（原始声明：{item.get('categoryDeclared')}）" if item.get("categoryDeclared") not in ("", None, item.get("category")) else ""),
                    f"重复次数：{item.get('times')}",
                    f"异常类型：{item.get('excType')}",
                    f"异常文本：{item.get('excText')}",
                    f"记录者说：{item.get('note')}" if item.get("note") else "记录者说：（没有附加说明）",
                    f"栈字节数：{item.get('traceBytes')}" + ("（被截断过）" if item.get("traceTruncated") else ""),
                    f"栈指纹：{item.get('traceSha256')}",
                    f"脱敏：{'、'.join((item.get('redacted') or {}).get('kinds') or []) or '没有命中任何凭据形状'}",
                    f"上下文：{json.dumps(item.get('context') or {}, ensure_ascii=False)}",
                    f"运行环境：{json.dumps(item.get('runtime') or {}, ensure_ascii=False)}",
                    "-" * 72,
                    str(item.get("traceback") or "（没有栈文本）"),
                    "",
                ]
            )
        )
    return "\n".join(head) + "\n".join(blocks)


def delete(report_id: str) -> dict[str, Any]:
    path = _report_path(report_id)
    if path is None:
        return {"ok": False, "reason": f"编号不合法或越出崩溃现场目录：{str(report_id)[:80]}"}
    if not path.exists():
        return {"ok": False, "reason": f"没有这份崩溃现场：{str(report_id)[:80]}（可能已经删过）"}
    try:
        path.unlink()
    except OSError as exc:
        return {"ok": False, "reason": f"删不掉这份现场：{type(exc).__name__}：{str(exc)[:160]}"}
    return {"ok": True, "reportId": report_id, "removed": True}


def status() -> dict[str, Any]:
    """界面开场要的那一眼：崩过没有、几份、有没有等着导出的。"""
    root = reports_dir()
    rows = [p for p in (root.glob(f"*{REPORT_SUFFIX}") if root.exists() else []) if p.is_file()]
    pending = 0
    newest = 0
    for path in rows:
        data = _read_report(path)
        if data.get("_loadError"):
            pending += 1
            continue
        if not int(data.get("exportedAtMs") or 0):
            pending += 1
        newest = max(newest, int(data.get("atMs") or 0))
    return {
        "count": len(rows),
        "pendingExport": pending,
        "newestAtMs": newest,
        "dir": str(root),
        "note": (
            "本机没有崩溃现场记录" if not rows
            else f"有 {pending} 份现场还没导出：界面应当提示用户导出并附到问题报告里（这些只在本机，没有自动发出去过）"
        ),
    }


# ── 挂钩安装 ────────────────────────────────────────────────────

def _text_from_exception(exc_type: Any, exc_value: Any, exc_tb: Any) -> tuple[str, str, str]:
    try:
        tb_text = "".join(_traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception as exc:  # noqa: BLE001  format 自己炸了也不能丢现场
        tb_text = f"（栈文本生成失败：{type(exc).__name__}：{exc}）"
    return (
        getattr(exc_type, "__name__", str(exc_type or ""))[:120],
        str(exc_value or "")[:2000],
        tb_text,
    )


def handle_exception(
    category: str,
    exc_type: Any = None,
    exc_value: Any = None,
    exc_tb: Any = None,
    *,
    context: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """把一次异常落成现场；**永远不抛**（挂钩里再抛就是把现场彻底弄丢）。"""
    try:
        et, ev, tb = _text_from_exception(exc_type, exc_value, exc_tb)
        return record(category, exc_type=et, exc_text=ev, traceback_text=tb, context=context, note=note)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"现场处理失败：{type(exc).__name__}：{str(exc)[:160]}"}


def _main_hook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
    result = handle_exception("uncaught_exception", exc_type, exc_value, exc_tb)
    _previous("excepthook", exc_type, exc_value, exc_tb)
    if not result.get("ok"):
        sys.stderr.write(f"[engine] 崩溃现场未能落盘：{result.get('reason')}\n")


def _thread_hook(args: Any) -> None:
    result = handle_exception(
        "worker_thread",
        getattr(args, "exc_type", None),
        getattr(args, "exc_value", None),
        getattr(args, "exc_traceback", None),
        context={"lane": getattr(getattr(args, "thread", None), "name", "")[:60]},
    )
    if not result.get("ok"):
        sys.stderr.write(f"[engine] 工作线程崩溃现场未能落盘：{result.get('reason')}\n")


def loop_exception_handler(_loop: Any, context: dict[str, Any]) -> None:
    """asyncio 循环回调里抛出来的异常：默认只印一行，这里补成现场。

    循环给的 `message` 走 `note` 而不是 `context`：上下文字段表是「哪个任务哪条车道」，
    把框架自己那句话塞进去会让白名单失去意义（那张表同时是遥测的口径）。
    """
    exc = None
    if isinstance(context, dict):
        exc = context.get("exception")
    if exc is not None:
        handle_exception(
            "event_loop",
            type(exc),
            exc,
            exc.__traceback__,
            note=str(context.get("message") or ""),
        )
        return
    # 没有 exception 对象的循环告警（比如 slow callback）不是崩溃，不落现场
    sys.stderr.write(f"[engine] 事件循环告警：{str((context or {}).get('message') or '')[:200]}\n")


def _previous(name: str, *args: Any) -> None:
    """把异常继续交给原来的钩子：接管不等于吞掉。"""
    hook = _STATE["previous"].get(name)
    if hook is None:
        return
    try:
        if name == "excepthook" and args:
            hook(*args)
    except Exception:  # noqa: BLE001
        pass


def install(*, engine_version: str = "", loop: Any = None) -> dict[str, Any]:
    """挂上主线程 / 工作线程 / 事件循环三个钩子；**幂等**（重复调用只补新对象）。

    `loop` 只在能拿到循环对象时接（uvicorn 的循环要在 lifespan 里才有），
    拿不到就只装前两个，状态面如实说明「循环钩子未装」。
    """
    installed: list[str] = []
    try:
        if not _STATE["installed"]:
            _STATE["previous"]["excepthook"] = sys.excepthook
            sys.excepthook = _main_hook
            threading.excepthook = _thread_hook
            _STATE["installed"] = True
        installed.append("sys.excepthook")
        installed.append("threading.excepthook")
        _STATE["engineVersion"] = str(engine_version or "")[:40]
        if loop is not None and hasattr(loop, "set_exception_handler"):
            try:
                loop.set_exception_handler(loop_exception_handler)
                installed.append("loop.set_exception_handler")
                _STATE["loopInstalled"] = True
                _STATE["loopError"] = ""
            except Exception as exc:  # noqa: BLE001
                # 必须把上一轮的成功清掉：换循环对象时如果这次没接上，读数还写着「已装」
                # 就是在对界面撒谎（而界面正是要靠这句话决定要不要提示用户重启）。
                _STATE["loopInstalled"] = False
                _STATE["loopError"] = f"{type(exc).__name__}：{str(exc)[:160]}"
    except Exception as exc:  # noqa: BLE001  挂钩本身失败也不能影响启动
        _STATE["installError"] = f"{type(exc).__name__}：{str(exc)[:200]}"
    return {
        "ok": not _STATE["installError"],
        "installed": installed,
        "engineVersion": _STATE.get("engineVersion", ""),
        "loopHook": bool(_STATE.get("loopInstalled")),
        "loopError": _STATE.get("loopError", ""),
        "installError": _STATE.get("installError", ""),
        "note": (
            "崩溃现场只写在本机数据根下，不会自动发出去；导出是显式动作"
            if not _STATE["installError"]
            else f"钩子没装上：{_STATE['installError']}（引擎照旧启动，但崩了不会留现场）"
        ),
    }


def installed_state() -> dict[str, Any]:
    return {
        "sysExcepthook": _STATE["installed"],
        "loopHandler": bool(_STATE.get("loopInstalled")),
        "installError": _STATE.get("installError", ""),
    }


def describe() -> dict[str, Any]:
    """状态面的一眼：目录、上限、规则名与钩子状态（不含任何现场正文）。"""
    return {
        "ok": True,
        "dir": str(crash_home()),
        "maxReports": MAX_REPORTS,
        "maxTracebackBytes": MAX_TRACEBACK_BYTES,
        "maxNoteChars": MAX_NOTE_CHARS,
        "dedupeWindowMs": DEDUPE_WINDOW_MS,
        "dedupeLookback": DEDUPE_LOOKBACK,
        "categories": list(CATEGORIES),
        "contextFields": list(CONTEXT_FIELDS),
        "redactionKinds": list(REDACTION_KINDS),
        "hooks": installed_state(),
        "recorder": recorder_state(),
        "note": "现场只在本机；导出是显式动作，导出物是副本且已脱敏",
    }


def reset_for_tests() -> None:
    """自测隔离用：把钩子与观察者状态清空（不删盘上现场，那由用例自己指到临时数据根）。"""
    with _lock:
        _STATE["installed"] = False
        _STATE["previous"] = {}
        _STATE["installError"] = ""
        _STATE.pop("loopInstalled", None)
        _STATE.pop("loopError", None)
        _STATE.pop("engineVersion", None)
        _OBSERVER["callback"] = None
        _OBSERVER["notified"] = 0
        _OBSERVER["error"] = ""
