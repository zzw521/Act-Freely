# -*- coding: utf-8 -*-
"""可视化工作流编排（缺陷 D-10 / 卡 #156）：触发 → 动作 → 条件 → 人工卡点。

先说清这个模块**不做**什么，免得读端点时误会「引擎会自己跑工作流」：

* **不新造执行体**（约束 A）。动作步的落地方式是往 D-48 那张工单表里投一条唤醒
  （`TicketWorkerRegistry.start_or_wake`）或走 #148 的团队派工（`TeamRuntime.dispatch`）。
  于是「投递即落盘、同工单保序、槽位与人用预留」三条一并继承，本文件里没有第二个队列，
  也没有第二次保序。回合起不起、什么时候起，仍由 worker 调度器与桌面领取回路决定。
* **不新造审批开关**（约束 C）。人工卡点的授权口径就是 `tool_approval.needs_approval`
  那一个门（档位 `off` / `sensitive` / `all`，缺省 `off`）：
  门说不需要问人 ⇒ 这一步如实放行，并在运行视图里写 `decidedBy: "policy-off"`，
  界面不许把它显示成「人已批准」；门说需要问人而网关不在 ⇒ **挂起等人**，
  不静默绕过（与 `tool_approval` 头注释里「网关不可用且档位已开启时不允许静默绕过」同口径）。
  `SENSITIVE_TOOLS` 在本仓没有消费者，这里也没拿它当门。
* **不执行任意代码**。条件判断是九个算子的数据比对，没有 `eval`、没有表达式语言。
  「可视化」要的是定义 + 每步视图能被界面画出来，不是让引擎跑脚本。

版本化与导入导出按 D-10 方案第 3 句做：每次保存都升一版，旧版进 `versions` 有界历史；
导出是一份带 `formatVersion` 的 JSON，导入对着同一套校验器过一遍——**导入不认「先收下再修」**，
收下了一份跑不通的定义，下次运行才会知道，而那时已经投出去几条唤醒了。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

import team_runtime as tm
import tool_approval as tap

#: 两份落盘文件名（定义一本含版本历史，运行账本一本；同样请 W4 合并时搬进 `data_paths`，
#: 见分片 docs/并行实施/156.md）。
WORKFLOW_REGISTRY_FILENAME = "workflows.json"
WORKFLOW_RUNS_FILENAME = "workflow_runs.json"

STORE_VERSION = 1
MAX_WORKFLOWS = 64
MAX_STEPS = 40
MAX_NAME_CHARS = 60
MAX_TEXT_CHARS = 2_000
#: 人工卡点的问题原文上限，与服务端 `MAX_QUESTION` 同源（自测 `group_crosswire` 钉住）。
MAX_QUESTION_CHARS = 1_000
#: 一个工作流保留的历史版本条数：版本化是为了「回看与复用」，不是变更审计台账。
MAX_VERSIONS = 20
#: 单次运行最多执行多少步（含重试与回环）：条件分支允许回边，没有这个上限，
#: 一份「A→B→A」的定义会把运行账本写满并把唤醒无限投下去。
MAX_STEPS_EXECUTED = 200
MAX_RUNS = 2_000
MAX_ATTEMPTS = 5
#: 步 id 上限：与服务端 `MAX_STEP_ID` 同值（`group_crosswire` 钉）。步 id 会进运行视图的键，
#: 长度得两边一致，否则云端认得的 id 桌面截过之后指向另一一步。
MAX_STEP_ID = 64

WORKFLOW_KINDS = ("trigger", "action", "condition", "approval")
#: 动作步只允许这几种落地方式，且**每一种都是往既有 substrate 里投一条**：
#: 新增一种动作要先证明它不需要第二套保序，否则就该走上面那两个。
ACTION_KINDS = ("team_dispatch", "ticket_wake", "notify_host")
ON_ERROR = ("stop", "retry", "continue")
CONDITION_OPS = (
    "equals", "notEquals", "contains", "nonEmpty", "empty",
    "greaterThan", "lessThan", "isTrue", "isFalse",
)

#: 运行状态。前三个是「还活着」，后三个是终态。
#: `blocked` 专指「这套装配没有可执行这一步的东西」——它不是失败（失败是执行了且做坏了），
#: 把它并进 failed 会让界面把一个没接线的安装说成「工作流出错了」。
RUN_STATES = (
    "running", "awaiting_step", "awaiting_approval",
    "blocked", "failed", "succeeded", "cancelled",
)
ACTIVE_RUN_STATES = ("running", "awaiting_step", "awaiting_approval")
STEP_STATES = (
    "pending", "running", "waiting", "succeeded", "failed",
    "allowed", "denied", "skipped", "blocked",
)
APPROVAL_DECISIONS = ("allow", "deny")
#: 卡点结论的**来源**，界面必须显示它：把「档位关着自动放行」画成「人已批准」
#: 是这一层最坏的一种谎。
APPROVAL_SOURCES = ("policy-off", "human", "gate-unavailable")

_FORMAT_VERSION = 1


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(text: Any, chars: int = 16) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:chars]


def _s(value: Any) -> str:
    return str(value or "").strip()


def _clip(text: Any, limit: int) -> str:
    body = str(text or "")
    return body if len(body) <= limit else body[:limit] + "…（已截断）"


def _check_len(value: str, limit: int, who: str, errors: list[str]) -> None:
    """名字 / 标题 / 问题原文这一类**标识性**字段：超长就报错，不做「截了但说明一下」。

    为什么和 `_clip` 分开：正文截了带「已截断」标记，读的人知道自己看到的是节选；
    名字截了看不出来——两本前 60 字相同的定义在界面上就成了同一条，而「复用」点的正是名字。
    卡点的问题原文更要紧：把人要签字的那句话截断，等于让审批人签一份他没读完的东西。
    上限本身与服务端同源（`group_crosswire` 钉住），否则同一本定义云端存得下、桌面跑不动。"""
    if len(value) > limit:
        errors.append(
            f"{who}有 {len(value)} 字、超过上限 {limit}：请自己改短，这一层不代你删字——"
            "名字被截会让两本定义在界面上撞成一条，问题被截会让人在没读完的话上签字"
        )


def _slug(value: Any, fallback: str) -> str:
    """步骤 id：只留字母数字与 `-_`，其余换成 `-`。

    步 id 会参与运行视图的键与 `source: "step:x.output"` 的解析，留空格或 `.` 进去
    就会出现「两个不同的步算出同一个键」。"""
    raw = _s(value) or fallback
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw)
    return out[:MAX_STEP_ID].strip("-") or fallback


# ── 存储：与 team_runtime 同一个基类（不第三条实现原子写）──────────


class WorkflowStore(tm.DurableStore):
    """工作流定义（含版本历史）。"""

    FILENAME = WORKFLOW_REGISTRY_FILENAME

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {"version": STORE_VERSION, "workflows": {}}

    def label(self) -> str:
        return "工作流定义档"

    def all_workflows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(w) for w in self.data.get("workflows", {}).values()]

    def get(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self.data.get("workflows", {}).get(_s(workflow_id))
            return dict(found) if isinstance(found, dict) else None

    def put(self, workflow: dict[str, Any]) -> dict[str, Any]:
        """整体替换当前版本，并把**被替换掉的那一版**压进历史。

        历史压的是旧副本而不是新副本：`versions` 的语义是「回看」，
        把刚存的这份也塞进去会让「当前版本」出现两次。"""
        wid = _s(workflow.get("id"))
        with self._lock:
            table = self.data.setdefault("workflows", {})
            if wid not in table and len(table) >= MAX_WORKFLOWS:
                return {"ok": False, "reason": (
                    f"工作流定义已有 {len(table)} 份、到上限 {MAX_WORKFLOWS}：这条**没有落盘**。"
                    "先删掉不再复用的定义，不要靠挤掉旧的——被挤掉的那份可能正有运行挂在上面"
                )}
            previous = table.get(wid)
            if isinstance(previous, dict):
                history = list(previous.get("versions") or [])
                history.append({k: v for k, v in previous.items() if k != "versions"})
                workflow = {**workflow, "versions": history[-MAX_VERSIONS:]}
            table[wid] = workflow
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                if isinstance(previous, dict):
                    table[wid] = previous
                else:
                    table.pop(wid, None)
                return {"ok": False, "reason": f"工作流定义落盘失败：{exc}"}
        return {"ok": True, "workflow": dict(workflow)}

    def remove(self, workflow_id: str) -> dict[str, Any]:
        wid = _s(workflow_id)
        with self._lock:
            table = self.data.setdefault("workflows", {})
            gone = table.pop(wid, None)
            if gone is None:
                return {"ok": False, "reason": f"查不到工作流「{wid}」"}
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                table[wid] = gone
                return {"ok": False, "reason": f"删除没落盘（{exc}）：定义仍在盘上，下次加载会回来"}
        return {"ok": True, "removed": wid}


class RunStore(tm.DurableStore):
    """每次运行的视图（D-10 验收项「查看每次运行」）。"""

    FILENAME = WORKFLOW_RUNS_FILENAME

    @classmethod
    def empty(cls) -> dict[str, Any]:
        return {"version": STORE_VERSION, "runs": {}, "order": []}

    def label(self) -> str:
        return "工作流运行档"

    def upsert(self, run: dict[str, Any]) -> dict[str, Any]:
        rid = _s(run.get("runId"))
        with self._lock:
            table = self.data.setdefault("runs", {})
            order = self.data.setdefault("order", [])
            is_new = rid not in table
            if is_new and len(table) >= MAX_RUNS:
                # 不静默挤掉最早的：那批运行可能正挂在人工卡点上等人点。
                return {"ok": False, "reason": (
                    f"运行记录已有 {len(table)} 条、到上限 {MAX_RUNS}：这条**没有落盘**。"
                    "先清理已结束的运行；「查看每次运行」不能靠丢掉旧的那几次来腾地方"
                )}
            table[rid] = json.loads(json.dumps(run, ensure_ascii=False, default=str))
            if is_new:
                order.append(rid)
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001  落盘失败必须让调用方知道这条运行没进账
                if is_new:
                    table.pop(rid, None)
                    if order and order[-1] == rid:
                        order.pop()
                return {"ok": False, "reason": f"运行记录落盘失败：{exc}"}
        return {"ok": True, "run": dict(run)}

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self.data.get("runs", {}).get(_s(run_id))
            return json.loads(json.dumps(found, ensure_ascii=False, default=str)) if found else None

    def all_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            order = list(self.data.get("order") or [])
            table = self.data.get("runs", {})
            out = [table[r] for r in order if r in table]
            # 掉出 order 的那几条（手工改档、半截写）也要能看见，不然「查不到」是假的
            out += [v for k, v in table.items() if k not in order]
            return json.loads(json.dumps(out, ensure_ascii=False, default=str))


# ── 定义：校验与归一 ──────────────────────────────────────────


def _normalize_step(raw: Any, index: int, errors: list[str]) -> dict[str, Any]:
    step = raw if isinstance(raw, dict) else {}
    kind = _s(step.get("kind")).lower()
    if kind not in WORKFLOW_KINDS:
        errors.append(
            f"第 {index + 1} 步的 kind「{kind or '（空）'}」不在四种节点里（触发 / 动作 / 条件 / 人工卡点）"
        )
    sid = _slug(step.get("id"), f"step-{index + 1}")
    step_name = _s(step.get("name"))
    _check_len(step_name, MAX_NAME_CHARS, f"第 {index + 1} 步（{sid}）的名字", errors)
    spec = step.get("spec") if isinstance(step.get("spec"), dict) else {}
    out: dict[str, Any] = {
        "id": sid,
        "kind": kind,
        "name": step_name or sid,
        "spec": spec,
        "next": _slug(step.get("next"), "") if _s(step.get("next")) else "",
        "onError": _s(step.get("onError")).lower() or "stop",
        "attempts": 1,
        # 条件步引用的那一步 id（跨步引用），由 normalize_workflow 统一判存在性。
        "sourceStepId": "",
    }
    if out["onError"] not in ON_ERROR:
        errors.append(f"步骤「{sid}」的 onError「{out['onError']}」只能是 stop / retry / continue")
    if out["onError"] == "retry":
        try:
            want = int(step.get("attempts") or 1)
        except (TypeError, ValueError):
            errors.append(f"步骤「{sid}」的 attempts 不是整数：按 1 次处理意味着重试根本没开")
            want = 1
        if want > MAX_ATTEMPTS:
            errors.append(
                f"步骤「{sid}」要求重试 {want} 次、超过上限 {MAX_ATTEMPTS}：要么改小，"
                "要么这一本就不该自动重试（多半是它每次都会投一条真唤醒出去）"
            )
            want = min(want, MAX_ATTEMPTS)
        out["attempts"] = max(1, want)
    else:
        # `attempts` 归一后每步都带着（缺省就是 1），所以「导出的定义再导回来」不能因为
        # 看见这个字段就报错——那会把版本化与导入导出两条能力互相打死。只有真写了大于 1
        # 的数字、却没把 onError 开成 retry，才是「这个数字不会生效」那种配错。
        given = 1
        if step.get("attempts") is not None:
            try:
                given = int(step.get("attempts"))
            except (TypeError, ValueError):
                errors.append(
                    f"步骤「{sid}」的 attempts「{_clip(_s(step.get('attempts')), 20)}」不是整数："
                    "重试次数只能写数字"
                )
                given = 1
        if given > 1:
            errors.append(
                f"步骤「{sid}」给了 attempts={given} 却没把 onError 设成 retry：那个数字不会生效"
            )

    if kind == "action":
        action = _s(spec.get("action")).lower()
        if action not in ACTION_KINDS:
            errors.append(
                f"步骤「{sid}」的动作「{action or '（空）'}」不在表里：动作只能是 "
                f"{' / '.join(ACTION_KINDS)}（每一个都投进既有工单，本层不新造执行体）"
            )
        elif action == "team_dispatch":
            if not isinstance(spec.get("memberIds"), list) or not spec.get("memberIds"):
                errors.append(f"步骤「{sid}」是团队派工却没有 memberIds：派给谁都不知道，不能算合法定义")
            if not _s(spec.get("brief")):
                errors.append(f"步骤「{sid}」是团队派工却没有 brief：成员看不到上游对话，这句必须自己说清要做什么")
        elif action == "ticket_wake" and not _s(spec.get("text")):
            errors.append(f"步骤「{sid}」是工单唤醒却没有正文：空消息连收件箱都进不去")
        elif action == "notify_host" and not _s(spec.get("text")):
            errors.append(f"步骤「{sid}」要给主持人回话却没有正文")
    elif kind == "condition":
        # 条件节点的走向只有 nextTrue / nextFalse 两条（见 advance 里的分支）：主 `next`
        # 对它没有任何意义。留着它不填才是常态，硬要填反而让人以为还有第三条路。
        if out["next"]:
            errors.append(
                f"条件步骤「{sid}」同时给了 next 和两个分支：条件只按 nextTrue / nextFalse 走，"
                "next 永远不会被用到——去掉它，免得看着像有第三条路"
            )
        for key in ("nextTrue", "nextFalse"):
            target = _slug(step.get(key), "") if _s(step.get(key)) else ""
            if not target:
                errors.append(f"条件步骤「{sid}」缺 {key}：两个分支必须各有去向（要收工就指到一个终步并在说明里写清）")
            out[key] = target
        op = _canonical_op(spec.get("op"))
        if not op:
            errors.append(
                f"条件步骤「{sid}」的算子「{_clip(_s(spec.get('op')), 30) or '（空）'}」不在九个里"
                f"（{' / '.join(CONDITION_OPS)}）：这里只做数据比对，不接受表达式"
            )
        else:
            # 落盘存表里那个写法（camelCase）：`greaterThan` 被小写成 `greatthan` 之后
            # 既对不上校验表，运行时也认不出来——九个算子会悄悄只剩四个能用。
            spec = {**spec, "op": op}
            out["spec"] = spec
        source = _s(spec.get("source"))
        if not source:
            errors.append(
                f"条件步骤「{sid}」没写 source：不指明比的是触发载荷（payload）还是某一步的产出"
                "（step:<步 id>.output），判断结果没有依据"
            )
        elif source != "payload" and not source.startswith("step:"):
            errors.append(
                f"条件步骤「{sid}」的 source「{_clip(source, 40)}」看不懂：只认 payload 或 step:<步 id>.output"
            )
        elif source.startswith("step:"):
            if not _source_step_id(source):
                errors.append(f"条件步骤「{sid}」的 source「{_clip(source, 40)}」里没有可用的步 id")
            # 指到定义里没有的步上，运行时只会得到「那一步还没有产出」，于是条件永远走
            # 不通过分支——那是配置错误，不是业务判断。跨步引用要等所有步都归一完才判得了，
            # 所以这里只把要引用的步 id 挑出来，交给 normalize_workflow 收尾。
            out["sourceStepId"] = _source_step_id(source)
    elif kind == "approval":
        out["onDeny"] = _slug(step.get("onDeny"), "") if _s(step.get("onDeny")) else ""
        question = _s(step.get("question"))
        _check_len(question, MAX_QUESTION_CHARS, f"卡点步骤「{sid}」的问题原文", errors)
        out["question"] = question or "这一步需要有人确认后再继续"
        tool = _s(spec.get("tool"))
        if tool and not tap.needs_approval(tool, "all"):
            # `all` 档都不问人的工具只有「已有逐操作确认」那一族（浏览器 / 电脑操控）：
            # 卡点绑在它上面，运行视图会永远停在「等人」——那一步根本没被问过。
            errors.append(
                f"人工卡点「{sid}」绑的是既有豁免工具「{tool}」（浏览器/电脑操控已有逐操作确认）："
                "审批门不会为它问第二次，这一步永远不会等人"
            )
    return out


def normalize_workflow(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """校验一份定义，返回（归一后的定义, 错误清单）。错误清单非空就不许落盘。"""
    data = raw if isinstance(raw, dict) else {}
    errors: list[str] = []
    name = _s(data.get("name"))
    _check_len(name, MAX_NAME_CHARS, "工作流的名字", errors)
    if not name:
        errors.append("工作流要有名字：界面上「复用」靠的是认得出这是哪一本")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, errors + ["steps 要是非空数组：没有节点的工作流保存下来只会是一条空运行"]
    if len(raw_steps) > MAX_STEPS:
        return None, errors + [f"steps 有 {len(raw_steps)} 个、超过上限 {MAX_STEPS}：先拆成几本"]

    steps = [_normalize_step(s, i, errors) for i, s in enumerate(raw_steps)]
    ids = [s["id"] for s in steps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errors.append(f"步骤 id 重复：{'、'.join(dupes)}（重复的 id 会让运行视图里两条记录看不出是哪一步）")
    triggers = [s for s in steps if s["kind"] == "trigger"]
    if len(triggers) != 1:
        errors.append(
            f"触发节点必须恰好一个，这里有 {len(triggers)} 个："
            "运行要从哪儿起得只有一个答案，多于一个就不是「不知道从哪起」而是「两个入口互相不知道对方」"
        )
    known = set(ids)
    for s in steps:
        for key in ("next", "nextTrue", "nextFalse", "onDeny"):
            target = s.get(key) or ""
            if target and target not in known:
                errors.append(f"步骤「{s['id']}」的 {key} 指向「{target}」，定义里没有这一步：走到那儿就断了")
        ref = s.get("sourceStepId") or ""
        if ref and ref not in known:
            errors.append(
                f"条件步骤「{s['id']}」要比「{ref}」的产出，定义里没有这一步："
                "这个条件永远走不通过分支，那是配置错误而不是业务判断"
            )

    entry = _slug(data.get("entryStepId"), "") or (triggers[0]["id"] if len(triggers) == 1 else "")
    if not entry:
        errors.append("没有可用的入口步（触发节点不唯一或缺失），这份定义跑不起来")
    if errors:
        return None, errors

    wf_id = _slug(data.get("id"), f"wf-{_digest(f'{name}|{_now_ms()}|{os.getpid()}', 12)}")
    now = _now_ms()
    return {
        "id": wf_id,
        "name": name,
        "description": _clip(_s(data.get("description")), MAX_TEXT_CHARS),
        "version": max(1, int(data.get("version") or 1)),
        "entryStepId": entry,
        "steps": steps,
        "stepIds": ids,
        "createdAtMs": int(data.get("createdAtMs") or now),
        "updatedAtMs": now,
        "versions": list(data.get("versions") or [])[-MAX_VERSIONS:],
    }, []


# ── 条件求值：九个算子，只比数据 ──────────────────────────────

#: 「这句话算不算真」与「这个值算不算有内容」是两件事，分两个集合：
#: 混成一个会让 `empty` 把 "0"、"false" 当非空，而 `isFalse` 又把同一个值判成真。
_TRUTHY = ("true", "1", "yes", "y", "ok", "是", "对", "通过")
_EMPTYISH = ("null", "[]", "{}", '""', "none", "undefined")


def _looks_nonempty(text: Any) -> bool:
    """`empty` 与 `nonEmpty` 必须严格互反，所以两边共用同一个「非空」判据。"""
    stripped = str(text or "").strip()
    return bool(stripped) and stripped.lower() not in _EMPTYISH


def _source_step_id(source: str) -> str:
    """从 `step:<id>` / `step:<id>.output` 里取出步 id。

    `.output` 这个尾巴是定义里写给界面看的（「比的是那一步的产出」），但产出表是按
    **裸步 id** 存的——不剥掉它，`_slug` 会把 `.` 变成 `-`，条件就永远比到一个空值。"""
    body = str(source or "")[5:]
    if body.endswith(".output"):
        body = body[: -len(".output")]
    return _slug(body, "")


def _canonical_op(raw: Any) -> str:
    """算子按大小写不敏感收下，但只认表里那九个写法。

    表里是 camelCase（`notEquals` / `greaterThan` / `isTrue`），所以任何「先 lower 再比表」
    的写法都会让这九个里的一半永远对不上——校验拒收合法定义，运行时又会落到「算子不在表里」。"""
    value = _s(raw)
    for op in CONDITION_OPS:
        if value.lower() == op.lower():
            return op
    return ""


def _lookup(source: Any, dotted: str) -> Any:
    """按点号路径从字典里取值；取不到就返回空串（不抛，让算子自己判 empty）。"""
    cur = source
    for part in str(dotted or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return cur


def _coerce_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_condition(spec: dict, context: dict) -> tuple[bool, str]:
    """返回（是否通过, 中文说明）。说明会进运行视图——界面要答得出「为什么走了这条」。"""
    op = _canonical_op(spec.get("op"))
    source = _s(spec.get("source"))
    field = _s(spec.get("field"))
    expected = spec.get("value")
    if source == "payload":
        base: Any = context.get("payload", {})
    elif source.startswith("step:"):
        step_id = _source_step_id(source)
        if step_id not in context.get("outputs", {}):
            return False, f"条件要比「{step_id}」的产出，但那一步还没有产出（没跑过或被拒）：按不通过走"
        base = context.get("outputs", {}).get(step_id, {})
    else:
        return False, f"条件的 source「{source or '（空）'}」看不懂：只认 payload 或 step:<步 id>.output 这一类"
    value = _lookup(base, field) if field else base
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    want = "" if expected is None else (expected if isinstance(expected, str) else str(expected))

    if op == "equals":
        ok = text.strip() == want.strip()
    elif op == "notEquals":
        ok = text.strip() != want.strip()
    elif op == "contains":
        ok = bool(want) and want in text
    elif op == "nonEmpty":
        ok = _looks_nonempty(text)
    elif op == "empty":
        ok = not _looks_nonempty(text)
    elif op == "isTrue":
        ok = text.strip().lower() in _TRUTHY
    elif op == "isFalse":
        ok = text.strip().lower() not in _TRUTHY
    elif op in ("greaterThan", "lessThan"):
        left, right = _coerce_number(value), _coerce_number(expected)
        if left is None or right is None:
            return False, f"条件要比大小，但「{field or source}」读出来是「{_clip(text, 40)}」不是数字：按不通过走"
        ok = left > right if op == "greaterThan" else left < right
    else:
        return False, f"算子「{op or '（空）'}」不在表里：按不通过走"
    return ok, f"{_clip(text, 60)} {op} {want} ⇒ {'通过' if ok else '不通过'}"


# ── 运行时 ──────────────────────────────────────────────────


def run_ticket_id(room: Any, run_id: Any) -> str:
    """一次运行在执行体表里的键：`房间~wf-<运行摘要>`。

    为什么**不**直接用房间（= 主持人那张单）：#148 已经论证过「替主持人领一份领取」的
    下场——三个槽位被三份没人执行的领取占死，这个房间再也派不动活。工作流动作若默认
    投到主持人那张单上，就是在重演同一个错。
    给每次运行一张自己的单，「同工单保序」正好变成「同一次运行的几步串行」——
    这正是线性工作流要的顺序，也不需要新写一套保序（约束 A）。"""
    return tm.member_ticket_id(room, f"wf-{_digest(run_id, 10)}")


class WorkflowRuntime:
    """编排层：把定义跑成一次运行，动作步投进既有工单，卡点交给既有审批门。

    注入的三个句柄都**可空**，空的时候这一步如实 blocked 而不是「当它成功了」：

    * `team_provider()` → `TeamRuntime`（`team_dispatch` / `notify_host` 要走它）；
    * `workers_provider()` → `TicketWorkerRegistry`（`ticket_wake` 要走它）；
    * `approval_provider(room)` → `tool_approval.ToolApproval`（人工卡点读它的档位与网关；
      档位随会话下发，所以这个 provider 收的是运行绑定的房间 id）。

    之所以收 callable 而不是对象：引擎那侧的团队运行时与注册表是**进程内单例**，
    晚一步才装配好（D-48 的装配形状）；传死对象就逼本层在建的时候再起一份注册表，
    那是第二个槽位池。
    """

    def __init__(
        self,
        *,
        definitions: WorkflowStore | None = None,
        runs: RunStore | None = None,
        team_provider: Callable[[], Any] | None = None,
        workers_provider: Callable[[], Any] | None = None,
        approval_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.definitions = definitions if definitions is not None else WorkflowStore()
        self.runs = runs if runs is not None else RunStore()
        self._team_provider = team_provider
        self._workers_provider = workers_provider
        self._approval_provider = approval_provider
        self._clock = clock
        self._lock = threading.Lock()

    # ── 注入面 ──────────────────────────────────────────────

    def _team(self) -> Any:
        return self._team_provider() if self._team_provider else None

    def _workers(self) -> Any:
        return self._workers_provider() if self._workers_provider else None

    def _approval(self, room: str = "") -> Any:
        """审批门按**房间**取：档位是 Rust 随 `/api/chat` 下发在会话 runtime 里的
        （`toolApproval`），没有「全进程一个档位」这种东西。

        取不到会话时 provider 自己给一份缺省 off 的 `ToolApproval`（off 的语义是「不问人」，
        运行视图会如实写 `decidedBy: "policy-off"`）；但 provider **报错**是另一回事：那是
        「读不出档位」，不是「档位是 off」，这里不吞异常——吞掉就等于静默绕过审批门（约束 C）。
        """
        if self._approval_provider:
            return self._approval_provider(_s(room))
        return tap.ToolApproval()

    # ── 定义 CRUD / 版本 / 导入导出 ─────────────────────────

    def save(self, raw: Any) -> dict[str, Any]:
        wf, errors = normalize_workflow(raw)
        if errors:
            return {"ok": False, "code": "WORKFLOW_INVALID", "errors": errors,
                    "reason": "定义没通过校验，一个字节都没落盘：" + errors[0]}
        existing = self.definitions.get(wf["id"])
        if isinstance(existing, dict):
            wf["version"] = int(existing.get("version") or 1) + 1
            wf["createdAtMs"] = int(existing.get("createdAtMs") or wf["createdAtMs"])
        res = self.definitions.put(wf)
        if not res.get("ok"):
            return {"ok": False, "code": "WORKFLOW_STORE_FULL", "reason": res.get("reason")}
        return {"ok": True, "workflow": res["workflow"],
                "note": (f"已存为第 {wf['version']} 版（上一版进了 versions，"
                         f"最多回看 {MAX_VERSIONS} 版）") if wf["version"] > 1 else "已存为第 1 版"}

    def list_definitions(self) -> list[dict[str, Any]]:
        out = []
        for wf in self.definitions.all_workflows():
            runs = [r for r in self.runs.all_runs() if r.get("workflowId") == wf.get("id")]
            out.append({
                "id": wf.get("id"), "name": wf.get("name"), "version": wf.get("version"),
                "description": wf.get("description") or "",
                "stepCount": len(wf.get("steps") or []),
                "kinds": [s.get("kind") for s in wf.get("steps") or []],
                "hasApproval": any(s.get("kind") == "approval" for s in wf.get("steps") or []),
                "updatedAtMs": wf.get("updatedAtMs"),
                "versionCount": 1 + len(wf.get("versions") or []),
                "runCount": len(runs),
                "activeRuns": sum(1 for r in runs if r.get("state") in ACTIVE_RUN_STATES),
            })
        out.sort(key=lambda w: -int(w.get("updatedAtMs") or 0))
        return out

    def get_definition(self, workflow_id: str, *, version: int = 0) -> dict[str, Any]:
        """取当前版或某一历史版（`version` 填 0 就是当前版）。"""
        wf = self.definitions.get(workflow_id)
        if not isinstance(wf, dict):
            return {"ok": False, "code": "WORKFLOW_NOT_FOUND",
                    "reason": f"查不到工作流「{_s(workflow_id)}」：定义只有盘上这一份"}
        want = int(version or 0)
        if want and want != int(wf.get("version") or 1):
            for old in wf.get("versions") or []:
                if int(old.get("version") or 0) == want:
                    return {"ok": True, "workflow": old, "historic": True,
                            "note": f"这是第 {want} 版（当前是第 {wf.get('version')} 版）：只读，不能按历史版起运行"}
            return {"ok": False, "code": "VERSION_UNAVAILABLE", "reason": (
                f"第 {want} 版已经不在保留窗口里（只留最近 {MAX_VERSIONS} 版）："
                "这里如实说取不到，不按当前版糊过去——用户要看的正是「当时那一版长什么样」")}
        return {"ok": True, "workflow": wf, "historic": False}

    def remove(self, workflow_id: str, *, force: bool = False) -> dict[str, Any]:
        live = [r for r in self.runs.all_runs()
                if r.get("workflowId") == _s(workflow_id) and r.get("state") in ACTIVE_RUN_STATES]
        if live and not force:
            return {"ok": False, "code": "RUNS_INFLIGHT", "runIds": [r.get("runId") for r in live],
                    "reason": (f"这份定义还挂着 {len(live)} 条在跑或等人的运行：删掉定义之后那些卡点"
                               "就没人认得了。要么先取消运行，要么带 force 明确承认它们在盘上留证")}
        res = self.definitions.remove(workflow_id)
        if not res.get("ok"):
            return {"ok": False, "code": "WORKFLOW_NOT_FOUND", "reason": res.get("reason")}
        return {"ok": True, "removed": res["removed"],
                "inflightLeft": len(live) if force else 0,
                "note": "定义删了，运行记录不跟着删——「查看每次运行」的历史不能因为一本定义没了就蒸发"
                        if live else "定义已删除；运行记录留在盘上"}

    def export(self, workflow_id: str, *, version: int = 0) -> dict[str, Any]:
        got = self.get_definition(workflow_id, version=int(version or 0))
        if not got.get("ok"):
            return got
        wf = got["workflow"]
        return {
            "ok": True,
            "formatVersion": _FORMAT_VERSION,
            "exportedAtMs": int(self._clock() * 1000),
            "exportedVersion": int(wf.get("version") or 0),
            "workflow": {k: v for k, v in wf.items() if k != "versions"},
            "versions": [] if got.get("historic") else (wf.get("versions") or []),
            "note": (
                "导出的是一份纯定义（不含任何运行记录与正文）：导入方对着同一套校验器过一遍"
                if not got.get("historic") else
                f"这是第 {wf.get('version')} 版历史定义，只导出这一版本身（不含别的版本）"
            ),
        }

    def import_definition(self, payload: Any, *, keep_id: bool = True) -> dict[str, Any]:
        raw = payload if isinstance(payload, dict) else {}
        body = raw.get("workflow") if isinstance(raw.get("workflow"), dict) else raw
        fmt = raw.get("formatVersion")
        if fmt not in (None, _FORMAT_VERSION):
            return {"ok": False, "code": "FORMAT_UNSUPPORTED", "reason": (
                f"导入文件的 formatVersion 是 {fmt}，本引擎只认 {_FORMAT_VERSION}："
                "不接受「先收下再说」，收下就跑不通的定义会把人引到一次半途卡住的运行上")}
        existing = self.definitions.get(_s(body.get("id"))) if keep_id else None
        prepared = dict(body)
        if not keep_id or isinstance(existing, dict):
            # 撞 id 不静默覆盖：换一个新 id 导入，让「同名两份」在盘上分开存在，
            # 覆盖掉的那一份可能正有运行挂在它的某一步上。
            prepared.pop("id", None)
            prepared.pop("versions", None)
            prepared["name"] = _s(body.get("name")) or "导入的工作流"
        res = self.save(prepared)
        if res.get("ok") and keep_id and isinstance(existing, dict):
            res["note"] = f"已导入为「{res['workflow']['name']}」的新版本（同 id 覆盖 = 升版）"
        elif res.get("ok"):
            res["note"] = "已按新 id 导入（原 id 已存在或要求重新分配）"
        return res

    # ── 运行 ────────────────────────────────────────────────

    def _orphaned(self, run: dict[str, Any]) -> dict[str, Any]:
        """定义查不到（被删或版本滚出保留窗口）时的统一收法：如实 blocked 落盘。

        让运行继续挂在 `awaiting_approval` 上说谎——那个卡点已经没有人答得了，界面会一直
        催人去点一个永远推不动的按钮。也不能退回当前版：一次升版就把在跑的运行改道到新的
        步骤形状上，步 id 对得上的那几步会按新参数执行，两边都不是用户点下去时的那本。"""
        run["state"] = "blocked"
        run["note"] = ("定义查不到（被删或版本滚出保留窗口）：这条运行停在这儿，不猜一份定义把它推完")
        run["updatedAtMs"] = int(self._clock() * 1000)
        self.runs.upsert(run)
        return run

    def _definition_for_run(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """这条运行当初是哪一版定义。

        版本滚出保留窗口时返回 `None`（→ advance 把运行标 blocked）：**不能退回当前版**，
        那等于一次升版把在跑的运行悄悄改道到新的步骤形状上——步 id 对不上时它会走到
        「当前步在定义里找不到」，而对得上的那几步会按新参数执行，两边都不是用户点下去时的那本。
        """
        wf = self.definitions.get(run.get("workflowId", ""))
        if not isinstance(wf, dict):
            return None
        want = int(run.get("workflowVersion") or 0)
        if want and int(wf.get("version") or 1) != want:
            for old in wf.get("versions") or []:
                if int(old.get("version") or 0) == want:
                    return old
            return None
        return wf

    def start(self, *, workflow_id: str, room: Any = "", payload: Any = None,
              title: Any = "") -> dict[str, Any]:
        got = self.get_definition(workflow_id)
        if not got.get("ok"):
            return got
        wf = got["workflow"]
        if got.get("historic"):
            return {"ok": False, "code": "HISTORIC_VERSION",
                    "reason": "不能按历史版起运行：运行视图要对得上「当前定义」，"
                              "要跑旧形状请先把它另存为新定义"}
        run_title = _s(title) or _s(wf.get("name")) or "一次运行"
        if len(run_title) > MAX_NAME_CHARS:
            # 标题是运行清单上唯一认得出「这是哪一次」的一行，截了就撞车；拒得早比让台账
            # 里留下两条同名运行好。上限与服务端 `MAX_TITLE` 同源。
            return {"ok": False, "code": "WORKFLOW_INVALID",
                    "reason": f"运行标题有 {len(run_title)} 字、超过上限 {MAX_NAME_CHARS}："
                              "起运行前先改短，这一层不代你截断"}
        rid = f"wr-{_digest(f'{wf['id']}|{room}|{_now_ms()}|{os.getpid()}', 12)}"
        body = payload if isinstance(payload, dict) else {"text": _s(payload)}
        now = int(self._clock() * 1000)
        run = {
            "runId": rid,
            "workflowId": wf["id"],
            "workflowVersion": int(wf.get("version") or 1),
            "workflowName": wf.get("name"),
            "title": run_title,
            "room": _s(room),
            "state": "running",
            "currentStepId": wf.get("entryStepId") or "",
            "payload": _clip(json.dumps(body, ensure_ascii=False, default=str), MAX_TEXT_CHARS),
            "payloadDigest": _digest(json.dumps(body, ensure_ascii=False, default=str)),
            "context": {"payload": body, "outputs": {}},
            "steps": [self._fresh_step(s) for s in wf.get("steps") or []],
            "executed": 0,
            "startedAtMs": now,
            "updatedAtMs": now,
            "endedAtMs": 0,
            "note": "",
        }
        saved = self.runs.upsert(run)
        if not saved.get("ok"):
            return {"ok": False, "code": "RUN_STORE_FAILED", "reason": saved.get("reason")}
        advanced = self.advance(rid)
        return {"ok": True, "runId": rid, "run": advanced.get("run", run),
                "state": advanced.get("state", run["state"]),
                "note": "本层只负责投唤醒与记账：回合起不起由 worker 调度器决定"}

    @staticmethod
    def _fresh_step(step: dict[str, Any]) -> dict[str, Any]:
        return {
            "stepId": step.get("id"), "kind": step.get("kind"), "name": step.get("name"),
            "state": "pending", "attempt": 0, "startedAtMs": 0, "endedAtMs": 0,
            "output": "", "outputDigest": "", "error": "",
            #: 每一次失败各追加一条，永不覆盖：重试成功的步也要答得出前几次为什么慢。
            "history": [],
            "approval": ({"question": step.get("question"), "state": "pending",
                           "decidedBy": "", "note": "", "askedAtMs": 0, "decidedAtMs": 0}
                          if step.get("kind") == "approval" else None),
        }

    def _step_map(self, wf: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(s.get("id")): s for s in wf.get("steps") or []}

    def advance(self, run_id: str) -> dict[str, Any]:
        """从当前步往下推，直到：等人工 / 等异步回执 / 终态 / 装配缺件 / 步数预算用尽。"""
        run = self.runs.get(run_id)
        if not isinstance(run, dict):
            return {"ok": False, "code": "RUN_NOT_FOUND", "reason": f"查不到运行「{_s(run_id)}」"}
        if run.get("state") not in ACTIVE_RUN_STATES:
            return {"ok": True, "run": run, "state": run.get("state"),
                    "note": "这条运行已经收口，推不动也不需要推"}
        wf = self._definition_for_run(run)
        if not isinstance(wf, dict):
            return {"ok": True, "run": self._orphaned(run), "state": "blocked",
                    "reason": run["note"]}
        steps = self._step_map(wf)
        views = {str(s.get("stepId")): s for s in run.get("steps") or []}

        with self._lock:
            while True:
                sid = _s(run.get("currentStepId"))
                if not sid:
                    return self._finish(run, "succeeded", "走完最后一步：正常收口")
                step = steps.get(sid)
                view = views.get(sid)
                if step is None or view is None:
                    run["state"] = "blocked"
                    run["note"] = f"当前步「{sid}」在这份定义里找不到：停在这儿，不跳到下一 step 蒙着跑"
                    self.runs.upsert(run)
                    return {"ok": True, "run": run, "state": "blocked", "reason": run["note"]}
                if int(run.get("executed") or 0) >= MAX_STEPS_EXECUTED:
                    run["state"] = "failed"
                    run["note"] = (f"一次运行执行了 {MAX_STEPS_EXECUTED} 步还在走：判定为环，"
                                   "已停。每条唤醒都真投进过工单，先查是不是有回边没有出口")
                    self.runs.upsert(run)
                    return {"ok": True, "run": run, "state": "failed", "reason": run["note"]}
                run["executed"] = int(run.get("executed") or 0) + 1
                outcome = self._execute(run, step, view)
                run = outcome["run"]
                if outcome.get("stop"):
                    self.runs.upsert(run)
                    return {"ok": True, "run": run, "state": run.get("state"),
                            "note": outcome.get("note") or run.get("note") or ""}
                run["currentStepId"] = outcome.get("next", "")
                self.runs.upsert(run)

    def _execute(self, run: dict[str, Any], step: dict[str, Any],
                 view: dict[str, Any]) -> dict[str, Any]:
        now = int(self._clock() * 1000)
        kind = step.get("kind")
        view["attempt"] = int(view.get("attempt") or 0) + 1
        view["startedAtMs"] = view.get("startedAtMs") or now
        view["state"] = "running"

        if kind == "trigger":
            view["state"] = "succeeded"
            view["output"] = _clip(str(run.get("payload") or ""), MAX_TEXT_CHARS)
            view["outputDigest"] = _s(run.get("payloadDigest"))
            view["endedAtMs"] = int(self._clock() * 1000)
            return {"run": run, "next": step.get("next") or ""}

        if kind == "condition":
            ok, why = evaluate_condition(step.get("spec") or {}, run.get("context") or {})
            view["state"] = "succeeded"
            view["output"] = _clip(why, MAX_TEXT_CHARS)
            view["endedAtMs"] = int(self._clock() * 1000)
            nxt = step.get("nextTrue") if ok else step.get("nextFalse")
            return {"run": run, "next": _s(nxt), "note": f"条件判定：{why}"}

        if kind == "approval":
            return self._approval_gate(run, step, view)

        return self._run_action(run, step, view)

    def _approval_gate(self, run: dict[str, Any], step: dict[str, Any],
                       view: dict[str, Any]) -> dict[str, Any]:
        try:
            gate = self._approval(_s(run.get("room")))
        except Exception as exc:  # noqa: BLE001
            # provider 报错 = 「读不出这个会话的档位」，不是「档位是 off」。
            # 退回缺省就是把审批门静默绕过（约束 C 明确不许）：这里如实 blocked，等人补。
            return self._blocked(run, step, view,
                                 f"读不出本会话的审批档位（{type(exc).__name__}: {_clip(str(exc), 120)}）："
                                 "这一步既不按 off 放行，也不假装问过人")
        mode = tap.normalize_mode(getattr(gate, "mode", "off"))
        tool = _s((step.get("spec") or {}).get("tool")) or "workflow_approval"
        question = _s(step.get("question"))
        info = view.setdefault("approval", {"question": question, "state": "pending",
                                            "decidedBy": "", "note": "",
                                            "askedAtMs": 0, "decidedAtMs": 0})
        info["question"] = question
        if not tap.needs_approval(tool, mode):
            # 档位 off（缺省）⇒ 既有审批门不问人。这一步放行，但**来源写清是 policy-off**：
            # 把「没人被问过」画成「人已批准」是这一层最坏的一种谎。
            view["state"] = "allowed"
            view["endedAtMs"] = int(self._clock() * 1000)
            info["state"] = "allowed"
            info["decidedBy"] = "policy-off"
            info["note"] = (f"工具审批档位是 {mode}：这一步按既有口径自动放行，没有人被问过")
            return {"run": run, "next": step.get("next") or "",
                    "note": "卡点自动放行（档位 off），来源已在运行视图里标明"}
        if not getattr(gate, "ready", False):
            # 档位开着而网关不可用 ⇒ 挂起等人，不静默绕过（与 tool_approval 头注释同口径）。
            # 这里不自动放行，也不失败收工：人说「这次不用问」的合法通道是 approve 端点。
            view["state"] = "waiting"
            info["state"] = "waiting"
            info["decidedBy"] = "gate-unavailable"
            info["askedAtMs"] = int(self._clock() * 1000)
            info["note"] = (f"审批档位已开（{mode}）但本机确认网关不可用："
                            f"{_s(getattr(gate, 'error', '')) or '未下发网关'}——这一步等人通过"
                            " /api/workflow/runs/approve 收，绝不会自动放行")
            run["state"] = "awaiting_approval"
            return {"run": run, "stop": True, "note": info["note"]}
        view["state"] = "waiting"
        info["state"] = "waiting"
        info["decidedBy"] = ""
        info["askedAtMs"] = int(self._clock() * 1000)
        info["note"] = (f"等人确认：{tap.summarize_input(tool, {'question': question})}"
                        f"（网关 {getattr(gate, 'gate_url', '')}）")
        run["state"] = "awaiting_approval"
        return {"run": run, "stop": True, "note": "运行停在人工卡点，等桌面或审批网关给结论"}

    def _run_action(self, run: dict[str, Any], step: dict[str, Any],
                    view: dict[str, Any]) -> dict[str, Any]:
        spec = step.get("spec") or {}
        action = _s(spec.get("action")).lower()
        room = _s(run.get("room"))
        text = _clip(_s(spec.get("text") or spec.get("brief")), MAX_TEXT_CHARS)
        # 动作正文里的 `{step:<id>.output}` 占位符先展开：工作流的「复用」靠的是
        # 同一本定义喂不同内容，写死的正文只能跑一次。
        for sid, out in (run.get("context", {}).get("outputs") or {}).items():
            text = text.replace(f"{{step:{sid}.output}}", _clip(str(out), 800))
        view["output"] = ""

        if action == "team_dispatch":
            team = self._team()
            if team is None:
                return self._blocked(run, step, view, "团队运行时没接上（工作流装配没给 team_provider）")
            res = team.dispatch(room=room, member_ids=[_s(m) for m in (spec.get("memberIds") or []) if _s(m)],
                                brief=text, title=_s(spec.get("title")) or run.get("title", ""))
            return self._settle(run, step, view, res, ok_key="ok",
                                output=json.dumps({"dispatched": res.get("dispatched") or [],
                                                   "refused": res.get("refused") or []},
                                                  ensure_ascii=False, default=str))
        if action == "ticket_wake":
            workers = self._workers()
            if workers is None:
                return self._blocked(run, step, view, "执行体注册表没接上（工作流装配没给 workers_provider）")
            ticket = _s(spec.get("ticket"))
            if not ticket:
                if not room:
                    return self._failed(run, step, view,
                                        "这一步既没写 ticket 也不在带房间的运行里：投给谁都不知道")
                # 缺省走「这次运行自己的那张单」，不是主持人那张单（见 run_ticket_id）。
                ticket = run_ticket_id(room, run.get("runId"))
            res = workers.start_or_wake(
                ticket_id=ticket, title=_s(spec.get("title")) or run.get("title", ""),
                assignment_id=f"wf-{_digest(f'{run.get('runId')}|{step.get('id')}', 12)}",
                message=text, origin=_s(spec.get("origin")) or "agent",
            )
            return self._settle(run, step, view, res, ok_key="ok",
                                output=json.dumps({"delivered": res.get("delivered"),
                                                   "ticketId": ticket}, ensure_ascii=False, default=str))
        if action == "notify_host":
            team = self._team()
            if team is None:
                return self._blocked(run, step, view, "团队运行时没接上（notify_host 需要主持人那张单）")
            res = team.notify_host(room=room, text=text, origin_member="workflow")
            return self._settle(run, step, view, res, ok_key="ok",
                                output=_s(res.get("delivered")) or "journal-only")
        return self._failed(run, step, view, f"动作「{action}」不在表里：这一本定义里那一步没被执行")

    def _settle(self, run: dict[str, Any], step: dict[str, Any], view: dict[str, Any],
                res: dict[str, Any], *, ok_key: str, output: str) -> dict[str, Any]:
        if not isinstance(res, dict) or not res.get(ok_key):
            reason = _s((res or {}).get("reason") or (res or {}).get("message") or "执行体没给成功回执")
            return self._failed(run, step, view, reason)
        if _s(res.get("delivered")) == "awaiting-report":
            # 预留：执行回路要是想「投出去之后再回结果」，就把 delivered 填这个值，
            # 本层停在 awaiting_step 等 /runs/report。
            view["state"] = "waiting"
            run["state"] = "awaiting_step"
            return {"run": run, "stop": True, "note": "动作已投出去，等执行体回执"}
        view["state"] = "succeeded"
        view["endedAtMs"] = int(self._clock() * 1000)
        view["output"] = _clip(output, MAX_TEXT_CHARS)
        view["outputDigest"] = _digest(output)
        run.setdefault("context", {}).setdefault("outputs", {})[_s(step.get("id"))] = output
        return {"run": run, "next": step.get("next") or ""}

    def _blocked(self, run: dict[str, Any], step: dict[str, Any], view: dict[str, Any],
                 reason: str) -> dict[str, Any]:
        """装配缺件：如实 blocked，不是 failed（这一步根本没被执行），也不是成功。"""
        view["state"] = "blocked"
        view["error"] = reason
        view["endedAtMs"] = int(self._clock() * 1000)
        run["state"] = "blocked"
        run["note"] = (f"{reason}。停在步骤「{step.get('id')}」：这不是「跑完了」，"
                       "也不是这步做坏了，是这套安装里根本没有能执行它的东西")
        return {"run": run, "stop": True, "note": run["note"]}

    def _failed(self, run: dict[str, Any], step: dict[str, Any], view: dict[str, Any],
                reason: str) -> dict[str, Any]:
        view["error"] = reason
        # 失败一条记一条（追加，不覆盖）：重试第三次成功时，`output` 只剩最后那份回执，
        # 前两次「为什么慢」必须还在步视图里答得出来，否则界面只能说「成功了」。
        view.setdefault("history", []).append({
            "attempt": int(view.get("attempt") or 1),
            "error": _clip(reason, MAX_TEXT_CHARS),
            "atMs": int(self._clock() * 1000),
        })
        attempts = int(step.get("attempts") or 1)
        if step.get("onError") == "retry" and int(view.get("attempt") or 1) < attempts:
            view["state"] = "running"
            view["output"] = f"第 {view['attempt']} 次失败：{reason}（准备重试，共允许 {attempts} 次）"
            return {"run": run, "next": _s(step.get("id")),  # 原地再来一次
                    "note": f"步骤「{step.get('id')}」失败后重试"}
        view["state"] = "failed"
        view["endedAtMs"] = int(self._clock() * 1000)
        if step.get("onError") == "continue":
            return {"run": run, "next": step.get("next") or "",
                    "note": f"步骤「{step.get('id')}」没做成（{reason}），按 onError=continue 继续往下"}
        run["state"] = "failed"
        run["note"] = f"步骤「{step.get('id')}」失败且没有重试余量：{reason}"
        return {"run": run, "stop": True, "note": run["note"]}

    def _finish(self, run: dict[str, Any], state: str, note: str) -> dict[str, Any]:
        run["state"] = state
        run["note"] = note
        run["currentStepId"] = ""
        run["endedAtMs"] = int(self._clock() * 1000)
        run["updatedAtMs"] = run["endedAtMs"]
        self.runs.upsert(run)
        return {"ok": True, "run": run, "state": state, "note": note}

    # ── 外部回执：人工卡点与异步动作 ─────────────────────────

    def resolve_approval(self, *, run_id: str, step_id: str, decision: str,
                         note: str = "", decided_by: str = "human") -> dict[str, Any]:
        run = self.runs.get(run_id)
        if not isinstance(run, dict):
            return {"ok": False, "code": "RUN_NOT_FOUND", "reason": f"查不到运行「{_s(run_id)}」"}
        if run.get("state") != "awaiting_approval":
            return {"ok": False, "code": "NOT_AWAITING_APPROVAL", "reason": (
                f"这条运行现在不是「等人」状态（{run.get('state')}）：重复点确认不能再推一步，"
                "否则一份定义会跑出两条前进")}
        wf = self._definition_for_run(run)
        if not isinstance(wf, dict):
            # 人已给的结论**没有落脚点**：不能凭一步的名字猜它是不是卡点，也判不了通过后
            # 走向哪儿。结论不应用，但这条运行要如实改成 blocked——否则它会永远冒充「等人」，
            # 而实际上已经没有定义能把它推下去了。
            orphan = self._orphaned(run)
            return {"ok": False, "code": "DEFINITION_UNAVAILABLE", "run": orphan,
                    "state": "blocked", "reason": (
                        "这条运行对应的定义已经查不到（被删或版本滚出保留窗口）："
                        "你给的结论没有应用，运行已按 blocked 记账")}
        step_map = self._step_map(wf)
        step = step_map.get(_s(step_id))
        if step is None or step.get("kind") != "approval":
            return {"ok": False, "code": "STEP_NOT_APPROVAL",
                    "reason": f"步骤「{_s(step_id)}」不是这一本里的人工卡点：不能替别的步给结论"}
        view = next((s for s in run.get("steps") or [] if s.get("stepId") == _s(step_id)), None)
        if view is None:
            return {"ok": False, "code": "STEP_VIEW_MISSING",
                    "reason": f"运行记录里没有步骤「{_s(step_id)}」的视图"}
        dec = _s(decision).lower()
        if dec not in APPROVAL_DECISIONS:
            return {"ok": False, "code": "DECISION_INVALID",
                    "reason": f"结论「{decision}」只能是 allow 或 deny：这里没有「跳过」这种第三种答案"}
        info = view.setdefault("approval", {})
        info["state"] = "allowed" if dec == "allow" else "denied"
        info["decidedBy"] = _s(decided_by) or "human"
        info["note"] = _clip(_s(note), MAX_TEXT_CHARS)
        info["decidedAtMs"] = int(self._clock() * 1000)
        view["state"] = "allowed" if dec == "allow" else "denied"
        view["endedAtMs"] = int(info["decidedAtMs"])
        run["state"] = "running"
        run["updatedAtMs"] = int(info["decidedAtMs"])
        if dec == "allow":
            run["currentStepId"] = step.get("next") or ""
        else:
            run["currentStepId"] = step.get("onDeny") or ""
            if not run["currentStepId"]:
                return self._finish(run, "cancelled",
                                    f"人工拒绝：{info['note'] or '没给原因'}（这一步之后没有配拒绝后的去向，运行按取消收）")
        self.runs.upsert(run)
        return self.advance(run.get("runId", ""))

    def report(self, *, run_id: str, step_id: str, ok: bool, output: str = "",
               error: str = "") -> dict[str, Any]:
        """执行体对某一步异步回执（对应 `awaiting_step`）。"""
        run = self.runs.get(run_id)
        if not isinstance(run, dict):
            return {"ok": False, "code": "RUN_NOT_FOUND", "reason": f"查不到运行「{_s(run_id)}」"}
        if run.get("state") != "awaiting_step":
            # 文案得跟状态对得上：停在「等人确认」的运行根本没收口，说它已收口是把两种
            # 停顿混成一句；但两者都不能凭空接住一份回执，这点是一样的。
            return {"ok": False, "code": "NOT_AWAITING_STEP", "reason": (
                f"这条运行当前是「{run.get('state')}」，不在等回执：这份回执没有落脚点"
                "（只有停在 awaiting_step 的那一步能被回执收，重复上报也不会变成第二次成功）")}
        view = next((s for s in run.get("steps") or [] if s.get("stepId") == _s(step_id)), None)
        if view is None or view.get("state") != "waiting":
            return {"ok": False, "code": "STEP_NOT_WAITING",
                    "reason": f"步骤「{_s(step_id)}」没有在等回执：重复上报不会被当成第二次成功"}
        wf = self._definition_for_run(run)
        if not isinstance(wf, dict):
            orphan = self._orphaned(run)
            return {"ok": False, "code": "DEFINITION_UNAVAILABLE", "run": orphan,
                    "state": "blocked", "reason": (
                        "这条运行的定义已经查不到（被删或版本滚出保留窗口）："
                        "这一步之后该走向哪儿没有依据，回执不能被凭猜推进；运行已按 blocked 记账")}
        step = self._step_map(wf).get(_s(step_id)) or {}
        if ok:
            return self._settle_after_report(run, step, view, output)
        return self._failed_after_report(run, step, view, error or "执行体说这一步没做完")

    def _settle_after_report(self, run: dict[str, Any], step: dict[str, Any],
                             view: dict[str, Any], output: str) -> dict[str, Any]:
        view["state"] = "succeeded"
        view["output"] = _clip(output, MAX_TEXT_CHARS)
        view["outputDigest"] = _digest(output)
        view["endedAtMs"] = int(self._clock() * 1000)
        run.setdefault("context", {}).setdefault("outputs", {})[_s(step.get("id"))] = _clip(output, MAX_TEXT_CHARS)
        run["state"] = "running"
        run["currentStepId"] = step.get("next") or ""
        self.runs.upsert(run)
        return self.advance(run.get("runId", ""))

    def _failed_after_report(self, run: dict[str, Any], step: dict[str, Any],
                             view: dict[str, Any], reason: str) -> dict[str, Any]:
        run["state"] = "running"
        out = self._failed(run, step, view, reason)
        if not out.get("stop"):
            run["currentStepId"] = out.get("next", "")
        self.runs.upsert(run)
        if out.get("stop"):
            return {"ok": True, "run": run, "state": run.get("state"), "note": out.get("note")}
        return self.advance(run.get("runId", ""))

    def cancel(self, *, run_id: str, reason: str = "") -> dict[str, Any]:
        run = self.runs.get(run_id)
        if not isinstance(run, dict):
            return {"ok": False, "code": "RUN_NOT_FOUND", "reason": f"查不到运行「{_s(run_id)}」"}
        if run.get("state") not in ACTIVE_RUN_STATES:
            return {"ok": True, "run": run, "state": run.get("state"),
                    "note": "这条运行已经收口，取消不再改变它（重复点不会被记成第二次取消）"}
        waiting = [s.get("stepId") for s in run.get("steps") or [] if s.get("state") == "waiting"]
        return self._finish(run, "cancelled",
                            f"人取消了这次运行：{_clip(_s(reason), MAX_TEXT_CHARS) or '没给原因'}"
                            f"（已经投出去的唤醒{'：' + '、'.join(waiting) if waiting else '一条也没有'}"
                            "——取消只停本层的推进，不回滚已投递的唤醒，那条要靠工单自己的收口）")

    # ── 视图 ────────────────────────────────────────────────

    def run_view(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if not isinstance(run, dict):
            return {"ok": False, "code": "RUN_NOT_FOUND", "reason": f"查不到运行「{_s(run_id)}」"}
        steps = run.get("steps") or []
        return {
            "ok": True,
            "runId": run.get("runId"),
            "workflowId": run.get("workflowId"),
            "workflowVersion": run.get("workflowVersion"),
            "workflowName": run.get("workflowName"),
            "title": run.get("title"),
            "room": run.get("room"),
            "state": run.get("state"),
            "currentStepId": run.get("currentStepId"),
            "startedAtMs": run.get("startedAtMs"),
            "updatedAtMs": run.get("updatedAtMs"),
            "endedAtMs": run.get("endedAtMs"),
            "executed": run.get("executed"),
            "note": run.get("note"),
            "steps": [{k: v for k, v in s.items()} for s in steps],
            "counts": {
                # `allowed`（人工卡点放行，可能根本没人被问过）**不并进 succeeded**：
                # 并进同一格，界面就分不出「四步真做完了 + 一步自动放行」和「五步都跑完了」。
                "total": len(steps),
                "succeeded": sum(1 for s in steps if s.get("state") == "succeeded"),
                "allowed": sum(1 for s in steps if s.get("state") == "allowed"),
                "failed": sum(1 for s in steps if s.get("state") == "failed"),
                "denied": sum(1 for s in steps if s.get("state") == "denied"),
                "waiting": sum(1 for s in steps if s.get("state") == "waiting"),
                "blocked": sum(1 for s in steps if s.get("state") == "blocked"),
                "pending": sum(1 for s in steps if s.get("state") == "pending"),
            },
            "payload": run.get("payload"),
        }

    def list_runs(self, *, workflow_id: str = "", limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for run in self.runs.all_runs():
            if workflow_id and run.get("workflowId") != _s(workflow_id):
                continue
            steps = run.get("steps") or []
            out.append({
                "runId": run.get("runId"), "workflowId": run.get("workflowId"),
                "workflowVersion": run.get("workflowVersion"), "title": run.get("title"),
                "state": run.get("state"), "room": run.get("room"),
                "startedAtMs": run.get("startedAtMs"), "endedAtMs": run.get("endedAtMs"),
                "stepCount": len(steps),
                "currentStepId": run.get("currentStepId"),
                "awaitingApproval": run.get("state") == "awaiting_approval",
                "autoPassed": sum(1 for s in steps
                                  if (s.get("approval") or {}).get("decidedBy") == "policy-off"),
            })
        out.sort(key=lambda r: -int(r.get("startedAtMs") or 0))
        return out[:max(1, min(int(limit or 30), MAX_RUNS))]

    def pending(self, *, limit: int = 50) -> dict[str, Any]:
        """等人 / 等回执的运行（重启后照样答得出，读的全是盘）。"""
        items = []
        for run in self.runs.all_runs():
            if run.get("state") not in ("awaiting_approval", "awaiting_step", "blocked"):
                continue
            waiting = [s for s in run.get("steps") or [] if s.get("state") in ("waiting", "blocked")]
            items.append({
                "runId": run.get("runId"), "workflowName": run.get("workflowName"),
                "title": run.get("title"), "state": run.get("state"),
                "steps": [{"stepId": s.get("stepId"), "kind": s.get("kind"),
                           "question": (s.get("approval") or {}).get("question") or "",
                           "decidedBy": (s.get("approval") or {}).get("decidedBy") or "",
                           "note": (s.get("approval") or {}).get("note") or s.get("error") or ""}
                          for s in waiting],
            })
        return {"ok": True, "count": len(items), "runs": items[:max(1, min(int(limit or 50), MAX_RUNS))],
                "note": "这里只列**还没收口的**：blocked 也在内——它等人补装配，不等人点确认"}

    def snapshot(self) -> dict[str, Any]:
        runs = self.runs.all_runs()
        gate = self._approval()
        return {
            "workflows": len(self.definitions.all_workflows()),
            "runs": len(runs),
            "byState": {s: sum(1 for r in runs if r.get("state") == s) for s in RUN_STATES},
            "actionKinds": list(ACTION_KINDS),
            "stores": {"definitions": self.definitions.snapshot(), "runs": self.runs.snapshot()},
            "wiring": {
                "team": self._team() is not None,
                "workers": self._workers() is not None,
                "approvalMode": tap.normalize_mode(getattr(gate, "mode", "off")),
                "approvalReady": bool(getattr(gate, "ready", False)),
            },
            "limits": {"maxWorkflows": MAX_WORKFLOWS, "maxSteps": MAX_STEPS,
                       "maxRuns": MAX_RUNS, "maxStepsExecuted": MAX_STEPS_EXECUTED,
                       "maxVersions": MAX_VERSIONS},
        }


# ── 进程内单例（与 team_runtime 同一条规则：一份定义表、一份运行表）──────

_RUNTIME: WorkflowRuntime | None = None


def peek_runtime() -> WorkflowRuntime | None:
    return _RUNTIME


def reset_runtime() -> dict[str, Any]:
    global _RUNTIME
    _RUNTIME = None
    return {"ok": True, "cleared": "workflow runtime"}


def get_workflow_runtime(**kwargs: Any) -> WorkflowRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = WorkflowRuntime(**kwargs)
    return _RUNTIME
