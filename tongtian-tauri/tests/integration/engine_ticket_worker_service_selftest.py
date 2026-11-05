#!/usr/bin/env python3
"""
工单执行体的进程内服务装配自测（缺陷 D-48.7）

D-48.1~D-48.6 把六个引擎件各自做成了可独立测试的运行时，但每一层的自测都自己装配自己
那一套：注册表不知道工具箱、调度器不知道模型通道、补查不知道磁盘上还有第二个装配。
`engine/ticket_worker_service.py` 是它们第一次**在一个进程里一起活着**，也是 `server.py`
路由唯一要调的那一层。本文件钉住这一层独有的口径（每条都问过「什么缺陷会让它红」）：

* **本轮工单目录真的传到了工具实现**。注入的工具实现拿的是签名字典，里面没有工单信息，
  服务靠接管 `execute_turn` 把目录挂进线程上下文——接线漏一处，写盘就落到引擎 cwd 或者
  根本落不下去。§6 用「模型下的 write_file 真的出现在**这一单的目录**里」钉它。
* **路径不出工单目录**。`..`、绝对路径、还不存在的越界路径都在 `resolve()` 之后被拒；
  没有上下文（跨轮直接调工具箱）也拒，不退回引擎 cwd。
* **命令与后台作业都在这单的目录里**。前台 `run_shell` 的 cwd、后台作业 `spawn` 的 cwd
  都取本轮上下文——否则看门狗到点杀的是「引擎目录里那条命令」。§5 用真子进程把它落到盘上
  （作业自己写下它看到的 cwd），§4 用记账的 popen 与 run_fn 把契约钉死。
* **模型没配好就在投递之前拒**。槽位占了、目录建了、唤醒压在箱里却没有模型能起回合，
  那条唤醒就只能等一次重启补查——那不是「稍后再跑」，那是把用户的话扣下来。§7 断言被拒
  之后注册表、耐久箱、工单目录三处都**没有**它的痕迹。
* **通道没接线就按失败退避**。`send_fn=False` 是显式「不发」：外发记录留在账上、错误原话
  可查，一条也不假装送达；退避到点后 `run_outbox` 真的能把它发出去（§8 两半）。
* **状态面回得出去**。所有对外方法过一遍 JSON 往返，且递归检查没有 Path / 元组 / 集合
  这类「`default=str` 会悄悄糊掉、HTTP 层会当场 500」的值——500 的时候事实早已落盘，
  用户看到的是「服务出错」，实际唤醒已经投进去了。
* **重启补查经过服务也还是那套口径**。回合卡在模型里 = 喂进会话但没落账 = 磁盘上两条
  都还 pending；同一数据根上的第二套装配**一条候选都不给**也能捞回来（§10）。
* **单例的重建只认构造期事实，模型配置走「推」不走「重建」**。数据根移动、槽位/并行口径变了
  才重建；带 `model_config` 时装配不动，配置当场推给现有通道（`modelApplied` 如实回报推没推成），
  下一轮打的就是新端点——用户在设置页改完模型不重启引擎，而重建装配要拆掉活着的 worker、槽位和
  耐久箱。§11 两种都断言，§11b 还钉「改回没配好也当场生效」：配置冻结在构造期时，面板说配好了、
  底下还在打旧端点，两边分家比多等一轮严重。

离线运行：模型是脚本化的假实现，一次 HTTP 也不发；真正动盘与起进程的只有 §3/§5/§6
（工单目录里的读写、一条打印 cwd 的真子进程）。`TONGTIANWORK_HOME` 一节一根（outbox、
已读账本、耐久收件箱都是**跨工单共享的单文件账本**，共根会让别节的条数掺进本节判定）。
纯 stdlib，不碰 `engine/server.py` 与 Rust。

用法: python tests/integration/engine_ticket_worker_service_selftest.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-service-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-0")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []
SECTION = {"n": 0}


def section(title: str) -> None:
    SECTION["n"] += 1
    print(f"\n── {SECTION['n']}. {title} " + "─" * max(0, 56 - len(title)))


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def use_home(label: str) -> Path:
    """一节一根：outbox / 已读账本 / 耐久收件箱都是跨工单共享的单文件账本。"""
    home = _TMP / f"home-{label}"
    os.environ["TONGTIANWORK_HOME"] = str(home)
    return home


import checkpoint_runtime as cr  # noqa: E402
import data_paths as dp  # noqa: E402
import ticket_inbox_runtime as tir  # noqa: E402
import ticket_model_channel as tmc  # noqa: E402
import ticket_restart_resume as trs  # noqa: E402
import ticket_turn_scheduler as tts  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402
import ticket_worker_service as tws  # noqa: E402

#: 固定的到达时刻基准：重投要带**原始**到达时间，用墙钟就没法断言

T0 = 1_760_000_000_000
M_A = "先把这条工单做完"
M_B = "补一句：记得核对目录"


class ScriptedModel:
    """按脚本回话的假模型：记下每一次请求，超出脚本长度就重复最后一条。"""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, request: dict) -> dict:
        with self.lock:
            self.requests.append(dict(request))
            index = len(self.requests)
        reply = self.replies[min(index, len(self.replies)) - 1]
        return {
            "text": str(reply.get("text") or ""),
            "calls": [dict(c) for c in (reply.get("calls") or [])],
            "outcome": reply.get("outcome", ""),
            "reason": reply.get("reason", ""),
        }

    def systems(self) -> list:
        return [str(r.get("system") or "") for r in self.requests]

    def blob(self, index: int = -1) -> str:
        return request_blob(self.requests[index] if self.requests else {})


def request_blob(request: dict) -> str:
    parts = [str(request.get("system") or "")]
    parts += [str(m.get("content") or "") for m in (request.get("messages") or [])]
    return "\n".join(parts)


class Channel:
    """假 IM 通道：按到达顺序记 (通道, 会话, 正文)，可让指定正文投递失败。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent: list = []
        self.fail: set = set()

    def send(self, channel: str, target: str, text: str) -> None:
        if text in self.fail:
            raise ta.DeliveryError(f"通道拒绝投递「{text}」（用例设定）")
        with self.lock:
            self.sent.append((channel, target, text))

    def texts(self) -> list:
        with self.lock:
            return [t for _k, _s, t in self.sent]

    def clear_fail(self) -> None:
        with self.lock:
            self.fail.clear()


def daemon_thread_factory(*, target=None, kwargs=None, name=""):
    """真线程但设成 daemon：卡在模型里的回合代表「进程被重启打断」，收尾不许吊住解释器。"""
    return threading.Thread(target=target, kwargs=kwargs, name=name, daemon=True)


class FakeRun:
    """记账的 subprocess.run：断言 shell/cwd/timeout 怎么传的；没给假结果就真跑一次。"""

    def __init__(self, *, result=None, raises=None) -> None:
        self.calls: list = []
        self._result = result
        self._raises = raises

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return subprocess.run(cmd, **kwargs)


