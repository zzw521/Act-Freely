"""工单工作区的真实准备动作（缺陷 D-48.8 / #150，引擎侧）

D-48.1 起，`TicketWorkspace.prepare` 就留着 `prepare_fn` 这个注入位，注释写着「典型是 git
worktree，由调用方注入」——但到今天**没有任何代码注入过它**：全仓 `prepare_fn` 的实参只有
测试里的假回调，真实装配（`ticket_worker_service`）传的是 `None`。结果是工单执行体拿到的永远
是一个空目录，「先准备再干活」这一步只存在于台账里。DSH 那一侧是「目录不存在时先跑一次
prepare 命令（典型是 `git worktree`），十分钟超时，失败即工单失败」。

本模块把这一步变成真件，形状定成三条：

1. **只收三种规格，不收「随便一条命令字符串」**。`repo`（本地仓 → `git worktree add --detach`）、
   `url`（远端仓 → `git clone --depth`）、`command`（**argv 列表**，直接 exec，不经 shell）。
   把一条字符串交给 shell 解释，就等于给「准备工作区」这件事开一个任意代码执行面；这条路径
   按设计**不经 HTTP**（见下），所以它的注入面应当是零，而不是「指望调用方自觉」。
2. **只由进程内供给，绝不从请求体取**。准备动作决定执行体在哪个目录里跑命令，端点面把它
   开放出去等于让远程主体挑目录——那正是 D-48.7 接线时刻意不做的理由。因此规格的来源是桌面
   runtime 载荷 / `configure()`，`server.py` 的 `/api/tickets/*` 里没有一条能带它。
3. **失败要说得清，还要留下一次性能重试的现场**。DSH 那句「失败即工单失败」这边由
   `TicketWorkspace.prepare` 承担（领取置 `lost`、唤醒留在收件箱），本模块负责另一半：
   把退出码与 stderr 尾巴原样带回（**凭据先打掉**），并把目录里的半成品收干净。

最后一条是探针打出来的，不是推理出来的：`git worktree add` 登记过之后再删目录，源仓会留下
「missing but already registered worktree」的悬空登记，同一个路径重试第二次准备**必然**再撞一次
`rc=128`——所以失败清理的顺序是**先删目录、后 `git worktree prune`**，反过来挡不住。
`cleanup()` 就是为这个顺序而存在的口，由运行时在收完盘之后调用。

超时缺省 600 秒（DSH 的十分钟），夹在 [1, 3600]；`GIT_TERMINAL_PROMPT=0` 一律注入，否则
私有仓的 `clone` 会卡在交互式口令上，用户等到十分钟才看见一条「准备失败」——那十分钟里槽位
是被占着的。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

#: DSH 的「十分钟超时」；上限一小时是防止有人拿它当「后台常驻任务」的载体（那是看门狗的活）
DEFAULT_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600
#: 回话里带的 stderr 尾巴长度：够看清 git 在抱怨什么，又不至于把一条异常撑成一篇日志
STDERR_TAIL_CHARS = 600
#: prune 自己也要有界：它在失败清理路径上，不能再拖住一条已经失败的回话
CLEANUP_TIMEOUT_SECONDS = 30
#: 「这是不是一个 git 仓」的探针：本地一条 `rev-parse`，不该跟着准备的十分钟超时走
PROBE_TIMEOUT_SECONDS = 60

KIND_REPO = "repo"
KIND_URL = "url"
KIND_COMMAND = "command"
KNOWN_KINDS = (KIND_REPO, KIND_URL, KIND_COMMAND)

#: `git` 可执行名：走 PATH，与引擎既有口径一致（`sandbox_launcher` 也是找 PATH 上的件）
GIT_EXECUTABLE = "git"

_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SSH_REMOTE = re.compile(r"^[^/\s]+@[^/\s]+:.+")
#: 命令行 argv 里出现这些字符说明有人想把 argv 当 shell 语句用；直接拒，不解释
_SHELL_METACHARS = frozenset("&|;<>$\n\r")


class WorkspacePrepareError(RuntimeError):
    """准备失败。中文理由点名「哪一步、什么命令、退出码、stderr 尾巴」，不演成一句「准备失败」。"""


def clamp_timeout(value: Any, *, default: int = DEFAULT_TIMEOUT_SECONDS) -> int:
    """把超时夹进 [MIN, MAX]；认不出来就取缺省。

    这里**不**如实带话而是直接取数：它是装配期的配置读数，不是模型的一次请求，
    模型看不到也不该看到「你写的 99999 被夹到 3600」——那条纪律属于分页预算（`clamp_units`）。"""
    try:
        seconds = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return int(default)
    if seconds <= 0:
        return int(default)
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, seconds))


def redact_secrets(text: Any) -> str:
    """打掉一份文本里的凭据形状：URL 内嵌的 ``user:token@host`` 与 ``--password=X`` 这类。

    clone 的远端地址最容易被写成 `https://user:pat@host/repo.git`，而失败理由要原样进回话与
    状态面——不打掉就等于把令牌抄进引擎的日志与工单快照里。这里只做**形状**替换，
    不试图判断「这串到底是不是密钥」。"""
    out = str(text or "")
    out = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@", r"\1\2:***@", out)
    out = re.sub(r"(?i)(--(?:password|passwd|token|apikey|api-key|access-token|secret)[= ])(\S+)", r"\1***", out)
    return out


def _is_remote_location(raw: str) -> bool:
    """像不像一个「要 clone 的远端」：带协议的 URL，或 `git@host:path` 这种 scp 式地址。"""
    return bool(_URL_SCHEME.match(raw) or _SSH_REMOTE.match(raw))


def normalize_spec(spec: Any) -> dict[str, Any]:
    """把「一份准备规格」收敛成 `{kind, ...}`，不接受任何需要 shell 解释的写法。

    收的形状：
      * 一串路径 / URL / `git@host:path` → 自动判 `repo` 还是 `url`
      * `{"repo": 路径, "ref": 可选}`、`{"url": 地址, "depth": 可选}`、`{"command": [argv...]}`
      * argv 列表（等价于 `{"command": [...]}`）

    拒的形状（都带中文理由抛 `ValueError`，因为这是**装配期**的错误，越早越好）：
      空规格、既不认识 kind 也没有可用字段、`command` 是字符串、argv 里有 shell 控制字符。"""
    if spec is None:
        raise ValueError("没有给出工单工作区的准备规格（repo / url / command 三选一）")
    if isinstance(spec, (list, tuple)):
        return _command_spec(list(spec), {})
    if isinstance(spec, str):
        raw = spec.strip()
        if not raw:
            raise ValueError("准备规格是空字符串——要么给本地仓路径，要么给远端地址，要么给 argv 列表")
        if _is_remote_location(raw):
            return {"kind": KIND_URL, "url": raw, "depth": 1, "ref": "", "timeoutSeconds": None}
        return {"kind": KIND_REPO, "repo": raw, "ref": "", "timeoutSeconds": None}
    if not isinstance(spec, dict):
        raise ValueError(f"准备规格只能是字符串 / argv 列表 / 字典，收到的是 {type(spec).__name__}")
    body = dict(spec)
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in KNOWN_KINDS:
        # 没写 kind 就按字段猜一次；猜不出来就报错，不默认成「建空目录」——那是把没配当配好了
        if body.get("repo"):
            kind = KIND_REPO
        elif body.get("url"):
            kind = KIND_URL
        elif body.get("command"):
            kind = KIND_COMMAND
        else:
            raise ValueError(
                "准备规格不认识：kind 只能是 repo / url / command，"
                "或者直接给 repo / url / command 三个字段之一"
            )
    if kind == KIND_REPO:
        repo = str(body.get("repo") or "").strip()
        if not repo:
            raise ValueError("repo 类准备规格必须有 repo 字段（本地仓路径）——写的是「从哪份仓准备」，空的路径准备不出任何东西")
        if _is_remote_location(repo):
            raise ValueError(f"repo 字段要的是本地仓路径，给的是远端地址：{redact_secrets(repo)}——请用 url 类规格")
        return {"kind": KIND_REPO, "repo": repo, "ref": str(body.get("ref") or body.get("branch") or "").strip(), "timeoutSeconds": body.get("timeoutSeconds")}
    if kind == KIND_URL:
        url = str(body.get("url") or "").strip()
        if not url:
            raise ValueError("url 类准备规格必须有 url 字段（可 clone 的远端地址）——要是不打算 clone 远端仓，请改用 repo 或 command 类规格")
        if not _is_remote_location(url):
            raise ValueError(f"url 字段要的是远端地址，看着像本地路径：{url}——本地仓请用 repo 类规格（git worktree）")
        raw_depth = body.get("depth")
        if raw_depth is None or (isinstance(raw_depth, str) and not raw_depth.strip()):
            raw_depth = 1
        try:
            depth = int(raw_depth)
        except (TypeError, ValueError):
            raise ValueError("depth 只能是整数（浅克隆的层数，缺省 1）") from None
        if depth <= 0:
            raise ValueError(f"depth「{depth}」不是正整数——git 的浅克隆没有「0 层」这种写法，想要完整历史就把 depth 设为一个够大的层数")
        return {"kind": KIND_URL, "url": url, "depth": min(depth, 200), "ref": str(body.get("ref") or "").strip(), "timeoutSeconds": body.get("timeoutSeconds")}
    return _command_spec(body.get("command"), body)


def _command_spec(argv: Any, body: dict[str, Any]) -> dict[str, Any]:
    """argv 列表的检查：非空、每项是非空字符串、没有 shell 控制字符、每项都不是「一条子命令」。"""
    if isinstance(argv, str) or not isinstance(argv, (list, tuple)):
        raise ValueError(
            "command 类准备规格要的是 **argv 列表**（如 [\"git\", \"clone\", \"...\"]），不是字符串——"
            "把一条字符串交给 shell 解释，等于给「准备工作区」开一个任意代码执行面"
        )
    parts = [str(item) for item in argv]
    if not parts or not any(p.strip() for p in parts):
        raise ValueError("command 的 argv 是空的——没有要跑的准备命令就别用 command 类规格")
    for part in parts:
        hit = sorted({ch for ch in part if ch in _SHELL_METACHARS})
        if hit:
            raise ValueError(
                f"command 的 argv 里出现 shell 控制字符 {hit}（在「{redact_secrets(part)[:80]}」里）——"
                "本模块直接 exec 不经过 shell，这些字符不会被解释，出现它多半是想写管道或重定向；"
                "要那种能力请改用脚本文件，argv 只写「python 某个脚本」"
            )
        if not part.strip():
            raise ValueError("command 的 argv 里有空串——空串会被 exec 当成一个真实参数，请去掉")
    return {
        "kind": KIND_COMMAND,
        "argv": parts,
        "cwd": str(body.get("cwd") or "").strip(),
        "appendTarget": bool(body.get("appendTarget")),
        "timeoutSeconds": body.get("timeoutSeconds"),
    }


def _digest(joined: str) -> str:
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:12]


#: 不进来源指纹的字段：**换超时不是换来源**。同一个仓把准备从 600 秒调成 900 秒，目录里
#: 那份代码一个字都没变，若指纹跟着走，下一次唤醒会撞上「这目录是按另一份来源准备的」——
#: 一个纯粹的配置读数把在跑的工单判死，是这一层最不该犯的事。
_NON_PROVENANCE_KEYS = frozenset({"timeoutSeconds"})


def _fingerprint_of(spec: dict[str, Any]) -> str:
    source = {key: value for key, value in spec.items() if key not in _NON_PROVENANCE_KEYS}
    return _digest(json.dumps(source, ensure_ascii=False, sort_keys=True))


class WorkspacePreparer:
    """一份可执行、可指纹化、可如实自述的准备规格。

    它同时是 `TicketWorkspace.prepare` 要的 `prepare_fn`（`__call__(path)`），并且多带两个
    运行时按名字借用的口：`fingerprint`（工作区来源的凭据，写进准备标记）与 `cleanup(path)`
    （运行时收完半成品之后调用，用来清 git 那边的悬空登记）。
    """

    def __init__(
        self,
        spec: Any,
        *,
        timeout_seconds: Any = None,
        run: Callable[..., Any] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.spec = normalize_spec(spec)
        raw_timeout = timeout_seconds if timeout_seconds is not None else self.spec.get("timeoutSeconds")
        self.timeout_seconds = clamp_timeout(raw_timeout)
        self._run = run or subprocess.run
        self._env = env
        #: 观测读数（不是账本）：给状态面看「这轮准备跑了什么、结果如何」
        self.last_error = ""
        self.prepared = 0

    # ── 自述 ──────────────────────────────────────────────────
    @property
    def kind(self) -> str:
        return str(self.spec.get("kind") or "")

    @property
    def fingerprint(self) -> str:
        """来源指纹：同一工单目录只认同一个指纹（见 `TicketWorkspace.prepare` 的比对）。

        只算来源字段（仓/地址/argv/ref/深度/工作目录），`timeoutSeconds` 不参与——调超时
        改的是「等多久」，不是「在哪份代码上干活」。"""
        return _fingerprint_of(self.spec)

    def label(self) -> str:
        """给人看的那一句「从哪儿准备」——凭据已打掉，可以进状态面。"""
        spec = self.spec
        if self.kind == KIND_REPO:
            ref = f"@{spec['ref']}" if spec.get("ref") else ""
            return f"本地仓 {redact_secrets(spec['repo'])}{ref}"
        if self.kind == KIND_URL:
            return f"远端仓 {redact_secrets(spec['url'])}（浅克隆 {spec.get('depth', 1)} 层）"
        return " ".join(redact_secrets(part) for part in spec.get("argv", []))

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label(),
            "fingerprint": self.fingerprint,
            "timeoutSeconds": self.timeout_seconds,
            "lastError": self.last_error,
            "prepared": self.prepared,
        }

    # ── 执行 ──────────────────────────────────────────────────
    def _environ(self) -> dict[str, str]:
        base = dict(os.environ) if self._env is None else dict(self._env)
        # 私有仓的口令提示会把一次准备拖成十分钟的死等（槽位在这十分钟里是占着的），
        # 所以这里不是「可选优化」：不允许交互式问口令，让它立刻失败并回原因。
        base["GIT_TERMINAL_PROMPT"] = "0"
        # 用赋值而不是 setdefault：桌面机器上 `GIT_ASKPASS` 常常已经被 Git for Windows 指到
        # 图形口令框了，留着它等于让准备动作弹一个没人会点的窗——槽位照样被占十分钟。
        base["GIT_ASKPASS"] = ""
        return base

    def _exec(self, argv: list[str], *, cwd: Path | None, timeout: float) -> tuple[int, str, str]:
        try:
            done = self._run(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._environ(),
            )
        except subprocess.TimeoutExpired:
            return -1, "", f"超过 {int(self.timeout_seconds)} 秒没有结束（已按超时中止）"
        except FileNotFoundError:
            return -2, "", f"找不到可执行文件「{argv[0]}」——准备动作直接 exec，不经 shell，也不做替代猜测"
        except OSError as exc:
            return -3, "", f"{type(exc).__name__}: {redact_secrets(exc)}"
        rc = int(getattr(done, "returncode", 0) or 0)
        out = str(getattr(done, "stdout", "") or "")
        err = str(getattr(done, "stderr", "") or "")
        return rc, out, err

    @staticmethod
    def _tail(text: str) -> str:
        cleaned = redact_secrets(text or "").strip()
        return cleaned[-STDERR_TAIL_CHARS:]

    def _fail(self, step: str, rc: int, err: str) -> None:
        reason = f"工单工作区准备失败（{step}）：退出码 {rc}"
        tail = self._tail(err)
        if tail:
            reason = f"{reason}；输出尾部：{tail}"
        self.last_error = reason
        raise WorkspacePrepareError(reason)

    def __call__(self, path: Any) -> None:
        target = Path(str(path))
        if self.kind == KIND_REPO:
            self._prepare_repo(target)
        elif self.kind == KIND_URL:
            self._prepare_url(target)
        else:
            self._prepare_command(target)
        self.prepared += 1
        self.last_error = ""

    def _prepare_repo(self, target: Path) -> None:
        repo = Path(str(self.spec["repo"]))
        if not repo.exists():
            self.last_error = f"工单工作区准备失败：本地仓不存在（{redact_secrets(repo)}）"
            raise WorkspacePrepareError(self.last_error)
        if not repo.is_dir():
            self.last_error = f"工单工作区准备失败：repo 指向的不是目录（{redact_secrets(repo)}）"
            raise WorkspacePrepareError(self.last_error)
        probe, _, probe_err = self._exec([GIT_EXECUTABLE, "-C", str(repo), "rev-parse", "--git-dir"], cwd=None, timeout=float(PROBE_TIMEOUT_SECONDS))
        if probe != 0:
            self.last_error = (
                f"工单工作区准备失败：{redact_secrets(repo)} 不是一个 git 仓（`rev-parse` 退出码 {probe}"
                + (f"；输出尾部：{self._tail(probe_err)}）" if self._tail(probe_err) else "）")
            )
            raise WorkspacePrepareError(self.last_error)
        argv = [GIT_EXECUTABLE, "-C", str(repo), "worktree", "add", "--detach"]
        if self.spec.get("ref"):
            argv.append(str(self.spec["ref"]))
        argv.append(str(target))
        rc, _, err = self._exec(argv, cwd=None, timeout=float(self.timeout_seconds))
        if rc != 0:
            # 登记可能已经写进源仓（`add` 失败也登记过的情形实测存在），失败理由里先说清
            # 「要重试请先让 cleanup() 跑过」；真正的清理由运行时按「先删目录后 prune」的顺序调用。
            self._repo = repo  # 供 cleanup() 使用
            self._fail("git worktree add", rc, err)

    def _prepare_url(self, target: Path) -> None:
        argv = [GIT_EXECUTABLE, "clone", "--depth", str(self.spec.get("depth", 1))]
        if self.spec.get("ref"):
            argv += ["--branch", str(self.spec["ref"])]
        argv += ["--", str(self.spec["url"]), str(target)]
        rc, _, err = self._exec(argv, cwd=None, timeout=float(self.timeout_seconds))
        if rc != 0:
            self._fail("git clone", rc, err)

    def _prepare_command(self, target: Path) -> None:
        spec = self.spec
        argv = list(spec.get("argv", []))
        if spec.get("appendTarget"):
            argv.append(str(target))
        cwd_raw = str(spec.get("cwd") or "").strip()
        cwd = Path(cwd_raw) if cwd_raw else target
        if not cwd.is_dir():
            self.last_error = f"工单工作区准备失败：命令的工作目录不存在（{redact_secrets(cwd)}）"
            raise WorkspacePrepareError(self.last_error)
        rc, _, err = self._exec(argv, cwd=cwd, timeout=float(self.timeout_seconds))
        if rc != 0:
            self._fail(" ".join(redact_secrets(part) for part in spec.get("argv", [])), rc, err)

    # ── 失败之后的现场 ────────────────────────────────────────
    def cleanup(self, path: Any) -> str:
        """运行时**删掉半成品目录之后**调用：清掉源仓里那条悬空 worktree 登记。

        顺序是有原因的（实测）：目录还在时 `prune` 不会动那条登记，下一次准备撞
        「missing but already registered worktree」`rc=128`。返回一句中文说明（成功或失败），
        调用方把它并进准备失败的理由里——清不掉也要如实说，不能假装现场是干净的。
        """
        if self.kind != KIND_REPO:
            return ""
        repo = Path(str(getattr(self, "_repo", "") or self.spec.get("repo") or ""))
        if not repo.is_dir():
            return f"（没能清理：本地仓路径不可用：{redact_secrets(repo)}）"
        rc, _, err = self._exec([GIT_EXECUTABLE, "-C", str(repo), "worktree", "prune"], cwd=None, timeout=CLEANUP_TIMEOUT_SECONDS)
        if rc != 0:
            return f"（worktree prune 自己失败，退出码 {rc}：{self._tail(err)}；同一目录重试可能撞「already registered」）"
        return "（已在源仓 prune 掉悬空登记，同一目录可以再跑一次准备）"


def build_preparer(spec: Any, **kwargs: Any) -> tuple[WorkspacePreparer | None, str]:
    """装配用的安全入口：规格不对就回 `(None, 中文原因)`，不抛穿到调用方。

    为什么不让它抛：这份规格来自桌面 runtime 载荷，那是**用户能改的东西**。装配期一颗未捕的
    `ValueError` 会把整个工单服务起不来（IM 与聊天面跟着一起没），而「准备规格没配好」的正常
    结局应该是「本轮不准备、状态面说清为什么」。"""
    try:
        return WorkspacePreparer(spec, **kwargs), ""
    except ValueError as exc:
        return None, redact_secrets(exc)
    except (TypeError, OSError) as exc:  # 载荷形状千奇百怪（dict 里塞了 Path 之类）
        return None, f"准备规格读不出来：{type(exc).__name__}: {redact_secrets(exc)}"


def remove_created_tree(path: Path, *, root: Path) -> tuple[bool, str]:
    """运行时专用：删掉**本次自己创建**的半成品目录，但只在数据根之下才动手。

    三个条件一条不松：路径必须在 `root` 之下、必须是目录、必须刚由本次 prepare 创建。
    删别人建好的目录不是「清理」，是「把用户的东西当垃圾」——所以这里宁可留下半成品，
    也要把留下的事实说出来（返回的中文说明会并进失败理由）。
    """
    try:
        resolved = Path(path).resolve()
        base = Path(root).resolve()
    except OSError as exc:
        return False, f"（半成品目录路径判不出来，没删：{redact_secrets(exc)}）"
    if base == resolved or base not in resolved.parents:
        return False, "（半成品目录不在 worker 根之下，拒绝删除）"
    if not resolved.is_dir():
        return True, ""
    shutil.rmtree(resolved, ignore_errors=True)
    if resolved.exists():
        return False, f"（半成品目录删不干净，仍留在 {resolved.name}：多半是有进程还在里面占着文件）"
    return True, ""
