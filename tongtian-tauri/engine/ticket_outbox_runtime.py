"""工单外发的耐久副作用队列（缺陷 D-48.2）

Agent 侧的一切外发——启动 worker、把输入交付给会话、中继消息、通知人、回复
会话——都不能「边想边发」：进程在事务提交前崩掉，重跑时这些动作要么丢、要么
重复。本模块把外发变成**耐久命令**：在事务内入队、**提交之后才执行**；每条命令
带幂等键；同一工单发往同一会话严格保序；每个 kind 各有并发上限与指数退避；
执行租约超时会被回收——这套纪律复用网关审计 outbox（M5.5）的幂等与租约思路，
不重写第二套。

三条硬口径（与 D-47/D-48.1 同一套精神：拒绝与失败都要给出「为什么」）：

1. **提交后才执行**：`enqueue` 只挂进事务（status=queued），`commit(tx)` 之后才
   可能外发；`rollback(tx)` 直接作废——回滚的事务永远发不出去，这条钉死。
   未知 tx 拒绝入队，防止「忘了 begin 就直接发」。
2. **幂等**：每条命令有 dedupeKey（缺省由 kind+工单+会话+载荷摘要导出）。同键
   已有**活记录**（queued/pending/running/done）时入队返回既有记录、绝不重排——
   重启后重放同一个 enqueue 不会二次发送；只有 aborted/failed 之后允许显式重排
   （那是人决定的重发，不是意外重复）。
3. **同工单同会话严格保序**：按会话投递类 kind（deliver_input / relay / reply）
   在同一 `(ticketId, sessionKey)` 组内按入队顺序**队头放行**——前一条没发成功，
   后面的不许越序插队；前一条永久失败（failed）时整组停住并如实报告卡点，
   由 `retry_failed` 重投或 `resolve_block(skip)` 明确跳过，**不静默越过**。
   不按会话投递的 kind（launch / notify）不参与组内保序，各自按 kind 并发。

存储：单 JSON 账本（`data_paths.ticket_outbox_file()`，D-43 单一来源），原子替换
落盘；done/aborted/skipped 超过保留上限剪枝，failed 永远保留（那是没发出去的
证据）。账本损坏时改名让路并暴露 loadError——绝不静默丢待发命令，也绝不在坏
文件上继续追加。

边界：本模块不执行真实外发——dispatch 由调用方注入（引擎接线在后续切片）；
不发 HTTP、不起线程；`run_pending` 是同步顺序调度，kind 并发上限表达为「同一
时刻最多几条在途」，供接线侧在多轮调用与崩溃恢复语义里消费。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import data_paths as dp

OUTBOX_VERSION = 1

#: 全部合法 kind；不在表里的 kind 直接拒绝，防「先记下来再说」的假耐久
KIND_LAUNCH = "launch"
KIND_DELIVER_INPUT = "deliver_input"
KIND_RELAY = "relay"
KIND_NOTIFY = "notify"
KIND_REPLY = "reply"
KNOWN_KINDS = (KIND_LAUNCH, KIND_DELIVER_INPUT, KIND_RELAY, KIND_NOTIFY, KIND_REPLY)

#: 按会话投递、必须组内保序的 kind
ORDERED_KINDS = frozenset({KIND_DELIVER_INPUT, KIND_RELAY, KIND_REPLY})

#: 缺省 kind 并发上限（同一时刻最多几条在途）
DEFAULT_KIND_LIMITS: dict[str, int] = {
    KIND_LAUNCH: 2,
    KIND_DELIVER_INPUT: 1,
    KIND_RELAY: 2,
    KIND_NOTIFY: 3,
    KIND_REPLY: 2,
}

#: 状态集合：queued 未提交永不执行；pending 已提交待发；running 在途（带租约）；
#: done/failed/aborted/skipped 为终态
_LIVE_STATUSES = ("queued", "pending", "running", "done")
_TERMINAL_STATUSES = ("done", "failed", "aborted", "skipped")

DEFAULT_BASE_BACKOFF_SECONDS = 5.0
DEFAULT_BACKOFF_CAP_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 5
#: 在途租约超时：崩溃遗留的 running 会被回收重投（done 永不重放）
DEFAULT_LEASE_SECONDS = 120.0
#: done/aborted/skipped 的保留上限；failed 不受剪枝（证据不能丢）
MAX_RETAINED_FINISHED = 500
#: 单条载荷的字符上限：耐久队列不是第二存储，大材料应落盘后引用路径
MAX_PAYLOAD_CHARS = 20000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def derive_dedupe_key(kind: str, ticket_id: str, session_key: str, payload: Any) -> str:
    """载荷摘要幂等键：同样的话重放 enqueue 只会有一条命令。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"{kind}:{ticket_id}:{session_key}:{_digest(kind, ticket_id, session_key, canonical)}"


