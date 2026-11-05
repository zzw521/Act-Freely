#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-28 可选遥测与崩溃导出自测（#141，W4 领地）：两半模块 + 装配现场 + 端点面 + 跨语言同源。

这一族要钉住的是**两句承诺**，它们各自都可能被写成装饰：

  承诺一「默认关闭、显式同意、看得见将发送什么」——所以用例必须证明
  没同意时**一个字节也不写**（不是「写了但没发」），开启**没有确认词就开不了**，
  关闭**永远能关**且当场删缓冲，而 `preview` 给出的数组与 `flush` 交给出口的
  **是同一份构造代码的产物**（不是另写一遍的温和展示版）。

  承诺二「崩溃本地留存并提示导出」——所以用例必须证明凭据形状在**落盘之前**就被打掉
  （导出时再打等于已经写进盘了），现场只在本机、导出是**副本**，
  以及挂钩自己**绝不抛**（处理崩溃的代码再崩一次就把这件事彻底输掉了）。

分四层：

1. 模块本体（``crash_runtime`` / ``telemetry_runtime``）：脱敏、留存、去重、上限、
   导出/删除、同意三态、白名单与词表、缓冲上限、出口有/无/拒/炸四种下场的如实回话；
2. 装配现场（``server.py``）：钩子在 main 与 lifespan 都装（幂等）、异常处理器接住
   请求级异常并回 500 而不带原文、收尾计数在 ``done`` 之后（事件流一个字节也不改）、
   观察者把「崩过一次」转成计数而**不让留存依赖外发面**；
