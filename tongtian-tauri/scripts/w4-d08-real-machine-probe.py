"""W4 实机取证（#144 / D-08 验收句「出网关闭时容器内连回环也不可达」）。

只用真实主程序的沙箱启动器（`src-tauri/target/debug/tongtian-tauri.exe --tt-sandbox-run`）
与引擎真实的授权件（`tool_grants.ToolGrantPolicy`），不掺假启动器、不 monkeypatch。
判定打 `^[PASS]` / `^[FAIL]`，跑在 `./vendor/python/python.exe -X utf8` 下。

五条设计说明（都是为了让「容器里连不上」这四个字真的算证据）：
① **宿主对照**：同一条命令在不进容器时必须连得上本机回环，否则容器里连不上什么也没证明；
② **会跑对照**：同一档位下跑一条不碰网络的命令必须成功，排除「容器起不来 / 解释器读不到」这种假绿；
③ **落盘对照**：容器里的进程要真的能写进**工作目录**，证明执行的确实是那条命令而不是空转。
   工作目录放在数据根之外（见 ④），否则测到的只是那条拒绝；
④ **越界对照**：容器内写数据根（`TONGTIANWORK_HOME`，密钥与会话库所在）必须失败。
   启动器对该根整棵拒绝，且拒绝压过 `--workspace` 的读写授权 —— 第一版把两者叠在同一
   目录上，撞出来的 `PermissionError` 正是这条边界，现在把它写成断言而不是背景知识；
⑤ **不拿「`--network on` 就能连回环」当断言**：Windows 的 AppContainer 即使拿到
   `internetClient` 能力，回环仍要系统级 LoopbackExempt 豁免（那是机器配置，不是本仓库的策略），
   这一档只打印读数。
解释器一律用 `vendor/python`（随包那份）。本机 `C:\\Python313\\python.exe` 在容器里会以
`0xc0000139` 起不来（同一条命令在宿主正常），读数记在 `[INFO]` 里，不当成本仓库的结论。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
EXE = ROOT / "src-tauri" / "target" / "debug" / "tongtian-tauri.exe"
VENDOR_DIR = ROOT / "vendor" / "python"

_tmp = tempfile.mkdtemp(prefix="w4-d08-home-")
_ws = tempfile.mkdtemp(prefix="w4-d08-ws-")  # 工作目录：数据根之外，见 docstring ④
os.environ["TONGTIANWORK_HOME"] = _tmp
os.environ["PATH"] = str(VENDOR_DIR) + os.pathsep + os.environ.get("PATH", "")
os.environ["TT_SANDBOX_BIN"] = str(EXE)
os.environ["TT_SANDBOX_NETWORK"] = "off"
os.environ["TT_SANDBOX_RUNTIME_DIRS"] = str(VENDOR_DIR)
sys.path.insert(0, str(ENGINE))

import sandbox_launcher as sl  # noqa: E402
import tool_grants as tg  # noqa: E402

PYNAME = "python"  # 授权件只收裸命令名，不收路径
PYEXE = shutil.which(PYNAME)

PASS = [0]
FAIL = [0]


def check(name, ok, detail=""):
    if ok:
        PASS[0] += 1
        print(f"[PASS] {name}")
    else:
        FAIL[0] += 1
        print(f"[FAIL] {name} :: {detail}")


def start_listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.sendall(b"TT-D08-ECHO\n")
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv, port


PROBE = (
    "import socket,sys;"
    "s=socket.create_connection(('127.0.0.1',%d),3);"
    "d=s.recv(64);s.close();"
    "sys.stdout.write('REACHED:'+d.decode().strip())"
)
BENIGN = "import sys;sys.stdout.write('OK-RAN')"

srv, port = start_listener()
check("PATH 上的裸命令 python 就是随包那份", PYEXE is not None and Path(PYEXE).parent == VENDOR_DIR, repr(PYEXE))
check("真实启动器在盘上（不是假 .bat）", EXE.exists(), str(EXE))

# ── 1. 宿主对照：不进容器，同一条命令必须连得上 ─────────────────────────
hp = subprocess.run([PYEXE, "-c", PROBE % port], capture_output=True, text=True, timeout=60)
check(
    "宿主（不进容器）能连上本机回环监听",
    hp.returncode == 0 and "REACHED:TT-D08-ECHO" in hp.stdout,
    f"rc={hp.returncode} out={hp.stdout[:200]!r} err={hp.stderr[:200]!r}",
)

# ── 2. 启动器直接跑：--network off 时容器内不许连回环 ────────────────────
wrapped = sl.wrap(PYNAME, ["-c", PROBE % port], workspace=_ws)
check("sandbox_launcher.wrap 在实机 exe 上给出可执行 argv", wrapped is not None, "wrap 返回 None")
if wrapped:
    exe, largv = wrapped
    check("启动器 argv 带 --network off", "--network" in largv and "off" in largv, str(largv)[:300])
    cp = subprocess.run([exe, *largv], capture_output=True, text=True, timeout=180)
    check(
        "容器内（--network off）没有连上本机回环",
        "REACHED:TT-D08-ECHO" not in cp.stdout,
        f"rc={cp.returncode} out={cp.stdout[:300]!r}",
    )
    benign = sl.wrap(PYNAME, ["-c", BENIGN], workspace=_ws)
    bp = subprocess.run([benign[0], *benign[1]], capture_output=True, text=True, timeout=180)
    check(
        "同档位下不碰网络的命令在容器内正常成功（拒绝来自出线而不是容器坏了）",
        bp.returncode == 0 and "OK-RAN" in bp.stdout,
        f"rc={bp.returncode} out={bp.stdout[:200]!r} err={bp.stderr[:400]!r}",
    )
    wfile = Path(_ws) / "container-written.txt"
    write_case = sl.wrap(
        PYNAME,
        ["-c", f"open(r'{wfile}','w').write('FROM-CONTAINER')"],
        workspace=_ws,
    )
    wp = subprocess.run([write_case[0], *write_case[1]], capture_output=True, text=True, timeout=180)
    check(
        "容器内的进程真的写了工作目录（执行的是那条命令，不是空转）",
        wp.returncode == 0 and wfile.exists() and wfile.read_text(encoding="utf-8") == "FROM-CONTAINER",
        f"rc={wp.returncode} err={wp.stderr[:300]!r}",
    )
    # ④ 越界对照：数据根（密钥与会话库所在）在容器内必须写不进去
    root_file = Path(_tmp) / "should-not-exist.txt"
    escape_case = sl.wrap(
        PYNAME,
        ["-c", f"open(r'{root_file}','w').write('ESCAPED')"],
        workspace=_ws,
    )
    ep = subprocess.run([escape_case[0], *escape_case[1]], capture_output=True, text=True, timeout=180)
    check(
        "容器内写不进数据根（--workspace 的授权不会外溢到密钥与会话库那棵树）",
        ep.returncode != 0 and not root_file.exists(),
        f"rc={ep.returncode} 文件已生成={root_file.exists()} out={ep.stdout[:200]!r} err={ep.stderr[:300]!r}",
    )
    on_prev = os.environ["TT_SANDBOX_NETWORK"]
    os.environ["TT_SANDBOX_NETWORK"] = "on"
    w2 = sl.wrap(PYNAME, ["-c", PROBE % port], workspace=_ws)
    c2 = subprocess.run([w2[0], *w2[1]], capture_output=True, text=True, timeout=180)
    print(
        "[INFO] --network on 的读数（受系统 LoopbackExempt 影响，不参与判定）："
        f"rc={c2.returncode} reached={'REACHED:TT-D08-ECHO' in c2.stdout} err={c2.stderr[:160]!r}"
    )
    host_other = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Python313" / "python.exe"
    if host_other.exists():
        w3 = sl.wrap(str(host_other), ["-c", BENIGN], workspace=_ws)
        c3 = subprocess.run([w3[0], *w3[1]], capture_output=True, text=True, timeout=180)
        hp3 = subprocess.run([str(host_other), "-c", BENIGN], capture_output=True, text=True, timeout=60)
        print(
            f"[INFO] 本机 {host_other} 在宿主 rc={hp3.returncode} / 在容器内 rc={c3.returncode}"
            "（容器内起不来的是这台机器的另一个解释器，不是随包那份；不判定，留给人查）"
        )
    os.environ["TT_SANDBOX_NETWORK"] = on_prev

# ── 3. 引擎授权件 run_command 走真实容器 ────────────────────────────────
base = Path(_ws) / "engine"
base.mkdir(parents=True, exist_ok=True)


def policy(shell_on: bool) -> tg.ToolGrantPolicy:
    return tg.ToolGrantPolicy.from_runtime({
        "sandbox": {
            "execEnabled": True,
            "execAllowlist": [PYNAME],
            "shellEnabled": shell_on,
            "networkEnabled": False,
            "networkAllowlist": [],
        }
    })


pol = policy(False)
plan = pol.registration_plan()
check(
    "实机下 registration_plan 判定 run_command 可注册（容器就绪）",
    plan["run_command"]["registered"] is True,
    json.dumps(plan, ensure_ascii=False)[:400],
)
check(
    "container_state 报 enabled 且 ready",
    plan["container"].get("ready") is True and plan["container"].get("enabled") is True,
    str(plan["container"])[:300],
)

audits = []
tool = pol._exec_tool(base, lambda ev, p: audits.append((ev, dict(p))), "w4-d08")
text = "".join(
    b.get("text", "") for b in tool(PYNAME, json.dumps(["-c", PROBE % port]), 90).content
    if isinstance(b, dict)
)
check("run_command 回执点名系统隔离容器（AppContainer）", "AppContainer" in text, text[:400])
check("run_command 回执说明出网关闭", "出网关闭" in text, text[:400])
check("run_command 在容器内没拿到回环数据", "REACHED:TT-D08-ECHO" not in text, text[:400])
check("run_command 确实起了进程（回执有退出码一行）", "退出码" in text, text[:200])
det = " | ".join(str(p.get("detail", "")) for _, p in audits)
check("审计里记了 container=on", "container=on" in det, det[:400])

text_b = "".join(
    b.get("text", "") for b in tool(PYNAME, json.dumps(["-c", BENIGN]), 90).content
    if isinstance(b, dict)
)
check("run_command 在容器内照常干活并带回 stdout（套壳没改变命令的含义）", "OK-RAN" in text_b, text_b[:500])

# ── 4. 通用 shell 通道（Windows 命令行字符串那支）同样进容器 ──────────────
pol2 = policy(True)
plan2 = pol2.registration_plan()
check("实机下 shell 也判定可注册", plan2["run_shell"]["registered"] is True, str(plan2)[:300])
audits2 = []
tool2 = pol2._shell_tool(base, lambda ev, p: audits2.append((ev, dict(p))), "w4-d08-shell")
shell_cmd = f'{PYNAME} -c "{PROBE % port}"'

# ① 的同一条命令走 shell 通道，但**不进容器**：先证明这套引号在宿主真的跑起来了。
# 没有这一条，「容器里没有回环数据」就可能只是「引号没生效、命令压根没跑」——
# 那正是 D-48.4c 抓过的假绿（rc=0 静默空跑），本仓自己踩过。
_bin = os.environ.pop("TT_SANDBOX_BIN")
host_text = "".join(b.get("text", "") for b in tool2(shell_cmd, 90).content if isinstance(b, dict))
os.environ["TT_SANDBOX_BIN"] = _bin
check(
    "宿主对照：同一条带引号的命令走 shell 通道能连上回环（证明命令真的跑了）",
    "REACHED:TT-D08-ECHO" in host_text and "AppContainer" not in host_text,
    host_text[:500],
)

text2 = "".join(b.get("text", "") for b in tool2(shell_cmd, 90).content if isinstance(b, dict))
check("run_shell 回执点名容器", "AppContainer" in text2, text2[:500])
check("run_shell 在容器内没拿到回环数据", "REACHED:TT-D08-ECHO" not in text2, text2[:500])
check(
    "run_shell 进了容器但命令照样执行（stderr 是连线失败不是启动器报错）",
    "Traceback" in text2 and "create_connection" in text2 and "沙箱启动器参数错误" not in text2,
    text2[:500],
)
check("run_shell 起了进程并给出退出码（引号那支没有静默空跑）", "退出码" in text2, text2[:300])
det2 = " | ".join(str(p.get("detail", "")) for _, p in audits2)
check("run_shell 审计记了 container=on", "container=on" in det2, det2[:400])

text3 = "".join(
    b.get("text", "") for b in tool2(f'{PYNAME} -c "{BENIGN}"', 90).content if isinstance(b, dict)
)
check("容器内 shell 跑通不碰网络的命令并带回 stdout", "OK-RAN" in text3, text3[:500])

# ── 5. 声明清单（界面那一栏读的东西）在实机读数下自洽 ────────────────────
import extension_decls as exd  # noqa: E402

exd.reset_for_tests()
exd.observe({
    "sandbox": {
        "execEnabled": True,
        "execAllowlist": [PYNAME],
        "shellEnabled": True,
        "networkEnabled": False,
        "networkAllowlist": [],
    }
}, task_id="w4-d08")
snap = exd.snapshot()
run = snap["execution"]
check("声明清单读到执行面三项", {"runCommand", "runShell", "fetchUrl"} <= set(run), str(run)[:300])
check("声明清单里 runCommand 标为已注册", run["runCommand"]["registered"] is True, str(run)[:300])
check("声明清单里未授权的 fetchUrl 标为不可用并带原因", run["fetchUrl"]["registered"] is False and bool(run["fetchUrl"]["reason"]), str(run)[:300])
check("声明清单里的容器读数与授权件同源", run["container"]["ready"] is True, str(run["container"])[:300])

srv.close()
print(f"[SUMMARY] pass={PASS[0]} fail={FAIL[0]}")
