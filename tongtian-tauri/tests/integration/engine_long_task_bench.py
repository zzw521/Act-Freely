#!/usr/bin/env python3
"""
长任务基准 · 引擎侧 counterpart（D-40 第 6 项）

与 Rust harness 侧的 `harness::runner::tests::bench_long_task_100_rounds` 连同一个
假 OpenAI 兼容端点、按同一套脚本轮次走、用同一条 token 折算公式，把两条执行链路的
「token 体量 / 首包 / 整包 / 每轮节奏 / 检查点读写 / 中断续跑」量成一份可对比的 JSON：

    ENGINE_BENCH_JSON {...}

跑法（默认口径 4 段 × 每段 25 轮工具 + 1 次收尾 = 104 次模型调用、100 次工具写盘）：

    tongtian-tauri/.venv/Scripts/python.exe tongtian-tauri/tests/integration/engine_long_task_bench.py
    BENCH_SMOKE=1 …/engine_long_task_bench.py     # 2 段 × 3 轮的快速自检

口径（必须和报告一起读，别把这里的数当成真实模型成本）：

1. 端点是本机假服务：不联网、不花真实 token。`prompt_tokens` = 实际请求体字节 ÷ 4
   向上取整（端点算好回在 usage 里），completion 固定回报 40/次。
   引擎侧与 harness 侧连的是同一个端点、用的是同一条公式，所以两侧可比；绝对值不代表真实分词。
2. 引擎不自算这一份 token：它自己的压缩判据用 tiktoken 精算（中文约 1 字 1 token，
   比字节 ÷ 4 高），所以「压缩在第几轮触发」两侧本来就不完全同，报告里如实分开写。
3. 首包 = 客户端从发出请求到拿到第一个**有内容**增量的时间。假端点先只发响应头，
   隔 TTFT_MS 才发第一个内容包，再隔 STREAM_MS 发 usage + [DONE]；
   整包 = 同一次调用到流结束的时间。两侧都由各自模型层自己记：
   harness 记在 `model::stream` 的 Outcome，引擎记在 `UsageTrackingModel` 的 usage 事件。
4. 「30 个工具」按设置页同口径实现为「单轮工具循环上限 30」（`runtime.agentPolicy.maxIters`），
   整场共 100 次工具调用。两侧实际下发的工具清单数量不同（引擎按自己的注册表，
   harness 只有内置那几个），这本身就是 prompt 体量差的一部分，不抹平。
5. 中断续跑：harness 侧停在确认框上（待审批工具欠着），引擎侧用「工具执行后模型链路 500」
   触发；两边都落成同一形状的检查点——已完成的工具不重放、欠着的补完。
   引擎的逐工具审批要连本机审批网关，基准不带网关跑，所以用链路中断代替，报告里写明。
6. 工作目录、会话目录、检查点目录（TONGTIANWORK_HOME）全部指向临时目录；
   隐私模式关闭（要量的就是落盘那一份开销）；文件档位显式 auto_approve，
   基准绝不弹系统确认框；不写意识记忆。

D-18 效率三指标（`ENGINE_BENCH_JSON.efficiency`，与上面的口径并列，不是替换）：

11. **每任务 tokens** = 折算 prompt 总量 ÷ 段数（一段就是一个用户任务：一次输入跑到收尾）。
12. **每千 tokens 墙钟** = （第 1 阶段墙钟 − 端点脚本留白）÷ 折算 prompt 千数。
    留白是假端点自己 `sleep` 的 `ttft_ms + stream_ms`（每次请求 65 ms），它跟注入面大小无关，
    不减掉就会得出「注入越少、每千 tokens 越慢」的倒挂结论；`stub_sleep_ms` 一并打出来供核对。
13. **重复注入占比** = 同一任务内「与本轮之前某次请求逐字相同的消息字节」÷ 请求体总字节。
    判定在端点侧按真实收到的 messages 逐条比对（不是估算）：system 段每轮重发、
    上一轮的工具结果与回复每轮重发，都算进去；只有本轮新出现的那条消息算新增。
    为此基准默认随请求下发一份真实形状的注入负载（`BENCH_INJECTION=typical`），
    `BENCH_INJECTION=heavy` 是上限压力档，`none` 复现 2026-11-03 报告那一版「裸提问」口径。
    注入负载是假数据但形状是真的：意识四文件按实际字数分布、技能描述长度取自 SKILL.md
    frontmatter 的常见失控（整段说明写进 description）、附件按常见多文件上传。

fastapi/agentscope 缺失时输出 SKIP 并以 0 退出（与其余引擎 selftest 同一约定）。
"""

import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 数据根要在 import server 之前换成临时目录：检查点、会话日志、用量都落在这里
DATA_ROOT = Path(tempfile.mkdtemp(prefix="tt-engine-bench-home-"))
os.environ["TONGTIANWORK_HOME"] = str(DATA_ROOT)
os.environ["TT_ENABLE_SEARCH"] = "false"

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    from fastapi.testclient import TestClient
    import checkpoint_runtime as ck
    import runtime_context as rc
    import server as es
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 引擎基准依赖不可用（{exc}）")
    sys.exit(0)

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def parse_sse(text: str) -> list:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev = {}
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    ev["data"] = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    ev["data"] = {"raw": line[5:].strip()}
        if ev.get("event"):
            events.append(ev)
    return events