def canonical_payload(payload: Any) -> dict:
    """载荷落盘前规范化为 {data, text} 两形——与网关 outbox 命令载荷同思路，
    保序与幂等只依赖这个规范化结果。"""
    if payload is None:
        return {"data": None, "text": ""}
    if isinstance(payload, str):
        return {"data": None, "text": payload}
    return {"data": payload, "text": str(payload)}


class TicketOutbox:
    """工单耐久副作用队列。所有查询接口返回视图 dict，不改内部记录本身。

    clock 注入用于退避与租约的确定性测试；线程安全走一把全局锁（队列本身是
    单文件账本，粒度到记录级锁没有意义）。"""

    def __init__(
        self,
        path: Path | None = None,
        *,
        kind_limits: dict[str, int] | None = None,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path) if path is not None else dp.ticket_outbox_file()
        self.kind_limits = dict(DEFAULT_KIND_LIMITS)
        if kind_limits:
            unknown = [k for k in kind_limits if k not in KNOWN_KINDS]
            if unknown:
                raise ValueError(f"未知的 kind：{'、'.join(unknown)}——只认 {'/'.join(KNOWN_KINDS)}")
            self.kind_limits.update(kind_limits)
        self.base_backoff_seconds = base_backoff_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._txs: set[str] = set()
        self._seq = 0
        self._records: list[dict[str, Any]] = []
        self.load_error = ""
        self._load()

    # ── 账本读写 ──────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or int(raw.get("version") or 0) != OUTBOX_VERSION:
                raise ValueError("版本不符或结构不是对象")
            records = raw.get("records")
            if not isinstance(records, list) or not all(isinstance(r, dict) and r.get("id") for r in records):
                raise ValueError("records 结构损坏")
            self._seq = int(raw.get("seq") or 0)
            self._records = records
            # 事务集合不跨重启：重启后还挂着 queued 的记录说明提交没发生，
            # 它们按「未提交」作废——耐久语义宁可丢未提交的，不发未提交的。
            for record in self._records:
                if record.get("status") == "queued":
                    record["status"] = "aborted"
                    record["error"] = "进程重启时事务未提交，按未提交作废（重发请开新事务）"
        except Exception as exc:  # noqa: BLE001  坏账本改名让路，不静默也不卡死
            stamp = int(self._clock() * 1000)
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                self.path.replace(aside)
                self.load_error = f"账本损坏，已改名保留 {aside.name} 并从空队列起步：{exc}"
            except OSError as move_exc:
                self.load_error = f"账本损坏且无法移开（原地拒写，请先人工处理）：{exc} / {move_exc}"

    def _save(self) -> None:
        self._prune()
        data = {"version": OUTBOX_VERSION, "seq": self._seq, "records": self._records}
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) > 8_000_000:
            raise RuntimeError("副作用账本超过 8M 上限——请清理已完成记录或缩小载荷")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp-{self._clock()}-{len(self._records)}")
        tmp.write_text(encoded, encoding="utf-8")
        import os

        os.replace(tmp, self.path)

    def _prune(self) -> None:
        keep = [r for r in self._records if r.get("status") not in ("done", "aborted", "skipped")]
        finished = [r for r in self._records if r.get("status") in ("done", "aborted", "skipped")]
        overflow = len(finished) - MAX_RETAINED_FINISHED
        # 只有超过保留上限才剪最旧的；未超限时终态记录必须原样留着——
        # done 记录是幂等的凭据，剪光了重启重放就会二次发送。
        keep.extend(finished[overflow:] if overflow > 0 else finished)
        keep.sort(key=lambda r: int(r.get("seq") or 0))
        self._records = keep

    def _find(self, record_id: str) -> dict[str, Any] | None:
        for record in self._records:
            if record.get("id") == record_id:
                return record
        return None

    # ── 事务面：begin / enqueue / commit / rollback ───────────

    def begin(self, tx_id: str) -> dict[str, Any]:
        tx = str(tx_id or "").strip()
        if not tx:
            return {"ok": False, "reason": "事务 id 不能为空"}
        with self._lock:
            if tx in self._txs:
                return {"ok": False, "reason": f"事务「{tx}」已存在——先 commit/rollback 再复用名字"}
            self._txs.add(tx)
        return {"ok": True, "txId": tx}

    def enqueue(
        self,
        *,
        kind: str,
        ticket_id: str,
        session_key: str = "",
        payload: Any = None,
        tx_id: str,
        dedupe_key: str = "",
    ) -> dict[str, Any]:
        if kind not in KNOWN_KINDS:
            return {"ok": False, "reason": f"未知的 kind「{kind}」——只认 {'/'.join(KNOWN_KINDS)}，不在表里的动作不许先记账"}
        tid = str(ticket_id or "").strip()
        if not tid:
            return {"ok": False, "reason": "ticketId 为空——耐久命令必须知道是哪个工单发的"}
        tx = str(tx_id or "").strip()
        if not tx:
            return {"ok": False, "reason": "txId 必填：外发必须在事务内入队、提交之后才执行（忘了 begin 就不该发出去）"}
        body = canonical_payload(payload)
        if len(body["text"]) > MAX_PAYLOAD_CHARS:
            return {"ok": False, "reason": f"载荷超过 {MAX_PAYLOAD_CHARS} 字符上限——大材料请落盘后引用路径，别把耐久队列当第二存储"}
        key = str(dedupe_key or "").strip() or derive_dedupe_key(kind, tid, str(session_key or ""), body)
        with self._lock:
            if tx not in self._txs:
                return {"ok": False, "reason": f"未知事务「{tx}」——先 begin 再 enqueue，回滚过的名字也不行"}
            for record in self._records:
                if record.get("dedupeKey") == key and record.get("status") in _LIVE_STATUSES:
                    return {
                        "ok": True,
                        "deduped": True,
                        "record": dict(record),
                        "note": "同幂等键已有活记录：这条外发不会重排（重启后重放 enqueue 正是靠这条不二次发送）",
                    }
            self._seq += 1
            record = {
                "id": f"out-{self._seq}",
                "seq": self._seq,
                "kind": kind,
                "ticketId": tid,
                "sessionKey": str(session_key or ""),
                "payload": body,
                "txId": tx,
                "dedupeKey": key,
                "status": "queued",
                "attempts": 0,
                "error": "",
                "blockedBy": "",
                "createdAtMs": _now_ms(),
                "updatedAtMs": _now_ms(),
                "nextAttemptAtMs": 0,
                "acquiredAtMs": 0,
            }
            self._records.append(record)
            self._save()
        return {"ok": True, "deduped": False, "record": dict(record)}

    def commit(self, tx_id: str) -> dict[str, Any]:
        tx = str(tx_id or "").strip()
        with self._lock:
            if tx not in self._txs:
                return {"ok": False, "reason": f"未知事务「{tx}」，没有可提交的命令"}
            self._txs.discard(tx)
            released = 0
            now = self._sched_ms()
            for record in self._records:
                if record.get("txId") == tx and record.get("status") == "queued":
                    record["status"] = "pending"
                    record["nextAttemptAtMs"] = now
                    record["updatedAtMs"] = now
                    released += 1
            self._save()
        return {"ok": True, "txId": tx, "released": released}

    def rollback(self, tx_id: str) -> dict[str, Any]:
        tx = str(tx_id or "").strip()
        with self._lock:
            if tx not in self._txs:
                return {"ok": False, "reason": f"未知事务「{tx}」"}
            self._txs.discard(tx)
            aborted = 0
            for record in self._records:
                if record.get("txId") == tx and record.get("status") == "queued":
                    record["status"] = "aborted"
                    record["error"] = "事务回滚：这条外发从未授权执行，永久作废"
                    record["updatedAtMs"] = _now_ms()
                    aborted += 1
            self._save()
        return {"ok": True, "txId": tx, "aborted": aborted}

    # ── 执行面：队头放行、kind 并发、退避、租约 ───────────────

    def _touch(self, record: dict[str, Any], **fields: Any) -> None:
        record.update(fields)
        record["updatedAtMs"] = _now_ms()

    def _sched_ms(self) -> int:
        """调度时间基准：必须与 run_pending 的时钟同源（可注入），否则测试与
        未来接入统一时钟时，nextAttemptAtMs 会和比较用的 now 两套历法。"""
        return int(self._clock() * 1000)

    def _inflight_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._records:
            if record.get("status") == "running":
                counts[record["kind"]] = counts.get(record["kind"], 0) + 1
        return counts

    def _ordered_blocker(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """组内是否有前序还没了结：有则返回第一条这样的记录（队头纪律）。"""
        group = (record["ticketId"], record["sessionKey"])
        for other in self._records:
            if other is record or other.get("seq", 0) >= record["seq"]:
                continue
            if other.get("txId") and other.get("status") == "queued":
                continue  # 未提交的不在序上（可能回滚），不挡队头
            if other["kind"] not in ORDERED_KINDS:
                continue
            if (other["ticketId"], other["sessionKey"]) != group:
                continue
            if other["status"] in ("pending", "running", "failed"):
                return other
        return None

    def _reclaim_leases(self, now_ms: int) -> int:
        """崩溃遗留的在途记录回收：租约超时 → pending 重投（带幂等键，dispatch
        侧仍应自证一次；done 永不重放）。"""
        reclaimed = 0
        for record in self._records:
            if record.get("status") != "running":
                continue
            if now_ms - int(record.get("acquiredAtMs") or 0) >= self.lease_seconds * 1000:
                self._touch(
                    record,
                    status="pending",
                    error=f"在途超过 {int(self.lease_seconds)} 秒未见结果，按租约回收重投",
                    nextAttemptAtMs=now_ms,
                )
                reclaimed += 1
        return reclaimed

    def run_pending(self, dispatch: Callable[[dict[str, Any]], Any], *, limit: int = 16) -> dict[str, Any]:
        """同步顺序执行一轮：回收过期租约 → 按队头/并发/退避放行 → 调 dispatch。

        dispatch 返回真值视为成功（done）；返回假值或抛错记一次失败：退避重投，
        到 max_attempts 置 failed 终态并停住所在保序组（如实报告，不静默越过）。"""
        summary = {"dispatched": 0, "succeeded": 0, "failed": 0, "reclaimed": 0, "blocked": 0, "reasons": []}
        with self._lock:
            if self.load_error and not self.path.exists():
                summary["reasons"].append(f"账本状态：{self.load_error}")
            now_ms = int(self._clock() * 1000)
            summary["reclaimed"] = self._reclaim_leases(now_ms)
            if summary["reclaimed"]:
                self._save()  # 回收即落账：就算本轮没有可放行的记录，崩溃证据也已处置
            inflight = self._inflight_by_kind()
            candidates = sorted(
                (r for r in self._records if r.get("status") == "pending" and int(r.get("nextAttemptAtMs") or 0) <= now_ms),
                key=lambda r: r["seq"],
            )
            for record in candidates:
                if summary["dispatched"] >= limit:
                    break
                kind = record["kind"]
                if inflight.get(kind, 0) >= self.kind_limits.get(kind, 1):
                    summary["blocked"] += 1
                    continue
                if kind in ORDERED_KINDS:
                    blocker = self._ordered_blocker(record)
                    if blocker is not None:
                        self._touch(record, blockedBy=str(blocker["id"]))
                        summary["blocked"] += 1
                        if blocker["status"] == "failed":
                            summary["reasons"].append(
                                f"{record['id']} 停在组 ({record['ticketId']}, {record['sessionKey']})："
                                f"前序 {blocker['id']} 已永久失败——用 retry_failed 重投或 resolve_block 跳过，不越序"
                            )
                        continue
                record["blockedBy"] = ""
                self._touch(record, status="running", acquiredAtMs=now_ms)
                self._save()
                summary["dispatched"] += 1
                # 本轮占坑不回收：同步执行里 dispatch 返回即完成，但如果当场减回，
                # 「kind 并发上限」就退化成「不限量」——上限的语义是按轮放行，
                # 与崩溃遗留的 running 一起计入在途。
                inflight[kind] = inflight.get(kind, 0) + 1
                try:
                    ok = bool(dispatch(dict(record)))
                    error = "" if ok else "dispatch 返回失败"
                except Exception as exc:  # noqa: BLE001  外发炸了要记在账上，不能带崩队列
                    ok = False
                    error = f"{type(exc).__name__}: {exc}"
                attempts = int(record.get("attempts") or 0) + 1
                if ok:
                    self._touch(record, status="done", error="", nextAttemptAtMs=0)
                    summary["succeeded"] += 1
                elif attempts >= self.max_attempts:
                    self._touch(record, status="failed", error=f"第 {attempts} 次仍失败：{error}", attempts=attempts)
                    summary["failed"] += 1
                    summary["reasons"].append(f"{record['id']} 到 {self.max_attempts} 次上限置 failed（保序组已停住，见账本）")
                else:
                    delay = min(self.base_backoff_seconds * (2 ** (attempts - 1)), self.backoff_cap_seconds)
                    self._touch(
                        record,
                        status="pending",
                        attempts=attempts,
                        error=error,
                        nextAttemptAtMs=now_ms + int(delay * 1000),
                    )
                    summary["failed"] += 1
                self._save()
        return summary

    # ── 逃生口：重投与显式跳过（都要留痕） ────────────────────

    def retry_failed(self, record_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._find(str(record_id))
            if record is None:
                return {"ok": False, "reason": f"找不到记录「{record_id}」"}
            if record["status"] != "failed":
                return {"ok": False, "reason": f"「{record_id}」当前是 {record['status']}，只有 failed 可重投"}
            self._touch(
                record,
                status="pending",
                attempts=0,
                error="",
                nextAttemptAtMs=self._sched_ms(),
            )
            record["retries"] = int(record.get("retries") or 0) + 1
            self._save()
            return {"ok": True, "record": dict(record), "note": "failed 重投已登记（这是人决定的重发，与幂等重放不同）"}

    def resolve_block(self, record_id: str, action: str) -> dict[str, Any]:
        """卡住保序组的 failed 前序的处置：action="skip" 显式跳过（承认这条没发出去，
        放行后序）；重投请走 retry_failed。只有这两种，没有第三种「悄悄越过」。"""
        if action != "skip":
            return {"ok": False, "reason": f"未知处置「{action}」——组卡点只有 skip（跳过）或 retry_failed（重投）"}
        with self._lock:
            record = self._find(str(record_id))
            if record is None:
                return {"ok": False, "reason": f"找不到记录「{record_id}」"}
            if record["status"] != "failed":
                return {"ok": False, "reason": f"「{record_id}」当前是 {record['status']}，只有 failed 可跳过"}
            self._touch(record, status="skipped", error="人已显式跳过：这条外发承认没发出去，后序放行")
            for later in self._records:
                if (
                    later.get("status") == "pending"
                    and later["kind"] in ORDERED_KINDS
                    and later.get("blockedBy") == record["id"]
                ):
                    later["blockedBy"] = ""
            self._save()
            return {"ok": True, "record": dict(record)}

    # ── 查询面 ────────────────────────────────────────────────

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records if r.get("status") in ("pending", "running"))

    def record_view(self, record_id: str) -> dict | None:
        with self._lock:
            record = self._find(str(record_id))
            return dict(record) if record else None

    def inflight_for(self, ticket_id: str) -> dict[str, Any]:
        """恢复注记的输入：这个工单当前「可能已发出待确认」（running）与「未提交」
        （queued）的外发——崩溃重启后先拿这份证据，才谈是否重发。"""
        with self._lock:
            running = [
                str(r["id"])
                for r in self._records
                if r.get("status") == "running" and r.get("ticketId") == str(ticket_id)
            ]
            queued = [
                str(r["kind"])
                for r in self._records
                if r.get("status") == "queued" and r.get("ticketId") == str(ticket_id)
            ]
            return {"runningIds": running, "queuedKinds": queued}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for record in self._records:
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            groups: dict[str, list[dict[str, Any]]] = {}
            for record in self._records:
                if record["kind"] in ORDERED_KINDS and record["status"] in ("pending", "running", "failed"):
                    groups.setdefault(f"{record['ticketId']}::{record['sessionKey']}", []).append(dict(record))
            return {
                "version": OUTBOX_VERSION,
                "path": str(self.path),
                "records": len(self._records),
                "statusCounts": counts,
                "kindLimits": dict(self.kind_limits),
                "openTx": sorted(self._txs),
                "orderedGroups": groups,
                "loadError": self.load_error,
            }
