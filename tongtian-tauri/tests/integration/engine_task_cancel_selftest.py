#!/usr/bin/env python3
"""缺陷 D-14.5：被取消 / 被预算拦停的任务不许谎报「任务完成」

真模型验收（`engine_task_budget_realmodel_selftest.py`）暴露出这条：agentscope 的
`AgentBase.__call__` 把 `CancelledError` 吞在自己肚子里（`_agent_base.py:458`），
改调 `handle_interrupt` 回一句 `I noticed that you have interrupted me. What can I do
for you?` 并在 `metadata["_is_interrupted"]` 里留了记号（`_react_agent.py:814`）。
引擎收到的是**一次正常返回**，于是照旧 `deliver「任务完成」`、照旧把检查点标成
`resumable=false`（理由「任务已正常完成，无需续跑」）——引擎通道上「调高预算后点继续」
根本没有可续的检查点。

本自测用**假模型**把这条路径钉死，不需要 Key、不联网：

    §1 判定函数：中断记号认得 / 不认得哪些形状，取消成因文案与码表对齐
    §2 中断不是取消端点发起的（`session.cancelled` 为假）⇒ 只靠 metadata 也要改道
    §3 真取消端点带成因码（预算拦停那条路）⇒ 状态码、检查点原因、无 deliver
    §4 成因码不在表里 ⇒ 取消照样成功，回落「用户手动取消」，不拿一个说明字段让取消失败
    §5 在途调用的用量收口：流被中途关掉时，**已经到达的 usage 必须落账**；
       一个都没收到就什么都不写（不许用估算值填台账）
    §6 数据根隔离自查（假用量与假任务都不许进真实 `~/.tongtianwork`）

跑法：

    cd tongtian-tauri && ./vendor/python/python.exe -X utf8 \\
      tests/integration/engine_task_cancel_selftest.py

判定只看 `^[PASS]` 行；缺 fastapi/uvicorn/agentscope 时打印一行 SKIP 并以 0 退出
（与其余引擎自测同一约定，跳过不等于通过）。
"""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 数据根要在 import server 之前换掉：检查点、会话、用量台账全落在这里
DATA_ROOT = Path(tempfile.mkdtemp(prefix="tt-engine-cancel-home-"))
os.environ["TONGTIANWORK_HOME"] = str(DATA_ROOT)
os.environ["TT_ENABLE_SEARCH"] = "false"

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as es  # noqa: E402
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 引擎依赖不可用（{exc}）")
    sys.exit(0)

try:
    import uvicorn  # noqa: E402
    from agentscope.formatter import DashScopeChatFormatter  # noqa: E402
    from agentscope.model import ChatModelBase  # noqa: E402
    from agentscope.model._model_response import ChatResponse  # noqa: E402
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 缺 uvicorn / agentscope（{exc}）")
    sys.exit(0)

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


BASE = ""
_server = None


def http(method: str, path: str, body: dict | None = None, timeout: int = 30):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            code = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    return code, raw


def start_engine() -> int:
    """真引擎按桌面同款形态起在回环上：uvicorn 独立线程 + 空闲端口"""
    global BASE, _server
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(es.app, host="127.0.0.1", port=port,
                            log_level="error", access_log=False)
    _server = uvicorn.Server(config)
    threading.Thread(target=_server.run, daemon=True).start()
    BASE = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            code, _body = http("GET", "/health")
            if code == 200:
                return port
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    raise RuntimeError("引擎启动超时")


def parse_sse(text: str) -> list:
    events: list = []
    for block in text.split("\n\n"):
        name = "message"
        payload = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload += line[5:].strip()
        if not payload:
            continue
        try:
            events.append((name, json.loads(payload)))
        except Exception:  # noqa: BLE001
            events.append((name, {"raw": payload[:200]}))
    return events


def events_of(events: list, name: str) -> list:
    return [data for (kind, data) in events if kind == name]


