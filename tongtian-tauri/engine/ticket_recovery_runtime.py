"""重启恢复注记、已读落账与后台作业看门狗（缺陷 D-48.3）

D-48.1 给了工单执行体的目录/检查点/槽位，D-48.2 给了耐久外发队列；这一层补上
**崩溃与重启之后**的三条纪律，让「恢复续跑」不靠自觉而靠账本：

1. **恢复注记**：重启后给新 Assignment 的每一份任务说明都必须带一段恢复注记，
   顺序写死——先查 worker 目录、再查 git 历史与远端、再对照副作用账本里**可能已
   发出**的记录，最后才允许重复任何外部动作。注记里点名旧检查点的保存时间与
   在途外发 id，让执行体拿证据决定重放还是补做；崩溃时仍在跑的后台作业会消失，
   注记必须说出这一点，不装「一切都在」。
2. **已读落账**：「消息是否已被会话读进历史」按 (Assignment, 消息) 落账到磁盘。
   这是重放判断的事实来源——重启后补查用 `unread_of` 列出没读过的消息，读过的
   绝不重复交付；落账幂等，重复 mark 不会堆事实。
3. **30 分钟看门狗**：后台作业到点就杀，并在报告里**点名被杀了什么**（描述、
   跑了多久、杀没杀干净）。杀不掉要如实说杀不掉；杀完的条目留在记录里供汇报。
   另提供槽位口径：回合结束但后台作业还在跑时，worker 不该上报 waiting——
   `turn_done_state` 把这条判定收在一处，接线方只需要照它翻状态。

边界：本模块不起进程、不发 HTTP、不碰模型；kill_fn 由调用方注入（真实杀进程
是接线的执行回路的事），账本落盘走 D-43 单一来源。三个机制各自可独立接线，
但共享「拒绝与结果都给中文理由」的口径。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import data_paths as dp

#: 后台作业硬上限：30 分钟到点杀（验收项③）
JOB_TIMEOUT_SECONDS = 30 * 60

#: 恢复注记里列在途外发时的最大点名条数（再多就只报数量，注记不是账本副本）
MAX_INFLIGHT_IDS_IN_NOTE = 12

READS_VERSION = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── 1. 恢复注记 ───────────────────────────────────────────────


def build_recovery_note(
    *,
    ticket_id: str,
    prev_assignment_id: str = "",
    workspace_dir: str = "",
    dir_exists: bool = False,
    checkpoint_saved_at: str = "",
    checkpoint_messages: int = 0,
    inflight_outbox_ids: list[str] | None = None,
    pending_outbox_kinds: list[str] | None = None,
    crashed_jobs: list[str] | None = None,
    carried_wakes: int = 0,
) -> str:
    """给新 Assignment 的恢复注记：动作顺序与证据点名，缺证据也如实说缺。

    文案是给执行体读的指令，不是给人看的日志——但被拒时调用方拿到的也是同一
    份可读文本，不是一句「恢复失败」。"""
    tid = str(ticket_id or "").strip()
    if not tid:
        return ""
    inflight = [str(x) for x in (inflight_outbox_ids or []) if str(x).strip()]
    pending = [str(x) for x in (pending_outbox_kinds or []) if str(x).strip()]
    jobs = [str(x) for x in (crashed_jobs or []) if str(x).strip()]
    lines = [
        "【恢复注记（D-48.3）】本轮是崩溃/重启后的续跑，禁止直接重复任何外部动作，按顺序核证据：",
        (
            f"1) 先查 worker 目录：{workspace_dir or '（未提供路径）'}"
            f"——{'目录在，半成品文件按已发生处理，别当不存在重来' if dir_exists else '目录不在，本轮从零开始（无历史半成品）'}"
        ),
        "2) 再查 git 历史与远端：本地 commit、分支、远端状态都可能已包含上一轮成果，先比对再动手。",
    ]
    if inflight:
        shown = "、".join(inflight[:MAX_INFLIGHT_IDS_IN_NOTE])
        more = f"（共 {len(inflight)} 条，其余略）" if len(inflight) > MAX_INFLIGHT_IDS_IN_NOTE else ""
        lines.append(
            f"3) 对照副作用账本：这些外发在崩溃时仍在途、可能已经发出去——{shown}{more}；"
            "先到目标会话里核实，确认没送达才允许重发，确认送达就 mark done，不重复发送。"
        )
    elif pending:
        lines.append(
            f"3) 副作用账本没有「已发出待确认」的在途记录，但崩溃时未提交事务里有 {len(pending)} 条 queued"
            f"（kind：{'、'.join(dict.fromkeys(pending))}）——按未发出处理，重做请开新事务显式重排。"
        )
    else:
        lines.append("3) 副作用账本没有在途与未提交记录：上一轮没有悬空的外发。")
    if checkpoint_saved_at:
        lines.append(
            f"4) 旧检查点证据：上一轮（{prev_assignment_id or '未知领取'}）检查点保存于 {checkpoint_saved_at}，"
            f"含 {checkpoint_messages} 条对话消息——恢复对话上下文可以读它，但它属于旧 Assignment，"
            "不能当作「本轮已做完」的证明。"
        )
    else:
        lines.append(
            f"4) 没有可用的旧检查点（{'上一轮 ' + prev_assignment_id + ' 未留下检查点' if prev_assignment_id else '上一轮信息缺失'}）"
            "——对话上下文无法恢复，缺口要在汇报里说明，不假装有。"
        )
    if jobs:
        lines.append(
            f"5) 崩溃时仍在跑的后台作业 {len(jobs)} 个已随进程消失：{'、'.join(jobs[:MAX_INFLIGHT_IDS_IN_NOTE])}"
            "——它们没有完成，需要时按新作业重启，并在汇报里点名旧作业中止。"
        )
    if carried_wakes:
        lines.append(f"6) 收件箱里还带着 {carried_wakes} 条崩溃前未消费的唤醒：先消费它们，再考虑重做原任务。")
    return "\n".join(lines)


# ── 2. 已读落账 ───────────────────────────────────────────────


class ReadLedger:
    """「消息是否已被会话读进历史」的磁盘账本，按 (Assignment, 消息) 记录。

    事实按步骤落账（step_id 可空但要落）：重放判断只看这份账本，不信执行体自述。
    落盘原子替换；账本损坏改名让路并暴露 loadError，与副作用队列同一口径。"""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else dp.ticket_reads_file()
        self._clock = clock
        self._lock = threading.Lock()
        self._marks: list[dict[str, Any]] = []
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or int(raw.get("version") or 0) != READS_VERSION:
                raise ValueError("版本不符或结构不是对象")
            marks = raw.get("marks")
            if not isinstance(marks, list) or not all(
                isinstance(m, dict) and m.get("assignmentId") and m.get("messageId") for m in marks
            ):
                raise ValueError("marks 结构损坏")
            self._marks = marks
        except Exception as exc:  # noqa: BLE001  坏账本改名让路，不在坏文件上追加事实
            stamp = int(self._clock() * 1000)
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                self.path.replace(aside)
                self.load_error = f"已读账本损坏，已改名保留 {aside.name} 并从空账本起步：{exc}"
            except OSError as move_exc:
                self.load_error = f"已读账本损坏且无法移开（拒写，请人工处理）：{exc} / {move_exc}"

    def _save(self) -> None:
        data = {"version": READS_VERSION, "marks": self._marks}
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) > 8_000_000:
            raise RuntimeError("已读账本超过 8M 上限——请归档旧 Assignment 的落账")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp-{stamp_suffix()}")
        tmp.write_text(encoded, encoding="utf-8")
        import os

        os.replace(tmp, self.path)

    def _key(self, assignment_id: str, message_id: str) -> tuple[str, str]:
        return (str(assignment_id), str(message_id))

    def mark_read(
        self,
        assignment_id: str,
        message_id: str,
        *,
        step_id: str = "",
        session_key: str = "",
        ticket_id: str = "",
        arrival_key: str = "",
    ) -> dict[str, Any]:
        """落一条「这条消息被这份领取读进会话」的事实（幂等）。

        `ticket_id` / `arrival_key` 是 D-48.5g 补查用的**跨领取**凭据：已读键按领取计，
        换一份领取就从零核对，这是「不丢唤醒」的那一半；另一半是「同一句话不要因为重启
        k 次就被喂 k 次」——(正文, 到达时间) 摘要不随领取变，落账时顺手记下，重启补查
        才能问「这句话在这一单的哪份领取里进过会话」。旧落账没有这两个字段就是没有，
        补查据此一律按未读重投（宁可重投不丢唤醒），不假装能对账。"""
        aid = str(assignment_id or "").strip()
        mid = str(message_id or "").strip()
        if not aid or not mid:
            return {"ok": False, "reason": "assignmentId 与 messageId 都必填——已读事实必须说清「谁读到了哪条」"}
        with self._lock:
            if self.load_error and "无法移开" in self.load_error:
                return {"ok": False, "reason": f"账本不可写：{self.load_error}"}
            for m in self._marks:
                if self._key(m.get("assignmentId", ""), m.get("messageId", "")) == (aid, mid):
                    return {
                        "ok": True,
                        "deduped": True,
                        "mark": dict(m),
                        "note": "这条已读过：重放判断以既有落账为准，不重复交付",
                    }
            mark = {
                "assignmentId": aid,
                "messageId": mid,
                "stepId": str(step_id or ""),
                "sessionKey": str(session_key or ""),
                "markedAtMs": _now_ms(),
            }
            if ticket_id:
                mark["ticketId"] = str(ticket_id)
            if arrival_key:
                mark["arrivalKey"] = str(arrival_key)
            self._marks.append(mark)
            self._save()
        return {"ok": True, "deduped": False, "mark": dict(mark)}

    def is_read(self, assignment_id: str, message_id: str) -> bool:
        with self._lock:
            return any(
                self._key(m.get("assignmentId", ""), m.get("messageId", "")) == (str(assignment_id), str(message_id))
                for m in self._marks
            )

    def unread_of(self, assignment_id: str, message_ids: list[str]) -> list[str]:
        """重启补查的入口：给一批候选消息，返回还没落账为已读的（保持输入顺序）。"""
        with self._lock:
            read = {m.get("messageId") for m in self._marks if m.get("assignmentId") == str(assignment_id)}
        return [str(x) for x in (message_ids or []) if str(x) not in read]

    def marks_of(self, assignment_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(m) for m in self._marks if m.get("assignmentId") == str(assignment_id)]

    def fed_arrivals(self, ticket_id: str) -> set[str]:
        """跨领取核对：这一单**任何**一份领取喂进过会话的消息摘要集合。

        只认落账里带了 `arrivalKey` 且 `ticketId` 对得上的条目——旧格式条目不参与，
        宁可让补查把那条当未读重投（丢唤醒比重复喂一句严重）。"""
        tid = str(ticket_id or "")
        with self._lock:
            return {
                str(m["arrivalKey"])
                for m in self._marks
                if m.get("ticketId") == tid and str(m.get("arrivalKey") or "")
            }

    def unattributed_marks(self) -> int:
        """账本里有多少条落账带不出跨领取凭据（D-48.5g 之前落的旧格式）。

        这些条目**归不到工单**，跨领取核对覆盖不到它们——补查据此如实报核对缺口，
        而不是假装「查过没有就是没有」。"""
        with self._lock:
            return sum(1 for m in self._marks if not str(m.get("arrivalKey") or "") or not str(m.get("ticketId") or ""))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_assignment: dict[str, int] = {}
            for m in self._marks:
                by_assignment[m["assignmentId"]] = by_assignment.get(m["assignmentId"], 0) + 1
            return {"marks": len(self._marks), "byAssignment": by_assignment, "loadError": self.load_error}


def stamp_suffix() -> str:
    import os

    return f"{os.getpid()}-{int(time.time() * 1000)}"


# ── 3. 后台作业看门狗 ─────────────────────────────────────────


class JobWatchdog:
    """后台作业登记与 30 分钟到点杀。

    `tick(kill_fn=…)` 由接线的执行回路周期性调用：到点作业交给 kill_fn 处置，
    杀成功/杀失败都要点名进报告——「作业悄悄跑了 3 小时」与「杀了但没报告」
    都不算口径落地。回合结束的槽位口径也收在这里：还有作业在跑就不是 waiting。"""

    def __init__(self, *, timeout_seconds: float = JOB_TIMEOUT_SECONDS, clock: Callable[[], float] = time.time):
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._killed: list[dict[str, Any]] = []

    def register(self, job_id: str, *, description: str = "", ticket_id: str = "") -> dict[str, Any]:
        jid = str(job_id or "").strip()
        if not jid:
            return {"ok": False, "reason": "jobId 为空——看门狗不认识匿名作业"}
        with self._lock:
            if jid in self._jobs:
                return {"ok": False, "reason": f"作业「{jid}」已在跑，重复登记请换 id（看门狗不合并两条计时）"}
            job = {
                "jobId": jid,
                "description": str(description or jid),
                "ticketId": str(ticket_id or ""),
                "startedAt": self._clock(),
                "state": "running",
            }
            self._jobs[jid] = job
        return {"ok": True, "job": dict(job)}

    def complete(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return {"ok": False, "reason": f"找不到在跑的作业「{job_id}」（已完成的走报告，不回来）"}
            job["state"] = "completed"
            job["completedAt"] = self._clock()
            del self._jobs[str(job_id)]
        return {"ok": True, "elapsedSeconds": round(job["completedAt"] - job["startedAt"], 1)}

    def running_for(self, ticket_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(j)
                for j in self._jobs.values()
                if j["state"] == "running" and (not ticket_id or j["ticketId"] == str(ticket_id))
            ]

    def tick(self, kill_fn: Callable[[dict[str, Any]], Any] | None = None, *, now: float | None = None) -> list[dict[str, Any]]:
        """到点杀：返回本轮被处置的作业清单（含杀失败项），并留存供汇报点名。"""
        now_s = self._clock() if now is None else now
        expired: list[dict[str, Any]] = []
        with self._lock:
            for job in list(self._jobs.values()):
                if job["state"] != "running":
                    continue
                if now_s - job["startedAt"] < self.timeout_seconds:
                    continue
                entry = {
                    "jobId": job["jobId"],
                    "description": job["description"],
                    "ticketId": job["ticketId"],
                    "elapsedSeconds": round(now_s - job["startedAt"], 1),
                    "killOk": True,
                    "killError": "",
                }
                if kill_fn is not None:
                    try:
                        entry["killOk"] = bool(kill_fn(dict(job)))
                    except Exception as exc:  # noqa: BLE001  杀不掉必须点名，不能吞
                        entry["killOk"] = False
                        entry["killError"] = f"{type(exc).__name__}: {exc}"
                job["state"] = "timeout"
                del self._jobs[job["jobId"]]
                entry["killedAt"] = now_s
                self._killed.append(entry)
                expired.append(dict(entry))
        return expired

    def kill_report_lines(self, entries: list[dict[str, Any]] | None = None, *, limit_minutes: int = 30) -> list[str]:
        """把被杀作业翻译成中文汇报行——验收项③「在报告里点名」。"""
        items = entries if entries is not None else list(self._killed)
        lines: list[str] = []
        for e in items:
            if e.get("killOk"):
                lines.append(
                    f"后台作业「{e['description']}」运行 {round(e['elapsedSeconds'] / 60)} 分钟，"
                    f"超过 {limit_minutes} 分钟上限，已中止（缺陷 D-48.3 看门狗）。"
                )
            else:
                lines.append(
                    f"后台作业「{e['description']}」运行 {round(e['elapsedSeconds'] / 60)} 分钟超过上限，"
                    f"但中止失败：{e.get('killError') or 'kill 返回失败'}——它可能仍在跑，需要人工处理。"
                )
        return lines

    def turn_done_state(self, ticket_id: str) -> str:
        """回合结束后的对外状态：还有后台作业在跑就保持 working，不上报 waiting。"""
        return "working" if self.running_for(ticket_id) else "waiting"

    def killed_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._killed]

    def crashed_job_descriptions(self, ticket_id: str = "") -> list[str]:
        """重启注记的输入：崩溃前还在跑的作业名（进程没了，它们也一定没完成）。"""
        return [f"{j['description']}（{j['jobId']}）" for j in self.running_for(ticket_id)]
