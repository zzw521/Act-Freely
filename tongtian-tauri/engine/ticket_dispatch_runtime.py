"""工单执行体的回合循环胶水（缺陷 D-48.5 预备切片）

把 D-48.1/2/3 三个运行时接成一个可测的 **worker 回合循环**：`TicketDispatcher
.wake_and_run` 一次推进一份 Assignment 的一轮——唤醒入队、消费收件箱、跑注入的
execute_turn、外发走「提交后才执行」的副作用事务、已读按步骤落账、检查点每轮
落盘（带 `messages` 契约）、看门狗到点点名处置，最后按口径决定收口/保持/作废。

写死的判定顺序（都不由 Agent 自述，账本说了算）：

1. **恢复优先**：带 `prev_assignment_id` 的续跑先收集证据（旧检查点、副作用账本
   在途/未提交、崩溃时在跑的后台作业、目录是否已有半成品），生成恢复注记塞进
   新绑定——注记没生成就不开始。
2. **回合失败不外发**：execute_turn 返回失败或抛错，本轮入队的副作用事务整体
   rollback；检查点保留为可续跑证据（dropped 收口时不覆盖已落的对话检查点）。
3. **收口口径**：outcome=finished **且** 看门狗确认没有作业在跑，才 end_assignment
   (finished)（删检查点、还槽位）；作业还在跑就保持 working（槽位不还在、
   不上报 waiting）；回合失败走 dropped 留证据；发明第四种结局被拒。
4. **已读落账在消费之后**：唤醒被喂进本轮输入才 mark_read——重启补查用
   `ledger.unread_of` 判断哪条还没进过会话历史，不靠执行体自述。

边界：execute_turn / send_dispatch / kill_job 全部由调用方注入——本模块不碰
模型、不发 HTTP、不起进程；引擎服务端口与 Rust 领取回路只需把真实实现接进
这三个注入点（server.py 正被并行批次修改，故适配器单独排刀）。

`wake_and_run(deliver=False)` 是给**并发调度器**留的续跑轮：唤醒已由调度器投进
收件箱（同工单同一时刻只允许一轮在飞，见 ticket_turn_scheduler），这一轮只消费
收件箱，不重投消息、不新建领取、不占槽；没有活绑定时当场失败，绝不「顺手起一份
新领取」——那会让同一工单裂成两个并行的自己。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

import checkpoint_runtime as cr
import ticket_inbox_runtime as tir
import ticket_recovery_runtime as trr
import ticket_worker_runtime as twr

VALID_OUTCOMES = ("finished", "running")


def _digest(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


def wake_read_id(wake: dict[str, Any]) -> str:
    """唤醒消息的落账键：正文 + 到达时间 + 投递时绑定的领取——同文案重投不误判已读。

    三个字段里任何一个不同就是另一条消息：正文重复但时间不同（用户又发了一遍），
    或时间相同但投给了新的 Assignment（重启后重新交付），都不该被旧落账吃掉。"""
    return "wake-" + _digest(
        "%s|%s|%s" % (wake.get("text", ""), wake.get("atMs", ""), wake.get("assignmentId", ""))
    )


def wake_arrival_key(wake: dict[str, Any]) -> str:
    """跨领取的落账凭据：只取（正文, 到达时间），**不含领取 id**。

    `wake_read_id` 按领取计，换一份领取就从零核对——那是对的（不丢唤醒），但如果补查
    只认它，同一条唤醒在每次重启后都会「对新领取而言是未读」而被重喂一遍。这个摘要给
    `ReadLedger.fed_arrivals` 用，回答的是「这句话在这一单的**任何**一份领取里进过会话
    没有」。"""
    return "arr-" + _digest("%s|%s" % (wake.get("text", ""), wake.get("atMs", "")))


class TicketDispatcher:
    """一单一回合的推进器。execute_turn 约定：

    调用签名 `execute_turn(worker_snapshot, prompt, history) -> dict`，返回：
      ok: bool           回合是否成功（失败触发事务 rollback + dropped 收口）
      messages: list     本轮结束后的对话历史（落检查点，契约要求必须是列表）
      sends: list[dict]  外发动作 {kind, sessionKey?, payload, dedupeKey?}
      jobs: list[dict]   仍在跑的后台作业 {jobId, description, command[, processStarted=False]}
                         （description 是给人看的截断，command 是起进程用的全文；
                         processStarted=False 表示接线没给进程表、只登记计时，没有真进程可杀）
      outcome: str       finished / running（其它值被拒——与工单层三态同纪律）
      reason: str        失败或续跑原因（如实进事件流/查询接口）
    """

    def __init__(
        self,
        *,
        registry: Any,
        outbox: Any,
        ledger: trr.ReadLedger,
        watchdog: trr.JobWatchdog,
        execute_turn: Callable[[dict, str, list], dict],
        send_dispatch: Callable[[dict], Any],
        kill_job: Callable[[dict], Any] | None = None,
        goal: str = "",
        inbox_journal: Any = None,
    ):
        self.registry = registry
        self.outbox = outbox
        self.ledger = ledger
        self.watchdog = watchdog
        self.execute_turn = execute_turn
        self.send_dispatch = send_dispatch
        self.kill_job = kill_job
        self.goal = goal
        #: 耐久收件箱（D-48.6）：本轮把唤醒喂进提示词并落账为已读后，向它销账。
        #: 没接就是旧行为——唤醒只活在内存里，重启后引擎不知道自己收过哪几条。
        self.inbox_journal = inbox_journal
        # 事务序号：毫秒时间戳会同毫秒撞名（一轮跑不完 1ms 是常态），撞上一次
        # 就把这一轮的外发挤进还没提交的事务——那是真错，所以名字里带单调计数。
        self._tx_seq = 0

    def _recovery_note(self, ticket_id: str, *, prev_assignment_id: str, workspace_dir: str, dir_exists: bool, carried_wakes: int = 0) -> str:
        """收集上一轮的证据，生成新 Assignment 的恢复注记（D-48.3 的三条口径都在这里汇入）。

        旧检查点按 **prev_assignment_id** 读，不是按新的 checkpoint_key 读——新领取
        还没有检查点，读它永远是空。"""
        prev_key = twr.checkpoint_task_id(prev_assignment_id) if prev_assignment_id else ""
        data = cr.load_checkpoint(prev_key) if prev_key else None
        saved_at = ""
        messages = 0
        if data:
            saved_ms = int(data.get("savedAtMs") or 0)
            saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_ms / 1000)) if saved_ms else ""
            messages = len(data.get("messages") or [])
        inflight = self.outbox.inflight_for(ticket_id)
        return trr.build_recovery_note(
            ticket_id=ticket_id,
            prev_assignment_id=prev_assignment_id,
            workspace_dir=workspace_dir,
            dir_exists=dir_exists,
            checkpoint_saved_at=saved_at,
            checkpoint_messages=messages,
            inflight_outbox_ids=inflight["runningIds"],
            pending_outbox_kinds=inflight["queuedKinds"],
            crashed_jobs=self.watchdog.crashed_job_descriptions(ticket_id),
            carried_wakes=carried_wakes,
        )

    def recovery_note_for(
        self,
        ticket_id: str,
        *,
        prev_assignment_id: str,
        title: str = "",
        carried_wakes: int | None = None,
    ) -> str:
        """公开的恢复注记入口：接线方（并发调度器、重启补查）先投递再跑轮时，用它拿到注记。

        与 `_recovery_note` 同一条判定——只在「带着旧领取来续跑、且当前没有活绑定」时
        才生成；已有活绑定时返回空串（那份领取的注记早就绑过了，不该每轮重播一遍）。

        `carried_wakes` 是给**重启补查**留的口子：新进程的收件箱是空的，将要随新领取
        一起带走的唤醒还躺在调用方（服务端工单消息表）那边。不传就按本地箱里已有的条数
        算；传了就如实按将携带的条数点名——注记里的条数必须和真正会进 prompt 的条数一致，
        否则执行体会以为「少了几条已经处理过了」。"""
        worker0 = self.registry.worker(ticket_id)
        live = worker0 is not None and worker0.assignment_id and worker0.ended_status == ""
        if not prev_assignment_id or live:
            return ""
        path = self.registry.workspaces.path_for(ticket_id, title)
        carried = len(worker0.inbox) if worker0 is not None else 0
        if carried_wakes is not None:
            carried = max(int(carried_wakes), carried)
        return self._recovery_note(
            str(ticket_id),
            prev_assignment_id=prev_assignment_id,
            workspace_dir=str(path),
            dir_exists=path.exists(),
            carried_wakes=carried,
        )

    def wake_and_run(
        self,
        *,
        ticket_id: str,
        title: str = "",
        assignment_id: str = "",
        message: str = "",
        origin: str = "human",
        prepare_fn: Callable[[Any], None] | None = None,
        prev_assignment_id: str = "",
        deliver: bool = True,
        arrived_at_ms: int = 0,
    ) -> dict[str, Any]:
        worker0 = self.registry.worker(ticket_id)
        live_binding = worker0 is not None and worker0.assignment_id and worker0.ended_status == ""
        recovery_note = ""
        # 续跑轮也要带 prev_assignment_id：调度器可能已经替它把绑定建好（那份旧领取的
        # 对话上下文只能按旧键读），少这一句就是「注记有了、上下文没接上」的半截恢复。
        # 这里**不能**再按「有没有活绑定」设卡：调度器与重启补查都是先投递、再起轮，
        # 走到这一步活绑定必然在——那一层判断原本由下面 `if checkpoint is not None`
        # 承担（新领取自己的检查点优先，读不到才借旧的那份），加卡只会把注记承诺的
        # 上下文在真接线上静默丢掉（D-48.5g 探针实测：走 submit 的续跑 historyMessages 恒 0）。
        prev_key = twr.checkpoint_task_id(prev_assignment_id) if prev_assignment_id else ""
        if not deliver:
            # 续跑轮：唤醒已由调用方（并发调度器）投进收件箱，这一轮只负责把它喂进会话。
            # 不重投同一条、不新建领取、不占槽——没有活绑定就当场停，不猜「该给谁跑」。
            if not live_binding:
                return {
                    "ok": False,
                    "stage": "advance",
                    "reason": (
                        f"续跑轮（deliver=False）要落在工单 {ticket_id} 的活领取上，当前没有——"
                        "本轮不新建领取、不占槽；要起新回合请按正常投递带上 assignment_id 与 message。"
                    ),
                }
        else:
            if prev_assignment_id and not live_binding:
                recovery_note = self.recovery_note_for(
                    ticket_id, prev_assignment_id=prev_assignment_id, title=title
                )
            started = self.registry.start_or_wake(
                ticket_id=ticket_id,
                title=title,
                assignment_id=assignment_id,
                message=message,
                origin=origin,
                recovery_note=recovery_note,
                prepare_fn=prepare_fn,
                arrived_at_ms=arrived_at_ms,
            )
            if not started["ok"]:
                return {"ok": False, "stage": "start_or_wake", "reason": started["reason"]}
        worker = self.registry.worker(ticket_id)
        wakes = self.registry.drain_inbox(ticket_id)
        self.registry.set_run_state(ticket_id, "running")

        history: list = []
        history_source = "none"
        checkpoint = cr.load_checkpoint(worker.checkpoint_key) if worker.checkpoint_key else None
        if checkpoint is not None:
            history = list(checkpoint.get("messages") or [])
            history_source = "assignment"
        elif prev_key:
            # 本轮是续跑：新 Assignment 名下还没有检查点，对话上下文只能读旧的那份。
            # 恢复注记已写明「属于旧领取，不能当本轮已做完」——这里只借上下文，不借结论。
            carried = cr.load_checkpoint(prev_key)
            if carried is not None:
                history = list(carried.get("messages") or [])
                history_source = "prev-assignment"
        prompt = self._build_prompt(wakes, worker.recovery_note)
        turn: dict[str, Any] = {}
        turn_error = ""
        try:
            turn = dict(self.execute_turn(worker.snapshot(), prompt, history) or {})
        except Exception as exc:  # noqa: BLE001  回合炸了走失败口径，不带着半截状态外发
            turn_error = f"{type(exc).__name__}: {exc}"
            turn = {"ok": False, "outcome": "finished", "messages": history, "sends": [], "jobs": [], "reason": turn_error}

        outcome = str(turn.get("outcome") or "")
        if turn.get("ok") and outcome not in VALID_OUTCOMES:
            # 第四种结局不采信：不猜它「大概干完了」，也不猜它「还在跑」——
            # 按失败口径走：外发作废、检查点保留，缺口在 reason 里说清楚。
            turn = {
                **turn,
                "ok": False,
                "reason": f"回合返回了未知结局「{outcome}」，只接受 {'/'.join(VALID_OUTCOMES)}——本轮不采信，外发作废、检查点保留",
            }
            turn_error = str(turn["reason"])
            outcome = ""

        # 已读落账：唤醒被喂进本轮输入这一事实发生在这里，不是执行体自述
        read_ids = [
            self.ledger.mark_read(
                worker.assignment_id,
                wake_read_id(w),
                step_id=f"{assignment_id}-turn",
                ticket_id=ticket_id,
                arrival_key=wake_arrival_key(w),
            )
            for w in wakes
        ]

        # 作业登记（续跑口径的输入之一，必须在收口判定前）。
        # 注意 turn["jobs"] 里也可能有「登记了但没起进程」的那类（进程表为 None 的中间态，
        # 条目带 processStarted=False）：它照样要计时，到点由 kill_fn 如实报「没有句柄」，
        # 而不是悄悄从看门狗里消失。
        for job in turn.get("jobs") or []:
            self.watchdog.register(str(job.get("jobId") or ""), description=str(job.get("description") or ""), ticket_id=ticket_id)
        killed = self.watchdog.tick(kill_fn=self.kill_job)
        kill_lines = self.watchdog.kill_report_lines(killed)

        # 检查点每轮落盘（messages 契约由调用方保证，这里兜底成列表）
        messages = turn.get("messages")
        if not isinstance(messages, list):
            messages = list(history)
        stats = {"wakesConsumed": len(wakes), "readsMarked": sum(1 for r in read_ids if r.get("ok"))}
        notes: list[str] = []

        # 耐久收件箱销账：与已读落账同一批唤醒，且**排在已读之后**（D-48.6 的顺序要害）。
        # 反过来做会留一个窗口：箱里说「没投过」、已读账本里也还没这条——那条唤醒在重启
        # 补查眼里两头都不存在，等于丢。现在的次序最坏是「两边都说读过」，重投由幂等键挡下。
        # 键与 `wake_read_id` / `wake_arrival_key` 同一条算式（前缀不同），两边都对得上。
        if self.inbox_journal is not None and wakes:
            cleared = self.inbox_journal.mark_consumed(
                [tir.wake_journal_id(w) for w in wakes],
                [tir.arrival_journal_key(w) for w in wakes],
            )
            stats["journalCleared"] = int(cleared.get("removed") or 0)
            stats["journalMissing"] = len(cleared.get("missing") or [])
            if not cleared.get("ok"):
                notes.append(f"耐久收件箱销账没落盘：{cleared.get('reason')}")
            if stats["journalMissing"]:
                # 销不掉不等于出错：本轮消费的唤醒在磁盘上没有对应记录是正常的——那条是
                # **补查重投**投的（旧记录已按到达键一起清掉），或这份装配根本没接耐久箱。
                # 报出来是为了「箱里越积越多」这种缺陷当场可见，不是为了让绿变得难。
                notes.append(
                    f"耐久收件箱有 {stats['journalMissing']} 条按本轮键找不到对应投递：仍按已读处理，"
                    "但请核对面是不是投递侧没落盘（journalWarning）"
                )
        checkpoint_saved = False
        if turn.get("ok"):
            saved, err = cr.save_checkpoint(
                worker.checkpoint_key,
                {
                    "messages": messages,
                    # 归属自证：重启补查要核对「这份旧检查点确实是这条工单的」——
                    # 检查点键只由 Assignment id 摘要决定，键里看不出工单，给错 prev
                    # 就会把别的工单的对话接进本轮（D-48.5g 的 wrong-owner 拒绝靠它）。
                    "ticketId": ticket_id,
                    "assignmentId": worker.assignment_id,
                    "resumable": outcome == "running",
                    "reason": str(turn.get("reason") or ""),
                    "stats": stats,
                },
            )
            checkpoint_saved = saved
            if not saved:
                notes.append(f"本轮检查点没有落盘：{err}（续跑证据缺失，收口仍按回合结果处理）")

        # 外发：事务内入队；回合成功才 commit（提交后才执行），失败整体 rollback
        self._tx_seq += 1
        tx = f"turn-{assignment_id}-{int(time.time() * 1000)}-{self._tx_seq}"
        sends = list(turn.get("sends") or [])
        outbox_summary: dict[str, Any] = {"dispatched": 0, "succeeded": 0, "failed": 0, "reclaimed": 0, "blocked": 0, "reasons": []}
        rolled_back = 0
        sends_enqueued = 0
        sends_deduped = 0
        sends_rejected = 0
        if sends:
            begin = self.outbox.begin(tx)
            if not begin.get("ok"):
                # 事务没开成 = 一条也没记账；继续 enqueue 只会得到一串「未知事务」，
                # 噪声盖住真正的原话，所以这里直接停，并把每条都算成未授权。
                notes.append(f"本轮外发事务没开成：{begin.get('reason')}（一条也没记账，动作从未发生）")
                sends_rejected = len(sends)
            else:
                for send in sends:
                    enq = self.outbox.enqueue(
                        kind=str(send.get("kind") or ""),
                        ticket_id=ticket_id,
                        session_key=str(send.get("sessionKey") or ""),
                        payload=send.get("payload"),
                        tx_id=tx,
                        dedupe_key=str(send.get("dedupeKey") or ""),
                    )
                    if enq.get("ok") and not enq.get("deduped"):
                        sends_enqueued += 1
                    elif enq.get("ok"):
                        sends_deduped += 1
                        notes.append(
                            f"外发「{send.get('kind')}」被幂等键挡下：{enq.get('note')}——账本没有多排一条，"
                            "同键的那条已经在队列里。"
                        )
                    else:
                        sends_rejected += 1
                        notes.append(
                            f"外发「{send.get('kind')}」没有记进账本：{enq.get('reason')}——这条动作从未被授权，"
                            "要重做请显式开新事务，不存在「静默丢弃」。"
                        )
                if turn.get("ok"):
                    self.outbox.commit(tx)
                    outbox_summary = self.outbox.run_pending(self.send_dispatch)
                else:
                    rb = self.outbox.rollback(tx)
                    rolled_back = int(rb.get("aborted") or 0)

        # 收口：finished 且无在跑作业才还槽删点；作业在跑保持 working；失败 dropped 留证据
        end_result: dict[str, Any] = {}
        state_after = self.watchdog.turn_done_state(ticket_id)
        if not turn.get("ok"):
            end_result = self.registry.end_assignment(ticket_id, "dropped", reason=str(turn.get("reason") or turn_error or "回合失败"))
        elif outcome == "finished" and state_after == "waiting":
            end_result = self.registry.end_assignment(ticket_id, "finished", reason=str(turn.get("reason") or ""))
        else:
            self.registry.set_run_state(ticket_id, "idle")

        return {
            "ok": bool(turn.get("ok")),
            "stage": "done",
            "ticketId": ticket_id,
            "assignmentId": worker.assignment_id,
            "checkpointKey": worker.checkpoint_key,
            "checkpointSaved": checkpoint_saved,
            "historySource": history_source,
            "historyMessages": len(history),
            "recoveryNote": worker.recovery_note,
            "wakesConsumed": len(wakes),
            "readsMarked": stats["readsMarked"],
            "sendsQueued": sends_enqueued,
            "sendsDeduped": sends_deduped,
            "sendsRejected": sends_rejected,
            "sendsRolledBack": rolled_back,
            "outbox": outbox_summary,
            "killedJobs": kill_lines,
            "notes": notes,
            "doneState": state_after,
            "heldWorking": bool(turn.get("ok") and outcome == "finished" and state_after == "working"),
            "ended": end_result.get("status", "") if end_result.get("ok") else "",
            "reason": str(turn.get("reason") or ""),
        }

    @staticmethod
    def _build_prompt(wakes: list[dict[str, Any]], recovery_note: str) -> str:
        parts: list[str] = []
        if recovery_note:
            parts.append(recovery_note)
            parts.append("")
        if wakes:
            parts.append("本轮收到的唤醒（按到达顺序，都已落账为已读）：")
            for i, wake in enumerate(wakes, 1):
                parts.append(f"{i}. [{wake.get('origin', '?')} @ {wake.get('atMs', '')}] {wake.get('text', '')}")
        else:
            # 续跑轮（调度器按收件箱还有货才 kick，理论上不会空）：空也必须说清楚，
            # 不能让模型看到一句「收到的唤醒：」后面什么都没有。
            parts.append("本轮没有新到达的唤醒——这是上一轮的续跑：把没收口的活做完，或如实宣告结局。")
        return "\n".join(parts)

    def state_of(self, ticket_id: str) -> dict[str, Any]:
        """接线方查询这一单当前对外该报什么：worker 快照 + 看门狗 + 队列缺口，一处凑齐。

        `external` 只有 working / waiting / free 三档：还有活绑定且（回合在跑 **或**
        后台作业在跑）就是 working——D-48.3 的「回合结束但作业没结束就不许上报
        waiting」在这里落地，适配器不必自己拼判定。"""
        worker = self.registry.worker(ticket_id)
        if worker is None:
            # 「这单现在挂着什么」在注册表为空时同样要能回答——耐久收件箱是磁盘件，
            # 进程刚重启、还没有活绑定的那段时间里，它是唯一说得清「收到过但没进会话」的地方。
            return {
                "ticketId": str(ticket_id),
                "external": "free",
                "reason": "这个工单在本进程没有执行体",
                "journalPending": len(self.inbox_journal.pending_for_ticket(str(ticket_id)))
                if self.inbox_journal is not None
                else 0,
            }
        live = bool(worker.assignment_id and not worker.ended_status)
        running_jobs = self.watchdog.running_for(ticket_id)
        inflight = self.outbox.inflight_for(ticket_id)
        if not live:
            external = "free"
        elif worker.run_state == "running" or running_jobs:
            external = "working"
        else:
            external = "waiting"
        return {
            "ticketId": str(ticket_id),
            "external": external,
            "assignmentId": worker.assignment_id,
            "runState": worker.run_state,
            "endedStatus": worker.ended_status,
            "queuedWakes": len(worker.inbox),
            "runningJobs": [j["description"] for j in running_jobs],
            "outboxInflight": len(inflight["runningIds"]),
            "outboxUncommitted": len(inflight["queuedKinds"]),
            "unreadWakes": self.ledger.unread_of(
                worker.assignment_id, [wake_read_id(w) for w in worker.inbox]
            ) if live else [],
            "checkpointKey": worker.checkpoint_key,
            # 耐久收件箱里这一单还挂着几条「投进过引擎但没进会话」。与 queuedWakes 不同：
            # 那份是内存箱（本轮就能吃到），这份是磁盘凭据——本进程没绑定也能查到，
            # 所以重启后「引擎自己知道自己收过什么」不必等调用方把候选送回来。
            "journalPending": len(self.inbox_journal.pending_for_ticket(str(ticket_id)))
            if self.inbox_journal is not None
            else 0,
        }