# ── 假模型：两种中断形状 ────────────────────────────────────────

# INTERRUPT_TEXT 是 agentscope handle_interrupt 的兜底正文；断言「没被当成交付」时用它
INTERRUPT_TEXT = "I noticed that you have interrupted me."

MODEL_CALLS: list = []
REACHED = threading.Event()      # 模型已被调到（此时会话一定在册，可以下手取消）
RELEASE = threading.Event()      # 放行：真取消会打断这个 await，没取消就自己走完


class InterruptingModel(ChatModelBase):
    """一次调用：先等放行。

    mode="cancel" —— 自己抛 CancelledError，模拟「中断不是引擎的取消端点发起的」
                     （例如内置 interrupt 通道），此时 session.cancelled 仍为假。
    mode="wait"   —— 睡到被放行；外部在这期间 POST /api/chat/cancel，等待点就会被
                     注入 CancelledError，与真机同一条路。
    """

    def __init__(self, mode: str = "wait"):
        super().__init__(model_name="fake-cancel-model", stream=False)
        self.mode = mode

    async def __call__(self, messages, *args, **kwargs):
        MODEL_CALLS.append(self.mode)
        REACHED.set()
        if self.mode == "cancel":
            await asyncio.sleep(0.05)
            raise asyncio.CancelledError()
        # 等外部下手：30 秒是上限而不是测量对象，判定不靠时长
        for _ in range(300):
            if RELEASE.is_set():
                break
            await asyncio.sleep(0.1)
        return ChatResponse(content=[{"type": "text", "text": "没被打断的话术"}])


def install_model(mode: str) -> None:
    MODEL_CALLS.clear()
    REACHED.clear()
    RELEASE.clear()
    model = InterruptingModel(mode)
    es.build_model_and_formatter = (
        lambda *a, **k: (model, DashScopeChatFormatter())
    )


MODEL_CONFIG = {
    "source": "custom",
    "baseUrl": "https://127.0.0.1:9/v1",
    "apiKey": "fake-key-not-used",
    "modelName": "fake-cancel-model",
    "displayName": "假模型（取消收尾验收）",
    "contextWindow": 32768,
}
RUNTIME = {
    "awareness": {"mode": True, "autoMemory": False},
    "filePermission": "auto_approve",
    "agentPolicy": {"maxIters": 2, "autoCompress": False},
}


def chat_payload(task_id: str, work: Path) -> dict:
    return {
        "message": "写一段话，不要调用任何工具",
        "task_id": task_id,
        "modelConfig": MODEL_CONFIG,
        "runtime": RUNTIME,
        "agentPolicy": RUNTIME["agentPolicy"],
        "workDir": str(work),
        "filePermission": "auto_approve",
        "history": [],
        "mode": "general",
        "privacyMode": False,
    }


