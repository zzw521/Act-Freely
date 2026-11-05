"""工单执行体的适配器（缺陷 D-48.5b 的非竞争半边）

`ticket_dispatch_runtime.TicketDispatcher` 留了三个注入点：execute_turn（跑一轮模型
回合）、send_dispatch（把已提交的副作用真发出去）、kill_job（处置到点该杀的后台作业）。
本模块把这三个注入点接到**引擎既有的实现**上，让「领取回路接线」剩下的工作只是
`build_dispatcher(...)` 加一个端点，而不是再拼一遍逻辑。

刻意不动并行批次的领地：`engine/server.py`（ReActAgent / Toolkit 的装配现场）与 Rust
harness 都不 import、不修改。因此这里的边界是清楚的：

* **模型通道是注入的**（`model_call(payload) -> dict`）——真正的 agentscope 调用、
  流式与用量计量仍归 server.py；本模块负责**回合契约的双向翻译**：把系统提示、工具面、
  对话历史组织成一次请求，把模型回的「文本 + 调用清单」翻译成 Dispatcher 认的
  `ok/outcome/messages/sends/jobs/reason`。
* **外发是真通道**：`make_im_send_fn()` 走 `im_channels` 既有的
  `runtime_of(kind).channel.send(creds, conversation_id, text)`，不新开发送路径；
  自测里注入假的通道，一次 HTTP 也不发。
* **预算与守卫在这里实现**：读取分页（文本默认 4000、上限 8000 单元；列表默认 20、
  上限 50 项）、shell 禁睡等（shell 动词与解释器内联代码里的 `time.sleep` / `setTimeout`
  都拦，`background=true` 不豁免；拦的是睡等**写法**，不是「跑得久」）、执行体只有 6 个
  工具——这三条是 DSH 对照（台账 D-48「差距」段）里唯一还没被任何代码承担的口径。

一条口径值得单独说明：**模型没宣告 finished 就按 running 处理**。回合结束但没结清
作业、或压根没回结局，都不等于「做完了」——按 running 保住槽位与检查点，比按 finished
删点还槽安全，也与 Dispatcher 的「收口不靠自述」同向。

还有一条**核实过之后决定不在这里做**的口径，写清楚免得后人当成漏项：DSH 说「所有外发
（启动、输入交付、中继、通知、回复）走耐久队列」，账本也因此认五个 kind。但**前两个的
对手方是执行体自己**（引擎启动这份领取、把输入交付给这份领取），不是 IM 会话。真把它们
塞进耐久队列会倒置「提交后才执行」——绑定与入箱（`registry.start_or_wake`）在事务
commit **之前**就发生了，先 enqueue 再执行就成了「未提交的动作已经留在账本里」。这一层
真正缺耐久形态的是**已投递未消费的收件箱**：唤醒进 `worker.inbox` 只活在内存里，所以
D-48.5g 的重启补查只能拿调用方（服务端消息表）给的候选来核对。那件事归 D-48.6（投递即
落盘的持久收件箱），不归本适配器。本刀做的是把它顺手能修的那半修掉：**模型面只留
reply / relay / notify**，`launch` / `deliver_input` 被单独拒绝并说明「那是引擎对 worker
的动作」——否则模型能把一条 IM 消息标成 `deliver_input`，而它参与组内保序，会连带卡住
同一（工单, 会话）后面 `reply` 的放行。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
import subprocess
import threading
import time
from typing import Any, Awaitable, Callable

import ticket_dispatch_runtime as tdr
import ticket_inbox_runtime as tir
import ticket_outbox_runtime as tor
import ticket_recovery_runtime as trr
import ticket_worker_runtime as twr

# ── 执行体工具面（收窄：只有 6 个，主会话那 14 个不在这里） ──────────────
WORKER_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "write_file",
    "run_shell",
    "send_to_session",
    "finish_work",
)

TEXT_READ_DEFAULT_UNITS = 4000
TEXT_READ_MAX_UNITS = 8000
LIST_DEFAULT_ITEMS = 20
LIST_MAX_ITEMS = 50

# ── 按剩余窗口取页（缺陷 D-48.8）───────────────────────────────
#: 一个 token 折算成多少个**单元（字符）**。中文约 1 字符/token，英文约 4，代码介于两者
#: 之间——这里取 **2**：宁可少给半页（代价是多翻一页），不要多给半页（代价是这一轮直接
#: 撞穿窗口、整轮失败并回滚副作用）。口径与 `context_budget` 的估算一样是折算值，
#: 不是厂商 tokenizer 的精确计数，页面里也必须把这句话带出来。
TOKENS_TO_UNITS = 2
#: 一页的下限：剩余窗口算出来比这还小也按这个给。工具面**永不返回空页**——空页让模型
#: 读到「后面还有很多」却一个字拿不到，只会反复申请，比撞穿窗口更难诊断。
#: 这个下限的单位是**单元（字符）**，只对正文那一面有意义；列表的单元是「一条」，
#: 拿 200 条当列表下限会顶穿固定上限（50 条），于是窗口再紧也压不动一页——见
#: `WINDOW_PAGE_FLOOR_ITEMS`。两个下限各说各的单位，不用同一个数字冒充。
WINDOW_PAGE_FLOOR_UNITS = 200
#: 列表那一面的下限：**条**。三条是「还能看出这一页长什么样」的最小值，同时保证不清零。
WINDOW_PAGE_FLOOR_ITEMS = 3
#: 留给「这一页之外」的余量比例：模型输出、下一轮的注入、tool schema 都要窗口里的位置。
WINDOW_RESERVE_FRACTION = 0.25

# 等待靠排队，不靠轮询：这几类写法在本机就是「占着槽位睡」，一律拒绝。
# 覆盖面不止 shell 动词——**解释器内联代码里的等待 API 同样是等**（`python -c
# "import time;time.sleep(20)"` 与 `sleep 20` 对调度器是同一件事：本轮不结束、槽不还）。
# background=true **不豁免**：回合结束时若作业还在跑，槽位是留着的（working），所以换
# 个 background 写法并不会让「睡」变成「干活」。
_SLEEP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # shell 层的等待动词
    (re.compile(r"(?<![\w.-])sleep\s+\d", re.IGNORECASE), "sleep"),
    (re.compile(r"start-sleep", re.IGNORECASE), "Start-Sleep"),
    (re.compile(r"(?<![\w.-])timeout\s+/t\s+\d", re.IGNORECASE), "timeout /t"),
    (re.compile(r"\bping\s+-t\b", re.IGNORECASE), "ping -t（无限重复）"),
    (re.compile(r"\bwsl\s+sleep\b", re.IGNORECASE), "wsl sleep"),
    (re.compile(r"\bping\s+-n\s+\d+\s+(?:127\.0\.0\.1|localhost|0\.0\.0\.1|\[::1\])\b", re.IGNORECASE), "ping 回环当 sleep"),
    # 各语言 / 库里的等待 API（含内联 -c / -e / -Command 写法）
    (re.compile(r"\bsleep\s*\(", re.IGNORECASE), "sleep()"),
    (re.compile(r"\b(?:nano|micro)?usleep\s*(?:\(|\s+\d)", re.IGNORECASE), "usleep"),
    (re.compile(r"\bsleep_\w+\s*\(", re.IGNORECASE), "sleep_* 计时函数"),
    (re.compile(r"\bset(?:timeout|interval)\s*\(", re.IGNORECASE), "setTimeout/setInterval"),
    (re.compile(r"\btime\.after\s*\(", re.IGNORECASE), "time.After"),
    (re.compile(r"\bthread\.sleep\s*\(", re.IGNORECASE), "Thread.sleep"),
    (re.compile(r"\bselect\s*\(\s*\d+\s*\)", re.IGNORECASE), "select(秒) 当 sleep"),
    (re.compile(r"\bselect\s*\(\s*undef", re.IGNORECASE), "select(undef…) 当 sleep"),
)

# 模型侧的 send_to_session.kind → 账本 kind。
#
# **只有「对某个会话说话」的三种**在这里。账本认的五个 kind 里，`launch` 与
# `deliver_input` 是**引擎对 worker 的动作**（启动这份领取、把输入交付给这份领取），
# 它们的对手方是执行体本身，不是 IM 会话——把这两种开放给模型会出两件实测得到的事：
# ① `deliver_input` 参与 `ORDERED_KINDS` 的组内保序，模型随手标一个 `deliver_input`
#    就能卡住同一（工单, 会话）后续 `reply` 的放行（保序组不认识「这其实是我发给自己的
#    启动指令」）；② `launch` 走的是另一套按轮放行上限（2），与会话外发的口径无关。
# 所以这两种从模型面摘掉，被拒时给的理由是「那是引擎的动作」而不是「不认识这个 kind」——
# 模型要唤醒执行体走的是服务端领取 + `submit` / 重启补查，不是往会话发一条叫 launch 的消息。
# 拒绝位置照旧：**进账本之前**，一条记录也不落。
MODEL_SEND_KINDS: dict[str, str] = {
    "relay": tor.KIND_RELAY,
    "reply": tor.KIND_REPLY,
    "notify": tor.KIND_NOTIFY,
}
#: 账本认识、但不属于模型外发面的两个 kind（引擎对 worker 的动作）
ENGINE_WORKER_KINDS: tuple[str, str] = (tor.KIND_LAUNCH, tor.KIND_DELIVER_INPUT)


class DeliveryError(RuntimeError):
    """外发投递失败的中文理由（账本记下原话，退避重投时看得见到底为什么没发出去）。"""


def _digest(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


# ── 读取分页预算 ─────────────────────────────────────────────────
def clamp_units(requested: Any, *, default: int, maximum: int, what: str) -> tuple[int, str]:
    """把「我要读多少」夹进 [1, maximum]，缺省取 default。

    越界**如实带话**而不是默默按默认：模型会看到自己申请的量被改成了多少，
    下一轮才可能改成「翻页读」而不是再申请一次超量。"""
    note = ""
    if requested is None or requested == "":
        return default, note
    try:
        value = int(str(requested).strip())
    except (TypeError, ValueError):
        return default, f"{what}「{requested}」不是整数，已按默认 {default} 执行。"
    if value <= 0:
        return default, f"{what}「{value}」不是正数，已按默认 {default} 执行。"
    if value > maximum:
        return maximum, f"{what}「{value}」超出上限 {maximum}，已按上限截断——要读更多请分页。"
    if value < default:
        return value, f"{what}按你的请求取 {value}（低于默认 {default}，本次只给这些）。"
    return value, note


def clamp_start(requested: Any) -> tuple[int, str]:
    """分页起点：非负整数，认不出来就按 0 并**如实带话**（不默默改模型的意图）。"""
    if requested is None or requested == "":
        return 0, ""
    try:
        value = int(str(requested).strip())
    except (TypeError, ValueError):
        return 0, f"start「{requested}」不是整数，本次从头开始读。"
    if value < 0:
        return 0, f"start「{value}」是负数，本次从头开始读。"
    return value, ""


def window_budget(
    window: Any,
    *,
    default: int,
    maximum: int,
    unit_tokens: float,
    what: str = "单元",
    floor: Any = None,
) -> tuple[int, int, str]:
    """按**剩余上下文窗口**把这一页的上限压小：回 `(可用的 default, 可用的 maximum, 中文说明)`。

    没有窗口信息（None / 空 / 窗口数不靠谱）就原样把固定预算还回去，并回**空说明**——这一条
    很重要：今天所有装配都没配 `contextWindow`，若这里回一句「按窗口收窄了」，页面就在说谎。

    三条刻意的取舍：

    * **只压不抬**。窗口剩得多也不会一页给过 `maximum`：固定上限是三面（worker 执行体、Rust
      harness、引擎主会话）同源的口径，本层的工作是把「这一轮装不下」兜住，不是重新发明预算。
    * **留出余量**（`WINDOW_RESERVE_FRACTION`）。这一页之后模型还要输出、下一轮还要注入系统段
      与工具 schema；按剩余窗口 100% 取页等于保证下一轮撞墙。
    * **永不清零**（`floor`，正文取 `WINDOW_PAGE_FLOOR_UNITS`、列表取 `WINDOW_PAGE_FLOOR_ITEMS`）。
      剩余算出来是 0 也按下限给一页，并在说明里写「窗口按实测已经用满」——工具面返回空正文是
      可读到的最坏结果：模型被告知后面还有内容，却一个字符都拿不到，只能反复申请同一页。
      真撞穿窗口由上下文压缩那层负责，边界如实留着。
      下限**必须按页的单位给**（正文是字符、列表是条目）：拿正文的 200 当列表的下限，
      会顶穿列表的固定上限 50，于是窗口再紧也算不出变化——这正是这一层最容易犯的错。

    `unit_tokens` 是**一个单元花多少 token**：正文的单元是字符（`1 / TOKENS_TO_UNITS`，按折算
    值），列表的单元是条目（由调用方按这一页的真实条目长度算，见 `list_unit_tokens`）。
    """
    info = window if isinstance(window, dict) else {}
    window_tokens = _positive_number(info.get("contextWindowTokens") or info.get("contextWindow"))
    if window_tokens <= 0:
        return default, maximum, ""
    floor_units = _positive_number(floor) if floor is not None else WINDOW_PAGE_FLOOR_UNITS
    floor_units = max(1, int(min(floor_units, maximum)))
    used_tokens = _positive_number(info.get("usedTokens") or info.get("used"))
    remaining = window_tokens - used_tokens
    if remaining <= 0:
        cap = floor_units
        return min(default, cap), cap, (
            f"上下文窗口按实测已经用满（窗口 {window_tokens} tokens，本轮已用约 {used_tokens}）："
            f"这一页仍按最小 {cap} {what} 给，不是「读完了」。剩下的窗口归压缩那层腾，"
            "这一页请只取真正需要的部分。"
        )
    available = int(remaining * (1.0 - WINDOW_RESERVE_FRACTION))
    cap = int(available / max(unit_tokens, 1e-6))
    if cap >= maximum:
        # 窗口还很宽：不收窄这一页，也不说一句关于窗口的好话（说了就是噪声）
        return default, maximum, ""
    # 下限不能越过 maximum：列表那一面调用方会拿 maximum=当前条数来复用本函数
    # （「只许把这一页压小」），按下限还给 200 项就成了「压小反而变大」。
    cap = max(floor_units, min(cap, maximum))
    note = (
        f"这一页按剩余上下文窗口取：窗口 {window_tokens} tokens、本轮已用约 {used_tokens}，"
        f"留 {int(WINDOW_RESERVE_FRACTION * 100)}% 余量后按 1 token≈{TOKENS_TO_UNITS} 字符的保守折算，"
        f"上限压到 {cap} {what}（固定上限 {maximum} 本来更高）。这是**折算值不是厂商 tokenizer 计数**，"
        "需要更多就带 start 接着翻页，别一轮塞满。"
    )
    return min(default, cap), cap, note


def list_unit_tokens(items: list[Any]) -> float:
    """一页列表的**实测**单条成本：拿这一页真实条目的平均字符数折算成 token。

    比拿一个「每条大约 N token」的编造值诚实：目录条目从 `notes.md` 到八行嵌套路径都可能，
    平均长度是能算的。列表为空时按 1 token/条（那本来就没有内容压力）。"""
    lst = list(items or [])
    if not lst:
        return 1.0
    chars = sum(len(str(item)) for item in lst)
    return max(1.0, (chars / len(lst)) / TOKENS_TO_UNITS)


class ReadWindowMeter:
    """一轮里「窗口多大 + 已经用了多少」的两个读数：取页时唯一要的输入。

    两个读数都**迟到**——窗口要等策略归一化算完（含故障转移链取最小窗口那条），用量要等
    第一次模型调用回报。所以这个对象先建后填，工具面读的是「现在这一轮的读数」而不是
    装配期的快照：快照在换模型配置的那一刻就过期了。

    `note_usage` 取**较大者**：一个任务里 prompt 逐轮变长，读数回退只可能是上游报了个残缺值，
    把它当真实用量往下走会让后面每一页都偏大。窗口为 0（没填、也没估）时 `reading()` 回空
    字典——空字典让 `window_budget` 走固定预算那条完全不变的老路。
    """

    def __init__(self, context_window: Any = 0, *, from_model: bool = False) -> None:
        self.context_window = int(_positive_number(context_window))
        self.from_model = bool(from_model)
        self.used_tokens = 0
        self.usage_from_report = False

    def configure(self, context_window: Any, from_model: bool) -> None:
        window = int(_positive_number(context_window))
        if window:
            self.context_window = window
            self.from_model = bool(from_model)

    def note_usage(self, prompt_tokens: Any, *, from_report: bool = True) -> None:
        used = int(_positive_number(prompt_tokens))
        if used <= 0:
            return
        if used >= self.used_tokens:
            self.used_tokens = used
            self.usage_from_report = bool(from_report)

    def reading(self) -> dict[str, Any]:
        if not self.context_window:
            return {}
        return {
            "contextWindowTokens": self.context_window,
            "contextWindowFromModel": self.from_model,
            "usedTokens": self.used_tokens,
            "usageFromReport": self.usage_from_report,
        }


def _positive_number(value: Any) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def budgeted_read(
    text: str, requested: Any = None, start: Any = 0, *, window: Any = None
) -> dict[str, Any]:
    """文件正文的读取预算：按**字符**计单元（不是 token，口径如实写明），支持 `start` 分页。

    预算提示词让模型「需要更多请带 start 分页」，所以分页这一半必须真的存在——
    否则每一轮拿到的都是同一段前缀，模型看着「还剩 8000 单元」反复申请，白烧轮次。

    `window` 给了本轮的窗口与用量（`{contextWindowTokens, usedTokens}`）就按剩余窗口压上限，
    见 `window_budget`；不给就是原来的固定预算，一个字都不多——页面里也不会出现关于窗口的话。"""
    default, maximum, window_note = window_budget(
        window,
        default=TEXT_READ_DEFAULT_UNITS,
        maximum=TEXT_READ_MAX_UNITS,
        unit_tokens=1.0 / TOKENS_TO_UNITS,
        what="个单元",
    )
    units, note = clamp_units(requested, default=default, maximum=maximum, what="读取单元")
    offset, offset_note = clamp_start(start)
    body = str(text or "")
    if offset > len(body):
        note = (note + " " if note else "") + f"start={offset} 已超过正文长度 {len(body)}，本次没有内容可给。"
    chunk = body[offset : offset + units]
    truncated = offset + len(chunk) < len(body)
    head = (window_note + " " if window_note else "") + note
    out = {
        "text": chunk,
        "units": units,
        "start": offset,
        "totalUnits": len(body),
        "truncated": truncated,
        "windowApplied": bool(window_note),
        "note": (head + " " if head else "") + offset_note,
    }
    if truncated:
        remaining = len(body) - (offset + len(chunk))
        more = f"后面还有 {remaining} 个单元。" if not out["note"] else f"（已截断，后面还有 {remaining} 个单元）"
        out["note"] = (out["note"] + " " if out["note"] else "") + f"正文按预算取第 {offset} 起 {units} 个单元（单元=字符，非 token）{more}需要更多就带 start={offset + len(chunk)} 接着读，别整篇塞进上下文。"
    return out


def budgeted_list(items: list[Any], requested: Any = None, *, window: Any = None) -> dict[str, Any]:
    """目录/清单类结果的条数预算：默认 20、上限 50，超量如实报还剩多少。

    列表的**单元是条目**，所以窗口换算的单条成本取这一页真实条目的平均长度（`list_unit_tokens`），
    不是「每条大约 N token」那种编造值——目录条目从 `notes.md` 到嵌套八层的路径都可能。"""
    lst = list(items or [])
    default, maximum, window_note = window_budget(
        window,
        default=LIST_DEFAULT_ITEMS,
        maximum=LIST_MAX_ITEMS,
        unit_tokens=list_unit_tokens(lst),
        what="项",
        floor=WINDOW_PAGE_FLOOR_ITEMS,
    )
    count, note = clamp_units(requested, default=default, maximum=maximum, what="列出条数")
    truncated = len(lst) > count
    return {
        "items": lst[:count],
        "count": count,
        "total": len(lst),
        "truncated": truncated,
        "windowApplied": bool(window_note),
        "note": ((window_note + " " if window_note else "") + note + " " if (window_note or note) else "")
        + (f"共 {len(lst)} 项，本次只给前 {count} 项，还有 {len(lst) - count} 项没列。" if truncated else ""),
    }


def find_sleep_pattern(command: str) -> str:
    """命令里有没有明摆着的「睡等写法」——有就返回它的名字，没有回空串。

    这是**判定面**，不带任何理由文案，所以主会话（`tool_grants.run_shell`）和执行体
    （`guard_shell_command`）可以共用同一张 `_SLEEP_PATTERNS` 表：表的真值是「什么算睡等」，
    而「为什么在你这一层不许」两边说得不一样（执行体占的是 worker 槽位、等的是唤醒；
    主会话占的是这一轮 ReAct 回合与任务额度）。文案各写各的，**判定只有一份**。"""
    cmd = str(command or "").strip()
    for pattern, label in _SLEEP_PATTERNS:
        if pattern.search(cmd):
            return label
    return ""


def guard_shell_command(command: str) -> dict[str, Any]:
    """执行体 shell 的等待类命令拒绝——等待靠排队（唤醒/副作用队列），不靠轮询。

    判定看的是**写法会不会明摆着在睡等**，不是「命令跑得久不久」：`npm run build`、
    `pytest -q` 这类真活再慢也放行（跑得久的后台作业归 30 分钟看门狗管，不归这里）。
    解释器内联代码里的 `time.sleep(...)` / `setTimeout(...)` 一样算睡等，不因换了
    `-c` / `-e` 写法放过；`background=true` 也不豁免——回合结束时作业还在跑就不还槽，
    所以「转后台再睡」并没有把等待变成不占槽。
    边界如实说明：只看这一条命令字符串。脚本文件内部的 sleep、`heredoc` 里的等待、
    以及 `Event().wait()` 这类不叫 sleep 的阻塞，本层判不出来（也不假装判得出）。"""
    cmd = str(command or "").strip()
    if not cmd:
        return {"ok": False, "reason": "命令为空。"}
    label = find_sleep_pattern(cmd)
    if label:
        return {
            "ok": False,
            "reason": f"命令里有 {label}——执行体不许用 sleep 占着槽位等；"
            "要等外部事件就让本轮结束，事件到了由唤醒（deliver_input）再拉起，等待靠排队不靠轮询。"
            "换 background=true 也一样拦：作业没跑完槽位就不还，转后台不是不占槽。",
        }
    return {"ok": True, "reason": ""}


# ── 系统提示与工具面声明 ──────────────────────────────────────────
def tool_specs() -> list[dict[str, Any]]:
    """6 个工具的声明（带预算默认值）——接线侧拿去注册 Toolkit，模型面也照这份收窄。"""
    return [
        {"name": "read_file", "args": ["path", "start", "units"], "budget": f"默认 {TEXT_READ_DEFAULT_UNITS} 单元、上限 {TEXT_READ_MAX_UNITS}（单元=字符）"},
        {"name": "list_dir", "args": ["path", "limit"], "budget": f"默认 {LIST_DEFAULT_ITEMS} 项、上限 {LIST_MAX_ITEMS}"},
        {"name": "write_file", "args": ["path", "content"], "budget": "写盘只在本工单目录内"},
        {"name": "run_shell", "args": ["command", "background"], "budget": "禁睡等：shell 动词与解释器内联代码里的 sleep/setTimeout 都拦，background 也不豁免；真慢的活可以跑，但必须在 30 分钟内自己结束"},
        {"name": "send_to_session", "args": ["kind", "sessionKey", "text", "dedupeKey"], "budget": "kind ∈ reply/relay/notify（只有对会话说话这三种）；launch/deliver_input 是引擎对 worker 的动作，发会被拒。只入队，提交后才真发"},
        {"name": "finish_work", "args": ["summary"], "budget": "只在真的做完时调用；没做完就别宣告完成"},
    ]


def worker_system_prompt(*, goal: str, workspace_dir: str, machine: str = "", budget_hint: str = "") -> str:
    lines = [
        "你是这条工单的执行体（worker），只在自己的目录里干活。",
        f"工单目标：{goal or '（未填写，先按唤醒内容判断）'}",
        f"工作目录：{workspace_dir}" + (f"（机器：{machine}）" if machine else ""),
        "可用工具只有这些：" + "、".join(WORKER_TOOLS) + "。没有 shell 之外的任意网络出口。",
        f"读取预算：正文默认 {TEXT_READ_DEFAULT_UNITS}、上限 {TEXT_READ_MAX_UNITS} 单元；列表默认 {LIST_DEFAULT_ITEMS}、上限 {LIST_MAX_ITEMS} 项。"
        "超限会被截断并告诉你还剩多少，需要更多请分页，不要把整篇塞进上下文。",
        "等待靠排队不靠轮询：shell 里禁用 sleep / Start-Sleep / timeout /t / ping -t，换成 `python -c \"import time;time.sleep(20)\"` 这类内联写法同样拦；"
        "要等外部事件就结束本轮，等唤醒再拉起。转 background 也不是不占槽——作业没结束槽位就不还。",
        "外发只有一条路：send_to_session，kind 只有 reply / relay / notify（都是「对某个会话说话」）。它只是把动作记进副作用账本，"
        "**事务提交之后才真的发出去**；同一会话内的投递严格保序，失败会退避重投，不许自己绕过队列发 HTTP。"
        "`launch` / `deliver_input` 不是你能发的——那是引擎对 worker 的动作，唤醒你的是服务端的领取与投递，不是你自己往会话里发一条启动指令。",
        "只有确证做完才调用 finish_work 并给出 summary；没做完就把本轮结束（会被当作仍在进行，槽位与检查点都留着）。",
        "恢复注记（如有）优先于你的记忆：先按注记核对目录、git 历史与副作用账本，再决定要不要重发任何外部动作。",
    ]
    if budget_hint:
        lines.append(budget_hint)
    return "\n".join(lines)


# ── execute_turn：模型通道 ⇄ 回合契约的双向翻译 ────────────────────
def _classify_call(call: dict[str, Any], *, default_session_key: str) -> dict[str, Any]:
    """把模型的一次工具调用翻译成 {channel: send|job|finish|reject, ...}。

    未知工具名与非法 kind 都在这里拒掉，**不进账本**——账本里那条
    「未知 kind 被拒」的注记是给已经排队的动作兜底的，不该拿它当常规出口。"""
    tool = str(call.get("tool") or call.get("name") or "").strip()
    args = call.get("args") if isinstance(call.get("args"), dict) else (call.get("input") if isinstance(call.get("input"), dict) else {})
    if tool not in WORKER_TOOLS:
        return {"channel": "reject", "tool": tool, "reason": f"工具「{tool or '（空）'}」不在执行体的 6 个工具里，这条调用没被执行。"}
    if tool == "send_to_session":
        wanted = str(args.get("kind") or "").strip()
        mapped = MODEL_SEND_KINDS.get(wanted)
        if mapped is None:
            if wanted in ENGINE_WORKER_KINDS:
                return {
                    "channel": "reject",
                    "tool": tool,
                    "reason": (
                        f"外发 kind「{wanted}」是**引擎对 worker 的动作**（启动这份领取 / 把输入交付给这份领取），"
                        "不是发给 IM 会话的消息，执行体没有发它的权限：这条没进队列。"
                        "要唤醒这条工单的执行体，走服务端领取 + `submit`（崩溃后是重启补查），"
                        "而不是往会话里发一条标成「"
                        + wanted
                        + "」的消息——`deliver_input` 还会占住同会话的保序组，把后面的 `reply` 一起卡住。"
                    ),
                }
            return {
                "channel": "reject",
                "tool": tool,
                "reason": f"外发 kind「{wanted or '（空）'}」不认识，能发给会话的只有 {'/'.join(MODEL_SEND_KINDS)} 三种；这条没进队列，"
                "不存在「当成别的形式发出去」。",
            }
        session_key = str(args.get("sessionKey") or default_session_key or "").strip()
        text = str(args.get("text") or args.get("payload") or "")
        if not session_key:
            return {"channel": "reject", "tool": tool, "reason": f"外发「{mapped}」没给 sessionKey，不知道要发给哪个会话；这条没进队列。"}
        if not text.strip():
            return {"channel": "reject", "tool": tool, "reason": f"外发「{mapped}」正文为空，这条没进队列（空消息不该打扰会话）。"}
        return {
            "channel": "send",
            "tool": tool,
            "send": {"kind": mapped, "sessionKey": session_key, "payload": text, "dedupeKey": str(args.get("dedupeKey") or "")},
        }
    if tool == "run_shell":
        command = str(args.get("command") or "")
        verdict = guard_shell_command(command)
        if not verdict["ok"]:
            return {"channel": "reject", "tool": tool, "reason": verdict["reason"]}
        if args.get("background"):
            return {
                "channel": "job",
                "tool": tool,
                "job": {
                    # 摘要而不是 hash()：内置 hash 按进程随机化，同一命令换进程就变 id，
                    # 恢复注记与作业表都会认不出同一条作业。
                    "jobId": "job-" + _digest(command.strip())[:12],
                    "description": command.strip()[:120],
                    # 起进程用这一份完整的：description 是给人看的、被 120 字截过的，
                    # 拿它去拆 argv 会在半个引号上炸（炸在回合里=整轮失败+副作用回滚，
                    # 一条命令不成形不该有这种威力）。
                    "command": command.strip(),
                },
            }
        return {"channel": "exec", "tool": tool, "command": command}
    if tool == "finish_work":
        return {"channel": "finish", "tool": tool, "summary": str(args.get("summary") or args.get("text") or "")}
    return {"channel": "work", "tool": tool, "args": args}


def make_execute_turn(
    model_call: Callable[[dict[str, Any]], Any],
    *,
    goal: str = "",
    default_session_key: str = "",
    run_command: Callable[[str], dict[str, Any]] | None = None,
    read_file: Callable[[str, int, int], dict[str, Any]] | None = None,
    list_dir: Callable[[str, int], dict[str, Any]] | None = None,
    write_file: Callable[[str, str], dict[str, Any]] | None = None,
    max_tool_rounds: int = 8,
    processes: "JobProcessTable | None" = None,
) -> Callable[[dict[str, Any], str, list], dict[str, Any]]:
    """造出 Dispatcher 要的 execute_turn。

    `model_call(payload)` 由接线侧提供（引擎里真实那一半是 `ticket_model_channel`），payload 形如
    `{system, messages, tools}`，返回 `{text?, calls?[], outcome?, reason?}`。本函数
    负责的是剩下的一切：多轮工具往返（上限 `max_tool_rounds`）、把调用清单翻译成
    外发/作业/收口，以及**任何被拒的动作都在对话里留下一句原话**，让下一轮的模型看得见
    自己哪里没做成。

    `calls` 里带了 `rejectReason` 的那条**不进 `_classify_call`**：那是通道层面已经判死的调用
    （参数 JSON 被截断、没有工具名），补全或改判成别的工具都是替模型编话，原话回给下一轮就够。
    """
    def execute_turn(worker_snapshot: dict[str, Any], prompt: str, history: list) -> dict[str, Any]:
        system = worker_system_prompt(
            goal=goal,
            workspace_dir=str(worker_snapshot.get("dirName") or ""),
            machine=str(worker_snapshot.get("machine") or ""),
            budget_hint=str(worker_snapshot.get("recoveryNote") or ""),
        )
        messages: list[dict[str, Any]] = list(history or [])
        messages.append({"role": "user", "content": prompt})
        sends: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        notes: list[str] = []
        outcome = ""
        reason = ""
        finished_summary = ""

        for _round in range(max(1, int(max_tool_rounds))):
            request = {"system": system, "messages": messages, "tools": list(WORKER_TOOLS)}
            try:
                raw = model_call(request)
            except Exception as exc:  # noqa: BLE001  模型通道炸了是回合失败，不带着半截状态外发
                return {
                    "ok": False,
                    "outcome": "finished",
                    "messages": messages,
                    "sends": [],
                    "jobs": jobs,
                    "reason": f"模型调用失败：{type(exc).__name__}: {exc}",
                }
            reply = dict(raw or {})
            declared = str(reply.get("outcome") or "")
            if declared and declared not in tdr.VALID_OUTCOMES:
                return {
                    "ok": False,
                    "outcome": "finished",
                    "messages": messages,
                    "sends": [],
                    "jobs": jobs,
                    "reason": f"模型回了未知结局「{declared}」，只接受 {'/'.join(tdr.VALID_OUTCOMES)}——本轮不采信。",
                }
            text = str(reply.get("text") or "")
            if text:
                messages.append({"role": "assistant", "content": text})
            calls = reply.get("calls")
            if not isinstance(calls, list):
                calls = []
            if not calls:
                outcome = declared or outcome
                reason = str(reply.get("reason") or finished_summary or "")
                break

            feedback: list[str] = []
            for call in calls:
                item = call if isinstance(call, dict) else {}
                pre_reject = str(item.get("rejectReason") or "").strip()
                if pre_reject:
                    # 模型通道已经判死的那条（参数不是合法 JSON / 没有工具名）：这里不重判、
                    # 不补全，也不当成别的工具跑——通道写明的原话直接回给下一轮的模型。
                    verdict = {"channel": "reject", "tool": str(item.get("tool") or ""), "reason": pre_reject}
                else:
                    verdict = _classify_call(item, default_session_key=default_session_key)
                channel = verdict["channel"]
                if channel == "reject":
                    notes.append(verdict["reason"])
                    feedback.append(f"被拒：{verdict['reason']}")
                elif channel == "send":
                    sends.append(verdict["send"])
                    feedback.append(f"已记入副作用账本（{verdict['send']['kind']} → {verdict['send']['sessionKey']}），提交后才真正发出。")
                elif channel == "job":
                    job = dict(verdict["job"])
                    if processes is None:
                        notes.append("run_shell(background) 没配进程表：这条作业没起进程，只按登记计时（接线侧应传入 JobProcessTable）。")
                        feedback.append(f"后台作业已登记（但没有进程表，实际进程未启动）：{job['description']}")
                        # 照样进 jobs 清单：看门狗据它计时并在报告里点名。悄悄丢掉的话，
                        # 这条 30 分钟上限对「登记了却没人管」的作业就完全不生效。
                        job["processStarted"] = False
                        jobs.append(job)
                    else:
                        try:
                            # 拆 argv 用完整命令（description 被 120 字截过，半个引号会让 shlex 当场抛）
                            argv = shlex.split(str(job.get("command") or job["description"]))
                        except ValueError as exc:
                            notes.append(f"后台命令拆不出 argv：{exc}——这条作业没起进程，也没登记。")
                            feedback.append(f"后台命令没能起进程（这条命令本身不成形）：{exc}")
                        else:
                            spawned = processes.spawn(job["jobId"], argv)
                            if not spawned.get("ok"):
                                notes.append(f"后台作业没起来：{spawned.get('reason')}——不登记一条并不存在的作业。")
                                feedback.append(f"后台作业启动失败：{spawned.get('reason')}")
                            else:
                                jobs.append(job)
                                feedback.append(f"后台作业已起进程并登记：{job['description']}（30 分钟到点会被处置）")
                elif channel == "finish":
                    outcome = "finished"
                    finished_summary = str(verdict.get("summary") or "")
                    feedback.append("已宣告完成，本轮收口。")
                elif channel == "exec":
                    if run_command is None:
                        feedback.append("run_shell 的真实执行由接线侧注入（本适配器不起进程）；这条命令没有执行。")
                        notes.append("run_shell 未注入执行实现，命令没跑。")
                    else:
                        result = dict(run_command(str(verdict.get("command") or "")) or {})
                        feedback.append(str(result.get("output") or result.get("reason") or "命令已执行，无输出"))
                        messages.append({"role": "tool", "content": str(result.get("output") or "")})
                else:  # channel == "work"：读/列/写，工具实现同样由接线侧注入，这里按预算给出口
                    feedback.append(_run_file_tool(verdict, read_file=read_file, list_dir=list_dir, write_file=write_file))
            if feedback:
                messages.append({"role": "tool", "content": "\n".join(feedback)})
            # 模型在同一回合里既下指令又报结局时，结局优先于「再来一轮」——
            # 否则一句 outcome:running 会把回合拖进往返上限，白烧轮次。
            if outcome == "finished" or declared:
                outcome = outcome or declared
                reason = finished_summary or str(reply.get("reason") or "")
                break
        else:
            return {
                "ok": False,
                "outcome": "finished",
                "messages": messages,
                "sends": [],
                "jobs": jobs,
                "reason": f"工具往返超过 {max_tool_rounds} 轮仍未收口——本轮按失败作废，外发一条也不提交。",
            }

        if not outcome:
            # 没宣告 finished = 还在跑：保住槽位与检查点，等下一次唤醒续跑
            outcome = "running"
            reason = reason or "模型未宣告完成，本轮按「仍在进行」处理（槽位与检查点都留着）。"
        return {"ok": True, "outcome": outcome, "messages": messages, "sends": sends, "jobs": jobs, "reason": reason, "adapterNotes": notes}

    return execute_turn


def _run_file_tool(verdict: dict[str, Any], *, read_file: Any, list_dir: Any, write_file: Any) -> str:
    tool = str(verdict.get("tool") or "")
    args = verdict.get("args") or {}
    impl = {"read_file": read_file, "list_dir": list_dir, "write_file": write_file}.get(tool)
    if impl is None:
        return f"{tool} 的真实实现由接线侧注入（引擎的 Toolkit/沙箱在 server.py 领地），这条调用没有执行。"
    if tool == "read_file":
        units, _ = clamp_units(args.get("units"), default=TEXT_READ_DEFAULT_UNITS, maximum=TEXT_READ_MAX_UNITS, what="读取单元")
        start, _ = clamp_start(args.get("start"))
        got = dict(impl(str(args.get("path") or ""), units, start) or {})
        return str(got.get("text") or got.get("reason") or "读取完成，无内容") + (f"　{got['note']}" if got.get("note") else "")
    if tool == "list_dir":
        limit, _ = clamp_units(args.get("limit"), default=LIST_DEFAULT_ITEMS, maximum=LIST_MAX_ITEMS, what="列出条数")
        got = dict(impl(str(args.get("path") or ""), limit) or {})
        items = got.get("items") or got.get("entries") or []
        return "、".join(str(x) for x in items) if items else str(got.get("reason") or "目录为空")
    written = dict(impl(str(args.get("path") or ""), str(args.get("content") or "")) or {})
    return str(written.get("reason") or "写入完成")


# ── send_dispatch：已提交的副作用 → 真实通道 ──────────────────────
def parse_session_key(session_key: str) -> tuple[str, str]:
    """会话键的口径：`<通道 kind>:<会话 id>`。

    与 `im_channels` 的会话台账同构（source + conversationId）。不合规就报错，
    不猜「大概是发给桌面端」——猜错的投递是对别人会话的打扰。"""
    raw = str(session_key or "").strip()
    if ":" not in raw:
        raise DeliveryError(f"sessionKey「{raw}」不是「通道:会话id」的形状，不知道往哪发")
    kind, target = raw.split(":", 1)
    kind = kind.strip()
    target = target.strip()
    if not kind or not target:
        raise DeliveryError(f"sessionKey「{raw}」的通道或会话 id 为空")
    return kind, target


def make_send_dispatch(send_fn: Callable[[str, str, str], Any] | None = None) -> Callable[[dict[str, Any]], Any]:
    """造出 outbox 的 dispatch：记录 → (通道 kind, 会话 id, 正文)，返回真值即成功。

    `send_fn=None` 时只解析不通话——给自测与「通道还没接」的中间态用：此时任何
    投递都如实失败（退避重投），而不是假装发出去了。"""
    def dispatch(record: dict[str, Any]) -> bool:
        payload = record.get("payload") or {}
        text = str(payload.get("text") or "")
        if not text.strip():
            raise DeliveryError("载荷正文为空，这条不该发出去")
        if send_fn is None:
            raise DeliveryError(f"IM 通道尚未接线（kind={record.get('kind')}），这条按失败退避，不假装已送达")
        channel_kind, target = parse_session_key(str(record.get("sessionKey") or ""))
        send_fn(channel_kind, target, text)
        return True

    return dispatch


def make_im_send_fn(
    *,
    runtime_of: Callable[[str], Any] | None = None,
    run_coro: Callable[[Awaitable[Any]], Any] | None = None,
    timeout_seconds: float = 30.0,
) -> Callable[[str, str, str], None]:
    """真实投递：走 im_channels 既有的 `runtime.channel.send(creds, conversation_id, text)`。

    两个注入点都是为了让这一层在自测里不碰网络：`runtime_of` 缺省取
    `im_channels.runtime_of`（惰性 import，不拉 FastAPI），`run_coro` 把协程跑成同步
    结果（worker 回合跑在同步循环里，通道 send 是 async）。凭据缺失、通道不支持
    都在此如实报中文理由，交给账本退避。"""
    def send_fn(channel_kind: str, target: str, text: str) -> None:
        if runtime_of is None:
            import im_channels  # 惰性：自测与未接线时都不该把 aiohttp/FastAPI 拉进来

            resolver = im_channels.runtime_of
        else:
            resolver = runtime_of
        try:
            runtime = resolver(channel_kind)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"通道「{channel_kind}」不存在或没起来：{type(exc).__name__}: {exc}") from exc
        channel = getattr(runtime, "channel", None)
        if channel is None:
            raise DeliveryError(f"通道「{channel_kind}」没有可用的 channel 对象")
        if not getattr(channel, "supported", False):
            raise DeliveryError(f"通道「{getattr(channel, 'label', channel_kind)}」尚未接入引擎侧长连接：{getattr(channel, 'unsupported_reason', '')}")
        creds = runtime.creds()
        if not creds:
            raise DeliveryError(f"通道「{channel_kind}」没配置凭据，发不出去")
        coro = channel.send(creds, target, text)
        if run_coro is not None:
            outcome = run_coro(coro)
        else:
            outcome = asyncio.run(_with_timeout(coro, timeout_seconds))
        if outcome is False:
            raise DeliveryError(f"通道「{channel_kind}」回报投递失败")

    return send_fn


async def _with_timeout(coro: Awaitable[Any], seconds: float) -> Any:
    return await asyncio.wait_for(coro, timeout=seconds)


def make_loop_bridge(loop: Any) -> Callable[[Awaitable[Any]], Any]:
    """同步线程里驱动引擎事件循环的桥：`asyncio.run_coroutine_threadsafe(...).result()`。

    引擎的 HTTP 会话与通道长连接都挂在 uvicorn 的循环上，worker 回合是同步循环，
    所以投递必须跨循环回投；超时要给死值，不能让一条卡住的投递挂住整个槽位。"""
    def run_coro(coro: Awaitable[Any], timeout: float = 30.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise DeliveryError(f"投递超过 {timeout} 秒没回话，已取消（账本会退避重投）") from exc

    return run_coro


# ── kill_job：到点作业的处置 ─────────────────────────────────────
class JobProcessTable:
    """jobId → 真实进程句柄。看门狗只交回作业字典，句柄在这里查，杀不掉就如实抛。"""

    def __init__(self, *, popen: Callable[..., Any] = subprocess.Popen):
        self._popen = popen
        self._lock = threading.Lock()
        self._handles: dict[str, Any] = {}

    def spawn(self, job_id: str, argv: list[str], *, cwd: str | None = None) -> dict[str, Any]:
        jid = str(job_id or "").strip()
        if not jid:
            return {"ok": False, "reason": "jobId 为空——没有句柄表收得下这条作业"}
        with self._lock:
            if jid in self._handles:
                return {"ok": False, "reason": f"作业「{jid}」的进程已在表里，重复起进程会被拒"}
        try:
            handle = self._popen(list(argv), cwd=cwd)
        except Exception as exc:  # noqa: BLE001  起进程失败要在账本外如实说
            return {"ok": False, "reason": f"起进程失败：{type(exc).__name__}: {exc}"}
        with self._lock:
            self._handles[jid] = handle
        return {"ok": True, "jobId": jid, "description": " ".join(str(x) for x in argv)[:120]}

    def kill_fn(self, job: dict[str, Any]) -> bool:
        """给 `JobWatchdog.tick(kill_fn=…)` 的实现：返回真值才算杀掉，杀不掉抛中文理由。"""
        jid = str(job.get("jobId") or "")
        with self._lock:
            handle = self._handles.get(jid)
        if handle is None:
            raise DeliveryError(f"作业「{jid}」在本机没有进程句柄（可能随上次崩溃消失），无法替你确认它已停止")
        try:
            handle.terminate()
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"终止作业「{jid}」失败：{type(exc).__name__}: {exc}") from exc
        wait = getattr(handle, "wait", None)
        if callable(wait):
            try:
                wait(timeout=5)
            except Exception:  # noqa: BLE001  没等掉也先认已发出终止信号，报告里另有点名
                pass
        with self._lock:
            self._handles.pop(jid, None)
        return True

    def forget(self, job_id: str) -> None:
        with self._lock:
            self._handles.pop(str(job_id or ""), None)


# ── 一键装配（接线侧只剩端点与领取循环） ─────────────────────────
def build_dispatcher(
    *,
    model_call: Callable[[dict[str, Any]], Any] | None = None,
    send_fn: Callable[[str, str, str], Any] | None = None,
    goal: str = "",
    default_session_key: str = "",
    agent_slots: int = 3,
    human_extra: int = 1,
    workspace: "twr.TicketWorkspace | None" = None,
    run_command: Callable[[str], dict[str, Any]] | None = None,
    read_file: Callable[[str, int, int], dict[str, Any]] | None = None,
    list_dir: Callable[[str, int], dict[str, Any]] | None = None,
    write_file: Callable[[str, str], dict[str, Any]] | None = None,
    max_tool_rounds: int = 8,
    inbox_journal: Any = None,
    processes: "JobProcessTable | None" = None,
    execute_turn: Callable[[dict[str, Any], str, list], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """装配出一台可直接 `wake_and_run` 的 Dispatcher 与它的五个运行时。

    路径全部走 D-43 单一来源（`data_paths`），不新建第二套目录规则；后台作业表与
    看门狗共用一份 jobId，恢复注记里的「崩溃时在跑」就是这么点名的。返回值把五个件
    都交出去，接线侧（server.py 端点 / Rust 领取回路）持有它们即可，不必再拼口径。

    `inbox_journal` 默认新建一个 `TicketInbox`（D-48.6 的耐久收件箱）：投递即落盘、
    喂进会话才销账，重启补查因此不再只能靠调用方给的候选。传 `False` 显式关掉（旧行为），
    传自定义对象则用它——测试注入假箱就走这条路。

    `processes` / `execute_turn` 是给 `ticket_worker_service` 那类**接管者**留的口：
    真实工具实现要把「本轮工单目录」当 cwd 与路径根，只能在回合执行处注入，
    不能让端点去改装配好的对象（看门狗的 `kill_job` 必须指向同一张进程表，
    接管了表却不接管 `kill_job`，到点的作业就杀不掉）。"""
    if execute_turn is None and model_call is None:
        raise ValueError("build_dispatcher 至少要给一个：`model_call`（由本函数造回合）或 `execute_turn`（接管者已有回合实现）")
    journal = tir.TicketInbox() if inbox_journal is None else (None if inbox_journal is False else inbox_journal)
    registry = twr.TicketWorkerRegistry(agent_slots=agent_slots, human_extra=human_extra, workspace=workspace, inbox_journal=journal)
    outbox = tor.TicketOutbox()
    ledger = trr.ReadLedger()
    watchdog = trr.JobWatchdog()
    processes = processes if processes is not None else JobProcessTable()
    dispatcher = tdr.TicketDispatcher(
        registry=registry,
        outbox=outbox,
        ledger=ledger,
        watchdog=watchdog,
        execute_turn=execute_turn
        if execute_turn is not None
        else make_execute_turn(
            model_call,
            goal=goal,
            default_session_key=default_session_key,
            run_command=run_command,
            read_file=read_file,
            list_dir=list_dir,
            write_file=write_file,
            max_tool_rounds=max_tool_rounds,
            processes=processes,
        ),
        send_dispatch=make_send_dispatch(send_fn),
        kill_job=processes.kill_fn,
        inbox_journal=journal,
    )
    return {
        "dispatcher": dispatcher,
        "registry": registry,
        "outbox": outbox,
        "ledger": ledger,
        "watchdog": watchdog,
        "processes": processes,
        "inboxJournal": journal,
        "toolSpecs": tool_specs(),
        "systemPromptOf": lambda **kw: worker_system_prompt(**kw),
    }
