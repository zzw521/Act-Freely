"""模型用量台账的落盘机制与调用来源标记（D-43）。

为什么要这两件事：

1. 台账只在进程内字典累计时，引擎重启（崩溃、升级、服务化滚动发布）就把用量清零，
   配额硬停与计费失去依据。这里提供「追加式增量日志 + 定期快照合并」的落盘文件，
   启动时重放即可恢复，且只追加不改写存量行，符合「表结构只做增量变更」的存储约束。
2. 同一份用量可能同时被桌面（Rust `model_usage` 表）和引擎台账记账。合并视图必须
   能分辨「这条引擎样本桌面已经记过」，否则要么重复计数，要么静默丢数据。因此每次
   采样都带一个来源标记，只有明确标记为 `desktop` 的样本才会在与桌面台账合并时被排除。

文件位置：`{engine_home()}/usage/model-usage.jsonl`（见 `data_paths.py`）。
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from pathlib import Path

from data_paths import usage_ledger_file

# 落盘行格式版本：字段含义变化时递增，重放时按版本兼容
LEDGER_SCHEMA_VERSION = 1

# 增量行累计到该数量后合并为快照行，避免文件无界增长拖慢启动重放
COMPACT_AFTER_DELTAS = 20_000

# 落盘总开关（置 0/false 时只保内存台账，供只读介质或排障使用）
PERSIST_ENV = "TT_ENGINE_USAGE_PERSIST"

KIND_DELTA = "delta"
KIND_MERGED = "merged"

# ── 调用来源 ─────────────────────────────────────────────────────

ORIGIN_DESKTOP = "desktop"
ORIGIN_WEB = "web"
ORIGIN_IM = "im"
ORIGIN_CLI = "cli"
ORIGIN_SCHEDULE = "schedule"
# 经模型网关的调用：网关自己会上报同一批用量，合并视图据此排除引擎这份，避免重复计数
ORIGIN_GATEWAY = "gateway"
ORIGIN_UNKNOWN = "unknown"

USAGE_ORIGINS = (
    ORIGIN_DESKTOP,
    ORIGIN_WEB,
    ORIGIN_IM,
    ORIGIN_CLI,
    ORIGIN_SCHEDULE,
    ORIGIN_GATEWAY,
    ORIGIN_UNKNOWN,
)

# 请求头：客户端显式声明自己是谁；桌面 Rust 桥未带头时按默认来源处理
ORIGIN_HEADER = "x-tt-client"

# 未带来源标记时的归属（值同白名单：desktop / web / im / cli / schedule / gateway）
DEFAULT_ORIGIN_ENV = "TT_ENGINE_USAGE_ORIGIN_DEFAULT"

_ORIGIN_ALIASES = {"tauri": ORIGIN_DESKTOP, "desktop": ORIGIN_DESKTOP, "browser": ORIGIN_WEB}


def normalize_origin(raw: object) -> str:
    """把外部传入的来源值收敛到白名单；无法识别按 unknown（宁可多计不漏计）。"""
    value = str(raw or "").strip().lower()
    if not value:
        return ORIGIN_UNKNOWN
    return _ORIGIN_ALIASES.get(value, value if value in USAGE_ORIGINS else ORIGIN_UNKNOWN)


def default_origin() -> str:
    """未标记请求的归属：部署方显式声明，缺省 unknown（合并视图会单独披露这一部分）。"""
    return normalize_origin(os.environ.get(DEFAULT_ORIGIN_ENV))


_CURRENT_USAGE_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tt_engine_usage_origin", default=""
)


def usage_origin() -> str:
    return normalize_origin(_CURRENT_USAGE_ORIGIN.get() or default_origin())


def set_usage_origin(origin: object) -> contextvars.Token:
    """为空值时不改写归属，让本次调用继续继承默认来源（而不是被压成 unknown）。"""
    raw = str(origin or "").strip()
    return _CURRENT_USAGE_ORIGIN.set(normalize_origin(raw) if raw else "")


def reset_usage_origin(token: contextvars.Token) -> None:
    _CURRENT_USAGE_ORIGIN.reset(token)


# ── 追加式台账文件 ────────────────────────────────────────────────


class UsageDeltaLog:
    """用量台账文件读写：只追加、按版本重放、超限合并快照。

    落盘失败绝不向上抛：模型调用链不能因为磁盘不可写而中断，但失败必须留下可见痕迹
    （`status()` 里的 `lastError`），否则运维会以为用量已经持久化。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.deltas_since_compact = 0
        self.last_error = ""
        self.last_error_at_ms = 0

    def enabled(self) -> bool:
        raw = str(os.environ.get(PERSIST_ENV) or "").strip().lower()
        return raw not in ("0", "false", "off", "no")

    def path(self) -> Path:
        return usage_ledger_file()

    def append(self, key: dict, delta: dict) -> bool:
        """追加一条增量；返回是否落盘成功（内存台账始终已更新）。"""
        line = {
            "v": LEDGER_SCHEMA_VERSION,
            "kind": KIND_DELTA,
            "atMs": int(time.time() * 1000),
            "key": key,
            "d": delta,
        }
        return self._write_line(line)

    def _write_line(self, line: dict) -> bool:
        if not self.enabled():
            return False
        try:
            payload = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            self._note_error(f"台账序列化失败：{exc}")
            return False
        path = self.path()
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(payload + "\n")
            except OSError as exc:
                self._note_error(f"台账写入失败：{exc}")
                return False
            self.deltas_since_compact += 1
        return True

    def read(self) -> tuple[list[dict], int, str]:
        """重放文件：返回 (合法行, 丢弃行数, 读取错误)。文件缺失视为空台账。"""
        path = self.path()
        try:
            if not path.exists():
                return [], 0, ""
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            message = f"台账读取失败：{exc}"
            self._note_error(message)
            return [], 0, message
        lines: list[dict] = []
        drops = 0
        for raw in text.splitlines():
            item = raw.strip()
            if not item:
                continue
            try:
                parsed = json.loads(item)
            except ValueError:
                drops += 1
                continue
            if not isinstance(parsed, dict) or parsed.get("kind") not in (KIND_DELTA, KIND_MERGED):
                drops += 1
                continue
            if int(parsed.get("v") or 0) > LEDGER_SCHEMA_VERSION:
                # 新版本的行只可能来自升级后的引擎：跳过而不是崩溃，旧版仍能读到自己的行
                drops += 1
                continue
            lines.append(parsed)
        self.deltas_since_compact = sum(1 for line in lines if line.get("kind") == KIND_DELTA)
        return lines, drops, ""

    def write_snapshot(self, rows: list[tuple[dict, dict]]) -> bool:
        """把当前累计态压成快照行整体重写（原子替换）：保留结果，丢弃增量过程。"""
        if not self.enabled():
            return False
        path = self.path()
        stamp = int(time.time() * 1000)
        body = "".join(
            json.dumps(
                {"v": LEDGER_SCHEMA_VERSION, "kind": KIND_MERGED, "atMs": stamp, "key": key, "row": row},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for key, row in rows
        )
        tmp = path.parent / f"{path.name}.tmp-{stamp}"
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, path)
            except OSError as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._note_error(f"台账合并失败：{exc}")
                return False
            self.deltas_since_compact = 0
        return True

    def should_compact(self) -> bool:
        return self.deltas_since_compact >= COMPACT_AFTER_DELTAS

    def _note_error(self, message: str) -> None:
        self.last_error = message
        self.last_error_at_ms = int(time.time() * 1000)

    def status(self, *, loaded: bool, rows: int, drops: int) -> dict:
        """对外披露落盘真实状态：不能只在内存里累计却声称已持久化。"""
        return {
            "schemaVersion": LEDGER_SCHEMA_VERSION,
            "enabled": self.enabled(),
            "file": str(self.path()),
            "loaded": loaded,
            "rows": rows,
            "pendingDeltas": self.deltas_since_compact,
            "droppedLines": drops,
            "lastError": self.last_error,
            "lastErrorAtMs": self.last_error_at_ms,
        }
