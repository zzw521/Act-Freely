#!/usr/bin/env python3
"""真模型 · 引擎通道的单任务预算验收（缺陷 D-14.4）

这条补齐 D-14 的另一半。内置 harness 那条链路的真机验收在 Rust 侧
（`harness::runner::tests::real_model_task_budget_pause_then_resume_to_done`，打印
`REALMODEL_JSON`）；引擎这条链路的拦停动作在 Rust 的流式循环里
（`bridge/agent.rs::stream_post` 每收到一批事件就判 `task_budget_blocked`，命中就
`cancel_engine_task` 断流并跳出）。本自测用**真模型**把这条链路的两块前提量出来：

    §1 真实用量到账——引擎的 `usage` 事件里 prompt/completion 不是 0。
       Rust 的计量点（`persist_and_meter`）吃的就是这一份；它要是恒 0，
       「跑满单任务预算」在引擎通道上就是句空话（内置通道已经真为此栽过一次：
       流式不带 `stream_options.include_usage` 就没有用量包）。
    §2 拦停执行器可用——`POST /api/chat/cancel` 在真模型出字的中途能把这条流停掉，
       并且停手之后不再产出 `deliver`。

跑法（Key 只从环境变量进，不落进任何文件，也不打印）：

    TT_API_KEY=sk-xxx tongtian-tauri/.venv/Scripts/python.exe \\
      tongtian-tauri/tests/integration/engine_task_budget_realmodel_selftest.py

口径（必须和结论一起读）：

1. 没设 `TT_API_KEY` 打印一行 SKIP 并以 0 退出（与其余引擎自测同一约定）；
   缺 fastapi/uvicorn/agentscope 同样跳过。
2. 端点就是真服务（缺省百炼兼容地址 + `qwen-plus`），会真的产生少量费用；
   `TT_BASE_URL` / `TT_MODEL` 可覆盖。走的是 `modelConfig.baseUrl` 非空那条
   「自定义端点」路径，与桌面真机一致。
3. 数据根 `TONGTIANWORK_HOME`、工作目录全部指向临时目录，用量台账落在临时根里，
   **不碰**用户真实 `~/.tongtianwork`；隐私模式关闭；文件档位显式 auto_approve，
   绝不弹系统确认框；不写意识记忆；`maxIters` 设小，白烧不了几次调用。
4. §2 验「能不能停 + 停了不再交付 + 停了留下可续的检查点」。**D-14.5 已修**（引擎收尾段
   先认 agentscope 吞下来的中断，`session.cancelled` 或 `metadata._is_interrupted` 为真
   就不许走 deliver 分支），所以这一段是**硬断言**，不再有任何「先不判失败」的豁免。
   成因区分靠 Rust 的 `cancel_engine_task` 带 `reason`（预算拦停发 `BUDGET_TASK_EXCEEDED`）；
   本条 §2 是手工取消、不带 reason，按 `USER_CANCELLED` 记账，两条成因的分支逻辑由
   `tests/integration/engine_task_cancel_selftest.py`（假模型、免 Key）逐条钉住。
"""

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

KEY = (os.environ.get("TT_API_KEY") or "").strip()
if not KEY:
    print('SKIP: 未设置 TT_API_KEY（真模型验收要 Key；引擎自测约定同其余：跳过不等于通过）')
    sys.exit(0)

BASE_URL = (os.environ.get("TT_BASE_URL") or "").strip() or \
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = (os.environ.get("TT_MODEL") or "").strip() or "qwen-plus"

# 数据根要在 import server 之前换掉：检查点、会话、用量台账全落在这里
DATA_ROOT = Path(tempfile.mkdtemp(prefix="tt-engine-budget-home-"))
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
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 缺 uvicorn（{exc}）")
    sys.exit(0)

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def http(method: str, path: str, body: dict | None = None, timeout: int = 20):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    return 200 if response.status < 400 else response.status, raw


BASE = ""
_server = None


def start_engine() -> int:
    """把真引擎按桌面同款形态起在回环上：uvicorn 独立线程 + 空闲端口"""
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
    """SSE 文本 → [(event, data)]；event 缺省按 'message'，data 解析失败留原文"""
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


def usage_rows(events: list) -> list:
    """引擎的 usage 事件形状：{"usage": {"promptTokens", "completionTokens", ...}}"""
    out = []
    for data in events_of(events, "usage"):
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            out.append(usage)
    return out


MODEL_CONFIG = {
    "source": "custom",
    "baseUrl": BASE_URL,
    "apiKey": KEY,
    "modelName": MODEL,
    "displayName": f"真机验收 {MODEL}",
    "contextWindow": 32768,
}
RUNTIME = {
    "awareness": {"mode": True, "autoMemory": False},
    "filePermission": "auto_approve",
    "agentPolicy": {"maxIters": 2, "autoCompress": False},
}


