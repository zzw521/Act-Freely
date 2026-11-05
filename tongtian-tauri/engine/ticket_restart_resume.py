"""重启后的唤醒补查与新领取续跑（缺陷 D-48.5g，候选来源由 D-48.6 加到两条）

D-48.3 把「谁读到了哪条」落进磁盘账本，D-48.5c 证明了**副作用层**重启不重放；但从崩溃
里醒过来的进程还缺一条编排：**收件箱是内存件**，活领取随进程一起没了，「上一进程里哪些
唤醒从没进过会话历史」这件事没有任何入口能回答——`TicketDispatcher.state_of` 里那个
`unreadWakes` 在注册表为空时只会给出空列表，查询本身随进程死了。本模块补的就是编排层这一
刀，只做四件事：

1. **补查（`plan`）**：候选有两个来源——调用方（服务端工单消息表）给的清单，**加上**
   耐久收件箱（D-48.6）里这一单「投进过引擎但没进会话」的记录；两边按已读键与到达键去重
   后合成一批，再按 `ReadLedger` 分成「从没进过会话历史」与「已读，不再重投」两堆，并把
   一次能核到的证据列全（旧检查点的归属与条数、旧领取的落账条数、账本在途外发、本进程
   残留唤醒、耐久箱在箱条数与从中捞回的条数）。
   判不出来的如实说：候选没带到达时间就没有可比对的键（按未读处理并报出条数）；旧领取
   一条落账都没有时，「都没读过」与「账本没写过」分不清——一律按未读重投，**宁可重投
   一条也不丢唤醒**。
   同一次判定里还有**跨领取**那一半：已读键按领取计（换领取从零核对，保住「不丢唤醒」），
   但重投与否另外看这句话在这一单**更早的**某份领取里进过会话没有——只看当前 prev 的键，
   同一批候选会每重启一次就被重喂一次，直到把上下文撑满。
   耐久箱来的候选按**各自投递时的领取**核对已读（不是一律按 prev）：那条投递可能记在
   更早的一份领取名下，拿 prev 查必然「未读」，于是每重启重喂一遍。
2. **重投（`resume`）**：只把未读的那几条投给**一次新领取**（新的 Assignment，带
   `prev_assignment_id`），原始到达时间原样带回；本进程收件箱里已经压着同一条的不重复投。
   随后 `scheduler.kick(prev_assignment_id=…)` 起一轮：本轮按旧检查点借对话上下文（只借
   上下文不借结论），已读落账记在**新领取**名下——所以二次重启拿着同一批候选不会再投一遍。
   顺带清掉耐久箱里「账本说读过、盘上还说没进会话」的遗留记录（`plan` 只点名，动手在这里）。
3. **拒绝面**：本进程已有活领取（那是正常路径，补查插手就是两份绑定）、缺 prev / 缺新领取
   id、旧检查点属于别的工单（prev 给错了，拒绝把别人的上下文接进本轮）、槽位没到手（一条
   也没投出去，候选还留在调用方那边）——每条都回一句中文理由，不给一个干巴巴的 False。
4. **不起空转轮**：候选全是已读，或一条未读都没有 → 报 `nothing-to-resume` 并说明下一步
   该做什么，不调模型、不占槽。

边界：不发 HTTP、不起进程、不调模型。候选清单从「唯一来源」变成「第二个来源」——耐久收件箱
能自己说出「投进过引擎但没进会话」的那几条，但**引擎停机期间到达**的唤醒磁盘上没有痕迹，
那部分只有服务端消息表知道，所以调用方候选不撤。假装能凭空「捞出」崩溃前的收件箱才是编故事。
跨进程的证据在 D-48.5c 的演练里；本模块的
自测用**同一数据根上的第二套装配**做重启：磁盘件（副作用账本 / 已读账本 / 检查点 / 耐久箱）
从盘重读，内存件（注册表 / 槽位池 / 看门狗 / 调度器）全新——编排层的语义恰好只依赖这一条。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import checkpoint_runtime as cr
import ticket_worker_runtime as twr
from ticket_dispatch_runtime import wake_arrival_key, wake_read_id

#: 候选唤醒允许带的字段（其余键忽略）——服务端消息表落到引擎时形状不止一种
_TEXT_KEYS = ("text", "message", "content")


def _now_ms() -> int:
    return int(time.time() * 1000)


class TicketRestartResume:
    """重启补查的编排入口：拿调度器（连同 dispatcher / registry），只读账本、只投唤醒。"""

    def __init__(self, scheduler: Any):
        self.scheduler = scheduler
        self.dispatcher = scheduler.dispatcher
        self.registry = scheduler.registry
        self.ledger = self.dispatcher.ledger
        self.outbox = self.dispatcher.outbox
        self.watchdog = self.dispatcher.watchdog
        #: 耐久收件箱（D-48.6）：接装配时自动跟随 dispatcher；没接就是旧的「只认候选」行为。
        self.inbox_journal = getattr(self.dispatcher, "inbox_journal", None)
        self._journal_read_error = ""
        #: 最近一次 plan 从磁盘上看到的在箱条数（含与候选重复的那几条）
        self._journal_inbox_total = 0

    # ── 对外 ──────────────────────────────────────────────────

    def plan(
        self,
        ticket_id: str,
        *,
        prev_assignment_id: str,
        candidate_wakes: list[Any] | None = None,
        title: str = "",
        origin: str = "human",
    ) -> dict[str, Any]:
        """把候选唤醒按「进过会话历史没有」分成两堆，并列出这次能核到的全部证据。

        判定只信账本：`ReadLedger.unread_of(prev, …)` 是唯一事实来源，执行体上一轮
        「我读过了」的自述在这里一个字都不算。"""
        tid = str(ticket_id or "").strip()
        if not tid:
            return self._refuse("need-ticket", "工单 id 为空：补查不知道该核对哪条工单的已读事实")
        prev = str(prev_assignment_id or "").strip()
        if not prev:
            return self._refuse(
                "need-prev",
                "缺 prev_assignment_id：补查必须说清「接着哪一次领取查」——已读是按 (领取, 消息) 记账的，"
                "没有旧领取 id 就无从比对，只能把整批当没读过重投，那是重复消费不是补查。",
                ticket_id=tid,
            )
        worker = self.registry.worker(tid)
        if worker is not None and worker.assignment_id and not worker.ended_status:
            return self._refuse(
                "already-live",
                f"工单 {tid} 在本进程已有活领取（{worker.assignment_id}）：那是正常路径，"
                "唤醒用 submit 投递即可。补查只处理「活领取随进程没了」的情况——"
                "在这里再起一份绑定就是同一工单的两个自己。",
                ticket_id=tid,
                prev_assignment_id=prev,
            )

        items = [self._normalize(c, prev_assignment_id=prev, default_origin=origin) for c in (candidate_wakes or [])]
        dropped_empty = sum(1 for i in items if not i["text"])
        items = [i for i in items if i["text"]]

        # 耐久收件箱（D-48.6）：磁盘上还记着「投进过引擎但没进会话」的那些投递，是补查的
        # **第二个候选来源**。5g 之前只有调用方给得出候选（内存件不会自己活过来），现在引擎
        # 自己也知道收到过什么——但它**不替代**候选清单：引擎停机期间到达的唤醒磁盘上
        # 没有痕迹，那条只有服务端消息表知道。
        journal_items = self._journal_items(tid, caller_items=items, default_origin=origin)
        items = items + journal_items
        journal_pending_total = self._journal_inbox_total

        inbox_read_ids = {wake_read_id(w) for w in (worker.inbox if worker is not None else [])}
        inbox_arrival_keys = {wake_arrival_key(w) for w in (worker.inbox if worker is not None else [])}
        # 跨领取核对：这一单任何一份领取喂进过会话的句子都不再重投（详见
        # `ticket_dispatch_runtime.wake_arrival_key` 的口径）——只看当前 prev 的已读键会
        # 让同一条唤醒每重启一次就「对新领取而言是未读」，被重喂一遍。
        fed_arrivals = self.ledger.fed_arrivals(tid)
        # 按**各自投递时的领取**分组核对已读：耐久箱捞回来的候选可能记在更早的一份领取
        # 名下，拿 prev 去查它必然「未读」，于是每次重启都重喂一句早就读过的话。
        by_claim: dict[str, list[str]] = {}
        for i in items:
            by_claim.setdefault(str(i.get("assignmentId") or prev), []).append(i["readId"])
        unread_ids: set[str] = set()
        for claim, ids in by_claim.items():
            unread_ids |= set(self.ledger.unread_of(claim, ids))
        unread: list[dict[str, Any]] = []
        already_read: list[dict[str, Any]] = []
        already_in_box: list[dict[str, Any]] = []
        for i in items:
            if i["readId"] in inbox_read_ids or i["arrivalKey"] in inbox_arrival_keys:
                already_in_box.append(i)  # 本进程箱里已经压着，起轮时自己会被带走，不重复投
            elif i["readId"] not in unread_ids:
                already_read.append(i)  # 旧领取的落账里有它：进过会话历史，不再重投
            elif i["arrivalKey"] in fed_arrivals:
                # 读过的不是「这次核对的那份领取」，而是这一单更早的某一份——同样不重投。
                i["via"] = "cross-claim"
                already_read.append(i)
            else:
                unread.append(i)
        # 磁盘说「还没进会话」、账本说「早进过」的那几条是销账没落盘的遗留：没有后续回合
        # 会再消费它，不点名出来它就永远占在箱里。plan 只读，清理由 resume 顺路做。
        journal_stale = [str(i.get("wakeId") or "") for i in already_read if i.get("source") == "journal"]

        prev_key = twr.checkpoint_task_id(prev)
        prev_checkpoint = cr.load_checkpoint(prev_key)
        owner = str((prev_checkpoint or {}).get("ticketId") or "")
        if prev_checkpoint is not None and owner and owner != tid:
            # 键里只有 Assignment 摘要，看不出工单——归属字段是本轮检查点落盘时带的。
            # prev 给错会把**别的工单**的对话历史接进本轮，那不是续跑是串台，直接拒。
            return self._refuse(
                "wrong-owner",
                f"旧领取 {prev} 的检查点属于工单「{owner}」，不是 {tid}：prev_assignment_id 给错了，"
                "补查拒绝把别的工单的上下文接进本轮。请核对领取归属后重发。",
                ticket_id=tid,
                prev_assignment_id=prev,
            )

        marks = self.ledger.marks_of(prev)
        inflight = self.outbox.inflight_for(tid)
        crashed_jobs = self.watchdog.crashed_job_descriptions(tid)
        notes: list[str] = []
        no_arrival = sum(1 for i in items if not i["hasArrival"])
        if no_arrival:
            notes.append(
                f"{no_arrival} 条候选没有到达时间：已读键是（正文+到达时间+领取），没有它就没有可比对的键——"
                "这几条按未读重投（可能重投一条其实读过的），下次请从消息表带上 arrivedAtMs。"
            )
        if dropped_empty:
            notes.append(f"{dropped_empty} 条候选正文为空，没有参与补查（空消息不进收件箱）。")
        if items and not marks:
            notes.append(
                f"旧领取 {prev} 在账本上一条落账都没有：分不清「都没读过」与「账本从没落笔」（崩溃正好落在"
                "落账之前）。这批全部按未读重投——宁可重投一条也不丢唤醒，真重复的外发由幂等键挡下。"
            )
        if prev_checkpoint is None:
            notes.append(f"旧领取 {prev} 没有留下可读的检查点（没落过盘、或已按 finished 删除）：对话上下文借不到，本轮从零开始。")
        elif not int(prev_checkpoint.get("savedAtMs") or 0):
            notes.append("旧检查点没有保存时间可核，注记里按缺失处理。")
        if not crashed_jobs:
            notes.append(
                "崩溃前在跑的后台作业在这一层无从得知（看门狗是内存件，随进程一起没了）——"
                "要那份证据请看 D-48.5c 的跨进程演练，本模块不假称「没有作业被打断」。"
            )
        in_process_wakes = len(worker.inbox) if worker is not None else 0
        if already_in_box:
            notes.append(
                f"{len(already_in_box)} 条候选在本进程收件箱里已经压着（属于已结束的那份领取）：不重复投递，"
                "新领取起轮时随 `drain_inbox` 一起带走（恢复注记按 carried_wakes 点名）。"
            )
        cross_claim = [i for i in already_read if i.get("via") == "cross-claim"]
        if cross_claim:
            notes.append(
                f"{len(cross_claim)} 条候选读过的不是本次核对的旧领取，而是这一单更早的某一份领取："
                "跨领取凭据（正文+到达时间）认出来了，同样不重投——否则每重启一次就把同一句话再喂一遍。"
            )
        unattributed = self.ledger.unattributed_marks()
        if unattributed:
            notes.append(
                f"账本里有 {unattributed} 条落账带不出跨领取凭据（D-48.5g 之前落的旧格式），核对不到它们："
                "这一层的「读过的不重投」只覆盖能归到本工单的落账，缺口如实报，不假装查过就是全查过。"
            )
        if self.inbox_journal is None:
            notes.append(
                "本装配没接耐久收件箱：候选只能来自调用方清单——引擎停机前收到过但没进会话的唤醒，"
                "这一层无从得知（D-48.6 的 `build_dispatcher` 默认已接）。"
            )
        elif journal_items:
            notes.append(
                f"{len(journal_items)} 条候选来自耐久收件箱（磁盘上写着「投进过引擎、没进会话」）："
                "它们不在你给的候选里也被捞回来了。反过来，引擎**停机期间**到达的唤醒磁盘上"
                "没有痕迹，调用方候选这条第二来源不能撤。"
            )
        if journal_stale:
            notes.append(
                f"{len(journal_stale)} 条耐久投递在磁盘上还挂着，但已读账本证明它们早进过会话"
                "（销账那次没落盘）：`resume` 会顺手清掉，`plan` 只点名不动盘。"
            )
        if self._journal_read_error:
            notes.append(self._journal_read_error)
            self._journal_read_error = ""

        return {
            "ok": True,
            "action": "resume" if unread else "nothing-to-resume",
            "resumable": bool(unread),
            "ticketId": tid,
            "prevAssignmentId": prev,
            "title": str(title or ""),
            "unread": unread,
            "alreadyRead": already_read,
            "alreadyInInbox": already_in_box,
            "inProcessWakes": in_process_wakes,
            "carriedWakes": len(unread) + in_process_wakes,
            "evidence": {
                "prevCheckpointKey": prev_key,
                "prevCheckpointExists": prev_checkpoint is not None,
                "prevCheckpointMessages": len((prev_checkpoint or {}).get("messages") or []),
                "prevCheckpointOwner": owner,
                "marksForPrev": len(marks),
                "crossClaimFedKeys": len(fed_arrivals),
                "unattributedMarks": self.ledger.unattributed_marks(),
                "inflightOutboxIds": inflight["runningIds"],
                "queuedOutboxKinds": inflight["queuedKinds"],
                "crashedJobs": crashed_jobs,
                # 耐久收件箱这一路的自证：在箱多少条、本次从中捞回几条、其中几条其实早已读过。
                # 接线方看这三个数就能分清「引擎自己知道」和「只有调用方知道」各占多少。
                "journalPending": journal_pending_total,
                "journalSourced": len(journal_items),
                "journalStale": journal_stale,
                "journalAttached": self.inbox_journal is not None,
                "workspaceDir": str(self.registry.workspaces.path_for(tid, title)),
            },
            "notes": notes,
            "reason": (
                ""
                if unread
                else "候选唤醒全部已进过会话历史（或没有可重投的未读条）：不起空转轮，不调模型也不占槽"
                + (f"；本进程那份已结束领取的箱里还压着 {in_process_wakes} 条，用带 prev 的正常 submit 起新领取就会带走" if in_process_wakes else "")
            ),
            "plannedAtMs": _now_ms(),
        }

    def resume(
        self,
        ticket_id: str,
        *,
        prev_assignment_id: str,
        new_assignment_id: str,
        candidate_wakes: list[Any] | None = None,
        title: str = "",
        origin: str = "human",
        prepare_fn: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        """按计划把**未读**的唤醒投给一次新领取，并起一轮（上下文借旧检查点）。

        顺序是刻意的：先把这一批全部投进箱，再 `kick` 起轮。反过来起轮再投递，后到的
        那几条就赶不上本轮的 `drain_inbox`，得等续跑轮——补查要的是一次带走。"""
        new_aid = str(new_assignment_id or "").strip()
        if not new_aid:
            return self._refuse(
                "need-new-assignment",
                "缺 new_assignment_id：补查是一次**新的领取**（已读落账要记在新领取名下，检查点也按新键命名），"
                "服务端领取回路没给新 id 就没有可绑的归属。",
                ticket_id=str(ticket_id or ""),
                prev_assignment_id=str(prev_assignment_id or ""),
            )
        p = self.plan(
            ticket_id,
            prev_assignment_id=prev_assignment_id,
            candidate_wakes=candidate_wakes,
            title=title,
            origin=origin,
        )
        if not p["ok"]:
            return p
        # 耐久箱的清理：账本证明早已进过会话、盘上却还挂着的那些条，这一路起轮不会再消费
        # 它们（它们不在 unread 里），不在这里清就永远占着——`plan` 只点名，动手在这。
        stale_cleared = self._clear_stale_journal(p)
        if not p["resumable"]:
            return {
                "ok": True,
                "action": "nothing-to-resume",
                "ticketId": p["ticketId"],
                "prevAssignmentId": p["prevAssignmentId"],
                "redelivered": 0,
                "skippedAlreadyRead": len(p["alreadyRead"]),
                "journalSourced": p["evidence"]["journalSourced"],
                "staleJournalCleared": stale_cleared,
                "reason": p["reason"],
                "notes": p["notes"],
                "plan": p,
            }

        tid = p["ticketId"]
        prev = p["prevAssignmentId"]
        # 注记里的「还带着几条」要等于真正会进本轮提示词的条数：将要重投的 + 箱里已压着的。
        note = self.dispatcher.recovery_note_for(
            tid, prev_assignment_id=prev, title=title, carried_wakes=p["carriedWakes"]
        )
        first = p["unread"][0]
        started = self.registry.start_or_wake(
            ticket_id=tid,
            title=title,
            assignment_id=new_aid,
            message=first["text"],
            origin=first["origin"],
            recovery_note=note,
            prepare_fn=prepare_fn,
            arrived_at_ms=first["atMs"],
        )
        if not started.get("ok"):
            return {
                "ok": False,
                "action": "not-resumed",
                "stage": "start_or_wake",
                "ticketId": tid,
                "prevAssignmentId": prev,
                "redelivered": 0,
                "reason": (
                    f"新领取没起，这一批候选一条也没投出去（还在你那边，没有丢）：{started.get('reason')}"
                ),
                "plan": p,
            }
        delivered = [first]
        failed: list[dict[str, Any]] = []
        for wake in p["unread"][1:]:
            got = self.registry.start_or_wake(
                ticket_id=tid,
                title=title,
                assignment_id=new_aid,
                message=wake["text"],
                origin=wake["origin"],
                arrived_at_ms=wake["atMs"],
            )
            if got.get("ok"):
                delivered.append(wake)
            else:
                failed.append({"text": wake["text"], "reason": got.get("reason", "")})
        kicked = self.scheduler.kick(ticket_id=tid, prev_assignment_id=prev)
        scheduled = bool(kicked.get("ok"))
        return {
            "ok": scheduled,
            "action": "resumed" if scheduled else "delivered-not-scheduled",
            "ticketId": tid,
            "assignmentId": new_aid,
            "prevAssignmentId": prev,
            "redelivered": len(delivered),
            "skippedAlreadyRead": len(p["alreadyRead"]),
            "skippedAlreadyInInbox": len(p["alreadyInInbox"]),
            "journalSourced": p["evidence"]["journalSourced"],
            "staleJournalCleared": stale_cleared,
            "carriedWakes": p["carriedWakes"],
            "recoveryNote": note,
            "started": started,
            "kick": kicked,
            "failedDeliveries": failed,
            "notes": p["notes"]
            + (
                []
                if scheduled
                else [
                    f"唤醒已投进新领取 {new_aid} 的收件箱（{len(delivered)} 条），但回合没起："
                    f"{kicked.get('reason') or kicked.get('action')}——箱里的不会丢，"
                    "槽位空了再 kick 一次即可。"
                ]
            )
            + [f"重投失败：{f['reason']}" for f in failed],
            "plan": p,
        }

    # ── 内部 ─────────────────────────────────────────────────

    def _clear_stale_journal(self, plan: dict[str, Any]) -> int:
        """把「账本说读过、盘上还说没进会话」的耐久记录清掉，返回清掉的条数。

        只在 `resume` 里做：`plan` 是查询，读一次盘不该顺带改盘（调用方可能只是想看一眼）。
        清不动不拦补查——落盘失败时原话进 notes，那条记录下次补查仍会被点名，
        最坏是重复点名，不是丢唤醒。"""
        ids = [str(x) for x in (plan.get("evidence") or {}).get("journalStale") or [] if str(x)]
        if self.inbox_journal is None or not ids:
            return 0
        try:
            out = self.inbox_journal.mark_consumed(ids)
        except Exception as exc:  # noqa: BLE001  清理是旁路，炸了也不能拦重投
            plan["notes"].append(f"耐久收件箱清理没做成：{type(exc).__name__}: {exc}（这些条下次补查仍会被点名）")
            return 0
        if not out.get("ok"):
            plan["notes"].append(
                f"耐久收件箱清理没落盘：{out.get('reason')}（这些条下次补查仍会被点名，不会静默消失）"
            )
        return int(out.get("removed") or 0)

    @staticmethod
    def _refuse(action: str, reason: str, *, ticket_id: str = "", prev_assignment_id: str = "") -> dict[str, Any]:
        return {"ok": False, "action": action, "ticketId": ticket_id, "prevAssignmentId": prev_assignment_id, "reason": reason}

    @staticmethod
    def _normalize(candidate: Any, *, prev_assignment_id: str, default_origin: str) -> dict[str, Any]:
        """把候选唤醒还原成**投递时**的形状：已读键要用 (正文, 到达时间, 旧领取) 三元组算才对得上落账。"""
        if isinstance(candidate, dict):
            text = ""
            for key in _TEXT_KEYS:
                if str(candidate.get(key) or "").strip():
                    text = str(candidate[key]).strip()
                    break
            at_ms = int(candidate.get("atMs") or candidate.get("arrivedAtMs") or 0)
            wake_origin = str(candidate.get("origin") or default_origin)
        else:
            text = str(candidate or "").strip()
            at_ms = 0
            wake_origin = default_origin
        wake = {"text": text, "atMs": at_ms, "origin": wake_origin, "assignmentId": prev_assignment_id}
        return {
            "text": text,
            "atMs": at_ms,
            "origin": wake_origin,
            "assignmentId": prev_assignment_id,
            "source": "caller",
            "hasArrival": at_ms > 0,
            "readId": wake_read_id(wake),
            "arrivalKey": wake_arrival_key(wake),
        }

    def _journal_items(self, ticket_id: str, *, caller_items: list[dict], default_origin: str) -> list[dict[str, Any]]:
        """把耐久收件箱里这一单的在箱投递变成候选（与调用方候选去重，D-48.6 的第二来源）。

        去重按「同一条唤醒」的两个认法：已读键（正文+到达时间+领取）相同，或到达键
        （正文+到达时间）相同——后者兜住「同一句话在磁盘上记在旧领取、候选里按 prev 算」
        这种换了领取的情况。留哪份？留调用方那份：它带的是服务端消息表的原文，没有截断。
        """
        if self.inbox_journal is None:
            self._journal_inbox_total = 0
            return []
        try:
            pending = self.inbox_journal.pending_wakes_for_ticket(str(ticket_id))
        except Exception as exc:  # noqa: BLE001  读盘失败不拦补查：候选清单照常核对
            self._journal_inbox_total = 0
            self._journal_read_error = f"耐久收件箱读不出候选：{type(exc).__name__}: {exc}"
            return []
        self._journal_inbox_total = len(pending)
        seen_read = {i["readId"] for i in caller_items}
        seen_arrival = {i["arrivalKey"] for i in caller_items}
        out: list[dict[str, Any]] = []
        for r in pending:
            if r["readId"] in seen_read or r["arrivalKey"] in seen_arrival:
                continue
            r = dict(r)
            r["origin"] = r.get("origin") or default_origin
            out.append(r)
        return out
