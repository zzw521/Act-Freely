"""多 Agent 团队协作运行时（缺陷 D-34 的引擎半边）

对标 Octop AgentTeams：**团队是一种专家**（主持人 `kind=team`），成员仍是普通专家。
主持人不做专业工作，只通过异步收件箱派工；同一成员串行、不同成员并行；成员回复直接
上群聊时间线并带说话人；每个成员用自己的目录与检查点；成员失败要回叫主持人；停止只停
主持人本轮；成员在途时禁止移出或删除。

本模块只做**编排语义**：编制、房间时间线、在途任务台账、派工与回叫的投递。它不发 HTTP、
不起进程、不调模型，也不自己实现任何队列——

**派工与投递一律复用 D-47/D-48 已有的那套执行体**（`ticket_worker_runtime.TicketWorkerRegistry`
+ `ticket_inbox_runtime.TicketInbox`），因此三条已经验收过的口径自动继承，不在这里抄第二遍：

* **投递即落盘**：唤醒进内存箱的同一条写盘路径（注册表锁内、返回 ok 之前）；
* **同工单保序**：一个成员就是一个「工单」（`ticket_id = 房间~成员`），同人两次派工走的是
  既有 `existing-worker` 分支，按到达时间进同一个箱，天然串行；
* **先落已读再销账**：消费与销账归 `ticket_dispatch_runtime`，本模块不碰。

于是团队协作的全部新增事实只剩三件：谁和谁组成团队（`team_registry.json`）、群里说过什么
（`team_rooms.json`，只存投影）、哪些派工还在途（`team_jobs.json`，重启即标 `interrupted`）。

能力边界（写进界面与文档的同一口径）：

* 成员执行体与主持人执行体**是否真的起回合**由接线方决定（引擎的 worker 调度器或桌面领取
  回路）；本层只保证「投进去了一定在盘上、没投进去一定说清楚为什么」。
* 主持人回叫投给 `ticket_id = 房间`，与工单层共用槽位池：Agent 起的绑定占不进人用预留槽。
* 桌面时间线渲染、IM 通道转播、Rust 领取回路属另外三张卡，见 `docs/并行实施/148.md`。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import checkpoint_runtime as ckpt
import data_paths as dp
import expert_runtime as er
import ticket_worker_runtime as twr

#: 编制与投影的落盘文件名。**数据根只从 data_paths.engine_home() 取**（D-43 单一来源），
#: 这两个解析点请 W4 合并时搬进 data_paths——本模块不自建第二套数据根规则。
TEAM_REGISTRY_FILENAME = "team_registry.json"
TEAM_ROOMS_FILENAME = "team_rooms.json"
TEAM_JOBS_FILENAME = "team_jobs.json"

STORE_VERSION = 1
MAX_TEAMS = 32
MAX_MEMBERS_PER_TEAM = 6
MAX_NAME_CHARS = 60
MAX_BRIEF_CHARS = 4_000
MAX_NOTE_CHARS = 1_000
#: 房间时间线条数上限（按房间计）：超了丢最早的那批投影——上墙是给人看的历程，
#: 不是账本；真正的投递凭据在耐久收件箱里，一条也不会因这里裁剪而丢。
MAX_ROOM_EVENTS = 2_000
#: 单条正文的落盘上限：派工说明是人写的短段落，超长的多半是粘贴的日志。
MAX_EVENT_TEXT_CHARS = 4_000

ROOM_MEMBER_SEP = "~"
#: 群聊里主持人的说话人标识：它不是专家 id，所以不与任何成员 id 相撞。
HOST_SPEAKER = "host"
#: 房间时间线里「引擎替人说的话」（建房间、加人、停主持人）的署名。同上一条规则：
#: 专家 id 由专家库生成，不会叫 `system`；渲染侧要是不认，只会显示成说话人名字，
#: 不会把系统提示伪装成某个成员在说话。
SYSTEM_SPEAKER = "system"

MAX_DISPATCHES_PER_ROOM = 12
#: 单房间在途派工上限（跨多次 dispatch 累计）：槽位池限的是「同时在跑」，
#: 这条限的是「一直在派」——没有它，主持人可以在一轮里无限加活。
MAX_MEMBERS_PER_ROOM_JOBS = 12

#: 派工台账的状态集合。`queued/running` 是在途（禁止移出成员），
#: `interrupted` 是重启留下的证据，`ended` 是收口，`refused` 是没投出去。
JOB_STATES = ("queued", "running", "interrupted", "ended", "refused")
INFLIGHT_STATES = ("queued", "running")
#: 一次派工投出去之后、执行体真正开始跑之前，台账只能说到 `queued`；
#: `running` 由接线方翻转（与 worker 的 run_state 同一口径，本层不自造在跑状态）。
_HOST_BINDING_PREFIX = "host-"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(text: Any, chars: int = 16) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:chars]


def _s(value: Any) -> str:
    return str(value or "").strip()


def _clip(text: Any, limit: int) -> str:
    body = str(text or "")
    return body if len(body) <= limit else body[:limit] + "…（已截断）"


# ── id 推导：三条线（房间 / 成员工单 / 派工）各一条算式，且只有这一处 ──


def room_of_session(session_id: Any) -> str:
    """房间 id 就是主持人的会话 id（不另发一套 id，否则「哪个会话是这个团队」要第二次回答）。"""
    sid = _s(session_id)
    if not sid:
        raise ValueError("房间 id 来自主持人会话 id，会话 id 为空就没有房间")
    return sid


def member_ticket_id(room: Any, member_id: Any) -> str:
    """成员在执行体注册表里的「工单 id」：`房间~成员`。

    这个键同时决定了成员的独立目录（`worker-<摘要>`）与既有检查点键的命名域，
    所以「同一成员串行、不同成员并行」不需要新代码——它本来就是一条工单一个 worker。
    `~` 是刻意选的分隔符：会话 id 与专家 id 都不含它（两侧都做过消毒），
    而 `/` `\\` `.` 会参与路径解析，绝不能出现在 id 片段里。"""
    rid, mid = _s(room), _s(member_id)
    if not rid or not mid:
        raise ValueError("房间 id 与成员 id 都必填：缺一个就不知道这条派工给谁")
    if ROOM_MEMBER_SEP in rid or ROOM_MEMBER_SEP in mid:
        raise ValueError(f"房间 id 与成员 id 里不得出现分隔符「{ROOM_MEMBER_SEP}」：会让两个不同的键算出同一个名字")
    return f"{rid}{ROOM_MEMBER_SEP}{mid}"


def host_ticket_id(room: Any) -> str:
    """主持人在同一张表里的键：房间本身（成员是 `房间~成员`，两种形状不可能撞名）。"""
    return room_of_session(room)


def job_id_of(dispatch_id: Any, member_id: Any) -> str:
    """一次派工里某个成员的 Assignment id：`<派工 id>:<成员 id>`。

    它同时是 worker 运行时的检查点键来源（`checkpoint_task_id`），所以换一次派工就是
    换一份领取——上一份的检查点不会被误当恢复点复用，这条口径继承 D-48.1，不在此重述。"""
    did, mid = _s(dispatch_id), _s(member_id)
    if not did or not mid:
        raise ValueError("派工 id 与成员 id 都必填")
    return f"{did}:{mid}"


def new_dispatch_id(team_id: Any, room: Any, brief: Any) -> str:
    return f"td-{_digest(f'{team_id}|{room}|{brief}|{_now_ms()}|{os.getpid()}', 12)}"


# ── 编制：团队 = 主持人专家 + 成员名单 ─────────────────────────


def normalize_team(raw: Any, experts: list[dict], *, experts_error: str = "") -> tuple[dict, str | None]:
    """校验并归一一条团队编制；任何一处不合法都整体拒绝并给出中文理由。

    为什么不静默忽略不认识的成员：主持人「以为已经派给了三人、实际只有两人接活」比直接
    报错难查得多，而团队的全部价值就在分工是否真实存在。"""
    if not isinstance(raw, dict):
        return {}, f"团队定义要是对象（收到 {type(raw).__name__}）"
    if experts_error:
        return {}, f"专家注册表不可用，无法组建团队：{experts_error}"
    name = _clip(_s(raw.get("name")), MAX_NAME_CHARS)
    host = _s(raw.get("hostExpertId") or raw.get("host"))
    members_raw = raw.get("memberExpertIds")
    if members_raw is None:
        members_raw = raw.get("members")
    if not name:
        return {}, "缺少团队名称"
    if not host:
        return {}, "缺少主持人专家 id：团队必须有一个不做专业工作、只负责派工与收口的主持人"
    if not isinstance(members_raw, list) or not members_raw:
        return {}, "成员名单要是非空数组：没有成员的团队就是单个专家，不必建团队"
    host_expert = er.find_expert(experts, host)
    if host_expert is None:
        return {}, f"主持人专家「{_clip(host, 40)}」不存在。可用专家：{er.available_line(experts)}"
    _, host_model_err = er.model_config_of(host_expert)
    if host_model_err:
        return {}, f"主持人不能开工：{host_model_err}"
    members: list[dict] = []
    seen: set[str] = set()
    for i, ref in enumerate(members_raw, 1):
        mid = _s(ref.get("id") if isinstance(ref, dict) else ref)
        if not mid:
            return {}, f"第 {i} 个成员缺 id"
        if mid.lower() == host_expert["id"]:
            return {}, f"第 {i} 个成员就是主持人「{host_expert['name']}」：主持人不做专业工作，不能给自己派工"
        expert = er.find_expert(experts, mid)
        if expert is None:
            return {}, f"第 {i} 个成员「{_clip(mid, 40)}」不存在。可用专家：{er.available_line(experts)}"
        if expert["id"] in seen:
            return {}, f"第 {i} 个成员「{expert['name']}」重复：重复的那一份永远不会被派到"
        _, err = er.model_config_of(expert)
        if err:
            return {}, f"第 {i} 个成员不能接活：{err}"
        seen.add(expert["id"])
        members.append({"id": expert["id"], "name": expert["name"]})
    if len(members) > MAX_MEMBERS_PER_TEAM:
        return {}, f"成员最多 {MAX_MEMBERS_PER_TEAM} 人（本次 {len(members)} 人）：更多成员只会让主持人无法逐个收口"
    member_ids = "|".join(m["id"] for m in members)
    id_seed = "%s|%s|%s" % (name, host_expert["id"], member_ids)
    team_id = _s(raw.get("id")) or ("team-" + _digest(id_seed, 10))
    return {
        "id": team_id,
        "name": name,
        "kind": "team",
        "hostExpertId": host_expert["id"],
        "hostExpertName": host_expert["name"],
        "members": members,
        "createdBy": _clip(_s(raw.get("createdBy")), MAX_NAME_CHARS),
        "createdAtMs": int(raw.get("createdAtMs") or _now_ms()),
    }, None


# ── 耐久层：三份 JSON，同一套原子写与坏文件让路纪律 ──────────


class _DurableStore:
    """单文件 JSON 存储：tmp + os.replace 原子写、坏文件改名让路、上限拒收不静默挤掉。

    这三条与 `ticket_inbox_runtime.TicketInbox` 完全同构（那里逐条解释过为什么），
    本类只做同一件事的第二次实现——**仅限编制/投影/台账这三份新事实**，
    唤醒投递仍然只用收件箱那一份，不在这里另开队列。"""

    FILENAME = ""
    #: 数据根下的子目录名；留空表示直接落在 `engine_home()` 根下——与 D-48 那三份
    #: 工单账本（`ticket_inbox.json` 等）同处，重启补查时不用两套目录规则。
    SUBDIR = ""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else self._default_path()
        self._clock = clock
        self._lock = threading.Lock()
        self.load_error = ""
        self.data: dict[str, Any] = self.empty()
        self._load()

    def _default_path(self) -> Path:
        home = dp.engine_home()
        return home / self.SUBDIR / self.FILENAME if self.SUBDIR else home / self.FILENAME

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {"version": STORE_VERSION}

    def _ms(self) -> int:
        return int(self._clock() * 1000)

    def _load(self) -> None:
        if not self.path.exists():
            self.on_load()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or int(raw.get("version") or 0) != STORE_VERSION:
                raise ValueError("版本不符或结构不是对象")
        except Exception as exc:  # noqa: BLE001  坏文件改名让路，不在坏文件上追加事实
            aside = self.path.with_name(f"{self.path.name}.corrupt-{self._ms()}")
            try:
                self.path.replace(aside)
                self.load_error = f"{self.label()}损坏，已改名保留 {aside.name} 并从空档起步：{exc}"
            except Exception as move_exc:  # noqa: BLE001  挪不动就明说不可写
                self.load_error = f"{self.label()}损坏且无法移开（{exc}；{move_exc}）：本层不可写"
            # 两种情况都从空档起步：读不懂的文件不能当数据用，也不能当数据**改**——
            # 改名让路至少留下现场，挪不动时写入照样会覆盖它，所以这里不再追加。
            self.data = self.empty()
            self.on_load()
            return
        self.data = raw
        self.on_load()

    def on_load(self) -> None:
        """子类补一步：加载后把「内存里才算在跑」的东西降级成磁盘凭据。"""

    def label(self) -> str:
        return self.FILENAME

    def _save(self) -> None:
        encoded = json.dumps(self.data, ensure_ascii=False, default=str)
        if len(encoded) > 8_000_000:
            raise RuntimeError("团队档超过 8M 上限——请清理已结束房间的时间线投影")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{self._ms()}")
        tmp.write_text(encoded, encoding="utf-8")
        os.replace(tmp, self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"path": str(self.path), "loadError": self.load_error}


#: 同一份原子写/坏文件让路/上限拒收的**公开别名**：#156 的工作流两份存储直接继承它，
#: 不再第三次实现一遍（第三条实现的规矩，迟早和头两条不一样）。
DurableStore = _DurableStore


class TeamStore(_DurableStore):
    """团队编制（重启后保留是 D-34 验收项⑤）。"""

    FILENAME = TEAM_REGISTRY_FILENAME

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {"version": STORE_VERSION, "teams": {}, "rooms": {}}

    def label(self) -> str:
        return "团队编制档"

    def teams(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self.data["teams"].values()]

    def get(self, team_id: str) -> dict[str, Any] | None:
        with self._lock:
            team = self.data["teams"].get(_s(team_id))
            return dict(team) if team else None

    def by_room(self, room: str) -> dict[str, Any] | None:
        with self._lock:
            team_id = self.data["rooms"].get(_s(room))
            team = self.data["teams"].get(team_id or "")
            return dict(team) if team else None

    def room_of_team(self, team_id: str) -> str:
        """团队当前绑到的**第一个**房间（主持人会话）。没绑过就是空串——编制可以先于会话存在。

        只给展示用。凡是要回答「这个团队还有没有在途的活」，请用 `rooms_of_team`：
        同一个团队可以绑在好几个房间里（用户换一轮继续用这队人），拿第一个房间去查台账
        会把别的房间的在途成员漏掉，止删判定就跟着错。"""
        rooms = self.rooms_of_team(team_id)
        return rooms[0] if rooms else ""

    def rooms_of_team(self, team_id: str) -> list[str]:
        """该团队当前绑到的全部房间（按绑定顺序）。"""
        with self._lock:
            return [room for room, tid in self.data["rooms"].items() if tid == _s(team_id)]

    def upsert(self, team: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if _s(team.get("id")) not in self.data["teams"] and len(self.data["teams"]) >= MAX_TEAMS:
                return {"ok": False, "reason": (
                    f"本机已有 {len(self.data['teams'])} 个团队、到上限 {MAX_TEAMS}：这条**没有落盘**。"
                    "先删掉不再使用的团队——不做「挤掉最旧一个」：用户以为新团队建好了，"
                    "实际那一队人根本不存在"
                )}
            self.data["teams"][team["id"]] = dict(team)
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                self.data["teams"].pop(team["id"], None)
                return {"ok": False, "reason": f"团队编制没落盘：{exc}"}
        return {"ok": True, "team": dict(team)}

    def bind_room(self, team_id: str, room: str) -> dict[str, Any]:
        """把编制绑到主持人会话。同一个团队换会话继续（用户重开一轮）是合法的；
        一个房间同时只认一份编制，后绑的要说清楚覆盖了谁。"""
        rid, tid = _s(room), _s(team_id)
        with self._lock:
            team = self.data["teams"].get(tid)
            if team is None:
                return {"ok": False, "reason": f"团队「{tid}」不存在，绑不了房间"}
            prev = self.data["rooms"].get(rid, "")
            if prev and prev != tid:
                return {"ok": False, "reason": (
                    f"房间 {rid} 已绑在团队「{prev}」上：一个房间同时只有一份编制，"
                    "换编制请新建房间（新开一轮主持人会话），不要在这里悄悄换人"
                )}
            self.data["rooms"][rid] = tid
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                if prev:
                    self.data["rooms"][rid] = prev
                else:
                    self.data["rooms"].pop(rid, None)
                return {"ok": False, "reason": f"房间绑定没落盘：{exc}"}
        return {"ok": True, "room": rid, "teamId": tid, "previousTeamId": prev}

    def remove(self, team_id: str) -> dict[str, Any]:
        """删除编制：房间绑定一起清（留一条指向已删团队的绑定就是坏数据）。"""
        tid = _s(team_id)
        with self._lock:
            if tid not in self.data["teams"]:
                return {"ok": False, "reason": f"团队「{tid}」不存在"}
            team = self.data["teams"].pop(tid)
            rooms = [r for r, v in self.data["rooms"].items() if v == tid]
            for r in rooms:
                self.data["rooms"].pop(r, None)
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001  回滚，别让盘上说还在、内存说没了
                self.data["teams"][tid] = team
                for r in rooms:
                    self.data["rooms"][r] = tid
                return {"ok": False, "reason": f"团队删除没落盘：{exc}（编制已回滚，仍在）"}
        return {"ok": True, "teamId": tid, "unboundRooms": rooms}


class RoomStore(_DurableStore):
    """群聊时间线：每个房间一条追加式投影，条目带说话人（D-34 验收项②）。"""

    FILENAME = TEAM_ROOMS_FILENAME

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {"version": STORE_VERSION, "rooms": {}}

    def label(self) -> str:
        return "群聊时间线档"

    def on_load(self) -> None:
        rooms = self.data.get("rooms")
        if not isinstance(rooms, dict):
            self.data["rooms"] = {}

    def append(self, room: str, entry: dict[str, Any]) -> dict[str, Any]:
        rid = _s(room)
        if not rid:
            return {"ok": False, "reason": "房间 id 为空：这条投影不知道该挂在哪个群"}
        with self._lock:
            events = self.data["rooms"].setdefault(rid, [])
            item = {
                "atMs": int(entry.get("atMs") or self._ms()),
                "speakerAgentId": _s(entry.get("speakerAgentId")) or HOST_SPEAKER,
                "speakerName": _clip(_s(entry.get("speakerName")), MAX_NAME_CHARS),
                "kind": _s(entry.get("kind")) or "note",
                "text": _clip(_s(entry.get("text")), MAX_EVENT_TEXT_CHARS),
            }
            if entry.get("jobId"):
                item["jobId"] = _s(entry["jobId"])
            # 保序只按一个字段：atMs。派工与回叫的到达时刻来自投递时那份唤醒（同一个数），
            # 这里不重排、不自己盖时间戳，否则同一条消息在收件箱和上墙里会给出两个顺序。
            events.append(item)
            dropped = 0
            if len(events) > MAX_ROOM_EVENTS:
                dropped = len(events) - MAX_ROOM_EVENTS
                del events[:dropped]
                item["note"] = f"为守 {MAX_ROOM_EVENTS} 条上限，丢掉了最早的 {dropped} 条投影"
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                events.pop()
                return {"ok": False, "reason": f"群聊投影没落盘：{exc}"}
        out = {"ok": True, "entry": item, "total": len(self.data["rooms"].get(rid, []))}
        if dropped:
            out["dropped"] = dropped
        return out

    def events(self, room: str, *, after_ms: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            events = self.data["rooms"].get(_s(room), [])
            items = [dict(e) for e in events if int(e.get("atMs") or 0) > int(after_ms or 0)]
        items.sort(key=lambda e: (int(e.get("atMs") or 0), str(e.get("speakerAgentId") or "")))
        return items[-max(1, int(limit or 50)):]


class JobStore(_DurableStore):
    """派工在途台账（计划里的 `team_jobs` 增量表的引擎侧同事实源）。

    启动时把 `queued/running` 一律降级成 `interrupted`：进程没了，这两态就是「说过在跑、
    没人能证明」。绝不标成 ended——D-34 验收项③要的就是「未完成任务显示中断，不标记成功」，
    与 D-14.5 那条「被取消的任务照样说任务完成」是同一个坑，这里不再犯第二次。"""

    FILENAME = TEAM_JOBS_FILENAME

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {"version": STORE_VERSION, "jobs": {}}

    def label(self) -> str:
        return "团队派工台账"

    def on_load(self) -> None:
        jobs = self.data.get("jobs")
        if not isinstance(jobs, dict):
            self.data["jobs"] = {}
            return
        interrupted = 0
        for job in jobs.values():
            if isinstance(job, dict) and job.get("state") in INFLIGHT_STATES:
                job["state"] = "interrupted"
                job["interruptedAtMs"] = self._ms()
                job["note"] = "引擎重启：这条派工在途时进程结束，未确认它做完过任何事"
                interrupted += 1
                # 检查点索引跟着降级：`inflight` 的含义是「这份领取还活着，正文归执行体」，
                # 而进程没了之后它已经不再是「在途」那一份——它就是下一次派工该接的旧证据。
                # 不一起改的话重启后所有中断条目都停在 inflight，`carry_forward` 永远找不到
                # 候选，验收项③那句「可以重派」在盘上有证据却读不出来。
                # 这里**不**判断磁盘上还有没有那份快照（`status_of`/`carry_forward` 会读
                # `checkpoint_summary` 如实回答可不可续跑）：本层只把「谁不再是活的」说清。
                if job.get("checkpointKey") and job.get("checkpointState") == "inflight":
                    job["checkpointState"] = "interrupted"
                    job["checkpointInterruptedAtMs"] = job["interruptedAtMs"]
                    job["checkpointInterruptedByRestart"] = True
        self.data["restartedInterrupted"] = interrupted
        if interrupted and not self.load_error:
            # 降级结果写回磁盘：只改内存的话，`interruptedAtMs` 每次开机都被刷成新时刻，
            # 「这条什么时候断的」那条证据就丢了；落一次盘之后第二次启动能看出它上次已经
            # 中断过。写失败不影响正确性（读侧每次启动都会重算一遍），所以只留原话不抛。
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                self.load_error = f"重启降级没落盘（每次启动仍会重算，不影响读取）：{exc}"

    def record(self, job: dict[str, Any]) -> dict[str, Any]:
        jid = _s(job.get("jobId"))
        if not jid:
            return {"ok": False, "reason": "派工 id 为空：没有 id 就没法收口"}
        with self._lock:
            self.data["jobs"][jid] = dict(job)
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001  台账落不下去就必须说这条派工没有凭据
                self.data["jobs"].pop(jid, None)
                return {"ok": False, "reason": f"派工台账没落盘：{exc}（这条派工的在途凭据没有，重启后查不到它）"}
        return {"ok": True, "job": dict(job)}

    def transition(self, job_id: str, state: str, **fields: Any) -> dict[str, Any]:
        if state not in JOB_STATES:
            return {"ok": False, "reason": f"未知的派工状态「{state}」，只接受 {'/'.join(JOB_STATES)}"}
        jid = _s(job_id)
        with self._lock:
            job = self.data["jobs"].get(jid)
            if job is None:
                return {"ok": False, "reason": f"查不到派工「{jid}」：台账里没有的派工不能凭空收口"}
            if job.get("state") == "ended":
                return {"ok": False, "reason": (
                    f"派工「{jid}」已经收口（{job.get('endStatus') or '未注明'}），不能再改成 {state}"
                    "——同一件事收两次口就是要有一个是假的"
                )}
            before = dict(job)
            job.update({"state": state, "updatedAtMs": self._ms(), **fields})
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                self.data["jobs"][jid] = before
                return {"ok": False, "reason": f"派工状态没落盘：{exc}（保持原状态 {before.get('state')}）"}
        return {"ok": True, "job": dict(job)}

    def jobs_for_room(self, room: str, *, states: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        rid = _s(room)
        with self._lock:
            # 房间留空 = 不限房间（调用方是「查全部」而不是「查一个叫空串的派工」）。
            items = [
                dict(j) for j in self.data["jobs"].values()
                if not rid or j.get("room") == rid
            ]
        if states:
            items = [j for j in items if j.get("state") in states]
        items.sort(key=lambda j: (int(j.get("createdAtMs") or 0), str(j.get("jobId") or "")))
        return items

    def all_jobs(self, *, states: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """全量派工（跨房间）：`jobs_for_room("")` 按「房间是空串」过滤，答不出这个问题，
        所以自述「哪些房间还有在途派工」必须走这条，不能拿空房间号凑。"""
        with self._lock:
            items = [dict(j) for j in self.data["jobs"].values()]
        if states:
            items = [j for j in items if j.get("state") in states]
        items.sort(key=lambda j: (int(j.get("createdAtMs") or 0), str(j.get("jobId") or "")))
        return items

    def inflight_members(self, room: str) -> list[str]:
        """在途成员 id（去重保序）：移出成员与删团队都读这份判定。

        房间为空直接回空表——这里不许走 `jobs_for_room("")` 的「不限房间」口径，
        否则一个没绑过房间的团队会把别的团队的在途成员算成自己的，编制止删。"""
        rid = _s(room)
        if not rid:
            return []
        out: list[str] = []
        for job in self.jobs_for_room(rid, states=INFLIGHT_STATES):
            mid = str(job.get("memberId") or "")
            if mid and mid not in out:
                out.append(mid)
        return out

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self.data["jobs"].get(_s(job_id))
            return dict(job) if job else None

    def set_fields(self, job_id: str, **fields: Any) -> dict[str, Any]:
        """只改附挂字段、不动状态：给检查点索引这类「收口之后还要补记事实」的写入方用。

        为什么不开通 `transition` 的同态改写：`ended` 那道拒绝是**防重复收口**的，
        不是防补记的。检查点状态正好要在收口之后写（先问执行体收成了什么、再记档在不在），
        让索引侧绕开那条拒绝，比把「已经收口」这条真话改弱划算。"""
        jid = _s(job_id)
        with self._lock:
            job = self.data["jobs"].get(jid)
            if job is None:
                return {"ok": False, "reason": f"查不到派工「{jid}」：没有条目的补记无处可落"}
            before = dict(job)
            job.update({"updatedAtMs": self._ms(), **fields})
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                self.data["jobs"][jid] = before
                return {"ok": False, "reason": f"派工补记没落盘：{exc}（保持原字段）"}
        return {"ok": True, "job": dict(job)}

    def prune(self, room: str = "", team_id: str = "") -> dict[str, Any]:
        """清理已结束房间的台账条目（上限保护用，不做自动挤掉）。"""
        rid, tid = _s(room), _s(team_id)
        with self._lock:
            keep: dict[str, Any] = {}
            removed = 0
            for jid, job in self.data["jobs"].items():
                if rid and job.get("room") == rid and job.get("state") not in INFLIGHT_STATES:
                    removed += 1
                    continue
                if tid and job.get("teamId") == tid and job.get("state") not in INFLIGHT_STATES:
                    removed += 1
                    continue
                keep[jid] = job
            self.data["jobs"] = keep
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                self.data["jobs"] = keep
                return {"ok": False, "reason": f"台账清理没落盘：{exc}"}
        return {"ok": True, "removed": removed}


# ── 编排：团队运行时（派工 / 回叫 / 停止 / 状态）────────────────


class TeamRuntime:
    """主持人 ↔ 成员的编排层。

    **房间里两类执行体都住在同一张工单表里**（这是这一层最重要的一条映射）：

    - 主持人 = `ticket_id = 房间`（房间 id 就是主持人会话 id）；
    - 成员 = `ticket_id = 房间~成员 id`。

    于是 D-48 已经验收过的那三件事直接继承，不新造第二套：投递即落盘（同一个
    `TicketInbox`）、同工单保序（同人两次派工走 `existing-worker`，因此**同一成员串行**）、
    槽位与人用预留（不同成员各占一单，因此**不同成员并行**且挤不掉人）。
    成员回叫主持人就是把一条 `origin=agent` 的唤醒投进主持人那张单——主持人下一轮
    （由既有的 worker 回合或桌面领取回路执行）收到的就是「谁做完了什么」。
    """

    def __init__(
        self,
        *,
        workers: Any,
        teams: TeamStore | None = None,
        rooms: RoomStore | None = None,
        jobs: JobStore | None = None,
        checkpoints: Any = None,
        member_checkpoints: bool = True,
    ) -> None:
        #: 执行体注册表：必须是引擎那**一份**装配里的（`ticket_worker_service.get_runtime().registry`），
        #: 自建一份会有第二个槽位池和第二个内存箱——同一条唤醒两条保序，谁先谁后没人说得出。
        if workers is None:
            raise ValueError(
                "TeamRuntime 必须接引擎那一份执行体注册表：没有它，派工就只是台账上的一行字，"
                "既没有目录也没有槽位，更不能声称「已经派给成员」"
            )
        self.workers = workers
        self.teams = teams if teams is not None else TeamStore()
        self.rooms = rooms if rooms is not None else RoomStore()
        self.jobs = jobs if jobs is not None else JobStore()
        #: 成员真运行时检查点台账（缺陷 D-40.5 / #149）；没接就是只靠会话级检查点的旧行为。
        #: 默认接本进程那一份派工台账（同一份事实源，不自建第二份 jobs）；
        #: 自测要观察「没接台账」的行为时传 `member_checkpoints=False`。
        if checkpoints is None and member_checkpoints:
            checkpoints = ckpt.MemberCheckpointLedger(
                self.jobs, key_fn=twr.checkpoint_task_id
            )
        self.checkpoints = checkpoints
        self._dispatch_count: dict[str, int] = {}
        self._lock = threading.Lock()

    # ── 编制 ──────────────────────────────────────────────────

    def create_team(self, raw: Any, experts: list[dict], *, experts_error: str = "") -> dict[str, Any]:
        team, err = normalize_team(raw, experts, experts_error=experts_error)
        if err:
            return {"ok": False, "reason": err}
        return self.teams.upsert(team)

    def list_teams(self) -> list[dict[str, Any]]:
        out = []
        for team in self.teams.teams():
            rooms = self.teams.rooms_of_team(team["id"])
            out.append({
                **team,
                "room": rooms[0] if rooms else "",
                "rooms": rooms,
                "inflightMembers": self._team_inflight(team["id"], rooms),
            })
        return out

    def _team_inflight(self, team_id: str, rooms: list[str] | None = None) -> list[str]:
        """这个团队在途的成员 id（跨它绑到的**全部**房间，去重保序）。"""
        out: list[str] = []
        for room in (rooms if rooms is not None else self.teams.rooms_of_team(team_id)):
            for mid in self.jobs.inflight_members(room):
                if mid not in out:
                    out.append(mid)
        return out

    def open_room(self, *, team_id: str, session_id: Any) -> dict[str, Any]:
        """把编制绑到主持人会话；房间 id 就是会话 id，成员单与检查点都从它派生。"""
        try:
            room = room_of_session(session_id)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        res = self.teams.bind_room(_s(team_id), room)
        if not res.get("ok"):
            return res
        team = self.teams.get(_s(team_id)) or {}
        self.rooms.append(room, {
            "speakerAgentId": SYSTEM_SPEAKER,
            "kind": "room_opened",
            "text": f"团队「{team.get('name', '')}」已接入本会话，成员 "
                    + "、".join(m["name"] for m in team.get("members") or []),
        })
        return {**res, "team": team}

    def amend_members(
        self,
        *,
        team_id: str,
        member_ids: list[Any],
        experts: list[dict] | None,
        registry_error: str = "",
    ) -> dict[str, Any]:
        """改编制：只允许整体替换成员名单。在途成员**禁止**移出（D-34 验收项⑤的后半）。

        `experts` 可以为空（None / 空数组）——那时这次改编制只允许**在当前名单里减人或换序**：
        移出成员不需要重新校验专家（那份快照是建队时对着注册表验过的），而新增成员必须有
        注册表可查，否则「新加的那个人存不存在、绑的模型能不能用」只能靠猜。
        `registry_error` 是调用方「想带注册表但没读到」的原话，只在需要它的那条拒绝里转述。
        """
        team = self.teams.get(_s(team_id))
        if team is None:
            return {"ok": False, "reason": f"团队「{_s(team_id)}」不存在"}
        rooms = self.teams.rooms_of_team(team["id"])
        inflight = self._team_inflight(team["id"], rooms)
        # 与 dispatch 同一套取 id 的口径（可以给字符串，也可以给 {"id": ...}），并且一律
        # 转小写：编制里存的是注册表归一化后的小写 id，拿大写去比会把「没变」读成
        # 「又减又加」，凭空多出一条移出、又逼调用方补一份注册表。
        keep_ids: list[str] = []
        for ref in (member_ids or []):
            mid = _s(ref.get("id") if isinstance(ref, dict) else ref).lower()
            if mid and mid not in keep_ids:
                keep_ids.append(mid)
        keep = set(keep_ids)
        dropped = [m for m in team["members"] if m["id"] not in keep]
        blocked = [m for m in dropped if m["id"] in inflight]
        if blocked:
            names = "、".join(self._member_name(team, m["id"]) for m in blocked)
            return {"ok": False, "code": "MEMBER_INFLIGHT", "reason": (
                f"{names} 还有在途派工，不能移出：移出之后那份活就没人收口了。"
                "请先停掉那一轮（或等成员回完话）"
            ), "inflightMembers": inflight}
        stored_ids = {m["id"] for m in team["members"]}
        registry = list(experts or [])
        added = sorted(i for i in keep_ids if i not in stored_ids)
        if added and not registry:
            return {"ok": False, "code": "EXPERTS_REQUIRED", "reason": (
                f"新编制要加入不在当前名单里的成员（{'、'.join(added)}），但这次没有可用的专家注册表："
                + (f"读注册表的原话——{_s(registry_error)}；" if registry_error else "")
                + "请带 experts 或 session 再来。这里没有第二份可查的专家表，"
                "「不猜」的意思就是我不知道这个人存不存在"
            )}
        # 注册表里查不到的旧成员用编制快照补上：移出/换序不该因为「这一轮没下发注册表」而失败。
        for item in (
            [{"id": team["hostExpertId"], "name": team.get("hostExpertName", "")}]
            + list(team["members"])
        ):
            if item["id"] and er.find_expert(registry, item["id"]) is None:
                registry.append({
                    "id": item["id"], "name": item["name"] or item["id"],
                    "description": "", "prompt": "", "modelId": "", "modelName": "", "skills": [],
                })
        raw = {**team, "memberExpertIds": list(keep_ids)}
        merged, err = normalize_team(raw, registry)
        if err:
            return {"ok": False, "code": "ROSTER_INVALID",
                    "reason": f"新编制不合法，未改动：{err}"}
        # 归一化会重算 id（成员集合变了摘要就变）；改编制必须保住原团队 id，
        # 否则房间绑定与在途台账会指向一个查不到的团队。
        merged["id"] = team["id"]
        merged["createdAtMs"] = team.get("createdAtMs") or merged.get("createdAtMs")
        res = self.teams.upsert(merged)
        if res.get("ok"):
            # 每个绑了的房间都要上墙：只写第一个房间的话，换会话用的那一队人看不到名单变了。
            for room in rooms:
                self.rooms.append(room, {
                    "speakerAgentId": SYSTEM_SPEAKER,
                    "kind": "roster_changed",
                    "text": "成员名单已更新：" + "、".join(m["name"] for m in merged["members"]),
                })
        return res

    def remove_team(self, *, team_id: str, force: bool = False) -> dict[str, Any]:
        """删团队：在途成员没收口就不删。`force` 只允许在**已经没有在途**但台账里留着
        中断证据时使用（那些条目一并清掉），它不能用来跳过在途判定。"""
        team = self.teams.get(_s(team_id))
        if team is None:
            return {"ok": False, "reason": f"团队「{_s(team_id)}」不存在"}
        rooms = self.teams.rooms_of_team(team["id"])
        inflight = self._team_inflight(team["id"], rooms)
        live_members = [
            mid for mid in inflight
            if any(self._worker_live(member_ticket_id(room, mid)) for room in rooms)
        ]
        if live_members:
            names = "、".join(self._member_name(team, m) for m in live_members)
            return {"ok": False, "code": "MEMBER_INFLIGHT", "reason": (
                f"团队里有成员正在跑（{names}）：删掉编制之后它们的回叫会找不到收件人，"
                "所以这里禁止删除。请先停主持人本轮，再删"
            ), "inflightMembers": inflight}
        # 按 teamId 查，不按房间查：未绑过房间的团队（room 为空）如果拿空串去过滤台账，
        # 「不限房间」的口径会把别的团队的在途条目算到它头上，删不掉了还说不出为什么。
        pending = [
            j for j in self.jobs.all_jobs(states=INFLIGHT_STATES)
            if j.get("teamId") == team["id"]
        ]
        if pending and not force:
            return {"ok": False, "code": "LEDGER_PENDING", "reason": (
                f"{len(pending)} 条派工仍记着在途状态（执行体那侧已经没有活绑定）："
                "确认这些活都不用了再带 force=true 删除，否则台账会留下指向已删团队的条目"
            ), "jobs": [p.get("jobId") for p in pending]}
        res = self.teams.remove(team["id"])
        if res.get("ok"):
            self.jobs.prune(team_id=team["id"])
        return res

    # ── 派工 ──────────────────────────────────────────────────

    def dispatch(
        self,
        *,
        room: Any,
        member_ids: list[Any],
        brief: str,
        title: str = "",
        max_parallel: int = MAX_MEMBERS_PER_TEAM,
    ) -> dict[str, Any]:
        """主持人一次并行派工；每人一条 Assignment，同一成员的第二条走既有串行路径。

        三条拒绝口径：成员不在编制里 → 拒（不静默派给「差不多的人」）；槽位排不上 →
        这条记 `refused` 并把原话回给主持人（不假装在跑）；房间没绑编制 → 整次拒。"""
        rid = _s(room)
        try:
            rid = room_of_session(rid)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        team = self.teams.by_room(rid)
        if team is None:
            return {"ok": False, "reason": (
                f"会话 {rid} 没有绑定团队：先用 /api/experts/teams 建队并绑定房间，"
                "或者本轮自己完成（没有编制就没有成员）"
            )}
        body = _clip(_s(brief), MAX_BRIEF_CHARS)
        if not body:
            return {"ok": False, "reason": "派工说明为空：成员看不到主持人的对话，说明必须自足"}
        known = {m["id"]: m for m in team["members"]}
        wanted: list[str] = []
        unknown: list[str] = []
        for ref in (member_ids or []):
            mid = _s(ref.get("id") if isinstance(ref, dict) else ref).lower()
            if not mid:
                unknown.append(str(ref))
                continue
            if mid not in known:
                unknown.append(mid)
                continue
            if mid in wanted:  # 同一次调用里重复写同一个人：并成一条，不派两遍同一件事
                continue
            wanted.append(mid)
        if not wanted:
            return {"ok": False, "reason": "没有可用成员：" + (
                f"这些 id 不在编制里 → {'、'.join(unknown)}" if unknown else "member_ids 是空的"
            )}
        if len(wanted) > max_parallel:
            return {"ok": False, "reason": (
                f"一次最多并行派 {max_parallel} 人（本次点名 {len(wanted)} 人）；"
                "分两批派，先收到第一批的结论"
            )}
        with self._lock:
            self._dispatch_count[rid] = self._dispatch_count.get(rid, 0) + 1
            count = self._dispatch_count[rid]
        if count > MAX_DISPATCHES_PER_ROOM:
            self._dispatch_count[rid] = count - 1
            return {"ok": False, "reason": (
                f"本房间已派工 {count - 1} 次、到上限 {MAX_DISPATCHES_PER_ROOM}："
                "先收口再派，否则主持人在循环里不断加活，谁也不知道这队人到底在忙什么"
            )}

        did = new_dispatch_id(team["id"], rid, body)
        dispatched: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        for mid in wanted:
            member = known[mid]
            ticket = member_ticket_id(rid, mid)
            jid = job_id_of(did, mid)
            note_fields: dict[str, Any] = {}
            if self.checkpoints is not None:
                carried = self.checkpoints.carry_forward(
                    room=rid, member_id=mid, next_assignment_id=jid,
                    title=_clip(title or member["name"], MAX_NAME_CHARS),
                )
                if carried.get("carriedFrom"):
                    note_fields = {
                        "recoveryNote": carried.get("recoveryNote") or "",
                        "carriedFrom": carried.get("carriedFrom"),
                        "carriedResumable": bool(carried.get("resumable")),
                        # 「这句注记是谁写的」要看得见：接上调度器时它生成的是与重启补查
                        # 同一口径的长注记，没接时是这里的兜底文案。都标成 dispatcher
                        # 就是把没做的事说成做过。
                        "noteSource": _s(carried.get("noteSource")) or "fallback",
                    }
            res = self.workers.start_or_wake(
                ticket_id=ticket,
                title=_clip(title or member["name"], MAX_NAME_CHARS),
                assignment_id=jid,
                message=body,
                origin="agent",  # 派工是 Agent 起活：不许占人用预留槽（D-48 的槽位口径）
                recovery_note=note_fields.get("recoveryNote", ""),
            )
            job = {
                "jobId": jid,
                "dispatchId": did,
                "room": rid,
                "teamId": team["id"],
                "memberId": mid,
                "memberName": member["name"],
                "ticketId": ticket,
                "assignmentId": jid,
                "brief": _clip(body, MAX_NOTE_CHARS),
                "createdAtMs": _now_ms(),
                "updatedAtMs": _now_ms(),
                "delivered": _s(res.get("delivered")),
                "state": "queued" if res.get("ok") else "refused",
                "workspaceDir": _s(res.get("workspaceDir")),
                "dirName": _s(res.get("dirName")),
                "checkpointKey": _s(res.get("checkpointKey")),
                "queuedWakes": int(res.get("queuedWakes") or 0),
                **note_fields,
            }
            if not res.get("ok"):
                job["reason"] = _s(res.get("reason"))
            rec = self.jobs.record(job)
            if not rec.get("ok"):
                # 台账落不下去：这条派工在磁盘上没有任何凭据。执行体那侧已经投进箱了
                # （start_or_wake 先落耐久收件箱才回 ok），所以**不能**回称失败——
                # 那是把已发生的投递说成没发生。如实带出两条事实让主持人判断。
                refused.append({"memberId": mid, "memberName": member["name"],
                                "reason": f"派工台账没落盘：{rec.get('reason')}",
                                "deliveredAnyway": bool(res.get("ok"))})
                continue
            if res.get("ok") and self.checkpoints is not None:
                # 只在真的投递成功后登记在途检查点键，并且按执行体**实际**绑上的那份领取
                # 索引：同一成员的第二条派工会并进他当前那份领取（existing-worker），
                # 这时 job id 只是台账号，真正会落检查点的是实际那一份。
                self.checkpoints.record_assignment(
                    room=rid, member_id=mid, assignment_id=jid, ticket_id=ticket,
                    delivered_assignment_id=_s(res.get("assignmentId")),
                    checkpoint_key=_s(res.get("checkpointKey")),
                )
            if res.get("ok"):
                dispatched.append(job)
            else:
                refused.append(job)
            if res.get("journalWarning"):
                job["journalWarning"] = res["journalWarning"]

        if dispatched:
            names = "、".join(d["memberName"] for d in dispatched)
            self.rooms.append(rid, {
                "speakerAgentId": HOST_SPEAKER,
                "speakerName": team.get("hostExpertName", ""),
                "kind": "dispatch",
                "text": f"已请 {names} 处理：" + _clip(_s(title) or body, 200),
                "jobId": did,
            })
        return {
            "ok": bool(dispatched),
            "dispatchId": did,
            "room": rid,
            "teamId": team["id"],
            "dispatched": dispatched,
            "refused": refused,
            "unknownMembers": unknown,
            "reason": (
                "" if dispatched else
                "一条也没派出去：" + "；".join(_s(r.get("reason")) for r in refused if r.get("reason"))
            ),
            "notes": self._dispatch_notes(dispatched, refused, unknown),
        }

    def _dispatch_notes(self, dispatched: list, refused: list, unknown: list) -> list[str]:
        notes: list[str] = []
        for d in dispatched:
            if d.get("delivered") == "existing-worker":
                notes.append(f"{d['memberName']}：这条排在他当前那件活后面（同一成员串行）")
            if d.get("carriedFrom"):
                notes.append(
                    f"{d['memberName']}：接上了他上一次中断的检查点"
                    f"（{'可续跑' if d.get('carriedResumable') else '但那一份没有可恢复快照，按新任务从头做'}）"
                )
            if d.get("journalWarning"):
                notes.append(f"{d['memberName']}：{d['journalWarning']}")
        for r in refused:
            if isinstance(r, dict) and r.get("reason"):
                notes.append(f"{r.get('memberName') or r.get('memberId') or '成员'}：{r['reason']}")
        if unknown:
            notes.append("这些成员不在编制里，没有派工：" + "、".join(unknown))
        return notes

    # ── 成员收口 → 回叫主持人 ─────────────────────────────────

    def member_reported(
        self,
        *,
        room: Any,
        member_id: Any,
        job_id: Any,
        report: str,
        ok: bool = True,
        end_status: str = "finished",
    ) -> dict[str, Any]:
        """成员把话说完：收执行体那侧的口、上墙一条带说话人的回复，然后回叫主持人。

        失败也要回叫（`ok=False`）——主持人必须知道「这摊活没人做完了」才能改派或自己接手；
        静默失败正是 D-34 要修的旧毛病。收口状态沿用执行体那三态：`finished` 删检查点，
        `dropped/lost` 保留证据（本层不自己发明第四态，也不重复实现删档——归档动作
        归 `TicketWorkerRegistry.end_assignment`，这里只转述它的回执）。"""
        rid, mid, jid = _s(room), _s(member_id).lower(), _s(job_id)
        body = _clip(_s(report), MAX_EVENT_TEXT_CHARS)
        if not rid or not mid:
            return {"ok": False, "reason": "房间 id 与成员 id 都必填：缺一个就不知道这条回复属于谁"}
        team = self.teams.by_room(rid)
        name = self._member_name(team, mid) if team else mid
        # 「这一轮没成功」与「正常干完」不能同时成立：finished 会让执行体删掉那份检查点，
        # 而失败的成员恰恰是最需要留证据的。口径不一致时按没做完收口。
        final_status = end_status
        if not ok and final_status == "finished":
            final_status = "dropped"
        # 先收执行体那侧的口：归还槽位、按状态处理检查点文件。不做这一步，成员跑完
        # 一轮后槽位永远占着，三个 Agent 槽用满整个房间就再也派不动活。
        ended = self.workers.end_assignment(
            member_ticket_id(rid, mid), final_status,
            reason="" if ok else _clip("成员这一轮没有成功收尾", MAX_NOTE_CHARS),
        )
        transition = self.jobs.transition(
            jid, "ended", endStatus=final_status, endReason="" if ok else "成员这一轮没有成功",
            reportedAtMs=_now_ms(), checkpointDeleted=bool(ended.get("checkpointDeleted")),
        )
        if not transition.get("ok"):
            # 台账没收下这次收口（已经收过口、或压根查不到这条派工）：这时**不再上墙、
            # 也不再回叫**。理由很直白——同一件事收两次口就要有一个是假的，而第二次回叫
            # 会让主持人再读一遍「某人交回了结论」，那才是 D-34 要修的重复。
            # 结论正文原样回给调用方，不冒充已经写进群聊。
            return {
                "ok": False,
                "room": rid,
                "memberId": mid,
                "jobId": jid,
                "endStatus": final_status,
                "ledger": transition,
                "workerEnd": ended,
                "callback": {"ok": False, "reason": "台账没收下这次收口，没有发起回叫"},
                "report": body,
                "note": f"台账没改动：{transition.get('reason')}（没有上墙、没有回叫，"
                        "否则同一份结论主持人会读两遍）",
            }
        self.rooms.append(rid, {
            "speakerAgentId": mid,
            "speakerName": name,
            "kind": "member_failed" if not ok else "member_reply",
            "text": ("" if ok else "【未完成】") + (body or "（成员没有给出文本结论）"),
            "jobId": jid,
        })
        if self.checkpoints is not None:
            self.checkpoints.release(
                room=rid, member_id=mid, assignment_id=jid,
                status=final_status, end_result=ended,
            )
        callback = self.notify_host(
            room=rid,
            text=self._callback_text(name, body, ok=ok),
            origin_member=mid,
            job_id=jid,
        )
        out = {
            "ok": bool(transition.get("ok")),
            "room": rid,
            "memberId": mid,
            "jobId": jid,
            "endStatus": final_status,
            "ledger": transition,
            "workerEnd": ended,
            "callback": callback,
            # 「谁收的口」要说得出：台账改不动（重复收口 / 查不到那条）时不能只回一个 False。
            "note": (
                "台账已收口" if transition.get("ok") else f"台账没改动：{transition.get('reason')}"
            ),
        }
        if not callback.get("ok"):
            out["callbackNote"] = f"主持人回叫没投出去：{callback.get('reason')}"
        return out

    @staticmethod
    def _callback_text(name: str, body: str, *, ok: bool) -> str:
        head = f"{name} 回话了" if ok else f"{name} 这一轮没有做完"
        return f"【成员回叫】{head}：{body or '（无正文）'}"

    def notify_host(self, *, room: Any, text: str, origin_member: str = "", job_id: str = "") -> dict[str, Any]:
        """把一条成员回叫投进主持人那张单。

        **这条不占 Agent 槽位**，两种落点：

        1. 主持人当前有活绑定（他的这一轮正在跑）：走 `TicketWorkerRegistry.start_or_wake`
           的 existing-worker 分支——进内存箱 + 同一次锁内落耐久盘，返回 ok 之前已经写完
           （D-48.6 验收过的那条顺序，本层直接继承，不另造投递路径）。
        2. 主持人没有活绑定（这一轮已经结束，或桌面还没起下一轮）：**不去替他起一份新领取**。
           起新领取要占一个 Agent 槽，而团队里槽位本该留给成员；更要紧的是主持人那一轮是
           用户面前那个会话（由桌面/`/api/chat` 那条回路跑），本层替他占着槽、又没人去跑它，
           结果就是三个槽被三份「没人执行的主持人领取」占死，这个房间再也派不动活。
           所以这条唤醒**只落耐久收件箱**：`host_pending` 读得到它，D-48 的重启补查读得到它，
           主持人下一轮起来就能拿走——回叫不会因为槽位或时序而丢。

        耐久箱也没接上时（这套装配不完整）如实回失败，绝不回「已送达」。
        """
        rid = _s(room)
        body = _clip(_s(text), MAX_EVENT_TEXT_CHARS)
        if not body:
            return {"ok": False, "reason": "回叫正文为空：主持人收到一句话都没有的唤醒只会白起一轮"}
        did = _s(job_id) or f"cb-{_digest(f'{rid}|{origin_member}|{_now_ms()}', 12)}"
        ticket = host_ticket_id(rid)
        res: dict[str, Any] = {"ok": False, "reason": "主持人这一轮没有活的领取绑定", "ticketId": ticket}
        if self._worker_live(ticket):
            # 只在已经有绑定时调 start_or_wake：那条路不占新槽，且投递即落盘。
            res = self.workers.start_or_wake(
                ticket_id=ticket, title="团队主持人", assignment_id=did,
                message=body, origin="agent",
            )
            if res.get("ok"):
                return {
                    **res,
                    "ticketId": ticket,
                    "assignmentId": _s(res.get("assignmentId")) or did,
                    "fromMember": _s(origin_member),
                }
        journal = getattr(self.workers, "inbox_journal", None)
        if journal is None:
            return {
                **res,
                "ok": False,
                "ticketId": ticket,
                "fromMember": _s(origin_member),
                "note": "这套装配没接耐久收件箱，主持人又没有活绑定：这条回叫只活在内存里，"
                        "重启就没了——请如实当成投递失败，不要当「主持人已经收到」",
            }
        arrival = _now_ms()
        # 占位领取号带 `pending-` 前缀：它**不是**一份真领取（没占到槽位、没起绑定）。
        # 耐久箱的键按 (正文, 到达时间, 领取) 算，主持人下一轮补查会按到达键一起销账，
        # 所以这个号只用来满足「每条记录必须有归属」，不会被当成活绑定去查。
        journaled = journal.append(
            ticket_id=ticket, assignment_id=f"pending-{did}", text=body,
            origin="agent", at_ms=arrival,
        )
        if not journaled.get("ok"):
            return {
                **res,
                "ok": False,
                "ticketId": ticket,
                "fromMember": _s(origin_member),
                "note": f"耐久收件箱没收下这条回叫：{journaled.get('reason')}"
                        "（主持人那边：{}）".format(_s(res.get("reason"))),
            }
        return {
            "ok": True,
            "delivered": "journal-only",
            "ticketId": ticket,
            "assignmentId": did,
            "fromMember": _s(origin_member),
            "wakeId": _s(journaled.get("wakeId")),
            "slotNote": _s(res.get("reason")),
            "note": "主持人这一轮不在跑：这条回叫已落耐久收件箱，等他下一轮（或重启补查）读走"
                    "——没起新领取，也没占 Agent 槽位",
        }

    def host_pending(self, *, room: Any, limit: int = 50) -> dict[str, Any]:
        """主持人还没读走的回叫：直接从**耐久收件箱**按房间查，不依赖内存箱还在不在。

        这条只读不动盘——引擎重启、内存箱蒸发之后它照样答得出「谁在等主持人」。"""
        journal = getattr(self.workers, "inbox_journal", None)
        rid = _s(room)
        if journal is None:
            return {"ok": True, "enabled": False, "pending": [], "reason": (
                "这套装配没接耐久收件箱（inbox_journal=False）：回叫只活在内存箱里，"
                "重启后这一段无从查证——请走 /api/tickets 那侧的装配，不要在这里降级成「当作没有」"
            )}
        items = journal.pending_wakes_for_ticket(rid)[-max(1, int(limit or 50)):]
        return {"ok": True, "enabled": True, "room": rid, "pending": items, "count": len(items)}

    # ── 停止：只停主持人 ──────────────────────────────────────

    def stop_host_round(self, *, room: Any, reason: str = "用户停止了主持人本轮") -> dict[str, Any]:
        """停主持人本轮：**成员不受影响**，继续跑完并回叫（D-34 验收项④）。

        收口用 `dropped` 而不是 `finished`：主持人这一轮是被停的，没有「做完」，
        标成 finished 会顺手删掉它那份可交接检查点，续跑就无从谈起。

        「主持人此刻根本没有活绑定」是合法状态（这一轮已经跑完了），不是错误：用户点停止
        要的是「主持人别再往下走」，这个结果已经成立，所以回 ok 但把 `hostRoundClaimed=False`
        带出来，界面上写「这一轮已经结束，没有可停的」——不去墙上补一条「已停止」，
        因为那一轮不是被这次点击停的。"""
        rid = _s(room)
        ticket = host_ticket_id(rid)
        res = self.workers.end_assignment(ticket, "dropped", reason=_clip(_s(reason), MAX_NOTE_CHARS))
        claimed = bool(res.get("ok"))
        if not claimed:
            # 没有活绑定：把执行体那侧的原话留在 workerReason，避免界面把它当成失败弹窗，
            # 也避免这里替它编一句「一切正常」。
            res = {
                "ok": True,
                "ticketId": ticket,
                "hostRoundClaimed": False,
                "workerReason": _s(res.get("reason")),
            }
        else:
            res["hostRoundClaimed"] = True
            self.rooms.append(rid, {
                "speakerAgentId": SYSTEM_SPEAKER,
                "kind": "host_stopped",
                "text": "主持人本轮已停止；成员仍在跑的活会照常回叫（停止不牵连成员）",
            })
        inflight = self.jobs.inflight_members(rid)
        res["inflightMembers"] = inflight
        res["stoppedScope"] = "host"
        res["note"] = (
            f"只停了主持人这一轮；{len(inflight)} 名成员的派工仍在途，收口后照常回叫"
            if inflight else "只停了主持人这一轮；当前没有在途成员"
        )
        if not claimed:
            res["note"] = (
                f"主持人这一轮没有活的领取（{res['workerReason'] or '已经收口'}）："
                f"这次点击没有停掉任何东西，成员该跑的照旧；在途 {len(inflight)} 名"
            )
        return res

    def end_member_job(
        self, *, room: Any, member_id: Any, job_id: Any, status: str, reason: str = ""
    ) -> dict[str, Any]:
        """成员收口但没有正文（超时/被杀/主动放弃）：留证据，不回叫成「做完了」。"""
        if status not in ("dropped", "lost"):
            return {"ok": False, "reason": (
                f"「{status}」不是这里能收的口：有结论走 member_reported，"
                "没做完只能记 dropped / lost（finished 会删掉那份检查点证据）"
            )}
        rid, mid = _s(room), _s(member_id).lower()
        ticket = member_ticket_id(rid, mid)
        res = self.workers.end_assignment(ticket, status, reason=_clip(_s(reason), MAX_NOTE_CHARS))
        if res.get("ok") and self.checkpoints is not None:
            self.checkpoints.release(
                room=rid, member_id=mid, assignment_id=_s(job_id) or _s(res.get("assignmentId")),
                status=status, end_result=res,
            )
        if res.get("ok"):
            self.jobs.transition(
                _s(job_id), "ended", endStatus=status, endReason=_clip(_s(reason), MAX_NOTE_CHARS)
            )
            self.rooms.append(rid, {
                "speakerAgentId": mid,
                "speakerName": self._member_name(self.teams.by_room(rid), mid),
                "kind": "member_stopped",
                "text": f"成员这一轮没做完就结束（{status}）：{_clip(_s(reason), 300) or '未说明原因'}",
            })
        return res

    # ── 状态与提示词 ─────────────────────────────────────────

    def room_state(self, *, room: Any, timeline_limit: int = 30) -> dict[str, Any]:
        rid = _s(room)
        if not rid:
            return {"ok": False, "reason": "房间 id 为空：没有房间就没有成员状态可查"}
        team = self.teams.by_room(rid)
        jobs = self.jobs.jobs_for_room(rid)
        members = []
        for member in (team or {}).get("members") or []:
            mine = [j for j in jobs if j.get("memberId") == member["id"]]
            latest = mine[-1] if mine else {}
            live = self._worker_live(member_ticket_id(rid, member["id"]))
            entry = {
                "id": member["id"],
                "name": member["name"],
                "ticketId": member_ticket_id(rid, member["id"]),
                "inflight": member["id"] in self.jobs.inflight_members(rid),
                "workerLive": live,
                "jobs": len(mine),
                "lastState": latest.get("state", ""),
                "lastJobId": latest.get("jobId", ""),
            }
            if self.checkpoints is not None:
                entry["checkpoint"] = self.checkpoints.status_of(rid, member["id"])
            members.append(entry)
        return {
            "ok": True,
            "room": rid,
            "team": team,
            "hostTicketId": host_ticket_id(rid),
            "hostLive": self._worker_live(host_ticket_id(rid)),
            "members": members,
            "jobs": jobs,
            "timeline": self.rooms.events(rid, limit=timeline_limit),
            "dispatchCount": self._dispatch_count.get(rid, 0),
            "restartedInterrupted": int(self.jobs.data.get("restartedInterrupted") or 0),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "teams": len(self.teams.teams()),
            "inflightRooms": sorted({
                str(j.get("room") or "")
                for j in self.jobs.all_jobs(states=INFLIGHT_STATES)
                if j.get("room")
            }),
            "registry": self.workers.snapshot() if hasattr(self.workers, "snapshot") else {},
            "stores": {
                "teams": self.teams.snapshot(),
                "rooms": self.rooms.snapshot(),
                "jobs": self.jobs.snapshot(),
            },
        }

    def _member_name(self, team: dict | None, member_id: str) -> str:
        for m in (team or {}).get("members") or []:
            if m.get("id") == _s(member_id):
                return str(m.get("name") or member_id)
        return _s(member_id)

    def _worker_live(self, ticket_id: str) -> bool:
        worker = None
        try:
            worker = self.workers.worker(ticket_id)
        except Exception:  # noqa: BLE001  注册表读不出活绑定就当不活，不据此删编制
            return False
        return bool(worker is not None and worker.assignment_id and worker.ended_status == "")

    # ── 主持人的工具面与提示词（交给 toolkit_orchestrate 车道）──

    def system_hint(self, *, room: Any) -> str:
        team = self.teams.by_room(_s(room))
        if team is None:
            return ""
        lines = [
            "\n\n## 团队编制（异步派工）",
            f"团队「{team['name']}」由你主持。你是主持人，不做专业工作："
            "分工、汇总、判断是否收工是你的活，专业产出交给成员。",
            f"- 主持人专家：{team['hostExpertName']}（{team['hostExpertId']}）",
        ]
        for m in team["members"]:
            lines.append(f"- 成员：{m['name']}（{m['id']}）")
        lines.append(
            f"用 team_dispatch 一次可以并行派给多名成员（同一成员自动串行，按你派给他的先后顺序做）；"
            f"每个成员的活上限 {MAX_MEMBERS_PER_ROOM_JOBS} 条在途。成员回话会作为新的唤醒送到你这里，"
            "带的是「谁做完了什么」，未经验证，必须由你核对后再整合；失败项要如实说出来，"
            "不能当成已完成。团队过程用 team_room_status 查。"
        )
        return "\n".join(lines)

    def tools(self, *, room: Any, emit: Callable[[str, dict], None] | None = None) -> list[Callable]:
        """返回绑好本房间的两个工具闭包（team_dispatch / team_room_status）。

        房间在闭包里定死：模型说的「派给谁」只会落进**它自己这一轮所在的房间**，
        不可能通过参数把活投到别人的会话里。"""
        rid = _s(room)

        async def team_dispatch(member_ids_json: str, brief: str, title: str = ""):
            """把一件可以独立完成的工作并行派给团队成员（同一成员自动串行排队）。

            Args:
                member_ids_json (`str`):
                    成员专家 id 数组的 JSON 字符串，例如 ["researcher","writer"]；
                    也可以写成 [{"id": "researcher"}]。只能填本团队编制里的成员。
                brief (`str`):
                    自足的任务说明。成员看不到你与此前的对话，缺的信息必须写在这里。
                title (`str`):
                    一句话标题，用于上墙与成员目录的标记，可省略。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            try:
                parsed = json.loads(member_ids_json or "[]")
            except Exception as exc:  # noqa: BLE001
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：member_ids_json 不是合法 JSON：{exc}"
                )])
            if isinstance(parsed, (str, dict)):
                parsed = [parsed]
            if not isinstance(parsed, list) or not parsed:
                return ToolResponse(content=[TextBlock(
                    type="text", text="错误：member_ids_json 要是非空的 id 数组"
                )])
            step = f"team-dispatch-{_digest(f'{rid}|{brief}|{_now_ms()}', 8)}"
            if emit is not None:
                emit("step_start", {
                    "step_id": step,
                    "stepType": "team",
                    "speakerAgentId": HOST_SPEAKER,
                    "description": f"主持人派工：{_clip(title or brief, 160)}",
                })
            result = self.dispatch(room=rid, member_ids=parsed, brief=brief, title=title)
            text = render_dispatch_result(result)
            if emit is not None:
                emit("step_done", {"step_id": step, "output": _clip(text, 300)})
            return ToolResponse(content=[TextBlock(type="text", text=text)])

        async def team_room_status(after_ms: int = 0, limit: int = 30):
            """查看团队过程：成员在忙什么、谁回了话、还有哪些派工在途。

            Args:
                after_ms (`int`):
                    只看这个时刻之后的群聊条目（毫秒），填上次结果里最大的 atMs 即可翻页。
                limit (`int`):
                    返回条数上限，1–100。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            state = self.room_state(room=rid, timeline_limit=max(1, min(int(limit or 30), 100)))
            events = self.rooms.events(rid, after_ms=int(after_ms or 0), limit=100)
            lines = [f"团队过程（房间 {rid}）："]
            for ev in events:
                who = ev.get("speakerName") or ev.get("speakerAgentId")
                lines.append(f"- [{ev.get('atMs')}] {who}（{ev.get('kind')}）：{ev.get('text')}")
            if not events:
                lines.append("- 这段时间里没有新发言")
            pending = self.host_pending(room=rid)
            if pending.get("ok") and pending.get("enabled"):
                lines.append(f"等你（主持人）读走的回叫：{pending.get('count', 0)} 条")
            return ToolResponse(content=[TextBlock(type="text", text="\n".join(lines))])

        return [team_dispatch, team_room_status]


def render_dispatch_result(result: dict[str, Any]) -> str:
    """把派工结果摊成给主持人看的中文；一条也没派出时绝不写成「已派工」。"""
    if not result.get("ok"):
        return f"没有派工成功：{result.get('reason') or '未知原因'}"
    dispatched = result.get("dispatched") or []
    refused = result.get("refused") or []
    lines = [
        f"已派工 {len(dispatched)} 名成员（派工号 {result.get('dispatchId')}）。"
        "成员是异步的：这一轮先记下「已请谁做什么」，收到回叫再整合，"
        "不要在没有结论时宣称成员已经完成。",
    ]
    for d in dispatched:
        serial = "（排在他当前那件活后面）" if d.get("delivered") == "existing-worker" else ""
        lines.append(f"- {d.get('memberName')}：{serial or '已起新的一份活'}；目录 {d.get('dirName')}")
    for r in refused:
        lines.append(f"- {r.get('memberName') or r.get('memberId')}：没派出去——{r.get('reason')}")
    notes = [n for n in (result.get("notes") or []) if n]
    if notes:
        lines.append("说明：" + "；".join(notes))
    return "\n".join(lines)


# ── 装配现场：一进程一套（与 ticket_worker_service 同一套数据根纪律）──────

_RUNTIME: "TeamRuntime | None" = None
_RUNTIME_HOME = ""
_INIT_LOCK = threading.Lock()


def peek_runtime() -> "TeamRuntime | None":
    """只看现成的一套，绝不顺手装配：自测与状态查询用它区分「没接」与「接坏了」。"""
    return _RUNTIME


def reset_runtime() -> dict[str, Any]:
    """丢弃装配（自测换数据根、或引擎换 home 时用）。

    只清引用，不关执行体——执行体那一份归 `ticket_worker_service.reset_runtime` 管，
    这里越权关掉会把工单车道正在跑的回合一起带走。"""
    global _RUNTIME, _RUNTIME_HOME
    with _INIT_LOCK:
        previous = _RUNTIME
        _RUNTIME = None
        _RUNTIME_HOME = ""
        return {"cleared": previous is not None}


def get_team_runtime(*, workers: Any = None) -> "TeamRuntime":
    """取本进程那一份团队运行时；数据根变了就重建（同一份装配只活在它自己的目录里）。

    `workers` 缺省取 `ticket_worker_service` 那一份注册表——**只取，不新建**：
    新建一个注册表等于开第二个槽位池和第二个内存箱，同一条唤醒会有两套保序，
    而 D-48 验收过的「同工单串行」从此只对一半。取不到就如实抛，调用方（车道路由）
    把它翻成一条人话错误，不降级成「先建个空的」。
    """
    global _RUNTIME, _RUNTIME_HOME
    home = str(dp.engine_home())
    with _INIT_LOCK:
        if _RUNTIME is not None and _RUNTIME_HOME != home:
            reset_runtime()
        if _RUNTIME is None:
            resolved = workers
            service = None
            if resolved is None:
                import ticket_worker_service as tws  # 延迟导入：装配现场由服务侧决定

                service = tws.peek_runtime() or tws.get_runtime()
                resolved = service.registry
            runtime = TeamRuntime(workers=resolved)
            if service is not None and runtime.checkpoints is not None:
                # 恢复注记交给调度器生成（与重启补查同一条口径），台账只负责问「接哪一份」。
                # 接不上就用兜底文案：口径差在 snapshot() 里如实标出来，不混着说。
                runtime.checkpoints.bind_dispatcher(getattr(service, "dispatcher", None))
            _RUNTIME = runtime
            _RUNTIME_HOME = home
        return _RUNTIME