def chat_payload(task_id: str, goal: str, work: Path) -> dict:
    return {
        "message": goal,
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


def main() -> int:
    print(f"--- 0. 真引擎起在回环（模型 {MODEL} @ {BASE_URL}，Key 只在环境变量里）---")
    port = start_engine()
    check("引擎 /health 就绪", bool(BASE), f"port={port}")

    work = Path(tempfile.mkdtemp(prefix="tt-engine-budget-work-"))

    print("--- 1. 真实用量到账：引擎通道的 usage 事件必须带非零 token ---")
    t0 = time.perf_counter()
    code, body = http("POST", "/api/chat",
                      chat_payload("tt-real-usage", "只回答两个字：收到", work), timeout=180)
    usage_wall_ms = int((time.perf_counter() - t0) * 1000)
    check("§1 HTTP 200", code == 200, str(code)[:200])
    events = parse_sse(body)
    errors = events_of(events, "error")
    check("§1 没有错误事件", not errors, str(errors)[:300])
    usages = usage_rows(events)
    check("§1 有用量事件", bool(usages), f"{len(usages)} 条")
    real = [u for u in usages
            if int(u.get("promptTokens") or 0) > 0 and int(u.get("completionTokens") or 0) > 0]
    check("§1 用量非零（Rust 计量点拿得到的就是这一份）", bool(real),
          f"用量事件：{usages[:2]}")
    prompt_total = sum(int(u.get("promptTokens") or 0) for u in usages)
    completion_total = sum(int(u.get("completionTokens") or 0) for u in usages)
    check("§1 有 done 收尾", bool(events_of(events, "done")), str(events[-2:])[:300])

    # 用量台账（model-usage.jsonl）也在临时数据根里长出同一批真实数字。
    # 增量行的形状是 {"v","kind","atMs","key":{...},"d":{"pt","ct",...}}：
    # token 在 `d` 里，不在顶层（顶层取键会把 2010+1 读成 0，看着像缺陷其实是读错地方）。
    ledger_file = DATA_ROOT / "usage" / "model-usage.jsonl"
    ledger_lines: list = []
    if ledger_file.exists():
        ledger_lines = [line for line in
                        ledger_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        if line.strip()]
    metered = 0
    for line in ledger_lines:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        delta = row.get("d") if isinstance(row.get("d"), dict) else row
        metered += int(delta.get("pt") or 0) + int(delta.get("ct") or 0)
    check("§1 用量台账落盘且非零（临时数据根，不碰 ~/.tongtianwork）",
          bool(ledger_lines) and metered > 0,
          f"{len(ledger_lines)} 行，累计 {metered} tokens；样例行 {ledger_lines[:1]}")

    print("--- 2. 拦停执行器：真模型出字中途 POST /api/chat/cancel 要能断流 ---")
    stream_box: dict = {}

    def run_long() -> None:
        started = time.perf_counter()
        try:
            status, raw = http("POST", "/api/chat",
                               chat_payload("tt-real-cancel",
                                            "写一篇不少于 1200 字的说明文，分六段逐段展开，"
                                            "题目是「黄河的改道史」", work),
                               timeout=240)
            stream_box.update(status=status, body=raw,
                              wall_ms=int((time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            stream_box.update(error=f"{type(exc).__name__}: {exc}",
                              wall_ms=int((time.perf_counter() - started) * 1000))

    worker = threading.Thread(target=run_long, daemon=True)
    worker.start()
    # 1200 字的正文在第一次模型调用里就流不完，1 秒后下手必然是「正在出字的时候打断」；
    # 早于完成才测得出断流，晚了就变成「它自己跑完了」（上一轮 2 秒就收到 deliver 正是这样）
    time.sleep(1.0)
    c_code, c_body = http("POST", "/api/chat/cancel", {"task_id": "tt-real-cancel"})
    check("§2 取消端点应答 200", c_code == 200, str(c_body)[:200])
    try:
        verdict = json.loads(c_body)
    except Exception:  # noqa: BLE001
        verdict = {}
    check("§2 任务在册且被取消（cancelled=true）",
          bool(verdict.get("cancelled")), str(verdict)[:200])
    worker.join(timeout=120)
    check("§2 流在取消后收束（没有挂在引擎里）", not worker.is_alive(),
          str(stream_box)[:200])
    cancel_events = parse_sse(stream_box.get("body", ""))
    cancel_usages = usage_rows(cancel_events)
    # 被打断的任务到底回了什么：把事件序列原文留在证据行里，别靠断言失败信息猜
    trace = [
        {
            "kind": kind,
            "head": json.dumps(data, ensure_ascii=False)[:160],
        }
        for (kind, data) in cancel_events
    ]
    print("ENGINECANCEL_TRACE " + json.dumps({
        "httpStatus": stream_box.get("status"),
        "error": stream_box.get("error"),
        "wallMs": stream_box.get("wall_ms"),
        "events": trace[:14],
    }, ensure_ascii=False))
    delivered = events_of(cancel_events, "deliver")
    # D-14.5 修复后的硬断言：agentscope 会把中断吞成「正常返回」（_agent_base.py:458 →
    # handle_interrupt 回一句英文兜底话并带 metadata._is_interrupted），引擎收尾段现在
    # 先认这件事，被取消/被拦停的任务一律改道到 cancelled 分支——不再 deliver、
    # 检查点留成可续跑（否则「调高预算后点继续」根本没有东西可续）。
    check("§2 停手后不再交付正文（没有 deliver）", not delivered, str(delivered)[:200])
    cancel_statuses = events_of(cancel_events, "status_change")
    check("§2 收尾状态是 cancelled 且成因在码表里",
          any(s.get("status") == "cancelled"
              and (s.get("statusReasonCode") or "") in ("USER_CANCELLED",
                                                         "BUDGET_TASK_EXCEEDED")
              for s in cancel_statuses),
          str(cancel_statuses)[:250])
    cancel_finals = events_of(cancel_events, "checkpoint")
    check("§2 被停下的任务留下可续跑的检查点",
          bool(cancel_finals) and bool(cancel_finals[-1].get("resumable")),
          str(cancel_finals[-1:])[:250])
    check("§2 检查点原因说的是停手，不是「任务已正常完成」",
          bool(cancel_finals)
          and "正常完成" not in str(cancel_finals[-1].get("reason") or "")
          and "继续" in str(cancel_finals[-1].get("reason") or ""),
          str(cancel_finals[-1:])[:250])
    # 在途调用的用量半边（D-14.5 的计量边界）：包装层的收口已挪进 `_wrap_stream` 的
    # finally，末包已经到达的那一份不再因为流被中途关掉而丢掉；取消发生在末包之前时，
    # 那一次调用本来就没有 usage 包可拿 ⇒ 仍然少计一次（不会多计）。
    # 如实留成 finding：**没收到就不写**，自测与产品都不许拿估算值填台账。
    print("ENGINEBUDGET_FINDING " + json.dumps({
        "inflightCallUnmetered": len(cancel_usages) == 0,
        "usageEventsOnCancelledTask": len(cancel_usages),
        "note": "取消落在模型调用中途时，若用量包尚未到达则这一次不进台账（少计一次，"
                "不会多计，也不会写估算值）；已到达的用量由 _wrap_stream 的 finally 收口落账。",
    }, ensure_ascii=False))
    check("§2 取消任务的用量事件只可能是真实到账的（有事件就必须是正数）",
          all(int(u.get("promptTokens") or 0) > 0
              and int(u.get("completionTokens") or 0) > 0 for u in cancel_usages),
          str(cancel_usages)[:200])
    # 如实记下当前边界：引擎的取消分支只有一种说法，因为 Rust 只传 task_id、不传原因
    reason_text = json.dumps(
        [d for (k, d) in cancel_events if k == "status_change"], ensure_ascii=False
    )
    print("ENGINEBUDGET_FINDING " + json.dumps({
        "engineCancelReasonText": reason_text[:200],
        "engineSaysUserCancelled": "用户取消" in reason_text,
        "note": "本条 §2 是**手工取消**（不带 reason），所以引擎按 USER_CANCELLED 记账；"
                "Rust 的预算拦停那条路（bridge/agent.rs::stream_post）带 "
                "BUDGET_TASK_EXCEEDED，引擎侧两条成因已经分道（区分逻辑见 "
                "engine_task_cancel_selftest.py 的 §3）。",
    }, ensure_ascii=False))

    print("--- 3. 数据根隔离自查 ---")
    try:
        from data_paths import engine_home

        resolved = str(engine_home())
    except Exception as exc:  # noqa: BLE001
        resolved = f"engine_home 不可读：{exc}"
    check("§3 引擎数据根指向临时目录（真实 ~/.tongtianwork 未被动过）",
          resolved.startswith(str(DATA_ROOT)), resolved)
    print("ENGINEUSAGE_JSON " + json.dumps({
        "channel": "engine",
        "model": MODEL,
        "baseUrlHost": BASE_URL.split("//")[-1].split("/")[0],
        "usageEvents": len(usages),
        "promptTokensTotal": prompt_total,
        "completionTokensTotal": completion_total,
        "meteredLedgerTokens": metered,
        "ledgerLines": len(ledger_lines),
        "wallMs": usage_wall_ms,
        "cancelWallMs": stream_box.get("wall_ms"),
        "keyPrinted": False,
        "dataRoot": str(DATA_ROOT),
    }, ensure_ascii=False))

    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    try:
        start = time.perf_counter()
        rc = main()
        print(f"真模型引擎验收耗时 {int((time.perf_counter() - start) * 1000)} ms")
        if _server is not None:
            _server.should_exit = True
        shutil.rmtree(DATA_ROOT, ignore_errors=True)
        if FAILURES:
            print("FAILURES: " + ", ".join(FAILURES))
        sys.exit(1 if FAILURES else rc)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        shutil.rmtree(DATA_ROOT, ignore_errors=True)
        sys.exit(1)