class FakeHandle:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        return 0


def strict_json(value) -> tuple[bool, str]:
    """没有 `default=str` 的一次序列化：撞不上 HTTP 层的坑才算真回得出去。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def native_problems(value, path: str = "$") -> list:
    """递归找「不是 JSON 原生」的值：Path、元组、集合在 `default=str` 下会被悄悄糊掉。"""
    out: list = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if not isinstance(key, str):
                out.append(f"{path}: 键 {key!r} 不是字符串（{type(key).__name__}）")
            out += native_problems(sub, f"{path}.{key}")
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            out += native_problems(sub, f"{path}[{index}]")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        out.append(f"{path}: {type(value).__name__} 不是 JSON 原生类型")
    return out


# ── 1. 装配本身：六个件 + 工具箱 + 进程表是一套 ─────────────────
section("装配本身")

home1 = use_home("1")
model1 = ScriptedModel([{"text": "收口", "calls": [{"tool": "finish_work", "args": {"summary": "做完了"}}]}])
chan1 = Channel()
svc1 = tws.TicketServiceRuntime(model_call=model1, send_fn=chan1.send, goal="把这条工单做完")
check("服务起来就是完整一套（注册表/队列/已读/看门狗/收件箱/调度器/补查）",
      all(getattr(svc1, attr, None) is not None for attr in ("registry", "outbox", "ledger", "watchdog", "inbox_journal", "scheduler", "resume")),
      repr({k: type(getattr(svc1, k, None)).__name__ for k in ("registry", "outbox", "ledger", "watchdog", "inbox_journal", "scheduler", "resume")}))
check("耐久收件箱默认接上，且注册表与 dispatcher 拿同一个对象",
      isinstance(svc1.inbox_journal, tir.TicketInbox)
      and svc1.registry.inbox_journal is svc1.inbox_journal
      and svc1.dispatcher.inbox_journal is svc1.inbox_journal)
check("工具箱与注册表同一个工作区根（不同根就会把产物写到别处）", svc1.toolbox.workspace is svc1.workspace)
check("进程表是服务那一份（不是 build_dispatcher 自己新建的第二张）", svc1.parts["processes"] is svc1.processes)
check("后台作业的进程表按本轮工单目录起进程", isinstance(svc1.processes, tws.WorkspaceProcessTable))
check("补查挂在服务自己的调度器上（不是另接一台）", svc1.resume.scheduler is svc1.scheduler)
check("数据根在构造时就定下（面板说的就是这一套的根）", svc1.home == str(dp.engine_home()) and Path(svc1.home) == home1)
use_home("1b")
check("环境变量移动之后，构造期那份 home 还指着旧根（重建判定靠它）", svc1.home != str(dp.engine_home()), repr((svc1.home, str(dp.engine_home()))))
use_home("1")
check("注入模型时不建通道（自测不碰网络）", svc1.channel is None and svc1.signature["model"] == "injected", repr(svc1.signature))
check("通道接线状态进自述与面板", svc1.signature["sendWired"] is True and svc1.status()["sendWired"] is True)

# ── 2. 路径裁决：只在本工单目录里 ──────────────────────────────
section("路径裁决")

home2 = use_home("2")
box2 = tws.TicketToolbox()
ws2 = twr.TicketWorkspace()
tid2 = "tk-svc-2"
dir2, err2 = ws2.prepare(tid2, "")
check("工单目录由 prepare 建好（用例前提）", err2 is None and dir2.exists(), repr(err2))

outside, deny0 = box2.resolve("a.txt")
check("没有回合上下文时不解析路径（也不退回引擎 cwd）", outside is None and "没有工单目录上下文" in deny0, repr(deny0))
with box2.turn("tk-not-prepared"):
    check("本轮上下文挂上后目录由 path_for 现算", box2.current_ticket() == "tk-not-prepared" and box2.current_dir() == ws2.path_for("tk-not-prepared"))
with box2.turn(tid2):
    inside, deny2 = box2.resolve("notes/a.txt")
    check("相对路径落在工单目录内", deny2 == "" and str(inside).startswith(str(dir2)), repr((inside, deny2)))
    abs_in, deny3 = box2.resolve(str(dir2 / "b.txt"))
    check("绝对路径只要落在目录内就放行", deny3 == "" and abs_in == (dir2 / "b.txt").resolve(), repr((abs_in, deny3)))
    root_ok, deny8 = box2.resolve(".")
    check("工单目录自己不算越界", deny8 == "" and root_ok == dir2.resolve(), repr((root_ok, deny8)))
    _, deny4 = box2.resolve("../../outside.txt")
    check("`..` 越界被拒", "越出本工单目录" in deny4, repr(deny4))
    _, deny5 = box2.resolve(str(_TMP / "elsewhere.txt"))
    check("指向工单目录外的绝对路径被拒", "越出本工单目录" in deny5, repr(deny5))
    _, deny6 = box2.resolve(str(home2.parent / "not-created-yet.txt"))
    check("还不存在的越界路径也先拒（裁决不看存不存在）", "越出本工单目录" in deny6, repr(deny6))
    _, deny7 = box2.resolve("   ")
    check("空路径被拒且说清要什么", "路径为空" in deny7, repr(deny7))
check("turn 退出把上下文复原（不留「上一单的目录」给下一轮）", box2.current_dir() is None and box2.current_ticket() == "")
with box2.turn(tid2):
    nested_seen = []
    with box2.turn("tk-inner"):
        nested_seen.append(box2.current_ticket())
    nested_after = box2.current_ticket()
check("嵌套 turn 复原上一层（回合同一线程里再套一层不丢上下文）", nested_seen == ["tk-inner"] and nested_after == tid2, repr((nested_seen, nested_after)))

# ── 3. 读写列：真盘 + 预算 + 只在本单 ──────────────────────────
section("读、写、列的真实实现")

with box2.turn(tid2):
    written = box2.write_file("out/note.md", "第一行\n第二行\n")
    on_disk = dir2 / "out" / "note.md"
    check("write_file 真的落盘（父目录随建），路径在工单目录里",
          on_disk.exists() and on_disk.read_text(encoding="utf-8") == "第一行\n第二行\n", repr(written))
    check("写入回执带字节数（整文件覆盖，不是追加）", written.get("bytes", 0) > 0 and "整文件覆盖" in written.get("reason", ""), repr(written))
    body = "甲" * 5000
    on_disk.write_text(body, encoding="utf-8")
    page1 = box2.read_file("out/note.md", 2000, 0)
    page2 = box2.read_file("out/note.md", 4000, 2000)
    check("分页读：两页拼起来正好是原文（start 真的到了实现）",
          page1["text"] + page2["text"] == body and page2["truncated"] is False, repr({k: page1[k] for k in ("units", "start", "totalUnits")}))
    check("第一页告诉模型还剩多少、下一跳的 start 是多少", "后面还有 3000" in page1["note"] and "start=2000" in page1["note"], repr(page1["note"]))
    over = box2.read_file("out/note.md", 100000, 0)
    check("读取量超上限按上限截断并如实带话", over["units"] == ta.TEXT_READ_MAX_UNITS and "超出上限" in over["note"], repr(over["note"][:80]))
    past = box2.read_file("out/note.md", 1000, 90000)
    check("起点超过正文长度说「没有内容可给」，不给空串当成功", past["text"] == "" and "没有内容可给" in past["note"], repr(past["note"]))
    missing = box2.read_file("nope.md", 100, 0)
    check("读不存在的文件是拒绝不是空文本", missing["text"] == "" and "文件不存在" in missing["reason"], repr(missing))
    isdir = box2.read_file("out", 100, 0)
    check("读目录被指去 list_dir", "是目录" in isdir["reason"], repr(isdir))

    listed = box2.list_dir(".", 50)
    check("list_dir 隐藏 prepare 标记（那是引擎的账，不是产物）", twr.PREPARE_MARKER not in " ".join(listed["items"]), repr(listed["items"]))
    check("目录项带斜杠、子目录里的文件不会被当成同级项", "out/" in listed["items"] and "note.md" not in listed["items"], repr(listed["items"]))
    (dir2 / "c1.txt").write_text("1", encoding="utf-8")
    (dir2 / "c2.txt").write_text("2", encoding="utf-8")
    tight = box2.list_dir(".", 1)
    check("列目录条数超预算只给前 N 项并报还剩多少",
          len(tight["items"]) == 1 and tight["total"] >= 2 and f"还有 {tight['total'] - 1} 项没列" in tight["note"], repr(tight))
    empty_path_list = box2.list_dir("", 10)
    check("list_dir 不给路径时就是列本单目录（read/write 不给路径才拒）", empty_path_list.get("items") and "reason" not in empty_path_list, repr(empty_path_list))
    big = box2.write_file("huge.md", "x" * (tws.MAX_WRITE_CHARS + 1))
    check("超单次写入上限的正文一个字节也不写", "一个字节也没写" in big["reason"] and not (dir2 / "huge.md").exists(), repr(big["reason"][:80]))
    check("超上限那次没动已存在的文件", on_disk.read_text(encoding="utf-8") == body)
    onto_dir = box2.write_file("out", "x")
    check("不往已存在的目录上写文件", "是已存在的目录" in onto_dir["reason"], repr(onto_dir))
    escaped = box2.write_file("../逃逸.md", "x")
    check("写也被路径裁决挡在目录内", "越出本工单目录" in escaped["reason"] and not (_TMP / "逃逸.md").exists(), repr(escaped["reason"][:80]))
with box2.turn("tk-no-context-dir"):
    cross = box2.read_file("out/note.md", 100, 0)
check("跨单调用读不到上一单的正文（上下文换一个目录就是换一个目录）",
      cross["text"] == "" and ("文件不存在" in cross["reason"] or "没有工单目录上下文" in cross["reason"]), repr(cross["reason"][:120]))

# ── 4. run_command：真实 cwd、guard、超时、上限、解码 ──────────
section("run_shell 的真实执行")

probe = dir2 / "cwd_probe.py"
probe.write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
real_run = FakeRun()
box4 = tws.TicketToolbox(run_fn=real_run)
with box4.turn(tid2):
    got = box4.run_command(f'"{sys.executable}" "{probe}"')
check("命令真的跑在本工单目录里（子进程自己打印的 cwd 就是它）",
      os.path.normcase(got["output"].strip()) == os.path.normcase(str(dir2)), repr(got)[:300])
check("回执带 returnCode 与 cwd（端点要能原样回）", got["returnCode"] == 0 and got["cwd"] == str(dir2), repr(got))
check("run_fn 收到的是 shell=True + 本单 cwd + 默认超时",
      len(real_run.calls) == 1 and real_run.calls[0]["shell"] is True
      and real_run.calls[0]["cwd"] == str(dir2) and real_run.calls[0]["timeout"] == tws.DEFAULT_SHELL_TIMEOUT_SECONDS,
      repr(real_run.calls[:1]))
sleepy = FakeRun()
box5 = tws.TicketToolbox(run_fn=sleepy)
with box5.turn(tid2):
    denied = box5.run_command("sleep 30")
    empty = box5.run_command("   ")
check("睡等命令在工具箱这层也拦得住（接线方直接调 toolbox 时不放过）", "sleep" in denied["reason"] and sleepy.calls == [], repr(denied))
check("空命令不执行也不起进程", "命令为空" in empty["reason"] and sleepy.calls == [], repr(empty))
norefresh = box5.run_command("echo hi")
check("没有工单目录上下文时命令一条也不跑", "没有工单目录上下文" in norefresh["reason"] and sleepy.calls == [], repr(norefresh["reason"][:80]))

timeout_run = FakeRun(raises=subprocess.TimeoutExpired(cmd="long.sh", timeout=1))
box6 = tws.TicketToolbox(run_fn=timeout_run)
with box6.turn(tid2):
    late = box6.run_command("long.sh")
check("超时不报「已杀掉」：点名孙进程可能还活着 + 指路 background",
      "超过" in late["reason"] and "孙进程可能还活着" in late["reason"] and "background=true" in late["reason"], repr(late["reason"]))
ghost_box = tws.TicketToolbox(run_fn=FakeRun())
with ghost_box.turn("tk-dir-missing"):
    ghost = ghost_box.run_command("echo hi")
check("工单目录还没建出来就不跑命令", "还不在这台机器上" in ghost["reason"], repr(ghost["reason"][:80]))

noise = FakeRun(result=subprocess.CompletedProcess(args="x", returncode=0, stdout=("y" * 9000).encode(), stderr=b""))
box7 = tws.TicketToolbox(run_fn=noise)
with box7.turn(tid2):
    capped = box7.run_command("noisy.sh")
check("输出超上限只给前一段并报还剩多少", len(capped["output"]) < 9000 and "后面还有" in capped["output"], repr(capped["output"][-80:]))
merged_run = FakeRun(result=subprocess.CompletedProcess(args="x", returncode=7, stdout=b"out", stderr=b"err happened"))
box8 = tws.TicketToolbox(run_fn=merged_run)
with box8.turn(tid2):
    both = box8.run_command("mixed.sh")
check("stderr 单独分段，非零退出码写进回执",
      "[stderr]" in both["output"] and "err happened" in both["output"] and "退出码 7" in both["output"], repr(both["output"][:200]))
gbk_run = FakeRun(result=subprocess.CompletedProcess(args="x", returncode=0, stdout="中文输出".encode("gbk"), stderr=b""))
box9 = tws.TicketToolbox(run_fn=gbk_run)
with box9.turn(tid2):
    cn = box9.run_command("gbk.sh")
check("chcp 936 的输出解码成中文（不是假失败也不是乱码）", "中文输出" in cn["output"] and "\ufffd" not in cn["output"], repr(cn["output"]))

# ── 5. 后台作业的 cwd：契约 + 真进程 ───────────────────────────
section("后台作业起在谁的目录里")

popen_calls: list = []
handles: list = []


def fake_popen(argv, cwd=None):
    popen_calls.append({"argv": list(argv), "cwd": cwd})
    handle = FakeHandle()
    handles.append(handle)
    return handle


table = tws.WorkspaceProcessTable(toolbox=box2, popen=fake_popen)
spawned_out = table.spawn("job-out", ["prog", "arg"])
check("本轮上下文之外不起带目录的进程（退回父类行为）", popen_calls[-1]["cwd"] is None and spawned_out["ok"] is True, repr(popen_calls[-1]))
with box2.turn(tid2):
    spawned_in = table.spawn("job-in", ["prog", "arg"])
check("后台作业的 cwd = 本轮工单目录（看门狗杀的是这一单的进程）", spawned_in["ok"] and popen_calls[-1]["cwd"] == str(dir2), repr(popen_calls[-1]))
dup = table.spawn("job-in", ["prog", "arg"])
check("同 jobId 重复起进程仍被拒（父类口径没被覆盖掉）", dup["ok"] is False and "已在表里" in dup["reason"], repr(dup))
check("kill_fn 交回真实句柄：terminate 被调到", table.kill_fn({"jobId": "job-in"}) is True and handles[-1].terminated)

# 真子进程版：作业自己把看到的 cwd 写进盘
home5 = use_home("5")
tid5 = "tk-svc-5"
ws5 = twr.TicketWorkspace()
dir5, err5 = ws5.prepare(tid5, "")
check("为后台作业准备好这一单的目录（用例前提）", err5 is None and dir5.exists(), repr(err5))
(dir5 / "job_cwd.py").write_text(
    "import os, threading\n"
    "open('job-cwd.txt', 'w', encoding='utf-8').write(os.getcwd())\n"
    "threading.Event().wait()\n",
    encoding="utf-8",
)
py = sys.executable.replace("\\", "/")
job_cmd = f"{py} job_cwd.py"
model5 = ScriptedModel([
    {"text": "起个长驻的", "calls": [{"tool": "run_shell", "args": {"command": job_cmd, "background": True}}]},
    {"text": "还没完", "calls": []},
])
chan5 = Channel()
svc5 = tws.TicketServiceRuntime(model_call=model5, send_fn=chan5.send, thread_factory=tts.inline_thread_factory)
submit5 = svc5.submit(ticket_id=tid5, message=M_A, assignment_id="as-job-1", arrived_at_ms=T0 + 10)
cwd_file = dir5 / "job-cwd.txt"
deadline = time.time() + 15.0
while time.time() < deadline and not cwd_file.exists():
    time.sleep(0.1)
check("真子进程在这一单的目录里跑起来了（服务把这单的 cwd 交给了作业）", cwd_file.exists(), repr(submit5)[:300])
if cwd_file.exists():
    check("它写下的 cwd 就是这一单的目录，不是引擎目录",
          os.path.normcase(cwd_file.read_text(encoding="utf-8")) == os.path.normcase(str(dir5)), repr(cwd_file.read_text(encoding="utf-8")))
running5 = svc5.watchdog.running_for()
check("作业登记在表上（回合没收口，槽位因此不还）", len(running5) == 1 and running5[0].get("ticketId") == tid5, repr(running5))
check("有作业在跑时这单对外仍是 working", svc5.state(tid5)["worker"]["external"] == "working", repr(svc5.state(tid5)["worker"]))
job_id5 = str(running5[0]["jobId"]) if running5 else ""
down5 = svc5.shutdown(timeout_seconds=1.0)
check("关停报告点名还在跑的后台作业（不假装已经停干净）",
      any("job_cwd.py" in line for line in down5["runningJobs"]), repr(down5["runningJobs"]))
killed5 = svc5.processes.kill_fn({"jobId": job_id5}) if job_id5 else False
check("接线侧能用同一张表终止这条作业", killed5 is True)
try:
    svc5.processes.kill_fn({"jobId": job_id5})
    second_kill = "没抛"
except ta.DeliveryError as exc:
    second_kill = str(exc)
check("句柄已交回：二次终止如实抛错，不假称杀干净", "没有进程句柄" in second_kill, repr(second_kill))

# ── 6. submit → 真回合：产物落盘、外发提交、检查点、销账 ────────
section("一次真实回合跑完整套")

home6 = use_home("6")
tid6 = "tk-svc-6"
model6 = ScriptedModel([
    {"text": "先写产物", "calls": [{"tool": "write_file", "args": {"path": "out/note.md", "content": "工单产物一行"}}]},
    {"text": "汇报", "calls": [
        {"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C9", "text": "已完成：产物在 out/note.md", "dedupeKey": "k-6-1"}},
        {"tool": "finish_work", "args": {"summary": "写完并汇报"}},
    ]},
])
chan6 = Channel()
svc6 = tws.TicketServiceRuntime(
    model_call=model6,
    send_fn=chan6.send,
    goal="把这条工单做完",
    default_session_key="slack:C9",
    thread_factory=tts.inline_thread_factory,
)
res6 = svc6.submit(ticket_id=tid6, message=M_A, assignment_id="as-6", title="服务单", arrived_at_ms=T0 + 20)
ticket_dir6 = svc6.workspace.path_for(tid6)
check("投递后起了一轮并收口", res6.get("action") == "turn-scheduled" and svc6.registry.worker(tid6).ended_status == "finished", repr(res6))
check("模型的 write_file 落在**这一单**的目录里（本轮上下文真的传到了实现）",
      (ticket_dir6 / "out" / "note.md").exists() and (ticket_dir6 / "out" / "note.md").read_text(encoding="utf-8") == "工单产物一行",
      repr(sorted(p.name for p in ticket_dir6.iterdir())))
check("两次模型往返：第一次的工具回执在第二次的对话里", len(model6.requests) == 2 and "已记入副作用账本" in model6.blob(-1), repr(model6.blob(-1)[-300:]))
check("系统提示词里的「工作目录」是这一单的目录名", f"工作目录：{ticket_dir6.name}" in model6.systems()[0], repr(model6.systems()[0][:200]))
check("工具面就是适配器那 6 个（不是通道另写一份名字表）", model6.requests[0]["tools"] == list(ta.WORKER_TOOLS), repr(model6.requests[0]["tools"]))
check("外发真的到了通道（一条，正文就是模型那句）", chan6.texts() == ["已完成：产物在 out/note.md"], repr(chan6.texts()))
snap6 = svc6.outbox.snapshot()
check("副作用账本里那条是 done", snap6["statusCounts"].get("done") == 1 and snap6["records"] == 1, repr(snap6["statusCounts"]))
check("收口（finished）之后这份领取的检查点按口径删除（不给死键留一堆上下文）",
      cr.load_checkpoint(twr.checkpoint_task_id("as-6")) is None, repr(cr.load_checkpoint(twr.checkpoint_task_id("as-6"))))
check("回合喂进会话后耐久箱销账（盘上不留已消费的）", svc6.inbox_state(tid6)["inbox"]["records"] == 0, repr(svc6.inbox_state(tid6)))
check("状态面说得出这单已收口、没有回合在飞",
      svc6.state(tid6)["worker"]["endedStatus"] == "finished" and svc6.state(tid6)["scheduler"]["inFlight"] is False, repr(svc6.state(tid6)))
hist6 = svc6.history(tid6)
check("历史里有一轮且带 ticketId", hist6["count"] == 1 and hist6["items"][0]["ticketId"] == tid6, repr(hist6))
check("服务面板把跑过的轮数与活着的 worker 数说出来", svc6.status()["scheduler"]["turnsRun"] == 1 and svc6.status()["workers"]["live"] == 0, repr(svc6.status()["workers"]))
again6 = svc6.submit(ticket_id=tid6, message=M_B, assignment_id="as-6b", prev_assignment_id="as-6", arrived_at_ms=T0 + 21)
check("收口之后新领取仍能起轮（不是「这单结束了就拒」）", again6.get("action") == "turn-scheduled" and len(model6.requests) >= 3, repr(again6))
check("旧领取没有检查点时恢复注记如实说「借不到上下文」（不假装有）",
      "没有可用的旧检查点" in model6.blob(-1) and M_B in model6.blob(-1), repr(model6.blob(-1)[-400:]))

# 没收口的那一轮：检查点留住，下一轮接得上对话
home6c = use_home("6c")
tid6c = "tk-svc-6c"
model6c = ScriptedModel([{"text": "先跑一半", "calls": []}, {"text": "接着跑", "calls": []}])
svc6c = tws.TicketServiceRuntime(model_call=model6c, send_fn=Channel().send, thread_factory=tts.inline_thread_factory)
first6c = svc6c.submit(ticket_id=tid6c, message=M_A, assignment_id="as-6c", arrived_at_ms=T0 + 22)
point6c = cr.load_checkpoint(twr.checkpoint_task_id("as-6c"))
check("没收口的回合把检查点留住（下一轮的上下文靠它）", (point6c or {}).get("ticketId") == tid6c, repr(first6c))
check("没收口时这单对外不是 finished（worker 仍活着、槽位仍占）",
      svc6c.registry.worker(tid6c).ended_status == "" and svc6c.state(tid6c)["worker"]["external"] in ("working", "waiting"),
      repr(svc6c.state(tid6c)["worker"]))
second6c = svc6c.submit(ticket_id=tid6c, message=M_B, assignment_id="as-6c", arrived_at_ms=T0 + 23)
check("同一份领取的下一轮接上了上一轮对话（旧正文在新请求里）",
      "先跑一半" in model6c.blob(-1) and M_B in model6c.blob(-1), repr({k: second6c.get(k) for k in ("action", "delivered")}))
check("续跑轮不再重复投同一条唤醒（内存箱只压一条）", svc6c.registry.worker(tid6c).snapshot()["queuedWakes"] == 0, repr(svc6c.registry.worker(tid6c).snapshot()))

# ── 7. 模型没配好：投递之前就拒 ────────────────────────────────
section("模型通道没配置")

home7 = use_home("7")
_saved = {k: os.environ.pop(k, None) for k in ("TT_API_KEY", "TT_BASE_URL", "TT_MODEL", "TT_PROVIDER")}
tid7 = "tk-svc-7"
svc7 = tws.TicketServiceRuntime(model_config={}, send_fn=False)
check("没有任何模型配置时通道判为 none（不是退回假端点）", svc7.channel is not None and svc7.channel.ready() is False, repr(svc7.channel.describe()))
refused7 = svc7.submit(ticket_id=tid7, message=M_A, assignment_id="as-7", arrived_at_ms=T0 + 30)
check("submit 在投递之前就拒（action=rejected + stage=model-channel）",
      refused7["ok"] is False and refused7["action"] == "rejected" and refused7["stage"] == "model-channel", repr(refused7))
check("理由把「没配模型」和「这条没替你记住」都说清",
      "模型通道还没配置" in refused7["reason"] and "没有可用模型" in refused7["reason"] and "没有替你记住" in refused7["reason"], repr(refused7["reason"]))
check("被拒的唤醒在注册表里没有痕迹（没占槽、没建 worker）",
      svc7.registry.worker(tid7) is None and svc7.registry.pool.snapshot()["occupied"] == {}, repr(svc7.registry.pool.snapshot()))
check("被拒的唤醒也没进耐久箱（磁盘上没有一条等着重启补查）", svc7.inbox_state(tid7)["inbox"]["records"] == 0, repr(svc7.inbox_state(tid7)))
check("被拒的唤醒没建工单目录", not svc7.workspace.path_for(tid7).exists(), repr(str(svc7.workspace.path_for(tid7))))
check("状态面板如实报 modelReady=False 并带上原因",
      svc7.status()["modelReady"] is False and "没有可用模型" in svc7.status()["model"]["reason"], repr(svc7.status()["model"]))
try:
    svc7.channel({"system": "", "messages": [{"role": "user", "content": "hi"}]})
    raised7 = ""
except tmc.ModelChannelError as exc:
    raised7 = str(exc)
check("绕过 submit 直接起回合也照样不起（通道自己拒，不带半截状态外发）", "没有可用模型" in raised7 or "模型通道没有可用配置" in raised7, repr(raised7))
for key, value in _saved.items():
    if value is not None:
        os.environ[key] = value
env_model = tws.TicketServiceRuntime(model_config={"baseUrl": "https://example.invalid/v1", "apiKey": "sk-test", "modelName": "m-1"}, send_fn=False)
check("配了 baseUrl 之后同一份装配判为 custom 且模型名跟着走",
      env_model.channel.ready() and env_model.signature["model"] == "custom" and env_model.signature["modelName"] == "m-1", repr(env_model.signature))
keyed = env_model.status()
check("面板与自述里都不带密钥（sk-test 一次也没出现）", "sk-test" not in json.dumps(keyed, ensure_ascii=False), repr(json.dumps(keyed, ensure_ascii=False)[:260]))

# ── 8. 通道没接线 / 退避之后 run_outbox 真能发 ─────────────────
section("外发通道：没接线就如实失败")

home8 = use_home("8")
tid8 = "tk-svc-8"
model8 = ScriptedModel([
    {"text": "回一句", "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C8", "text": "这条还没通道可发"}}]},
    {"text": "本轮先这样", "calls": []},
])
svc8 = tws.TicketServiceRuntime(model_call=model8, send_fn=False, thread_factory=tts.inline_thread_factory)
svc8.outbox.base_backoff_seconds = 0.0  # 用例要把「退避到点重投」当场跑完，不等真 5 秒
res8 = svc8.submit(ticket_id=tid8, message=M_A, assignment_id="as-8", arrived_at_ms=T0 + 40)
group8 = f"{tid8}::slack:C8"
snap8 = svc8.outbox.snapshot()
check("send_fn=False 是显式「通道没接」，自述与面板都这么报", svc8.send_fn is None and svc8.status()["sendWired"] is False)
check("回合本身照常跑完、唤醒照常消费", res8.get("action") == "turn-scheduled" and svc8.inbox_state(tid8)["inbox"]["records"] == 0, repr(res8))
check("外发记录留在账上且写明通道未接线", snap8["records"] == 1 and "IM 通道尚未接线" in snap8["orderedGroups"][group8][0]["error"], repr(snap8["orderedGroups"][group8][0]))
check("一条也没假装送达（done 计数为空）", snap8["statusCounts"].get("done") is None, repr(snap8["statusCounts"]))
retry8 = svc8.run_outbox()
check("run_outbox 到点再投：仍失败就继续挂着（不是悄悄丢）",
      retry8["dispatched"] == 1 and retry8["failed"] == 1 and svc8.outbox.pending_count() == 1, repr(retry8))
check("重试的原话还是「通道未接线」", "IM 通道尚未接线" in svc8.outbox.snapshot()["orderedGroups"][group8][0]["error"])
check("放行不指名就拒", svc8.resolve_outbox_block("", "skip")["ok"] is False and "record_id 为空" in svc8.resolve_outbox_block("", "skip")["reason"], repr(svc8.resolve_outbox_block("", "skip")))

home8b = use_home("8b")
tid8b = "tk-svc-8b"
model8b = ScriptedModel([
    {"text": "回一句", "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C8", "text": "先失败后成功"}}]},
    {"text": "本轮先这样", "calls": []},
])
chan8b = Channel()
chan8b.fail.add("先失败后成功")
svc8b = tws.TicketServiceRuntime(model_call=model8b, send_fn=chan8b.send, thread_factory=tts.inline_thread_factory)
svc8b.outbox.base_backoff_seconds = 0.0
svc8b.submit(ticket_id=tid8b, message=M_A, assignment_id="as-8b", arrived_at_ms=T0 + 50)
check("通道当场拒投时记录按失败退避挂着", chan8b.texts() == [] and svc8b.outbox.pending_count() == 1, repr(svc8b.outbox.snapshot()["statusCounts"]))
chan8b.clear_fail()
retry8b = svc8b.run_outbox()
check("周期驱动的 run_outbox 把退避到点的那条真发出去了", retry8b["succeeded"] == 1 and chan8b.texts() == ["先失败后成功"], repr(retry8b))
check("发出去之后账上那条是 done、待投计数归零",
      svc8b.outbox.snapshot()["statusCounts"].get("done") == 1 and svc8b.outbox.pending_count() == 0, repr(svc8b.outbox.snapshot()["statusCounts"]))
state8b = svc8b.outbox_state()
check("outbox_state 把队列与待投数一起报出来", state8b["pendingNow"] == 0 and state8b["outbox"]["statusCounts"]["done"] == 1, repr(state8b))
not_found = svc8b.retry_outbox("ob-not-exist")
check("重投不存在的记录如实报", not_found.get("ok") is False and "找不到" in json.dumps(not_found, ensure_ascii=False), repr(not_found))
check("重投空 id 也拒", svc8b.retry_outbox("")["ok"] is False and "record_id 为空" in svc8b.retry_outbox("")["reason"], repr(svc8b.retry_outbox("")))

# ── 9. 状态面 JSON 原生 + 限量语义 ─────────────────────────────
section("端点面回得出去")

for label, value in (
    ("status()", svc6.status()),
    ("state()", svc6.state(tid6)),
    ("outbox_state()", svc6.outbox_state()),
    ("inbox_state()", svc6.inbox_state(tid6)),
    ("history()", svc6.history()),
    ("plan()", svc6.plan(tid6, prev_assignment_id="as-6")),
    ("status()（注入通道）", svc8b.status()),
):
    ok_strict, err = strict_json(value)
    problems = native_problems(value)
    check(f"{label} 严格 JSON 可序列化（不靠 default=str 糊）", ok_strict, err)
    check(f"{label} 递归里没有 Path/元组/集合", problems == [], repr(problems[:4]))
    check(f"{label} 过 json_ready 之后原样不变", tws.json_ready(value) == value, repr(str(tws.json_ready(value))[:160]))
check("没接耐久箱时 inbox_state 说得清为什么", (svc_off := tws.TicketServiceRuntime(model_call=ScriptedModel([{"calls": []}]), send_fn=False, inbox_journal=False)).inbox_state("")["enabled"] is False and "只能靠调用方候选" in svc_off.inbox_state("")["reason"], repr(svc_off.inbox_state("")))
hist_all = svc8b.history()
check("history 的 limit 只砍返回条数、不改总数", hist_all["count"] == len(hist_all["items"]) and len(svc8b.history(limit=1)["items"]) == 1 and svc8b.history(limit=1)["count"] == hist_all["count"], repr(svc8b.history(limit=1)))
check("limit 传 0 走默认面（不回空列表）", svc8b.history(limit=0)["returned"] == min(hist_all["count"], tws.DEFAULT_HISTORY_LIMIT), repr(svc8b.history(limit=0)["returned"]))
check("history 指名单时只回这单的轮", all(item["ticketId"] == tid8b for item in svc8b.history(tid8b)["items"]) and svc8b.history(tid8b)["count"] >= 1, repr(svc8b.history(tid8b)["count"]))

# ── 10. 重启补查：卡在模型里的回合 + 第二套装配 ────────────────
section("重启补查经过服务这一层")

home10 = use_home("10")
tid10 = "tk-svc-10"
release = threading.Event()  # 永不 set：那条回合代表「进程被打断」，收尾不许它活过来重写盘
seen10: list = []
lock10 = threading.Lock()


def hung_model(request: dict) -> dict:
    with lock10:
        seen10.append(dict(request))
    release.wait(timeout=3600.0)
    return {"text": "卡住之后才回", "calls": []}


svc10 = tws.TicketServiceRuntime(
    model_call=hung_model,
    send_fn=Channel().send,
    thread_factory=daemon_thread_factory,
    max_parallel_turns=2,
)
first10 = svc10.submit(ticket_id=tid10, message=M_A, assignment_id="as-10-old", title="卡住的单", arrived_at_ms=T0 + 60)
check("第一条唤醒抢到了这一单的单飞位", first10.get("action") == "turn-scheduled", repr(first10))
deadline = time.time() + 5.0
while time.time() < deadline and not seen10:
    time.sleep(0.02)
check("回合确实进了模型并卡在这里（喂进会话但还没落账）", len(seen10) == 1 and M_A in request_blob(seen10[0]), repr(len(seen10)))
second10 = svc10.submit(ticket_id=tid10, message=M_B, assignment_id="as-10-old", arrived_at_ms=T0 + 61)
check("第二条只投递不起第二轮（在飞那轮的单飞位没被抢）", second10.get("action") == "deliver-only", repr(second10))
check("状态面说得出这一单有人在飞、内存箱压着第二条",
      svc10.state(tid10)["scheduler"]["inFlight"] is True and svc10.state(tid10)["worker"]["queuedWakes"] == 1, repr(svc10.state(tid10)))
pending10 = svc10.inbox_state(tid10)["inbox"]
check("磁盘上两条都还挂着：喂进会话没落账的那条 + 压在箱里的那条", pending10["records"] == 2, repr(pending10))
check("dispatcher 的状态面也报得出在箱条数（重启前就看得见缺口）", svc10.state(tid10)["worker"]["journalPending"] == 2, repr(svc10.state(tid10)["worker"]))

# 第二套装配 = 同一数据根上的重启（内存件全新，磁盘件从盘重读）
model10b = ScriptedModel([{"text": "接着做", "calls": [{"tool": "finish_work", "args": {"summary": "补齐两条唤醒后收口"}}]}])
chan10b = Channel()
svc10b = tws.TicketServiceRuntime(model_call=model10b, send_fn=chan10b.send, thread_factory=tts.inline_thread_factory)
check("重启后的装配拿到同一份耐久日志（路径同一来源）", str(svc10b.inbox_journal.path) == str(svc10.inbox_journal.path))
check("新装配的注册表是空的（内存件随进程死了）", svc10b.registry.worker(tid10) is None)
plan10 = svc10b.plan(tid10, prev_assignment_id="as-10-old", candidate_wakes=[], title="卡住的单")
check("**一条候选都不给**，服务面也捞得回这两条未读",
      plan10["ok"] and [w["text"] for w in plan10["unread"]] == [M_A, M_B], repr({k: plan10.get(k) for k in ("action", "unread", "reason")}))
check("证据分清「从盘捞回的」与「磁盘在箱总数」",
      plan10["evidence"]["journalSourced"] == 2 and plan10["evidence"]["journalPending"] == 2, repr(plan10["evidence"]))
check("注记点名旧领取没有落账（分不清没读过与没落笔）", any("一条落账都没有" in n for n in plan10["notes"]), repr(plan10["notes"]))
check("注记点名旧领取没有检查点（上下文借不到）", any("没有留下可读的检查点" in n for n in plan10["notes"]), repr(plan10["notes"]))
check("plan 只读不动盘", svc10b.inbox_state(tid10)["inbox"]["records"] == 2, repr(svc10b.inbox_state(tid10)))
resume10 = svc10b.resume_wakes(tid10, prev_assignment_id="as-10-old", new_assignment_id="as-10-new", candidate_wakes=[], title="卡住的单")
check("补查一次带走：两条重投给新领取并起一轮",
      resume10["ok"] and resume10["action"] == "resumed" and resume10["redelivered"] == 2, repr(resume10))
check("两条唤醒都进了新那轮的提示词（原始正文原样）", M_A in model10b.blob(0) and M_B in model10b.blob(0), repr(model10b.blob(0)[-400:]))
check("重投落到新领取后被消费并销账（盘上归零）", svc10b.inbox_state(tid10)["inbox"]["records"] == 0, repr(svc10b.inbox_state(tid10)))
plan_again = svc10b.plan(tid10, prev_assignment_id="as-10-old", candidate_wakes=[], title="卡住的单")
check("再重启一次不重投同一句话（跨领取凭据认得出）", plan_again["action"] == "nothing-to-resume", repr({k: plan_again.get(k) for k in ("action", "reason")}))
check("补查这一路的服务面板也回得出去", native_problems(svc10b.status()) == [] and strict_json(svc10b.status())[0], repr(native_problems(svc10b.status())[:3]))
check("补查这一轮确实跑完并记进服务面板（跑完收口，检查点按口径删除）",
      svc10b.status()["scheduler"]["turnsRun"] == 1
      and svc10b.state(tid10)["worker"]["endedStatus"] == "finished"
      and cr.load_checkpoint(twr.checkpoint_task_id("as-10-new")) is None,
      repr((svc10b.status()["scheduler"], cr.load_checkpoint(twr.checkpoint_task_id("as-10-new")))))
check("收口只 finish_work 不会自动外发一条（外发只能由模型点名）", chan10b.texts() == [] and svc10b.outbox.snapshot()["records"] == 0, repr(svc10b.outbox.snapshot()["statusCounts"]))

# ── 11. 单例：configure / get_runtime / reset_runtime ──────────
section("进程内单例")

home11 = use_home("11")
check("起手没有装配", tws.reset_runtime()["existed"] is False)
tws.configure(
    model_call=ScriptedModel([{"text": "收口", "calls": [{"tool": "finish_work", "args": {"summary": "单例收口"}}]}]),
    send_fn=Channel().send,
    agent_slots=2,
    human_extra=1,
    goal="单例这轮",
    thread_factory=tts.inline_thread_factory,
)
first11 = tws.get_runtime()
same11 = tws.get_runtime(agent_slots=99)
check("两次 get_runtime 拿到同一套装配（不是一人一套）", first11 is same11)
check("建好之后 kwargs 改不动口径（要改走 configure）", same11.signature["agentSlots"] == 2, repr(same11.signature))
check("configure 记下的口径在建装配时用上了", first11.goal == "单例这轮" and first11.signature["agentSlots"] == 2, repr(first11.signature))
model_cfg_only = tws.configure(model_config={"baseUrl": "https://example.invalid/v1", "apiKey": "sk-keep"})
check("只改模型配置不重建（令牌每轮现取）", model_cfg_only["rebuilt"] is False and tws.get_runtime() is first11, repr(model_cfg_only))
check("注入模型的装配没有通道可推，回报如实说没推成（不假装生效）",
      model_cfg_only["modelApplied"].get("applied") is False and "注入" in str(model_cfg_only["modelApplied"].get("reason")),
      repr(model_cfg_only.get("modelApplied")))
use_home("11b")
moved = tws.configure(goal="数据根换了之后这一句才生效")
check("数据根移动触发重建并说明为什么", moved["rebuilt"] is True and moved["shutdown"].get("why") == "数据根换了", repr(moved))
after_move = tws.get_runtime()
check("重建后的装配落在新根上并吃到新口径",
      after_move is not first11 and after_move.home == str(dp.engine_home()) and after_move.goal == "数据根换了之后这一句才生效",
      repr((after_move.home, after_move.goal)))
slots = tws.configure(agent_slots=5)
check("并发口径变了也重建（构造期事实改不了就得重来）", slots["rebuilt"] is True and slots["shutdown"].get("why") == "并发口径变了", repr(slots))
check("重建后的装配用上新槽位数", tws.get_runtime().signature["agentSlots"] == 5, repr(tws.get_runtime().signature))
status11 = tws.runtime_status()
check("runtime_status 一行拿到自述且落在当前根", status11["ok"] is True and status11["home"] == str(dp.engine_home()), repr(status11["home"]))
check("单例这套装配的密钥也不外泄", "sk-keep" not in json.dumps(status11, ensure_ascii=False), repr(json.dumps(status11, ensure_ascii=False)[:200]))
report11 = tws.reset_runtime()
check("reset_runtime 交回关停报告并清掉单例", report11["existed"] is True and report11["ok"] is True, repr(report11))
check("再 reset 一次如实说没有装配", tws.reset_runtime()["existed"] is False)

# 11b. 模型配置改动：不重建装配，但要当场推给通道（用户改完设置不重启引擎）
home11c = use_home("11c")
tws.configure(
    model_config={"baseUrl": "https://first.invalid/v1", "apiKey": "sk-first", "modelName": "m-first"},
    send_fn=False,
    goal="推模型配置这一路",
    thread_factory=tts.inline_thread_factory,
)
chan_rt = tws.get_runtime()
check("带模型配置建的装配走的是真通道（不是注入的 model_call）",
      chan_rt.channel is not None and chan_rt.channel.describe()["host"] == "first.invalid"
      and chan_rt.signature["model"] == "custom" and chan_rt.signature["modelName"] == "m-first",
      repr(chan_rt.signature))
pushed = tws.configure(model_config={"baseUrl": "https://other.invalid/v1", "apiKey": "sk-push", "modelName": "m-push"})
check("换模型配置不重建装配（活 worker、槽位、耐久箱不为此拆掉）",
      pushed["rebuilt"] is False and pushed["shutdown"] == {} and tws.get_runtime() is chan_rt, repr(pushed))
check("回报里说清了推没推成、配好没配好",
      pushed["modelApplied"].get("applied") is True and pushed["modelApplied"].get("modelReady") is True,
      repr(pushed.get("modelApplied")))
check("下一轮打的就是新端点：通道、状态面、签名三处口径一致",
      chan_rt.channel.describe()["host"] == "other.invalid"
      and chan_rt.channel.resolve()["baseUrl"] == "https://other.invalid/v1"
      and chan_rt.status()["model"]["host"] == "other.invalid"
      and chan_rt.signature["modelName"] == "m-push",
      repr((chan_rt.channel.describe(), chan_rt.signature["modelName"])))
check("推送后的面板仍然不带密钥", "sk-push" not in json.dumps(chan_rt.status(), ensure_ascii=False), repr(json.dumps(chan_rt.status()["model"], ensure_ascii=False)))
turned_off = tws.configure(model_config={})
check("改回「没配好」也当场生效（ready 跟着翻，不留构造期那份判定）",
      turned_off["modelApplied"].get("applied") is True and turned_off["modelApplied"].get("modelReady") is False
      and chan_rt.channel.ready() is False and chan_rt.status()["modelReady"] is False,
      repr(turned_off.get("modelApplied")))
rejected_after = chan_rt.submit(ticket_id="tk-svc-11d", message=M_A, assignment_id="as-11d")
check("配置改成没配好之后，唤醒在投递之前就被拒（不落盘、不起轮）",
      rejected_after.get("ok") is False and rejected_after.get("stage") == "model-channel", repr(rejected_after))
check("被拒那条没在盘上留痕（注册表与耐久箱都干净）",
      chan_rt.registry.worker("tk-svc-11d") is None and chan_rt.inbox_state("tk-svc-11d")["inbox"]["records"] == 0,
      repr(chan_rt.inbox_state("tk-svc-11d")))
check("11b 这一路跑完把单例收回（不给下一节留一套模型没配好的装配）",
      tws.reset_runtime()["existed"] is True and tws.reset_runtime()["existed"] is False)

# ── 12. 端点面的拒绝口径（空参数一律中文理由）──────────────────
section("拒绝面")

home12 = use_home("12")
svc12 = tws.TicketServiceRuntime(model_call=ScriptedModel([{"text": "不用", "calls": []}]), send_fn=False, thread_factory=tts.inline_thread_factory)
faces = [
    ("state（空 ticket_id）", svc12.state(""), "ticket_id 为空"),
    ("kick（空 ticket_id）", svc12.kick(""), "ticket_id 为空"),
    ("plan（空 ticket_id）", svc12.plan("", prev_assignment_id="as-x"), "need-ticket"),
    ("plan（缺 prev）", svc12.plan("tk-12", prev_assignment_id=""), "need-prev"),
    ("resume（缺新领取 id）", svc12.resume_wakes("tk-12", prev_assignment_id="as-a", new_assignment_id=""), "need-new-assignment"),
    ("retry_outbox（空 id）", svc12.retry_outbox(""), "record_id 为空"),
    ("resolve_outbox_block（空 id）", svc12.resolve_outbox_block("", "send"), "record_id 为空"),
    ("submit（空正文）", svc12.submit(ticket_id="tk-12", message="", assignment_id="as-12"), "reason"),
]
for label, value, needle in faces:
    blob = json.dumps(value, ensure_ascii=False)
    check(f"{label} 拒得下来且说得清", value.get("ok") is False and needle in blob, repr(blob)[:220])
    check(f"{label} 的答复回得出去", strict_json(value)[0] and native_problems(value) == [], repr(native_problems(value)[:2]))
check("空参数没有留下半个 worker", svc12.registry.worker("tk-12") is None, repr(svc12.registry.snapshot()))
wait_idle12 = svc12.wait_idle(timeout_seconds=0.2)
check("没有回合在飞时 wait_idle 立即返回 ok", wait_idle12["ok"] is True and wait_idle12["inFlight"] == [], repr(wait_idle12))
down12 = svc12.shutdown(timeout_seconds=0.5)
check("shutdown 交回报告并点名还在跑的后台作业（这里没有作业）", down12["ok"] is True and down12["runningJobs"] == [], repr(down12))

# ── 收尾 ───────────────────────────────────────────────────────
tws.reset_runtime()
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n合计 {SECTION['n']} 节，失败 {len(FAILURES)} 项")
if FAILURES:
    for name in FAILURES:
        print(f"  FAIL: {name}")
    sys.exit(1)
print("engine_ticket_worker_service_selftest: 全绿")
