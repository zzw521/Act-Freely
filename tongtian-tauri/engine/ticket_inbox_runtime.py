"""工单唤醒的耐久收件箱（缺陷 D-48.6）

D-48.5g 把「哪几条唤醒从没进过会话历史」做成了可查的——但那份核对**完全建立在调用方给的
候选之上**：服务端消息表说有哪些，引擎才知道有哪些。原因很实在：唤醒投进 `worker.inbox`
之后只活在内存里，进程一没，「引擎收到过但没有进会话」这批事实就整批蒸发，连到达顺序都拿不
回来。本模块把这一批落到盘上，只记一件事：

**投递过、还没被喂进本轮会话**。

它不是已读账本的副本（那份记「进过会话」，这份记「还没进」），也不是副作用队列（那份是
对外动作的事务，这里没有任何对外动作）。销账的唯一时机是回合把这批唤醒喂进提示词并落账
为已读之后——所以「回合喂进去了但 mark_read 之前崩了」会留下一条 pending，重启后被重投一次：
这与 D-48.5g 的取舍一致，**宁可重投一条也不丢唤醒**，真重复的外发由幂等键挡下。

边界与诚实面：

* 这份日志只认识**投进引擎之后**的唤醒。引擎停机期间到达的那几条，只有服务端消息表知道，
  补查的外部候选因此不会被淘汰，只是从「唯一来源」降级为「第二个来源」。
* 落盘失败**不拦投递**：唤醒照样进内存箱、照样起轮，但结果里带回 `journalWarning` 原话——
  这条唤醒此刻只有内存知道，重启就得靠调用方的候选。假装落了盘才是丢唤醒。
* 容量有硬上限（`MAX_RECORDS`）：超了就拒收并如实报，不静默丢最旧的那条——丢一条唤醒等于
  用户的话没被读到。
* 账本损坏不静默删：改名 `.corrupt-<stamp>` 让路，`loadError` 暴露出来，与 `ReadLedger` /
  `TicketOutbox` 同一套纪律。

纯 stdlib，不发 HTTP、不起进程、不调模型；路径走 D-43 的单一来源（`data_paths.ticket_inbox_file()`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import data_paths as dp

INBOX_VERSION = 1
#: 在箱条数上限：满了就拒收并如实报，不做「挤掉最旧一条」那种省事但丢唤醒的事。
MAX_RECORDS = 5000
#: 单条正文的落盘上限：唤醒是给人读的话，超长的多半是粘贴的日志；截断留痕，不整条拒收。
MAX_TEXT_CHARS = 20_000


def _digest(joined: str) -> str:
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:16]


def wake_journal_id(wake: dict) -> str:
    """一条投递的键：正文 + 到达时间 + **投递时那份领取**（直接从唤醒字典算）。

    输入形状与 `ticket_dispatch_runtime.wake_read_id` 完全一致——同一个唤醒字典喂进两边，
    出来的摘要必须相同（只差前缀），这不是巧合而是接口：回合落账后要说「销掉的就是这条」。
    所以这里**接收字典**而不是三个散参数：散参数各写一遍格式化，`atMs` 缺失时一个拼成
    "0"、一个拼成 ""，两边就悄悄分道了。自测里有专门的同键断言。"""
    return "wj-" + _digest(
        "%s|%s|%s" % (wake.get("text", ""), wake.get("atMs", ""), wake.get("assignmentId", ""))
    )


def arrival_journal_key(wake: dict) -> str:
    """跨领取的到达键：只取（正文, 到达时间），与 `wake_arrival_key` 同一条算式。

    它解决的是耐久日志自己的泄漏：补查把一条旧投递重投给**新领取**，磁盘上于是有两条
    （旧领取一条、新领取一条）；新那条被本轮销掉，旧那条却永远留着。到达键让「这句话
    已经进过会话」也能清掉它在旧领取名下的那条记录。"""
    return "wjarr-" + _digest("%s|%s" % (wake.get("text", ""), wake.get("atMs", "")))


def read_id_of_journal_record(record: dict) -> str:
    """耐久记录 → 已读账本里的键（`wake_read_id` 算出的那把）。

    两个键是**同一个摘要换前缀**：`wake-` ↔ `wj-`。所以这里不重算——重算要用落盘正文，
    而正文可能被截断（见 `MAX_TEXT_CHARS`），截断后的正文算出来的键和账本对不上，
    那条唤醒就永远「没读过」、每重启一次就被重喂一次。前缀是两边共同的约定，改任何
    一边都要同步（自测里有把这条钉死的断言）。"""
    wake_id = str(record.get("wakeId") or "")
    if not wake_id.startswith("wj-"):
        return ""
    return "wake-" + wake_id[len("wj-"):]


class TicketInbox:
    """唤醒投递的磁盘日志：入箱即落盘，喂进会话并落账为已读之后才销账。"""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else dp.ticket_inbox_file()
        self._clock = clock
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self.load_error = ""
        self._load()

    def _ms(self) -> int:
        """本层的墙钟：注入时钟只为「损坏文件改名」与「落盘时刻」这两个自证字段服务，
        测试里要能断言 `deliveredAtMs`，不能让它在两个实例间漂移。"""
        return int(self._clock() * 1000)

    # ── 落盘 ──────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or int(raw.get("version") or 0) != INBOX_VERSION:
                raise ValueError("版本不符或结构不是对象")
            records = raw.get("records")
            if not isinstance(records, list) or not all(
                isinstance(r, dict) and r.get("wakeId") and r.get("ticketId") and r.get("assignmentId")
                for r in records
            ):
                raise ValueError("records 结构损坏")
            self._records = records
        except Exception as exc:  # noqa: BLE001  坏文件改名让路，不在坏文件上追加事实
            stamp = self._ms()
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                self.path.replace(aside)
                self.load_error = f"耐久收件箱损坏，已改名保留 {aside.name} 并从空日志起步：{exc}"
            except Exception as move_exc:  # noqa: BLE001  挪不动就明说不可写，不假装能落盘
                self.load_error = f"耐久收件箱损坏且无法移开（{exc}；{move_exc}）：本层不可写，投递只进内存箱"

    def _save(self) -> None:
        data = {"version": INBOX_VERSION, "records": self._records}
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) > 8_000_000:
            raise RuntimeError("耐久收件箱超过 8M 上限——请归档已结束领取的落盘记录")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{int(self._clock() * 1000)}")
        tmp.write_text(encoded, encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 写入 ──────────────────────────────────────────────────

    def append(
        self,
        *,
        ticket_id: str,
        assignment_id: str,
        text: str,
        origin: str = "human",
        at_ms: Any = 0,
    ) -> dict[str, Any]:
        """记下一条「已投进这份领取、还没进会话」的唤醒（幂等）。

        幂等按 (正文, 到达时间, 领取) 计：同一句话在同一次领取里被投两次（并发首触、
        调用方重试）只留一条——那本来就是一条唤醒。换一份领取再投是同一条算式下的另一条，
        各留一条，与已读键的口径一致。"""
        tid = str(ticket_id or "").strip()
        aid = str(assignment_id or "").strip()
        body = str(text or "")
        if not tid or not aid:
            return {"ok": False, "reason": "工单 id 与领取 id 都必填：没有归属的投递没法销账"}
        if not body.strip():
            return {"ok": False, "reason": "正文为空：空消息不进收件箱，也不进耐久日志"}
        arrival = int(at_ms or 0) or self._ms()
        # 两个键都在**截断之前**算：它们要和唤醒字典（带的是原文）对齐，用截断后的正文
        # 算出来的键对不上已读账本，销账时会把这条永远留在箱里。
        source_wake = {"text": body, "atMs": arrival, "assignmentId": aid}
        wake_id = wake_journal_id(source_wake)
        arrival_key = arrival_journal_key(source_wake)
        truncated = ""
        if len(body) > MAX_TEXT_CHARS:
            truncated = f"正文 {len(body)} 字符超过落盘上限 {MAX_TEXT_CHARS}，已截断留痕（原条不丢）"
            body = body[:MAX_TEXT_CHARS] + "…（已截断）"
        with self._lock:
            if self.load_error and ("无法移开" in self.load_error or "不可写" in self.load_error):
                return {"ok": False, "reason": f"耐久收件箱不可写：{self.load_error}", "wakeId": wake_id}
            for r in self._records:
                if r.get("wakeId") == wake_id:
                    return {"ok": True, "deduped": True, "wakeId": wake_id, "record": dict(r)}
            if len(self._records) >= MAX_RECORDS:
                return {
                    "ok": False,
                    "wakeId": wake_id,
                    "reason": (
                        f"耐久收件箱已有 {len(self._records)} 条、到上限 {MAX_RECORDS}：这条**没有落盘**，"
                        "只在内存箱里。不落盘就等于重启后只有调用方的候选能把它捞回来——"
                        "先查是不是有领取长期不收口（每条滞留唤醒都占一格），不要指望挤掉旧记录。"
                    ),
                }
            record = {
                "wakeId": wake_id,
                "arrivalKey": arrival_key,
                "ticketId": tid,
                "assignmentId": aid,
                "text": body,
                "atMs": arrival,
                "origin": str(origin or ""),
                "deliveredAtMs": self._ms(),
            }
            try:
                self._records.append(record)
                self._save()
            except Exception as exc:  # noqa: BLE001  落盘失败不吞：内存箱那条还在，但没有耐久副本
                self._records = [r for r in self._records if r.get("wakeId") != wake_id]
                return {"ok": False, "wakeId": wake_id, "reason": f"耐久收件箱落盘失败：{exc}", "memoryOnly": True}
        out = {"ok": True, "deduped": False, "wakeId": wake_id, "record": dict(record)}
        if truncated:
            out["note"] = truncated
        return out

    def mark_consumed(self, wake_ids: list[str], arrival_keys: list[str] | None = None) -> dict[str, Any]:
        """销账：这批唤醒已被喂进本轮会话并落账为已读。

        两道键（缺一不可）：

        * `wake_ids`（正文+到达时间+领取）按本轮的领取销——正常一轮就走这条路；
        * `arrival_keys`（正文+到达时间）兜住**补查重投**留下的旧记录：同一条唤醒崩溃前
          投在旧领取名下、重启后重投在新领取名下，磁盘上是两条。本轮只销得掉新那条，
          旧那条若不按到达键一起清，就永远挂在箱里，下次补查再捞一遍——耐久日志自己
          变成了不丢的垃圾。

        两边都对不上的 id 不报错也不补记，`missing` 如实带出条数交给调用方的注记说明。"""
        wanted = {str(x) for x in (wake_ids or []) if str(x)}
        keys = {str(x) for x in (arrival_keys or []) if str(x)}
        if not wanted and not keys:
            return {"ok": True, "removed": 0, "missing": []}
        with self._lock:
            matched = {
                r.get("wakeId")
                for r in self._records
                if r.get("wakeId") in wanted or r.get("arrivalKey") in keys
            }
            missing = sorted(w for w in wanted if w not in matched)
            self._records = [r for r in self._records if r.get("wakeId") not in matched]
            removed = len(matched)
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001  销账没落盘要说清：下次重启会把它们当未投递过
                return {
                    "ok": False,
                    "removed": removed,
                    "missing": missing,
                    "reason": (
                        f"销账没落盘（{exc}）：这 {removed} 条下次重启仍会被当成「投过但没进会话」"
                        "重投一次——不丢唤醒的代价是真重复，由外发幂等键挡下"
                    ),
                }
        return {"ok": True, "removed": removed, "missing": missing}

    # ── 读 ────────────────────────────────────────────────────

    def pending_for_ticket(self, ticket_id: str) -> list[dict[str, Any]]:
        """这一单当前「投进引擎但没进会话」的唤醒，按到达时间升序（重启补查的输入）。"""
        tid = str(ticket_id or "")
        with self._lock:
            items = [dict(r) for r in self._records if r.get("ticketId") == tid]
        items.sort(key=lambda r: (int(r.get("atMs") or 0), str(r.get("wakeId") or "")))
        return items

    def pending_of_assignment(self, assignment_id: str) -> list[dict[str, Any]]:
        aid = str(assignment_id or "")
        with self._lock:
            return [dict(r) for r in self._records if r.get("assignmentId") == aid]

    def pending_wakes_for_ticket(self, ticket_id: str) -> list[dict[str, Any]]:
        """把这一单的在箱投递还原成**唤醒字典**（重启补查的候选来源，D-48.6 的主用途）。

        比 `pending_for_ticket` 多做两件补查要用到的事：

        * **readId 从前缀换来**而不是从正文重算——落盘正文可能被截断（`MAX_TEXT_CHARS`），
          重算出来的键和已读账本对不上，那条就永远「没读过」；摘要两边本来就相同，
          换前缀是唯一稳的换法。
        * 截断过的记录带一句 `truncated` 注记：它的到达键是按截断后正文算的，跨领取核对
          可能认不出来，宁可重投一条也不假装读过。
        """
        out: list[dict[str, Any]] = []
        for r in self.pending_for_ticket(ticket_id):
            wake = {"text": r.get("text", ""), "atMs": r.get("atMs"), "assignmentId": r.get("assignmentId", "")}
            item = {
                "text": str(r.get("text") or ""),
                "atMs": int(r.get("atMs") or 0),
                "origin": str(r.get("origin") or "human"),
                "assignmentId": str(r.get("assignmentId") or ""),
                "hasArrival": bool(r.get("atMs")),
                "readId": read_id_of_journal_record(r),
                "arrivalKey": arrival_journal_key(wake),
                "wakeId": str(r.get("wakeId") or ""),
                "source": "journal",
            }
            if str(r.get("text") or "").endswith("…（已截断）"):
                item["truncated"] = True
            out.append(item)
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_ticket: dict[str, int] = {}
            for r in self._records:
                by_ticket[r["ticketId"]] = by_ticket.get(r["ticketId"], 0) + 1
            return {
                "version": INBOX_VERSION,
                "path": str(self.path),
                "records": len(self._records),
                "byTicket": by_ticket,
                "maxRecords": MAX_RECORDS,
                "loadError": self.load_error,
            }
