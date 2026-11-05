"""工单执行体的进程内服务装配（缺陷 D-48.7）

D-48.1 到 D-48.6 把六个引擎件各自做成了可独立测试的运行时（注册表 / 副作用队列 / 恢复
注记 / 适配器 / 并发调度 / 重启补查 / 耐久收件箱），但它们从来没有**在一个进程里一起活过**：
每一层的自测都自己装配自己那一套，端点接线时才是它们第一次互相看见。本模块把这件事先在
非竞争区做完——`server.py` 的路由只需要一行包装调这里的方法，接线时不必现场拼口径。

三件事是这一层独有的，下面两层都给不出：

1. **本轮工单目录**。`read_file` / `write_file` / `list_dir` / `run_shell` 的真实实现必须
   知道「现在跑的是哪一单的哪份目录」。`make_execute_turn` 拿得到 `worker_snapshot`（里面有
   `ticketId`），注入的工具实现拿不到——所以服务在装配时**接管 execute_turn**：进真回合前把
   目录挂进线程上下文，出来复原。后台作业表同理接一份（`WorkspaceProcessTable`），否则
   看门狗到点杀的是「引擎 cwd 里的那条命令」，不是这单目录里的。
2. **路径不出工单目录**。相对路径按工单目录解析，绝对路径与 `..` 都必须落在目录内
   （`resolve()` 之后再比，软链接指向外面也一并拒掉）。这一层挡不住 `run_shell` 里的命令自己
   去读别处——shell 能做的事由引擎的沙箱与审批那两层管，这里如实写明边界，不假称 shell 也被
   关进目录。
3. **状态要能被端点直接回**。所有对外方法都返回 JSON 可序列化的字典，失败一律带中文 `reason`；
   没有「抛异常给 HTTP 层」这条路，因为回合失败早已被适配器收口成「外发不提交」，端点再抛
   只会把已落盘的事实说成没发生。

模型与通道都在这里现取：模型走 `ticket_model_channel`（OpenAI 兼容），IM 走
`ticket_worker_adapter.make_im_send_fn`（`im_channels` 的既有 send）。两个口子都能注入，
所以自测不需要网络；`send_fn=False` 是显式「通道没接」——那时的投递按失败退避，不假装已送达。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import data_paths as dp
import ticket_inbox_runtime as tir
import ticket_model_channel as tmc
import ticket_restart_resume as trs
import ticket_turn_scheduler as tts
import ticket_worker_adapter as ta
import ticket_worker_runtime as twr
import ticket_workspace_prepare as twp

#: 前台 run_shell 的墙钟上限：后台作业归 30 分钟看门狗，前台命令不能占着槽位不放
DEFAULT_SHELL_TIMEOUT_SECONDS = 600.0
#: 命令输出进对话的上限（单元=字符）；超了给前一段并如实报还剩多少
MAX_OUTPUT_CHARS = 8000
#: 单次 write_file 的正文上限：工单执行体写的是产物，不是往盘上堆日志
MAX_WRITE_CHARS = 1_000_000
#: 历史查询默认条数（端点不填时给的面）
DEFAULT_HISTORY_LIMIT = 50
#: 进程侧默认工作区准备规格来自这个环境变量（**没有任何请求体能填它**）
PREPARE_ENV_VAR = "TT_TICKET_WORKER_PREPARE"


def prepare_spec_from_env(env: Any = None) -> tuple[Any, str]:
    """从环境变量读默认准备规格：读不到 / 读不懂就回 `(None, 中文理由)`，不抛。

    这是「默认准备规格」**唯一的来源**（另一个是进程内 `configure(prepare=…)`）。给请求体开
    这个口等于让远程主体挑目录——一个 `{"command": [...]}` 就是一张在桌面机器上起任意进程的票，
    而工单写面那套「只对本机」的闸门挡的是消息内容，不是「谁来指定执行体在哪台仓上干活」。

    JSON 对象 / 数组优先（`{"repo": …, "timeoutSeconds": …}` 说得清超时），退化成裸字符串按
    「仓路径或 URL」解释。坏 JSON 不静默退回不准备：那种情况下执行体会在一个空目录里干活，
    而配置的人以为它站在 worktree 上——理由里写清楚是哪种读法失败了。"""
    source = os.environ if env is None else env
    raw = str(source.get(PREPARE_ENV_VAR) or "").strip()
    if not raw:
        return None, ""
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001  不是 JSON 就按裸字符串（路径 / URL）读
        parsed = raw
    preparer, reason = twp.build_preparer(parsed)
    if preparer is None:
        return None, f"{PREPARE_ENV_VAR} 里的准备规格用不了：{reason}"
    return parsed, ""


_TEXT_DECODINGS = ("utf-8", "gbk")


def _decode(raw: bytes) -> str:
    """命令输出/文件正文的解码：先 utf-8，再 Windows 中文控制台的 gbk，最后 replace。

    这一步不做 fallback 就会在 `chcp 936` 的机器上抛 UnicodeDecodeError——那是「工具没跑成」
    的样子，实际命令早就跑完了，模型只看到一个假失败。"""
    for encoding in _TEXT_DECODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class _TurnContext(threading.local):
    """线程内的「本轮工单」上下文：一线程一回合，调度器就是这么跑的。"""

    def __init__(self) -> None:
        self.ticket_id = ""
        self.dir: Path | None = None


class TicketToolbox:
    """执行体四个文件类工具的真实实现，全部锁在本工单目录里。

    `workspace` 决定目录根（缺省 `data_paths.ticket_workers_dir()`，与注册表同一来源）。
    工单目录由 `start_or_wake` 的 prepare 建好，工具只用它、不替它建目录——目录还没建好时
    如实报「这份领取没有工作区」，不在引擎数据根外面凭空造一个同名目录。"""

    def __init__(
        self,
        *,
        workspace: twr.TicketWorkspace | None = None,
        shell_timeout_seconds: float = DEFAULT_SHELL_TIMEOUT_SECONDS,
        run_fn: Callable[..., Any] = subprocess.run,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        window_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.workspace = workspace or twr.TicketWorkspace()
        self.shell_timeout_seconds = float(shell_timeout_seconds)
        self._run_fn = run_fn
        self.max_output_chars = int(max_output_chars)
        # 「按剩余窗口取页」的读数来源（D-48.8）：一个零参回调，回 `{contextWindowTokens, usedTokens}`
        # 或空。缺省 None 就是**固定预算**——与今天所有装配的行为逐字节一致，页面里也不会
        # 出现一句关于窗口的话。窗口读不到不是拒绝取页的理由，只是这一页没法按剩余量收窄。
        self.window_provider = window_provider
        self._ctx = _TurnContext()

    def set_window_provider(self, provider: Callable[[], dict[str, Any]] | None) -> None:
        """接上/换掉本轮窗口读数的来源（装配在工具箱之后才建好，所以留这个口）。"""
        self.window_provider = provider

    # ── 本轮上下文 ────────────────────────────────────────────
    def current_window(self) -> dict[str, Any]:
        """本轮可用的窗口读数；拿不到就回空字典（调用方据此走固定预算）。

        读一次要调回调，回调可能抛（通道正在被换、模型配置读到一半坏了）——工具面不能被
        「窗口是几」问倒，所以这里把异常吞成「没有窗口信息」并在返回值里留话。"""
        if self.window_provider is None:
            return {}
        try:
            value = self.window_provider()
        except Exception as exc:  # noqa: BLE001
            return {"unreadable": f"{type(exc).__name__}: {exc}"}
        return value if isinstance(value, dict) else {}

    def current_ticket(self) -> str:
        return str(getattr(self._ctx, "ticket_id", "") or "")

    def current_dir(self) -> Path | None:
        return getattr(self._ctx, "dir", None)

    @contextlib.contextmanager
    def turn(self, ticket_id: str, *, directory: Path | None = None):
        """把这一轮的工具调用钉在某个工单目录上（嵌套调用会先保存再复原）。"""
        tid = str(ticket_id or "").strip()
        previous = (self.current_ticket(), self.current_dir())
        resolved_dir = directory
        if resolved_dir is None and tid:
            try:
                resolved_dir = self.workspace.path_for(tid)
            except Exception:  # noqa: BLE001  目录名算不出来（工单 id 为空/越界）就当没有目录
                resolved_dir = None
        self._ctx.ticket_id = tid
        self._ctx.dir = resolved_dir
        try:
            yield resolved_dir
        finally:
            self._ctx.ticket_id, self._ctx.dir = previous

    # ── 路径裁决 ──────────────────────────────────────────────
    def resolve(self, raw: Any) -> tuple[Path | None, str]:
        """把模型给的路径收成工单目录内的绝对路径；越界返回中文理由。"""
        base = self.current_dir()
        if base is None:
            return None, (
                f"当前线程没有工单目录上下文（ticketId={self.current_ticket() or '空'}）："
                "工具只在某一轮执行体的回合里可用，跨轮调用不给目录。"
            )
        text = str(raw or "").strip()
        if not text:
            return None, "路径为空：read/write/list 都要给一个工单目录内的路径。"
        try:
            candidate = Path(text)
            target = candidate if candidate.is_absolute() else base / candidate
            resolved = target.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return None, f"路径「{text}」解析失败：{type(exc).__name__}: {exc}"
        base_resolved = os.path.normcase(str(base.resolve()))
        probe = os.path.normcase(str(resolved))
        if probe != base_resolved and not probe.startswith(base_resolved + os.sep):
            return None, (
                f"路径「{text}」越出本工单目录（{base.name}），已拒绝——执行体只在自己的目录里干活，"
                "要看别处的文件请在唤醒里说清楚要哪一份。"
            )
        return resolved, ""

    # ── 四个工具 ──────────────────────────────────────────────
    def read_file(self, path: str, units: int, start: int = 0) -> dict[str, Any]:
        target, deny = self.resolve(path)
        if deny:
            return {"text": "", "reason": deny}
        if not target.exists():
            return {"text": "", "reason": f"文件不存在：{target.name}（工单目录里没有这个路径，本轮没有读到任何内容）"}
        if target.is_dir():
            return {"text": "", "reason": f"{target.name} 是目录，要列内容用 list_dir。"}
        try:
            body = _decode(target.read_bytes())
        except OSError as exc:
            return {"text": "", "reason": f"读取失败：{type(exc).__name__}: {exc}"}
        budget = ta.budgeted_read(body, units, start, window=self.current_window())
        budget["path"] = str(target)
        return budget

    def list_dir(self, path: str, limit: int) -> dict[str, Any]:
        target, deny = self.resolve(path if str(path or "").strip() else ".")
        if deny:
            return {"items": [], "reason": deny}
        if not target.exists():
            return {"items": [], "reason": f"目录不存在：{target.name}（本轮没有列到任何东西）"}
        if not target.is_dir():
            return {"items": [target.name], "reason": f"{target.name} 是文件不是目录，这里只给了它自己。"}
        try:
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            return {"items": [], "reason": f"列目录失败：{type(exc).__name__}: {exc}"}
        names = [f"{p.name}/" if p.is_dir() else p.name for p in entries if p.name != twr.PREPARE_MARKER]
        budgeted = ta.budgeted_list(names, limit, window=self.current_window())
        budgeted["path"] = str(target)
        return budgeted

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target, deny = self.resolve(path)
        if deny:
            return {"reason": deny}
        body = str(content or "")
        if len(body) > MAX_WRITE_CHARS:
            return {
                "reason": f"正文 {len(body)} 字超过单次写入上限 {MAX_WRITE_CHARS}，本次一个字节也没写："
                "分几次写（每次一段），别把日志整份塞进工单目录。"
            }
        if target.is_dir():
            return {"reason": f"{target.name} 是已存在的目录，不往目录上写文件。"}
        try:
            if target.parent != target:
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            return {"reason": f"写入失败：{type(exc).__name__}: {exc}"}
        return {"reason": f"已写入 {target.name}（{len(body)} 字，整文件覆盖）", "path": str(target), "bytes": len(body.encode('utf-8'))}

    def run_command(self, command: str) -> dict[str, Any]:
        cmd = str(command or "").strip()
        if not cmd:
            return {"output": "", "reason": "命令为空，没有执行。"}
        guard = ta.guard_shell_command(cmd)
        if not guard["ok"]:
            # 适配器在进这里之前已经拦过一次；留这一道是因为接线方可能直接调 toolbox
            # （自测、演练、或未来的批处理入口），那时 guard 不能只在上面那层生效。
            return {"output": "", "reason": guard["reason"]}
        base = self.current_dir()
        if base is None:
            return {"output": "", "reason": "当前线程没有工单目录上下文，命令没有执行（不在引擎 cwd 里替你跑一条来路不明的命令）。"}
        if not base.exists():
            return {"output": "", "reason": f"工单目录 {base.name} 还不在这台机器上，命令没有执行。"}
        try:
            proc = self._run_fn(cmd, shell=True, cwd=str(base), capture_output=True, timeout=self.shell_timeout_seconds)
        except subprocess.TimeoutExpired:
            return {
                "output": "",
                "reason": f"命令超过 {self.shell_timeout_seconds:.0f} 秒没结束，本层只等到超时："
                "shell 包装进程被终止，它派生的孙进程可能还活着（这里不假称杀干净了）——"
                "长驻的活请用 background=true 交给看门狗登记。",
            }
        except OSError as exc:
            return {"output": "", "reason": f"命令起不来：{type(exc).__name__}: {exc}"}
        stdout = _decode(proc.stdout or b"") if isinstance(getattr(proc, "stdout", None), (bytes, bytearray)) else str(getattr(proc, "stdout", "") or "")
        stderr = _decode(proc.stderr or b"") if isinstance(getattr(proc, "stderr", None), (bytes, bytearray)) else str(getattr(proc, "stderr", "") or "")
        output = stdout if not stderr.strip() else f"{stdout}\n[stderr]\n{stderr}".strip()
        note = ""
        if len(output) > self.max_output_chars:
            note = f"输出超过 {self.max_output_chars} 单元，只给前一段（后面还有 {len(output) - self.max_output_chars} 单元没进对话）。"
            output = output[: self.max_output_chars]
        code = int(getattr(proc, "returncode", -1) or 0)
        if code != 0:
            note = (note + " " if note else "") + f"退出码 {code}（命令自己说它没成，别把这条当成功收口）。"
        return {"output": (output + (f"\n{note}" if note else "")), "returnCode": code, "cwd": str(base)}


class WorkspaceProcessTable(ta.JobProcessTable):
    """后台作业的进程表：起进程时的 cwd = 本轮工单目录。

    适配器调 `spawn(job_id, argv)` 时不带 cwd（它不该知道磁盘布局），所以这里从工具箱取
    当前轮目录。没有上下文时退回父类行为，并在返回的 reason 里不带花活——那条会进 notes。"""

    def __init__(self, *, toolbox: TicketToolbox, popen: Callable[..., Any] = subprocess.Popen):
        super().__init__(popen=popen)
        self.toolbox = toolbox

    def spawn(self, job_id: str, argv: list[str], *, cwd: str | Path | None = None) -> dict[str, Any]:  # noqa: D102
        if cwd is None:
            base = self.toolbox.current_dir()
            cwd = str(base) if base is not None and base.exists() else None
        return super().spawn(job_id, list(argv), cwd=cwd)


class TicketServiceRuntime:
    """一进程一套的工单执行体装配 + 端点面。

    构造即装配（注册表 / 队列 / 已读 / 看门狗 / 收件箱 / 调度器 / 补查），不惰性——惰性会把
    「数据根换了」这种错误留到第一个请求。`model_call` 与 `send_fn` 都能注入；都给假的就是一台
    离线装配（自测与演练用），都缺省就是一台真机装配（模型走 OpenAI 兼容端点、外发走 im_channels）。
    """

    def __init__(
        self,
        *,
        model_call: Callable[[dict[str, Any]], Any] | None = None,
        model_config: Any = None,
        chat: Callable[..., dict[str, Any]] | None = None,
        token_provider: Callable[[], str] | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
        send_fn: Callable[[str, str, str], Any] | None | bool = None,
        loop: Any = None,
        goal: str = "",
        default_session_key: str = "",
        agent_slots: int = 3,
        human_extra: int = 1,
        max_parallel_turns: int = tts.DEFAULT_MAX_PARALLEL_TURNS,
        max_followup_turns: int = tts.DEFAULT_MAX_FOLLOWUP_TURNS,
        max_tool_rounds: int = 8,
        thread_factory: Callable[..., Any] = threading.Thread,
        workspace: twr.TicketWorkspace | None = None,
        toolbox: TicketToolbox | None = None,
        inbox_journal: Any = None,
        prepare: Any = None,
        prepare_env: Any = None,
    ):
        self.home = str(dp.engine_home())
        self.workspace = workspace or twr.TicketWorkspace()
        # 工作区准备规格：进程内 `prepare=` 优先，其次环境变量，两处都没给就是「只建目录」。
        # 建不出来（非法 argv / 字符串命令 / 越界超时）不抛、不拦装配：如实记一条理由，
        # 后续投递按不准备走（空目录照样能干活），状态面把「为什么没准备」说出来。
        self.prepare_spec, self.prepare_reason = self._resolve_prepare_spec(prepare, prepare_env)
        self.preparer: twp.WorkspacePreparer | None = None
        if self.prepare_spec is not None:
            self.preparer, build_error = twp.build_preparer(self.prepare_spec)
            if self.preparer is None:
                # 规格给了但用不了：不抛（那会让整个工单服务起不来），按「不准备」继续，
                # 但理由必须留着——状态面不说，配的人以为自己已经站上了 worktree。
                self.prepare_reason = f"准备规格用不了：{build_error}"
        self.toolbox = toolbox or TicketToolbox(workspace=self.workspace)
        self.processes = WorkspaceProcessTable(toolbox=self.toolbox)
        self.channel: tmc.TicketModelChannel | None = None
        if model_call is None:
            self.channel = tmc.build_model_channel(
                model_config, chat=chat, token_provider=token_provider, on_usage=on_usage
            )
            model_for_turn: Callable[[dict[str, Any]], Any] = self.channel
        else:
            model_for_turn = model_call
        self.model_call = model_for_turn
        # 工具箱的窗口读数在这里接（不在构造参数里）：注入的 toolbox 也要能读到本轮窗口，
        # 否则「按剩余窗口取页」在最像真机的那条路上反而不生效。已有来源就不覆盖。
        if self.toolbox.window_provider is None:
            self.toolbox.set_window_provider(self.window_state)

        # 通道接线：False = 显式不接（投递一律如实失败退避）；None = 走 im_channels 真发。
        resolved_send: Callable[[str, str, str], Any] | None
        if send_fn is False:
            resolved_send = None
        elif callable(send_fn):
            resolved_send = send_fn  # type: ignore[assignment]
        else:
            resolved_send = ta.make_im_send_fn(run_coro=ta.make_loop_bridge(loop) if loop is not None else None)
        self.send_fn = resolved_send

        def execute_turn(worker_snapshot: dict[str, Any], prompt: str, history: list) -> dict[str, Any]:
            run = ta.make_execute_turn(
                self.model_call,
                goal=goal,
                default_session_key=default_session_key,
                run_command=self.toolbox.run_command,
                read_file=self.toolbox.read_file,
                list_dir=self.toolbox.list_dir,
                write_file=self.toolbox.write_file,
                max_tool_rounds=max_tool_rounds,
                processes=self.processes,
            )
            snapshot = worker_snapshot if isinstance(worker_snapshot, dict) else {}
            with self.toolbox.turn(str(snapshot.get("ticketId") or "")):
                return run(worker_snapshot, prompt, history)

        self.parts = ta.build_dispatcher(
            execute_turn=execute_turn,
            send_fn=resolved_send,
            agent_slots=agent_slots,
            human_extra=human_extra,
            workspace=self.workspace,
            inbox_journal=inbox_journal,
            processes=self.processes,
        )
        self.dispatcher = self.parts["dispatcher"]
        self.registry = self.parts["registry"]
        self.outbox = self.parts["outbox"]
        self.ledger = self.parts["ledger"]
        self.watchdog = self.parts["watchdog"]
        self.inbox_journal: tir.TicketInbox | None = self.parts["inboxJournal"]
        self.scheduler = tts.TicketTurnScheduler(
            self.dispatcher,
            max_parallel_turns=max_parallel_turns,
            max_followup_turns=max_followup_turns,
            thread_factory=thread_factory,
        )
        self.resume = trs.TicketRestartResume(self.scheduler)
        self.goal = goal
        self.default_session_key = default_session_key
        self.signature = {
            "home": self.home,
            "agentSlots": int(agent_slots),
            "humanExtraSlots": int(human_extra),
            "maxParallelTurns": int(max_parallel_turns),
            "maxFollowupTurns": int(max_followup_turns),
            "model": self.channel.describe().get("mode", "injected") if self.channel is not None else "injected",
            "modelName": self.channel.describe().get("modelName", "") if self.channel is not None else "",
            "sendWired": resolved_send is not None,
        }

    # ── 端点面：投递与查询 ────────────────────────────────────
    @staticmethod
    def _resolve_prepare_spec(prepare: Any, prepare_env: Any) -> tuple[Any, str]:
        """装配期定规格：进程内 `prepare=` > 环境变量 > 没有（只建目录）。"""
        if prepare is not None:
            return prepare, ""
        return prepare_spec_from_env(prepare_env)

    def window_state(self) -> dict[str, Any]:
        """本轮取页用的窗口读数；注入的 `model_call` 没有通道，就如实回空（走固定预算）。"""
        if self.channel is None:
            return {}
        return self.channel.window_state()

    def paging_state(self) -> dict[str, Any]:
        """这一轮取页按什么算：固定预算，还是按剩余窗口收窄（并把「凭什么」一起回）。"""
        window = self.window_state()
        tokens = int(window.get("contextWindowTokens") or 0)
        used = int(window.get("usedTokens") or 0)
        if not tokens:
            return {
                "mode": "fixed",
                "reason": "读不到上下文窗口（模型没配窗口，或模型是注入的没有通道）："
                          "读取分页按固定预算执行，不假装按剩余窗口收窄。",
            }
        return {
            "mode": "window" if used else "window-no-usage",
            "contextWindowTokens": tokens,
            "usedTokens": used,
            "windowFromModel": bool(window.get("contextWindowFromModel")),
            "usageFromReport": bool(window.get("usageFromReport")),
            # 每一半的确定性都单独说：填的窗口 vs 估算窗口、上游回报的用量 vs 本地字符折算。
            # 页面只写「按剩余窗口取页」会把两套不同的确定性说成一句。
            "note": (
                f"窗口 {'用户填写' if window.get('contextWindowFromModel') else '按默认估算'} 的 "
                f"{tokens} tokens；本轮用量 {'取上游回报' if window.get('usageFromReport') else '按字符折算'} 约 {used}。"
                + ("还没发生过一次模型调用，第一页按满窗口算。" if not used else "")
            ),
        }

    def prepare_state(self) -> dict[str, Any]:
        """工作区准备的面：有没有、从哪儿、跑成了几次、最后一次为什么失败。

        没有规格是**正常状态**不是故障（空目录就是可运行的最小工单工作区），所以 `enabled`
        之外还回一句 `note`，免得状态面把「没配」显示成「配了但坏了」。"""
        if self.preparer is not None:
            return {"enabled": True, **self.preparer.describe(), "source": "process"}
        return {
            "enabled": False,
            "source": "process",
            "reason": self.prepare_reason,
            "note": (
                "没配准备规格：工单目录按最小形态建（只建目录）。要在一份真实仓库上干活，"
                f"给 {PREPARE_ENV_VAR} 或进程内 configure(prepare=…)——请求体没有这个口。"
            )
            if not self.prepare_reason
            else "准备规格没配上，本轮起工单只建空目录（不假装准备成功）。",
        }

    def apply_prepare(self, spec: Any) -> dict[str, Any]:
        """换准备规格而不重建装配（与 `apply_model_config` 同一口径）。

        活着的 worker、占着的槽位、盘上的耐久箱都挂在这套对象上，为换一份仓地址把它们拆掉
        不划算。**已经在跑的工单不会被换掉目录**：目录已存在时 `prepare()` 直接返回，新规格
        只影响之后新建的工作区；而真要换来源，`TicketWorkspace` 的指纹比对会先把复用挡住。"""
        if spec is None:
            self.prepare_spec, self.preparer = None, None
            self.prepare_reason = ""
            return {"ok": True, "applied": True, "prepare": self.prepare_state()}
        preparer, reason = twp.build_preparer(spec)
        if preparer is None:
            # 换规格失败：旧的这份**不动**——把已经配好的准备动作换成「没有」，
            # 会让下一单在空目录里干活，而配的人以为换错了就还是原来那份。
            return {"ok": False, "applied": False, "reason": reason, "prepare": self.prepare_state()}
        self.prepare_spec, self.preparer = spec, preparer
        self.prepare_reason = ""
        return {"ok": True, "applied": True, "prepare": self.prepare_state()}

    def apply_model_config(self, model_config: Any, *, transport: dict[str, Any] | None = None) -> dict[str, Any]:
        """把新模型配置喂给现有通道（端点「保存设置」后调它）。

        不重建装配：活着的 worker、占着的槽位、盘上的耐久箱都挂在这套对象上，为了换个
        模型端点把它们拆掉，比「多等一轮」严重得多。注入的 `model_call` 没有可更新的配置，
        那时如实报「不归这层管」，不假装应用成功。`transport` 是给通道的配套件（换票件 /
        用量回调），键在就换——**换模式必须连它一起换**，否则下一轮撞的是「网关模式没有换票件」。"""
        if self.channel is None:
            return {
                "ok": False,
                "applied": False,
                "reason": "这套装配的模型是注入的（model_call），没有通道配置可更新——注入方自己负责每轮现取。",
            }
        resolved = self.channel.set_model_config(model_config, transport=transport)
        self.signature["model"] = str(resolved.get("mode") or "none")
        self.signature["modelName"] = str(resolved.get("modelName") or "")
        return {"ok": True, "applied": True, "modelReady": self.channel.ready(), "model": self.channel.describe()}

    def submit(
        self,
        *,
        ticket_id: str,
        message: str,
        assignment_id: str,
        title: str = "",
        origin: str = "human",
        prev_assignment_id: str = "",
        arrived_at_ms: int = 0,
        prepare_fn: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        """投一条唤醒（端点唯一入口）：槽位与模型通道都在**投递之前**裁决，被拒的唤醒哪儿也没去。

        `prepare_fn` 是**进程内**回调（领取回路用），端点面永远传 None——请求体里没有这个字段，
        有配默认规格的装配由这层补上 `self.preparer`。"""
        if self.channel is not None and not self.channel.ready():
            return {
                "ok": False,
                "action": "rejected",
                "stage": "model-channel",
                "ticketId": str(ticket_id or ""),
                "reason": f"模型通道还没配置好，这条唤醒没有投递：{self.channel.config.get('reason') or '没有可用模型'}"
                "（配好模型后重新投一次即可，引擎没有替你记住这条）。",
            }
        if prepare_fn is None and self.preparer is not None:
            # 默认规格只补「调用方没自己带准备动作」的这一路：领取回路显式传了 prepare_fn
            # 就以它为准，装配期那份不能覆盖它（那是同一工单在不同来源之间的第二次裁决）。
            prepare_fn = self.preparer
        result = self.scheduler.submit(
            ticket_id=ticket_id,
            message=message,
            assignment_id=assignment_id,
            title=title,
            origin=origin,
            prev_assignment_id=prev_assignment_id,
            prepare_fn=prepare_fn,
            arrived_at_ms=arrived_at_ms,
        )
        if result.get("delivered") == "started":
            # 这份目录是按哪个来源准备的：调用方（界面/领取回路）据此判断「换仓了没有」，
            # 不必再去读磁盘上的准备标记。只对**新起的绑定**给——`existing-worker` 这一路
            # 根本没碰磁盘，标记里存的才是那份目录的真实来源，这里不替它编一个当前值。
            result["prepareFingerprint"] = twr.TicketWorkspace.prepare_fingerprint(prepare_fn)
        return result

    def state(self, ticket_id: str) -> dict[str, Any]:
        tid = str(ticket_id or "").strip()
        if not tid:
            return {"ok": False, "reason": "ticket_id 为空：状态查询要指明查哪一单。"}
        return {"ok": True, **self.scheduler.state_of(tid)}

    def history(self, ticket_id: str = "", *, limit: int = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
        items = self.scheduler.history(ticket_id)
        cap = max(1, int(limit)) if limit else DEFAULT_HISTORY_LIMIT
        return {
            "ok": True,
            "ticketId": str(ticket_id or ""),
            "count": len(items),
            "returned": min(len(items), cap),
            "items": items[-cap:] if cap else items,
            "turnsRun": self.scheduler.snapshot().get("turnsRun", 0),
        }

    def kick(self, ticket_id: str, *, prev_assignment_id: str = "") -> dict[str, Any]:
        tid = str(ticket_id or "").strip()
        if not tid:
            return {"ok": False, "action": "rejected", "reason": "ticket_id 为空：推进一轮总得说要推哪一单。"}
        return self.scheduler.kick(ticket_id=tid, prev_assignment_id=prev_assignment_id)

    def wait_idle(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        return self.scheduler.wait_idle(timeout_seconds=timeout_seconds)

    # ── 端点面：重启补查 ─────────────────────────────────────
    def plan(
        self,
        ticket_id: str,
        *,
        prev_assignment_id: str,
        candidate_wakes: list[Any] | None = None,
        title: str = "",
        origin: str = "human",
    ) -> dict[str, Any]:
        return self.resume.plan(
            ticket_id,
            prev_assignment_id=prev_assignment_id,
            candidate_wakes=candidate_wakes,
            title=title,
            origin=origin,
        )

    def resume_wakes(
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
        return self.resume.resume(
            ticket_id,
            prev_assignment_id=prev_assignment_id,
            new_assignment_id=new_assignment_id,
            candidate_wakes=candidate_wakes,
            title=title,
            origin=origin,
            prepare_fn=prepare_fn,
        )

    # ── 端点面：队列与收件箱 ─────────────────────────────────
    def run_outbox(self, *, limit: int = 16) -> dict[str, Any]:
        """推一把已提交的外发（退避到点的、保序放行下一块的）。端点或周期任务调它。"""
        return self.outbox.run_pending(self.dispatcher.send_dispatch, limit=int(limit))

    def retry_outbox(self, record_id: str) -> dict[str, Any]:
        rid = str(record_id or "").strip()
        if not rid:
            return {"ok": False, "reason": "record_id 为空：重试要指名是哪一条外发记录。"}
        return self.outbox.retry_failed(rid)

    def resolve_outbox_block(self, record_id: str, action: str) -> dict[str, Any]:
        rid = str(record_id or "").strip()
        if not rid:
            return {"ok": False, "reason": "record_id 为空：放行保序组要指名挡住的那条。"}
        return self.outbox.resolve_block(rid, str(action or ""))

    def outbox_state(self) -> dict[str, Any]:
        snap = self.outbox.snapshot()
        return {"ok": True, "outbox": snap, "pendingNow": self.outbox.pending_count()}

    def inbox_state(self, ticket_id: str = "") -> dict[str, Any]:
        if self.inbox_journal is None:
            return {"ok": True, "enabled": False, "reason": "耐久收件箱没启用（inbox_journal=False）：重启补查只能靠调用方候选。"}
        snap = self.inbox_journal.snapshot()
        tid = str(ticket_id or "").strip()
        if tid:
            snap["forTicket"] = self.inbox_journal.pending_for_ticket(tid)
        return {"ok": True, "enabled": True, "inbox": snap}

    # ── 端点面：自述与关停 ───────────────────────────────────
    def status(self) -> dict[str, Any]:
        """服务面板：模型模式 / 槽位 / 在飞 / 队列条数 / 收件箱条数 / 通道是否接线。

        只报自述与计数，不报密钥（`channel.describe()` 里就没有那一项）。"""
        registry_snapshot = self.registry.snapshot()
        workers = registry_snapshot.get("workers") or []
        live = [w for w in workers if w.get("assignmentId") and not w.get("endedStatus")]
        scheduler = self.scheduler.snapshot()
        outbox = self.outbox.snapshot()
        return {
            "ok": True,
            "home": self.home,
            "workersRoot": str(self.workspace.root),
            "model": self.channel.describe() if self.channel is not None else {"mode": "injected"},
            "modelReady": True if self.channel is None else self.channel.ready(),
            "sendWired": self.send_fn is not None,
            "prepare": self.prepare_state(),
            # 「按剩余窗口取页」生效与否是每轮现算的事实，不是配置项：窗口没填 / 上游没回用量
            # 时这一项会显示 fromModel=false / usageFromReport=false，页面据此说实话。
            "paging": self.paging_state(),
            "signature": dict(self.signature),
            "slots": registry_snapshot.get("slots") or {},
            "workers": {"total": len(workers), "live": len(live), "queuedWakes": sum(int(w.get("queuedWakes") or 0) for w in workers)},
            "scheduler": {
                "turnsRun": scheduler.get("turnsRun", 0),
                "inFlight": scheduler.get("inFlight") or [],
                "strandedWakes": scheduler.get("strandedWakes") or [],
                "deferred": scheduler.get("deferred") or [],
                "errors": (scheduler.get("errors") or [])[-5:],
                "maxParallelTurns": scheduler.get("maxParallelTurns"),
                "maxFollowupTurns": scheduler.get("maxFollowupTurns"),
            },
            "outbox": {"records": outbox.get("records", 0), "statusCounts": outbox.get("statusCounts") or {}, "openTx": outbox.get("openTx") or []},
            "inbox": (self.inbox_journal.snapshot() if self.inbox_journal is not None else {"enabled": False}),
        }

    def shutdown(self, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        report = self.scheduler.shutdown(timeout_seconds=timeout_seconds)
        report["runningJobs"] = [f"{j.get('description', '')}（{j.get('jobId', '')}）" for j in self.watchdog.running_for()]
        return report


# ── 进程内单例（端点拿的就是这一套）─────────────────────────────
_LOCK = threading.RLock()
_RUNTIME: TicketServiceRuntime | None = None
_CONFIG: dict[str, Any] = {}


def configure(**kwargs: Any) -> dict[str, Any]:
    """端点接线时调用一次：记下装配参数。

    只有两种情况会重建已有装配，且都当场说明为什么：数据根换了（`TONGTIANWORK_HOME` 移动，
    旧那份的账本与目录都不再是现在这套路径）、或并发口径变了（槽位数 / 并行回合数——这些是
    构造期事实，改了必须重建才生效）。模型配置**不**在这里触发重建：带 `model_config` 时当场
    推给现有装配的通道（`modelApplied` 如实回报推没推成），下一轮就是新端点——网关令牌会到期，
    每轮现取。重建前尽力收尾旧装配，超时尚在飞的回合如实报出来，不假装已经停干净。"""
    global _RUNTIME, _CONFIG
    with _LOCK:
        merged = dict(_CONFIG)
        merged.update(kwargs)
        _CONFIG = merged
        rebuilt = False
        report: dict[str, Any] = {}
        if _RUNTIME is not None:
            home_moved = _RUNTIME.home != str(dp.engine_home())
            slots_changed = any(
                _RUNTIME.signature.get(name) != merged.get(key)
                for name, key in (
                    ("agentSlots", "agent_slots"),
                    ("humanExtraSlots", "human_extra"),
                    ("maxParallelTurns", "max_parallel_turns"),
                    ("maxFollowupTurns", "max_followup_turns"),
                )
                if merged.get(key) is not None
            )
            if home_moved or slots_changed:
                report = _RUNTIME.shutdown(timeout_seconds=2.0)
                report["why"] = "数据根换了" if home_moved else "并发口径变了"
                _RUNTIME = None
                rebuilt = True
        # 模型配置：不重建装配，直接推给现有通道。用户在设置页改完模型不重启引擎，
        # 这条是唯一的生效路径——不推的话 `_CONFIG` 里那份新配置只有等到下次重建才被读到，
        # 而「下次重建」可能要等到进程结束（数据根和并发口径都不会变）。
        model_report: dict[str, Any] = {}
        if _RUNTIME is not None and "model_config" in kwargs:
            # 配套件跟着配置一起推：kwargs 里给了就换，没给就保持原样（只换端点不换传输）
            transport = {key: kwargs[key] for key in ("token_provider", "on_usage") if key in kwargs}
            model_report = _RUNTIME.apply_model_config(kwargs.get("model_config"), transport=transport or None)
        # 准备规格：与模型配置同口径——推给现有装配，不重建。换一份仓地址不值得拆掉活着的
        # worker、占着的槽位与盘上的耐久箱；失败也不重建（旧那份继续用，理由如实回报）。
        prepare_report: dict[str, Any] = {}
        if _RUNTIME is not None and "prepare" in kwargs:
            prepare_report = _RUNTIME.apply_prepare(kwargs.get("prepare"))
        return {
            "ok": True,
            "rebuilt": rebuilt,
            "shutdown": report,
            "modelApplied": model_report,
            "prepareApplied": prepare_report,
            "configKeys": sorted(merged.keys()),
        }


def get_runtime(**kwargs: Any) -> TicketServiceRuntime:
    """取（必要时建）进程内那一套装配。`kwargs` 只在还没建过时生效——建好之后改口径要走 `configure`。"""
    global _RUNTIME, _CONFIG
    with _LOCK:
        if _RUNTIME is None:
            _RUNTIME = TicketServiceRuntime(**{**_CONFIG, **kwargs})
            _CONFIG = {}
        return _RUNTIME


def reset_runtime(*, timeout_seconds: float = 2.0) -> dict[str, Any]:
    """拆掉单例（自测换数据根、或引擎退出前收尾）。返回关停报告，没有装配就是空报告。"""
    global _RUNTIME, _CONFIG
    with _LOCK:
        if _RUNTIME is None:
            return {"ok": True, "existed": False}
        report = _RUNTIME.shutdown(timeout_seconds=timeout_seconds)
        _RUNTIME = None
        _CONFIG = {}
        return {"ok": True, "existed": True, **report}


def runtime_status(**kwargs: Any) -> dict[str, Any]:
    """给状态端点的一行调用：取装配 + 回自述。"""
    return get_runtime(**kwargs).status()


def peek_runtime() -> "TicketServiceRuntime | None":
    """看一眼有没有已建好的装配，**不**因此建一套。

    周期泵用它判断「装配建好了没」：引擎刚起来时泵每 20 秒醒一次，若醒一次就 `get_runtime()`
    一次，等于把「第一次建装配失败」变成每 20 秒重演一次，还会在建成功前反复跑构造期的目录
    检查。返回 None 时泵什么都不做——没有 worker、没有外发，也没有可推的东西。"""
    with _LOCK:
        return _RUNTIME


def json_ready(value: Any) -> Any:
    """把结果过一遍 JSON 往返，端点回出去之前先确认它真能序列化。

    这不是多余的一步：这些字典里混着 Path、异常对象与作业句柄，`JSONResponse` 撞上它们时
    报的是 500 且**已经落盘的事实不会回滚**——那时用户看到「服务出错」，实际唤醒投进去了。
    在本层先撞上，比在 uvicorn 里撞上便宜。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