3. 端点面（``routes_exec`` 的 ``/api/exec/telemetry/*`` 与 ``/api/exec/crash/*``）：
   两族整族只对桌面壳开放、只读走 GET 会改状态走 POST、出口不接受 HTTP 设定、
   删除不接受整批、业务拒绝一律 200 + ok=false + 中文 reason；
4. 跨语言同源：``src/shared/contracts_exec.ts`` 的接口字段与**实际回话键集**双向核对
   （D-48.4b 与 #161 的教训：TS 漂移一次，界面就显示一份引擎从来没回过的东西）。

必须用 ``./vendor/python/python.exe -X utf8`` 跑。缺第三方依赖时整族 SKIP；
**引擎自己的文件导入断了则大声红**（静默 SKIP 会把真红报成没测，代价在案）。

未验收的一半如实写明：真实桌面设置页的开关与「崩溃记录」面板、Rust 侧把这些读数
推进界面（本卡零新增 IPC 通道）、以及真实进程被杀之后现场仍在（那是重启后另一进程读盘，
本文件用同一进程内的临时数据根跑通，不声称已经演过杀进程）。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_telemetry_crash_selftest.py
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-telemetry-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

ENGINE_MODULES = {path.stem for path in (PROJECT_ROOT / "engine").glob("*.py")}

FAILURES: list[str] = []

#: 第 4 节真调用时记下的回话键集，第 8 节拿它去和 TS 接口双向核对
#: （形状从**实际回话**来，不在测试里手抄第二份清单——手抄的两份可以一起过期）
SEEN_KEYS: dict[str, set[str]] = {}


def _drop_tmp() -> None:
    """跑干净就删掉这份临时数据根；**红了留着**——那份目录是唯一能看出「现场到底被写成什么样」的东西，
    失败时清掉等于把案发现场扫干净再报「案发现场找不到」。"""
    if not FAILURES:
        shutil.rmtree(_TMP, ignore_errors=True)


atexit.register(_drop_tmp)

LOCAL_TOKEN = "tt-selftest-local-token"
STATIC_TOKEN = "tt-selftest-static-token"

#: 形状像真模型密钥的假值（只进临时目录）。多处用例靠它反证「落盘前已经打掉」，
#: 所以正文里不许出现第二个含义；一旦盘上找到这个串，就是脱敏漏了。
FAKE_MODEL_KEY = "sk-" + "W4Selftest" + "K9x7Qm2Zd4Rn"
FAKE_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJ0ZXN0In0" + "." + "signature_here_ok"

TS_CONTRACT = PROJECT_ROOT / "src" / "shared" / "contracts_exec.ts"
ROUTES_SRC = PROJECT_ROOT / "engine" / "routes_exec.py"
CRASH_SRC = PROJECT_ROOT / "engine" / "crash_runtime.py"
TELEMETRY_SRC = PROJECT_ROOT / "engine" / "telemetry_runtime.py"
SERVER_SRC = PROJECT_ROOT / "engine" / "server.py"

#: 现场/事件里绝不该出现的字段名（白名单表的反向尺子：加字段先过这一关）
FORBIDDEN_FIELD_WORDS = (
    "prompt", "content", "message", "messages", "text", "body", "path", "file",
    "workspace", "cwd", "command", "argv", "url", "model", "api", "key", "token",
    "secret", "password", "traceback", "stdout", "output", "session", "conversation",
)


def _forbidden_field(name: str) -> bool:
    """这个词表扫描到底拦什么。

    拦的是**取值形状**：正文、路径、命令、模型名、密钥——进了白名单就等于往外发。
    放行以 `Count` 结尾的纯计数：`modelCallCount` 记的是「打了三次模型」这个整数，
    不含模型标识；一刀切会让遥测连「这一轮跑了几次」都留不住，那是把治理面写成哑巴。
    口子只此一条，且下面有用例钉住 `modelId` / `promptText` 仍然被拦。
    """
    low = str(name or "").lower()
    if low.endswith("count"):
        return False
    return any(word in low for word in FORBIDDEN_FIELD_WORDS)

#: 本机原始异常钩子：本文件会真的换掉 sys.excepthook，收尾必须还原，
#: 否则同进程里后面别的自测（或被测代码本身）的崩溃会写进本文件的临时目录
_ORIGINAL_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREAD_HOOK = threading.excepthook


def import_gate(exc: BaseException, suite: str) -> None:
    """只在**真缺第三方依赖**时优雅跳过；我们自己的引擎文件断了必须红着退出。"""
    root = exc
    for _ in range(12):
        nxt = root.__cause__ or root.__context__
        if nxt is None:
            break
        root = nxt
    missing = getattr(root, "name", "") or ""
    if isinstance(root, ModuleNotFoundError) and missing not in ENGINE_MODULES:
        print(f"SKIP: {suite} unavailable（缺第三方依赖 {missing or root}）")
        sys.exit(0)
    print(f"[FAIL] {suite} 起不来：引擎侧导入断了，不是依赖缺失 — {type(root).__name__}: {root}")
    print("[FAIL] 整族未运行（拒绝把上面的断裂报成 SKIP）")
    sys.exit(1)


try:
    import crash_runtime as crt
    import data_paths as dp
    import routes_exec as rx
    import routes_lanes as rl
    import telemetry_runtime as tlm
    import server
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "telemetry crash selftest")

# server 的导入链可能把数据根拧到别处，这里再拧回本文件的临时目录：
# 现场与缓冲都必须落在临时目录，不许碰真的引擎数据目录（假用量写进真实台账的代价在案）。
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")
HOME_ROOT = _TMP / "home"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"--- {title} ---")


def headers(token: str = LOCAL_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def desktop_env() -> None:
    """桌面壳在场：本机令牌 + 远程令牌都配好，请求带本机令牌才算桌面壳。"""
    os.environ["TT_ENGINE_TOKEN"] = STATIC_TOKEN
    os.environ["TT_ENGINE_LOCAL_TOKEN"] = LOCAL_TOKEN


def _ts_interface_fields(name: str) -> set[str]:
    """从 contracts_exec.ts 刮出一个 interface 的字段名（反向核对用，不在测试里手抄清单）。

    声明行可能是 `export interface X {`，也可能是 `export interface X extends Y {`——
    后者少刮一次就会把整个接口读成空字段集，把「TS 多写了十个字段」报成「都对上了」。
    """
    ts = TS_CONTRACT.read_text(encoding="utf-8")
    head = ts.index(f"export interface {name}")
    brace = ts.index("{", head)
    body = ts[brace:ts.index("\n}", brace)]
    # 字段名里带数字是真事（`sha256`、`traceSha256`），只匹配字母的话这类字段会从
    # 声明里凭空消失，于是「TS 明明写了 sha256」也被读成「TS 缺这个键」。
    return set(re.findall(r"^\s+(?:readonly\s+)?([a-zA-Z][a-zA-Z0-9_]*)\??[:,]", body, re.M))


def raise_demo(text: str = "demo") -> tuple:
    """造一份**每次都不同**的真实 traceback：去重按栈指纹，测试要区分条数就得换正文。"""
    try:
        exec(f"raise ValueError({text!r})")  # noqa: S102  自测自己造的栈，正文只进临时目录
    except ValueError as exc:
        return type(exc), exc, exc.__traceback__
    raise AssertionError("自测造不出演示栈")


def scene(category: str = "manual", text: str = "", **kw) -> dict:
    et, ev, tb = raise_demo(text or f"{time.time_ns()}")
    return crt.record(
        category,
        exc_type=et.__name__,
        exc_text=str(ev),
        traceback_text="".join(__import__("traceback").format_exception(et, ev, tb)),
        **kw,
    )


# ── 1. 数据根归属 ───────────────────────────────────────────────


def layout_checks() -> None:
    section("1. 现场与缓冲都落在 D-43 数据根里，且路径只有一处来源")
    check("engine_home 已被环境指向临时目录", Path(str(dp.engine_home())).resolve() == HOME_ROOT.resolve(), str(dp.engine_home()))
    check("崩溃根由 data_paths 解析（本模块不自己拼目录名）",
          crt.crash_home() == dp.crash_dir(), f"{crt.crash_home()} vs {dp.crash_dir()}")
    check("遥测根由 data_paths 解析", tlm.telemetry_home() == dp.telemetry_dir())
    check("同意文件与缓冲文件都来自 data_paths",
          tlm.consent_file() == dp.telemetry_consent_file() and tlm.events_file() == dp.telemetry_events_file())
    check("崩溃目录名与遥测目录名不同名（两份事实不混在一个目录里）",
          dp.CRASH_DIRNAME != dp.TELEMETRY_DIRNAME and dp.CRASH_DIRNAME in str(crt.crash_home()))
    check("现场不在任何模型能写的工作目录里（数据根下，与写暂存同一条理由）",
          str(HOME_ROOT.resolve()) in str(crt.reports_dir().resolve())
          and "workdir" not in str(crt.reports_dir()).lower())
    check("本文件没在真数据根留东西（所有写入都进临时目录）",
          str(HOME_ROOT) in str(tlm.events_file()) and str(HOME_ROOT) in str(crt.reports_dir()))
    names = CRASH_SRC.read_text(encoding="utf-8") + TELEMETRY_SRC.read_text(encoding="utf-8")
    check("两个模块都不自己读环境变量找数据根（D-43 单一来源）",
          "TONGTIANWORK_HOME" not in names and "TT_DATA_HOME" not in names, str(re.findall(r"TONGTIANWORK_HOME|TT_DATA_HOME", names))[:80])
    mine = Path(__file__).read_text(encoding="utf-8")
    check("本自测自己不留垃圾：跑干净就删临时数据根，红了才留着（一次绿跑换一个永久目录是债）",
          "atexit.register(_drop_tmp)" in mine and "if not FAILURES:" in mine)


# ── 2. 脱敏（落盘之前，不是导出时）─────────────────────────────


def redaction_checks() -> None:
    section("2. 凭据形状在写盘之前就被打掉；家目录与数据根换成占位符")
    cases = {
        "认证头": ('headers={"Authorization": "Bearer super-secret-value"}', "super-secret-value"),
        "键值凭据": ("api_key = abcdef123456 and password=hunter22", "abcdef123456"),
        "模型密钥": ("using sk-abcdef1234567890 for chat", "sk-abcdef1234567890"),
        "JWT": (FAKE_JWT, FAKE_JWT),
        "长随机串": ("blob Zm9vYmFyYmF6cXV1eGNvcmdlZm9vYmFyMDEyMzQ=", "Zm9vYmFyYmF6cXV1eGNvcmdlZm9vYmFy"),
    }
    for kind, (probe, secret) in cases.items():
        got = crt.redact_secrets(probe)
        check(f"{kind}：这一类形状被打掉", kind in got["kinds"] and got["hits"] > 0, str(got)[:200])
        check(f"{kind}：原值不再出现在结果里", secret not in got["text"], got["text"][:160])
    home = str(Path.home())
    root = str(dp.engine_home())
    got = crt.redact_secrets(f'File "{root}\\x\\y.py" line 1 in m\n{home}\\secrets.txt')
    check("数据根换成占位符 <数据根>", "<数据根>" in got["text"] and root not in got["text"], got["text"][:200])
    check("家目录换成 ~", "~" in got["text"] and home not in got["text"], got["text"][:200])
    check("家目录与数据根两类都登记进 kinds", "家目录" in got["kinds"] and "数据根" in got["kinds"], str(got["kinds"]))
    check("先换长的数据根：不出现 ~/<数据根> 这种套娃", "~<" not in got["text"], got["text"][:200])
    plain = crt.redact_secrets("ValueError: 第 3 步的参数不是整数")
    check("普通中文文本不被改坏", plain["text"] == "ValueError: 第 3 步的参数不是整数" and plain["hits"] == 0)
    check("空串与 None 都不炸", crt.redact_secrets(None)["text"] == "" and crt.redact_secrets("")["kinds"] == [])
    src = CRASH_SRC.read_text(encoding="utf-8")
    check("规则名表与正则表同源于本文件（不是两处各抄一份）",
          set(re.findall(r'\("([^"]+)", re\.compile', src)) == set(crt.REDACTION_KINDS) - {"家目录", "数据根"},
          str(sorted(set(re.findall(r'\("([^"]+)", re\.compile', src)))))
    tele_src = TELEMETRY_SRC.read_text(encoding="utf-8")
    check("脱敏规则全仓库只有一份（遥测不抄正则：没有 re.compile、不 import re，只调 crash_runtime 的函数）",
          "redact_secrets" in tele_src and "re.compile" not in tele_src
          and not re.search(r"^\s*import re\s*$", tele_src, re.M),
          f"redact_secrets={'redact_secrets' in tele_src} re.compile={'re.compile' in tele_src}")


# ── 3. 现场落盘与索引形状 ─────────────────────────────────────


def record_checks() -> None:
    section("3. 一份现场长什么样：落盘、截断、列表不带正文、坏 JSON 让路")
    crt.reset_for_tests()
    res = scene("manual", text=f"记录一条 {FAKE_MODEL_KEY} {FAKE_JWT}",
                note=f"密钥 {FAKE_MODEL_KEY} 与 {FAKE_JWT} 在这里")
    check("record 成功回 ok 与编号", res.get("ok") is True and res.get("reportId", "").startswith("crash-"), str(res)[:200])
    check("现场文件真的在盘上", Path(res["path"]).exists(), res["path"])
    data = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
    check("note 进了现场且已脱敏", "sk-***" in data["note"] and FAKE_MODEL_KEY not in data["note"], str(data["note"])[:120])
    check("traceback 里没有那条密钥", FAKE_MODEL_KEY not in data["traceback"] and FAKE_JWT not in data["traceback"],
          str(data["traceback"])[-160:])
    on_disk = "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in sorted(crt.reports_dir().glob(f"*{crt.REPORT_SUFFIX}"))
    ) + "".join(p.read_text(encoding="utf-8", errors="ignore") for p in crt.reports_dir().glob("*"))
    check("脱敏发生在写盘之前：把整个现场目录读一遍也找不到那两个形状",
          FAKE_MODEL_KEY not in on_disk and FAKE_JWT not in on_disk,
          f"目录下 {len(list(crt.reports_dir().glob('*')))} 个文件里出现了原值")
    check("现场带栈指纹与字节数（导出后仍能对上同一条栈）",
          len(data["traceSha256"]) == 64 and data["traceBytes"] > 0)
    check("现场带 runtime 但不带用户名与机器名",
          set(data["runtime"]) == {"python", "implementation", "platform", "pid", "frozen"}, str(sorted(data["runtime"])))
    check("导出时间戳缺省为 0（没导过就是 0，不是缺字段）", data["exportedAtMs"] == 0)
    big = scene("worker_thread", text="超长栈")
    huge = "X" * (crt.MAX_TRACEBACK_BYTES + 4096)
    r2 = crt.record("event_loop", exc_type="RuntimeError", exc_text="超长", traceback_text=huge)
    check("超过上限的 traceback 被截断并如实标 traceTruncated", r2.get("ok") is True, str(r2)[:160])
    d2 = crt.detail(r2["reportId"])
    check("截断后现场正文就是上限", d2["traceBytes"] == crt.MAX_TRACEBACK_BYTES, str(d2["traceBytes"]))
    check("超长但不是凭据形状的正文没被打码打掉（留存还是第一位的）",
          d2["traceback"].count("X") > crt.MAX_TRACEBACK_BYTES - 10, str(d2["traceback"].count("X")))
    check("列表里那条标了 truncated", any(x["reportId"] == r2["reportId"] and x["truncated"] for x in crt.list_reports(200)["reports"]))
    listing = crt.list_reports(5)
    check("列表回话形状齐全", {"ok", "count", "returned", "limit", "loadErrors", "reports", "hasMore", "dir"} <= set(listing))
    check("列表**不带** traceback 正文（几十条正文会把回话撑爆，也没人读）",
          all("traceback" not in row for row in listing["reports"]), str(sorted(listing["reports"][0])))
    check("limit 认字符串、夹到上限、非法值退回缺省",
          crt.list_reports("3")["limit"] == 3 and crt.list_reports("99999")["limit"] == crt.MAX_LIST_LIMIT
          and crt.list_reports("随便")["limit"] == crt.DEFAULT_LIST_LIMIT)
    bad = crt.reports_dir() / "crash-broken.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    listed = crt.list_reports(200)
    check("坏 JSON 改名让路并计入 loadErrors（不静默当成没有）",
          listed["loadErrors"] == 1 and any(x["category"] == "unreadable" for x in listed["reports"]), str(listed)[:200])
    check("坏文件被改名留档，不原地删掉", not bad.exists() and list(crt.reports_dir().glob("crash-broken.json.corrupt-*")))
    check("详情读坏现场回 ok=false 加中文 reason", crt.detail("crash-broken")["ok"] is False)
    for probe in ("../etc/passwd", "a/b", "", "无中文编号", "x" * 200):
        check(f"编号「{probe[:16]}」不接受：不越出目录也不炸", crt.detail(probe).get("ok") is False, str(crt.detail(probe))[:120])
    check("不存在的合法编号也回 ok=false（不是抛 KeyError）", crt.detail("crash-nope")["ok"] is False)
    check("delete 不存在的编号回 ok=false 与中文 reason", crt.delete("crash-nope")["ok"] is False)
    Path(big["path"]).unlink(missing_ok=True)
    Path(r2["path"]).unlink(missing_ok=True)
    crt.delete(res["reportId"])


# ── 4. 去重、上限与上下文白名单 ────────────────────────────────


def dedupe_checks() -> None:
    section("4. 同栈在窗口内合成一条；超上限丢最旧并如实计数；上下文只认白名单")
    crt.reset_for_tests()
    base = int(time.time() * 1000)
    et, ev, tb = raise_demo("同一条栈")
    tb_text = "".join(__import__("traceback").format_exception(et, ev, tb))
    first = crt.record("uncaught_exception", exc_type="ValueError", exc_text="同一条栈",
                       traceback_text=tb_text, at_ms=base)
    again = crt.record("uncaught_exception", exc_type="ValueError", exc_text="同一条栈",
                       traceback_text=tb_text, at_ms=base + 1000)
    check("同类同栈在窗口内不重复落一份", again.get("deduped") is True and again["reportId"] == first["reportId"], str(again)[:200])
    row = crt.detail(first["reportId"])
    check("合成一条时 times 累加（一次故障常在几个钩子里各抛一次）", row["times"] == 2, str(row["times"]))
    late = crt.record("uncaught_exception", exc_type="ValueError", exc_text="同一条栈",
                      traceback_text=tb_text, at_ms=base + crt.DEDUPE_WINDOW_MS + 5000)
    check("超出去重窗口的同一条栈**另算一次**（不抹掉两次独立崩溃之间的时间事实）",
          late.get("deduped") is False and late["reportId"] != first["reportId"], str(late)[:200])
    other = crt.record("worker_thread", exc_type="ValueError", exc_text="同一条栈",
                       traceback_text=tb_text, at_ms=base + 2000)
    check("类别不同就不合并（主线程崩与工作线程崩是两条事实）", other.get("deduped") is False, str(other)[:160])
    unknown = crt.record("完全没听过的类别", exc_type="E", exc_text="x", traceback_text="tb " + str(time.time_ns()))
    check("跑出版表外的类别归 unknown 并保留原声明",
          unknown.get("ok") is True and crt.detail(unknown["reportId"])["category"] == "unknown"
          and crt.detail(unknown["reportId"])["categoryDeclared"] == "完全没听过的类别")
    for probe in ("../x", "a/b"):
        check(f"delete 拒绝越界编号「{probe}」", crt.delete(probe)["ok"] is False)
    for item in (first, late, other, unknown):
        crt.delete(item["reportId"])

    made = []
    for index in range(crt.MAX_REPORTS + 6):
        got = scene("manual", text=f"填到第 {index} 条", at_ms=base + index)
        made.append(got)
    # 每次写盘只清掉「这一次多出来的那一份」，所以单次回话里的 droppedByCap 是 1，
    # 累计才是 6——把 6 写在单次上是用错了口径，不是代码错了。
    check("越上限之后每一次写盘都如实报丢了一份", sum(m["droppedByCap"] for m in made) == 6,
          str([m["droppedByCap"] for m in made][-8:]))
    check("没越上限的那些次不谎报丢弃", sum(m["droppedByCap"] for m in made[:crt.MAX_REPORTS]) == 0)
    after = crt.list_reports(500)
    check("盘上条数正好停在上限", after["count"] == crt.MAX_REPORTS, str(after["count"]))
    survivors = {r["reportId"] for r in after["reports"]}
    check("丢的是最旧的（编号带毫秒，文件名序就是时间序）",
          not (survivors & {made[index]["reportId"] for index in range(6)}),
          f"最旧 6 份里还在盘上的：{sorted(survivors & {m['reportId'] for m in made[:6]})[:3]}")
    check("留下的正好是最后 50 份", {m["reportId"] for m in made[6:]} == {r["reportId"] for r in after["reports"]})
    for row in after["reports"]:
        crt.delete(row["reportId"])

    ctx = crt.record("manual", exc_type="E", exc_text="x", traceback_text="上下文用例 " + str(time.time_ns()),
                     context={"engineVersion": "0.3.0", "taskId": {"不是": "纯量"}, "lane": "主线程",
                              "endpoint": f"/api/chat?token={FAKE_MODEL_KEY}", "provider": f"{Path.home()}/私密",
                              "prompt": "绝对不该进来的正文", "filePath": "C:/私密/x.txt",
                              "nested": {"a": 1}, "big": 10})
    saved = crt.detail(ctx["reportId"])
    dumped = json.dumps(saved["context"], ensure_ascii=False)
    check("上下文只留白名单里的五个键 + 标量",
          {"engineVersion", "taskId", "lane", "endpoint", "provider"} >= set(saved["context"]) - {"_droppedKeys"},
          str(saved["context"])[:200])
    check("白名单外的键被丢掉并留下痕迹（不是静默丢）",
          "prompt" in json.dumps(saved["context"], ensure_ascii=False), str(saved["context"])[:200])
    check("非纯量值也算丢掉的键", any("(非纯量)" in x for x in saved["context"].get("_droppedKeys", [])), str(saved["context"]["_droppedKeys"]))
    check("白名单内的字符串值同样过脱敏：密钥形状进不来",
          FAKE_MODEL_KEY not in dumped and "***" in dumped, dumped[:200])
    check("被打掉的是值不是整条：接口路径本身还在（现场还得看得出撞在哪个端点）",
          "/api/chat" in dumped, dumped[:200])
    check("上下文里的家目录换成占位符（现场不给读出机器上的真实路径）",
          str(Path.home()) not in dumped and "私密" in dumped, dumped[:200])
    check("被丢掉的键按名字列出来，界面能回显「这里我没给你看」",
          {"prompt", "filePath", "nested", "big"} <= set(saved["context"]["_droppedKeys"]),
          str(saved["context"]["_droppedKeys"])[:200])
    crt.delete(ctx["reportId"])


# ── 5. 导出与删除 ───────────────────────────────────────────────


def export_checks() -> None:
    section("5. 导出是副本、原件不删、标上导出时间；没有现场时如实拒绝")
    crt.reset_for_tests()
    check("没有现场时导出如实拒绝（不产一个空文件冒充成功）",
          crt.export("*")["ok"] is False or crt.list_reports(1)["count"] > 0)
    one = scene("manual", text="要导出的现场")
    two = scene("worker_thread", text="另一份现场")
    got = crt.export("*")
    check("导出成功回路径、字节数与指纹", got.get("ok") is True and Path(got["path"]).exists()
          and got["bytes"] > 0 and len(got["sha256"]) == 16, str(got)[:220])
    text = Path(got["path"]).read_text(encoding="utf-8")
    check("导出物里写明「这是一份改过的现场」", "这" in text and "已经在写盘时打掉" in text.replace("已在写盘时", "已经在写盘时"), text[:120])
    check("导出物含两条的编号与栈正文", one["reportId"] in text and two["reportId"] in text)
    check("导出**不删原件**（删是另一条显式动作）",
          Path(one["path"]).exists() and Path(two["path"]).exists())
    check("导出的那两条都落上 exportedAtMs",
          crt.detail(one["reportId"])["exportedAtMs"] > 0 and crt.detail(two["reportId"])["exportedAtMs"] > 0)
    single = crt.export(one["reportId"])
    check("按编号只导那一条", single["count"] == 1 and single["reports"] == [one["reportId"]], str(single)[:200])
    check("导出目录里确实有多份（导出不是覆盖同一个文件）",
          len(list(crt.exports_dir().glob("crash-export-*.txt"))) >= 2)
    gone = crt.delete(one["reportId"])
    check("删除回 removed=true 且文件真没了", gone["ok"] is True and not Path(one["path"]).exists())
    check("再删一次如实说已经删过", "删过" in crt.delete(one["reportId"]).get("reason", ""), str(crt.delete(one["reportId"]))[:160])
    st = crt.status()
    check("已经导出过的不再算进待导出（导过一次还催第二次是噪音）",
          st["count"] == 1 and st["pendingExport"] == 0, str(st)[:200])
    fresh = scene("manual", text="还没导出的现场")
    st = crt.status()
    check("状态面给出待导出条数（界面据此提示导出）",
          st["pendingExport"] == 1 and "还没导出" in st["note"], str(st)[:200])
    crt.delete(fresh["reportId"])
    crt.delete(two["reportId"])
    for f in crt.exports_dir().glob("crash-export-*.txt"):
        f.unlink()


# ── 6. 挂钩与观察者（永不抛是这一节的存在理由）──────────────────


def hook_checks() -> None:
    section("6. 三个钩子都接得上；钩子自己炸了也不能把现场弄丢")
    crt.reset_for_tests()
    sentinel_calls: list[tuple] = []

    def previous_hook(*args):
        sentinel_calls.append(args)

    original = sys.excepthook
    sys.excepthook = previous_hook
    try:
        first = crt.install(engine_version="0.3.0")
        second = crt.install(engine_version="0.3.0", loop=None)
        check("install 回装上了哪几个钩子", set(first["installed"]) == {"sys.excepthook", "threading.excepthook"}, str(first)[:200])
        check("主线程钩子真的换上了", sys.excepthook is crt._main_hook)
        check("工作线程钩子真的换上了", threading.excepthook is crt._thread_hook)
        check("重复 install 不把上一次接管的钩子当成「原始钩子」（接管不等于套娃）",
              crt._STATE["previous"]["excepthook"] is previous_hook and second["ok"], str(crt._STATE["previous"])[:120])
        et, ev, tb = raise_demo("主线程未捕获")
        crt._main_hook(et, ev, tb)
        check("未捕获异常落成一份现场", crt.list_reports(50)["count"] >= 1, str(crt.list_reports(50)["count"]))
        check("异常仍交给原来的钩子（接管不等于吞掉）", len(sentinel_calls) == 1 and sentinel_calls[0][0] is et, str(sentinel_calls)[:160])

        class FakeLoop:
            def __init__(self):
                self.handler = None
                self.error = None

            def set_exception_handler(self, handler):
                self.handler = handler

        loop = FakeLoop()
        third = crt.install(engine_version="0.3.0", loop=loop)
        check("拿到循环对象时补装循环钩子", "loop.set_exception_handler" in third["installed"] and third["loopHook"], str(third)[:200])
        check("循环钩子确实交到那个循环上", isinstance(loop.handler, type(crt.loop_exception_handler)) or loop.handler is crt.loop_exception_handler)
        et2, ev2, tb2 = raise_demo("循环回调里抛")
        loop.handler(loop, {"message": "Unhandled error in callback", "exception": ev2})
        check("循环回调异常落成 event_loop 类别",
              any(x["category"] == "event_loop" for x in crt.list_reports(50)["reports"]), str([x["category"] for x in crt.list_reports(50)["reports"]]))
        detail = crt.detail(next(x["reportId"] for x in crt.list_reports(50)["reports"] if x["category"] == "event_loop"))
        check("循环自己那句话走 note 而不是塞进上下文白名单",
              "Unhandled error" in detail["note"] and "message" not in detail["context"], str(detail["note"])[:120])

        class Boom:
            def set_exception_handler(self, handler):
                raise OSError("循环不接")

        broken = crt.install(engine_version="0.3.0", loop=Boom())
        check("循环拒绝挂钩时如实说并继续跑（不抛）",
              broken["ok"] is True and "循环不接" in broken["loopError"] and broken["loopHook"] is False, str(broken)[:200])

        # 观察者：新落一份才通知；去重那次不通知；观察者炸了不影响留存
        # 两条必须用**同一份 traceback**——去重按栈指纹，各造一条新栈就等于没测去重。
        seen: list[dict] = []
        check("set_recorder 拒绝不可调用对象", crt.set_recorder("不是函数")["ok"] is False)
        crt.set_recorder(lambda event: seen.append(event))
        obs_tb = "观察者用例 " + str(time.time_ns())
        new_one = crt.record("manual", exc_type="E", exc_text="观察者", traceback_text=obs_tb)
        dup = crt.record("manual", exc_type="E", exc_text="观察者", traceback_text=obs_tb)
        check("新落一份现场就通知观察者一次", len(seen) == 1 and seen[0]["reportId"] == new_one["reportId"], str(seen)[:200])
        check("去重合并那一次不再通知（去重的意义就是别再吵一遍）",
                len(seen) == 1 and dup.get("deduped") is True, f"{len(seen)} / {dup}")
        crt.set_recorder(lambda event: (_ for _ in ()).throw(RuntimeError("观察者炸了")))
        boom = crt.record("manual", exc_type="E", exc_text="观察者炸", traceback_text="观察者炸 " + str(time.time_ns()))
        check("观察者抛异常不影响现场落盘", boom.get("ok") is True, str(boom)[:160])
        check("观察者抛异常被如实记进读数（不是静默吞）",
              "观察者炸了" in crt.recorder_state()["error"], str(crt.recorder_state())[:200])
        crt.set_recorder(None)
        check("撤掉观察者后不再计数", crt.recorder_state()["registered"] is False)

        # handle_exception 面对荒唐输入也不能抛
        junk = crt.handle_exception("uncaught_exception", None, None, None)
        check("空异常对象也能落一份现场（不抛）", junk.get("ok") is True or "reason" in junk, str(junk)[:160])
        junk2 = crt.handle_exception("manual", int, 3, "不是 traceback")
        check("参数类型完全不对时也只是回话，不抛", isinstance(junk2, dict), str(junk2)[:120])
        if junk.get("ok"):
            crt.delete(junk["reportId"])
        if junk2.get("ok"):
            crt.delete(junk2["reportId"])
        for row in crt.list_reports(200)["reports"]:
            crt.delete(row["reportId"])
    finally:
        sys.excepthook = original
        threading.excepthook = _ORIGINAL_THREAD_HOOK
        crt.reset_for_tests()


# ── 7. 遥测：默认关、确认词、退出面 ────────────────────────────


def consent_checks() -> None:
    section("7. 默认关就是关：没同意一个字节也不写；开要确认词，关永远不设门槛")
    tlm.reset_for_tests()
    st0 = tlm.consent_state()
    check("本机没有同意文件 = 关闭", st0["on"] is False and st0["source"] == "absent", str(st0)[:160])
    refused = tlm.set_consent(True, via="自测")
    check("不带确认词的开启被拒", refused["ok"] is False and refused["changed"] is False)
    check("拒绝理由点名要哪个确认词", tlm.CONFIRM_WORD in refused["reason"], refused["reason"][:120])
    check("被拒时状态没动（不是先开再退）", tlm.consent_state()["on"] is False)
    check("被拒的开启不写缓冲（连试都没试）", not tlm.events_file().exists())
    wrong = tlm.set_consent(True, word="同意", via="自测")
    check("中文「同意」不算确认词（要的就是那几个字）", wrong["ok"] is False, str(wrong)[:120])
    check("确认词按大小写与空格归一（agree / AGREE / 都算）",
          tlm.set_consent(True, word=" agree ", via="自测")["ok"] is True)
    on = tlm.consent_state()
    check("开好了：on、时间、来源与安装标识齐全",
          on["on"] is True and on["atMs"] > 0 and on["source"] == "file" and len(on["installationId"]) == 32, str(on)[:200])
    check("同意文件写明「确认词给过了」这个事实，但**不记确认词本身**",
          json.loads(tlm.consent_file().read_text(encoding="utf-8"))["confirmWordSeen"] is True
          and "AGREE" not in tlm.consent_file().read_text(encoding="utf-8").replace('"confirmWordSeen": true', ""))
    off = tlm.set_consent(False, via="自测")
    check("关闭**不需要确认词**（退出面不许比进入面难走）", off["ok"] is True and off["on"] is False, str(off)[:160])
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    tlm.record("engine_started", {"engineVersion": "0.3.0", "osFamily": "Windows", "pythonFamily": "cpython"})
    check("开启后确实缓冲了一条", tlm.pending_count() == 1, str(tlm.pending_count()))
    out = tlm.set_consent(False, via="自测")
    check("关闭当场把本地缓冲整份删掉（不留「先收了再说」）",
          out["eventsRemoved"] == 1 and not tlm.events_file().exists() and out["bufferDeleteFailed"] is False, str(out)[:200])
    again = tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    check("重新开启会换一个新的安装标识（关闭前后关联不到同一个安装）",
          again["installationId"] != on["installationId"], f"{again['installationId']} vs {on['installationId']}")
    tlm.set_consent(False, via="自测")
    keys = {
        "absent": set(tlm.consent_state()),
    }
    tlm.consent_file().parent.mkdir(parents=True, exist_ok=True)
    tlm.consent_file().write_text("{坏了", encoding="utf-8")
    corrupt = tlm.consent_state()
    keys["corrupt"] = set(corrupt)
    check("同意文件坏掉 = 关闭（不猜上次是什么档）", corrupt["on"] is False and corrupt["source"] == "corrupt", str(corrupt)[:160])
    check("坏文件改名让路（不静默吃掉一次读失败）",
          corrupt["movedTo"] != tlm.consent_file().name and (tlm.consent_file().parent / corrupt["movedTo"]).exists(), str(corrupt)[:200])
    # 这条要**合法 JSON 但不是对象**（数组）：写「[不是对象]」根本读不出来，
    # 走的是 corrupt 分支，于是 invalid 这一态从来没被真的测到过。
    tlm.consent_file().write_text("[1,2]", encoding="utf-8")
    keys["invalid"] = set(tlm.consent_state())
    check("同意文件不是对象时也算 invalid 三态之一", tlm.consent_state()["source"] == "invalid")
    check("四个分支的键集合一致（少一个键就是 status() 当场 KeyError——本卡真踩过这一下）",
          keys["absent"] == keys["corrupt"] == keys["invalid"] == set(tlm.consent_state()), str({k: sorted(v - keys["absent"]) for k, v in keys.items()})[:220])
    tlm.consent_file().unlink(missing_ok=True)
    audit = tlm.decisions(50)
    kinds = [x["action"] for x in audit["decisions"]]
    check("同意侧每一步都落本机审计（含被拒的尝试）",
          {"opt_in_refused", "opt_in", "opt_out"} <= set(kinds), str(kinds)[:200])
    check("审计里那条被拒的记录说了原因", any(x.get("reason") for x in audit["decisions"]))
    check("审计不在缓冲里（它是本机治理记录，不是外发事件）",
          "decisions.jsonl" in str(tlm.decisions_file().name) and tlm.decisions_file() != tlm.events_file())


# ── 8. 字段白名单、词表与缓冲 ──────────────────────────────────


def field_checks() -> None:
    section("8. 白名单就是白名单：不在表里的进不来，词表外的值也进不来")
    tlm.reset_for_tests()
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    tlm.discard_all()
    check("每张表都只覆盖登记过的类别", set(tlm.ALLOWED_FIELDS) == set(tlm.KINDS), str(sorted(tlm.ALLOWED_FIELDS)))
    dirty = tlm.record("task_finished", {
        "endReason": "completed", "durationMs": 10, "hadError": False,
        "prompt": "用户整段提示词", "filePath": "C:/Users/admin/私密.docx",
        "modelId": "qwen-max", "messages": [{"role": "user"}], "engineVersion": "0.3.0",
    })
    check("白名单外的键整条丢掉并如实列出",
          {"prompt", "filePath", "modelId", "messages"} <= set(dirty["droppedKeys"]), str(dirty["droppedKeys"]))
    kept = tlm.preview(10)["payload"][-1]["fields"]
    check("正文与路径与模型名**没有**进缓冲（不是打了码进去，是没进去）",
          "prompt" not in kept and "filePath" not in kept and "modelId" not in kept
          and "私密" not in json.dumps(kept, ensure_ascii=False), str(kept)[:200])
    check("白名单内的字段照常保留", kept["endReason"] == "completed" and kept["durationMs"] == 10)
    vocab = tlm.record("task_finished", {"endReason": "莫名其妙", "durationMs": 1})
    check("词表外的取值被丢掉（endReason 不是任意字符串的容身处）",
          vocab["offVocab"] and "endReason" in vocab["offVocab"][0], str(vocab)[:200])
    check("词表与 server 的四处赋值逐字对上",
          set(tlm.FIELD_VOCAB[("task_finished", "endReason")]) == {"completed", "cancelled", "privacy_blocked", "error"})
    src = SERVER_SRC.read_text(encoding="utf-8")
    emitted = set(re.findall(r'end_reason = "([a-z_]+)"', src))
    check("server 里真实出现过的 end_reason 取值全都在词表里（词表不超前也不落后）",
          emitted and emitted <= set(tlm.FIELD_VOCAB[("task_finished", "endReason")]), str(sorted(emitted)))
    crash_vocab = set(tlm.FIELD_VOCAB[("crash_recorded", "category")])
    check("崩溃类别词表直接从 crash_runtime 取，没在这里抄第二份",
          crash_vocab == set(crt.CATEGORIES) | {crt.CATEGORY_UNKNOWN}, str(sorted(crash_vocab)))
    check("类别表与词表都不含敏感词（不写「暂时没列」的字段）",
          not [kind for kind in tlm.KINDS if any(word in kind.lower() for word in FORBIDDEN_FIELD_WORDS)])
    leaked = [f"{kind}.{field}" for kind, fields in tlm.ALLOWED_FIELDS.items() for field in fields
              if _forbidden_field(field)]
    check("逐类字段白名单里没有任何「正文/路径/命令/模型名/密钥」类字段", leaked == [], str(leaked))
    check("词表扫描不是「见词就放过」：只有纯计数字段才豁免，模型名那类照样拦",
          _forbidden_field("modelId") and _forbidden_field("promptText")
          and not _forbidden_field("modelCallCount") and not _forbidden_field("toolCallCount"),
          "豁免口子开大了")
    scalar = tlm.record("task_finished", {"endReason": "error", "durationMs": {"不是": "纯量"}, "hadError": True})
    check("非标量值算丢掉的那种（不是序列化成字符串塞进去）",
          any("(非纯量)" in x for x in scalar["droppedKeys"]), str(scalar["droppedKeys"]))
    check("unknown 类别不落账（类别表是唯一来源）",
          tlm.record("nonsense", {"a": 1})["ok"] is False and tlm.pending_count() == len(tlm.preview(50)["payload"]))
    priv = tlm.record("task_finished", {"endReason": "completed"}, privacy=True)
    check("隐私轮**连计数都不记**（隐私档的语义没有遥测例外）",
          priv["buffered"] is False and "隐私" in priv["reason"], str(priv)[:160])
    check("本机没同意时也照样不记（关闭态与隐私态是两条独立的门）",
          tlm.set_consent(False, via="自测")["ok"] is True
          and tlm.record("task_finished", {"endReason": "completed"})["buffered"] is False)
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    tlm.discard_all()
    limit_backup = tlm.MAX_BUFFERED_EVENTS
    try:
        tlm.MAX_BUFFERED_EVENTS = 4
        for index in range(7):
            tlm.record("task_finished", {"endReason": "completed", "durationMs": index})
        trimmed = tlm._read_events()
        check("缓冲超上限时丢最旧的（不是静默覆盖也不是无限长）",
              len(trimmed) == 4 and trimmed[0]["fields"]["durationMs"] == 3, str([x["fields"]["durationMs"] for x in trimmed]))
        check("丢掉的条数进计数器（有账可查）", tlm.read_state()["droppedByCap"] == 3, str(tlm.read_state())[:160])
    finally:
        tlm.MAX_BUFFERED_EVENTS = limit_backup
    tlm.discard_all()
    check("坏行只丢那一行，不整份作废", (
        tlm.record("engine_started", {"engineVersion": "0.3.0"}),
        tlm.events_file().open("a", encoding="utf-8").write("这不是 JSON\n"),
        tlm.record("engine_started", {"engineVersion": "0.3.1"}),
    ) and tlm.preview(10)["badLines"] == 1 and tlm.preview(10)["count"] == 2)
    check("预览里不含那条坏行", all(x["kind"] for x in tlm.preview(10)["payload"]))
    tlm.discard_all()


# ── 9. 发与不发（本模块存在的意义）─────────────────────────────


def egress_checks() -> None:
    section("9. 没有出口就不许说「已发送」；出口没收下就一条也不删")
    tlm.reset_for_tests()
    tlm.discard_all()
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    for index in range(3):
        tlm.record("task_finished", {"endReason": "completed", "durationMs": 100 + index})
    got = tlm.flush()
    check("没登记出口时如实回未发送", got["ok"] is True and got["sent"] == 0 and got["delivered"] is False, str(got)[:200])
    check("未发送时一条也不删（删了就没有可核对的东西了）", got["remaining"] == 3 and tlm.pending_count() == 3, str(got)[:160])
    check("未发送的回话里带着将要发的那份东西（让人先看清再决定）", len(got["preview"]) == 3)
    check("这一句「没有出口」写进理由（不是空 reason 让界面猜）", "没有出口" in got["reason"], got["reason"][:120])
    check("计数器说清这次没发（lastAttemptReason 有字）",
          "没有出口" in tlm.read_state()["lastAttemptReason"] and tlm.read_state()["flushedEvents"] == 0, str(tlm.read_state())[:200])
    check("configure_sink 拒绝不可调用对象", tlm.configure_sink("不是函数")["ok"] is False)
    calls: list[list] = []

    def refuse(rows):
        calls.append(list(rows))
        return {"ok": False, "reason": "服务端说这批次不收"}

    tlm.configure_sink(refuse, label="自测拒绝出口")
    rejected = tlm.flush()
    check("出口拒绝时如实报失败并说原因", rejected["ok"] is False and "服务端说这批次不收" in rejected["reason"], str(rejected)[:200])
    check("出口没收下就一条也不动", tlm.pending_count() == 3, str(tlm.pending_count()))
    check("拒绝也进计数器理由（不是静默失败）", "出口拒绝" in tlm.read_state()["lastAttemptReason"], str(tlm.read_state())[:200])

    def boom(rows):
        raise OSError("出口炸了")

    tlm.configure_sink(boom, label="自测崩溃出口")
    crashed = tlm.flush()
    check("出口抛异常不弄坏引擎（回话而不是往上抛）", crashed["ok"] is False and "出口调用失败" in crashed["reason"], str(crashed)[:200])
    check("出口炸了同样一条不删", tlm.pending_count() == 3)

    rows_seen: list[list] = []

    def accept(rows):
        rows_seen.append(list(rows))
        return {"ok": True}

    tlm.configure_sink(accept, label="自测出口")
    # 预览要在 flush **之前**取：flush 之后缓冲只剩一条，拿事后的 preview 比对
    # 等于用「发完剩下的」去核对「发出去的」，永远对不上（也永远查不出顺序反了）。
    shown = tlm.preview(3)["payload"]
    one = tlm.flush(limit=2)
    check("出口确认后按批次移除（limit 生效）", one["sent"] == 2 and one["delivered"] is True and tlm.pending_count() == 1, str(one)[:200])
    check("发出去的就是预览里的那份数组（同一构造代码，界面不可能显示温和版）",
          rows_seen[0] == shown[:2], f"出口收到 {rows_seen[0][:1]}；预览给的是 {shown[:1]}")
    check("先送最旧的（先进先出；只发最近几条会让最旧的那条永远烂在缓冲里）",
          [x["eventId"] for x in rows_seen[0]] == [x["eventId"] for x in shown[:2]]
          and shown[0]["fields"]["durationMs"] == 100
          and shown[1]["fields"]["durationMs"] == 101, str(shown)[:200])
    rest = tlm.flush()
    check("剩下的那条下一次发走", rest["sent"] == 1 and tlm.pending_count() == 0, str(rest)[:160])
    check("发走的条数进计数器", tlm.read_state()["flushedEvents"] == 3, str(tlm.read_state())[:160])
    check("空缓冲 flush 如实回「没有要发的」", tlm.flush()["reason"].count("空的") == 1, str(tlm.flush())[:160])
    check("关掉遥测后 flush 直接拒绝（不再拿旧出口发东西）",
          tlm.set_consent(False, via="自测")["ok"] is True and tlm.flush()["sent"] == 0 and "未开启" in tlm.flush()["reason"])
    check("预览在未开启时说明这只是「如果开启会看到什么」",
          "如果开启" in tlm.preview(5)["note"] and tlm.preview(5)["enabled"] is False)
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    tlm.record("crash_recorded", {"category": "manual", "pendingReports": 2, "engineVersion": "0.3.0"})
    dis = tlm.discard_all()
    check("discard 只清缓冲、不动同意状态", dis["removed"] == 1 and tlm.consent_state()["on"] is True, str(dis)[:160])
    check("空缓冲 discard 如实说本来就是空的", "空" in tlm.discard_all()["reason"], str(tlm.discard_all())[:160])
    check("未开启时 discard 仍能用（这是退出面，不设档位门槛）",
          tlm.set_consent(False, via="自测")["ok"] is True and tlm.discard_all()["ok"] is True)
    tlm.reset_for_tests()


# ── 10. 装配现场（server.py 的那三件只在引擎里才存在的事）──────


def wiring_checks() -> None:
    section("10. 装配现场：钩子、请求级异常、收尾计数与观察者的位置")
    src = SERVER_SRC.read_text(encoding="utf-8")
    check("启动路径先装主线程与工作线程钩子（uvicorn.run 之前，炸在启动阶段也留现场）",
          "_crash_telemetry_bootstrap()" in src and src.index("_crash_telemetry_bootstrap()") < src.index("uvicorn.run(app"))
    check("lifespan 里补装循环钩子（只有那里拿得到循环对象）",
          "_crash_telemetry_bootstrap(asyncio.get_running_loop())" in src)
    check("装配函数幂等且自己不抛（bootstrap 里必有一层兜底）",
          "def _crash_telemetry_bootstrap" in src and src.split("def _crash_telemetry_bootstrap")[1].split("def _telemetry_task_finished")[0].count("except Exception") >= 2)
    check("注册了请求级未捕获异常处理器（这类异常今天的下场是被 uvicorn 印进没有目的地的 stderr）",
          '@app.exception_handler(Exception)' in src)
    handler = src.split("@app.exception_handler(Exception)")[1].split("# CORS")[0]
    check("处理器把现场编号回给人（界面才能写「这次崩了记为 X」）", "crashReport" in handler)
    # 只扫**响应体那一段**：函数自己的说明里必然出现 traceback / exc.__traceback__
    # 这些词（它正是在解释「为什么不进响应」），拿整段源码做子串扫描等于把注释读成载荷，
    # 既会误报，也护不住真正要护的东西——回给人那一行字典。
    resp_block = handler.split("body: dict[str, Any] = {")[-1].split("return JSONResponse")[0]
    check("处理器不回栈正文与异常原文（本机才有详情）",
          "body: dict[str, Any] = {" in handler and resp_block
          and "traceback" not in resp_block and "exc_text" not in resp_block and "str(exc)" not in resp_block,
          resp_block[:200])
    check("响应体只有那五个键（多一个就是往远程调用方身上多塞一份本机信息）",
          set(re.findall(r'^\s+"([a-zA-Z]+)":', resp_block, re.M)) == {"ok", "code", "message", "crashReport", "deduped"},
          str(sorted(set(re.findall(r'^\s+"([a-zA-Z]+)":', resp_block, re.M)))))
    check("业务性失败不进崩溃族（处理器只接 Exception，不接 HTTPException）",
          "exception_handler(HTTPException)" not in src)
    check("收尾计数发生在 done 之后（事件流一个字节也不改）",
          src.index('_safe_put(queue, "done", {})') < src.index("_telemetry_task_finished(session, end_reason, checkpoint)"))
    body = src.split("def _telemetry_task_finished")[1].split("\n# ── FastAPI")[0]
    check("隐私轮走 record(privacy=) 而不是自己判档位", "privacy=bool(getattr(session, \"privacy_mode\"" in body)
    check("计数字段取自 checkpoint.last_summary（不另起一套计数器）",
          'summary.get("modelCalls")' in body and 'summary.get("executedTools")' in body)
    check("续跑事实取自 checkpoint.resumable", "checkpoint.resumable" in body)
    check("崩溃观察者在装配现场登记（crash_runtime 本身不认识 telemetry_runtime）",
          "crt.set_recorder(_crash_observer)" in src and "import telemetry_runtime" not in CRASH_SRC.read_text(encoding="utf-8"))
    check("引擎侧没有任何 routes_* 文件调 configure_sink（出口只能进程内登记）",
          not [p for p in (PROJECT_ROOT / "engine").glob("routes_*.py")
               if "configure_sink(" in p.read_text(encoding="utf-8").replace("def configure_sink(", "def _x(")])
    check("configure_sink 在本仓库只被定义处、自测与说明文字提到",
          sum(1 for p in (PROJECT_ROOT / "engine").glob("*.py")
              if "configure_sink" in p.read_text(encoding="utf-8")) <= 2,
          str([p.name for p in (PROJECT_ROOT / "engine").glob("*.py") if "configure_sink" in p.read_text(encoding="utf-8")]))

    # 真跑一遍：装配现场的两个函数在开着的档下确实产出事件
    tlm.reset_for_tests()
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="自测")
    tlm.discard_all()
    boot = server._crash_telemetry_bootstrap()
    check("bootstrap 回话说明装上了钩子与观察者", boot.get("ok") is True and boot["recorder"]["registered"] is True, str(boot)[:200])
    check("起一轮就记一条 engine_started", [x["kind"] for x in tlm.preview(10)["payload"]] == ["engine_started"], str(tlm.preview(10)["payload"])[:200])
    session = types.SimpleNamespace(created_at=time.time() - 2.0, secure_env=False, privacy_mode=False)
    checkpoint = types.SimpleNamespace(resumable=True, last_summary={"modelCalls": 7, "executedTools": 3})
    done = server._telemetry_task_finished(session, "cancelled", checkpoint)
    check("收尾记一条 task_finished", done["ok"] is True and done["buffered"] is True, str(done)[:160])
    fields = tlm.preview(10)["payload"][-1]["fields"]
    check("时长、调用数与续跑事实都对得上（字段不是摆设）",
          fields["endReason"] == "cancelled" and fields["modelCallCount"] == 7
          and fields["toolCallCount"] == 3 and fields["checkpointResumable"] is True
          and 1500 <= fields["durationMs"] <= 60000, str(fields)[:200])
    privacy = server._telemetry_task_finished(
        types.SimpleNamespace(created_at=time.time(), secure_env=False, privacy_mode=True), "error", checkpoint)
    check("隐私轮那条计数根本不记", privacy["buffered"] is False and tlm.pending_count() == 2, str(privacy)[:160])
    check("崩一次就转成一条 crash_recorded 计数",
          crt.record("manual", exc_type="E", exc_text="装配", traceback_text="装配用例 " + str(time.time_ns()))["ok"] is True
          and [x["kind"] for x in tlm.preview(10)["payload"]][-1] == "crash_recorded")
    crash_row = tlm.preview(10)["payload"][-1]["fields"]
    check("计数事件只带类别与待导条数，不带任何正文", set(crash_row) == {"engineVersion", "category", "pendingReports"}, str(crash_row)[:200])
    tlm.set_consent(False, via="自测")
    tlm.reset_for_tests()


# ── 11. 端点面 ─────────────────────────────────────────────────


def endpoint_checks(client) -> None:
    section("11. 端点面：整族只对桌面壳开放、GET/POST 分工、出口不接受设定、拒绝整批删除")
    app_paths = {getattr(route, "path", "") for route in server.app.routes}
    missing = [p for p in rx.EXEC_ENDPOINT_PATHS if p not in app_paths]
    check("登记在清单里的每条路径都真的挂上了（清单是唯一来源）", missing == [], str(missing))
    new = [p for p in rx.EXEC_ENDPOINT_PATHS if p.startswith(("/api/exec/telemetry/", "/api/exec/crash/"))]
    check("本卡新增 13 条端点（遥测 7 + 崩溃 6）", len(new) == 13, str(len(new)))
    check("两族都落在 exec 车道前缀里（不占用别人的段）",
          all(any(p.startswith(prefix) for prefix in ("/api/exec",)) for p in new), str(new[:3]))
    check("遥测族与崩溃族的路径都在 /api/exec/ 下（不新开冒名前缀）",
          {p.split("/")[2] for p in new} == {"exec"})

    remote_paths = list(new)
    blocked_codes = {"telemetry": set(), "crash": set()}
    for path in remote_paths:
        verb = client.post if any(path.endswith(x) for x in ("/consent", "/flush", "/discard", "/export", "/delete")) else client.get
        resp = verb(path, headers=headers(STATIC_TOKEN), json={}) if verb is client.post else verb(path, headers=headers(STATIC_TOKEN))
        check(f"远程身份 {path} 一律 403", resp.status_code == 403, str(resp.status_code))
        body = resp.json()
        check(f"{path} 的拒绝理由说了整族不开放", "整族不开放" in body.get("message", ""), str(body)[:160])
        blocked_codes["telemetry" if "telemetry" in path else "crash"].add(body.get("code", ""))
    check("遥测族的码是 TELEMETRY_ 前缀", blocked_codes["telemetry"] == {"TELEMETRY_DESKTOP_ONLY"}, str(blocked_codes["telemetry"]))
    check("崩溃族的码是 CRASH_ 前缀", blocked_codes["crash"] == {"CRASH_DESKTOP_ONLY"}, str(blocked_codes["crash"]))

    read_only = [p for p in new if p.endswith(("/describe", "/status", "/preview", "/decisions", "/list", "/detail"))]
    for path in read_only:
        check(f"只读端点 {path} 不接受 POST", client.post(path, headers=headers(), json={}).status_code == 405, path)
    for path in [p for p in new if p.endswith(("/consent", "/flush", "/discard", "/export", "/delete"))]:
        check(f"会改状态的 {path} 不接受 GET", client.get(path, headers=headers()).status_code == 405, path)

    describe = client.get("/api/exec/telemetry/describe", headers=headers())
    check("describe 一次就把口径摊全（界面不必猜）", describe.status_code == 200
          and describe.json()["defaultOn"] is False and describe.json()["confirmWord"] == tlm.CONFIRM_WORD, str(describe.json())[:200])
    SEEN_KEYS["describe"] = set(describe.json())
    status = client.get("/api/exec/telemetry/status", headers=headers())
    check("缺省状态就是关闭且缓冲 0 条", status.json()["enabled"] is False and status.json()["buffered"] == 0, str(status.json())[:200])
    SEEN_KEYS["status"] = set(status.json())
    check("状态面把安装标识只给后 8 位", len(status.json()["installationIdTail"]) <= 8, str(status.json())[:160])

    egress = client.post("/api/exec/telemetry/consent", headers=headers(),
                         json={"on": True, "word": tlm.CONFIRM_WORD, "endpoint": "https://collect.example.com"})
    check("带 endpoint 的请求被整条拒绝（不静默丢掉那个键）",
          egress.status_code == 400 and egress.json()["code"] == "TELEMETRY_EGRESS_NOT_SETTABLE", str(egress.json())[:200])
    check("拒绝理由说要的只有 on 与 word", "on" in egress.json()["message"] and "word" in egress.json()["message"], str(egress.json())[:200])
    SEEN_KEYS["egress_refused"] = set(egress.json())
    check("被拒的这次没改档位", client.get("/api/exec/telemetry/status", headers=headers()).json()["enabled"] is False)
    for key in ("sink", "destination", "target", "url", "events", "fields"):
        probe = client.post("/api/exec/telemetry/consent", headers=headers(), json={"on": True, "word": tlm.CONFIRM_WORD, key: 1})
        check(f"键「{key}」同样整条拒绝", probe.status_code == 400, str(probe.status_code))
    no_param = client.post("/api/exec/telemetry/consent", headers=headers(), json={})
    check("不带 on 的同意请求按缺参拒绝（回 400，不是 500）", no_param.status_code == 400, str(no_param.status_code))
    bad_json = client.post("/api/exec/telemetry/consent", headers=headers(), content=b"not json")
    check("请求体不是 JSON 时按缺参拒绝", bad_json.status_code == 400, str(bad_json.status_code))

    refused = client.post("/api/exec/telemetry/consent", headers=headers(), json={"on": True, "word": "不对"})
    check("确认词不对走 200 + ok=false（是流程内的结果，不是服务故障）",
          refused.status_code == 200 and refused.json()["ok"] is False, str(refused.json())[:200])
    check("回话里带中文 reason 与「什么都没改」", refused.json()["reason"] and refused.json()["changed"] is False, str(refused.json())[:200])
    SEEN_KEYS["consent_refused"] = set(refused.json())

    opened = client.post("/api/exec/telemetry/consent", headers=headers(), json={"on": True, "word": tlm.CONFIRM_WORD, "via": "自测端点"})
    check("带确认词的开启成功并回安装标识", opened.json()["ok"] is True and len(opened.json()["installationId"]) == 32, str(opened.json())[:200])
    SEEN_KEYS["consent_on"] = set(opened.json())
    check("回话说明了记的是哪几类、且没有出口时仍只缓冲", "只记" in opened.json()["note"] and "未发送" in opened.json()["note"], str(opened.json())[:220])
    tlm.record("task_finished", {"endReason": "completed", "durationMs": 12})
    preview = client.get("/api/exec/telemetry/preview", headers=headers())
    check("预览给出的就是将要发出去的数组", preview.json()["count"] >= 1 and "fields" in preview.json()["payload"][-1], str(preview.json())[:220])
    SEEN_KEYS["preview"] = set(preview.json())
    check("预览也摊开字段白名单（不用再去别处找）",
          set(preview.json()["allowedFields"]) == set(tlm.KINDS), str(preview.json())[:160])
    flushed = client.post("/api/exec/telemetry/flush", headers=headers(), json={})
    check("没有出口时 flush 如实回未发送（HTTP 面也一样不装）",
          flushed.json()["sent"] == 0 and flushed.json()["delivered"] is False, str(flushed.json())[:200])
    SEEN_KEYS["flush"] = set(flushed.json())
    decisions = client.get("/api/exec/telemetry/decisions", headers=headers())
    check("同意审计在 HTTP 面读得到（含被拒那次）", decisions.json()["count"] >= 2, str(decisions.json())[:200])
    SEEN_KEYS["decisions"] = set(decisions.json())
    discarded = client.post("/api/exec/telemetry/discard", headers=headers(), json={})
    check("discard 清缓冲并回条数", discarded.json()["removed"] >= 1 and tlm.pending_count() == 0, str(discarded.json())[:160])
    SEEN_KEYS["discard"] = set(discarded.json())
    closed = client.post("/api/exec/telemetry/consent", headers=headers(), json={"on": False, "via": "自测端点"})
    check("关闭走 200 且不带确认词也成功", closed.json()["ok"] is True and closed.json()["on"] is False, str(closed.json())[:160])
    SEEN_KEYS["consent_off"] = set(closed.json())
    check("空缓冲 flush 也算一次正常回话", client.post("/api/exec/telemetry/flush", headers=headers(), json={"limit": "脏值"}).status_code == 200)

    crash_describe = client.get("/api/exec/crash/describe", headers=headers())
    check("崩溃口径一次摊开：上限、类别表、脱敏规则名与钩子状态",
          crash_describe.json()["maxReports"] == crt.MAX_REPORTS
          and crash_describe.json()["redactionKinds"] == list(crt.REDACTION_KINDS)
          and "hooks" in crash_describe.json(), str(crash_describe.json())[:200])
    SEEN_KEYS["crash_describe"] = set(crash_describe.json())
    keep = scene("manual", text="端点面留下的现场", note="sk-abcdefghijklmnop 要被打掉")
    listed = client.get("/api/exec/crash/list", headers=headers())
    check("列表回话形状齐全且不带正文", listed.json()["count"] >= 1 and "reports" in listed.json()
          and all("traceback" not in row for row in listed.json()["reports"]), str(listed.json())[:200])
    SEEN_KEYS["crash_list"] = set(listed.json())
    check("列表里那条现场标了打掉了哪几类",
          any(x["reportId"] == keep["reportId"] and x["redactedKinds"] for x in listed.json()["reports"]), str(listed.json())[:220])
    detail = client.get("/api/exec/crash/detail", headers=headers(), params={"reportId": keep["reportId"]})
    check("详情带 traceback 且已脱敏（原值不在回话里）",
          detail.json()["ok"] is True and "abcdefghijklmnop" not in json.dumps(detail.json(), ensure_ascii=False), str(detail.json())[:200])
    SEEN_KEYS["crash_detail"] = set(detail.json())
    miss_detail = client.get("/api/exec/crash/detail", headers=headers(), params={"reportId": "crash-不存在"})
    check("找不到现场回 200 + ok=false + reason", miss_detail.status_code == 200 and miss_detail.json()["ok"] is False, str(miss_detail.json())[:160])
    SEEN_KEYS["crash_detail_missing"] = set(miss_detail.json())
    no_id = client.get("/api/exec/crash/detail", headers=headers())
    check("缺 reportId 的详情请求按缺参拒绝（400）", no_id.status_code == 400, str(no_id.status_code))
    exported = client.post("/api/exec/crash/export", headers=headers(), json={})
    check("导出回副本路径与指纹，原件仍在", exported.json()["ok"] is True and Path(keep["path"]).exists(), str(exported.json())[:200])
    SEEN_KEYS["crash_export"] = set(exported.json())
    check("导出物路径在崩溃根下", str(crt.crash_home()) in exported.json()["path"], str(exported.json())[:160])
    if Path(exported.json()["path"]).exists():
        Path(exported.json()["path"]).unlink()
    # 被拒的那一次也要进键集比对：`reason` 这个键只在失败回话里出现，不跑一次失败
    # 就等于让 TS 声明里那个字段永远处于「声明了但没人证明引擎真会给」的状态。
    before_exports = {p.name for p in crt.exports_dir().glob("*")} if crt.exports_dir().exists() else set()
    refused_export = client.post("/api/exec/crash/export", headers=headers(), json={"reportId": "crash-没有的这一份"})
    check("导出不存在的现场：200 + ok=false + 中文理由（不是 500，也不是静默空文件）",
          refused_export.status_code == 200 and refused_export.json()["ok"] is False
          and refused_export.json().get("reason"), str(refused_export.json())[:200])
    check("被拒的导出不落任何文件（没东西可导出却写出一个空副本是骗人）",
          ({p.name for p in crt.exports_dir().glob("*")} if crt.exports_dir().exists() else set()) == before_exports,
          str(sorted({p.name for p in crt.exports_dir().glob("*")} - before_exports))[:160])
    SEEN_KEYS["crash_export"] |= set(refused_export.json())
    bulk = client.post("/api/exec/crash/delete", headers=headers(), json={"reportId": "*"})
    check("整批删除被拒（这些是「本机崩过」的唯一证据）",
          bulk.status_code == 200 and bulk.json()["ok"] is False and "不接受一次删光" in bulk.json()["reason"], str(bulk.json())[:200])
    SEEN_KEYS["crash_delete_bulk"] = set(bulk.json())
    no_id_del = client.post("/api/exec/crash/delete", headers=headers(), json={})
    check("删除不给编号时按缺参拒绝", no_id_del.status_code == 400, str(no_id_del.status_code))
    removed = client.post("/api/exec/crash/delete", headers=headers(), json={"reportId": keep["reportId"]})
    check("按编号删除成功", removed.json()["ok"] is True and removed.json()["removed"] is True, str(removed.json())[:160])
    SEEN_KEYS["crash_delete"] = set(removed.json())
    stat = client.get("/api/exec/crash/status", headers=headers())
    check("状态面给出钩子装配情况（界面能读「循环那一半装没装上」）",
          set(stat.json()["hooks"]) == {"sysExcepthook", "loopHandler", "installError"}, str(stat.json())[:200])
    SEEN_KEYS["crash_status"] = set(stat.json())


# ── 12. 请求级异常处理器真的接得住 ─────────────────────────────


def exception_surface_checks(client) -> None:
    section("12. 一条会炸的临时路由：现场留得下、回话是人话、原文不外泄")
    before = crt.list_reports(200)["count"]
    tlm.reset_for_tests()

    @server.app.get("/_selftest/boom")
    async def _boom():
        raise RuntimeError(f"自测造的未捕获异常 {time.time_ns()}")

    resp = client.get("/_selftest/boom", headers=headers())
    check("未处理异常仍回 500（不因为留了现场就改成 200）", resp.status_code == 500, str(resp.status_code))
    body = resp.json()
    check("回话给得出中文说明与现场编号", body["code"] == "ENGINE_INTERNAL_ERROR" and body["crashReport"], str(body)[:200])
    check("回话里没有 traceback 原文与本机路径", "Traceback" not in json.dumps(body, ensure_ascii=False)
          and str(HOME_ROOT) not in json.dumps(body, ensure_ascii=False), str(body)[:200])
    check("现场确实多了一份", crt.list_reports(200)["count"] == before + 1, str(crt.list_reports(200)["count"]))
    detail = crt.detail(body["crashReport"])
    check("现场类别是 http_unhandled（与钩子那三类分得开）", detail["category"] == "http_unhandled", str(detail)[:200])
    check("现场里说了是哪个接口", "/_selftest/boom" in detail["note"], str(detail["note"])[:160])
    check("上下文字段只用了白名单里的 endpoint", "endpoint" in detail["context"], str(detail["context"])[:160])
    crt.delete(body["crashReport"])
    server.app.router.routes = [r for r in server.app.router.routes if getattr(r, "path", "") != "/_selftest/boom"]


# ── 13. 跨语言同源与错误码形状 ─────────────────────────────────


def contract_checks() -> None:
    section("13. TS 契约与实际回话双向对键；新错误码不进「CODE: 详情」那种形状")
    # 第 11 节把档位关回默认并清了缓冲，所以这里先自己造一条真事件：
    # 对键要拿**真实回话**的键集，不能拿空数组——空数组会让「TS 多出十个字段」也判通过。
    tlm.set_consent(True, word=tlm.CONFIRM_WORD, via="契约对键用例")
    recorded = tlm.record("task_finished", {
        "endReason": "completed", "durationMs": 4321, "modelCallCount": 3, "toolCallCount": 7,
        "hadError": False, "privacyBlocked": False, "checkpointResumable": True, "secureEnv": True,
        "engineVersion": "0.3.0",
    })
    check("对键用的事件真的落进缓冲了（否则后面的键集比对是空的）",
          recorded["buffered"] is True and len(tlm.preview(5)["payload"]) > 0, str(recorded)[:200])
    pairs = (
        ("TelemetryDescribeResponse", SEEN_KEYS["describe"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryStatusResponse", SEEN_KEYS["status"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryPreviewResponse", SEEN_KEYS["preview"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryDecisionsResponse", SEEN_KEYS["decisions"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryConsentResponse", SEEN_KEYS["consent_on"] | SEEN_KEYS["consent_off"]
         | SEEN_KEYS["consent_refused"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryFlushResponse", SEEN_KEYS["flush"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryDiscardResponse", SEEN_KEYS["discard"] | SEEN_KEYS["egress_refused"]),
        ("CrashDescribeResponse", SEEN_KEYS["crash_describe"] | SEEN_KEYS["egress_refused"]),
        ("CrashStatusResponse", SEEN_KEYS["crash_status"] | SEEN_KEYS["egress_refused"]),
        ("CrashListResponse", SEEN_KEYS["crash_list"] | SEEN_KEYS["egress_refused"]),
        ("CrashDetailResponse", SEEN_KEYS["crash_detail"] | SEEN_KEYS["crash_detail_missing"] | SEEN_KEYS["egress_refused"]),
        ("CrashExportResponse", SEEN_KEYS["crash_export"] | SEEN_KEYS["egress_refused"]),
        ("CrashDeleteResponse", SEEN_KEYS["crash_delete"] | SEEN_KEYS["crash_delete_bulk"] | SEEN_KEYS["egress_refused"]),
        ("TelemetryEventPayload", set(tlm.preview(5)["payload"][-1])),
        ("TelemetrySinkState", set(tlm.sink_state())),
        ("CrashHookState", set(crt.installed_state())),
        ("CrashRecorderState", set(crt.recorder_state())),
        ("CrashRuntimeInfo", set(crt.runtime_info())),
    )
    detail_base = _ts_interface_fields("CrashReportRow")
    for iface, keys in pairs:
        declared = _ts_interface_fields(iface)
        if iface == "CrashDetailResponse":
            # 详情回话 = 列表那一行的那套键 + 详情独有的四个 + 找不到时的 reason。
            # `reason` 属于**响应**而不是**行**：列表里每条都是真实现场，不会有 reason，
            # 把它写进 CrashReportRow 等于声明「行里可能带一句理由」，界面就会去读一个恒为空的键。
            declared |= detail_base
        absent = sorted(keys - declared)
        extra = sorted(declared - keys)
        check(f"{iface} 与端点实际回话的键集双向相等", not absent and not extra,
              f"TS 缺 {absent}；TS 多出（引擎从来没回过的字段）{extra}")
    consent_union = tlm.consent_state()
    tlm.consent_file().parent.mkdir(parents=True, exist_ok=True)
    tlm.consent_file().write_text("{坏", encoding="utf-8")
    corrupt_keys = set(tlm.consent_state())
    tlm.consent_file().unlink(missing_ok=True)
    for name in list(tlm.consent_file().parent.glob("consent.json.corrupt-*")):
        name.unlink()
    check("TelemetryConsentState 覆盖四个分支的键并集",
          _ts_interface_fields("TelemetryConsentState") == set(consent_union) | corrupt_keys,
          f"TS={sorted(_ts_interface_fields('TelemetryConsentState'))} 实={sorted(set(consent_union) | corrupt_keys)}")
    row_keys = set()
    got = crt.record("manual", exc_type="E", exc_text="形状", traceback_text="行形状 " + str(time.time_ns()))
    row_keys |= set(crt.detail(got["reportId"]))
    for key in ("traceback", "runtime", "categoryDeclared", "ok"):
        row_keys.discard(key)
    check("CrashReportRow 就是列表行的那套键", _ts_interface_fields("CrashReportRow") == row_keys,
          f"TS={sorted(_ts_interface_fields('CrashReportRow'))} 实={sorted(row_keys)}")
    crt.delete(got["reportId"])
    ts = TS_CONTRACT.read_text(encoding="utf-8")
    check("TS 契约写明了两族端点的关键路径",
          all(x in ts for x in ("/api/exec/telemetry/consent", "/api/exec/telemetry/preview", "/api/exec/crash/export")))
    check("这一族同样不走 IPC（渲染层 engineFetch 直连本机引擎）", "不走 IPC" in ts)
    check("TS 里逐字钉住「预览就是真要发出去的那份」", "将发送内容的逐字预览" in ts)
    check("TS 里写明安装标识只给后 8 位这条口径", "后 8 位" in ts)
    codes = sorted(set(re.findall(r'"((?:TELEMETRY|CRASH)_[A-Z_]+)"', ROUTES_SRC.read_text(encoding="utf-8"))))
    check("新族都有成套的自有码（至少 14 个）", len(codes) >= 14, str(codes))
    check("新码一律 TELEMETRY_ / CRASH_ 前缀（不与写暂存那族混用）",
          all(c.startswith(("TELEMETRY_", "CRASH_")) for c in codes), str(codes))
    bare = re.findall(r'["\'][A-Z][A-Z0-9_]{3,}:[^"\']', ROUTES_SRC.read_text(encoding="utf-8"))
    check("本文件不出现「CODE: 详情」那种载荷（那种形状会被跨进程错误口径契约要求逐条映射）",
          bare == [], str(bare)[:200])
    for name, src in (("crash_runtime.py", CRASH_SRC), ("telemetry_runtime.py", TELEMETRY_SRC)):
        hits = re.findall(r'["\'][A-Z][A-Z0-9_]{3,}:[^"\']', src.read_text(encoding="utf-8"))
        check(f"{name} 同样不用那种形状", hits == [], str(hits)[:200])
    new_paths = [p for p in rx.EXEC_ENDPOINT_PATHS if p.startswith(("/api/exec/telemetry/", "/api/exec/crash/"))]
    check("本卡新增的 13 条路径齐了（少一条就是路由掉了但清单没涨）",
          len(new_paths) == 13, f"{len(new_paths)}: {sorted(new_paths)}")
    check("车道前缀仍是既有的四个段（本卡没新开前缀，只挂在 exec 车道里）",
          set(rl.LANE_PREFIXES["exec"]) == {"/api/exec", "/api/approval", "/api/staging", "/api/persona"},
          str(sorted(rl.LANE_PREFIXES["exec"])))
    check("新路径全部落在 exec 车道允许的前缀里（越界会在挂载时被 routes_lanes 拒）",
          all(any(p.startswith(prefix) for prefix in rl.LANE_PREFIXES["exec"]) for p in new_paths),
          str([p for p in new_paths if not any(p.startswith(x) for x in rl.LANE_PREFIXES["exec"])]))


# ── 14. 诚实面：本卡没做的那一半 ───────────────────────────────


def honesty_checks() -> None:
    section("14. 如实写明还没做与没验的那一半")
    src = SERVER_SRC.read_text(encoding="utf-8")
    check("崩溃族不谎称自动外发（明确只在本机）",
          "只在本机" in CRASH_SRC.read_text(encoding="utf-8") and "不会自动发出去" in CRASH_SRC.read_text(encoding="utf-8"))
    check("没有接收端这件事写在遥测模块里（不谎称已经在发）",
          "没有遥测接收端" in TELEMETRY_SRC.read_text(encoding="utf-8")
          or "本仓库目前没有" in TELEMETRY_SRC.read_text(encoding="utf-8"))
    rust_files = sorted((PROJECT_ROOT / "src-tauri" / "src").rglob("*.rs"))
    rust_hits = [p.name for p in rust_files
                 if re.search(r"(?i)\b(telemetry|crash_runtime|crash_report)", p.read_text(encoding="utf-8", errors="ignore"))]
    check("Rust 侧确实没有本族的命令与通道（没做的那一半不许被读成做了）",
          bool(rust_files) and rust_hits == [], str(rust_hits))
    check("共享 IPC 契约里也没有本族通道名（本族只走 engineFetch 直连本机引擎）",
          not re.search(r"(?i)\b(telemetry|crash)[A-Za-z]*:", (PROJECT_ROOT / "src" / "shared" / "contracts.ts").read_text(encoding="utf-8")))
    check("设置页还没接线（本卡只交付引擎侧与契约）",
          not any("api/exec/telemetry" in p.read_text(encoding="utf-8", errors="ignore")
                  for p in (PROJECT_ROOT / "src" / "renderer").rglob("*.tsx")))
    check("依赖清单零变更（两族全用标准库）",
          "telemetry" not in (PROJECT_ROOT / "engine" / "requirements.txt").read_text(encoding="utf-8")
          and "crash" not in (PROJECT_ROOT / "engine" / "requirements.txt").read_text(encoding="utf-8"))
    check("server 的导入链里带上了两族（装配现场才拿得到）",
          "import crash_runtime as crt" in src and "import telemetry_runtime as tlm" in src)


# ── 主流程 ─────────────────────────────────────────────────────


def main() -> int:
    layout_checks()
    redaction_checks()
    record_checks()
    dedupe_checks()
    export_checks()
    hook_checks()
    consent_checks()
    field_checks()
    egress_checks()
    wiring_checks()
    desktop_env()
    client = TestClient(server.app)
    endpoint_checks(client)
    # 这一节要的是「异常真的穿过处理函数变成一份 500 回话」，而 TestClient 缺省
    # `raise_server_exceptions=True` 会把异常原样抛回调用方——那样处理器根本没机会跑完，
    # 看起来像崩在测试里。这里换一个不替我们抛的客户端，让回话本身接受检查。
    exception_surface_checks(TestClient(server.app, raise_server_exceptions=False))
    contract_checks()
    honesty_checks()
    print("----------------------------------------------------")
    if FAILURES:
        print(f"### {len(FAILURES)} 项未过：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("telemetry crash selftest: ALL PASS")
    return 0


if __name__ == "__main__":
    # `sys.exit` 不能写在 finally 里：那会把 main() 抛出的原始异常吃掉，只留下一个
    # 退出码——一个「崩了但看起来像正常结束」的自测正是本卡要治的病。
    try:
        CODE = main()
    finally:
        sys.excepthook = _ORIGINAL_EXCEPTHOOK
        threading.excepthook = _ORIGINAL_THREAD_HOOK
        tlm.reset_for_tests()
        crt.reset_for_tests()
    sys.exit(CODE)
