#!/usr/bin/env python3
"""文件访问档位自测：对话输入框选的三档 → 引擎里的真实边界

锁住「对话框里选了档位但没人执行」这条假承诺。口径（与渲染层档位说明一一对应）：

1. 每次询问 / 自动审批：写盘限定在绑定工作目录 ∪ 工作区文件白名单，越界即拒；
   每次询问额外在目录内写盘前弹一次确认（网关可用时），拒绝或连不上都不执行；
2. 完全访问：可越出绑定目录，但家目录/系统关键目录/引擎数据目录仍在硬拒清单内；
3. 删除文件：任何档位下都需要完全访问，且删除前必须先在引擎回收目录留下副本；
4. 档位来自顶层 runtime.filePermission，与 sandbox 段无关：壳没下发沙箱配置时照样拦；
5. 工作区沙箱 L2（目录不限制）比档位宽时，取更严的档位，不静默放开；
6. /api/chat 拒绝未知档位值（FILE_PERMISSION_INVALID），档位说明进可见事件流。

离线运行：假模型 + 真实 ReActAgent / Toolkit，无网络、无需 API Key。
引擎数据目录（含回收目录）整体重定向到临时目录，不碰用户真实的 ~/.tongtianwork。
依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_file_permission_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    import sandbox_runtime as sr
    from fastapi.testclient import TestClient
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine file permission selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
RECORDED = []
LAST_ASK: dict = {}

GATE = {"mode": "off", "gateUrl": "http://127.0.0.1:54321/tool/approve", "gateToken": "t-1"}


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class RecordingModel(ChatModelBase):
    def __init__(self):
        super().__init__(model_name="fake-perm-model", stream=False)

    async def __call__(self, messages, *args, **kwargs):
        RECORDED.append(json.dumps(messages, ensure_ascii=False, default=str))
        return ChatResponse(content=[{"type": "text", "text": "已完成。"}], usage=None)


def patch_model():
    engine_server.build_model_and_formatter = lambda *a, **k: (
        RecordingModel(), DashScopeChatFormatter()
    )


def runtime_of(perm: str = "", sandbox: dict | None = None, approval: dict | None = None) -> dict:
    bundle: dict = {"skills": []}
    if perm:
        bundle["filePermission"] = perm
    if sandbox is not None:
        bundle["sandbox"] = sandbox
    if approval is not None:
        bundle["toolApproval"] = approval
    return bundle


def rule_of(denial) -> str:
    return "" if denial is None else str(denial[2])


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def far_path(tmp: Path, name: str = "x.txt") -> Path:
    """盘根下的路标路径：既不在绑定目录内，也不在家目录/系统关键目录里。

    纯判定用（不需要真存在）。临时目录在 Windows 上就在家目录里，拿它当「外部目录」
    会先命中硬拒清单，测不到「档位越界」这条分支。
    """
    root = Path(str(tmp.drive) + "/") if tmp.drive else Path("/tmp")
    return root / "tt-perm-far" / name


def pure_checks(tmp: Path) -> None:
    print("--- 1. 档位解析与边界判定（纯函数层） ---")
    check("缺省即最保守档", sr.normalize_file_permission("") == "ask_every_time")
    check("非法档位回最保守档", sr.normalize_file_permission("yolo") == "ask_every_time")

    plain = sr.SandboxPolicy.from_runtime(None, work_dir=tmp)
    check("没有 runtime 也带档位", plain.file_permission == "ask_every_time")
    check("未下发沙箱段仍拦越界写", rule_of(
        plain.file_denial(far_path(tmp), write=True)
    ) == "outside_boundary")
    check("目录内写放行", plain.file_denial(tmp / "a.txt", write=True) is None)
    check("读不受档位限制", plain.file_denial(far_path(tmp, "read.txt")) is None)

    auto = sr.SandboxPolicy.from_runtime(runtime_of("auto_approve"), work_dir=tmp)
    check("档位读进策略", auto.file_permission == "auto_approve")
    outside = far_path(tmp)
    check("自动审批越界写被拒", rule_of(auto.file_denial(outside, write=True)) == "outside_boundary")
    check("自动审批不能删文件", rule_of(
        auto.file_denial(tmp / "a.txt", write=True, delete=True)
    ) == "delete_requires_full_access")

    full = sr.SandboxPolicy.from_runtime(runtime_of("full_access"), work_dir=tmp)
    check("完全访问可越界写", full.file_denial(outside, write=True) is None)
    check("完全访问越界仍可删", full.file_denial(outside, write=True, delete=True) is None)
    check("完全访问仍要硬拒家目录", rule_of(
        full.file_denial(Path.home() / "不许往这里写.txt", write=True)
    ) == "hard_deny")
    check("硬拒命中家目录", sr.hard_deny_hit(Path.home() / "x.txt", tmp) != "")
    check("硬拒命中系统目录", sr.hard_deny_hit("C:/Windows/System32/x.txt", tmp) != "")
    # Windows 的 resolve() 可能给出 `\\?\` 命名空间前缀；不剥掉就变成换个写法躲过硬拒
    check("命名空间前缀剥成普通路径",
          sr._strip_namespace_prefix("\\\\?\\C:\\Windows\\a") == "C:\\Windows\\a"
          and sr._strip_namespace_prefix("\\\\?\\UNC\\srv\\share") == "\\\\srv\\share"
          and sr._strip_namespace_prefix("\\\\.\\C:\\Windows\\a") == "C:\\Windows\\a")
    check("带前缀的系统目录仍命中硬拒",
          sr.hard_deny_hit("\\\\?\\C:\\Windows\\System32\\x.txt", tmp) != ""
          or os.name != "nt")
    check("硬拒不命中普通外部目录", sr.hard_deny_hit(outside, tmp) == "")
    check("完全访问可删目录内文件",
          full.file_denial(tmp / "a.txt", write=True, delete=True) is None)

    # 工作区文件白名单是用户在策略页里明确扩出去的目录，算「目录内」
    allowed = sr.SandboxPolicy.from_runtime(runtime_of(
        "auto_approve",
        sandbox={"workspaceId": "ws", "configured": True, "executionLevel": "L1",
                 "fileAllowlist": [str(tmp.parent)]},
    ), work_dir=tmp)
    check("工作区文件白名单算目录内", allowed.file_denial(tmp.parent / "ok.txt", write=True) is None)

    wide = sr.SandboxPolicy.from_runtime(runtime_of(
        "auto_approve",
        sandbox={"configured": True, "executionLevel": "L2"},
    ), work_dir=tmp)
    check("沙箱 L2 与档位冲突时取更严的档位",
          rule_of(wide.file_denial(outside, write=True)) == "outside_boundary")
    check("冲突在说明里可见", any("L2" in n and "档位更严" in n for n in wide.notes()), str(wide.notes()))
    check("档位说明摊进上下文", any("文件访问档位" in n for n in plain.notes()), str(plain.notes()))
    check("无网关时说明如实降级", any(
        "无法弹确认框" in n for n in plain.permission_notes()
    ), str(plain.permission_notes()))
    check("沙箱策略损坏时仍先走 fail-closed", (
        sr.SandboxPolicy.from_runtime({"sandbox": {"error": "POLICY_CORRUPT: x"}}, work_dir=tmp)
        .file_denial(tmp / "a.txt", write=True) or ("", "")
    )[1] == "policy_error")


def toolkit_checks(tmp: Path) -> None:
    print("--- 2. 内置文件工具的真实行为（含删除回收副本） ---")
    patch_model()
    ext = tmp.parent / (tmp.name + "-ext")
    ext.mkdir(exist_ok=True)
    # 系统临时目录在 Windows 上就在家目录里，真实硬拒清单会把「越界」一律钉死；
    # 这里换掉硬拒根，才能同时验到「完全访问真越界可写」和「命中硬拒仍被拒」。
    # 家目录/系统目录本身由第 1 组的纯函数检查用真实清单锁死，不是靠这里放行。
    protected = tmp.parent / (tmp.name + "-locked")
    protected.mkdir(exist_ok=True)
    real_roots = sr._deny_roots
    sr._deny_roots = lambda: [sr._norm_path(protected)]
    try:
        ask = sr.SandboxPolicy.from_runtime(runtime_of("ask_every_time"), work_dir=tmp)
        agent = engine_server.build_agent(None, tmp, None, runtime={}, sandbox=ask)
        tools = agent.toolkit.tools
        write = tools["write_text_file"].original_func
        delete = tools["delete_text_file"].original_func
        view = tools["view_text_file"].original_func

        inside = tool_text(write(str(tmp / "in.txt"), "hello"))
        check("目录内写盘正常", "已成功写入" in inside, inside[:160])
        escaped = tool_text(write(str(ext / "out.txt"), "hello"))
        check("越界写被档位拒绝", "文件访问权限拒绝" in escaped and "完全访问" in escaped, escaped[:200])
        check("拒绝时没有真的写出文件", not (ext / "out.txt").exists())

        guarded = tool_text(view(str(ext / "nothing.txt")))
        check("越界读按原有工作目录边界拒绝", "超出工作目录" in guarded, guarded[:160])

        removed = tool_text(delete(str(tmp / "in.txt")))
        check("非完全访问不能删", "完全访问" in removed and (tmp / "in.txt").exists(), removed[:200])
        check("删除工具已在内置清单里", "delete_text_file" in tools)

        full = sr.SandboxPolicy.from_runtime(runtime_of("full_access"), work_dir=tmp)
        full_agent = engine_server.build_agent(None, tmp, None, runtime={}, sandbox=full)
        full_write = full_agent.toolkit.tools["write_text_file"].original_func
        full_delete = full_agent.toolkit.tools["delete_text_file"].original_func
        crossed = tool_text(full_write(str(ext / "out.txt"), "crossed"))
        check("完全访问可越界写", "已成功写入" in crossed, crossed[:160])
        denied_root = tool_text(full_write(str(protected / "nope.txt"), "x"))
        check("完全访问命中硬拒仍被拒", "硬拒清单" in denied_root, denied_root[:200])
        gone = tool_text(full_delete(str(ext / "out.txt"), "自测清理"))
        check("完全访问删除成功", "已删除文件" in gone, gone[:200])
        check("原件确实消失", not (ext / "out.txt").exists())
        import data_paths

        trash = data_paths.file_trash_dir()
        copies = list(Path(trash).glob("*out.txt"))
        check("回收目录留了副本", bool(copies), str(trash))
        metas = list(Path(trash).glob("*out.txt.from.json"))
        check("副本带来源与原因说明", bool(metas) and "自测清理" in metas[0].read_text(encoding="utf-8"),
              str(metas))
        again = tool_text(full_delete(str(ext / "out.txt")))
        check("重复删除报文件不存在而不是崩", "文件不存在" in again, again[:160])
        dir_delete = tool_text(full_delete(str(ext)))
        check("不支持删目录", "不支持目录" in dir_delete, dir_delete[:160])
    finally:
        sr._deny_roots = real_roots


async def _run_middleware(mw, kwargs, calls):
    async def next_handler(**kw):
        calls.append(kw.get("tool_call"))

        async def agen():
            yield ToolResponse(content=[TextBlock(type="text", text="handler-ran")])

        return agen()

    out = []
    async for resp in mw(kwargs, next_handler):
        out.append(resp)
    return out


def middleware_checks(tmp: Path) -> None:
    print("--- 3. 中间件：无条件装门禁 + 每次询问真的问用户 ---")
    events = []

    def emit(ev, data):
        events.append((ev, data))

    calls: list = []

    ask = sr.SandboxPolicy.from_runtime(runtime_of("ask_every_time", approval=GATE), work_dir=tmp)
    check("网关在位时判定可询问", ask.ask_available() and ask.should_ask("write_text_file"))
    check("只读工具不问", not ask.should_ask("view_text_file"))
    check("完全访问不需要询问", not sr.SandboxPolicy.from_runtime(
        runtime_of("full_access", approval=GATE), work_dir=tmp
    ).should_ask("write_text_file"))
    wide = sr.SandboxPolicy.from_runtime(runtime_of(
        "ask_every_time",
        approval={"mode": "all", "gateUrl": GATE["gateUrl"], "gateToken": "t"},
    ), work_dir=tmp)
    check("逐工具审批已弹窗时不重复询问", not wide.should_ask("write_text_file"))

    # 真网关连不上：ask_user 自身返回不放行（fail-closed，不是超时后默认执行）
    down = sr.SandboxPolicy.from_runtime(runtime_of(
        "ask_every_time",
        approval={"mode": "off", "gateUrl": "http://127.0.0.1:1/tool/approve", "gateToken": "t"},
    ), work_dir=tmp)
    allowed, why = asyncio.run(sr.ask_user(down, "write_text_file", "写入 x.txt", "task"))
    check("网关连不上时不放行", allowed is False and bool(why), f"{allowed} {why}")

    mw = sr.tool_middleware(ask, emit, "task-perm")
    answers = iter([(True, ""), (False, "用户在确认框里点了拒绝")])

    async def fake_ask(policy, tool, summary, task_id):
        LAST_ASK["summary"] = summary
        LAST_ASK["tool"] = tool
        return next(answers)

    real_ask = sr.ask_user
    sr.ask_user = fake_ask
    try:
        out = asyncio.run(_run_middleware(
            mw, {"tool_call": {"name": "write_text_file", "input": {"file_path": str(tmp / "a.txt")}}},
            calls,
        ))
        check("用户同意后照常执行", len(calls) == 1 and "handler-ran" in tool_text(out[0]), str(calls))
        check("确认摘要带上路径", "a.txt" in LAST_ASK.get("summary", ""), str(LAST_ASK))

        calls.clear()
        events.clear()
        out = asyncio.run(_run_middleware(
            mw, {"tool_call": {"name": "write_text_file", "input": {"file_path": str(tmp / "b.txt")}}},
            calls,
        ))
        check("用户拒绝则不执行", not calls, str(calls))
        text = tool_text(out[0])
        check("拒绝文案走文件权限口径", "文件访问权限档位" in text and "点了拒绝" in text, text[:200])
        check("拒绝发 sandbox_denied 事件带档位", any(
            e == "sandbox_denied" and d.get("source") == "file_permission_ask"
            and d.get("filePermission") == "ask_every_time"
            for e, d in events
        ), str(events))

        # 越界写在问用户之前就被拒：不给「先弹窗再发现越界」的机会
        calls.clear()
        events.clear()
        far = str(far_path(tmp, "c.txt"))
        out = asyncio.run(_run_middleware(
            mw, {"tool_call": {"name": "write_text_file", "input": {"file_path": far}}},
            calls,
        ))
        check("越界写不问用户直接拒", not calls and "outside_boundary" == rule_of(
            ask.file_denial(far, write=True)
        ), f"{calls} {tool_text(out[0])[:160]}")

        # 删除：非完全访问在中间件层就被拒，且带着 delete 语义
        calls.clear()
        out = asyncio.run(_run_middleware(
            mw, {"tool_call": {"name": "delete_text_file", "input": {"file_path": str(tmp / "a.txt")}}},
            calls,
        ))
        check("中间件拦下非完全访问的删除", not calls and "完全访问" in tool_text(out[0]), tool_text(out[0])[:160])

        # 未配置沙箱段也要装门禁：档位判定没有第二条执行路径
        quiet = sr.SandboxPolicy.from_runtime(None, work_dir=tmp)
        holder = {}

        class FakeToolkit:
            def register_middleware(self, mw2):
                holder["mw"] = mw2

        check("未配置沙箱也注册门禁", quiet.install(FakeToolkit(), emit, "t") is None and "mw" in holder)
    finally:
        sr.ask_user = real_ask


def http_checks(tmp: Path) -> None:
    print("--- 4. /api/chat：档位校验与说明可见 ---")
    patch_model()
    engine_server.WORK_DIR = tmp
    client = TestClient(engine_server.app)
    try:
        bad = client.post("/api/chat", json={
            "message": "写个文件", "task_id": "perm-bad", "filePermission": "whatever",
        })
        check("未知档位直接拒绝", bad.status_code == 400, str(bad.status_code))
        check("拒绝带错误码", bad.json().get("code") == "FILE_PERMISSION_INVALID", bad.text[:200])

        ok = client.post("/api/chat", json={
            "message": "写个文件", "task_id": "perm-full", "filePermission": "full_access",
        })
        check("合法档位照常跑完", "event: done" in ok.text, ok.text[:200])
        check("完全访问约束进可见说明", "完全访问" in ok.text and "硬拒清单" in ok.text, ok.text[:400])

        asky = client.post("/api/chat", json={
            "message": "写个文件", "task_id": "perm-ask", "filePermission": "ask_every_time",
        })
        check("没有网关时如实说明降级", "无法弹确认框" in asky.text, asky.text[:400])

        via_runtime = client.post("/api/chat", json={
            "message": "写个文件", "task_id": "perm-rt", "runtime": runtime_of("auto_approve"),
        })
        check("桌面端经 runtime 下发档位同样生效",
              "自动审批" in via_runtime.text, via_runtime.text[:400])
    finally:
        engine_server.WORK_DIR = Path(".")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tt-perm-"))
    # 引擎数据根（含删除回收目录）整体重定向，自测不碰用户真实的 ~/.tongtianwork
    os.environ["TONGTIANWORK_HOME"] = str(tmp / "engine-home")
    pure_checks(tmp)
    toolkit_checks(tmp)
    middleware_checks(tmp)
    http_checks(tmp)
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine file permission selftest: ALL PASS")


if __name__ == "__main__":
    main()
