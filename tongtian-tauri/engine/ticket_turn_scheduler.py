"""工单回合的并发调度（缺陷 D-48.5d）

`TicketDispatcher.wake_and_run` 一次推进**一轮**，`ticket_worker_adapter` 给了
三个注入点的真实实现，但**没有任何人保证同一工单同时只跑一轮**。引擎服务端口是
并发的（FastAPI 在一个事件循环上收多个唤醒；同步回合真实接线时还会被丢进线程池
执行），两个线程对同一工单各调一次 `wake_and_run` 就是两份检查点互相覆盖、两条
外发事务交错、同一个 Assignment 被当成两个回合的自述——D-48 要的「工单级隔离」
在并发下会当场裂开。本层补的就是这个缺口，四件事：

1. **同工单单飞**：同一工单同一时刻只允许一轮在飞。单飞位（claim）的检查与释放
   都在同一把锁里，且**投递先于复查 claim**——这个顺序保证**不丢唤醒**：调用方先把
   唤醒投进收件箱，再看有没有人在飞；若投递之后单飞位刚好被释放（在飞那轮的
   「读收件箱 + 释放」是同一把锁里的原子动作，必然看到这条投递），它就自己接管并
   起一轮，绝不让唤醒压在箱里没人管。
2. **跨工单真并行**：回合跑在调度器自己的线程上（`max_parallel_turns` 上限，与
   D-48.1 槽位池默认 3+1 对齐）。真正的准入裁决仍是**槽位池**，而且它在 `submit`
   里同步完成——被槽位池拒掉的唤醒**当场**带着理由回到调用方，不会先答一句「已排程」
   再在线程里悄悄失败；线程上限只约束本进程同时开跑的回合数，不替代槽位口径。
3. **续跑由收件箱驱动**：一轮跑完后，若这份领取还活着且收件箱还有货，就在**同一次
   claim 内**再推进一轮（`wake_and_run(deliver=False)`——不重投同一条消息、不新建
   领取、不占槽）。续跑轮有上限（`max_followup_turns`）：到顶就松手并**如实报告**
   还剩几条、为什么没接着跑，不为清队列无限轮下去（那是白烧模型额度）。
4. **领取已结束时的滞留唤醒点名**：回合以 finished/dropped 收口后，在飞期间到达的
   唤醒会留在收件箱里——这份领取已经结束，它不能再消费它们。调度器把这种情况记进
   `snapshot()["strandedWakes"]`（条数 + 结束态 + 原因），说明「要一次新的领取
   （带 prev_assignment_id 续跑）才会被消费」；下一次正常投递确实会把这批一起带走，
   恢复注记里的 `carried_wakes` 就是这件事的账。

边界：本层不调模型、不发 HTTP、不起进程，也不新建第二套状态——它只持有
`TicketDispatcher`（连同它的 registry / outbox / ledger / watchdog），回合的全部
判定仍在 dispatcher 与三个运行时里。调度器自己的账（claim、历史、滞留、错误）
只回答「谁在跑、跑了几轮、有没有丢」，不回答「活干得对不对」。

线程里抛出的异常**不吞**：记进 `snapshot()["errors"]` 并当场释放单飞位——一个炸掉
的回合不能让槽位和单飞锁永远悬着。「在飞」的事实来源是 claim，不是线程对象，
所以 `wait_idle` / `shutdown` 都以 claim 为准；回合线程是 daemon：进程退出时不等
人，但检查点与副作用账本早已落盘，留下的缺口由重启恢复注记点名（D-48.3）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

#: 同时进行的回合数上限：与 D-48.1 槽位池默认（3 个 Agent + 1 个永久留给人）对齐——
#: 线程不该比槽位多，否则多出来的回合只是在等一个本来就轮不到的位置。
DEFAULT_MAX_PARALLEL_TURNS = 4

#: 一次 claim 内最多连推几轮（首轮 + 续跑轮）。到顶就松手如实报告，不清空收件箱。
DEFAULT_MAX_FOLLOWUP_TURNS = 8

#: 回合历史保留条数（查询接口用，不是账本；超了丢最老的）
HISTORY_KEEP = 200


def _now_ms() -> int:
    return int(time.time() * 1000)


class _InlineThread:
    """`thread_factory` 的假实现之一：`start()` 当场把目标函数跑完。

    接线侧不需要它；自测用它把「多工单交叠」变成可断言的确定序（不需要真并发）。"""

    def __init__(self, *, target, kwargs=None, name=""):
        self._target = target
        self._kwargs = kwargs or {}
        self.name = name
        self._alive = False

    def start(self) -> None:
        self._alive = True
        try:
            self._target(**self._kwargs)
        finally:
            self._alive = False

    def join(self, timeout: float | None = None) -> None:
        return None

    @property
    def is_alive(self) -> bool:  # noqa: D102  与 threading.Thread 的同名属性对齐
        return self._alive


def inline_thread_factory(*, target=None, kwargs=None, name=""):
    """给 `TicketTurnScheduler(thread_factory=...)` 用的同步执行工厂。"""
    return _InlineThread(target=target, kwargs=kwargs, name=name)


class TicketTurnScheduler:
    """工单回合的单飞 + 有界并行调度器。

    `submit(...)` 是唯一的投递入口（引擎端点应当只调它）：调用方拿到的是**这条唤醒
    去哪了**的即时答复，不是「活干完了没」——回合结果去 `history()` / `state_of()` 看。
    """

    def __init__(
        self,
        dispatcher: Any,
        *,
        max_parallel_turns: int = DEFAULT_MAX_PARALLEL_TURNS,
        max_followup_turns: int = DEFAULT_MAX_FOLLOWUP_TURNS,
        thread_factory: Callable[..., Any] = threading.Thread,
        history_keep: int = HISTORY_KEEP,
    ):
        if int(max_parallel_turns) < 1:
            raise ValueError("max_parallel_turns 至少为 1——0 个回合位等于调度器不推进任何工单")
        if int(max_followup_turns) < 1:
            raise ValueError("max_followup_turns 至少为 1（首轮也算在内）")
        self.dispatcher = dispatcher
        self.registry = dispatcher.registry
        self.max_parallel_turns = int(max_parallel_turns)
        self.max_followup_turns = int(max_followup_turns)
        self._thread_factory = thread_factory
        self._cap = threading.BoundedSemaphore(self.max_parallel_turns)
        self._lock = threading.Lock()
        self._claims: dict[str, dict[str, Any]] = {}
        self._history: deque = deque(maxlen=max(int(history_keep), 1))
        self._errors: list[str] = []
        self._stranded: dict[str, dict[str, Any]] = {}
        self._deferred: dict[str, dict[str, Any]] = {}
        self._turns_run = 0

    # ── 对外：投递并推进 ─────────────────────────────────────

    def submit(
        self,
        *,
        ticket_id: str,
        message: str,
        assignment_id: str = "",
        title: str = "",
        origin: str = "human",
        prev_assignment_id: str = "",
        prepare_fn: Callable[[Any], None] | None = None,
        arrived_at_ms: int = 0,
    ) -> dict[str, Any]:
        """投一条唤醒，并保证它最终被某一轮消费——要么在飞那轮的续跑，要么新起一轮。

        **投递与槽位裁决都在本调用里同步完成**（都发生在 `registry.start_or_wake`），
        所以调用方拿到的答复不会说谎：`action` 只有三种——
        - `turn-scheduled`：唤醒已进箱，本调用抢到这份工单的单飞位，回合线程会消费它。
        - `deliver-only`：唤醒已进箱，但同工单已有一轮在飞，**不起第二轮**；在飞那轮
          收尾时会在同一把锁下看到它并续跑。
        - `rejected`：参数为空或**槽位池拒了**——这条唤醒哪儿也没去（含被拒理由），
          调度器不留悬着的单飞位、不起空线程。

        `arrived_at_ms` 请带**消息在源头真实到达的时刻**（服务端消息表 / IM 通道给的），
        不要图省事让引擎自己盖时间戳：已读落账的键是（正文+到达时间+领取），重启补查在
        另一个进程里拿同一张表重投时对不上这个键，就会把读过的再喂一遍——引擎时钟盖的
        那条时刻，服务端那边根本没有。"""
        tid = str(ticket_id or "").strip()
        if not tid:
            return {"ok": False, "action": "rejected", "reason": "ticket_id 为空——调度器不知道该把唤醒给谁"}
        text = str(message or "").strip()
        if not text:
            return {"ok": False, "action": "rejected", "reason": "唤醒正文为空：空消息不进收件箱，也不起回合"}
        aid = str(assignment_id or "").strip()
        if not aid:
            return {
                "ok": False,
                "action": "rejected",
                "ticketId": tid,
                "reason": (
                    "缺 assignment_id——新领取要按 Assignment 命名检查点（D-48.1），"
                    "没有它这份唤醒落不到任何一份领取上。"
                ),
            }

        # **先投递，再抢单飞位**：投递走注册表的 `start_or_wake`，槽位裁决就在这一句里
        # ——所以「槽位占满被拒」当场回到调用方，而不是先回一句「已排程」再在线程里失败。
        # 恢复注记也在这里生成（只有带着旧领取、且当前没有活绑定时才有内容）。
        note = ""
        if prev_assignment_id:
            note = self.dispatcher.recovery_note_for(tid, prev_assignment_id=prev_assignment_id, title=title)
        started = self.registry.start_or_wake(
            ticket_id=tid,
            title=title,
            assignment_id=aid,
            message=text,
            origin=origin,
            recovery_note=note,
            prepare_fn=prepare_fn,
            arrived_at_ms=arrived_at_ms,
        )
        if not started.get("ok"):
            return {
                "ok": False,
                "action": "rejected",
                "ticketId": tid,
                "stage": "start_or_wake",
                "reason": f"唤醒没有被投递：{started.get('reason')}",
            }

        # 投递之后才查单飞位：单飞位的「读收件箱 + 释放」是一个原子动作，它一旦释放
        # 就必然看到这次投递（投递在它之前）。反过来，若位还在，说明在飞那轮的收尾还没
        # 发生，它会接手这条唤醒——所以这里既不丢唤醒、也不会起重合的轮。
        with self._lock:
            if tid in self._claims:
                return {
                    "ok": True,
                    "action": "deliver-only",
                    "ticketId": tid,
                    "assignmentId": started.get("assignmentId", ""),
                    "delivered": started.get("delivered", ""),
                    "queuedWakes": started.get("queuedWakes", 0),
                    "note": (
                        "同工单已有一轮在飞：这条唤醒只进收件箱，不另起一轮——"
                        "由在飞那轮的续跑轮消费（上限内）。"
                    ),
                }
            self._claims[tid] = self._new_claim(started.get("assignmentId", "") or aid)

        return self._start_job(
            tid,
            {"ticket_id": tid, "deliver": False, "prev_assignment_id": prev_assignment_id},
            action="turn-scheduled",
            name=f"ticket-turn-{tid}",
            started=started,
        )

    def kick(self, *, ticket_id: str, prev_assignment_id: str = "") -> dict[str, Any]:
        """显式要求推进一轮（不投新唤醒）：给「上一轮到上限没清完」或重启补查用。

        只有这份领取还活着、收件箱还有货、且没有别的回合在飞时才起线程，
        否则如实说明为什么不动——不起空转轮（那等于白烧一次模型调用）。

        `prev_assignment_id` 是**重启补查**带来的旧领取：这一轮要按旧键借对话上下文
        （新领取自己还没有检查点）。不传就是本进程内的普通续跑——上下文来自自己那份检查点。"""
        tid = str(ticket_id or "").strip()
        if not tid:
            return {"ok": False, "reason": "ticket_id 为空"}
        pending = self._pending_wakes(tid)
        if not pending:
            return {
                "ok": False,
                "ticketId": tid,
                "reason": "收件箱是空的：没有要推进的活，不起空转轮（要投递请用 submit）。",
            }
        if not self._is_live(tid):
            return {"ok": False, "ticketId": tid, "queuedWakes": pending, "reason": self._no_live_reason(tid, pending)}
        with self._lock:
            if tid in self._claims:
                return {
                    "ok": True,
                    "action": "already-in-flight",
                    "ticketId": tid,
                    "queuedWakes": pending,
                    "reason": "这份工单已有一轮在飞，续跑由它自己收尾时接手——kick 不插队。",
                }
            self._claims[tid] = self._new_claim(self._live_assignment(tid))
        return self._start_job(
            tid,
            {"ticket_id": tid, "deliver": False, "prev_assignment_id": str(prev_assignment_id or "")},
            action="kick-scheduled",
            name=f"ticket-kick-{tid}",
        )

    def wait_idle(self, *, timeout_seconds: float = 30.0, poll_seconds: float = 0.005) -> dict[str, Any]:
        """等到没有工单持有单飞位为止（自测与端点自检用）。超时不报错，只如实报。"""
        deadline = time.monotonic() + float(timeout_seconds)
        started_ms = _now_ms()
        while True:
            with self._lock:
                inflight = sorted(self._claims)
            if not inflight:
                return {"ok": True, "timedOut": False, "inFlight": [], "waitedMs": _now_ms() - started_ms}
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "timedOut": True,
                    "inFlight": inflight,
                    "reason": f"等待 {timeout_seconds}s 后这些工单仍有一轮在飞：{'、'.join(inflight)}",
                }
            time.sleep(poll_seconds)

    # ── 对外：查询 ────────────────────────────────────────────

    def history(self, ticket_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if not ticket_id:
            return items
        return [h for h in items if h.get("ticketId") == str(ticket_id)]

    def snapshot(self) -> dict[str, Any]:
        """接线方的面包屑：谁在飞、跑了几轮、还有几条压着、哪里丢过球，一次看全。"""
        with self._lock:
            inflight = [
                {
                    "ticketId": tid,
                    "assignmentId": claim["assignmentId"],
                    "turns": claim["turns"],
                    "claimedAtMs": claim["claimedAtMs"],
                    "queuedWakes": self._pending_wakes(tid),
                    "live": self._is_live(tid),
                }
                for tid, claim in sorted(self._claims.items())
            ]
            return {
                "maxParallelTurns": self.max_parallel_turns,
                "maxFollowupTurns": self.max_followup_turns,
                "turnsRun": self._turns_run,
                "inFlight": inflight,
                "strandedWakes": [
                    {"ticketId": tid, **report} for tid, report in sorted(self._stranded.items())
                ],
                "deferred": [{"ticketId": tid, **report} for tid, report in sorted(self._deferred.items())],
                "errors": list(self._errors),
            }

    def state_of(self, ticket_id: str) -> dict[str, Any]:
        """工单视角：调度器（谁在跑）+ dispatcher.state_of（对外该报什么状态）。

        端点只需要这一个调用就能回答「这单现在在做什么、还有没有位置」；两层的判定
        各归各处，不在这里互相覆盖。"""
        tid = str(ticket_id or "")
        with self._lock:
            claim = self._claims.get(tid)
            scheduler = {
                "inFlight": claim is not None,
                "assignmentId": (claim or {}).get("assignmentId", ""),
                "turns": (claim or {}).get("turns", 0),
                "queuedWakes": self._pending_wakes(tid),
                "stranded": dict(self._stranded.get(tid) or {}),
                "deferred": dict(self._deferred.get(tid) or {}),
            }
        return {"ticketId": tid, "scheduler": scheduler, "worker": self.dispatcher.state_of(tid)}

    def shutdown(self, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """尽力收尾：等所有单飞位释放。超时如实报还剩谁在飞——不强杀回合。"""
        waited = self.wait_idle(timeout_seconds=timeout_seconds)
        with self._lock:
            stranded = [{"ticketId": tid, **report} for tid, report in sorted(self._stranded.items())]
        return {
            "ok": not waited["inFlight"],
            "inFlight": waited["inFlight"],
            "timedOut": bool(waited.get("timedOut")),
            "strandedWakes": stranded,
            "reason": waited.get("reason", ""),
        }

    # ── 内部 ─────────────────────────────────────────────────

    @staticmethod
    def _new_claim(assignment_id: str) -> dict[str, Any]:
        return {"assignmentId": str(assignment_id or ""), "claimedAtMs": _now_ms(), "turns": 0}

    def _start_job(
        self,
        tid: str,
        first_args: dict[str, Any],
        *,
        action: str,
        name: str,
        started: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        thread = self._thread_factory(target=self._guarded_job, kwargs={"ticket_id": tid, "first_args": first_args}, name=name)
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001  起不了线程 = 回合从未开始，退回单飞位
            with self._lock:
                self._claims.pop(tid, None)
                self._errors.append(f"回合线程起不来（工单 {tid}）：{type(exc).__name__}: {exc}")
            pending = self._pending_wakes(tid)
            return {
                "ok": False,
                "action": "rejected",
                "ticketId": tid,
                "queuedWakes": pending,
                "reason": (
                    f"唤醒已进收件箱，但回合线程起不来：{type(exc).__name__}: {exc}——单飞位已退回，"
                    f"这条没有被丢（箱里还压着 {pending} 条），下次 submit 或 kick 会把它带走。"
                ),
            }
        return {
            "ok": True,
            "action": action,
            "ticketId": tid,
            "assignmentId": (started or {}).get("assignmentId", "") or self._live_assignment(tid),
            "delivered": (started or {}).get("delivered", ""),
            "queuedWakes": (started or {}).get("queuedWakes", self._pending_wakes(tid)),
            "parallelTurnCap": self.max_parallel_turns,
            "note": (
                "唤醒已投递并交给本工单的回合线程（首轮由它消费收件箱）。同工单在跑期间再来的"
                "唤醒只进收件箱，由续跑轮消费——同一工单同时只允许一轮。"
            ),
        }

    def _pending_wakes(self, ticket_id: str) -> int:
        worker = self.registry.worker(ticket_id)
        return len(worker.inbox) if worker is not None else 0

    def _is_live(self, ticket_id: str) -> bool:
        worker = self.registry.worker(ticket_id)
        return bool(worker is not None and worker.assignment_id and not worker.ended_status)

    def _live_assignment(self, ticket_id: str) -> str:
        worker = self.registry.worker(ticket_id)
        if worker is None or not worker.assignment_id or worker.ended_status:
            return ""
        return str(worker.assignment_id)

    def _ended_status(self, ticket_id: str) -> str:
        worker = self.registry.worker(ticket_id)
        return str(worker.ended_status) if worker is not None else ""

    def _no_live_reason(self, ticket_id: str, pending: int) -> str:
        ended = self._ended_status(ticket_id)
        return (
            f"这份领取已{'以 ' + ended + ' 结束' if ended else '不存在'}，收件箱里 {pending} 条唤醒没有活领取能消费它——"
            "续跑轮只落在活领取上；要消费这批唤醒请用 submit 起一次新的领取"
            "（带 prev_assignment_id，恢复注记会把它们按 carried_wakes 点名）。"
        )

    def _guarded_job(self, *, ticket_id: str, first_args: dict[str, Any]) -> None:
        try:
            self._run_rounds(ticket_id=ticket_id, first_args=first_args)
        except Exception as exc:  # noqa: BLE001  线程里的异常不吞：记账 + 释放单飞位
            with self._lock:
                self._claims.pop(ticket_id, None)
                self._errors.append(f"回合线程异常（工单 {ticket_id}）：{type(exc).__name__}: {exc}")

    def _run_rounds(self, *, ticket_id: str, first_args: dict[str, Any]) -> None:
        rounds = 0
        with self._cap:  # 跨工单并行上限：挡的是「同时开跑」，不是唤醒的投递
            while True:
                args = dict(first_args) if rounds == 0 else {"ticket_id": ticket_id, "deliver": False}
                result = self.dispatcher.wake_and_run(**args)
                rounds += 1
                with self._lock:
                    self._turns_run += 1
                    consumed = int(result.get("wakesConsumed") or 0)
                    self._history.append(
                        {
                            "ticketId": ticket_id,
                            "round": rounds,
                            "assignmentId": result.get("assignmentId", ""),
                            "ok": bool(result.get("ok")),
                            "ended": result.get("ended", ""),
                            "doneState": result.get("doneState", ""),
                            "wakesConsumed": consumed,
                            # 上下文来自哪一份检查点：本轮是从自己这份领取的键读的，还是
                            # 借的旧领取那份（重启补查要能在历史里看见这条，否则「续跑」只
                            # 有一句注记，上下文接没接上无人可查）。
                            "historySource": result.get("historySource", ""),
                            "reason": result.get("reason", ""),
                            "atMs": _now_ms(),
                        }
                    )
                    # 「没清完」的报告只在这批唤醒真的没人管时存在：一旦某轮真把它们喂进了
                    # 会话（含新领取带走的滞留唤醒），缺口已合上，报告随之消失。
                    if consumed:
                        self._deferred.pop(ticket_id, None)
                        self._stranded.pop(ticket_id, None)
                    claim = self._claims.get(ticket_id)
                    if claim is not None:
                        claim["turns"] = rounds
                        claim["assignmentId"] = result.get("assignmentId", "") or claim["assignmentId"]

                    pending = self._pending_wakes(ticket_id)
                    live = self._is_live(ticket_id)
                    # 上限内、还活着、箱里还有货——三者齐了才续跑；否则交还单飞位并如实记账
                    keep = bool(pending and live and rounds < self.max_followup_turns)
                    if not keep:
                        self._claims.pop(ticket_id, None)
                        self._stranded.pop(ticket_id, None)
                        self._deferred.pop(ticket_id, None)
                        if pending and not live:
                            self._stranded[ticket_id] = {
                                "queuedWakes": pending,
                                "endedStatus": self._ended_status(ticket_id),
                                "turnsRun": rounds,
                                "note": self._no_live_reason(ticket_id, pending),
                            }
                        elif pending:
                            self._deferred[ticket_id] = {
                                "queuedWakes": pending,
                                "turnsRun": rounds,
                                "note": (
                                    f"续跑轮已到上限（{self.max_followup_turns} 轮），收件箱还压着 {pending} 条——"
                                    "不再自动连推（无限轮等于白烧模型额度）。下次 submit 会把它们一起带走；"
                                    "要立刻推进用 kick。"
                                ),
                            }
                if not keep:
                    break