def events_of(events: list, name: str) -> list:
    return [e["data"] for e in events if e.get("event") == name]


def stats(values: list) -> dict:
    """平均 / 中位 / 95 分位 / 峰值：与 Rust 侧 `stats()` 同一口径"""
    if not values:
        return {"avg": 0, "p50": 0, "p95": 0, "max": 0, "n": 0}
    ordered = sorted(values)
    idx = lambda p: min(len(ordered) - 1, p * len(ordered) // 100)  # noqa: E731
    return {
        "avg": sum(ordered) // len(ordered),
        "p50": ordered[idx(50)],
        "p95": ordered[idx(95)],
        "max": ordered[-1],
        "n": len(ordered),
    }


# ── D-18 注入负载：让「重复注入」量得出来（形状取自真实配置分布，内容是假数据）──

INJECTION_PROFILE = os.environ.get("BENCH_INJECTION", "typical")


def _filler(unit: str, n: int) -> str:
    """按单元循环出 n 个字符的正文（可复现：同一 profile 每次跑出同一份字节）"""
    return (unit * (n // len(unit) + 1))[:n]


# 技能描述的字数分布：多数技能 frontmatter 只写一句，少数把整段使用说明塞进 description
_DESC_LEN = {"typical": [90, 180, 620, 240], "heavy": [700, 520, 640, 480]}
_SKILL_COUNT = {"typical": 14, "heavy": 30}
_AWARENESS_CHARS = {
    "typical": {"SOUL.md": 1600, "USER_PROFILE.md": 900, "WORK_MANUAL.md": 2400},
    "heavy": {"SOUL.md": 6000, "USER_PROFILE.md": 6000, "WORK_MANUAL.md": 6000},
}
_MEMORY = {"typical": (24, 34, 1100), "heavy": (60, 40, 3000)}   # 条数, 每条字数, 手写正文
_ATTACH = {"typical": (2, 9000), "heavy": (6, 18000)}            # (件数, 每件字数)


def injection_bundle(profile: str) -> tuple[dict, list]:
    """返回 (awareness 块, 附件列表)；`none` 档给空，用于复现旧报告的裸提问口径。"""
    if profile == "none":
        return {}, []
    files = {name: _filler("基准意识样例句子。", n) for name, n in _AWARENESS_CHARS[profile].items()}
    count, per, freeform = _MEMORY[profile]
    memory_lines = [
        f"- mem-{i:02d} · auto · 2026-10-{i % 28 + 1:02d} · {_filler('既有约定。', per)}"
        for i in range(count)
    ]
    files["MEMORY.md"] = "# 长期记忆\n" + "\n".join(memory_lines) + "\n" + _filler("手写备忘正文。", freeform)
    return {"mode": True, "autoMemory": False, "files": files}, [
        {
            "fileName": f"材料{idx}.md",
            "mime": "text/markdown",
            "stagedPath": f"/tmp/bench-attach-{idx}.md",
            "text": _filler("附件材料正文。", _ATTACH[profile][1]),
        }
        for idx in range(_ATTACH[profile][0])
    ]


def injection_skills(profile: str) -> list:
    """启用的技能清单：正文很长但不进 prompt（由 use_skill 取），只有描述进清单"""
    if profile == "none":
        return []
    lens = _DESC_LEN[profile]
    return [
        {
            "id": f"bench-skill-{i:02d}",
            "name": f"基准技能 {i:02d}",
            "description": _filler("该技能的做法与适用场景说明。", lens[i % len(lens)]),
            "content": _filler("技能正文步骤。", 8000),
            "kind": "skill",
            "source": "installed",
        }
        for i in range(_SKILL_COUNT[profile])
    ]


# ── 假端点：与 Rust 侧 pump_bench 同一套脚本与同一条折算公式 ──────────────


class BenchStub:
    """按脚本一轮轮回答，并把「实际发出去的东西」量出来。

    - 带 `tools` 的请求算一轮任务调用：未到上限回工具调用，到上限回正文收尾；
    - 不带 `tools` 的是压缩摘要调用，单独计数，不占任务轮次；
      其中按 AgentScope 的结构化协议回 JSON 的，另记进 compression_ok_attempts；
    - `prompt_tokens` = 请求体字节 ÷ 4 向上取整；
    - 响应分三段写：只发响应头 → 隔 ttft_ms 发第一个内容增量 → 隔 stream_ms 发 usage + [DONE]。
    """

    def __init__(self, ttft_ms: int, stream_ms: int):
        self.lock = threading.Lock()
        self.ttft_ms = ttft_ms
        self.stream_ms = stream_ms
        self.rounds = 25
        self.segments = 4
        self.body_chars = 2400
        self.round_in_segment = 0
        self.fail_after = -1          # >=0：第 N 次任务调用之后一律回 500（直到重新 arm）
        # 写盘序号：正常每次进一位。续跑时 reuse_last_index 让端点重发中断前那一条
        # 一模一样的参数——幂等中间件按「工具名 + 规范化参数」判签名，序号一变就跳不掉。
        self.index = 0
        self.last_index = -1
        self.reuse_last_index = False
        self.last_written_path: str | None = None
        # 计量
        self.task_calls = 0
        self.summary_calls = 0
        self.compression_ok_attempts = 0   # 端点按结构化协议回了摘要的那几次
        self.tool_calls = 0
        self.http_500 = 0             # 端点侧被拒的请求数（SDK 重试也计在这里）
        self.segments_done = 0
        self.bytes: list = []
        self.gaps_ms: list = []
        self.last_messages: list = []
        self.last_tools: list = []
        self._prev_arrival = None
        # D-18 重复注入判定：按「任务窗口」归集，窗口内逐条比对收到的消息
        self.window = "warmup"
        self.per_window: dict = {}

    def begin_window(self, name: str) -> None:
        """开一个任务窗口：一次 /api/chat 就是一个任务，它里面的每次模型调用都归到这里。
        TestClient 是同步的，同一任务的请求严格顺序到达，不需要额外加锁。"""
        self.window = name

    def _record_injection(self, messages: list, body_bytes: int) -> None:
        """量「这一轮里有多少字节是上一轮已经发过、本轮原样再发一遍」的。

        逐条按序列化结果比对：只要这条消息在本窗口更早的某次请求里逐字出现过，
        它的字节就计入重复。system 段因此每轮都被算一次，历史里活着的旧消息也是；
        本轮新产生的那条（工具结果 / 用户新输入）才算新增。
        """
        window = self.window
        w = self.per_window.get(window)
        if w is None:
            w = {"calls": 0, "body_bytes": 0, "repeat_bytes": 0,
                 "repeat_system_bytes": 0,
                 "system_variants": [], "system_bytes": 0, "seen": set()}
            self.per_window[window] = w
        serials = [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in messages]
        w["calls"] += 1
        w["body_bytes"] += body_bytes
        w["repeat_bytes"] += sum(
            len(s.encode("utf-8")) for s in serials if s in w["seen"])
        system = next(
            (s for s, m in zip(serials, messages)
             if str((m or {}).get("role") or "").lower() == "system"),
            "",
        )
        # 重复字节要分得开「每轮原样重发的 system 注入段」和「活着的历史」：
        # 两件事的修法不在同一个文件里，混成一个数就没人知道该动哪半边。
        if system and system in w["seen"]:
            w["repeat_system_bytes"] += len(system.encode("utf-8"))
        if system:
            # 同一任务里 system 段应当逐字不变；冒出第二种就说明装配每轮都在动，
            # 那就不只是「重发同一份注入」，而是注入本身在长
            if system not in w["system_variants"]:
                w["system_variants"].append(system)
            w["system_bytes"] = len(system.encode("utf-8"))
        w["seen"].update(serials)

    def reset_counters(self) -> None:
        """暖场跑完清空计量：正式口径不含建连、格式化器初始化那一笔冷启动开销"""
        self.task_calls = 0
        self.summary_calls = 0
        self.compression_ok_attempts = 0
        self.tool_calls = 0
        self.http_500 = 0
        self.segments_done = 0
        self.index = 0
        self.last_index = -1
        self.reuse_last_index = False
        self.last_written_path = None
        self.bytes = []
        self.gaps_ms = []
        self._prev_arrival = None
        self.window = "warmup"
        self.per_window = {}

    def arm(self, rounds: int, segments: int, body_chars: int | None = None,
            fail_after: int = -1, reuse_last_index: bool = False) -> None:
        """换一段脚本：轮次与段数复位，累计计量不清（整场数据要连起来看）"""
        self.rounds = rounds
        self.segments = segments
        if body_chars is not None:
            self.body_chars = body_chars
        self.round_in_segment = 0
        self.fail_after = fail_after
        self.reuse_last_index = reuse_last_index

    def note_arrival(self, when: float) -> None:
        with self.lock:
            if self._prev_arrival is not None:
                self.gaps_ms.append(int((when - self._prev_arrival) * 1000))
            self._prev_arrival = when

    def respond(self, body: bytes):
        """返回 (状态码, 首包字节, 尾包字节)；500 时后两项为 None"""
        parsed = {}
        try:
            parsed = json.loads(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            parsed = {}
        tokens = -(-len(body) // 4)          # 字节 ÷ 4 向上取整
        with self.lock:
            self.bytes.append(len(body))
            messages = parsed.get("messages") or []
            tools = parsed.get("tools") or []
            with_tools = bool(tools)
            self._record_injection(messages, len(body))
            usage_tail = (
                "data: " + json.dumps({"choices": [], "usage": {
                    "prompt_tokens": tokens, "completion_tokens": 40}}) + "\n\n"
                "data: [DONE]\n\n"
            ).encode()
            if not with_tools:
                self.summary_calls += 1
                # AgentScope 的压缩摘要走「结构化输出」：模型必须回一段符合
                # SummarySchema 的 JSON。回纯文本时 last_chunk.metadata 为空，
                # 压缩整轮失败（只在日志里留一条 WARNING），上下文一路涨——
                # 第一版基准就是这么跑的，数字会把引擎侧的压缩成本算成零收益。
                last_text = str((messages[-1] or {}).get("content") or "") \
                    if messages else ""
                if "continuation summary" in last_text:
                    self.compression_ok_attempts += 1
                    first = _chunk(content=json.dumps({
                        "task_overview": "长任务基准：每轮把一个中间结果写进 bench 目录并收尾。",
                        "current_state": f"已写入 {self.tool_calls} 个基准产物，字段与路径核对通过。",
                        "important_discoveries": "端点是本地假服务，token 按请求体字节 ÷ 4 折算；"
                                                 "工具循环上限 30。",
                        "next_steps": "按目标继续写剩余基准产物，最后正文收尾。",
                        "context_to_preserve": "命名 bench/kN.mod.md；不覆盖已有产物。",
                    }, ensure_ascii=False))
                else:
                    first = _chunk(content="基准摘要：已读数据并核对字段")
                return 200, first, usage_tail
            self.task_calls += 1
            self.last_messages = messages
            self.last_tools = tools
            if self.fail_after >= 0 and self.task_calls > self.fail_after:
                # 之后一律 500：openai SDK 自带两次重试，只失败一次会被重试成功，
                # 中断就模拟不出来（重新 arm 才恢复应答）
                self.http_500 += 1
                return 500, None, None
            if self.round_in_segment < self.rounds:
                self.round_in_segment += 1
                if self.reuse_last_index:
                    i = max(self.last_index, 0)
                    self.reuse_last_index = False
                else:
                    i = self.index
                    self.index += 1
                self.last_index = i
                self.tool_calls += 1
                self.last_written_path = f"bench/k{i}.mod.md"
                filler = ("基准正文样例" * (self.body_chars // 6 + 1))[: self.body_chars]
                args = json.dumps({
                    "file_path": self.last_written_path,
                    "content": f"{filler}（第 {i} 次写入）",
                }, ensure_ascii=False)
                first = _chunk(tool_call={
                    "index": 0,
                    # 调用 id 每次唯一（不参与签名）：续跑重发的是同一条写盘参数，
                    # 但记忆里不能出现两个同 id 的 tool_use
                    "id": f"bench-call-{self.task_calls}",
                    "type": "function",
                    "function": {"name": "write_text_file", "arguments": args},
                })
                return 200, first, usage_tail
            self.round_in_segment = 0
            self.segments_done += 1
            first = _chunk(content=f"第 {self.segments_done} 段收尾：基准产物已写进 bench 目录")
            return 200, first, usage_tail


def _chunk(content: str = "", tool_call: dict | None = None) -> bytes:
    delta: dict = {}
    if content:
        delta["content"] = content
    if tool_call is not None:
        delta["tool_calls"] = [tool_call]
    return (
        "data: "
        + json.dumps(
            {
                "id": "bench",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "bench-fake",
                "choices": [{"index": 0, "delta": delta}],
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: D102
        stub: BenchStub = self.server.stub  # type: ignore[attr-defined]
        conn = self.request
        arrived = time.perf_counter()
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                piece = conn.recv(65536)
                if not piece:
                    return
                data += piece
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        length = int(line.split(b":", 1)[1].strip())
                    except ValueError:
                        length = 0
            while len(rest) < length:
                piece = conn.recv(65536)
                if not piece:
                    break
                rest += piece
            stub.note_arrival(arrived)
            status, first, tail = stub.respond(rest)
            if status == 500:
                body = json.dumps({
                    "error": {
                        "message": "基准：工具执行后模型链路中断",
                        "type": "server_error",
                        "code": "bench_interrupt",
                    }
                }).encode()
                conn.sendall(
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Type: application/json\r\nConnection: close\r\n\r\n" + body
                )
                return
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
            )
            if stub.ttft_ms:
                time.sleep(stub.ttft_ms / 1000)
            conn.sendall(first)
            if stub.stream_ms:
                time.sleep(stub.stream_ms / 1000)
            conn.sendall(tail)
        except OSError:
            return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── 基准主体 ─────────────────────────────────────────────────────────────


def main() -> int:
    smoke = os.environ.get("BENCH_SMOKE") == "1"
    rounds = 3 if smoke else 25
    segments = 2 if smoke else 4
    body_chars = 300 if smoke else 2400
    ttft_ms, stream_ms = 40, 25

    # A/B 开关：`BENCH_CAPS=off` 复现注入面「只有单件上限、没有总量上限」的改动前形状。
    # 上限值在这里改，生产代码里不留开关 —— 基准侧把常量推到不生效的高度就够了，
    # 组装段读的是模块全局，运行时立刻生效。
    caps = os.environ.get("BENCH_CAPS", "on")
    if caps == "off":
        rc.MAX_SKILL_DESC_CHARS = 10 ** 9
        rc.MAX_AWARENESS_TOTAL_CHARS = 10 ** 9
        rc.MAX_ATTACH_TOTAL_CHARS = 10 ** 9
        rc.MIN_SECTION_FIT_CHARS = 0
        print("[caps] off —— 三个注入总量上限已推到不生效（改动前形状）")
    elif caps != "on":
        print(f"[caps] 未知取值 {caps}，按 on 跑")

    stub = BenchStub(ttft_ms, stream_ms)
    stub.arm(rounds, segments, body_chars)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.stub = stub  # type: ignore[attr-defined]
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    work = Path(tempfile.mkdtemp(prefix="tt-engine-bench-work-"))
    es.WORK_DIR = work
    client = TestClient(es.app)

    model_config = {
        "source": "custom",
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "apiKey": "EMPTY",
        "modelName": "bench-fake",
        "displayName": "基准假模型",
        "contextWindow": 32768,
    }
    awareness_block, attachments = injection_bundle(INJECTION_PROFILE)
    runtime = {
        "awareness": awareness_block or {"mode": True, "autoMemory": False},
        "skills": injection_skills(INJECTION_PROFILE),
        "filePermission": "auto_approve",
        "agentPolicy": {"maxIters": 30, "autoCompress": True},
    }
    if INJECTION_PROFILE != "none":
        # 注入负载的字符量打一行，便于把三指标和「注入面有多大」对上；
        # 注意这是 runtime 输入侧的字数，进 prompt 后还会被上限截断（这正是 D-18 要量的）
        injected_chars = sum(len(str(v)) for v in awareness_block.get("files", {}).values())
        injected_chars += sum(len(a["text"]) for a in attachments)
        injected_chars += sum(len(s["description"]) + len(s["content"]) for s in runtime["skills"])
        print(f"[injection] profile={INJECTION_PROFILE} 输入侧合计 {injected_chars} 字")

    def run_chat(task_id: str, goal: str, history: list,
                 expect_error: bool = False, window: str = "") -> tuple[list, float]:
        stub.begin_window(window or task_id)
        t0 = time.perf_counter()
        resp = client.post(
            "/api/chat",
            json={
                "message": goal,
                "task_id": task_id,
                "modelConfig": model_config,
                "runtime": runtime,
                "agentPolicy": {"maxIters": 30, "autoCompress": True},
                "workDir": str(work),
                "filePermission": "auto_approve",
                "history": history,
                "attachments": attachments if not history else [],
                "mode": "general",
                "privacyMode": False,
            },
        )
        wall = (time.perf_counter() - t0) * 1000
        check(f"{task_id} HTTP 200", resp.status_code == 200, str(resp.status_code)[:200])
        events = parse_sse(resp.text)
        errs = events_of(events, "error")
        if expect_error:
            check(f"{task_id} 如实报错（基准要的就是这一刀）", bool(errs), str(events)[:300])
        else:
            check(f"{task_id} 没有错误事件", not errs, str(errs)[:400])
            check(f"{task_id} 有 done 收尾", bool(events_of(events, "done")),
                  str(events[-3:])[:300])
        return events, wall

    # 暖场：openai/httpx 客户端第一条请求要建连接池、初始化格式化器，首包会被这一笔
    # 冷启动开销盖过去（量出来比满载上下文还慢，就是它）。harness 侧每次调用都新建
    # 客户端，没有这个台阶，所以只在引擎侧跑一个丢弃结果的暖场任务，随后计量清零。
    print("--- 0. 暖场（不计入任何数字）---")
    stub.arm(1, 1, 60)
    run_chat("bench-engine-warmup", "基准暖场：写一个文件就行", [])
    stub.reset_counters()
    # 暖场产物从 bench 目录里清掉：files_written 只能算正式口径那几段
    shutil.rmtree(work / "bench", ignore_errors=True)

    print(f"--- 1. 长任务 {segments} 段 × 每段 {rounds} 轮工具 + 1 次收尾 ---")
    stub.arm(rounds, segments, body_chars)
    usage_rows: list = []
    walls: list = []
    history: list = []
    compression_events = 0
    started = time.perf_counter()
    for seg in range(1, segments + 1):
        goal = f"长任务基准第 {seg} 段：把 {rounds} 个中间结果分别写进 bench 目录"
        events, wall = run_chat(f"bench-engine-{seg}", goal, list(history))
        walls.append(wall)
        for data in events_of(events, "usage"):
            u = data.get("usage") or {}
            if u:
                usage_rows.append(u)
        # 压缩真的发生与否看引擎自己发的步骤（与 harness 的 ctx-compress 同一个 stepId）
        compression_events += len([
            e for e in events
            if e.get("event") == "step_done" and (e.get("data") or {}).get("step_id") == "ctx-compress"
        ])
        check(f"第 {seg} 段真的收尾（端点侧计数）",
              stub.segments_done >= seg, f"segments_done={stub.segments_done}")
        delivered = events_of(events, "deliver")
        final_text = str((delivered[-1] if delivered else {}).get("final_text") or "")
        history.append({"role": "user", "content": goal})
        history.append({"role": "assistant", "content": final_text or f"第 {seg} 段收尾"})
    wall_ms = int((time.perf_counter() - started) * 1000)

    # 主口径只取这一段：后面的中断演练会额外打出 500 与 SDK 重试，混进来就不是同一件事了
    stage1_task_calls = stub.task_calls
    stage1_summary_calls = stub.summary_calls
    stage1_compression_ok = stub.compression_ok_attempts
    stage1_tool_calls = stub.tool_calls
    stage1_bytes = list(stub.bytes)
    stage1_gaps = list(stub.gaps_ms)
    stage1_usage = list(usage_rows)
    stage1_messages = list(stub.last_messages or [])   # 第 4 段收尾时那份最重的上下文
    stage1_tools = list(stub.last_tools or [])
    # 注入归集只取第 1 阶段的四个窗口（与主口径同一段），中断那一窗另算
    stage1_windows = [f"bench-engine-{s}" for s in range(1, segments + 1)]
    stage1_injection = [stub.per_window.get(w) or {} for w in stage1_windows]
    check("任务调用数 = 段数 ×（轮次 + 1 次收尾）",
          stage1_task_calls == segments * (rounds + 1),
          f"实际 {stage1_task_calls}，期望 {segments * (rounds + 1)}（引擎可能额外发摘要/收尾调用）")
    check("工具写盘次数达标", stage1_tool_calls >= segments * rounds,
          f"{stage1_tool_calls} < {segments * rounds}")
    if smoke:
        print("[SKIP] 快速自检的正文撑不到压缩阈值（引擎按 tiktoken 精算判），不检查摘要调用")
    else:
        check("真的跨过压缩触发点（有摘要调用）", stage1_summary_calls >= 1,
              f"summary_calls={stage1_summary_calls}")
        # 压缩「触发」不等于「成功」：AgentScope 要模型回结构化 JSON，回纯文本就静默失败，
        # 上下文一路涨。这一条保证基准量的是真压缩，不是每次都失败的假压缩。
        check("压缩真的取到了结构化摘要", stage1_compression_ok == stage1_summary_calls,
              f"结构化应答 {stage1_compression_ok} / 摘要调用 {stage1_summary_calls}")
    files_written = len(list((work / "bench").glob("*.md"))) if (work / "bench").exists() else 0
    check("基准产物真的落盘", files_written >= segments * rounds,
          f"bench 目录里只有 {files_written} 个文件")

    thin = [u for u in stage1_usage if u.get("promptTokens")]
    thinnest = min(thin, key=lambda u: u["promptTokens"]) if thin else {}
    fattest = max(thin, key=lambda u: u["promptTokens"]) if thin else {}
    max_prompt = int(fattest.get("promptTokens") or 0)
    check("引擎侧也量到了首包（firstTokenMs 有值）",
          bool(usage_rows) and all(
              isinstance(u.get("firstTokenMs"), int) for u in usage_rows),
          str(usage_rows[:2])[:300])

    print("--- 2. 中断（工具执行后模型链路 500）→ /api/chat/review resume ---")
    interrupt_task = "bench-engine-interrupt"
    interrupt_rounds = 2 if smoke else 3
    done_before = stub.task_calls
    stub.arm(interrupt_rounds, 1, body_chars, fail_after=done_before + interrupt_rounds)
    events, interrupt_wall = run_chat(
        interrupt_task, "基准：把三个续跑文件写完（中断演练）", [], expect_error=True)
    errs = events_of(events, "error")
    cps = events_of(events, "checkpoint")
    check("中断如实报错（没有假装完成）", bool(errs), str(events)[:300])
    check("中断检查点标为可续跑",
          bool(cps) and cps[-1].get("resumable") is True, str(cps)[:400])
    disk_path = ck._checkpoint_file(interrupt_task)
    disk_bytes = disk_path.stat().st_size if disk_path.exists() else 0
    check("中断检查点真的落盘（非隐私模式）", disk_bytes > 0, str(disk_path))
    load_ms: list = []
    for _ in range(3):
        t0 = time.perf_counter()
        loaded = ck.load_checkpoint(interrupt_task)
        load_ms.append(int((time.perf_counter() - t0) * 1000))
    check("磁盘检查点读得回来", bool(loaded), str(loaded)[:200])
    save_ms: list = []
    # 用读回来的同一份内容再落一次盘：量的就是引擎每轮写检查点的固定成本。
    # 信封三键（version/taskId/savedAtMs）由 save_checkpoint 自己盖，留旧值会把过期时间写回去。
    payload = dict(loaded or {})
    for key in ("version", "taskId", "savedAtMs"):
        payload.pop(key, None)
    for _ in range(3):
        t0 = time.perf_counter()
        ok, err = ck.save_checkpoint(interrupt_task, payload)
        save_ms.append(int((time.perf_counter() - t0) * 1000))
        check("检查点重复落盘成功", ok, err)

    # 中断前最后真正执行成功的那一次写盘：续跑时端点会重发同一条参数。
    # 不能用 glob 排序取「最后一个文件」——k10 会排在 k2 前面，探错了文件这条证据就废了。
    probe_rel = stub.last_written_path
    probe = (work / probe_rel) if probe_rel else None
    check("中断前确实有一次写盘（续跑要跳过的目标存在）",
          probe is not None and probe.exists(), str(probe))
    if probe is not None:
        probe.write_text("中断后有人改过，续跑不许覆盖", encoding="utf-8")
    # 续跑脚本：一次同签名写盘（该被跳过）+ 一次正文收尾
    stub.arm(1, 1, body_chars, reuse_last_index=True)
    t0 = time.perf_counter()
    resp = client.post("/api/chat/review",
                       json={"task_id": interrupt_task, "action": "resume"})
    restore_ms = int((time.perf_counter() - t0) * 1000)
    r_events = parse_sse(resp.text)
    r_errs = events_of(r_events, "error")
    r_usage = [d.get("usage") or {} for d in events_of(r_events, "usage")]
    usage_rows.extend([u for u in r_usage if u])
    r_cp = events_of(r_events, "checkpoint")
    check("续跑 HTTP 200 且没有错误", resp.status_code == 200 and not r_errs,
          f"status={resp.status_code} errs={str(r_errs)[:300]}")
    check("续跑把恢复明细摊开（checkpoint-restore）",
          "checkpoint-restore" in resp.text, resp.text[:200])
    check("续跑跳过同签名写盘（不重放副作用）",
          "已跳过重复执行" in resp.text, resp.text[-400:])
    skipped = [c for c in r_cp if int(c.get("skippedDuplicates") or 0) > 0]
    check("跳过计数进到了检查点里", bool(skipped), str(r_cp)[:400])
    if probe is not None:
        check("被跳过的是真调用（人工改动没被覆盖）",
              probe.read_text(encoding="utf-8") == "中断后有人改过，续跑不许覆盖",
              probe.read_text(encoding="utf-8")[:120])
    check("续跑成功后检查点清除且不再可续跑",
          bool(r_cp) and r_cp[-1].get("resumable") is False and not disk_path.exists(),
          str(r_cp)[:300])
    # 续跑用掉几次模型：直接数续跑那条流里的 usage 事件（每次真实调用记一条），
    # 不用端点计数器倒减——500 之后 SDK 的重试也打在端点计数里，倒减会多算
    resume_calls = len([u for u in r_usage if u.get("promptTokens")])
    print(f"--- 续跑用掉的模型调用：{resume_calls} 次；整场任务调用 {stub.task_calls} 次 ---")

    # 每轮固定要做的预处理：历史重编 + 请求体序列化（引擎在 formatter / 组装阶段）。
    # 用第 4 段收尾时那份最重的上下文来量，和 harness 侧的 full ctx 采样同一个位置。
    msgs = stage1_messages
    t0 = time.perf_counter()
    payload_bytes = 0
    for _ in range(20):
        body = json.dumps(
            {"model": "bench-fake", "messages": msgs, "tools": stage1_tools,
             "stream": True},
            ensure_ascii=False,
        ).encode("utf-8")
        payload_bytes = len(body)
    prep_us = int((time.perf_counter() - t0) * 1_000_000 / 20)

    request_bytes_total = sum(stage1_bytes)

    # ── D-18 三指标 ────────────────────────────────────────────────
    prompt_tokens_total = -(-request_bytes_total // 4)
    inj_calls = sum(int(w.get("calls") or 0) for w in stage1_injection)
    inj_body = sum(int(w.get("body_bytes") or 0) for w in stage1_injection)
    inj_repeat = sum(int(w.get("repeat_bytes") or 0) for w in stage1_injection)
    inj_repeat_system = sum(int(w.get("repeat_system_bytes") or 0) for w in stage1_injection)
    system_bytes_each = [int(w.get("system_bytes") or 0) for w in stage1_injection]
    system_variants = sum(len(w.get("system_variants") or []) for w in stage1_injection)
    repeat_ratio = inj_repeat / inj_body if inj_body else 0.0
    scripted_floor_ms = inj_calls * (ttft_ms + stream_ms)
    engine_overhead_ms = max(0, wall_ms - scripted_floor_ms)
    check("重复注入按真实收到的请求体算（与请求体总量同一批请求）",
          inj_body == request_bytes_total,
          f"窗口归集 {inj_body} vs 端点计量 {request_bytes_total}")
    check("每个任务窗口内 system 注入段逐字只有一种（确实每轮在重发同一份）",
          system_variants == len(stage1_windows),
          f"出现 {system_variants} 种，窗口 {len(stage1_windows)} 个")
    check("重复注入占比在 (0,1] 内", 0 < repeat_ratio <= 1, f"{repeat_ratio}")
    # 拆分要自洽：system 那一块本身就是「重复字节」的子集，不能各算各的
    check("重复字节能拆成「每轮重发的注入段」与「活着的历史」两部分",
          0 < inj_repeat_system <= inj_repeat,
          f"注入段 {inj_repeat_system} / 合计 {inj_repeat}")

    efficiency = {
        "injectionProfile": INJECTION_PROFILE,
        "capsProfile": caps,
        "tokensPerTask": prompt_tokens_total // segments,
        "wallMsPerTask": wall_ms // segments,
        "wallMsPerKToken": round(engine_overhead_ms * 1000 / max(prompt_tokens_total, 1), 3),
        "wallMsPerKTokenRaw": round(wall_ms * 1000 / max(prompt_tokens_total, 1), 3),
        "engineOverheadMsPerCall": round(engine_overhead_ms / max(inj_calls, 1), 3),
        "repeatInjectionRatio": round(repeat_ratio, 4),
        "repeatInjectionBytes": inj_repeat,
        "repeatInjectionBytesSystem": inj_repeat_system,
        "repeatInjectionBytesHistory": inj_repeat - inj_repeat_system,
        "measuredBodyBytes": inj_body,
        "measuredCalls": inj_calls,
        "systemBlockBytesEach": system_bytes_each,
        "systemBlockBytesTotal": sum(system_bytes_each),
        "systemBlockVariants": system_variants,
        "scriptedFloorMs": scripted_floor_ms,
        "engineOverheadMs": engine_overhead_ms,
        "perWindow": [
            {
                "window": w_name,
                "calls": int(w.get("calls") or 0),
                "bodyBytes": int(w.get("body_bytes") or 0),
                "repeatBytes": int(w.get("repeat_bytes") or 0),
                "repeatBytesSystem": int(w.get("repeat_system_bytes") or 0),
                "systemBytes": int(w.get("system_bytes") or 0),
            }
            for w_name, w in zip(stage1_windows, stage1_injection)
        ],
    }

    out = {
        "runtime": "python-agentscope-engine",
        "caliber": {
            "endpoint": "本地假端点（与 Rust harness 基准同一个脚本、同一套时序留白）",
            "promptTokens": "实际请求体字节 ÷ 4 向上取整（端点算好回报；折算口径，非真实分词）",
            "completionTokens": "假端点固定回报 40/次，不代表真实产出",
            "firstPacket": "引擎 UsageTrackingModel 记的第一个有内容 chunk 的耗时",
            "engineOwnCompressionCounter": "tiktoken 精算（中文约 1 字 1 token，高于字节÷4），"
                                          "所以压缩触发轮次与 harness 不完全同",
            "warmUp": "跑了一个不计量的暖场任务：引擎侧首条请求含客户端建连，"
                      "这一笔冷启动只从引擎侧剔除（harness 每次调用新建客户端，没有台阶）",
            "stage": "model_calls / token / 首包等主口径只含第 1 阶段（4 段长任务）；"
                     "中断与续跑单列在 restore，那里含 SDK 重试的 500",
            "stampedTTFTms": ttft_ms,
            "stampedStreamMs": stream_ms,
            "maxItersPerSegment": 30,
            "efficiency": "三指标口径：tokensPerTask = 折算 prompt ÷ 段数；"
                          "wallMsPerKToken = （第 1 阶段墙钟 − 端点脚本留白）÷ 折算 prompt 千数，"
                          "留白是假端点每次请求自己 sleep 的 ttft+stream（与注入面无关，不减会倒挂）；"
                          "repeatInjectionRatio = 端点收到的、本窗口更早某次请求里已逐字出现过的消息字节 ÷ "
                          "窗口内请求体总字节（逐条按序列化比对，非估算）。"
                          "重复字节另按 repeatInjectionBytesSystem（每轮原样重发的注入段）与 "
                          "repeatInjectionBytesHistory（活着的历史与新带上的附件）分列，"
                          "两半的修法不在同一个文件里，混成一个数就没人知道该动哪半边。"
                          "注意 wallMsPerKToken 的分子是链路自己的开销（已减留白），它按调用次数走、"
                          "不按体量走：注入变少时该比值会上升，而绝对墙钟与每任务 tokens 同时下降，"
                          "所以它只能和 wallMsPerTask / engineOverheadMsPerCall / measuredCalls 一起读，"
                          "不能当成「越小越好」的单指标。",
            "injection": "基准默认带注入负载（BENCH_INJECTION=typical|heavy|none）；"
                         "none 复现 2026-11-03 报告的裸提问口径，与那一版数字才可直接对比。",
        },
        "segments": segments,
        "rounds_per_segment": rounds + 1,
        "model_calls": stage1_task_calls,
        "tool_calls": stage1_tool_calls,
        "summary_calls": stage1_summary_calls,
        "model_calls_total": stub.task_calls,
        "endpoint_500_responses": stub.http_500,
        "files_written": files_written,
        "wall_ms": wall_ms,
        "segment_wall_ms": [int(w) for w in walls],
        "usage_events": len(stage1_usage),
        "round_gap_ms": stats(stage1_gaps),
        "first_packet_ms": stats([u.get("firstTokenMs") or 0 for u in stage1_usage]),
        "whole_call_ms": stats([u.get("latencyMs") or 0 for u in stage1_usage]),
        "first_packet_ms_thin_ctx": thinnest.get("firstTokenMs"),
        "whole_call_ms_thin_ctx": thinnest.get("latencyMs"),
        "first_packet_ms_full_ctx": fattest.get("firstTokenMs"),
        "whole_call_ms_full_ctx": fattest.get("latencyMs"),
        "request_bytes_total": request_bytes_total,
        "request_bytes_avg": request_bytes_total // max(len(stage1_bytes), 1),
        "request_bytes_max": max(stage1_bytes) if stage1_bytes else 0,
        "prompt_tokens_total": prompt_tokens_total,
        "prompt_tokens_max": max_prompt,
        "efficiency": efficiency,
        "completion_tokens_reported": sum(
            int(u.get("completionTokens") or 0) for u in stage1_usage),
        "carried_messages": len(msgs),
        "compression_events": compression_events,
        "checkpoint_disk_bytes": disk_bytes,
        "checkpoint_save_ms": stats(save_ms),
        "checkpoint_load_ms": stats(load_ms),
        "preprocess_us": prep_us,
        "payload_bytes_serialized": payload_bytes,
        "restore": {
            "interrupt_wall_ms": int(interrupt_wall),
            "restore_ms": restore_ms,
            "resume_model_calls": resume_calls,
            "skipped_duplicates": max(
                [int(c.get("skippedDuplicates") or 0) for c in r_cp] or [0]),
        },
    }
    print("ENGINE_BENCH_JSON " + json.dumps(out, ensure_ascii=False))

    server.shutdown()
    for path in (work, DATA_ROOT):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    if FAILURES:
        print(f"\nSELFTEST FAILED：{len(FAILURES)} 项未过 — {FAILURES}")
        return 1
    print("\nENGINE BENCH PASSED（数字口径见文件头与报告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
