#!/usr/bin/env python3
"""
工单工作区的真实准备动作与按剩余窗口取页（缺陷 D-48.8）自测

D-48.7 把工单执行体接到了 HTTP 面上，但台账留下两件「代码没承担」的口径：**准备规格**
（`prepare_fn` 一直是注入点，引擎侧从来没有一份真实现，默认只会建空目录）与**按剩余窗口
取页**（三面都只做到固定预算）。本文件把这两件的口径钉住，每条都问过「什么缺陷会让它红」：

准备那一半：

* **只有三种规格**：`repo`（本地仓 `git worktree add --detach`）、`url`（浅克隆）、`command`
  （**argv 列表**）。`command` 给字符串一律拒——把一条字符串交给 shell 解释，等于给
  「准备工作区」开一个任意代码执行面；argv 里出现 `& | ; < > $` 也一并拒，本层不 exec shell。
* **凭据不出门**：失败理由要进回话与状态面，所以 `https://user:pat@host` 与 `--password=X`
  这类形状在 `label()` / `describe()` / 异常文本里全部打掉。假传输层专门断言这一点。
* **不许交互式问口令**：`GIT_TERMINAL_PROMPT=0` 是必须的——私有仓的口令提示会把一次准备
  拖成十分钟死等，而槽位在这十分钟里是占着的。
* **准备失败不留假目录**：只收**本次自己创建**的目录（不在 worker 根之下的一律不动），
  然后才叫来源侧的 `cleanup()`。顺序不能倒：目录还在时 `git worktree prune` 不动那条登记，
  同一目录重试撞「missing but already registered worktree」（`rc=128`，实测）。
* **换来源不复用同一个目录**：准备标记存着来源指纹，B 仓的唤醒想用 A 仓准备出来的目录会被拒。
  `timeoutSeconds` **不参与指纹**——调超时改的是「等多久」，不是「在哪份代码上干活」，
  让一个配置读数把在跑的工单判死是这一层最不该犯的事。
* **不经 HTTP**：规格只认进程内 `configure(prepare=…)` 与环境变量；请求体里出现就当场 400，
  不静默忽略——静默会让配的人以为自己的 worktree 站上了，而执行体其实只在空目录里干活。

取页那一半：

* **没有窗口信息就是固定预算**：今天大多数装配没填 `contextWindow`，这时页面里不许出现
  一句关于窗口的话（说了就是假）。
* **只压不抬、永不清零**：窗口剩得多也不给过固定上限；算出 0 也按下限给一页——空正文让模型
  读到「后面还有很多」却一个字拿不到，比撞穿窗口更难诊断，真撞穿由压缩那层负责。
* **确定性分两半说实话**：窗口是用户填的还是估算的、用量是上游回报的还是本地折算的，
  四个字段各自可见。折算值不许冒充厂商 tokenizer 计数，更**不许进用量台账**
  （既有口径：模型没回 usage 就不自造台账）。

`TONGTIANWORK_HOME` 一节一根。真 git 那节在没有 git 的机器上打 `[SKIP]`（不计入 PASS）。
纯 stdlib + 引擎件，不起服务、不碰 Rust。

用法: python tests/integration/engine_ticket_workspace_prepare_selftest.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-prepare-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-0")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []
SECTION = ""


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{SECTION} :: {name}")


def skip(name: str, detail: str = "") -> None:
    print(f"[SKIP] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    global SECTION
    SECTION = title
    print(f"\n=== {title} ===")


def use_home(tag: str) -> Path:
    home = _TMP / f"home-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["TONGTIANWORK_HOME"] = str(home)
    return home


import data_paths as dp  # noqa: E402
import ticket_model_channel as tmc  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402
import ticket_worker_service as tws  # noqa: E402
import ticket_workspace_prepare as twp  # noqa: E402

T0 = 1_760_000_000_000
SECRET = "sk-SECRETDONTLEAK9"


class FakeRun:
    """记账的 `subprocess.run`：记下 argv / cwd / timeout / env，可按 argv 关键字设退出码。"""

    def __init__(self, results: dict | None = None, default_rc: int = 0, raises: Exception | None = None) -> None:
        self.calls: list = []
        self.results = results or {}
        self.default_rc = default_rc
        self.raises = raises

    def __call__(self, argv, **kwargs) -> object:
        joined = " ".join(str(a) for a in argv)
        self.calls.append({"argv": list(argv), "joined": joined, **kwargs})
        if self.raises is not None:
            raise self.raises
        rc = self.default_rc
        stdout = ""
        stderr = ""
        for needle, outcome in self.results.items():
            if needle in joined:
                rc = outcome.get("rc", 0)
                stdout = outcome.get("out", "")
                stderr = outcome.get("err", "")
                break

        class _Done:
            pass

        done = _Done()
        done.returncode = rc
        done.stdout = stdout
        done.stderr = stderr
        return done

    def joined_calls(self) -> list:
        return [c["joined"] for c in self.calls]


# ── 1. 三种规格的形状 ──────────────────────────────────────────
section("准备规格：只认这三种形状")

p_repo = twp.build_preparer({"repo": "C:/repos/app"}, run=FakeRun())[0]
check("repo 类规格归一成 kind=repo", p_repo.kind == twp.KIND_REPO and p_repo.spec["repo"] == "C:/repos/app", repr(p_repo.spec))
bare = twp.build_preparer("C:/repos/app", run=FakeRun())[0]
check("裸路径字符串按本地仓读", bare.kind == twp.KIND_REPO and bare.spec["repo"] == "C:/repos/app", repr(bare.spec))
url_spec = twp.build_preparer("https://example.com/a/b.git", run=FakeRun())[0]
check("裸 URL 字符串按远端仓读（浅克隆一层）", url_spec.kind == twp.KIND_URL and url_spec.spec["depth"] == 1, repr(url_spec.spec))
ssh_spec = twp.build_preparer("git@example.com:a/b.git", run=FakeRun())[0]
check("`git@host:path` 这种 scp 形状也算远端", ssh_spec.kind == twp.KIND_URL, repr(ssh_spec.spec))
cmd_spec = twp.build_preparer(["python", "bootstrap.py"], run=FakeRun())[0]
check("argv 列表等价于 command 类规格", cmd_spec.kind == twp.KIND_COMMAND and cmd_spec.spec["argv"] == ["python", "bootstrap.py"], repr(cmd_spec.spec))
ref_spec = twp.build_preparer({"repo": "C:/r", "branch": "main"}, run=FakeRun())[0]
check("`branch` 是 `ref` 的别名（两种写法都收到 ref）", ref_spec.spec["ref"] == "main", repr(ref_spec.spec))
deep = twp.build_preparer({"url": "https://e/x.git", "depth": 99999}, run=FakeRun())[0]
check("depth 过大有界（夹到 200，不当「克隆整个历史」的口子）", deep.spec["depth"] == 200, repr(deep.spec))
timeout_spec = twp.build_preparer({"repo": "C:/r", "timeoutSeconds": 30}, run=FakeRun())[0]
check("规格里的 timeoutSeconds 真生效（不是被静默丢掉退回 600）", timeout_spec.timeout_seconds == 30, repr(timeout_spec.timeout_seconds))
check("构造参数 timeout_seconds 优先于规格里的", twp.build_preparer({"repo": "C:/r", "timeoutSeconds": 30}, timeout_seconds=45, run=FakeRun())[0].timeout_seconds == 45)

# ── 2. 拒形 ────────────────────────────────────────────────────
section("拒形：需要 shell 解释的写法一律不收")

rejected: list = []
for label, bad in (
    ("command 给字符串", {"command": "git clone x && rm -rf /"}),
    ("argv 里有管道", {"command": ["git", "clone", "x", "|", "tee", "log"]}),
    ("argv 里有重定向", ["python", "setup.py", ">", "out.txt"]),
    ("argv 里有分号", ["python", "a.py;", "python", "b.py"]),
    ("argv 里有变量", ["echo", "$HOME"]),
    ("argv 空列表", {"command": []}),
    ("argv 里有空串", ["python", ""]),
    ("空字符串规格", "   "),
    ("既没 repo 也没 url 也没 command", {"kind": "repo"}),
    ("不认识的 kind", {"kind": "docker", "image": "x"}),
    ("repo 字段给了远端地址", {"repo": "https://example.com/a.git"}),
    ("url 字段给了本地路径", {"url": "C:/repos/app"}),
    ("depth 不是整数", {"url": "https://e/x.git", "depth": "很多"}),
    ("depth 是 0", {"url": "https://e/x.git", "depth": 0}),
    ("规格是整数", 42),
    ("规格是 Path 类对象", Path("C:/x").parent),
):
    preparer, reason = twp.build_preparer(bad)
    rejected.append((label, preparer is None and bool(reason), reason))
    check(f"拒：{label}", preparer is None and bool(reason), repr(reason))
check("拒绝理由全是中文且点名怎么改（不是「参数错误」那种废话）", all(("——" in r or "请" in r or "只能是" in r or "必填" in r or "要的是" in r) for _l, _ok, r in rejected if r), repr([r for _l, _ok, r in rejected][:3]))
check("build_preparer 不抛穿装配（坏规格回 (None, 理由)）", all(isinstance(r, str) for _l, _ok, r in rejected))

# ── 3. 超时夹取 ────────────────────────────────────────────────
section("超时：有界，且不跟分页预算抢口径")

check("缺省 600 秒（DSH 的十分钟）", twp.clamp_timeout(None) == twp.DEFAULT_TIMEOUT_SECONDS == 600)
check("上界一小时（更长那是看门狗的活）", twp.clamp_timeout(99999) == twp.MAX_TIMEOUT_SECONDS == 3600)
check("下界 1 秒", twp.clamp_timeout(-5) == twp.DEFAULT_TIMEOUT_SECONDS and twp.clamp_timeout(0) == twp.DEFAULT_TIMEOUT_SECONDS)
check("认不出来的写法取缺省而不是 0", twp.clamp_timeout("一会儿") == 600 and twp.clamp_timeout("") == 600)
check("小数按整数读（30.7 → 30）", twp.clamp_timeout("30.7") == 30)

# ── 4. 凭据形状 ────────────────────────────────────────────────
section("凭据不出门：失败理由要进回话与状态面")

check("URL 内嵌 user:token@ 被打掉", twp.redact_secrets(f"https://u:{SECRET}@g.example.com/x.git") == "https://u:***@g.example.com/x.git", twp.redact_secrets(f"https://u:{SECRET}@g.example.com/x.git"))
check("--password=X 打掉", SECRET not in twp.redact_secrets(f"git clone --password={SECRET} https://e/x.git"))
check("--token= 打掉", SECRET not in twp.redact_secrets(f"curl --token={SECRET}"))
check("--api-key= 打掉", SECRET not in twp.redact_secrets(f"--api-key={SECRET}"))
check("没有凭据形状时逐字不动", twp.redact_secrets("git worktree add --detach C:/x") == "git worktree add --detach C:/x")
leaky = twp.build_preparer({"url": f"https://u:{SECRET}@g.example.com/x.git", "depth": 1}, run=FakeRun({"clone": {"rc": 128, "err": f"fatal: authentication failed for https://u:{SECRET}@g.example.com/x.git"}}))[0]
try:
    leaky(Path("C:/target"))
    check("clone 失败会抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("失败理由里不带凭据（异常文本进回话，凭据进去就等于抄进日志）", SECRET not in str(exc), str(exc))
    check("理由点名是哪一步（git clone）与退出码", "git clone" in str(exc) and "128" in str(exc), str(exc))
check("label() 也不带凭据", SECRET not in leaky.label(), leaky.label())
check("describe() 整份 JSON 里没有凭据", SECRET not in json.dumps(leaky.describe(), ensure_ascii=False), repr(leaky.describe()))

# ── 5. repo 类的真实动作形状 ───────────────────────────────────
section("repo 类：worktree 的 argv 形状与不许问口令")

use_home("5")
fr5 = FakeRun()
prep5 = twp.build_preparer({"repo": str(_TMP / "src5")}, run=fr5)[0]
(_TMP / "src5").mkdir(parents=True, exist_ok=True)
target5 = _TMP / "worker-5"
target5.mkdir(parents=True, exist_ok=True)
try:
    prep5(target5)
    check("repo 不存在时不抛（用例先把目录建出来了，这里应当走通）", fr5.joined_calls() and "worktree add" in fr5.joined_calls()[-1], repr(fr5.joined_calls()))
except twp.WorkspacePrepareError as exc:
    check("假 git 探针默认成功时不该抛", False, str(exc))
calls5 = fr5.joined_calls()
check("先探一次是不是 git 仓（rev-parse --git-dir），再谈 worktree", any("rev-parse --git-dir" in c for c in calls5), repr(calls5))
add = next((c for c in fr5.calls if "worktree add" in c["joined"]), None)
check("`worktree add --detach` 且目标目录在最后（不带 ref 时 argv 里不插东西）", add is not None and add["argv"][-1] == str(target5) and "--detach" in add["argv"], repr(add["argv"]) if add else "没跑 add")
check("在源仓里跑（git -C <repo>），不靠当前目录", add is not None and add["argv"][:3] == ["git", "-C", str(_TMP / "src5")], repr(add["argv"][:3]) if add else "")
check("探针也有界（60 秒，不跟着十分钟准备超时走）", any(c["joined"].endswith("rev-parse --git-dir") and c["timeout"] == twp.PROBE_TIMEOUT_SECONDS for c in fr5.calls), repr([(c["joined"], c["timeout"]) for c in fr5.calls]))
check("env 里钉死 GIT_TERMINAL_PROMPT=0（私有仓问口令会把这轮拖成十分钟死等）", all(c["env"].get("GIT_TERMINAL_PROMPT") == "0" for c in fr5.calls) and bool(fr5.calls), repr([c["env"].get("GIT_TERMINAL_PROMPT") for c in fr5.calls]))
check("GIT_ASKPASS 置空（GUI 口令框同样不许弹）", all(c["env"].get("GIT_ASKPASS") == "" for c in fr5.calls) and bool(fr5.calls))
check("准备成功计数进观测读数", prep5.prepared == 1 and prep5.last_error == "", repr(prep5.describe()))

fr5b = FakeRun()
prep5b = twp.build_preparer({"repo": str(_TMP / "src5"), "ref": "main"}, run=fr5b)[0]
prep5b(target5)
add_b = next((c for c in fr5b.calls if "worktree add" in c["joined"]), None)
check("带 ref 时 ref 排在目标目录之前（否则 git 把目录当分支名）", add_b is not None and add_b["argv"][-2:] == ["main", str(target5)], repr(add_b["argv"]) if add_b else "")

missing = twp.build_preparer({"repo": str(_TMP / "does-not-exist-5")}, run=FakeRun())[0]
try:
    missing(target5)
    check("本地仓不存在要抛（不能演成「已就绪」）", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("本地仓不存在的理由是中文并点名路径", "本地仓不存在" in str(exc), str(exc))
notgit = twp.build_preparer({"repo": str(_TMP / "src5")}, run=FakeRun({"rev-parse": {"rc": 128, "err": "fatal: not a git repository"}}))[0]
try:
    notgit(target5)
    check("不是 git 仓也要抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("非 git 仓的理由点名 rev-parse 退出码", "不是一个 git 仓" in str(exc) and "128" in str(exc), str(exc))
fr_notgit = notgit._run
check("探针失败 = 不 attempt add（失败的那一步决定要不要走下一步）", all("worktree add" not in c for c in fr_notgit.joined_calls()), repr(fr_notgit.joined_calls()))
a_file = _TMP / "not-a-repo.txt"
a_file.write_text("x", encoding="utf-8")
fr_isdir = FakeRun()
isdir_fail = twp.build_preparer({"repo": str(a_file)}, run=fr_isdir)[0]
try:
    isdir_fail(target5)
    check("repo 指向文件要抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("repo 指向文件（不是目录）也如实拒", "不是目录" in str(exc), str(exc))
check("目录判据先于任何外部动作（连探针都不发）", fr_isdir.calls == [], repr(fr_isdir.joined_calls()))

# ── 6. url / command 类的形状 ──────────────────────────────────
section("url 与 command 类：直接 exec，不经 shell")

fr6 = FakeRun()
prep6 = twp.build_preparer({"url": "https://example.com/a/b.git", "depth": 3, "ref": "dev"}, run=fr6)[0]
prep6(_TMP / "target6")
clone = fr6.calls[0]["argv"]
check("浅克隆带 --depth N", "--depth" in clone and clone[clone.index("--depth") + 1] == "3", repr(clone))
check("指定分支走 --branch", "--branch" in clone and clone[clone.index("--branch") + 1] == "dev", repr(clone))
check("地址前有 `--` 分隔（仓库名长得像 - 开头时不当选项吃）", clone[clone.index("--") + 1] == "https://example.com/a/b.git", repr(clone))
check("clone 的目标就是工单目录", clone[-1] == str(_TMP / "target6"), repr(clone))
check("url 类不跑 rev-parse 探针（没有本地源仓可探）", len(fr6.calls) == 1, repr(fr6.joined_calls()))

fr6c = FakeRun()
prep6c = twp.build_preparer({"command": ["python", "scripts/bootstrap.py"], "appendTarget": True, "cwd": str(_TMP)}, run=fr6c)[0]
prep6c(_TMP / "target6c")
check("command 类直接 exec argv（argv[0] 就是可执行名，没有 shell）", fr6c.calls[0]["argv"] == ["python", "scripts/bootstrap.py", str(_TMP / "target6c")], repr(fr6c.calls[0]["argv"]))
check("cwd 取规格给的（给了就用它，不用工单目录）", fr6c.calls[0]["cwd"] == str(_TMP), repr(fr6c.calls[0]["cwd"]))
fr6d = FakeRun()
prep6d = twp.build_preparer({"command": ["python", "-m", "tools.sync"]}, run=fr6d)[0]
(_TMP / "target6d").mkdir(parents=True, exist_ok=True)
prep6d(_TMP / "target6d")
check("不给 cwd 就在工单目录里跑", fr6d.calls[0]["cwd"] == str(_TMP / "target6d"), repr(fr6d.calls[0]["cwd"]))
check("不给 appendTarget 就不把目录追加到 argv 尾巴", fr6d.calls[0]["argv"] == ["python", "-m", "tools.sync"], repr(fr6d.calls[0]["argv"]))
nocwd = twp.build_preparer({"command": ["python", "x.py"], "cwd": str(_TMP / "no-such-dir")}, run=FakeRun())[0]
try:
    nocwd(_TMP / "t")
    check("命令的工作目录不存在要抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("理由点名「工作目录不存在」", "工作目录不存在" in str(exc), str(exc))
fr6e = FakeRun({"boom": {"rc": 3, "err": "Traceback..."}})
prep6e = twp.build_preparer({"command": ["python", "boom.py"]}, run=fr6e)[0]
(_TMP / "target6e").mkdir(parents=True, exist_ok=True)
try:
    prep6e(_TMP / "target6e")
    check("命令非零退出要抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("失败理由带上那条命令（红字可查到底跑了什么）", "python boom.py" in str(exc) and "退出码 3" in str(exc), str(exc))
missing_bin = twp.build_preparer({"command": ["definitely-not-a-real-binary"]}, run=FakeRun(raises=FileNotFoundError("not found")))[0]
(_TMP / "target6f").mkdir(parents=True, exist_ok=True)
try:
    missing_bin(_TMP / "target6f")
    check("找不到可执行文件要抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("理由说清「不经 shell，也不做替代猜测」", "找不到可执行文件" in str(exc) and "不经 shell" in str(exc), str(exc))
timed_out = twp.build_preparer({"command": ["python", "slow.py"], "timeoutSeconds": 1}, run=FakeRun(raises=subprocess.TimeoutExpired(cmd=["python"], timeout=1)))[0]
try:
    timed_out(_TMP / "target6f")
    check("超时要抛", False, "没抛")
except twp.WorkspacePrepareError as exc:
    check("超时理由点名「按超时中止」并带上那一个秒数", "超过 1 秒" in str(exc) and "已按超时中止" in str(exc), str(exc))

# ── 7. 来源指纹 ────────────────────────────────────────────────
section("来源指纹：换仓就是换来源，换超时不是")

f_a = twp.build_preparer({"repo": "C:/r", "ref": "main"}, run=FakeRun())[0]
f_a2 = twp.build_preparer({"repo": "C:/r", "ref": "main"}, run=FakeRun())[0]
f_b = twp.build_preparer({"repo": "C:/other", "ref": "main"}, run=FakeRun())[0]
f_ref = twp.build_preparer({"repo": "C:/r"}, run=FakeRun())[0]
f_tmo = twp.build_preparer({"repo": "C:/r", "ref": "main", "timeoutSeconds": 42}, run=FakeRun())[0]
check("同一份规格两次装配指纹一致（幂等复用的凭据）", f_a.fingerprint == f_a2.fingerprint and len(f_a.fingerprint) == 12, f_a.fingerprint)
check("换仓就是换指纹", f_a.fingerprint != f_b.fingerprint)
check("不带 ref 与带 ref 是两个来源（worktree 的内容真的不同）", f_a.fingerprint != f_ref.fingerprint)
check("**换超时不换指纹**（调超时不该把在跑的工单判成换来源）", f_a.fingerprint == f_tmo.fingerprint, f"{f_a.fingerprint} vs {f_tmo.fingerprint}")
check("裸路径与字典写法同指纹", twp.build_preparer("C:/r", run=FakeRun())[0].fingerprint == twp.build_preparer({"repo": "C:/r"}, run=FakeRun())[0].fingerprint)
check("指纹不是路径本身（不可逆，状态面可以放心带）", f_a.fingerprint not in json.dumps({"repo": "C:/r"}, ensure_ascii=False))

# ── 8. cleanup 的顺序与说法 ────────────────────────────────────
section("cleanup：只在 repo 类清登记，说法诚实")

note_cmd = twp.build_preparer({"command": ["python", "x.py"]}, run=FakeRun())[0]
check("command 类没有 git 登记可清，回空话不是回谎话", note_cmd.cleanup(_TMP / "t") == "")
note_url = twp.build_preparer("https://e/x.git", run=FakeRun())[0]
check("url 类同理（clone 出来的目录删了就删了）", note_url.cleanup(_TMP / "t") == "")
fr_prune = FakeRun()
note_repo = twp.build_preparer({"repo": str(_TMP / "src5")}, run=fr_prune)[0]
said = note_repo.cleanup(_TMP / "t8")
check("repo 类跑的是 `git -C <源仓> worktree prune`", fr_prune.joined_calls() == [f"git -C {os.path.join(str(_TMP), 'src5')} worktree prune"] or any(c.argv[-1] == "prune" for c in fr_prune.calls), repr(fr_prune.joined_calls()))
check("prune 成功时给一句「可以再跑一次准备」", "prune" in said and "可以再跑一次" in said, said)
bad_prune = twp.build_preparer({"repo": str(_TMP / "src5")}, run=FakeRun({"prune": {"rc": 128, "err": "fatal: cannot"}}))[0]
bad_said = bad_prune.cleanup(_TMP / "t8")
check("prune 自己失败时不许说「已清理」，并预警重试可能撞 already registered", "已清理" not in bad_said and "already registered" in bad_said, bad_said)
gone_repo = twp.build_preparer({"repo": str(_TMP / "no-such-src")}, run=FakeRun())[0]
check("源仓路径不可用时如实说「没能清理」", "没能清理" in gone_repo.cleanup(_TMP / "t8"), gone_repo.cleanup(_TMP / "t8"))
check("cleanup 的 timeout 也有界（它在失败清理路径上，不能再拖住一条已失败的回话）", all(c["timeout"] == twp.CLEANUP_TIMEOUT_SECONDS for c in fr_prune.calls), repr([c["timeout"] for c in fr_prune.calls]))

# ── 9. TicketWorkspace.prepare 的现场 ──────────────────────────
section("工作区：标记存指纹、换来源拒、失败不留假目录")

use_home("9")
root9 = dp.ticket_workers_dir()
ws9 = twr.TicketWorkspace()


class BoomPreparer:
    """准备到一半抛错的规格：先留下半成品内容，再失败——正是老写法 `rmdir()` 收不掉的那种。"""

    def __init__(self, fingerprint: str, *, leaves: bool = True, cleanup_note: str = "（用例设定：已 prune）", raises_in_cleanup: bool = False) -> None:
        self.fingerprint = fingerprint
        self.leaves = leaves
        self.calls = 0
        self.cleanup_calls: list = []
        self.cleanup_note = cleanup_note
        self.raises_in_cleanup = raises_in_cleanup

    def __call__(self, path: Path) -> None:
        self.calls += 1
        if self.leaves:
            (Path(path) / "half-done.txt").write_text("半成品", encoding="utf-8")
        raise RuntimeError("worktree add 挂了（用例设定）")

    def cleanup(self, path: Path) -> str:
        # **顺序证据**：运行时必须在删掉目录之后才叫 cleanup，这里当场断言目录已经不在了
        self.cleanup_calls.append({"path": Path(path), "stillExists": Path(path).exists()})
        if self.raises_in_cleanup:
            raise ValueError("清理自己炸了（用例设定）")
        return self.cleanup_note


boom9 = BoomPreparer("fp-nine")
path9, err9 = ws9.prepare("tk9", "换来源这条", boom9)
check("准备失败的中文理由带上了原始异常", err9 and "工单工作区准备失败" in err9 and "用例设定" in err9, repr(err9))
check("**半成品目录被收走了**（老写法只 rmdir，非空就吞掉 OSError 留个脏目录）", not path9.exists(), str(path9))
check("cleanup 在目录删掉之后才被叫（倒序会撞「missing but already registered worktree」）", boom9.cleanup_calls and boom9.cleanup_calls[0]["stillExists"] is False, repr(boom9.cleanup_calls))
check("清理说明并进了失败理由", "已 prune" in err9, repr(err9))
check("失败时不落准备标记（下次唤醒会重新准备，而不是以为已经好了）", not (path9 / twr.PREPARE_MARKER).exists())

boom9b = BoomPreparer("fp-nine", cleanup_note="")
boom9b.leaves = True
path9b, err9b = ws9.prepare("tk9b", "", boom9b)
check("没有清理说明时不编一句出来", err9b and "已 prune" not in err9b, repr(err9b))

boom9c = BoomPreparer("fp-nine", raises_in_cleanup=True)
path9c, err9c = ws9.prepare("tk9c", "", boom9c)
check("清理自己抛了也如实说，不改口成「已清理」", "来源侧清理自己抛了" in err9c and "已清理" not in err9c, repr(err9c))


def plain_boom(path: Path) -> None:
    (Path(path) / "junk.log").write_text("x", encoding="utf-8")
    raise RuntimeError("普通函数式的准备动作失败")


plain_boom.fingerprint = "fp-plain"
path9d, err9d = ws9.prepare("tk9d", "", plain_boom)
check("没有 cleanup 口但删干净了时，不多留一句「登记可能还在仓里」", not path9d.exists() and "没有来源侧清理口" not in err9d, repr(err9d))


def plain_boom_noattr(path: Path) -> None:
    raise RuntimeError("失败且没有指纹")


path9e, err9e = ws9.prepare("tk9e", "", plain_boom_noattr)
check("没指纹的准备动作照样能跑（空目录路线不欠指纹）", err9e is None or "失败" in err9e, repr(err9e))

class OkPreparer:
    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self.calls = 0

    def __call__(self, path: Path) -> None:
        self.calls += 1
        (Path(path) / "worktree-checkout.txt").write_text("真的准备了", encoding="utf-8")

    def cleanup(self, path: Path) -> str:
        return "（已 prune）"


ok9 = OkPreparer("fp-ok-1")
path9g, err9g = ws9.prepare("tk9g", "幂等", ok9)
check("准备成功时不留错误、并把产物写在工单目录里", err9g is None and (path9g / "worktree-checkout.txt").exists(), repr(err9g))
marker9g = json.loads((path9g / twr.PREPARE_MARKER).read_text(encoding="utf-8"))
check("标记存了来源指纹", marker9g.get("prepareFingerprint") == "fp-ok-1", repr(marker9g))
check("标记说了这轮真跑过 prepare", marker9g.get("ranPrepare") is True and marker9g.get("ticketId") == "tk9g", repr(marker9g))
again = OkPreparer("fp-ok-1")
path9g2, err9g2 = ws9.prepare("tk9g", "换了标题", again)
check("同一来源第二次唤醒：幂等复用，一次 prepare 也不多跑", err9g2 is None and again.calls == 0 and path9g2 == path9g, repr((err9g2, again.calls)))
other = OkPreparer("fp-OTHER")
path9g3, err9g3 = ws9.prepare("tk9g", "", other)
check("**换来源被拒**（A 仓准备出来的目录不能假装是 B 仓）", err9g3 is not None and "另一份来源" in err9g3 and other.calls == 0, repr(err9g3))
check("拒绝理由把两份指纹都点出来（现场可查，不是一句「冲突」）", "fp-ok-1" in err9g3 and "fp-OTHER" in err9g3, repr(err9g3))
check("被拒时目录与产物一个字没动（拒绝不是清理）", (path9g / "worktree-checkout.txt").exists())

legacy_dir = ws9.path_for("tk9h", "")
legacy_dir.mkdir(parents=True, exist_ok=True)
(legacy_dir / twr.PREPARE_MARKER).write_text(json.dumps({"ticketId": "tk9h", "dirName": legacy_dir.name}), encoding="utf-8")
legacy_preparer = OkPreparer("fp-new")
legacy_path, legacy_err = ws9.prepare("tk9h", "", legacy_preparer)
check("旧标记没有指纹字段时按「不知道来源」放行（升级期不把在跑的工单判死）", legacy_err is None and legacy_path == legacy_dir, repr(legacy_err))
check("放行不等于重跑：目录还在标记就还在，一次外部动作也不多来", legacy_preparer.calls == 0, repr(legacy_preparer.calls))
broken_marker = ws9.path_for("tk9i", "")
broken_marker.mkdir(parents=True, exist_ok=True)
(broken_marker / twr.PREPARE_MARKER).write_text("{ 不是 JSON", encoding="utf-8")
broken_preparer = OkPreparer("fp-x")
b_path, b_err = ws9.prepare("tk9i", "", broken_preparer)
check("标记读不动时同样放行而不是抛穿投递（读不懂来源≠来源冲突）", b_err is None and b_path == broken_marker, repr(b_err))
check("读不懂标记时不猜「已经准备过」也不重跑（放行是复用既有目录这条老口径）", broken_preparer.calls == 0, repr(broken_preparer.calls))
check("prepare_fingerprint 优先用显式给的 key", twr.TicketWorkspace.prepare_fingerprint(OkPreparer("fp-obj"), "fp-explicit") == "fp-explicit")
check("不给 key 就从可调用对象上取", twr.TicketWorkspace.prepare_fingerprint(OkPreparer("fp-obj"), None) == "fp-obj")
check("两样都没有就是空串（不与已有来源较劲）", twr.TicketWorkspace.prepare_fingerprint(plain_boom_noattr, None) == "")

# ── 10. 并发首触与 prepare 的组合 ──────────────────────────────
section("并发首触：prepare 只跑一次，两个线程共用一份工作区")

use_home("10")
ws10 = twr.TicketWorkspace()
count10 = {"n": 0}
lock10 = threading.Lock()


class SlowPreparer:
    fingerprint = "fp-10"

    def __call__(self, path: Path) -> None:
        with lock10:
            count10["n"] += 1
        time.sleep(0.2)
        (Path(path) / "who.txt").write_text(str(count10["n"]), encoding="utf-8")

    def cleanup(self, path: Path) -> str:
        return ""


results10: list = []


def wake10(index: int) -> None:
    got = ws10.prepare("tk10", "并发单", SlowPreparer(), prepare_key=None)
    results10.append(got)


threads10 = [threading.Thread(target=wake10, args=(i,)) for i in range(2)]
for t in threads10:
    t.start()
for t in threads10:
    t.join(30)
check("两条并发首触都拿到同一路径且都没报错", len(results10) == 2 and all(err is None for _p, err in results10), repr(results10))
check("**prepare 只跑了一次**（另一条在锁内看到标记就复用，不重复外部动作）", count10["n"] == 1, repr(count10))
check("两条拿到的目录名一致", results10[0][0] == results10[1][0], repr([str(p) for p, _e in results10]))

explicit_key = ws10.prepare("tk10b", "", OkPreparer("fp-obj-key"), prepare_key="fp-explicit")
marker_key = json.loads((explicit_key[0] / twr.PREPARE_MARKER).read_text(encoding="utf-8"))
check("显式 prepare_key 覆盖对象自带的指纹", marker_key["prepareFingerprint"] == "fp-explicit", repr(marker_key))

# ── 11. remove_created_tree 的边界 ─────────────────────────────
section("半成品清理：只在 worker 根之下动手")

use_home("11")
root11 = dp.ticket_workers_dir()
(root11).mkdir(parents=True, exist_ok=True)
inside = root11 / "worker-inside"
(inside / "sub").mkdir(parents=True, exist_ok=True)
(inside / "sub" / "f.txt").write_text("x", encoding="utf-8")
removed, note = twp.remove_created_tree(inside, root=root11)
check("根之下的非空目录能整份收走", removed is True and not inside.exists() and note == "", repr((removed, note)))
outside = _TMP / "user-stuff"
outside.mkdir(parents=True, exist_ok=True)
(outside / "keep.txt").write_text("别动", encoding="utf-8")
removed2, note2 = twp.remove_created_tree(outside, root=root11)
check("**越出 worker 根就不动手**，并说清为什么（删别人的东西不是清理）", removed2 is False and outside.exists() and "不在 worker 根之下" in note2, repr((removed2, note2)))
removed3, note3 = twp.remove_created_tree(root11 / "never-created", root=root11)
check("路径本来就不存在算「清掉了」，不编一句失败", removed3 is True and note3 == "", repr((removed3, note3)))
removed4, note4 = twp.remove_created_tree(outside / "keep.txt", root=_TMP)
check("指向文件（不是目录）不动它也不报错", removed4 is True and (outside / "keep.txt").exists(), repr((removed4, note4)))
held = root11 / "worker-held"
held.mkdir(parents=True, exist_ok=True)
handle = (held / "locked.bin").open("wb")
handle.write(b"x" * 100)
removed5, note5 = twp.remove_created_tree(held, root=root11)
check("报告与现场一致：删成功就不留话，删不干净就点名「还留着」", removed5 == (not held.exists()) and (note5 == "" if removed5 else "删不干净" in note5), repr((removed5, note5, held.exists())))
handle.close()
shutil.rmtree(held, ignore_errors=True)

# ── 12. 真 git（有就验，没有就 SKIP）──────────────────────────
section("真 git：worktree 建出来的是真工作树")

git_path = shutil.which("git")
if git_path is None:
    skip("真 git 一节", "机器上找不到 git（不计入 PASS，也不假装验过）")
else:
    use_home("12")
    src12 = _TMP / "src12"
    src12.mkdir(parents=True, exist_ok=True)
    env12 = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    base12 = {"cwd": str(src12), "capture_output": True, "text": True, "env": env12}
    subprocess.run(["git", "init", "-q", "."], **base12)
    subprocess.run(["git", "config", "user.email", "selftest@example.invalid"], **base12)
    subprocess.run(["git", "config", "user.name", "selftest"], **base12)
    (src12 / "README.md").write_text("# 源仓\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], **base12)
    commit12 = subprocess.run(["git", "commit", "-q", "-m", "init"], **base12)
    if commit12.returncode != 0:
        skip("真 git 一节", f"这台 git 建不起带提交的仓（rc={commit12.returncode}：{commit12.stderr[:120]}）")
    else:
        ws12 = twr.TicketWorkspace()
        real12 = twp.WorkspacePreparer({"repo": str(src12)})
        path12, err12 = ws12.prepare("tk12", "真 worktree", real12)
        check("真跑一次 `git worktree add --detach` 建成了目录", err12 is None and path12.is_dir(), repr(err12))
        check("工单目录里带着源仓的正文（不是空壳目录）", (path12 / "README.md").exists() if err12 is None else False, repr(path12))
        check("它是真 worktree（.git 是文件而不是目录，指向源仓）", err12 is None and (path12 / ".git").is_file(), repr([str(path12 / ".git"), (path12 / ".git").exists(), (path12 / ".git").is_file()]))
        listed = subprocess.run(["git", "-C", str(src12), "worktree", "list", "--porcelain"], capture_output=True, text=True, env=env12)
        check("源仓的 worktree 登记里能看见这一份", path12.name in listed.stdout, repr(listed.stdout[:200]))
        second12 = twp.WorkspacePreparer({"repo": str(src12)})
        p2, e2 = ws12.prepare("tk12", "", second12)
        check("第二次唤醒幂等复用（一次 add 也不多跑）", e2 is None and second12.prepared == 0 and p2 == path12, repr((e2, second12.prepared)))
        other12 = twp.WorkspacePreparer({"repo": str(src12), "ref": "HEAD~0"})
        p3, e3 = ws12.prepare("tk12", "", other12)
        check("同一目录换 ref 判成换来源并拒（真 git 下也是这条口径）", e3 is not None and "另一份来源" in e3, repr(e3))
        # 半成品现场：目标目录已被占住时 add 会失败——真 git 的拒绝语就是这条证据
        collision = ws12.path_for("tk12c", "")
        collision.mkdir(parents=True, exist_ok=True)
        (collision / "占了位置.txt").write_text("不是空目录", encoding="utf-8")
        # 直接把一个已被占用的目录交给准备器：git 会拒（目录非空），现场必须能被清干净后重试
        occupied = subprocess.run(["git", "-C", str(src12), "worktree", "add", "--detach", str(collision)], capture_output=True, text=True, env=env12)
        check("真 git 对非空目标目录会说「already exists」", occupied.returncode != 0 and "already exists" in (occupied.stdout + occupied.stderr), repr((occupied.returncode, occupied.stderr[:160])))
        if occupied.returncode == 0:
            shutil.rmtree(collision, ignore_errors=True)
            subprocess.run(["git", "-C", str(src12), "worktree", "prune"], capture_output=True, text=True, env=env12)
        # 登记悬空（目录被删）时，prune 之后同一目录可以再次 add：这就是顺序证据的真机版
        dprep = twp.WorkspacePreparer({"repo": str(src12)})
        dp, derr = ws12.prepare("tk12d", "", dprep)
        check("另一份真 worktree 建起来了（用例前提）", derr is None and dp.is_dir(), repr(derr))
        shutil.rmtree(dp, ignore_errors=True)
        pruned = dprep.cleanup(dp)
        rc_retry = subprocess.run(["git", "-C", str(src12), "worktree", "add", "--detach", str(dp)], capture_output=True, text=True, env=env12)
        check("删目录 + prune 之后，同一目录能再次 add（倒序时这里会撞 rc=128）", rc_retry.returncode == 0, repr((pruned, rc_retry.returncode, rc_retry.stderr[:160])))
        if rc_retry.returncode == 0:
            shutil.rmtree(dp, ignore_errors=True)
            subprocess.run(["git", "-C", str(src12), "worktree", "prune"], capture_output=True, text=True, env=env12)
        # 反证：先 prune（目录还在）不会动登记，随后 add 撞「already registered」
        bprep = twp.WorkspacePreparer({"repo": str(src12)})
        bp, berr = ws12.prepare("tk12e", "", bprep)
        check("第三条真 worktree 建起来了（反证前提）", berr is None, repr(berr))
        if berr is None:
            before = subprocess.run(["git", "-C", str(src12), "worktree", "add", "--detach", str(bp)], capture_output=True, text=True, env=env12)
            check("目录还在时重复 add 被 git 拒（这就是必须先删目录的理由）", before.returncode != 0, repr(before.returncode))
            prune_first = subprocess.run(["git", "-C", str(src12), "worktree", "prune"], capture_output=True, text=True, env=env12)
            still = subprocess.run(["git", "-C", str(src12), "worktree", "list", "--porcelain"], capture_output=True, text=True, env=env12)
            check("目录还在时 prune 清不掉那条登记（顺序不可倒的第二半证据）", bp.name in still.stdout and prune_first.returncode == 0, repr(still.stdout[:200]))
            shutil.rmtree(bp, ignore_errors=True)
            subprocess.run(["git", "-C", str(src12), "worktree", "prune"], capture_output=True, text=True, env=env12)

# ── 13. 服务层：默认准备规格的来源与降级 ──────────────────────
section("服务层：规格只认进程内与环境变量，坏规格降级不拦装配")

use_home("13")


class OkModel:
    def __init__(self) -> None:
        self.requests: list = []

    def __call__(self, request: dict) -> dict:
        self.requests.append(dict(request))
        return {"text": "收口", "calls": [{"tool": "finish_work", "args": {"summary": "做完了"}}], "outcome": "", "reason": ""}


class _NoThread:
    """不起线程的 thread_factory：投递照常，回合留在「还没跑」的状态里，方便只看磁盘事实。"""

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs

    def start(self) -> None:
        return None

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


svc13 = tws.TicketServiceRuntime(
    model_call=OkModel(),
    send_fn=False,
    thread_factory=_NoThread,
    prepare={"repo": str(_TMP / "no-such-13")},
    prepare_env={},
)
state13 = svc13.prepare_state()
check("进程内 prepare= 规格进了装配（enabled=True）", state13["enabled"] is True and state13["kind"] == "repo", repr(state13))
check("装配出来的 preparer 就是 submit 时默认的准备动作", svc13.preparer is not None and callable(svc13.preparer))


svc13b = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, prepare_env={})
check("没配规格时是正常状态而不是故障（enabled=False 且明说「只建目录」）", svc13b.prepare_state()["enabled"] is False and "只建目录" in svc13b.prepare_state()["note"], repr(svc13b.prepare_state()))
check("没配规格时 preparer 是 None（投递不带任何外部动作）", svc13b.preparer is None and svc13b.prepare_reason == "")

svc14 = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, prepare={"command": "随便一条字符串"}, prepare_env={})
check("**坏规格不抛、不拦装配**：装配照样起来，理由进状态面", svc14.preparer is None and "用不了" in svc14.prepare_state()["reason"], repr(svc14.prepare_state()))
check("坏规格的理由里带着「argv 列表」这句怎么改", "argv" in svc14.prepare_state()["reason"], repr(svc14.prepare_state()["reason"]))

os.environ[tws.PREPARE_ENV_VAR] = json.dumps({"repo": "C:/from-env"})
try:
    svc_env = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread)
    check(f"环境变量 {tws.PREPARE_ENV_VAR} 是默认规格的唯一外部来源", svc_env.prepare_state()["enabled"] is True and "from-env" in svc_env.prepare_state()["label"], repr(svc_env.prepare_state()))
    svc_both = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, prepare={"repo": "C:/in-process"})
    check("进程内规格优先于环境变量（两个来源同时给时按更近的那个）", "in-process" in svc_both.prepare_state()["label"], repr(svc_both.prepare_state()))
    os.environ[tws.PREPARE_ENV_VAR] = "   "
    svc_blank = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread)
    check("环境变量是空白就是「没配」，不是「配坏了」", svc_blank.preparer is None and svc_blank.prepare_reason == "", repr(svc_blank.prepare_state()))
    os.environ[tws.PREPARE_ENV_VAR] = "42"
    svc_bad = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread)
    check("坏 JSON 不静默退回不准备：理由点名是哪个变量", svc_bad.preparer is None and tws.PREPARE_ENV_VAR in svc_bad.prepare_reason, repr(svc_bad.prepare_reason))
finally:
    os.environ.pop(tws.PREPARE_ENV_VAR, None)

use_home("15")
svc15 = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, prepare_env={})
sub15 = svc15.submit(ticket_id="tk15", message="投一条", assignment_id="as-15", title="默认准备", arrived_at_ms=T0 + 1)
dir15 = svc15.workspace.path_for("tk15")
check("没 preparer 时投递照常成功并建出目录（最小工作区可运行）", sub15["ok"] is True and dir15.is_dir(), repr(sub15))
check("标记写明这轮没跑 prepare（ranPrepare=False）", json.loads((dir15 / twr.PREPARE_MARKER).read_text(encoding="utf-8"))["ranPrepare"] is False)
svc15b = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, prepare_env={})
okp15 = OkPreparer("fp-15")
svc15b.preparer = okp15
sub15b = svc15b.submit(ticket_id="tk15b", message="投一条", assignment_id="as-15b", arrived_at_ms=T0 + 2)
check("装配期 preparer 会在调用方没带 prepare_fn 时补上", okp15.calls == 1 and sub15b["ok"] is True, repr((sub15b, okp15.calls)))
check(" started 结果里带着这份目录的来源指纹（界面不必再读磁盘标记）", sub15b.get("prepareFingerprint") == "fp-15", repr(sub15b))
explicit15 = OkPreparer("fp-explicit-15")
sub15c = svc15b.submit(ticket_id="tk15c", message="投一条", assignment_id="as-15c", arrived_at_ms=T0 + 3, prepare_fn=explicit15)
check("调用方显式带的 prepare_fn 不被装配默认值覆盖（领取回路优先）", explicit15.calls == 1 and okp15.calls == 1, repr((explicit15.calls, okp15.calls)))
check("已有活绑定时不再碰磁盘（投递只进收件箱，prepare 一次也不多跑）", svc15b.submit(ticket_id="tk15b", message="再来一条", assignment_id="as-15b", arrived_at_ms=T0 + 4)["delivered"] == "existing-worker" and okp15.calls == 1, repr(okp15.calls))

pushed = svc15b.apply_prepare({"url": "https://example.com/pushed.git"})
check("apply_prepare 换规格成功并回报新指纹", pushed["applied"] is True and pushed["prepare"]["kind"] == "url", repr(pushed))
before_fp = svc15b.preparer.fingerprint
failed_push = svc15b.apply_prepare({"command": "字符串命令"})
check("坏的新规格**不换掉旧的**（配错不该把已经配好的准备动作清空）", failed_push["applied"] is False and svc15b.preparer is not None and svc15b.preparer.fingerprint == before_fp, repr(failed_push))
check("失败理由中文可查", "argv" in failed_push["reason"] or "字符串" in failed_push["reason"], repr(failed_push["reason"]))
cleared = svc15b.apply_prepare(None)
check("显式给 None 是「关掉准备」，并如实回 enabled=False", cleared["applied"] is True and svc15b.prepare_state()["enabled"] is False)

status15 = svc15b.status()
check("状态面的 prepare 块能原样过 JSON 往返", json.loads(json.dumps(status15["prepare"], ensure_ascii=False)) == status15["prepare"], repr(status15["prepare"]))
check("状态面有 prepare 这一块（端点拿得到「有没有、从哪儿」）", "prepare" in status15 and "enabled" in status15["prepare"], repr(list(status15)))

# ── 14. 端点面：请求体不开这个口 ───────────────────────────────
section("端点面：准备规格不经 HTTP，出现即拒不静默忽略")

engine_server = None
try:
    import server as engine_server  # noqa: E402  （routes 自测证明 vendor 解释器 import 得动）
except Exception as exc:  # noqa: BLE001
    skip("端点面一节", f"import engine/server.py 失败：{type(exc).__name__}: {exc}")

if engine_server is not None:
    refused = engine_server._ticket_prepare_refused({"prepare": {"command": ["calc"]}})
    check("请求体里出现 prepare → 400 并给中文理由", refused is not None and refused.status_code == 400, repr(refused))
    text = json.loads(refused.body.decode("utf-8"))
    check("拒绝理由说清「只认进程内与环境变量」并点名那个变量名", tws.PREPARE_ENV_VAR in text["message"] and "configure(prepare=" in text["message"], repr(text))
    check("理由先讲清后果（交给 HTTP 等于让远程主体挑目录）", "挑目录" in text["message"], repr(text["message"]))
    for name in engine_server.TICKET_PREPARE_BODY_KEYS:
        got = engine_server._ticket_prepare_refused({name: {}})
        check(f"字段别名 {name} 同样被挡", got is not None and got.status_code == 400, repr(got))
    check("正常唤醒请求不受影响（不误伤 message/ticketId）", engine_server._ticket_prepare_refused({"ticketId": "t", "message": "干活", "assignmentId": "a"}) is None)
    src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    config_body = src.split("async def tickets_config", 1)[1].split("@app.post", 1)[0]
    wakes_body = src.split("async def tickets_wake", 1)[1].split("@app.get", 1)[0]
    check("config 端点真调了这道闸（不是只写了个函数没人调）", "_ticket_prepare_refused(payload)" in config_body, config_body[:200])
    check("wakes 端点也调了（唤醒请求体里同样不许带目录来源）", "_ticket_prepare_refused(payload)" in wakes_body, wakes_body[:200])
    check("闸在桌面判定之后、装配调用之前（被拒的请求一个字也没落进装配）", config_body.index("_ticket_prepare_refused") < config_body.index("tws.configure"))
    check("请求体的白名单里确实没有 prepare 这一项（不靠「传了也没用」兜底）", "prepare" not in config_body.split("kwargs: dict[str, Any] = {}")[1].split("if not kwargs")[0].replace("_ticket_prepare_refused", ""))
    check("端点文档字符串写了这条口径（模型/接线方读得到为什么不开这个口）", "TT_TICKET_WORKER_PREPARE" in config_body)

# ── 15. 按剩余窗口取页：算术只有一份 ──────────────────────────
section("取页：没有窗口信息时逐字等于固定预算")

body15 = "x" * 12000
plain = ta.budgeted_read(body15, None, 0)
legacy = ta.budgeted_read(body15, None, 0, window=None)
check("不传 window 与传 None 的结果逐字一致（老路一点没动）", plain == legacy, repr((plain["units"], legacy["units"])))
check("空窗口字典也走老路：不带 windowApplied，也不出现一句关于窗口的话", legacy["windowApplied"] is False and "窗口" not in legacy["note"], repr(legacy["note"]))
check("窗口字段缺失（只有 usedTokens）也不收窄", ta.budgeted_read(body15, None, 0, window={"usedTokens": 900})["units"] == 4000)
check("窗口写 0 / 负数 / 非数字都按「没信息」处理", all(ta.budgeted_read(body15, None, 0, window=w)["units"] == 4000 for w in ({"contextWindowTokens": 0}, {"contextWindowTokens": -5}, {"contextWindowTokens": "很大"})))

wide = ta.budgeted_read(body15, None, 0, window={"contextWindowTokens": 400_000, "usedTokens": 100})
check("窗口宽到不用压时**一个字关于窗口的话都不说**（说了就是噪声）", wide["units"] == 4000 and "窗口" not in wide["note"] and wide["windowApplied"] is False, repr(wide["note"]))
narrow = ta.budgeted_read(body15, None, 0, window={"contextWindowTokens": 4096, "usedTokens": 3900})
check("剩余窗口逼出一页更小的正文", narrow["units"] < 4000 and narrow["windowApplied"] is True, repr(narrow["units"]))
check("说明里点名窗口、已用、留了多少余量、以及这是折算值", all(k in narrow["note"] for k in ("4096", "3900", "25", "折算")), repr(narrow["note"]))
check("说明承认这不是厂商 tokenizer 计数", "不是厂商" in narrow["note"], repr(narrow["note"]))
check("真少给了内容（不是只在 note 里说说），但**不按下限以下**给", len(narrow["text"]) == narrow["units"] and ta.WINDOW_PAGE_FLOOR_UNITS <= narrow["units"] < 4000, repr((len(narrow["text"]), narrow["units"])))
check("压小之后仍如实报后面还有多少、并给 start 指针", narrow["truncated"] is True and "start=" in narrow["note"], repr(narrow["note"]))
check("**只压不抬**：窗口剩很多也不给过固定上限 8000", ta.budgeted_read(body15, 100_000, 0, window={"contextWindowTokens": 400_000, "usedTokens": 0})["units"] == 8000)
zeroed = ta.budgeted_read(body15, None, 0, window={"contextWindowTokens": 4096, "usedTokens": 4096})
check("窗口按实测用满时按下限给一页，**不清零**（空页让模型反复申请，比撞穿更难诊断）", zeroed["units"] == ta.WINDOW_PAGE_FLOOR_UNITS and len(zeroed["text"]) > 0, repr(zeroed["units"]))
check("那种情况的理由说清「不是读完了」，把边界交回压缩层", "已经用满" in zeroed["note"] and "不是「读完了」" in zeroed["note"] and "压缩" in zeroed["note"], repr(zeroed["note"]))
past = ta.budgeted_read(body15, None, 0, window={"contextWindowTokens": 4096, "usedTokens": 99999})
check("used 超过窗口也照「用满」处理，不给出负数页", past["units"] == ta.WINDOW_PAGE_FLOOR_UNITS, repr(past["units"]))
check("used 读不到（0）时按满窗口算，不假装知道用量", ta.budgeted_read(body15, None, 0, window={"contextWindowTokens": 8192, "usedTokens": 0})["units"] == 4000)
half = ta.budgeted_read(body15, 8000, 0, window={"contextWindowTokens": 4096, "usedTokens": 2048})
check("模型申请 8000 而窗口只装得下半页时，夹取带话点名「超出上限」的那个上限是压后的值", "8000" not in half["note"] or str(half["units"]) in half["note"], repr(half["note"]))
check("这一页的实际字符数不超过说明里给的上限", len(half["text"]) <= half["units"])

section("取页：列表按真实条目长度折算")

items15 = [f"worker-{i:03d}/very/long/nested/path/to/some/file-{i}.txt" for i in range(50)]
plain_list = ta.budgeted_list(items15, None)
check("没窗口时列表仍是默认 20 条", plain_list["count"] == 20 and plain_list["windowApplied"] is False, repr(plain_list["count"]))
tight = ta.budgeted_list(items15, None, window={"contextWindowTokens": 4096, "usedTokens": 3800})
check("长条目 + 小窗口把列表压到 20 条以下", tight["count"] < 20 and tight["windowApplied"] is True, repr(tight["count"]))
check("压完仍说清共多少项、还有多少没列", "共 50 项" in tight["note"] and "项没列" in tight["note"], repr(tight["note"]))
tighter = ta.budgeted_list(items15, None, window={"contextWindowTokens": 4096, "usedTokens": 4090})
check("列表的下限按**条**给（拿正文那 200 个单元当列表下限，等于窗口再紧也压不动）", tighter["count"] == ta.WINDOW_PAGE_FLOOR_ITEMS, repr(tighter["count"]))
check("按条给下限也不清零", tighter["count"] > 0 and len(tighter["items"]) == tighter["count"], repr(tighter["count"]))
short_items = ["a", "b", "c"]
loose = ta.budgeted_list(items15, None, window={"contextWindowTokens": 200_000, "usedTokens": 0})
check("窗口宽到不用压时列表也不带话", loose["count"] == 20 and loose["windowApplied"] is False)
check("空列表的单条成本按 1 token 估（不除零、不编大值）", ta.list_unit_tokens([]) == 1.0 and ta.list_unit_tokens([""]) == 1.0)
measured = ta.list_unit_tokens(items15)
check("列表的单条成本按**真实平均长度**折算，不是写死的数", abs(measured - (sum(len(i) for i in items15) / len(items15)) / ta.TOKENS_TO_UNITS) < 1e-6, repr(measured))
cap_only = ta.window_budget(
    {"contextWindowTokens": 4096, "usedTokens": 4000},
    default=20,
    maximum=20,
    unit_tokens=ta.list_unit_tokens(items15),
    what="项",
    floor=ta.WINDOW_PAGE_FLOOR_ITEMS,
)
check("调用方拿 maximum=当前条数复用时，**下限也不越界**（压小反而变大是 bug）", cap_only[1] <= 20 and cap_only[0] <= 20, repr(cap_only))
check("那种复用下窗口紧就真压得动（按下限收到 3 条，而不是原地不动）", cap_only[1] < 20, repr(cap_only))
check("窗口给的是「按剩余」而不是「按固定上限」的说明文字", "剩余上下文窗口" in cap_only[2] or "已经用满" in cap_only[2], repr(cap_only[2]))
text_cap = ta.window_budget({"contextWindowTokens": 4096, "usedTokens": 4000}, default=4000, maximum=8000, unit_tokens=0.5, what="个单元")
check("正文那一面的下限仍是**单元**（200 个字符），两套下限不互相冒充", text_cap[1] == ta.WINDOW_PAGE_FLOOR_UNITS, repr(text_cap[1]))
check("下限都不许越过调用方给的 maximum（正文 8000、列表 50 都按得住）", ta.window_budget({"contextWindowTokens": 10, "usedTokens": 10}, default=4000, maximum=8000, unit_tokens=0.5)[1] == 200 and ta.window_budget({"contextWindowTokens": 10, "usedTokens": 10}, default=20, maximum=2, unit_tokens=25, what="项", floor=ta.WINDOW_PAGE_FLOOR_ITEMS)[1] == 2, repr("floor 越界"))

section("取页：窗口读数对象")

meter = ta.ReadWindowMeter()
check("还没填窗口的对象 reading() 回空（走固定预算）", meter.reading() == {}, repr(meter.reading()))
meter.note_usage(1000)
check("没有窗口时用量单独存在也不回读数（不拿半份事实去压页）", meter.reading() == {})
meter.configure(0, False)
check("configure(0) 不覆盖已有窗口，也不清空用量", meter.context_window == 0 and meter.used_tokens == 1000)
meter.configure(8192, True)
check("填齐之后 reading() 四个字段都在", meter.reading() == {"contextWindowTokens": 8192, "contextWindowFromModel": True, "usedTokens": 1000, "usageFromReport": True}, repr(meter.reading()))
meter.note_usage(600)
check("**读数取较大者**：上游报了个更小的残缺值不把已知用量冲掉", meter.used_tokens == 1000, repr(meter.used_tokens))
meter.note_usage(0)
check("报 0 等于没报：不动已有读数也不翻 fromReport 标记", meter.used_tokens == 1000 and meter.usage_from_report is True)
meter.note_usage(5000, from_report=False)
check("折算用量也记（并标 from_report=False），页面才分得清两种确定性", meter.used_tokens == 5000 and meter.usage_from_report is False, repr(meter.reading()))
meter.configure(4096, False)
check("窗口可以后填第二次（故障转移链算出更小窗口时以策略为准）", meter.context_window == 4096 and meter.from_model is False)
check("窗口为估算值时 reading 带 from_model=False", meter.reading()["contextWindowFromModel"] is False)
reading = meter.reading()
capped = ta.budgeted_read("y" * 9000, None, 0, window=reading)
check("读数直接喂给 budgeted_read 就生效（对象与函数之间不需要第二次换算）", capped["windowApplied"] is True and capped["units"] <= 4000, repr(capped["units"]))

# ── 16. 通道：窗口与用量的来源 ─────────────────────────────────
section("模型通道：窗口现读、折算只给取页不给台账")

chan_none = tmc.TicketModelChannel(None, env={})
check("没配模型时窗口按 context_budget 的估算值给出（不是 0，也不是猜的另一个数）", chan_none.config["contextWindow"] > 0 and chan_none.config["contextWindowFromModel"] is False, repr(chan_none.config.get("contextWindow")))
check("估算窗口时如实标 from_model=False", chan_none.window_state()["contextWindowFromModel"] is False)
chan_filled = tmc.TicketModelChannel({"baseUrl": "https://x.invalid/v1", "apiKey": "k", "modelName": "m", "contextWindow": 128000}, env={})
check("填了窗口就用填的，并标 from_model=True", chan_filled.config["contextWindow"] == 128000 and chan_filled.config["contextWindowFromModel"] is True, repr(chan_filled.config.get("contextWindow")))
check("非法窗口值（超范围）退回估算并说清", tmc.TicketModelChannel({"baseUrl": "https://x/v1", "apiKey": "k", "contextWindow": 5}, env={}).config["contextWindowFromModel"] is False)
chan_small = tmc.TicketModelChannel({"baseUrl": "https://x.invalid/v1", "apiKey": "k", "contextWindow": 1024}, env={})
check("窗口口径与压缩策略同源（同一个 context_budget.resolve，不另算一套）", chan_small.config["contextWindow"] == __import__("context_budget").resolve(None, {"contextWindow": 1024}).context_window)

seen_usage: list = []
transport = lambda body, *, url, api_key, timeout: {"choices": [{"message": {"content": "收到", "tool_calls": []}}]}  # noqa: E731  没有 usage 的端点
chan_nousage = tmc.TicketModelChannel({"baseUrl": "https://x.invalid/v1", "apiKey": "k", "modelName": "m", "contextWindow": 8192}, chat=transport, env={}, on_usage=lambda u: seen_usage.append(u))
reply_nc = chan_nousage({"system": "目标", "messages": [{"role": "user", "content": "干活"}]})
check("上游没回 usage 时**一条也不写进用量台账**（既有口径不许被取页需求带跑）", seen_usage == [], repr(seen_usage))
check("但取页读数照样有：本地按字符折算的 promptTokens", chan_nousage.last_usage["promptTokens"] > 0 and chan_nousage.last_usage["promptTokensFromReport"] is False, repr(chan_nousage.last_usage))
check("折算值明确标出来（页面不能把它说成实测）", chan_nousage.window_state()["usageFromReport"] is False)
transport2 = lambda body, *, url, api_key, timeout: {"choices": [{"message": {"content": "收到"}}], "usage": {"prompt_tokens": 4321, "completion_tokens": 7, "total_tokens": 4328}}  # noqa: E731
CHAN_KEY = "sk-CHANNELKEYDONTLEAK7"
chan_usage = tmc.TicketModelChannel({"baseUrl": "https://x.invalid/v1", "apiKey": CHAN_KEY, "contextWindow": 8192}, chat=transport2, env={}, on_usage=lambda u: seen_usage.append(u))
chan_usage({"system": "s", "messages": [{"role": "user", "content": "u"}]})
check("上游回了 usage 时台账收到原值（4321 不被折算值替换）", seen_usage and seen_usage[-1]["promptTokens"] == 4321, repr(seen_usage[-1]))
check("台账那份记录里没有自造字段（promptTokensFromReport 只活在通道内部）", "promptTokensFromReport" not in seen_usage[-1], repr(seen_usage[-1]))
check("取页读数用的是实测值并标 from_report=True", chan_usage.window_state()["usedTokens"] == 4321 and chan_usage.window_state()["usageFromReport"] is True, repr(chan_usage.window_state()))
check("window_state 的键形状就是取页函数吃的那个（contextWindowTokens/usedTokens）", {"contextWindowTokens", "usedTokens"} <= set(chan_usage.window_state()), repr(sorted(chan_usage.window_state())))
d = chan_usage.describe()
check("describe 里看得见窗口与本轮用量（状态面答得出取页有没有生效）", d["contextWindow"] == 8192 and d["lastPromptTokens"] == 4321, repr(d))
check("describe 仍然一个密钥也没有（长键原样不出现，也不带 apiKey 这个字段名）", CHAN_KEY not in json.dumps(d, ensure_ascii=False) and "apiKey" not in json.dumps(d, ensure_ascii=False), repr(d))
chan_pushed = tmc.TicketModelChannel({"baseUrl": "https://x.invalid/v1", "apiKey": "k", "contextWindow": 4096}, env={})
chan_pushed.set_model_config({"baseUrl": "https://y.invalid/v1", "apiKey": "k2", "contextWindow": 200000})
check("换模型配置时窗口跟着现读（旧窗口不会赖在装配期快照上）", chan_pushed.config["contextWindow"] == 200000 and chan_pushed.window_state()["contextWindowTokens"] == 200000, repr(chan_pushed.config.get("contextWindow")))
injected_state = {"contextWindowTokens": 0}
check("注入式模型（没有通道）时窗口读数回空，工具走固定预算", ta.budgeted_read("z" * 5000, None, 0, window=injected_state)["units"] == 4000)

# ── 17. 工具箱与服务的取页接线 ────────────────────────────────
section("工具箱：窗口读数接到实现，而不是只接在提示词里")

use_home("17")
meter17 = ta.ReadWindowMeter(8192, from_model=True)  # 窗口填了、用量还没报过：这一页不该被窗口压
svc17 = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, prepare_env={})
svc17.toolbox.set_window_provider(lambda: meter17.reading())
path17, err17 = svc17.workspace.prepare("tk17", "取页单", None)
big = (path17 / "big.txt")
big.write_text("q" * 9000, encoding="utf-8")
for i in range(60):
    (path17 / f"diritem-{i:02d}-with-a-long-name.txt").write_text("x", encoding="utf-8")
with svc17.toolbox.turn("tk17"):
    full = svc17.toolbox.read_file("big.txt", 4000, 0)
    many = svc17.toolbox.list_dir(".", 20)
    meter17.note_usage(7900)
    tight = svc17.toolbox.read_file("big.txt", 4000, 0)
    meter17.note_usage(8100)
    few = svc17.toolbox.list_dir(".", 20)
check("窗口宽到不用压时按固定预算给 4000 字符，且不带窗口那句话", full["units"] == 4000 and full["windowApplied"] is False, repr(full["units"]))
check("窗口快满时同一份文件给得更少（实现真的读到了本轮读数）", tight["units"] < full["units"] and tight["windowApplied"] is True, repr((tight["units"], full["units"])))
check("少给之后 truncated 与 start 指针照旧说清（不是默默截一半）", tight["truncated"] is True and "start=" in tight["note"], repr(tight["note"]))
check("列表同样吃窗口（长名字 + 满窗口 ⇒ 条目更少）", few["count"] < many["count"] and many["count"] == 20, repr((few["count"], many["count"])))
check("工具箱的窗口来源是回调不是快照（用量在两页之间涨上去，下一页当场生效）", few["windowApplied"] is True and many["windowApplied"] is False, repr(many["note"]))
check("翻页指针不因窗口而失真（少给也要指出下一段从哪儿读）", "start=" in few.get("note", "") or few["truncated"] is True, repr(few.get("note")))
transport17 = lambda body, *, url, api_key, timeout: {"choices": [{"message": {"content": "收口", "tool_calls": []}}]}  # noqa: E731  不回 usage 的端点
svc17p = tws.TicketServiceRuntime(
    model_config={"baseUrl": "https://x.invalid/v1", "apiKey": "k17", "modelName": "m", "contextWindow": 8192},
    chat=transport17,
    send_fn=False,
    thread_factory=_NoThread,
    prepare_env={},
)
check("带通道的装配：窗口读数来自通道本身（不是给工具箱单独编一份）", svc17p.channel is not None and svc17p.window_state()["contextWindowTokens"] == 8192, repr(svc17p.window_state()))
check("服务给的窗口读数与工具箱读到的是同一份（接线不是摆设）", svc17p.toolbox.current_window() == svc17p.window_state(), repr(svc17p.toolbox.current_window()))
before17 = svc17p.paging_state()
check("一次模型调用之前按满窗口算，并说清「还没发生过一次调用」", before17["mode"] == "window-no-usage" and before17["usedTokens"] == 0 and "还没发生过一次模型调用" in before17["note"], repr(before17))
svc17p.model_call({"system": "目标", "messages": [{"role": "user", "content": "干活"}]})
paging17 = svc17p.paging_state()
check("paging_state 说出窗口与用量，并标明窗口是填的还是估的", paging17["mode"] == "window" and paging17["contextWindowTokens"] == 8192 and paging17["windowFromModel"] is True, repr(paging17))
check("上游没回 usage 时 note 说「按字符折算」（把确定性说实话）", paging17["usageFromReport"] is False and "折算" in paging17["note"], repr(paging17))
transport17b = lambda body, *, url, api_key, timeout: {"choices": [{"message": {"content": "收口"}}], "usage": {"prompt_tokens": 500, "completion_tokens": 1, "total_tokens": 501}}  # noqa: E731
svc17q = tws.TicketServiceRuntime(
    model_config={"baseUrl": "https://x.invalid/v1", "apiKey": "k17", "contextWindow": 8192},
    chat=transport17b,
    send_fn=False,
    thread_factory=_NoThread,
    prepare_env={},
)
svc17q.model_call({"system": "目标", "messages": [{"role": "user", "content": "干活"}]})
reported17 = svc17q.paging_state()
check("上游回了 usage 时 note 改口「取上游回报」，用的就是回报值", reported17["usageFromReport"] is True and reported17["usedTokens"] == 500 and "取上游回报" in reported17["note"], repr(reported17))
check("注入 model_call 的装配（没有通道）如实报 fixed，不假装在按窗口收窄", svc17.paging_state()["mode"] == "fixed" and "固定预算" in svc17.paging_state()["reason"], repr(svc17.paging_state()))
status17 = svc17.status()
check("状态面带 paging 这一块且能原样出 JSON", "paging" in status17 and json.loads(json.dumps(status17["paging"], ensure_ascii=False)) == status17["paging"], repr(list(status17)))
status17p = svc17p.status()
check("状态面同时带 paging 与 prepare（一面看得全，不必两个端点拼）", "paging" in status17p and "prepare" in status17p, repr(sorted(status17p)))
check("注入 model_call 的装配（没有通道）不报错：channel 是 None 这一事实自己说得出", svc17.channel is None)

injected_box = tws.TicketToolbox(workspace=svc17.workspace)
svc_inj = tws.TicketServiceRuntime(model_call=OkModel(), send_fn=False, thread_factory=_NoThread, toolbox=injected_box, prepare_env={})
check("自带工具箱的装配也接上窗口读数（否则最像真机的那条路不生效）", injected_box.window_provider is not None and injected_box.current_window() == svc_inj.window_state(), repr(injected_box.current_window()))


class BadProvider:
    def __call__(self) -> dict:
        raise RuntimeError("通道正在被换掉（用例设定）")


broken_box = tws.TicketToolbox(workspace=svc17.workspace, window_provider=BadProvider())
check("窗口回调抛了不影响读取：current_window 把异常吞成一条留话", "unreadable" in broken_box.current_window(), repr(broken_box.current_window()))
broken_box2 = tws.TicketToolbox(workspace=svc17.workspace, window_provider=lambda: "不是字典")
check("回调返回怪形状也当没有窗口（不拿它去压页）", broken_box2.current_window() == {})
with broken_box.turn("tk17"):
    still_reads = broken_box.read_file("big.txt", 4000, 0)
check("读数坏了照样给固定预算那一页，不报错也不空手", still_reads["units"] == 4000 and len(still_reads["text"]) > 0, repr(still_reads["note"]))

# ── 18. 三面同源的机器核对 ─────────────────────────────────────
section("同源：取页的算术只有一份，三面都调它")

adapter_src = (PROJECT_ROOT / "engine" / "ticket_worker_adapter.py").read_text(encoding="utf-8")
service_src = (PROJECT_ROOT / "engine" / "ticket_worker_service.py").read_text(encoding="utf-8")
server_src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
check("预算数字只有一份来源（服务与主会话都引 twa 的常量，不各自写字面量）", "twa.LIST_DEFAULT_ITEMS" in server_src and "LIST_DEFAULT_ITEMS = 20" not in service_src and "TEXT_READ_MAX_UNITS = 8000" not in server_src)
check("服务侧两个工具都走 ta.budgeted_* 并带 window=", service_src.count("ta.budgeted_read(body, units, start, window=self.current_window())") == 1 and "ta.budgeted_list(names, limit, window=self.current_window())" in service_src)
check(
    "主会话三个读取件都吃到本轮窗口（两个正文件把读数内联传进 budgeted_read，列表件先取成变量再算）",
    server_src.count("window=_window_reading()") == 2 and "reading = _window_reading()" in server_src,
    repr((server_src.count("window=_window_reading()"), "reading = _window_reading()" in server_src)),
)
check("主会话列表用同一个 window_budget，不另写一套算术", "twa.window_budget(" in server_src and "def window_budget" not in server_src)
check("单条成本用同一个 list_unit_tokens", "twa.list_unit_tokens(" in server_src)
check("通道给工具面的窗口形状由 window_state 一处产出", "def window_state" in (PROJECT_ROOT / "engine" / "ticket_model_channel.py").read_text(encoding="utf-8"))
check("主会话的窗口读数来自策略（压缩与取页同一个 context_window）", "read_window.configure(policy.context_window, policy.context_window_from_model)" in server_src)
check("用量回调先记账再转发：包一层不改变台账行为", "_usage_capture_for_window(on_usage, read_window)" in server_src and "meter.note_usage" in server_src)
check("折算值进不了台账：通道里台账那份与取页那份是两个字典", "ledger_record" in (PROJECT_ROOT / "engine" / "ticket_model_channel.py").read_text(encoding="utf-8") and "promptTokens=reported or self._estimate_prompt_tokens(body, system)" in (PROJECT_ROOT / "engine" / "ticket_model_channel.py").read_text(encoding="utf-8"))
check("窗口常量也只有一份（Rust 侧那半的对照见 harness/tools.rs 的 const，本刀不动 Rust）", ta.TOKENS_TO_UNITS == 2 and ta.WINDOW_PAGE_FLOOR_UNITS == 200 and 0 < ta.WINDOW_RESERVE_FRACTION < 1)
check("TOKENS_TO_UNITS 的口径与 context_budget 的估算不互相冒充：本层叫「折算」不叫「精确」", "折算" in adapter_src and "不是厂商" in adapter_src)
# Rust 那一半（harness/tools.rs 的 ToolBox 构造）是并行批次领地，本刀不写；这条检查把
# 「未做」钉在盘上而不是钉在注释里：等对方接上窗口，这条会红，提示本文件补真正的用例。
rust_tools_src = (PROJECT_ROOT / "src-tauri" / "src" / "harness" / "tools.rs").read_text(encoding="utf-8")
check("Rust harness 侧今天仍是固定预算（未做就如实未做，不许在本文件里被说成已接）", "context_window" not in rust_tools_src and "window_budget" not in rust_tools_src)
check("Rust 读取件与 Python 同一套分页词汇（start/offset 的提示语同源，取页生效时不会两套说法）", "接着读请带 start=" in rust_tools_src or "接着读请带 offset=" in rust_tools_src)

print("-" * 52)
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("PASS: 工单工作区准备与按剩余窗口取页全部检查通过")
sys.exit(0)
