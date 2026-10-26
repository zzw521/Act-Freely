#!/usr/bin/env python3
"""
任务检查点 / 断点续跑自测

锁住「中断 → 检查点 → 续跑 → 副作用不重复」这条链，防止出现
「界面上有『从检查点继续』，真实任务里恢复不了或把写盘重做一遍」的假承诺：

1. 纯函数/单元层：副作用工具判定、调用签名稳定性、同一轮重复调用不误判、
   快照媒体标记与超限兜底、末尾用户消息不重复恢复、正常收尾清除快照
2. /api/chat 端到端（假模型）：工具写盘后模型中断 → checkpoint 事件
   resumable=true、原因可读、快照含已完成的工具结果
3. /api/chat/review action=resume：恢复记忆与副作用台账；模型重发同签名写盘时
   被跳过并留下可见说明；已产出的文件不被覆盖；续跑成功后检查点清除
4. 二次恢复被 409 明确拒绝；无检查点恢复给出 404 明确原因
5. 首次模型调用前就失败：如实标为不可续跑，不展示点了也没用的入口
6. 磁盘检查点：非隐私中断落盘、隐私不落盘、查询接口只返回摘要（不含正文/密钥）
7. 模拟引擎重启（清空进程内会话）后按磁盘续跑并跳过旧副作用，成功后删除文件
8. 模型/目录不可恢复与写盘失败时如实拒绝或降级，不承诺刷新后还能继续

离线运行：不联网、无需 API Key；WORK_DIR 与 TONGTIANWORK_HOME 都换成临时目录，
不触碰用户数据。fastapi/agentscope 缺失时 SKIP 并以 0 退出。

用法: python tests/integration/engine_checkpoint_selftest.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import checkpoint_runtime as ck
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: checkpoint runtime selftest unavailable ({exc})")
    sys.exit(0)

try:
    from fastapi.testclient import TestClient
    import server as engine_server
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.tool import ToolResponse
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 端到端依赖缺失（{exc}）")
    engine_server = None
    ChatModelBase = ChatResponse = DashScopeChatFormatter = Msg = ToolResponse = None

FAILURES: list = []

ARTIFACT = "artifact.txt"
ARTIFACT_MARK = "CHECKPOINT-ARTIFACT"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _blob(messages) -> str:
    try:
        return json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(messages)


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev = {}
        for line in block.split("\n"):
            if line.startswith("id:"):
                ev["id"] = line[3:].strip()
            elif line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    ev["data"] = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    ev["data"] = {"raw": line[5:].strip()}
        if ev.get("event") or ev.get("data"):
            events.append(ev)
    return events


def events_of(events, name: str):
    return [e.get("data") or {} for e in events if e.get("event") == name]


# ── 1. 单元层 ──────────────────────────────────────────────────

class FakeToolResponse:
    def __init__(self, text: str = "已写入"):
        self.content = [{"type": "text", "text": text}]
        self.metadata = {}
        self.is_interrupted = False


class FakeMsg:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "")
        self.role = kwargs.get("role", "user")
        self.content = kwargs.get("content", "")
        self.metadata = kwargs.get("metadata") or {}
        self.timestamp = kwargs.get("timestamp")


class FakeMemory:
    def __init__(self):
        self.items = []

    async def add(self, msg):
        self.items.append(msg)


class FakeAgent:
    def __init__(self):
        self.memory = FakeMemory()


def unit_checks() -> None:
    print("--- 1. 副作用判定、签名与快照（单元层） ---")
    check("写盘/外发/执行类工具按副作用处理",
          all(ck.is_side_effect_tool(n) for n in
              ("write_text_file", "insert_text_file", "send_email", "delete_file", "shell_exec")),
          str([n for n in ("write_text_file", "insert_text_file", "send_email", "delete_file", "shell_exec")
               if not ck.is_side_effect_tool(n)]))
    check("只读工具不记副作用台账",
          not ck.is_side_effect_tool("view_text_file") and not ck.is_side_effect_tool("use_skill"))
    check("未知且不像写操作的工具有独立判定",
          not ck.is_side_effect_tool("web_search"))

    sig1 = ck.signature_of("write_text_file", {"b": 2, "a": 1})
    sig2 = ck.signature_of("write_text_file", {"a": 1, "b": 2})
    sig3 = ck.signature_of("write_text_file", {"a": 1, "b": 3})
    check("同输入不同键序签名一致、输入变化签名变化",
          sig1 == sig2 and sig1 != sig3, f"{sig1[:8]} {sig2[:8]} {sig3[:8]}")

    cp = ck.TaskCheckpoint("unit")
    check("首次模型调用前如实说明无可恢复点",
          "首次模型调用之前" in cp.unavailable_reason(), cp.unavailable_reason())

    cp.begin_run()
    huge_list = [{"type": "text", "text": "长" * 4000} for _ in range(120)]
    ok = cp.capture_memory([
        FakeMsg(name="system", role="system", content="系统提示"),
        FakeMsg(name="user", role="user", content="写一个文件"),
        FakeMsg(name="assistant", role="assistant", content=[
            {"type": "tool_use", "id": "t1", "name": "write_text_file",
             "input": {"file_path": ARTIFACT, "content": ARTIFACT_MARK}},
        ]),
        FakeMsg(name="system", role="system", content=[
            {"type": "tool_result", "id": "t1", "name": "write_text_file",
             "output": [{"type": "text", "text": "写入成功"}]},
            {"type": "image", "source": {"type": "base64", "data": "..."}},
        ]),
    ])
    blob = json.dumps(cp.messages, ensure_ascii=False)
    check("快照采集成功并记录媒体未保存标记",
          ok and cp.model_calls == 1 and "未保存在内存快照中" in blob, blob[:200])
    check("快照存在时可续跑", cp.unavailable_reason() == "", cp.unavailable_reason())

    sig = ck.signature_of("write_text_file", {"file_path": ARTIFACT, "content": ARTIFACT_MARK})
    cp.record_tool("write_text_file", sig, FakeToolResponse("写入成功"))
    check("同一轮内的重复调用不被当作旧记录", cp.previous_record(sig) is None)
    cp.begin_run()
    check("续跑轮能识别上一轮已成功执行的副作用", cp.previous_record(sig) is not None)

    restored_cp = ck.TaskCheckpoint("unit-restored")
    restore_err = restored_cp.restore_from_disk({
        "savedAtMs": int(time.time() * 1000),
        **cp.disk_payload(),
    })
    restored_cp.begin_run()
    check("跨进程恢复沿用落盘轮次（旧副作用不会被当成新一轮重做）",
          restore_err == "" and restored_cp.run_id == cp.run_id + 1
          and restored_cp.previous_record(sig) is not None,
          f"err={restore_err}, run={restored_cp.run_id}, prev={restored_cp.previous_record(sig) is not None}")

    async def _middleware_case():
        called = {"next": False}

        async def next_handler(**kwargs):
            called["next"] = True
            yield ToolResponse(content=[{"type": "text", "text": "不该执行"}])

        emitted = []
        cp.emit = lambda ev, data: emitted.append((ev, data))
        chunks = [
            chunk async for chunk in ck.tool_middleware(cp)(
                {"tool_call": {"name": "write_text_file",
                               "input": {"file_path": ARTIFACT, "content": ARTIFACT_MARK}}},
                next_handler,
            )
        ]
        return called["next"], chunks, emitted

    skipped_next, chunks, emitted = asyncio.run(_middleware_case())
    text = _blob([getattr(c, "content", None) for c in chunks])
    check("续跑时同签名副作用被跳过，真实工具没有被调用", not skipped_next, text)
    check("跳过时返回当时结果并标明「断点续跑」",
          "断点续跑" in text and "写入成功" in text, text)
    check("跳过动作进入可见事件流",
          any(ev == "step_done" and "已跳过重复执行" in str(data.get("output"))
              for ev, data in emitted), str(emitted))
    check("跳过计数可被审计", cp.skipped_duplicates == 1, str(cp.skipped_duplicates))

    async def _restore_case():
        agent = FakeAgent()
        info = await cp.restore_into(agent, FakeMsg)
        return agent, info

    agent, info = asyncio.run(_restore_case())
    check("恢复写入全部快照消息（末尾不是用户消息时不摘除）",
          info["restoredMessages"] == 4 and len(agent.memory.items) == 4, str(info))

    cp.messages.append({"name": "user", "role": "user", "content": "同一条用户消息",
                        "metadata": {}, "timestamp": None})
    agent2, info2 = asyncio.run(_restore_case())
    check("恢复时摘掉末尾用户消息，避免同一句话出现两次",
          info2["restoredMessages"] == 4 and len(agent2.memory.items) == 4
          and all("同一条用户消息" not in _blob([m.content]) for m in agent2.memory.items), str(info2))

    cp.mark_final(False, "任务已正常完成，无需续跑")
    check("正常收尾后快照被清除且不可续跑",
          cp.messages == [] and cp.saved_at is None and cp.unavailable_reason() != "",
          cp.unavailable_reason())

    empty = ck.TaskCheckpoint("empty")
    empty.begin_run()
    empty.mark_final(True, "上次执行在模型调用前中断，可从检查点继续")
    check("没有快照时即便请求保留也如实降级为不可续跑",
          not empty.resumable and "无法续跑" in empty.reason, empty.reason)


# ── 2. 端到端：中断 → 续跑 → 去重 ─────────────────────────────

SCENARIO = {"phase": "run1", "blobs": []}


class ScriptedModel(ChatModelBase):
    """第一轮：写文件 → 第二次模型调用中断。
    续跑轮：重发同签名写盘（验证被跳过）→ 看到跳过说明后收尾。"""

    def __init__(self):
        super().__init__(model_name="fake-checkpoint-model", stream=False)

    async def __call__(self, messages, *args, **kwargs):
        blob = _blob(messages)
        SCENARIO["blobs"].append(blob)
        if SCENARIO["phase"] == "run1":
            # 格式化后的消息在工具执行后才出现第 3 条（工具结果）
            if len(messages) > 2:
                raise RuntimeError("模拟模型在工具执行后中断")
            return ChatResponse(content=[{
                "type": "tool_use",
                "id": "call-write-1",
                "name": "write_text_file",
                "input": {"file_path": ARTIFACT, "content": ARTIFACT_MARK},
            }])
        if "已跳过重复执行" not in blob:
            # 故意重发与中断前完全相同的调用：幂等门禁必须拦住
            return ChatResponse(content=[{
                "type": "tool_use",
                "id": "call-write-2",
                "name": "write_text_file",
                "input": {"file_path": ARTIFACT, "content": ARTIFACT_MARK},
            }])
        return ChatResponse(content=[{
            "type": "text",
            "text": "已核对中断前的文件，复用结果并完成任务。",
        }])


def e2e_checks() -> None:
    print("--- 2. /api/chat 中断 → /api/chat/review resume ---")
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ScriptedModel(), DashScopeChatFormatter())
    )
    work_dir = Path(tempfile.mkdtemp(prefix="tt-checkpoint-http-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    task_id = "checkpoint-e2e"
    try:
        SCENARIO["phase"] = "run1"
        SCENARIO["blobs"].clear()
        resp = client.post("/api/chat", json={
            "message": "把标记写入 artifact.txt",
            "task_id": task_id,
            "privacyMode": True,
        })
        body = resp.text
        events = parse_sse(body)
        errors = events_of(events, "error")
        finals = events_of(events, "checkpoint")
        check("中断如实上报 AGENT_ERROR",
              bool(errors) and errors[0].get("code") == "AGENT_ERROR"
              and "模拟模型在工具执行后中断" in str(errors[0].get("message")), str(errors))
        check("收尾事件顺序 checkpoint 在 done 之前",
              [e.get("event") for e in events][-2:] == ["checkpoint", "done"],
              str([e.get("event") for e in events]))
        check("检查点事件为可续跑且带可读原因",
              bool(finals) and finals[-1].get("resumable") is True
              and "执行中断" in str(finals[-1].get("reason")), str(finals))
        check("隐私模式下检查点标明仅当前引擎进程内有效",
              finals and finals[-1].get("scope") == "same-process"
              and bool(finals[-1].get("savedAt")), str(finals[-1]) if finals else "")
        check("快照含工具执行后的记忆与副作用台账",
              finals and int(finals[-1].get("snapshotMessages") or 0) >= 2
              and int(finals[-1].get("sideEffects") or 0) == 1,
              str(finals[-1]) if finals else "")
        check("中断前写入的文件真实落盘",
              (work_dir / ARTIFACT).exists()
              and (work_dir / ARTIFACT).read_text(encoding="utf-8") == ARTIFACT_MARK,
              str(list(work_dir.iterdir())))

        # 模拟「中断后有人/别的进程改过文件」：续跑若重放写盘会把内容覆盖回去
        (work_dir / ARTIFACT).write_text("MANUAL-EDIT", encoding="utf-8")

        SCENARIO["phase"] = "run2"
        SCENARIO["blobs"].clear()
        resp = client.post("/api/chat/review", json={"task_id": task_id, "action": "resume"})
        body = resp.text
        events = parse_sse(body)
        finals = events_of(events, "checkpoint")
        errors = events_of(events, "error")
        check("续跑请求 HTTP 200 且正常收尾",
              resp.status_code == 200 and not errors and events_of(events, "done"),
              f"status={resp.status_code}, errors={errors}")
        check("续跑恢复快照并摊出恢复明细",
              "checkpoint-restore" in body and "从检查点恢复" in body, body[:400])
        check("续跑把「断点续跑」要求传给模型",
              bool(SCENARIO["blobs"]) and "[断点续跑]" in SCENARIO["blobs"][0],
              SCENARIO["blobs"][0][:300] if SCENARIO["blobs"] else "")
        check("续跑模型看得到中断前已写入的文件调用",
              bool(SCENARIO["blobs"]) and ARTIFACT in SCENARIO["blobs"][0],
              SCENARIO["blobs"][0][:300] if SCENARIO["blobs"] else "")
        check("续跑中重发的同签名写盘被跳过并留下可见说明",
              "已跳过重复执行" in body, body[-800:])
        check("被跳过的是真实工具调用（文件未被覆盖）",
              (work_dir / ARTIFACT).read_text(encoding="utf-8") == "MANUAL-EDIT",
              (work_dir / ARTIFACT).read_text(encoding="utf-8"))
        check("续跑成功后检查点被清除",
              bool(finals) and finals[-1].get("resumable") is False
              and "正常完成" in str(finals[-1].get("reason")), str(finals))
        check("跳过次数与副作用次数可审计",
              finals and int(finals[-1].get("skippedDuplicates") or 0) == 1
              and int(finals[-1].get("sideEffects") or 0) == 1
              and int(finals[-1].get("restoredMessages") or 0) >= 3,
              str(finals[-1]) if finals else "")
        check("续跑把中断前已产出的文件仍算进交付物",
              any(ARTIFACT in str(p) for d in events_of(events, "deliver")
                  for p in (d.get("artifacts") or [])),
              str(events_of(events, "deliver")))
        check("续跑只多调用两次模型（重发写盘 + 收尾）",
              len(SCENARIO["blobs"]) == 2, str(len(SCENARIO["blobs"])))

        resp = client.post("/api/chat/review", json={"task_id": task_id, "action": "resume"})
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        check("任务已成功收尾后再次恢复被 409 明确拒绝",
              resp.status_code == 409 and payload.get("code") == "CHECKPOINT_UNAVAILABLE"
              and payload.get("message"), f"{resp.status_code} {payload}")

        resp = client.post("/api/chat/review", json={"task_id": "task-after-engine-restart", "action": "resume"})
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        check("没有检查点时恢复给出明确原因",
              resp.status_code == 404 and payload.get("code") == "TASK_NOT_FOUND"
              and "引擎" in str(payload.get("message")), f"{resp.status_code} {payload}")

        # ── 首次模型调用前失败：没有快照，不展示可点但无用的入口 ──
        original_build_agent = engine_server.build_agent

        def _broken_build_agent(*a, **k):
            raise RuntimeError("模型工厂按测试要求失败")

        engine_server.build_agent = _broken_build_agent
        try:
            resp = client.post("/api/chat", json={
                "message": "这次在模型调用前就失败",
                "task_id": "checkpoint-no-snapshot",
                "privacyMode": True,
            })
            events = parse_sse(resp.text)
            finals = events_of(events, "checkpoint")
            check("构建失败时检查点如实标为不可续跑",
                  bool(finals) and finals[-1].get("resumable") is False
                  and "尚未产生可恢复的检查点" in str(finals[-1].get("reason")),
                  str(finals))
            resp = client.post("/api/chat/review",
                               json={"task_id": "checkpoint-no-snapshot", "action": "resume"})
            payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            check("没有快照时恢复返回 409 并说明中断发生在首次模型调用之前",
                  resp.status_code == 409 and "首次模型调用之前" in str(payload.get("message")),
                  f"{resp.status_code} {payload}")
        finally:
            engine_server.build_agent = original_build_agent
    finally:
        engine_server.WORK_DIR = Path(".")
        shutil.rmtree(work_dir, ignore_errors=True)


def disk_checks() -> None:
    print("--- 3. 磁盘检查点：跨进程恢复 / 查询 / 清理 ---")
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ScriptedModel(), DashScopeChatFormatter())
    )
    work_dir = Path(tempfile.mkdtemp(prefix="tt-checkpoint-disk-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    task_id = "checkpoint-disk-e2e"
    try:
        check("上一组隐私任务没有写磁盘检查点",
              not ck._checkpoint_file("checkpoint-e2e").exists())

        SCENARIO["phase"] = "run1"
        SCENARIO["blobs"].clear()
        resp = client.post("/api/chat", json={
            "message": "把标记写入 artifact.txt",
            "task_id": task_id,
        })
        events = parse_sse(resp.text)
        finals = events_of(events, "checkpoint")
        check("非隐私中断把检查点写盘并标明 scope=disk",
              bool(finals) and finals[-1].get("scope") == "disk"
              and finals[-1].get("resumable") is True,
              str(finals[-1]) if finals else "")
        check("磁盘检查点文件真实存在", ck._checkpoint_file(task_id).exists())
        check("写盘内容不含模型密钥与 Hook 网关令牌",
              "apiKey" not in ck._checkpoint_file(task_id).read_text(encoding="utf-8")
              and "gateUrl" not in ck._checkpoint_file(task_id).read_text(encoding="utf-8"))

        resp = client.post("/api/chat/checkpoint/query",
                           json={"task_ids": [task_id, "unknown-task"]})
        data = resp.json() if resp.status_code == 200 else {}
        info = (data.get("checkpoints") or {}).get(task_id) or {}
        check("查询接口返回磁盘检查点摘要",
              info.get("scope") == "disk" and info.get("resumable") is True
              and int(info.get("snapshotMessages") or 0) >= 2, str(info))
        check("查询接口不返回正文/上下文/密钥",
              not ({"messages", "context", "runtime", "toolRecords"} & set(info))
              and "apiKey" not in json.dumps(info, ensure_ascii=False), str(info))
        check("未知任务不伪造检查点",
              "unknown-task" not in (data.get("checkpoints") or {}))

        # 模拟引擎重启：进程内会话全部丢失，只剩磁盘文件
        engine_server._TASK_SESSIONS.clear()
        resp = client.post("/api/chat/checkpoint/query", json={"task_ids": [task_id]})
        info = ((resp.json().get("checkpoints") or {}).get(task_id) or {}) if resp.status_code == 200 else {}
        check("引擎重启后仍能查询到检查点入口",
              info.get("scope") == "disk" and info.get("resumable") is True, str(info))

        (work_dir / ARTIFACT).write_text("MANUAL-EDIT", encoding="utf-8")
        SCENARIO["phase"] = "run2"
        SCENARIO["blobs"].clear()
        resp = client.post("/api/chat/review", json={"task_id": task_id, "action": "resume"})
        body = resp.text
        events = parse_sse(body)
        finals = events_of(events, "checkpoint")
        errors = events_of(events, "error")
        check("引擎重启后按磁盘检查点续跑成功",
              resp.status_code == 200 and not errors and events_of(events, "done"),
              f"status={resp.status_code}, errors={errors}")
        check("跨进程续跑从磁盘恢复记忆并说明来源",
              "checkpoint-restore" in body and "从检查点恢复" in body, body[:400])
        check("跨进程续跑跳过中断前已成功的同签名写盘（文件未被覆盖）",
              "已跳过重复执行" in body
              and (work_dir / ARTIFACT).read_text(encoding="utf-8") == "MANUAL-EDIT",
              (work_dir / ARTIFACT).read_text(encoding="utf-8"))
        check("跨进程续跑成功后磁盘检查点被删除",
              bool(finals) and finals[-1].get("resumable") is False
              and not ck._checkpoint_file(task_id).exists(), str(finals[-1]) if finals else "")
        resp = client.post("/api/chat/checkpoint/query", json={"task_ids": [task_id]})
        after = ((resp.json().get("checkpoints") or {}).get(task_id) or {}) if resp.status_code == 200 else {}
        check("任务收尾后查询不再返回可恢复入口",
              after.get("resumable") is False
              and "正常完成" in str(after.get("reason")), str(after))

        # ── 恢复失败必须是可读原因，不能悄悄换模型/换目录 ──
        bad_model = "checkpoint-model-missing"
        ck.save_checkpoint(bad_model, {
            "messages": [{"name": "user", "role": "user", "content": "目标",
                          "metadata": {}, "timestamp": None}],
            "resumable": True,
            "reason": "测试用检查点",
            "stats": {},
            "context": {"goal": "目标", "workDir": str(work_dir),
                        "modelId": "missing-model-id"},
        })
        resp = client.post("/api/chat/review", json={"task_id": bad_model, "action": "resume"})
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        check("模型配置无法重新解析时 409 拒绝并说明原因",
              resp.status_code == 409 and payload.get("code") == "CHECKPOINT_MODEL_UNAVAILABLE"
              and "模型" in str(payload.get("message")), f"{resp.status_code} {payload}")
        ck.delete_checkpoint(bad_model)

        missing_dir = Path(tempfile.mkdtemp(prefix="tt-checkpoint-gone-"))
        shutil.rmtree(missing_dir, ignore_errors=True)
        bad_dir = "checkpoint-workdir-missing"
        ck.save_checkpoint(bad_dir, {
            "messages": [{"name": "user", "role": "user", "content": "目标",
                          "metadata": {}, "timestamp": None}],
            "resumable": True,
            "reason": "测试用检查点",
            "stats": {},
            "context": {"goal": "目标", "workDir": str(missing_dir)},
        })
        resp = client.post("/api/chat/review", json={"task_id": bad_dir, "action": "resume"})
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        check("工作目录已不存在时 409 拒绝（不写到错误位置）",
              resp.status_code == 409 and payload.get("code") == "CHECKPOINT_WORKDIR_MISSING",
              f"{resp.status_code} {payload}")
        ck.delete_checkpoint(bad_dir)

        # ── 写盘失败：降级为进程内并如实说明，不能承诺刷新后还能继续 ──
        original_save = ck.save_checkpoint
        ck.save_checkpoint = lambda *a, **k: (False, "磁盘已满（测试）")
        try:
            SCENARIO["phase"] = "run1"
            SCENARIO["blobs"].clear()
            resp = client.post("/api/chat", json={
                "message": "写盘失败也要如实收尾",
                "task_id": "checkpoint-disk-fail",
            })
            finals = events_of(parse_sse(resp.text), "checkpoint")
            check("写盘失败时降级为进程内并暴露 diskError",
                  bool(finals) and finals[-1].get("scope") == "same-process"
                  and "磁盘已满（测试）" in str(finals[-1].get("diskError"))
                  and "仅当前引擎进程内有效" in str(finals[-1].get("reason")),
                  str(finals[-1]) if finals else "")
            check("写盘失败不会留下半成品检查点文件",
                  not ck._checkpoint_file("checkpoint-disk-fail").exists())
        finally:
            ck.save_checkpoint = original_save
    finally:
        engine_server.WORK_DIR = Path(".")
        engine_server._TASK_SESSIONS.clear()
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    prev_home = os.environ.get("TONGTIANWORK_HOME")
    temp_home = Path(tempfile.mkdtemp(prefix="tt-checkpoint-home-"))
    os.environ["TONGTIANWORK_HOME"] = str(temp_home)
    try:
        unit_checks()
        if engine_server is not None and ChatModelBase is not None:
            e2e_checks()
            disk_checks()
        else:
            print("SKIP: 端到端分组未运行（依赖缺失）")
    finally:
        if prev_home is None:
            os.environ.pop("TONGTIANWORK_HOME", None)
        else:
            os.environ["TONGTIANWORK_HOME"] = prev_home
        shutil.rmtree(temp_home, ignore_errors=True)
    print("-" * 52)
    if FAILURES:
        print(f"engine checkpoint selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine checkpoint selftest: ALL PASS")


if __name__ == "__main__":
    main()
