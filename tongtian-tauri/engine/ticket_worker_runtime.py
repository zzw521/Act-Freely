"""工单级执行体运行时（缺陷 D-48.1）

把「一次任务内部的临时并发子 Agent」（subagent_runtime）之上的**工单级执行体**
拆出来：一个工单一个 worker 会话、一份按 Assignment 独立命名的检查点、一个
带「人用预留槽」的全局槽位池。本模块只管这三件事的**编排语义**，不执行模型回合——
领取循环与真实回合执行由调用方接线（引擎服务端口与 Rust 侧领取回路在后续切片，
副作用队列在 D-48.2，恢复注记与看门狗在 D-48.3），这样这一层的口径可以先被单测钉死。

三条口径（与 D-47 工单层的三条硬口径同一套精神）：

1. **独立目录**：每个工单一个 `worker-<工单id摘要>` 目录（名字只由 id 决定——标题
   措辞变了也必须落回同一个目录），根在 `data_paths.ticket_workers_dir()`
   （引擎数据根，D-43 单一来源）。目录不存在时先跑一次 prepare（典型是 git worktree，
   真实规格见 `ticket_workspace_prepare`；不给规格就只建目录，那本身就是可运行的最小
   工单工作区）；prepare 失败要如实回报并把**本次自己创建**的半成品目录连来源侧的登记
   一起收走，不留一个「看起来在跑」的空壳。标记里存着 prepare 的来源指纹，换个来源想
   复用同一个目录会被拒。目录名由本模块生成并消毒，任何标题都拼不出目录穿越。
2. **独立检查点**：检查点按 **Assignment** 而不是按会话命名——一个工单换了执行体
   （新 Assignment），上一份检查点不会被误当恢复点复用；正常收口（finished）删除磁盘
   检查点，dropped / lost 保留（那是「上一次没跑完」的证据）。键由本模块生成，
   落盘走 checkpoint_runtime 的既有通道，不开第二套存储。
3. **槽位并发 + 人用预留槽**：并发上限作用于 worker 槽位；Agent 侧最多占
   `DEFAULT_AGENT_SLOTS` 个，池子**永久多留一个槽给人**（`HUMAN_EXTRA_SLOTS`），
   新的 Agent 任务挤不掉人对工单的唤醒。已经在跑的 worker 收人的消息**不占新槽**——
   「4 个工单占满时人的消息仍能立即唤醒对应工单」是验收项②，钉在用例里。

边界：本模块不发 HTTP、不领工单、不起子进程；`wake` 只负责入队与槽位裁决，
消费队列（drain_inbox）交给接线的执行回路。所有拒绝都给出中文理由，
调用方（界面/日志）拿到的是「为什么没跑起来」，不是一个 False。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import checkpoint_runtime as cr
import data_paths as dp
import ticket_workspace_prepare as twp

WORKER_DIR_PREFIX = "worker-"
#: Agent 侧并发上限；池子总量是 DEFAULT_AGENT_SLOTS + HUMAN_EXTRA_SLOTS
DEFAULT_AGENT_SLOTS = 4
#: 永久多留给人的槽：Agent 起新 worker 永远用不到它
HUMAN_EXTRA_SLOTS = 1
#: 目录短名里标题部分的最大长度（标题不进目录名，留给 normalize_slug 的调用方）
SLUG_MAX_CHARS = 24
PREPARE_MARKER = ".ticket-worker.json"

_ORIGIN_HUMAN = "human"
_ORIGIN_AGENT = "agent"

#: Assignment 结束态与工单层（D-47）的三态同集合，不在这里发明第四种
_END_STATUSES = ("finished", "dropped", "lost")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(text: str, chars: int) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:chars]


def normalize_slug(raw: Any, fallback: str) -> str:
    """把任意文本消毒成目录名可用的短段：只留小写字母数字与连字符。

    中文标题转不成 ASCII 是常态，不是错误——真留不下字符就退回摘要。
    工单目录名不引用这个函数（见 worker_dir_name），它留给需要可读短名的调用方。"""
    lowered = str(raw or "").strip().lower()
    kept = re.sub(r"[a-z0-9]+", "-", lowered)
    kept = re.sub(r"-{2,}", "-", kept).strip("-")[:SLUG_MAX_CHARS].strip("-")
    return kept or fallback


def worker_dir_name(ticket_id: str, title: Any = "") -> str:
    """`worker-<工单id摘要>`：**目录名只由工单 id 决定**。

    标题不进目录名——同一个工单在不同调用里标题措辞可能不同（列表页与唤醒
    各写一句），如果标题进名字，同单就会裂成两个目录，「一单一目录」的口径
    当场作废。可读性放在标记文件与快照的 title 字段里；恶意标题因此根本
    拼不进路径，目录穿越在构造上不可能。"""
    tid = str(ticket_id or "").strip()
    if not tid:
        raise ValueError("工单 id 为空，不能建 worker 目录")
    return f"{WORKER_DIR_PREFIX}{_digest(tid, 12)}"


def checkpoint_task_id(assignment_id: str) -> str:
    """检查点键：按 Assignment 命名，与任务/会话检查点天然分居同一存储的不同文件。

    checkpoint_runtime 落盘前对 task_id 做哈希，所以这里的可读前缀只是给我们
    调试与快照用的——它不会被拼进路径。"""
    aid = str(assignment_id or "").strip()
    if not aid:
        raise ValueError("Assignment id 为空，检查点必须按领取绑定命名")
    return f"{WORKER_DIR_PREFIX}{aid}"


# ── 槽位池：Agent 上限 + 人用预留槽 ────────────────────────────


class SlotPool:
    """worker 槽位池。occupant 记录每个已占槽的归属，便于快照如实展示。

    - origin=agent：只能占前 agent_slots 个；
    - origin=human：可以占任何一个，包括预留槽——预留槽的意义就是
      「Agent 把上限占满时，人依然能立刻起一个 worker」。
    """

    def __init__(self, agent_slots: int = DEFAULT_AGENT_SLOTS, human_extra: int = HUMAN_EXTRA_SLOTS):
        if agent_slots < 1 or human_extra < 1:
            raise ValueError("槽位池至少要有 1 个 Agent 槽和 1 个人用预留槽")
        self.agent_slots = agent_slots
        self.human_extra = human_extra
        self._occupied: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str, origin: str) -> tuple[bool, str]:
        if origin not in (_ORIGIN_HUMAN, _ORIGIN_AGENT):
            return False, f"未知的唤醒来源「{origin}」，槽位只认 human / agent"
        with self._lock:
            if key in self._occupied:
                return True, ""  # 同一个 worker 重复 acquire 是幂等的
            total = len(self._occupied)
            if total >= self.agent_slots + self.human_extra:
                return False, (
                    f"worker 槽位已满（{total}/{self.agent_slots + self.human_extra}，"
                    "其中人用预留槽也在用）；这条工单先排队，槽位释放后再唤醒"
                )
            # Agent 的额度只按 Agent 占的槽数算：人占了预留槽（甚至占了普通槽）
            # 不该缩小 Agent 侧的并发上限，否则释放一个 Agent 槽后新任务仍被
            # 「总占用 ≥ agent_slots」误拒——预留槽是留给人的，不是留给人挤 Agent 的。
            agent_owned = sum(1 for o in self._occupied.values() if o.get("origin") == _ORIGIN_AGENT)
            if origin == _ORIGIN_AGENT and agent_owned >= self.agent_slots:
                return False, (
                    f"Agent 侧的 {self.agent_slots} 个槽位已占满；"
                    "最后一个人用预留槽不接 Agent 起的任务——人的唤醒永远进得来"
                )
            self._occupied[key] = {"origin": origin, "sinceMs": _now_ms()}
            return True, ""

    def release(self, key: str) -> None:
        with self._lock:
            self._occupied.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agentSlots": self.agent_slots,
                "humanExtraSlots": self.human_extra,
                "occupied": dict(self._occupied),
            }


# ── 工单工作区：独立目录 + 一次性 prepare ──────────────────────


class TicketWorkspace:
    """`{ticket-workers}/worker-<工单id摘要>` 目录的创建与幂等 prepare。"""

    def __init__(self, root: Path | None = None):
        self._root_override = root
        # 同一份工作区的并发首触要串行建（见 prepare）；键是目录名，锁只在本进程内有效。
        self._prepare_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, path: Path) -> threading.Lock:
        key = path.name
        with self._locks_guard:
            lock = self._prepare_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._prepare_locks[key] = lock
            return lock

    @property
    def root(self) -> Path:
        return self._root_override if self._root_override is not None else dp.ticket_workers_dir()

    def path_for(self, ticket_id: str, title: Any = "") -> Path:
        path = self.root / worker_dir_name(ticket_id, title)
        # 最后一道防线：消毒之后的路径必须还在这个根下面
        root_resolved = self.root.resolve()
        if path.resolve() != root_resolved and root_resolved not in path.resolve().parents:
            raise RuntimeError(f"worker 目录越出数据根，已拒绝：{path}")
        return path

    def prepare(
        self,
        ticket_id: str,
        title: Any = "",
        prepare_fn: Callable[[Path], None] | None = None,
        *,
        prepare_key: str | None = None,
    ) -> tuple[Path, str | None]:
        """不存在时建目录并跑一次 prepare；已存在直接返回（幂等）。

        prepare_fn 是调用方注入的「典型 git worktree」那一步（真实实现见
        `ticket_workspace_prepare.WorkspacePreparer`）；默认没有——建一个空目录就是可运行的
        最小工单工作区。prepare 抛错时把**本次自己创建**的半成品目录收走（收干净之前先确认
        它在 worker 根之下），并清掉来源侧留下的现场（可调用对象自带 `cleanup()` 时调用），
        返回中文失败原因而不是留下假目录。

        `prepare_key` 是这份 prepare 的**来源指纹**（不给就从可调用对象上按名字取）。标记里
        存着指纹时，换一个来源来复用同一个目录会被拒：目录是按 A 仓准备的，来了一个 B 仓的
        唤醒却直接开跑，执行体下一轮看到的就不是它以为的那一份代码。旧标记没有指纹字段的
        按「不知道来源」放行——升级期不能把已经在跑的工单判死。

        「幂等」在并发下不是一句注释：同一工单的两条唤醒同时首触时，先建者成功后
        后者会撞上 FileExistsError——按老写法那是一条「工作区准备失败」的假拒绝，
        而 prepare_fn 更会被跑第二遍（重复的外部动作）。所以这里按目录名上锁，
        并在锁内双检标记文件：后进来的人复用已建好的工作区，一次 prepare 都不多跑。
        锁只在本进程内有效（工单执行体的注册表本来就是进程内的），跨进程仍有
        重启恢复注记兜底，这里不假称跨进程互斥。"""
        path = self.path_for(ticket_id, title)
        marker = path / PREPARE_MARKER
        key = self.prepare_fingerprint(prepare_fn, prepare_key)
        if marker.exists():
            conflict = self._provenance_conflict(marker, key, path)
            if conflict:
                return path, conflict
            return path, None
        created = False
        with self._lock_for(path):
            if marker.exists():  # 等锁期间别人已经建好——这就是并发首触的正常结局
                conflict = self._provenance_conflict(marker, key, path)
                if conflict:
                    return path, conflict
                return path, None
            try:
                if not path.exists():
                    path.mkdir(parents=True, exist_ok=True)
                    created = True
                if prepare_fn is not None:
                    prepare_fn(path)
                marker.write_text(
                    json.dumps(
                        {
                            "ticketId": str(ticket_id),
                            "title": str(title or ""),
                            "dirName": path.name,
                            "preparedAtMs": _now_ms(),
                            "ranPrepare": prepare_fn is not None,
                            "prepareFingerprint": key,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001  prepare 失败要如实回报，不演「已就绪」
                notes = [f"工单工作区准备失败：{type(exc).__name__}: {exc}"]
                if created:
                    # 老写法只 `rmdir()`：一次失败的 `git worktree add` 会留下非空目录，
                    # rmdir 撞 OSError 被吞掉，半成品就永远留在那儿——下一次 prepare 看到
                    # 目录已存在（`created=False`）便不再清理，直接往脏目录里再跑一遍准备。
                    removed, remove_note = twp.remove_created_tree(path, root=self.root)
                    if remove_note:
                        notes.append(remove_note)
                    cleaner = getattr(prepare_fn, "cleanup", None)
                    if callable(cleaner):
                        # 顺序不能倒：git 那边那条登记指向的目录还在时 `prune` 不动它，
                        # 同一目录重试会撞「missing but already registered worktree」(rc=128)。
                        try:
                            extra = cleaner(path)
                        except Exception as clean_exc:  # noqa: BLE001  清理自己失败也要说，不改成「已清理」
                            extra = f"（来源侧清理自己抛了 {type(clean_exc).__name__}: {clean_exc}）"
                        if extra:
                            notes.append(str(extra))
                    elif not removed:
                        notes.append("（没有来源侧清理口，登记可能留在仓里）")
                return path, "；".join(notes)
        return path, None

    @staticmethod
    def prepare_fingerprint(prepare_fn: Any, prepare_key: str | None = None) -> str:
        """这份 prepare 的来源指纹：显式给的 `prepare_key` 优先，否则从可调用对象上取。"""
        if prepare_key is not None:
            return str(prepare_key or "").strip()
        return str(getattr(prepare_fn, "fingerprint", "") or "").strip()

    @staticmethod
    def _provenance_conflict(marker: Path, key: str, path: Path) -> str | None:
        """目录已按别的来源准备过时给出中文理由；没问题就回 None。"""
        if not key:
            return None  # 这次没带规格（空目录路线），不与已有来源较劲
        try:
            stored = str(json.loads(marker.read_text(encoding="utf-8")).get("prepareFingerprint") or "").strip()
        except Exception:  # noqa: BLE001  旧标记 / 读不动：按「不知道来源」放行，不判死在跑的工单
            return None
        if not stored or stored == key:
            return None
        return (
            f"工单工作区 {path.name} 是按另一份来源准备的（现指纹 {stored}，这次要 {key}）——"
            "换来源请先删掉这个目录。把两个仓的内容混进同一个工作区，执行体下一轮读到的"
            "就不是它以为的那一份代码"
        )



# ── worker 会话：一个工单一条 ──────────────────────────────────


class TicketWorker:
    """一个工单的本地执行体会话：当前绑定、槽位归属、唤醒收件箱。

    `run_state` 只有三档：idle（没在跑回合）/ running（回合在跑）/
    ended（领取绑定已结束，槽位已还）。running 由接线的执行回路翻转，
    本模块不自造「看起来在跑」的状态。"""

    def __init__(self, ticket_id: str, title: Any = ""):
        self.ticket_id = str(ticket_id)
        self.title = str(title or "")
        self.dir_name = worker_dir_name(ticket_id, title)
        self.assignment_id = ""
        self.checkpoint_key = ""
        self.machine = ""
        self.recovery_note = ""
        self.slot_origin = ""
        self.run_state = "idle"
        self.ended_status = ""
        self.inbox: list[dict[str, Any]] = []

    def bind_assignment(self, assignment_id: str, *, machine: str = "", recovery_note: str = "") -> None:
        """绑定新领取：检查点键换到新的 Assignment——旧的一份不覆盖、不复用。"""
        self.assignment_id = str(assignment_id)
        self.checkpoint_key = checkpoint_task_id(assignment_id)
        self.machine = str(machine or "")
        self.recovery_note = str(recovery_note or "")
        self.ended_status = ""
        self.run_state = "idle"

    def snapshot(self) -> dict[str, Any]:
        return {
            "ticketId": self.ticket_id,
            "title": self.title,
            "dirName": self.dir_name,
            "assignmentId": self.assignment_id,
            "checkpointKey": self.checkpoint_key,
            "machine": self.machine,
            "recoveryNote": self.recovery_note,
            "slotOrigin": self.slot_origin,
            "runState": self.run_state,
            "endedStatus": self.ended_status,
            "queuedWakes": len(self.inbox),
        }


# ── 注册表：目录、槽位、检查点三条线的汇合处 ──────────────────


class TicketWorkerRegistry:
    """进程内的工单执行体注册表。

    start_or_wake 是唯一入口：有活绑定就投递唤醒（不占新槽），没有就按来源
    裁决槽位并挂上新的 Assignment 绑定。所有决定（含拒绝理由）都回在结果里。"""

    def __init__(self, *, agent_slots: int = DEFAULT_AGENT_SLOTS, human_extra: int = HUMAN_EXTRA_SLOTS, workspace: TicketWorkspace | None = None, inbox_journal: Any = None):
        self.pool = SlotPool(agent_slots=agent_slots, human_extra=human_extra)
        self.workspaces = workspace or TicketWorkspace()
        #: 耐久收件箱（D-48.6）：投进内存箱的同时留一份磁盘凭据，重启后引擎自己捞得回来。
        #: 没接（None）就是旧行为——只有内存知道这条投递过，重启补查得靠调用方给候选。
        self.inbox_journal = inbox_journal
        self._workers: dict[str, TicketWorker] = {}
        self._lock = threading.Lock()

    def _journal_append(self, ticket_id: str, wake: dict[str, Any]) -> str:
        """把一条已进内存箱的投递落到耐久收件箱，失败时**只回原话、不拦投递**。

        为什么不拦：这条唤醒已经进了 worker.inbox，起新一轮就会喂进提示词；此刻拒绝
        投递等于把用户的话扣下来。落盘的代价是「重启后引擎不知道自己收过这条」，
        那是可恢复的缺口，不是丢失——所以原话进 `journalWarning`，交给接线侧的日志。

        为什么在注册表锁内调用（调用点的顺序要害）：**先落盘、再回 ok**。反过来做会留
        一个窗口——返回「已投递」后、落盘前崩溃，日志说这条从没投过，重启补查就把它当
        不存在，而用户已经听到「已送达」。宁可锁内多一次磁盘写（这条写只有几 KB）。"""
        if self.inbox_journal is None:
            return ""
        try:
            out = self.inbox_journal.append(
                ticket_id=ticket_id,
                assignment_id=str(wake.get("assignmentId") or ""),
                text=str(wake.get("text") or ""),
                origin=str(wake.get("origin") or ""),
                at_ms=wake.get("atMs") or 0,
            )
        except Exception as exc:  # noqa: BLE001  收件箱是旁证层，炸了也不能拖垮投递
            return f"耐久收件箱写入异常：{type(exc).__name__}: {exc}"
        if out and not out.get("ok"):
            return f"耐久收件箱没落盘：{out.get('reason') or '未知原因'}"
        return ""

    def worker(self, ticket_id: str) -> TicketWorker | None:
        with self._lock:
            return self._workers.get(str(ticket_id))

    def start_or_wake(
        self,
        *,
        ticket_id: str,
        title: Any = "",
        assignment_id: str,
        message: str,
        origin: str = _ORIGIN_HUMAN,
        machine: str = "",
        recovery_note: str = "",
        prepare_fn: Callable[[Path], None] | None = None,
        prepare_key: str | None = None,
        arrived_at_ms: int = 0,
    ) -> dict[str, Any]:
        """投递一条唤醒（有活绑定就进箱，没有就裁决槽位并绑新领取）。

        `prepare_fn` / `prepare_key` 只在**起新绑定**时消费（已有活绑定的投递不碰磁盘），
        原样转给 `workspaces.prepare`；`prepare_key` 不给就从可调用对象上的 `fingerprint`
        取（见 `ticket_workspace_prepare.WorkspacePreparer`）。

        `arrived_at_ms` 只给**重启补查**用：崩溃前那条消息的真实到达时刻在服务端消息表里，
        重投时必须原样带回来。落回默认值就是把到达时间写成重启这一刻——提示词里的到达顺序
        会失真，更麻烦的是已读键（正文+到达时间+领取）对不上旧落账，同一条唤醒在二次重启时
        会被当成没读过而再投一遍。"""
        tid = str(ticket_id or "").strip()
        aid = str(assignment_id or "").strip()
        text = str(message or "").strip()
        if not tid or not aid or not text:
            return {"ok": False, "reason": "工单 id、领取绑定 id、唤醒消息三者都必填——缺一个就没有这条唤醒该给谁", "ticketId": tid}
        arrival = int(arrived_at_ms or 0) or _now_ms()
        with self._lock:
            worker = self._workers.get(tid)

        # 已经有活绑定：消息直接进收件箱，不占新槽（验收项②的口径）
        if worker is not None and worker.assignment_id and worker.ended_status == "":
            wake = {"text": text, "origin": origin, "atMs": arrival, "assignmentId": worker.assignment_id}
            worker.inbox.append(wake)
            warning = self._journal_append(tid, wake)
            result = {
                "ok": True,
                "delivered": "existing-worker",
                "ticketId": tid,
                "assignmentId": worker.assignment_id,
                "checkpointKey": worker.checkpoint_key,
                "dirName": worker.dir_name,
                "queuedWakes": len(worker.inbox),
                "note": "唤醒已投递进这个 worker 的收件箱；它占着的槽位不变，人不用等空位",
            }
            if warning:
                result["journalWarning"] = warning
            return result

        # 起新绑定：先问槽位池，再动磁盘——被拒的唤醒不该留下目录
        acquired, deny = self.pool.try_acquire(tid, origin)
        if not acquired:
            return {"ok": False, "reason": deny, "ticketId": tid}

        # 槽位到手后**先挂绑定、再建目录**（并发下这是顺序要害）：若反过来先跑 prepare，
        # 同一工单的两条并发唤醒都会看到「没有活绑定」，各绑一份 Assignment——检查点键
        # 随之漂移，先跑那轮的收口会结束别人的绑定。槽位池对同一 key 是幂等的（第二条
        # 线程拿到的也是 True），所以占不住也挡不住，只有把绑定放进同一把锁才挡得住。
        with self._lock:
            racer = self._workers.get(tid)
            if racer is not None and racer.assignment_id and racer.ended_status == "":
                # 极窄窗口里另一线程已把这份领取建好：本调用退化成投递，不起第二份绑定。
                # 注意这里**不能** release 槽位——槽是按工单 id 计的，那条线程还占着它。
                wake = {"text": text, "origin": origin, "atMs": arrival, "assignmentId": racer.assignment_id}
                racer.inbox.append(wake)
                warning = self._journal_append(tid, wake)
                result = {
                    "ok": True,
                    "delivered": "existing-worker",
                    "ticketId": tid,
                    "assignmentId": racer.assignment_id,
                    "checkpointKey": racer.checkpoint_key,
                    "dirName": racer.dir_name,
                    "queuedWakes": len(racer.inbox),
                    "note": "并发首触：另一线程已为这份工单建好领取，这条唤醒直接进它的收件箱，没有起第二份绑定",
                }
                if warning:
                    result["journalWarning"] = warning
                return result
            worker = racer or TicketWorker(tid, title)
            self._workers[tid] = worker
            worker.bind_assignment(aid, machine=machine, recovery_note=recovery_note)
            worker.slot_origin = origin
            wake = {"text": text, "origin": origin, "atMs": arrival, "assignmentId": aid}
            worker.inbox.append(wake)
            warning = self._journal_append(tid, wake)

        path, prep_err = self.workspaces.prepare(tid, title, prepare_fn, prepare_key=prepare_key)
        if prep_err:
            with self._lock:
                # 目录没建成 = 这份领取跑不了：置 lost 留证据，但**不丢掉已投递的唤醒**——
                # 它们留在收件箱里，下一次新领取会把这批一起带走（恢复注记按 carried_wakes 点名）。
                worker.ended_status = "lost"
                worker.run_state = "idle"
                worker.slot_origin = ""
                stranded = len(worker.inbox)
            self.pool.release(tid)
            result = {
                "ok": False,
                "reason": f"{prep_err}（这份领取已置 lost；{stranded} 条唤醒仍留在收件箱，等一次新的领取消费）",
                "ticketId": tid,
                "queuedWakes": stranded,
            }
            if warning:
                result["journalWarning"] = warning
            return result
        result = {
            "ok": True,
            "delivered": "started",
            "ticketId": tid,
            "assignmentId": aid,
            "checkpointKey": worker.checkpoint_key,
            "dirName": worker.dir_name,
            "workspaceDir": str(path),
            # 这份目录是按哪个来源准备的：调用方要靠它判断「换仓了没有」，界面不必再读磁盘标记
            "prepareFingerprint": self.workspaces.prepare_fingerprint(prepare_fn, prepare_key),
            "queuedWakes": len(worker.inbox),
            "recoveryNote": worker.recovery_note,
            "note": (
                "新领取已绑定：检查点按这份 Assignment 命名；"
                "恢复续跑时先查目录、git 历史与远端，再重复任何外部动作（D-48.3 接线时消费这条注记）"
            ),
        }
        if warning:
            result["journalWarning"] = warning
        return result

    def drain_inbox(self, ticket_id: str) -> list[dict[str, Any]]:
        """把收件箱整体交给执行回路（唤醒合并成一段输入，按到达顺序）。"""
        worker = self.worker(ticket_id)
        if worker is None:
            return []
        with self._lock:
            items, worker.inbox = list(worker.inbox), []
        return items

    def set_run_state(self, ticket_id: str, state: str) -> None:
        """执行回路翻转 running/idle 的唯一入口；本模块不自造在跑状态。"""
        if state not in ("idle", "running"):
            raise ValueError("run_state 只能是 idle 或 running——ended 走 end_assignment")
        worker = self.worker(ticket_id)
        if worker is not None:
            with self._lock:
                worker.run_state = state

    def end_assignment(self, ticket_id: str, status: str, *, reason: str = "", exit_payload: dict | None = None) -> dict[str, Any]:
        """结束当前领取：槽位归还；finished 删检查点，dropped / lost 保留。

        与工单层同一套语义——「正常干完」不留恢复点，「没干完」必须留证据。"""
        if status not in _END_STATUSES:
            return {"ok": False, "reason": f"未知的结束态「{status}」，只接受 {'/'.join(_END_STATUSES)}"}
        worker = self.worker(ticket_id)
        if worker is None or not worker.assignment_id:
            return {"ok": False, "reason": "这个工单当前没有活的领取绑定，没什么可结束", "ticketId": str(ticket_id)}
        key = worker.checkpoint_key
        with self._lock:
            worker.ended_status = status
            worker.run_state = "idle"
            worker.slot_origin = ""
        self.pool.release(str(ticket_id))
        deleted = False
        if status == "finished":
            cr.delete_checkpoint(key)
            deleted = True
        elif exit_payload is not None:
            # checkpoint_runtime.load_checkpoint 认「messages 必须是列表」——
            # 缺了这个字段，落盘的证据会在下一次读时被当成损坏文件删掉。
            # worker 退出证据没有对话消息，空列表就是它的诚实形态。
            cr.save_checkpoint(
                key,
                {
                    "messages": [],
                    "workerExit": {
                        "ticketId": str(ticket_id),
                        "status": status,
                        "reason": str(reason or ""),
                        **exit_payload,
                    },
                },
            )
        return {
            "ok": True,
            "ticketId": str(ticket_id),
            "assignmentId": worker.assignment_id,
            "status": status,
            "checkpointKey": key,
            "checkpointDeleted": deleted,
            "reason": str(reason or ""),
        }

    def checkpoint_of(self, ticket_id: str) -> dict | None:
        worker = self.worker(ticket_id)
        if worker is None or not worker.checkpoint_key:
            return None
        return cr.load_checkpoint(worker.checkpoint_key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            workers = [w.snapshot() for w in self._workers.values()]
        return {"slots": self.pool.snapshot(), "workers": workers}
