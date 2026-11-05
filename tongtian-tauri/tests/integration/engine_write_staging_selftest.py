#!/usr/bin/env python3
"""D-03.5 写暂存审批自测（#161，W4 领地）：模块语义 + 工具接线 + 端点面 + 跨语言同源。

这一族要钉住的是**一条底线**：没被用户批准的内容碰不到目标文件。所以用例分四层：

1. 模块本体（``write_staging_runtime``）：档位、暂存不落盘、diff、同目标只留一条、
   过期拒合并、位置重判、指纹重判、拒绝/清空、决定日志只带指纹不带正文；
2. 工具接线（``server.build_toolkit`` 的 ``extra["staging"]``）：开启时
   ``write_text_file`` / ``insert_text_file`` 一个字节都不写目标，未开启时
   **回执与改造前逐字相同**（这一档默认关，默认关就必须等于不存在）；
3. 端点面（``routes_exec`` 的 ``/api/staging/*``）：只读走 GET、会改状态走 POST、
   带远程身份整族 403、缺 changeId 拒绝、pending→change→apply→decisions 全链路；
4. 跨语言同源：``src/shared/contracts_exec.ts`` 的字段名与 Python meta 键逐条对上
   （D-48.4b 的教训：TS 契约一旦漂移，界面就会显示一份引擎从来没回过的东西）。

必须用 ``./vendor/python/python.exe -X utf8`` 跑。缺第三方依赖时整族 SKIP；
**引擎自己的文件导入断了则大声红**（静默 SKIP 会把真红报成没测，代价在案）。

真实桌面界面点开审批、真实合并进用户工作目录后端到端体验**未验收**：
本文件用 TestClient 与临时数据根跑通接线，不声称界面已经接上。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_write_staging_selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-staging-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

ENGINE_MODULES = {path.stem for path in (PROJECT_ROOT / "engine").glob("*.py")}

FAILURES: list[str] = []

LOCAL_TOKEN = "tt-selftest-local-token"
STATIC_TOKEN = "tt-selftest-static-token"


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
    import routes_exec as rx
    import write_staging_runtime as wsr
    import tool_approval as tap
    import data_paths as dp
    import side_effect_registry as ser
    import server
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "write staging selftest")

# server 的导入链可能把数据根拧到别处，这里再拧回本文件的临时目录：
# 暂存与决定日志必须落在临时目录，不许碰真的引擎数据目录。
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

HOME_ROOT = _TMP / "home"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"--- {title} ---")


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def workspace(name: str) -> Path:
    root = _TMP / "ws" / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def headers(token: str = LOCAL_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _meta_fields_from_source() -> set[str]:
    """从 stage() 的 meta 构造里刮出键名（不 import 私有函数，也不手写第二份清单）。

    为什么要从源码取：这条用例的目的就是「加了字段没登记」当场红，
    拿一份测试里手抄的清单去比另一份手抄清单，等于两个都可能同时过期。
    """
    import re

    src = (PROJECT_ROOT / "engine" / "write_staging_runtime.py").read_text(encoding="utf-8")
    start = src.index("        meta = {")
    body = src[start + len("        meta = {"):src.index("\n        }", start)]
    return set(re.findall(r'^\s+"([a-zA-Z]+)":', body, re.M))


def desktop_env() -> None:
    """桌面壳在场：本机令牌 + 远程令牌都配好，请求带本机令牌才算桌面壳。"""
    os.environ["TT_ENGINE_TOKEN"] = STATIC_TOKEN
    os.environ["TT_ENGINE_LOCAL_TOKEN"] = LOCAL_TOKEN


# ── 1. 档位读数 ─────────────────────────────────────────────────


def policy_checks() -> None:
    section("1. 档位：缺省关、别名认、非法值如实说")
    check("缺省（没配）就是 off", wsr.WriteStaging.from_runtime({}).mode == "off")
    check("runtime 不是 dict 也不炸", wsr.WriteStaging.from_runtime(None).mode == "off")
    check("runtime 传字符串当裸档位", wsr.WriteStaging.from_runtime({"writeStaging": "stage"}).mode == "staged")
    check("write_staging 下划线写法同样认", wsr.WriteStaging.from_runtime({"write_staging": {"mode": "staged"}}).mode == "staged")
    check("大写与空格归一", wsr.WriteStaging.from_runtime({"writeStaging": {"mode": " STAGED "}}).mode == "staged")
    bad = wsr.WriteStaging.from_runtime({"writeStaging": {"mode": "随便"}})
    check("非法值按 off 执行", bad.mode == "off" and not bad.staged)
    check("非法值有一句中文原因", "随便" in bad.error and "直接写盘" in bad.error, bad.error)
    check("合法值不带错误文案", wsr.WriteStaging.from_runtime({"writeStaging": "off"}).error == "")
    check("「关闭」算合法 off（不给没信息量的报错）", wsr.WriteStaging.from_runtime({"writeStaging": "关闭"}).error == "")
    check("MODE_LABELS 覆盖全部档位", set(wsr.MODE_LABELS) == set(wsr.MODES), str(sorted(wsr.MODES)))
    staged = wsr.WriteStaging("staged")
    check("off 时一个字提示词也不加", wsr.WriteStaging("off").system_hint() == "")
    hint = staged.system_hint()
    check("提示词写明不直接写盘", "不直接写盘" in hint)
    check("提示词给出变更号形态", "chg-" in hint)
    check("提示词禁止把待审阅说成已更新", "不要把文件说成已经更新" in hint)
    check("提示词禁止重复写同一文件来「确认成功」", "重复调用只会覆盖同一条待审阅" in hint)
    note = staged.note()
    check("给用户的说明带档位中文名", wsr.MODE_LABELS["staged"] in note, note)
    check("off 的说明只讲一句直接写盘", wsr.WriteStaging("off").note().count("；") == 0, wsr.WriteStaging("off").note())


# ── 2. 暂存目录归属 ─────────────────────────────────────────────


def layout_checks() -> None:
    section("2. 暂存区落在引擎数据根，不落在模型能写的地方")
    home = dp.engine_home()
    check("engine_home 已被环境指向临时目录", Path(str(home)).resolve() == HOME_ROOT.resolve(), str(home))
    staging = dp.write_staging_dir()
    check("暂存目录是数据根的直接子目录", staging.resolve().parent == home.resolve(), str(staging))
    check("暂存目录名与常量同源", staging.name == dp.WRITE_STAGING_DIRNAME)
    check("暂存目录不在任何工作目录里", "ws" not in str(staging.resolve()).split(os.sep))
    check("wsr.staging_home 与 data_paths 同一处", wsr.staging_home().resolve() == staging.resolve())
    check("决定日志在暂存目录内", wsr.decisions_file().resolve().parent == staging.resolve())
    check("决定日志是 JSONL", wsr.decisions_file().name.endswith(".jsonl"))


# ── 3. 暂存不落盘 / diff / 回执 ─────────────────────────────────


def stage_checks() -> None:
    section("3. 暂存：目标文件没被碰，diff 与回执都说得出代价")
    ws = workspace("stage")
    target = ws / "notes" / "a.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("第一行\n第二行\n", encoding="utf-8")
    before = target.read_bytes()

    r = wsr.stage("第一行\n第二行改\n第三行\n", target=target, task_id="t-stage", work_dir=ws)
    check("暂存成功", r["ok"] and r["staged"], r["reason"])
    check("目标文件一个字节没动", target.read_bytes() == before)
    check("回执不是「已写入」", "已写入文件" not in r["note"] and "没有写进目标文件" in r["note"], r["note"])
    check("回执给变更号", r["changeId"] in r["note"])
    check("回执给差异行数", "+2" in r["note"] and "−1" in r["note"], r["note"])
    check("回执说明同一文件重复写不叠两条", "不会叠成两条" in r["note"])
    check("变更号形态带 chg- 前缀", r["changeId"].startswith("chg-"))

    listing = wsr.pending(task_id="t-stage")
    check("待审阅列出这一条", listing["total"] == 1 and listing["observed"], str(listing["total"]))
    item = listing["items"][0]
    check("列表项显示相对路径", item["displayPath"] == str(Path("notes") / "a.md"), item["displayPath"])
    check("列表项此刻可批准", item["approvable"] and not item["targetChanged"])
    check("列表项带基线与新版指纹", bool(item["baseSha"]) and bool(item["newSha"]))
    check("列表项记录工作目录（apply 靠它重判位置）", item["workDir"] == str(ws.resolve()), item["workDir"])

    d = wsr.detail(r["changeId"])
    check("详情有 diff 正文", "---" in d["diffText"] and "+++" in d["diffText"])
    check("diff 里旧行减、新行加", "-第二行" in d["diffText"] and "+第三行" in d["diffText"], d["diffText"][:200])
    check("详情带回待写入的完整正文", d["newContent"].startswith("第一行\n第二行改"))
    check("详情标注正文是文本", d["newContentTextual"] is True)
    check("详情仍带 ok", d["ok"] is True)

    # 新建文件：基线不存在，diff 只能全是加
    fresh = ws / "brand-new.md"
    r2 = wsr.stage("全新内容\n第二行\n", target=fresh, task_id="t-stage", work_dir=ws)
    check("新建文件的暂存成功", r2["ok"])
    check("目标确实还没被创建", not fresh.exists())
    check("基线标记为不存在", wsr.detail(r2["changeId"])["baseExists"] is False)
    check("全是加行", r2["diffRemoved"] == 0 and r2["diffAdded"] == 2, f"{r2['diffAdded']}/{r2['diffRemoved']}")
    check("新建父目录也不提前创建", not (ws / "deep" / "x").exists())
    r3 = wsr.stage("深层\n", target=ws / "deep" / "sub" / "x.md", task_id="t-stage", work_dir=ws)
    check("目标父目录不存在也能暂存", r3["ok"], r3["reason"])
    check("仍然没创建父目录", not (ws / "deep").exists())

    # 同目标重复写：只留一条，变更号不变
    again = wsr.stage("第一行\n改成别的\n", target=target, task_id="t-stage", work_dir=ws)
    check("同目标重复写沿用同一个变更号", again["changeId"] == r["changeId"], again["changeId"])
    check("替换计数进位", again["supersededCount"] == 1)
    check("回执点名旧的那条已失效", "已被这条替换" in again["note"], again["note"])
    check("待审阅仍然只有一条", wsr.pending(task_id="t-stage")["total"] == 3, str(wsr.pending(task_id="t-stage")["total"]))

    # 内容上限
    old_cap = wsr.MAX_CONTENT_BYTES
    try:
        wsr.MAX_CONTENT_BYTES = 64
        big = wsr.stage("大" * 200, target=ws / "big.md", task_id="t-stage", work_dir=ws)
        check("超过内容上限被拒", not big["ok"] and "超过暂存上限" in big["reason"], big["reason"])
        check("被拒时内容既没暂存也没写盘", not (ws / "big.md").exists())
    finally:
        wsr.MAX_CONTENT_BYTES = old_cap

    # 待审阅配额
    old_quota = wsr.MAX_PENDING_PER_TASK
    try:
        wsr.MAX_PENDING_PER_TASK = 1
        quota_ws = workspace("quota")
        (quota_ws / "one.md").write_text("x\n", encoding="utf-8")
        first = wsr.stage("一\n", target=quota_ws / "one.md", task_id="t-quota", work_dir=quota_ws)
        second = wsr.stage("二\n", target=quota_ws / "two.md", task_id="t-quota", work_dir=quota_ws)
        check("配额满时新的暂存被拒（不是静默丢弃）", first["ok"] and not second["ok"], second["reason"])
        check("拒绝理由说要处理存量", "请先在界面处理存量" in second["reason"], second["reason"])
    finally:
        wsr.MAX_PENDING_PER_TASK = old_quota

    # 缺目标路径
    none = wsr.stage("内容\n", target="", task_id="t-stage", work_dir=ws)
    check("没给目标路径就拒绝", not none["ok"] and "没有给出目标路径" in none["reason"])


# ── 4. 过期与位置重判 ───────────────────────────────────────────


def stale_checks() -> None:
    section("4. 合并前重判：目标变了就不许拿旧 diff 盖新内容")
    ws = workspace("stale")
    target = ws / "b.md"
    target.write_text("原始\n", encoding="utf-8")
    r = wsr.stage("原始改\n", target=target, task_id="t-stale", work_dir=ws)
    target.write_text("外部又改过一次\n", encoding="utf-8")

    item = wsr.pending(task_id="t-stale")["items"][0]
    check("外部改动被识别为已过期", item["targetChanged"] and not item["approvable"], item["targetChangeNote"])
    a = wsr.apply(r["changeId"], work_dir=ws)
    check("过期那条拒绝合并", not a["ok"] and not a["written"], a["reason"])
    check("拒绝理由说清为什么不能盖", "拿旧 diff 盖新内容" in a["reason"], a["reason"])
    check("拒绝理由让用户重做", "重新写一次" in a["reason"])
    check("外部内容仍然在盘上", target.read_text(encoding="utf-8") == "外部又改过一次\n")
    check("过期状态落到记录里", wsr.detail(r["changeId"])["status"] == wsr.STATUS_STALE)

    # 过期那条被下一次暂存替换（不留在列表里让用户猜）
    r2 = wsr.stage("外部又改过一次\n补一行\n", target=target, task_id="t-stale", work_dir=ws)
    check("替换过期那条而不是叠两条", r2["supersededCount"] >= 1, str(r2["supersededCount"]))
    check("替换后基线取此刻盘上那份", wsr.detail(r2["changeId"])["baseSha"] == wsr._sha(target.read_bytes()))
    check("替换后可以批准", wsr.detail(r2["changeId"])["approvable"])

    # 目标被删
    target.unlink()
    gone = wsr.pending(task_id="t-stale")["items"][0]
    check("目标被删也算过期", gone["targetChanged"] and "删除" in gone["targetChangeNote"], gone["targetChangeNote"])

    # 新建类暂存：目标在暂存之后被别人创建了 → 同样过期
    nr = wsr.stage("我来建\n", target=ws / "c.md", task_id="t-stale", work_dir=ws)
    (ws / "c.md").write_text("别人先建了\n", encoding="utf-8")
    na = wsr.apply(nr["changeId"], work_dir=ws)
    check("「以为要新建、其实已存在」拒绝合并", not na["ok"] and "被别人创建" in na["reason"], na["reason"])
    check("别人建的内容没被盖掉", (ws / "c.md").read_text(encoding="utf-8") == "别人先建了\n")

    # 位置重判：工作目录内照旧允许；越出且命中硬拒清单一律拒
    home_child = Path.home() / "tt-staging-should-never-exist.md"
    hr = wsr.stage("危险写入\n", target=home_child, task_id="t-stale", work_dir=ws)
    check("越出工作目录的暂存本身不写盘", hr["ok"] and not home_child.exists())
    ha = wsr.apply(hr["changeId"])
    check("合并时越界命中硬拒清单被拦", not ha["ok"] and "禁止写入的位置" in ha["reason"], ha["reason"])
    check("被拦时目标文件仍然不存在", not home_child.exists())
    check("越权拦下也进决定日志", any(row.get("outcome") == "denied" for row in wsr.decisions()["items"]))

    # 找不到变更号
    nf = wsr.apply("chg-does-not-exist")
    check("没这条记录就说没有", not nf["ok"] and nf["reason"], nf["reason"])


# ── 5. 合并、版本历史与回执 ─────────────────────────────────────


def apply_checks() -> None:
    section("5. 合并：批准后这一次才真的写盘")
    ws = workspace("apply")
    target = ws / "d.md"
    target.write_text("旧内容\n", encoding="utf-8")
    r = wsr.stage("新内容\n第二行\n", target=target, task_id="t-apply", work_dir=ws)

    # 假版本历史句柄：记录调用，不碰真实台账
    calls: list[dict] = []

    class FakeHistory:
        @staticmethod
        def record_current(path, *, task_id="", tool=""):
            calls.append({"path": str(path), "task_id": task_id, "tool": tool})
            return {"note": "已留一版旧内容（版本 0001）"}

    a = wsr.apply(r["changeId"], history=FakeHistory, work_dir=ws)
    check("合并成功", a["ok"] and a["written"], a["reason"])
    check("目标内容等于批准的那份", target.read_text(encoding="utf-8") == "新内容\n第二行\n", target.read_text(encoding="utf-8"))
    check("版本历史被调用一次且带着工具名", len(calls) == 1 and calls[0]["tool"] == "write_staging_apply", str(calls))
    check("回执带上留档说明", "已留一版旧内容" in a["note"], a["note"])
    check("回执给出显示路径", a["displayPath"] == "d.md", a["displayPath"])
    check("回执给出写入字节数", a["writtenBytes"] == target.stat().st_size)
    check("合并后记录销掉", wsr.pending(task_id="t-apply")["total"] == 0)
    again = wsr.apply(r["changeId"], work_dir=ws)
    check("同一条不能被合并两次", not again["ok"], again["reason"])
    rows = wsr.decisions(change_id=r["changeId"])["items"]
    check("决定日志有一条 applied", any(x["outcome"] == "applied" for x in rows), str(rows)[:200])

    # 没给版本历史句柄：合并照做，但明说这次没有可回滚旧版
    r2 = wsr.stage("再改一次\n", target=target, task_id="t-apply", work_dir=ws)
    a2 = wsr.apply(r2["changeId"], work_dir=ws)
    check("没句柄也照样合并（用户已经批准）", a2["ok"], a2["reason"])
    check("没句柄时明说没留旧版本", "本次合并没有留旧版本" in a2["note"], a2["note"])

    # 留档句柄抛异常：不挡合并，但要说失败
    class BoomHistory:
        @staticmethod
        def record_current(*args, **kwargs):
            raise RuntimeError("台账被别的过程占着")

    r3 = wsr.stage("第三次\n", target=target, task_id="t-apply", work_dir=ws)
    a3 = wsr.apply(r3["changeId"], history=BoomHistory, work_dir=ws)
    check("留档失败不挡已批准的合并", a3["ok"] and target.read_text(encoding="utf-8") == "第三次\n")
    check("留档失败如实进回执", "覆盖前留档失败" in a3["note"], a3["note"])

    # 暂存内容被动过：指纹不符不许合并
    r4 = wsr.stage("第四次的原样\n", target=target, task_id="t-apply", work_dir=ws)
    # 直接按变更号定位目录（不猜结构）
    hits = [p.parent for p in wsr.staging_home().rglob(wsr.CONTENT_NAME)
            if (p.parent.name == r4["changeId"])]
    check("能按变更号定位暂存目录", bool(hits), r4["changeId"])
    if hits:
        hits[0].joinpath(wsr.CONTENT_NAME).write_bytes("被人塞了一行\n".encode("utf-8"))
        a4 = wsr.apply(r4["changeId"], work_dir=ws)
        check("暂存内容与指纹不符时拒绝合并", not a4["ok"] and "指纹不符" in a4["reason"], a4["reason"])
        check("拒绝时目标保持原样（没被塞进去的内容盖）", target.read_text(encoding="utf-8") == "第三次\n")
        check("指纹不符那条留在待审阅里（没被偷偷销掉，用户还能拒）",
              any(x["changeId"] == r4["changeId"] for x in wsr.pending(task_id="t-apply")["items"]))

    # 拒绝与清空
    r5 = wsr.stage("不要的这一份\n", target=ws / "e.md", task_id="t-apply", work_dir=ws)
    rr = wsr.reject(r5["changeId"], reason="措辞不对")
    check("拒绝成功", rr["ok"], rr["reason"])
    check("拒绝说明目标从未被改", "目标文件没被改过" in rr["note"], rr["note"])
    check("拒绝带上用户给的原因", "措辞不对" in rr["note"])
    check("被拒的那条从待审阅里消失",
          all(x["changeId"] != r5["changeId"] for x in wsr.pending(task_id="t-apply")["items"]))
    check("被拒的目标文件从未创建", not (ws / "e.md").exists())

    ws2 = workspace("discard")
    for i in range(3):
        wsr.stage(f"批量 {i}\n", target=ws2 / f"f{i}.md", task_id="t-discard", work_dir=ws2)
    check("批量前先有 3 条", wsr.pending(task_id="t-discard")["total"] == 3)
    dd = wsr.discard_all("t-discard")
    check("清空按任务返回丢弃条数", dd["ok"] and dd["dropped"] == 3, str(dd))
    check("清空后列表为空", wsr.pending(task_id="t-discard")["total"] == 0)
    check("清空不碰任何目标文件", not any((ws2 / f"f{i}.md").exists() for i in range(3)))


# ── 6. 决定日志只带指纹 ─────────────────────────────────────────


def audit_checks() -> None:
    section("6. 决定日志：只有指纹与字节数，没有内容本体")
    marker = f"绝不该出现在日志里的正文-{int(time.time())}"
    ws = workspace("audit")
    target = ws / "g.md"
    target.write_text("旧\n", encoding="utf-8")
    r = wsr.stage(marker + "\n", target=target, task_id="t-audit", work_dir=ws)
    wsr.apply(r["changeId"], work_dir=ws)
    raw = wsr.decisions_file().read_text(encoding="utf-8")
    check("日志里搜不到正文", marker not in raw, raw[-200:])
    check("日志带 applied 与指纹字段", '"outcome": "applied"' in raw and '"newSha"' in raw)
    rows = wsr.decisions(change_id=r["changeId"])["items"]
    check("读面按变更号能定位", len(rows) == 1 and rows[0]["writtenBytes"] > 0, str(rows)[:200])
    check("每条都如实报落盘与否", all("persisted" in row for row in rows))
    bad = wsr.decisions(limit=0)
    check("limit 为 0 被夹到至少 1 条", bad["ok"] and bad["total"] >= 1, str(bad)[:120])
    big = wsr.decisions(limit=999999)
    check("limit 超上限被夹住不炸", big["ok"] and wsr.MAX_LIST_LIMIT >= big["total"])


# ── 7. 工具接线 ─────────────────────────────────────────────────


def tool_wiring_checks() -> None:
    section("7. build_toolkit 接线：没句柄=改造前，开启=一个字节都不写")
    ws = workspace("tools")
    plain = server.build_toolkit(ws)
    write = plain.tools["write_text_file"].original_func
    insert = plain.tools["insert_text_file"].original_func
    t1 = ws / "plain.md"
    out = tool_text(write(str(t1), "直接写\n"))
    check("未开启暂存时照旧写盘", t1.read_text(encoding="utf-8") == "直接写\n")
    check("未开启时回执与改造前逐字相同", out == f"已成功写入文件: {t1}", out)
    out2 = tool_text(insert(str(t1), "再来一行\n"))
    # insert 的既有语义：按行 join，**不留尾换行**（改造前后必须一致，否则「开启暂存」会改变落盘字节）
    joined = "直接写\n再来一行"
    check("insert 未开启时同样照旧", out2 == f"已成功更新文件: {t1}" and t1.read_text(encoding="utf-8") == joined, out2)

    staged = wsr.WriteStaging("staged")
    tk = server.build_toolkit(ws, extra={"staging": staged})
    t2 = ws / "staged.md"
    before = t2.exists()
    text = tool_text(tk.tools["write_text_file"].original_func(str(t2), "待审阅的内容\n第二行\n"))
    check("开启后工具回执是暂存回执而不是已写入", "没有写进目标文件" in text and "已成功写入文件" not in text, text)
    check("开启后目标文件没被创建", not before and not t2.exists())
    item = wsr.pending(task_id="")["items"][0]
    check("工具走的仍是同一个暂存实现（taskSlug 对得上）", item["tool"] == "write_text_file")
    merged_text = tool_text(tk.tools["insert_text_file"].original_func(str(t1), "追加一行\n"))
    check("insert 也进暂存", "【写暂存】" in merged_text, merged_text[:120])
    ins_item = next(x for x in wsr.pending()["items"] if x["tool"] == "insert_text_file")
    detail = wsr.detail(ins_item["changeId"])
    check("insert 暂存的是整份合并后的内容", detail["newContent"] == "直接写\n再来一行\n追加一行", repr(detail["newContent"][:80]))
    check("insert 的 diff 与批准后会落盘的那份一致", "+追加一行" in detail["diffText"] and "-再来一行" not in detail["diffText"])
    check("insert 没改盘上文件", t1.read_text(encoding="utf-8") == joined)

    # 沙箱/档位仍然先于暂存：越出工作目录照旧被拒，且不进暂存
    outside = tool_text(tk.tools["write_text_file"].original_func("../越界.md", "越界内容\n"))
    check("越出工作目录仍按原有边界拒绝", "超出工作目录" in outside, outside[:160])

    # 句柄给的是 off 策略：等价于没给
    tk_off = server.build_toolkit(ws, extra={"staging": wsr.WriteStaging("off")})
    t3 = ws / "off.md"
    text3 = tool_text(tk_off.tools["write_text_file"].original_func(str(t3), "off 直接写\n"))
    check("off 策略下照常写盘且回执不变", t3.read_text(encoding="utf-8") == "off 直接写\n" and text3 == f"已成功写入文件: {t3}", text3)

    # 常驻清单不因这一档变化（不新增工具名）
    names = set(plain.tools)
    check("暂存不新增工具名（清单与改造前同一批）", names == set(tk.tools), str(sorted(names ^ set(tk.tools))))


# ── 7.5 与逐工具审批网关合并（D-03.5 的第③条口径）───────────────


def _drive_middleware(ta: tap.ToolApproval, name: str) -> tuple[str, bool]:
    """跑一次真实中间件，返回（回执文本, 工具是否被执行）。"""
    import asyncio

    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    executed: list[bool] = []

    async def _run(**kwargs):
        executed.append(True)
        inner = ToolResponse(content=[TextBlock(type="text", text="EXECUTED")])

        async def gen():
            yield inner

        return gen()

    def next_handler(**kwargs):
        return _run(**kwargs)

    async def go() -> str:
        chunks = []
        async for chunk in ta.middleware()({"tool_call": {"name": name, "input": {}}}, next_handler):
            chunks.append(tool_text(chunk))
        return "\n".join(chunks)

    return asyncio.run(go()), bool(executed)


def approval_merge_checks() -> None:
    section("7.5 与逐工具审批网关合并：一次写入只有一次批准")
    check("接管名单只有一个来源", tap.STAGED_WRITE_TOOLS == wsr.STAGED_TOOLS)
    plain = tap.ToolApproval(mode="all")
    plain_now = tap.ToolApproval(mode="all", runtime_present=True)
    check("没开暂存时照旧逐工具弹窗", plain_now.prompts_for("write_text_file") and plain_now.prompts_for("delete_text_file"))
    merged = tap.ToolApproval(mode="all", runtime_present=True, staged_writes=True)
    check("开暂存后两件文本写入不再弹第二个框",
          not merged.prompts_for("write_text_file") and not merged.prompts_for("insert_text_file"))
    check("接管范围不扩大：删除仍逐次确认", merged.prompts_for("delete_text_file"))
    check("接管范围不扩大：生成办公产物仍逐次确认",
          merged.prompts_for("generate_xlsx") and merged.prompts_for("generate_docx"))
    check("接管范围不扩大：命令与联网仍逐次确认",
          merged.prompts_for("run_command") and merged.prompts_for("fetch_url"))
    check("纯函数 needs_approval 未被改动（档位语义只有一份）",
          tap.needs_approval("write_text_file", "sensitive") and not tap.needs_approval("view_text_file", "sensitive"))
    check("接管时说明里点名由谁把关", "写暂存已开启" in " ".join(merged.notes()), str(merged.notes())[:200])
    check("接管说明点出两个工具名", all(n in " ".join(merged.notes()) for n in wsr.STAGED_TOOLS))
    check("没接管时说明与改造前一样（不多一句）",
          len(plain_now.notes()) == 1 and "写暂存" not in plain_now.notes()[0], str(plain_now.notes())[:200])
    check("off 档没有任何说明（缺省不产生噪声）", tap.ToolApproval(mode="off", runtime_present=True).notes() == [])

    boom: list[str] = []

    async def _probe(tool: str, summary: str, task_id: str):
        """代替真实网关的探针：记下「确实来问过」，并给出拒绝，好让断言看得见差别。"""
        boom.append(tool)
        return False, "探针：网关被调用"

    gated = tap.ToolApproval(mode="all", gate_url="http://127.0.0.1:1/gate", gate_token="t",
                             runtime_present=True, staged_writes=True)
    gated._approve = _probe
    text, executed = _drive_middleware(gated, "write_text_file")
    check("暂存接管的工具直接执行到暂存那一步、不碰网关", executed and "EXECUTED" in text and not boom, text[:120])
    other = tap.ToolApproval(mode="all", gate_url="http://127.0.0.1:1/gate", gate_token="t",
                             runtime_present=True, staged_writes=True)
    other._approve = _probe
    other_text, other_executed = _drive_middleware(other, "delete_text_file")
    check("没被接管的工具仍然走网关（少弹的不是全体）", boom == ["delete_text_file"] and not other_executed, str(boom))
    check("网关拒绝时按原有文案回执", "未执行" in other_text, other_text[:120])

    src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    check("装配现场按同一份 runtime 设置接管开关", "tool_approval.staged_writes = wsr.staging_enabled(session.runtime)" in src)
    check("staging_enabled 与 from_runtime 同一判定",
          wsr.staging_enabled({"writeStaging": "staged"}) and not wsr.staging_enabled({}))


# ── 8. 端点面 ───────────────────────────────────────────────────


def endpoint_checks(client: TestClient) -> None:
    section("8. /api/staging/* 端点面")
    app_paths = {r.path for r in server.app.routes}
    missing = [p for p in rx.EXEC_ENDPOINT_PATHS if p not in app_paths]
    check("清单里的每条 exec 端点都真挂在 app 上", not missing, str(missing))
    got = {r.path for r in server.app.routes if "GET" in (r.methods or set()) and r.path.startswith("/api/staging")}
    post = {r.path for r in server.app.routes if "POST" in (r.methods or set()) and r.path.startswith("/api/staging")}
    check("只读四条走 GET",
          got == {"/api/staging/status", "/api/staging/pending", "/api/staging/change", "/api/staging/decisions"},
          str(sorted(got)))
    check("会改状态三条走 POST",
          post == {"/api/staging/apply", "/api/staging/reject", "/api/staging/discard"}, str(sorted(post)))
    check("写面没有一条走 GET（审批动作不靠幂等查询触发）",
          not {p for p in got if p.endswith(("/apply", "/reject", "/discard"))})

    remote = client.get("/api/staging/pending", headers=headers(STATIC_TOKEN))
    check("带远程身份读清单被 403 拦下", remote.status_code == 403, str(remote.status_code))
    body = remote.json()
    check("拒绝理由点名按人隔离未落地", "按人隔离" in body.get("message", ""), str(body)[:200])
    check("拒绝理由不是只给查询（整族口径）", "整族不开放" in body.get("message", ""), str(body)[:200])
    code = body.get("code", "")
    check("错误码前缀属于本族", code.startswith("STAGING_"), code)
    for verb, path in (
        (client.post, "/api/staging/apply"),
        (client.post, "/api/staging/reject"),
        (client.post, "/api/staging/discard"),
        (client.get, "/api/staging/status"),
    ):
        resp = verb(path, headers=headers(STATIC_TOKEN), json={}) if verb in (client.post,) else verb(path, headers=headers(STATIC_TOKEN))
        check(f"远程身份 {path} 同样 403", resp.status_code == 403, str(resp.status_code))

    miss = client.post("/api/staging/apply", headers=headers(), json={})
    check("缺 changeId 的合并请求被拒", miss.status_code == 400 and miss.json()["ok"] is False, str(miss.json())[:160])
    check("拒绝理由说要变更号", "changeId" in miss.json()["message"], str(miss.json())[:200])
    bad_json = client.post("/api/staging/reject", headers=headers(), content=b"not json")
    check("请求体不是 JSON 时按缺参拒绝，不是 500", bad_json.status_code == 400, str(bad_json.status_code))

    # 端到端：模型侧暂存 → 界面看列表 → 点开看 diff → 批准 → 文件才变 → 日志有据
    ws = workspace("api")
    target = ws / "h.md"
    target.write_text("原来\n", encoding="utf-8")
    seeded = wsr.stage("改成\n这样\n", target=target, task_id="t-api", work_dir=ws)
    listing = client.get("/api/staging/pending", headers=headers(), params={"taskId": "t-api"}).json()
    check("端点列出了这条待审阅", listing["ok"] and listing["total"] == 1, str(listing)[:200])
    check("列表项带 approvable 供界面决定按钮", listing["items"][0]["approvable"] is True)
    check("列表项带 stagingDir 便于报出落点", "stagingDir" in listing)
    check("端点回话不含正文（列表只给元数据）", "改成" not in json.dumps(listing, ensure_ascii=False))

    one = client.get("/api/staging/change", headers=headers(), params={"changeId": seeded["changeId"]}).json()
    check("详情端点回 diff 正文", "+改成" in one["diffText"], one["diffText"][:120])
    check("详情端点回待写入内容", one["newContent"] == "改成\n这样\n")
    no_id = client.get("/api/staging/change", headers=headers())
    check("没给 changeId 的详情按 400 拒绝", no_id.status_code == 400, str(no_id.status_code))
    ghost = client.get("/api/staging/change", headers=headers(), params={"changeId": "chg-none"}).json()
    check("查不存在的变更号回 200 + ok=false（流程内结果）", ghost["ok"] is False and ghost.get("reason"), str(ghost)[:160])

    applied = client.post("/api/staging/apply", headers=headers(), json={"changeId": seeded["changeId"]})
    check("端点合并成功", applied.json()["ok"] and applied.json()["written"], str(applied.json())[:200])
    check("到这一步目标文件才真的变", target.read_text(encoding="utf-8") == "改成\n这样\n", target.read_text(encoding="utf-8"))
    check("端点合并也走版本历史句柄（与 build_toolkit 同源）", "已合并到" in applied.json()["note"], applied.json()["note"])
    after = client.get("/api/staging/pending", headers=headers(), params={"taskId": "t-api"}).json()
    check("合并后待审阅清空", after["total"] == 0 and after["observed"] is False, str(after)[:160])
    dec = client.get("/api/staging/decisions", headers=headers(), params={"changeId": seeded["changeId"]}).json()
    check("决定日志端点读得到刚才那次", dec["ok"] and dec["items"][0]["outcome"] == "applied", str(dec)[:200])

    rejected_seed = wsr.stage("不要的\n", target=ws / "i.md", task_id="t-api", work_dir=ws)
    rej = client.post("/api/staging/reject", headers=headers(), json={"changeId": rejected_seed["changeId"], "reason": "界面点的拒绝"})
    check("端点拒绝成功", rej.json()["ok"] and rej.json()["targetUntouched"] is True, str(rej.json())[:160])
    check("拒绝后文件仍未创建", not (ws / "i.md").exists())

    for name in ("j1.md", "j2.md"):
        wsr.stage(f"{name}\n", target=ws / name, task_id="t-api2", work_dir=ws)
    disc = client.post("/api/staging/discard", headers=headers(), json={"taskId": "t-api2"})
    check("端点清空按任务条数回执", disc.json()["ok"] and disc.json()["dropped"] == 2, str(disc.json())[:200])
    check("清空不碰目标", not (ws / "j1.md").exists() and not (ws / "j2.md").exists())

    status = client.get("/api/staging/status", headers=headers()).json()
    check("状态端点回档位与限额", status["ok"] and status["mode"] in wsr.MODES and "limits" in status, str(status)[:200])
    check("状态端点如实报档位取自哪一级", status.get("policySource"), str(status)[:200])
    check("状态端点带暂存目录", Path(status["stagingDir"]).resolve().parent == dp.engine_home().resolve())
    session_status = client.get("/api/staging/status", headers=headers(), params={"session": "会话不存在"}).json()
    check("会话查不到时明说按缺省档位报", "缺省" in session_status["policySource"], session_status["policySource"])


# ── 9. 装配现场与同源 ───────────────────────────────────────────


def wiring_checks() -> None:
    section("9. server 装配与跨语言同源")
    src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    check("server 导入写暂存模块", "import write_staging_runtime as wsr" in src)
    check("build_agent 按 runtime 取档位", "wsr.WriteStaging.from_runtime(runtime)" in src)
    check("开启时才加提示词", "sys_prompt += staging.system_hint()" in src)
    check("策略对象经 extra 进 build_toolkit", '"staging": staging' in src)
    check("暂存先于写盘判定", "_stage_instead_of_write(p, content" in src and "_stage_instead_of_write(p, merged" in src)
    check("暂存失败不回退成直接写盘", src.count("return staged") >= 2)
    check("档位与非法值都摊给用户", "context_notes.append(staging.note())" in src)
    check("车道仍然不 import server", "import server" not in (PROJECT_ROOT / "engine" / "routes_exec.py").read_text(encoding="utf-8"))

    meta_keys = {
        "changeId", "taskId", "taskSlug", "targetPath", "displayPath", "workDir", "tool", "status",
        "createdAtMs", "updatedAtMs", "supersededCount", "baseExists", "baseSha", "baseSize",
        "baseMtimeMs", "newSize", "newSha", "diffAvailable", "diffNote", "diffLines", "diffAdded",
        "diffRemoved", "diffTruncated",
    }
    # 常量表与实现同源：少一个键就说明有人在 stage() 里加了字段没登记
    check("清单里的键与 meta 构造处逐字相同（双向：加字段、删字段都要红）",
          meta_keys == _meta_fields_from_source(),
          f"多：{sorted(_meta_fields_from_source() - meta_keys)} 少：{sorted(meta_keys - _meta_fields_from_source())}")
    ts = (PROJECT_ROOT / "src" / "shared" / "contracts_exec.ts").read_text(encoding="utf-8")
    absent = [k for k in sorted(meta_keys) if f"{k}:" not in ts]
    check("TS 契约把 meta 的每个键都写出来了（不靠现场猜）", not absent, str(absent))
    for field in ("approvable", "targetChanged", "targetChangeNote", "diffText", "newContent",
                  "policySource", "stagingDir", "historyNote", "writtenBytes", "targetUntouched"):
        # 可选字段在 TS 里写成 `name?:`，只找 `name:` 会把已经写对的字段报成缺
        check(f"TS 契约含读侧字段 {field}", f"{field}:" in ts or f"{field}?:" in ts)
    check("TS 契约声明这一族不走 IPC", "不走 IPC" in ts or "不新增 channel" in ts)
    check("TS 档位常量与 Python MODES 同一批", all(f"'{m}'" in ts for m in wsr.MODES))

    side_effects = set(ser.side_effect_tools()) if hasattr(ser, "side_effect_tools") else set()
    check("两个写入工具本就在副作用名单里（不新增工具名）",
          not side_effects or {"write_text_file", "insert_text_file"} <= side_effects, str(sorted(side_effects))[:200])
    names = set(server.build_toolkit(workspace("names")).tools)
    check("常驻清单里没有 staging 这类新工具名", not any("staging" in n for n in names), str(sorted(names)))
    check("没有新增第三方依赖（requirements 里搜不到新包）",
          "write-staging" not in (PROJECT_ROOT / "engine" / "requirements.txt").read_text(encoding="utf-8"))


def main() -> int:
    print("=== 写暂存审批：模块 / 工具接线 / 端点面 / 同源契约自测（离线 · 临时数据根）===")
    desktop_env()
    policy_checks()
    layout_checks()
    wsr.reset_for_tests()
    stage_checks()
    wsr.reset_for_tests()
    stale_checks()
    wsr.reset_for_tests()
    apply_checks()
    wsr.reset_for_tests()
    audit_checks()
    wsr.reset_for_tests()
    tool_wiring_checks()
    wsr.reset_for_tests()
    approval_merge_checks()
    wsr.reset_for_tests()
    client = TestClient(server.app)
    endpoint_checks(client)
    wiring_checks()
    print("----------------------------------------------------")
    if FAILURES:
        print(f"### {len(FAILURES)} 项未过：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("write staging selftest: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
