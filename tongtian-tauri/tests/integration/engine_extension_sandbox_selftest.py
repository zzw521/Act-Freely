#!/usr/bin/env python3
"""D-08 扩展族自测：受控执行进容器 + 「这个扩展声明了什么能力」清单（W4 领地）。

验证项：
1. 未启用系统隔离时，`container_wrap` / `container_wrap_shell` 与改造前逐字一致
   （shell 那一头保住 D-48.4c 的**字符串命令行**形状，接线不许把它改回列表）
2. 启用系统隔离时 argv 真的带上启动器与 `--network on|off`，出网档位与容器读数同源
3. shell 走 `--raw-command-line`：整条命令行原样送到启动器，程序与参数仍在 `--` 之后
4. 跨语言同源：Python 写的这个标记与 Rust 启动器解析的那个标记是同一个字面量
5. 启动器不在盘上 ⇒ 抛 SandboxUnavailable；执行面**不注册**（不是注册后拒绝）
6. `registration_plan()` 是注册与界面的唯一判定；`notes()` 与它不吵架
7. 真起一个假启动器进程，证明套壳确实生效、回执里点名容器与出网档位
8. 声明清单：技能/套件/MCP/Hooks/ACP 各按声明摊开，正文与密钥值不外泄，
   未放行/被扫描拦下的条目按不可用列出（「未声明即不可用」可逐条核对）
9. `/api/exec/extensions` 挂得上、读得到，且不 import server（车道铁律）

判定只看 [PASS] 计数。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_extension_sandbox_selftest.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import extension_decls as exd  # noqa: E402
import routes_lanes as rln  # noqa: E402
import sandbox_launcher as sl  # noqa: E402
import tool_grants as tg  # noqa: E402

SANDBOX_KEYS = ("TT_SANDBOX_BIN", "TT_SANDBOX_NETWORK", "TT_SANDBOX_RUNTIME_DIRS")
ORIGINAL = {k: os.environ.get(k) for k in SANDBOX_KEYS}


def set_env(launcher: str = "", network: str = "") -> None:
    """只在测试里改环境，且每次调用都把三个变量摊平（避免上一段的状态串进下一段）"""
    for key in SANDBOX_KEYS:
        os.environ.pop(key, None)
    if launcher:
        os.environ["TT_SANDBOX_BIN"] = launcher
    if network:
        os.environ["TT_SANDBOX_NETWORK"] = network


def restore_env() -> None:
    for key, value in ORIGINAL.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class FakeToolkit:
    def __init__(self):
        self.tools = {}

    def register_tool_function(self, fn):
        self.tools[fn.__name__] = fn


def policy(**kwargs) -> tg.ToolGrantPolicy:
    block = {
        "execEnabled": kwargs.get("exec", False),
        "execAllowlist": kwargs.get("allow", []),
        "shellEnabled": kwargs.get("shell", False),
        "networkEnabled": kwargs.get("net", False),
        "networkAllowlist": kwargs.get("hosts", []),
    }
    return tg.ToolGrantPolicy.from_runtime({"sandbox": block})


def tool_text(resp) -> str:
    return "\n".join(
        str(getattr(block, "text", "") or (block.get("text") if isinstance(block, dict) else ""))
        for block in (resp.content or [])
    )


def main() -> int:
    print("--- 1. 未启用系统隔离：与改造前逐字一致 ---")
    set_env()
    check("§1 未注入变量时 enabled() 为假", sl.enabled() is False)
    state = tg.container_state()
    check("§1 容器读数如实说未启用", state["enabled"] is False and state["ready"] is False, str(state))
    check("§1 未启用时 reason 点出只有资源限制", "资源限制" in state["reason"], state["reason"])
    argv, note = tg.container_wrap("python.exe", ["-c", "print(1)"], "D:/ws")
    check("§1 run_command 的 argv 未被改动", argv == ["python.exe", "-c", "print(1)"], str(argv))
    check("§1 未启用时容器说明为空（回执与改造前一致）", note == "", repr(note))
    plain = 'cmd.exe /d /s /c "echo "hello world""'
    kept, note2 = tg.container_wrap_shell(plain, "cmd.exe", ["/d", "/s", "/c", '"echo "hello world""'], "D:/ws")
    check("§1 shell 未启用时交出的仍是**字符串**（D-48.4c 的形状）", kept is plain or kept == plain, repr(kept)[:80])
    check("§1 shell 未启用时同样不带容器说明", note2 == "", repr(note2))
    check("§1 未启用系统隔离时出网档位按关闭读", tg.sl_network() == "off", tg.sl_network())

    print("--- 2. 启用系统隔离：argv 带上启动器与出网档位 ---")
    fake = Path(tempfile.mkdtemp(prefix="tt-d08-")) / "fake-launcher.bat"
    fake.write_text("@echo off\r\necho TT-FAKE-LAUNCHER %*\r\n", encoding="ascii")
    set_env(str(fake), "off")
    state = tg.container_state()
    check("§2 启用且启动器在盘上 ⇒ ready", state["enabled"] and state["ready"], str(state))
    check("§2 容器读数带上启动器路径", state["launcher"] == str(fake), state["launcher"])
    check("§2 出网档位按 off 读", state["network"] == "off", str(state))
    wargv, wnote = tg.container_wrap("python.exe", ["-c", "print(1)"], "D:/ws")
    check("§2 首位是启动器", wargv[0] == str(fake), str(wargv[:2]))
    check("§2 带隐藏子命令标记", wargv[1] == sl.CLI_FLAG, str(wargv[1:3]))
    check("§2 profile 固定", "--profile" in wargv and sl.PROFILE in wargv, str(wargv))
    check("§2 出网档位随容器读数下发", ["--network", "off"] == wargv[wargv.index("--network"):wargv.index("--network") + 2], str(wargv))
    check("§2 原命令仍在 `--` 之后且顺序不变", wargv[wargv.index("--") + 1:] == ["python.exe", "-c", "print(1)"], str(wargv))
    check("§2 工作目录作为写权限授权下发", "--workspace" in wargv and "D:/ws" in wargv, str(wargv))
    check("§2 容器说明点名 AppContainer 与出网关闭", "AppContainer" in wnote and "出网关闭" in wnote, wnote)
    set_env(str(fake), "on")
    _, on_note = tg.container_wrap("python.exe", ["-c", "print(1)"], "D:/ws")
    check("§2 出网开启时说明跟着改口（与档位同源，不写死）", "出网开启" in on_note and "出网关闭" not in on_note, on_note)

    print("--- 3. shell 走 --raw-command-line：命令行原样送达 ---")
    set_env(str(fake), "off")
    shell = os.environ.get("COMSPEC") or "cmd.exe"
    tail = '"echo "hello world""'
    plain = f"{shell} /d /s /c {tail}"
    got, snote = tg.container_wrap_shell(plain, shell, ["/d", "/s", "/c", tail], "D:/ws", raw_command_line=True)
    check("§3 启用后交出的是启动器的真 argv 列表", isinstance(got, list) and got[0] == str(fake), str(got)[:60])
    check("§3 命令行标记出现在 `--` 之前", "--raw-command-line" in got and got.index("--raw-command-line") < got.index("--"), str(got))
    check("§3 `--` 之后的程序与参数逐字未变（没有插进一层转义）",
          got[got.index("--") + 1:] == [shell, "/d", "/s", "/c", tail], str(got[got.index("--") + 1:]))
    check("§3 整条 tail 仍是**一个**参数（cmd 自己解引号）", tail in got, str(got))
    check("§3 tail 这一项原样送达，没有被我们提前加引号或转义",
          got[-1] == tail and '\\"' not in got[-1], repr(got[-1]))
    check("§3 shell 的容器说明与 run_command 同一说法", "AppContainer" in snote and "出网关闭" in snote, snote)

    print("--- 4. 跨语言同源：Python 写的标记 = Rust 解析的标记 ---")
    cli_rs = (PROJECT_ROOT / "src-tauri" / "src" / "sandbox" / "cli.rs").read_text(encoding="utf-8")
    win_rs = (PROJECT_ROOT / "src-tauri" / "src" / "sandbox" / "win.rs").read_text(encoding="utf-8")
    launcher_py = (PROJECT_ROOT / "engine" / "sandbox_launcher.py").read_text(encoding="utf-8")
    check("§4 Rust 启动器解析这一档", '"--raw-command-line" =>' in cli_rs, "cli.rs 里没有该分支")
    check("§4 Rust 把这一档存进 RunOptions", "raw_command_line" in cli_rs, "RunOptions 缺字段")
    check("§4 Rust 的 raw 分支跳过逐参数加引号", "if raw {" in win_rs, "win.rs 的 resolve_program 没有 raw 分支")
    check("§4 Python 下发的字面量与 Rust 完全一致",
          launcher_py.count('"--raw-command-line"') >= 1 and cli_rs.count('"--raw-command-line"') >= 1,
          "两侧字面量对不上")
    check("§4 默认不带这一档时其余调用方不变（Rust 缺省 false）",
          "let mut raw_command_line = false;" in cli_rs, "缺省不是 false")

    print("--- 5. 启动器不在盘上：抛错，且不静默降级 ---")
    missing = str(Path(tempfile.mkdtemp(prefix="tt-d08-none-")) / "no-such-launcher.exe")
    set_env(missing, "off")
    bad = tg.container_state()
    check("§5 enabled 为真但 ready 为假", bad["enabled"] and not bad["ready"], str(bad))
    check("§5 reason 点名启动器不在盘上", "不在盘上" in bad["reason"], bad["reason"])
    try:
        tg.container_wrap("python.exe", ["-c", "print(1)"], "D:/ws")
        check("§5 容器不可用时 container_wrap 必须抛", False, "没抛")
    except sl.SandboxUnavailable as exc:
        check("§5 容器不可用时 container_wrap 抛 SandboxUnavailable", "SANDBOX_LAUNCHER_MISSING" in str(exc), str(exc))
    try:
        tg.container_wrap_shell(plain, shell, ["/d", "/s", "/c", tail], "D:/ws", raw_command_line=True)
        check("§5 shell 同样抛", False, "没抛")
    except sl.SandboxUnavailable as exc:
        check("§5 shell 容器不可用时同样抛（不许回落成裸跑）", "SANDBOX_LAUNCHER_MISSING" in str(exc), str(exc))
    set_env("", "on")
    real_enabled = sl.enabled
    sl.enabled = lambda: True  # 模拟 enabled() 与 wrap() 之间变量被抽走的那一瞬
    try:
        try:
            tg.container_wrap("python.exe", ["-c", "print(1)"], "D:/ws")
            check("§5 变量被抽走时按不可用处理，不放行", False, "没抛")
        except sl.SandboxUnavailable:
            check("§5 变量被抽走时按不可用处理，不放行", True)
        try:
            tg.container_wrap_shell(plain, shell, ["/d", "/s", "/c", tail], "D:/ws", raw_command_line=True)
            check("§5 shell 在同一条竞态上也不放行", False, "没抛")
        except sl.SandboxUnavailable:
            check("§5 shell 在同一条竞态上也不放行", True)
    finally:
        sl.enabled = real_enabled

    print("--- 6. 未授权/容器不可用：一律不注册（不是注册后拒绝） ---")
    p = policy(exec=True, allow=["python"], shell=True)
    set_env()
    plan = p.registration_plan()
    check("§6 未启用隔离时两个执行工具都注册", plan["run_command"]["registered"] and plan["run_shell"]["registered"], str(plan))
    tk = FakeToolkit()
    p.install(tk, Path(tempfile.mkdtemp(prefix="tt-d08-ws-")))
    check("§6 install 与 plan 结果一致", set(tk.tools) == {"run_command", "run_shell"}, str(sorted(tk.tools)))
    set_env(missing, "off")
    plan = p.registration_plan()
    check("§6 容器不可用时 run_command 不注册", plan["run_command"]["registered"] is False, str(plan["run_command"]))
    check("§6 容器不可用时 run_shell 不注册", plan["run_shell"]["registered"] is False, str(plan["run_shell"]))
    check("§6 不注册的原因就是容器那条读数", plan["run_command"]["reason"] == bad["reason"] or "不在盘上" in plan["run_command"]["reason"],
          plan["run_command"]["reason"])
    tk = FakeToolkit()
    p.install(tk, Path(tempfile.mkdtemp(prefix="tt-d08-ws-")))
    check("§6 容器不可用时 toolkit 里一个执行工具都没有", tk.tools == {}, str(sorted(tk.tools)))
    pn = policy(net=True, hosts=["example.com"])
    pl = pn.registration_plan()
    check("§6 联网工具不受容器影响（进程内 urllib，不产生子进程）",
          pl["fetch_url"]["registered"] is True, str(pl["fetch_url"]))
    tk = FakeToolkit()
    pn.install(tk, Path(tempfile.mkdtemp(prefix="tt-d08-ws-")))
    check("§6 容器不可用时 fetch_url 仍然注册", set(tk.tools) == {"fetch_url"}, str(sorted(tk.tools)))
    set_env(str(fake), "off")
    plan = p.registration_plan()
    check("§6 容器可用时执行面恢复注册", plan["run_command"]["registered"] and plan["run_shell"]["registered"], str(plan))
    check("§6 未授权命令执行时不注册并写明原因",
          policy().registration_plan()["run_command"] == {"registered": False, "reason": "用户未授权受控命令执行"},
          str(policy().registration_plan()["run_command"]))
    check("§6 白名单为空时不注册并写明原因",
          policy(exec=True, shell=True).registration_plan()["run_command"]["reason"] == "命令白名单为空",
          str(policy(exec=True, shell=True).registration_plan()["run_command"]))
    check("§6 只开 shell 不开命令执行时 run_shell 不注册",
          policy(shell=True).registration_plan()["run_shell"]["registered"] is False,
          str(policy(shell=True).registration_plan()["run_shell"]))
    broken = tg.ToolGrantPolicy(error="沙箱配置不是合法 JSON")
    bp = broken.registration_plan()
    check("§6 策略读取失败时三个都不注册",
          not any(bp[k]["registered"] for k in ("run_command", "run_shell", "fetch_url")), str(bp))
    check("§6 策略读取失败时原因写在每一项上",
          all("沙箱策略读取失败" in bp[k]["reason"] for k in ("run_command", "run_shell", "fetch_url")), str(bp))
    drift = []
    candidates = (policy(), policy(exec=True), policy(exec=True, allow=["python"]),
                  policy(exec=True, shell=True), policy(shell=True), policy(allow=["python"]),
                  policy(net=True), policy(net=True, hosts=["example.com"]), broken)
    for env_case, launcher_path in (("未启用隔离", ""), ("容器不可用", missing), ("容器可用", str(fake))):
        set_env(launcher_path, "off")
        for cand in candidates:
            notes_text = " ".join(cand.notes())
            plan2 = cand.registration_plan()
            blocked_by_container = "受控命令执行与通用 shell 未注册" in notes_text
            if blocked_by_container:
                # notes 里那句「已启用」讲的是用户开关，容器那一句才讲注册；两者同时出现时以后者为准
                if plan2["run_command"]["registered"] or plan2["run_shell"]["registered"]:
                    drift.append(f"{env_case}: notes 说容器挡住但 plan 说注册了")
            else:
                if "受控命令执行已启用" in notes_text and not plan2["run_command"]["registered"]:
                    drift.append(f"{env_case}: notes 说命令已启用但 plan 没注册")
                if "通用 shell 已启用" in notes_text and not plan2["run_shell"]["registered"]:
                    drift.append(f"{env_case}: notes 说 shell 已启用但 plan 没注册")
                if "run_command 未注册" in notes_text and plan2["run_command"]["registered"]:
                    drift.append(f"{env_case}: notes 说 run_command 未注册但 plan 说注册了")
                if "run_shell 未注册" in notes_text and plan2["run_shell"]["registered"]:
                    drift.append(f"{env_case}: notes 说 run_shell 未注册但 plan 说注册了")
            if "联网获取已启用" in notes_text and not plan2["fetch_url"]["registered"]:
                drift.append(f"{env_case}: notes 说联网已启用但 plan 没注册")
            if ("fetch_url 未注册" in notes_text or "联网工具未注册" in notes_text) and plan2["fetch_url"]["registered"]:
                drift.append(f"{env_case}: notes 说联网未注册但 plan 说注册了")
    check("§6 notes() 与 registration_plan() 在三种容器状态下不吵架", drift == [], "; ".join(drift)[:300])

    print("--- 7. 真起进程：套壳生效、回执点名容器 ---")
    work = Path(tempfile.mkdtemp(prefix="tt-d08-run-"))
    set_env(str(fake), "off")
    exec_policy = policy(exec=True, allow=["cmd"], shell=True)
    audits: list[tuple[str, dict]] = []
    run_command = exec_policy._exec_tool(work, lambda ev, data: audits.append((ev, data)), "task-7")

    def _which(name: str) -> str:
        return shutil.which(name) or ""

    if _which("cmd"):
        resp = run_command("cmd", json.dumps(["/c", "echo", "hi"]))
        text = tool_text(resp)
        check("§7 走容器时回执点名在 AppContainer 内", "AppContainer" in text, text[:200])
        check("§7 回执点名出网关闭", "出网关闭" in text, text[:200])
        check("§7 假启动器收到的是套壳后的命令行", "TT-FAKE-LAUNCHER" in text and "--tt-sandbox-run" in text, text[:260])
        check("§7 套壳命令行里带着出网档位", "--network" in text and "off" in text, text[:260])
        check("§7 审计记下 container=on", any(a[1].get("detail", "").endswith("container=on") for a in audits),
              str([a[1].get("detail") for a in audits]))
        shell_tool = exec_policy._shell_tool(work, lambda ev, data: audits.append((ev, data)), "task-7b")
        sresp = shell_tool('echo "hello world"')
        stext = tool_text(sresp)
        check("§7 shell 通道同样进容器", "AppContainer" in stext, stext[:200])
        check("§7 shell 带 --raw-command-line 交出去", "--raw-command-line" in stext, stext[:300])
        check("§7 shell 的命令行仍在 `--` 之后（cmd + /d /s /c）", "/d /s /c" in stext or ("-- " in stext and "cmd" in stext), stext[:300])
        set_env(missing, "off")
        r2 = run_command("cmd", json.dumps(["/c", "echo", "hi"]))
        t2 = tool_text(r2)
        check("§7 中途容器坏掉 ⇒ 当次拒绝，不回落到宿主权限",
              "为避免以宿主权限执行已拒绝运行" in t2 and "退出码" not in t2, t2[:220])
        s2 = shell_tool("echo hi")
        t3 = tool_text(s2)
        check("§7 shell 中途坏掉同样拒绝并且一次进程也没起",
              "为避免以宿主权限执行已拒绝运行" in t3 and "退出码" not in t3, t3[:220])
        denied = [a[1] for a in audits if a[1].get("outcome") == "denied"]
        check("§7 两次拒绝都进了审计", len(denied) >= 2, str(len(denied)))
        check("§7 审计写明是容器不可用", any("容器不可用" in d.get("detail", "") for d in denied),
              str([d.get("detail") for d in denied][:3]))
    else:
        check("§7 本机找不到 cmd，端到端这一段没跑", False, "PATH 里没有 cmd.exe")
    set_env()
    plain_run = exec_policy._shell_tool(work, None, "task-7c")("echo off-host")
    ptext = tool_text(plain_run)
    check("§7 未启用隔离时回执**不**提容器（与改造前一致）",
          "AppContainer" not in ptext and "退出码" in ptext, ptext[:160])

    print("--- 8. 扩展声明清单 ---")
    exd.reset_for_tests()
    empty = exd.snapshot()
    check("§8 没跑过任务时如实说没有读数", empty["observed"] is False and empty["extensions"] == [], str(empty)[:120])
    check("§8 空读数不伪造扩展", empty.get("counts") == {}, str(empty.get("counts")))
    check("§8 空读数仍给出容器读数（引擎自己看得到）", isinstance(empty.get("container"), dict) and "ready" in empty["container"], str(empty.get("container"))[:120])

    runtime = {
        "skills": [
            {"id": "s-ok", "name": "正常技能", "description": "d", "content": "正文里不许外泄的一句话", "kind": "skill", "source": "installed"},
            {"id": "s-blocked", "name": "高风险技能", "content": "x", "kind": "skill", "source": "installed", "scanBlocked": True},
            {"id": "s-broken", "name": "坏技能", "content": "", "kind": "skill", "source": "installed", "error": "SKILL.md 读不到"},
            {"id": "c-1", "name": "报价指令", "content": "y", "kind": "command", "source": "suite:fin", "suite": "金融行业套件"},
        ],
        "mcp": [
            {"serverId": "m1", "name": "本地打包器", "transport": "stdio",
             "command": "C:/Program Files/nodejs/node.exe", "args": ["mcp/server.js", "--verbose"],
             "env": {"API_TOKEN": "超级的密钥值"}, "tools": [{"name": "build"}], "url": "", "headers": {}},
            {"serverId": "m2", "name": "远程连接器", "transport": "http", "url": "https://example.com/mcp",
             "command": "", "args": [], "env": {}, "tools": [], "headers": {"Authorization": "Bearer 头部密钥值"}},
        ],
        "hooks": {"configured": 3, "mountedEvents": ["PreToolUse"], "firedEvents": ["PreToolUse"],
                  "unfiredEvents": ["Notification"], "mountedUnfired": [],
                  "ungranted": ["python -u guard.sh", "node check.js"], "sandboxed": True},
        "acp": {"agents": [{"id": "a1", "name": "外部审查", "command": "C:/tools/reviewer.exe", "args": []}]},
        "sandbox": {"execEnabled": True, "execAllowlist": ["python"], "shellEnabled": False,
                    "networkEnabled": False, "networkAllowlist": []},
    }
    set_env()
    snap = exd.observe(runtime, task_id="task-8")
    dumped = json.dumps(snap, ensure_ascii=False)
    by_id = {e["id"]: e for e in snap["extensions"]}
    check("§8 读数标记为已观察并带上任务号", snap["observed"] and snap["taskId"] == "task-8", str(snap)[:80])
    check("§8 正常技能可用", by_id["s-ok"]["usable"] is True, str(by_id.get("s-ok"))[:160])
    check("§8 被扫描拦下的技能按不可用列出", by_id["s-blocked"]["usable"] is False and "高风险" in by_id["s-blocked"]["reason"], str(by_id.get("s-blocked"))[:200])
    check("§8 没正文的扩展不可用并带上原因", by_id["s-broken"]["usable"] is False and "SKILL.md" in by_id["s-broken"]["reason"], str(by_id.get("s-broken"))[:200])
    check("§8 套件指令按 kind 区分并带上套件名",
          by_id["c-1"]["kind"] == "suite_command" and by_id["c-1"]["suite"] == "金融行业套件", str(by_id.get("c-1"))[:200])
    check("§8 技能只声明「正文交给模型」", by_id["s-ok"]["declares"] == ["instructions"], str(by_id.get("s-ok", {}).get("declares")))
    check("§8 正文不外泄进清单", "正文里不许外泄的一句话" not in dumped, "清单里出现了技能正文")
    check("§8 stdio 连接器声明了要跑的程序", by_id["mcp:m1"]["executable"] == "node.exe", str(by_id.get("mcp:m1", {}).get("executable")))
    check("§8 stdio 连接器声明了环境变量（只列名字）",
          "env" in by_id["mcp:m1"]["declares"] and "API_TOKEN" in by_id["mcp:m1"]["reason"], str(by_id.get("mcp:m1", {}).get("reason"))[:200])
    check("§8 连接器的密钥值不外泄", "超级的密钥值" not in dumped and "头部密钥值" not in dumped, "清单里出现了凭据值")
    check("§8 http 连接器声明出网但不声明程序",
          by_id["mcp:m2"]["executable"] == "" and "network" in by_id["mcp:m2"]["declares"], str(by_id.get("mcp:m2"))[:200])
    check("§8 Hook 已放行按条汇总", by_id["hooks:granted"]["usable"] is True and "1 条" in by_id["hooks:granted"]["name"], str(by_id.get("hooks:granted"))[:200])
    check("§8 未放行的 Hook 逐条列为不可用",
          by_id["hooks:ungranted:0"]["usable"] is False and "未授权即不注册" in by_id["hooks:ungranted:0"]["reason"],
          str(by_id.get("hooks:ungranted:0"))[:220])
    check("§8 未放行的 Hook 带上首个程序名", by_id["hooks:ungranted:0"]["executable"] == "python", str(by_id.get("hooks:ungranted:0", {}).get("executable")))
    check("§8 Hook 声明了「不等模型点头」这一能力", "auto_run" in by_id["hooks:granted"]["declares"], str(by_id.get("hooks:granted", {}).get("declares")))
    check("§8 桌面端与引擎容器读数不一致时摊开写",
          "不一致" in by_id["hooks:granted"]["reason"], by_id["hooks:granted"]["reason"][:220])
    check("§8 ACP 外派声明了启动命令", by_id["acp:a1"]["executable"] == "reviewer.exe" and "external_agent" in by_id["acp:a1"]["declares"], str(by_id.get("acp:a1"))[:200])
    check("§8 计数按种类归堆", sum(snap["counts"].values()) == len(snap["extensions"]), str(snap["counts"]))
    check("§8 执行面读数与 registration_plan 同源",
          snap["execution"]["runCommand"] == exd._execution(runtime)["runCommand"], str(snap["execution"].get("runCommand")))
    check("§8 本轮未授权 shell ⇒ 清单里就是没注册",
          snap["execution"]["runShell"]["registered"] is False, str(snap["execution"].get("runShell")))
    check("§8 未授权的原因写在清单上（界面能回答「为什么点不通」）",
          snap["execution"]["runShell"]["reason"] != "", str(snap["execution"].get("runShell"))[:160])
    limits_text = " ".join(snap["limits"])
    check("§8 边界摊开：toolsAllow/toolsDeny 到不了引擎", "toolsAllow" in limits_text, limits_text[:200])
    check("§8 边界摊开：已放行 Hook 正文不下发", "SQLite" in limits_text, limits_text[:260])
    check("§8 边界摊开：出网只有整组开关", "整组开关" in limits_text, limits_text[:320])
    check("§8 未启用隔离时边界里点名这一点", "本机未启用系统隔离" in limits_text, limits_text[:400])

    set_env(str(fake), "off")
    snap2 = exd.observe(runtime, task_id="task-8b")
    by_id2 = {e["id"]: e for e in snap2["extensions"]}
    check("§8 启用隔离后连接器标为进容器", by_id2["mcp:m1"]["container"] == "in", str(by_id2.get("mcp:m1", {}).get("container")))
    check("§8 启用隔离后 Hook 汇总的容器标记跟上", by_id2["hooks:granted"]["container"] == "in", str(by_id2.get("hooks:granted", {}).get("container")))
    check("§8 两侧读数一致时不再报差异", "不一致" not in by_id2["hooks:granted"]["reason"],
          by_id2["hooks:granted"]["reason"][:200])
    set_env(missing, "off")
    snap2b = exd.observe(runtime, task_id="task-8b2")
    granted_b = {e["id"]: e for e in snap2b["extensions"]}["hooks:granted"]
    check("§8 容器起不来时 Hook 汇总说明按不放行处理",
          "容器不可用" in granted_b["reason"], granted_b["reason"][:200])
    check("§8 容器起不来时清单里点名执行面不注册",
          {e["id"]: e for e in snap2b["extensions"]}.get("hooks:ungranted:0", {}).get("usable") is False
          and snap2b["execution"]["runCommand"]["registered"] is False,
          str(snap2b["execution"].get("runCommand")))
    check("§8 启用隔离后执行面注册判定不变（本轮仍受授权约束）",
          snap2["execution"]["runCommand"]["registered"] is True, str(snap2["execution"].get("runCommand")))

    big = {"skills": [{"id": f"s{i}", "name": f"技能{i}", "content": "x", "kind": "skill"} for i in range(exd.MAX_ENTRIES + 50)]}
    snap3 = exd.observe(big, task_id="task-8c")
    check("§8 条目超上限时截断并如实说明",
          len(snap3["extensions"]) == exd.MAX_ENTRIES and snap3["truncated"] is True, str(len(snap3["extensions"])))
    check("§8 截断说明进 limits", any("超过" in s for s in snap3["limits"]), str(snap3["limits"][-1:])[:200])
    snap4 = exd.observe({"skills": "不是列表", "mcp": {"error": "表读不出来"}, "hooks": "{坏 JSON", "acp": 7}, task_id="task-8d")
    check("§8 脏 runtime 不抛异常", snap4["observed"] is True, str(snap4)[:120])
    check("§8 连接器表读不出来时有专门条目",
          any(e["id"] == "mcp:table" and e["usable"] is False for e in snap4["extensions"]), str(snap4["extensions"])[:240])
    check("§8 坏 JSON 的 Hooks 也如实报读不出来",
          any(e["id"] == "hooks:read" and "JSON" in e["reason"] for e in snap4["extensions"]), str(snap4["extensions"])[:240])
    snap5 = exd.observe(None)
    check("§8 runtime 缺失时不崩且只给边界说明", snap5["observed"] is True and snap5["extensions"] == [], str(snap5)[:120])

    print("--- 9. 车道端点 ---")
    exd.reset_for_tests()
    rln.check_lane(__import__("routes_exec"))
    check("§9 exec 车道的前缀登记里没有既有路由重名（挂载面自检通过）", True)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routes_exec as rx

    app = FastAPI()
    app.include_router(rx.router)
    client = TestClient(app)
    body = client.get("/api/exec/extensions").json()
    check("§9 端点返回的是空读数而不是伪造清单", body.get("observed") is False and body.get("extensions") == [], str(body)[:160])
    exd.observe({"skills": [{"id": "s1", "name": "一个技能", "content": "x"}]}, task_id="task-9")
    got = client.get("/api/exec/extensions")
    body2 = got.json()
    check("§9 跑过一次任务后端点读得到同一份快照",
          got.status_code == 200 and body2.get("observed") is True and body2.get("taskId") == "task-9", str(body2)[:160])
    check("§9 清单条目形状稳定（界面按这几个字段渲染）",
          set(body2["extensions"][0]) >= {"id", "name", "kind", "declares", "usable", "reason", "container"},
          str(sorted(body2["extensions"][0]))[:200])
    check("§9 车道模块没有 import server（铁律）",
          "import server" not in (PROJECT_ROOT / "engine" / "routes_exec.py").read_text(encoding="utf-8"),
          "routes_exec.py 里出现了 import server")

    restore_env()
    exd.reset_for_tests()
    print(f"[SUMMARY] failures={len(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