def run_chat_and_cancel(task_id: str, work: Path, cancel_body: dict | None,
                        ) -> tuple[list, dict]:
    """在后台线程里跑一轮 /api/chat，等模型到位后下手取消，返回（事件序列, 取消回执）"""
    box: dict = {}

    def worker() -> None:
        try:
            code, body = http("POST", "/api/chat", chat_payload(task_id, work), timeout=120)
            box.update(code=code, body=body)
        except Exception as exc:  # noqa: BLE001
            box.update(error=f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    reached = REACHED.wait(timeout=60)
    verdict: dict = {}
    if cancel_body is not None and reached:
        code, raw = http("POST", "/api/chat/cancel", cancel_body)
        try:
            verdict = json.loads(raw)
        except Exception:  # noqa: BLE001
            verdict = {"raw": raw[:200]}
        verdict["_http"] = code
    RELEASE.set()
    thread.join(timeout=120)
    events = parse_sse(box.get("body", ""))
    if box.get("error"):
        events.append(("transport_error", {"message": box["error"]}))
    return events, verdict


# ── §1 判定函数 ────────────────────────────────────────────────

def unit_checks() -> None:
    print("--- 1. 中断判定与成因码（纯函数，形状逐条钉）---")
    check("§1 agentscope 的记号名与本模块常量一致",
          es.AGENT_INTERRUPTED_METADATA_KEY == "_is_interrupted",
          es.AGENT_INTERRUPTED_METADATA_KEY)

    class _Msg:
        def __init__(self, metadata):
            self.metadata = metadata

    check("§1 metadata 带记号 ⇒ 认成中断",
          es._agent_reported_interruption(_Msg({"_is_interrupted": True})) is True)
    check("§1 metadata 记号为假 ⇒ 不算中断",
          es._agent_reported_interruption(_Msg({"_is_interrupted": False})) is False)
    check("§1 metadata 是别的字典 ⇒ 不算中断",
          es._agent_reported_interruption(_Msg({"other": 1})) is False)
    check("§1 metadata 为 None ⇒ 不算中断（不许抛）",
          es._agent_reported_interruption(_Msg(None)) is False)
    check("§1 整个对象没有 metadata ⇒ 不算中断（不许抛）",
          es._agent_reported_interruption(object()) is False)
    check("§1 metadata 不是字典（字符串）⇒ 不算中断（不许抛）",
          es._agent_reported_interruption(_Msg("_is_interrupted")) is False)
    check("§1 结果为 None ⇒ 不算中断（不许抛）",
          es._agent_reported_interruption(None) is False)

    check("§1 成因码表就是这两条（不多造码）",
          list(es.CANCEL_REASONS) == ["USER_CANCELLED", "BUDGET_TASK_EXCEEDED"],
          str(es.CANCEL_REASONS))
    note_budget = es._cancel_note("BUDGET_TASK_EXCEEDED")
    note_user = es._cancel_note("USER_CANCELLED")
    check("§1 预算拦停的中文原因说清是谁停的",
          "单任务预算已用满" in note_budget and "检查点继续" in note_budget, note_budget)
    check("§1 用户取消的中文原因不冒充预算",
          "用户取消" in note_user and "预算" not in note_user, note_user)
    check("§1 空成因按用户取消说（不编第三种说法）",
          es._cancel_note("") == note_user, es._cancel_note(""))
    check("§1 未知成因也按用户取消说",
          es._cancel_note("WHATEVER") == note_user, es._cancel_note("WHATEVER"))
    check("§1 与内置执行体的预算文案同句（两条通道不许两套说法）",
          "单任务预算已用满，可从最近一次模型调用前的检查点继续" in note_budget, note_budget)


# ── §2 中断不是取消端点发起的 ──────────────────────────────────

def interrupted_without_flag(work: Path) -> None:
    print("--- 2. 模型自己承认被打断（没有走取消端点）⇒ 不许 deliver「任务完成」---")
    install_model("cancel")
    events, _verdict = run_chat_and_cancel("cancel-by-metadata", work, None)
    delivered = events_of(events, "deliver")
    statuses = events_of(events, "status_change")
    finals = events_of(events, "checkpoint")
    errors = events_of(events, "error")
    print("CANCELTRACE " + json.dumps({
        "kinds": [kind for (kind, _d) in events][:20],
        "deliverCount": len(delivered),
        "statusChanges": statuses[:3],
        "checkpoint": finals[-1] if finals else None,
        "errors": errors[:2],
    }, ensure_ascii=False)[:900])
    check("§2 任务真的被模型调用过（假模型到位）", bool(MODEL_CALLS), str(MODEL_CALLS))
    check("§2 没有任何 deliver（旧行为是发一条「任务完成」）", not delivered,
          str(delivered)[:200])
    check("§2 收尾状态是 cancelled",
          any(s.get("status") == "cancelled" for s in statuses), str(statuses)[:200])
    check("§2 状态成因回落码表内的用户取消",
          any(s.get("statusReasonCode") == "USER_CANCELLED" for s in statuses),
          str(statuses)[:200])
    check("§2 检查点保留为可续跑（旧行为是 resumable=false）",
          bool(finals) and finals[-1].get("resumable") is True, str(finals[-1:])[:250])
    check("§2 检查点原因写的是取消而不是「已正常完成」",
          bool(finals) and "用户取消" in str(finals[-1].get("reason") or ""),
          str(finals[-1:])[:250])
    check("§2 那句英文兜底话没有被当成交付正文",
          not any(INTERRUPT_TEXT in str(d) for d in delivered), str(delivered)[:200])
    check("§2 没有因为改道而冒出 AGENT_ERROR",
          not any((e.get("code") or "") == "AGENT_ERROR" for e in errors), str(errors)[:200])
    check("§2 收尾顺序仍是 checkpoint 在 done 之前",
          [kind for (kind, _d) in events][-2:] == ["checkpoint", "done"],
          str([kind for (kind, _d) in events][-3:]))


# ── §3 带成因码的取消（预算拦停那条路）────────────────────────

def cancel_with_budget_reason(work: Path) -> None:
    print("--- 3. POST /api/chat/cancel 带 BUDGET_TASK_EXCEEDED ⇒ 成因如实分道 ---")
    install_model("wait")
    events, verdict = run_chat_and_cancel(
        "cancel-by-budget", work, {"task_id": "cancel-by-budget",
                                   "reason": "BUDGET_TASK_EXCEEDED"})
    delivered = events_of(events, "deliver")
    statuses = events_of(events, "status_change")
    finals = events_of(events, "checkpoint")
    print("CANCELTRACE " + json.dumps({
        "cancelReply": verdict,
        "kinds": [kind for (kind, _d) in events][:20],
        "deliverCount": len(delivered),
        "statusChanges": statuses[:3],
        "checkpoint": finals[-1] if finals else None,
    }, ensure_ascii=False)[:900])
    check("§3 取消端点应答 200", verdict.get("_http") == 200, str(verdict)[:200])
    check("§3 任务在册且被取消", bool(verdict.get("cancelled")), str(verdict)[:200])
    check("§3 取消回执把真实成因码回给调用方（Rust/界面按它显示是谁停的）",
          verdict.get("statusReasonCode") == "BUDGET_TASK_EXCEEDED", str(verdict)[:200])
    check("§3 没有 deliver「任务完成」", not delivered, str(delivered)[:200])
    check("§3 状态事件成因是预算拦停，不是用户取消",
          any(s.get("statusReasonCode") == "BUDGET_TASK_EXCEEDED" for s in statuses),
          str(statuses)[:200])
    check("§3 检查点可续跑", bool(finals) and finals[-1].get("resumable") is True,
          str(finals[-1:])[:250])
    check("§3 检查点原因说「单任务预算已用满」",
          bool(finals) and "单任务预算已用满" in str(finals[-1].get("reason") or ""),
          str(finals[-1:])[:250])
    check("§3 流在取消之后收束（没挂在引擎里）", bool(events), "事件序列为空")


# ── §4 未知成因码不许拖累取消 ─────────────────────────────────

def cancel_with_unknown_reason(work: Path) -> None:
    print("--- 4. 成因码不在表里 ⇒ 取消照样成功并回落用户取消 ---")
    install_model("wait")
    events, verdict = run_chat_and_cancel(
        "cancel-bad-reason", work, {"task_id": "cancel-bad-reason",
                                     "reason": "我看它不顺眼"})
    statuses = events_of(events, "status_change")
    finals = events_of(events, "checkpoint")
    check("§4 未知成因码不拒绝取消", bool(verdict.get("cancelled")), str(verdict)[:200])
    check("§4 未知成因码不进判定，回执按用户取消",
          verdict.get("statusReasonCode") == "USER_CANCELLED", str(verdict)[:200])
    check("§4 不带 reason 字段（界面点「停止」那条路）仍按用户取消",
          all(s.get("statusReasonCode") == "USER_CANCELLED" for s in statuses
              if s.get("status") == "cancelled") and any(
              s.get("status") == "cancelled" for s in statuses), str(statuses)[:200])
    check("§4 这一轮的收尾仍是取消态",
          bool(finals) and finals[-1].get("resumable") is True
          and not events_of(events, "deliver"), str(finals[-1:])[:250])


# ── §5 在途调用的用量收口 ─────────────────────────────────────

def usage_wrap_checks() -> None:
    print("--- 5. 流被中途关掉：已到达的 usage 必须落账，没到达的什么都不写 ---")

    class _Inner:
        model_name = "fake-stream-model"
        stream = True

    def tracker(sink, chunks):
        seen = list(chunks)

        async def _gen():
            for chunk in seen:
                yield chunk

        model = es.UsageTrackingModel(_Inner(), on_usage=sink, prices=None, origin="direct")
        return model, _gen()

    def chunk(prompt: int | None, completion: int | None, text: str = "字"):
        usage = None
        if prompt is not None and completion is not None:
            usage = {"input_tokens": prompt, "output_tokens": completion}
        return {"content": [{"type": "text", "text": text}], "usage": usage, "metadata": None}

    async def consume_and_close(chunks, take: int) -> list:
        calls: list = []
        model, agen = tracker(lambda *a: calls.append(a), chunks)
        got = 0
        stream = model._wrap_stream(agen, time.monotonic())
        async for item in stream:
            got += 1
            if got >= take:
                break
        await stream.aclose()
        return calls

    async def consume_all(chunks) -> list:
        calls: list = []
        model, agen = tracker(lambda *a: calls.append(a), chunks)
        async for _item in model._wrap_stream(agen, time.monotonic()):
            pass
        return calls

    # ① usage 已经到达、流被中途关掉 ⇒ 落账一次（这正是原来会丢的那一份）
    arrived = asyncio.run(consume_and_close(
        [chunk(120, 40, "第一段"), chunk(120, 40), chunk(120, 40)], take=2))
    check("① 已到达的 usage 不因中途关闭而丢（旧行为：一次都不落）",
          len(arrived) == 1, f"落账 {len(arrived)} 次：{arrived}")
    check("① 落的是真实收到的那一份（120/40，不是估算）",
          len(arrived) == 1 and arrived[0][0] == 120 and arrived[0][1] == 40, str(arrived))

    # ② usage 只在末包出现（DashScope/OpenAI 的真实形状）⇒ 提前关闭时没有可落的
    early = asyncio.run(consume_and_close(
        [{"content": [{"type": "text", "text": "第一段"}], "usage": None, "metadata": None},
         {"content": [{"type": "text", "text": "第二段"}], "usage": None, "metadata": None},
         chunk(200, 66)], take=1))
    check("② 末包才带 usage 而流被提前关掉 ⇒ 什么都不写（不许拿估算填台账）",
          early == [], str(early)[:200])

    # ③ 正常消费完 ⇒ 恰好落一次，不重复累计
    whole = asyncio.run(consume_all([chunk(90, 10), chunk(90, 25)]))
    check("③ 消费完整条流仍只落一次账（取最后一次非空 usage）",
          len(whole) == 1 and whole[0][0] == 90 and whole[0][1] == 25, str(whole))

    # ④ 中途抛异常也收口：异常前到达的 usage 不丢
    async def boom_after() -> list:
        calls: list = []

        async def _gen():
            yield chunk(50, 5)
            raise RuntimeError("上游断了")

        model = es.UsageTrackingModel(_Inner(), on_usage=lambda *a: calls.append(a),
                                      prices=None, origin="direct")
        stream = model._wrap_stream(_gen(), time.monotonic())
        try:
            async for _item in stream:
                pass
        except RuntimeError:
            pass
        return calls

    broken = asyncio.run(boom_after())
    check("④ 调用中途抛异常 ⇒ 已到达的 usage 仍然落账",
          len(broken) == 1 and broken[0][0] == 50, str(broken))

    # ⑤ 台账真的写到了临时数据根，而不是用户目录
    ledger_root = str(es.USAGE_LEDGER.path) if hasattr(es.USAGE_LEDGER, "path") else ""
    check("⑤ 用量台账文件在临时数据根里（真实 ~/.tongtianwork 未被动过）",
          (not ledger_root) or ledger_root.startswith(str(DATA_ROOT)), ledger_root)


# ── §6 数据根与源码形状 ───────────────────────────────────────

def closing_checks() -> None:
    print("--- 6. 数据根隔离与收尾段形状 ---")
    try:
        from data_paths import engine_home

        resolved = str(engine_home())
    except Exception as exc:  # noqa: BLE001
        resolved = f"engine_home 不可读：{exc}"
    check("§6 引擎数据根指向临时目录", resolved.startswith(str(DATA_ROOT)), resolved)

    src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    check("§6 收尾段确实先判中断再走成功路径（顺序是这条修复的全部）",
          src.index("result = await agent(msg)") < src.index(
              "if session.cancelled or _agent_reported_interruption(result):"))
    check("§6 成功收尾的 deliver 只有一处且不在中断判定之前",
          src.count('"summary": "任务完成"') == 1, str(src.count('"summary": "任务完成"')))
    check("§6 取消端点记下成因码", "session.cancel_reason = reason" in src)
    check("§6 会话重开时清掉上一轮的成因（不把上一轮的账带到这一轮）",
          src.count("session.cancel_reason = \"\"") >= 2)
    stream_src = src[src.index("async def _wrap_stream"):]
    stream_src = stream_src[:stream_src.index("def _usage_trackers")]
    check("§6 流式收口在 finally 里（被取消/抛异常都走到）",
          "finally:" in stream_src and "self._record(last," in stream_src.split("finally:")[1],
          stream_src[:300])
    check("§6 收口只有一处 _record（不会既在循环后又在 finally 里各落一次）",
          stream_src.count("self._record(") == 1, str(stream_src.count("self._record(")))

    # 豁免必须随修复一起消失：真模型那条自测不许再打 ENGINEBUDGET_KNOWN_DEFECT
    exempt = (PROJECT_ROOT / "tests" / "integration"
              / "engine_task_budget_realmodel_selftest.py").read_text(encoding="utf-8")
    check("§6 真模型自测不再打「已知缺陷」豁免行",
          "ENGINEBUDGET_KNOWN_DEFECT" not in exempt,
          "该行仍在，说明 §2 还允许 deliver「任务完成」")
    check("§6 真模型自测不再有「暂不计入失败」的说法",
          "暂不计入失败" not in exempt, "找到旧措辞")
    check("§6 真模型自测把同一件事改成硬断言",
          "停手后不再交付正文" in exempt and "check(" in exempt)


def main() -> int:
    start_engine()
    work = Path(tempfile.mkdtemp(prefix="tt-engine-cancel-work-"))
    try:
        unit_checks()
        interrupted_without_flag(work)
        cancel_with_budget_reason(work)
        cancel_with_unknown_reason(work)
        usage_wrap_checks()
        closing_checks()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        started = time.perf_counter()
        rc = main()
        print(f"取消收尾自测耗时 {int((time.perf_counter() - started) * 1000)} ms")
        if _server is not None:
            _server.should_exit = True
        shutil.rmtree(DATA_ROOT, ignore_errors=True)
        if FAILURES:
            print("FAILURES: " + ", ".join(FAILURES))
        print("ALL PASS" if not FAILURES else "HAS FAILURE")
        sys.exit(1 if FAILURES else rc)
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        shutil.rmtree(DATA_ROOT, ignore_errors=True)
        sys.exit(1)
