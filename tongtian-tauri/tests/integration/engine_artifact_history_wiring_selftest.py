#!/usr/bin/env python3
"""D-26 接线自测：主会话写盘前真的留一版可回滚的旧内容（W4 落 W2 分片 138.md §5-c 的那根线）

被 `artifact_history_runtime` 自己的 80 项自测覆盖不到的是**接线面**——那边证明的是「调用
`record_current` 会留下什么」，本文件证明的是「引擎的写入工具真的在覆盖前调了它，而且只在
该调的时候调」。锁九件事：

1. **未点亮即零行为改变**：`build_toolkit` 被自测/执行体直接调用时（`extra` 为空）回执与
   接线前**逐字相同**，数据根里连 `artifact-history` 目录都不会出现——不许有「默默多写一份盘」的副作用；
2. 点亮后**首次创建不算留档**（盘上没有即将消失的内容），但这句话要给到用户；
3. 第二次覆盖时留下的那一版**内容等于第一次盘上那份**（不是新写入的那份）；
4. 逐字节相同走去重：照样写回，历史长度不增；
5. `insert_text_file` 同一条闸；文件不存在时不留下半截版本；
6. 单份超过 `MAX_TRACK_BYTES` ⇒ **覆盖照常**，回执点名实际字节数与上限，不悄悄少留；
7. 留档这一侧任何异常都不许把用户的写入带崩（故障隔离），但失败要照实说；
8. 删除**不接**版本历史（走 `file-trash`，两套恢复来源不双写——这是 W4 对分片 §5-c 后半的决策）；
9. 反向钉住「Rust 内置执行体的写路径本轮一行没接」，不许把引擎半边读成两条通道都好了。

只读临时目录 + 临时数据根（`TONGTIANWORK_HOME`），不碰用户真实工作区与 `~/.tongtianwork`。

用法: python tests/integration/engine_artifact_history_wiring_selftest.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

TEMP_HOME = tempfile.mkdtemp(prefix="tt-ahw-home-")
os.environ["TONGTIANWORK_HOME"] = TEMP_HOME

try:
    import server as engine_server
    import artifact_history_runtime as ahr
    import data_paths
    import toolkit_lanes as tl
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine artifact history wiring selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def call(toolkit, name, *args, **kwargs):
    return tool_text(toolkit.tools[name].original_func(*args, **kwargs))


def version_blobs(path: Path) -> list[Path]:
    """只数**版本正文**那一份：`.meta.json` 与 `.diff` 是随版本生成的旁件，不是版本。"""
    directory = ahr.artifact_dir(path)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if re.match(r"^\d{4}-", p.name) and not p.name.endswith((".meta.json", ".diff"))
    )


def lit_toolkit(work: Path, task_id: str = "tt-ahw"):
    """点亮 artifact_history 的装配现场——与 build_agent 真实交给车道的句柄同源。"""
    return engine_server.build_toolkit(work, task_id=task_id, extra={"artifact_history": ahr})


def main() -> int:
    print("--- 1. 开关形状：artifact_history 是 knowledge 车道自己的门 ---")
    check("§1 SWITCH_BOARD 把 artifact_history 登记给 knowledge 车道",
          tl.SWITCH_BOARD["knowledge"] == ("knowledge", "artifact_history"),
          str(tl.SWITCH_BOARD["knowledge"]))
    ctx_lit = tl.ToolkitContext(toolkit=None, work_dir=None, base=None, sandbox=None,
                                emit=None, task_id="t", slides=False, office=False,
                                grants=None, pptx_templates=None,
                                extra={"artifact_history": ahr})
    check("§1 装配现场给了句柄时开关读数为真", ctx_lit.artifact_history is ahr)
    ctx_dark = tl.ToolkitContext(toolkit=None, work_dir=None, base=None, sandbox=None,
                                 emit=None, task_id="t", slides=False, office=False,
                                 grants=None, pptx_templates=None)
    check("§1 没给句柄时开关为 None（车道与写入都不动）", ctx_dark.artifact_history is None)
    registered = tl.register_all(
        tl.ToolkitContext(toolkit=None, work_dir=None, base=None, sandbox=None, emit=None,
                          task_id="t", slides=False, office=False, grants=None,
                          pptx_templates=None, extra={"artifact_history": ahr}),
        lane_names=("toolkit_knowledge",))
    check("§1 点亮开关不等于注册工具（W2 车道仍一件不注册）",
          all(not row["registered"] for row in registered), str(registered)[:200])

    print("--- 2. 未点亮＝零行为改变（回执逐字不变，数据根不落历史目录）---")
    work2 = Path(tempfile.mkdtemp(prefix="tt-ahw-dark-"))
    tk2 = engine_server.build_toolkit(work2, task_id="tt-ahw-dark")
    got = call(tk2, "write_text_file", "quiet.txt", "第一版")
    check("§2 未点亮时回执与接线前逐字相同",
          got == f"已成功写入文件: {work2 / 'quiet.txt'}", got[:160])
    call(tk2, "write_text_file", "quiet.txt", "第二版")
    check("§2 未点亮时数据根里不出现 artifact-history",
          not (Path(TEMP_HOME) / ahr.VERSIONS_DIRNAME).exists(),
          str(sorted(p.name for p in (Path(TEMP_HOME) / ahr.VERSIONS_DIRNAME).iterdir())[:5]
               if (Path(TEMP_HOME) / ahr.VERSIONS_DIRNAME).exists() else "不存在"))
    check("§2 未点亮时覆盖照常写入新内容",
          (work2 / "quiet.txt").read_text(encoding="utf-8") == "第二版")

    print("--- 3. 点亮：首次创建 / 覆盖留旧内容 / 去重 ---")
    work3 = Path(tempfile.mkdtemp(prefix="tt-ahw-lit-"))
    tk3 = lit_toolkit(work3)
    first = call(tk3, "write_text_file", "report.md", "初稿：甲")
    check("§3 首次创建的回执如实说明没有旧内容需要留档", "首次创建" in first, first[:200])
    check("§3 首次创建不硬造版本", version_blobs(work3 / "report.md") == [],
          str(version_blobs(work3 / "report.md")))
    second = call(tk3, "write_text_file", "report.md", "二稿：乙")
    blobs = version_blobs(work3 / "report.md")
    check("§3 第二次覆盖留下一版", len(blobs) == 1, str([b.name for b in blobs]))
    check("§3 留下的那一版内容等于第一次盘上那份（不是新写入的）",
          bool(blobs) and blobs[0].read_bytes().decode("utf-8") == "初稿：甲",
          blobs[0].read_text(encoding="utf-8")[:80] if blobs else "无版本")
    check("§3 回执带出版本号", bool(re.search(r"0001", second)), second[:200])
    check("§3 写入结果仍是新内容",
          (work3 / "report.md").read_text(encoding="utf-8") == "二稿：乙")
    # 去重的真实触发条件：即将被盖掉的那一份 == 最近一版（连着两次留到同一份内容）
    (work3 / "report.md").write_text("初稿：甲", encoding="utf-8")
    dup = call(tk3, "write_text_file", "report.md", "三稿：丙")
    check("§3 与最近一版逐字节相同时走去重并说明", "逐字节相同" in dup, dup[:200])
    check("§3 去重把版本号指回已有那一版", "0001" in dup, dup[:200])
    check("§3 去重不增长历史长度", len(version_blobs(work3 / "report.md")) == 1,
          str([b.name for b in version_blobs(work3 / "report.md")]))
    check("§3 去重那次照样把新内容写进盘",
          (work3 / "report.md").read_text(encoding="utf-8") == "三稿：丙")
    other = call(tk3, "write_text_file", "notes.txt", "另一份产物")
    check("§3 另一个产物首次创建同样不硬造版本",
          version_blobs(work3 / "notes.txt") == [], str(version_blobs(work3 / "notes.txt")))
    call(tk3, "write_text_file", "notes.txt", "另一份产物·改")
    check("§3 每个产物各自一棵历史目录（按路径哈希，互不串味）",
          ahr.artifact_dir(work3 / "notes.txt") != ahr.artifact_dir(work3 / "report.md"),
          f"{ahr.artifact_dir(work3 / 'notes.txt')} vs {ahr.artifact_dir(work3 / 'report.md')}")
    check("§3 后一份产物的历史长度独立",
          len(version_blobs(work3 / "notes.txt")) == 1,
          str([b.name for b in version_blobs(work3 / "notes.txt")]))
    check("§3 「另一份产物」那次首次创建的回执也给了原因", "首次创建" in other, other[:160])

    print("--- 4. insert_text_file 同一条闸 ---")
    ins = call(tk3, "insert_text_file", "report.md", "追加一行", None)
    after_insert = version_blobs(work3 / "report.md")
    check("§4 插入前留了当时那份", len(after_insert) == 2, str([b.name for b in after_insert]))
    check("§4 新留的那一版等于插入前的正文",
          bool(after_insert) and after_insert[-1].read_text(encoding="utf-8") == "三稿：丙",
          after_insert[-1].read_text(encoding="utf-8")[:60] if after_insert else "无版本")
    check("§4 插入回执说明留档并保留了原句",
          "已成功更新文件" in ins and "0002" in ins, ins[:200])
    check("§4 插入结果真的落在盘上",
          (work3 / "report.md").read_text(encoding="utf-8").endswith("追加一行"),
          (work3 / "report.md").read_text(encoding="utf-8")[:60])
    missing = call(tk3, "insert_text_file", "nope.txt", "内容", None)
    check("§4 文件不存在时如实报错", "文件不存在" in missing, missing[:160])
    check("§4 文件不存在时不留下半截版本",
          not ahr.artifact_dir(work3 / "nope.txt").exists(),
          str(version_blobs(work3 / "nope.txt")))

    print("--- 5. 单份超限：覆盖照常，原因点名实际字节数 ---")
    big = work3 / "big.bin.txt"
    payload = "x" * (ahr.MAX_TRACK_BYTES + 7)
    first_small = call(tk3, "write_text_file", "big.bin.txt", "小的一版")
    check("§5 首次创建超限文件前不留档（本来就是首次）",
          version_blobs(big) == [], str([b.name for b in version_blobs(big)]))
    big.write_text(payload, encoding="utf-8")  # 绕过工具把盘上撑到超限，模拟既有的大产物
    blobs_before = len(version_blobs(big))
    after = call(tk3, "write_text_file", "big.bin.txt", "新的内容")
    check("§5 超限时覆盖照常完成",
          big.read_text(encoding="utf-8") == "新的内容", "内容没写成")
    check("§5 超限那次如实说明未留档并点名实际字节数与上限",
          "超过单份留档上限" in after and f"{len(payload):,}" in after, after[:240])
    check("§5 超限时没有多写一份版本", len(version_blobs(big)) == blobs_before,
          f"{blobs_before} → {len(version_blobs(big))}")
    check("§5 超限那一版根本不在历史目录里", not ahr.artifact_dir(big).exists(),
          str(ahr.artifact_dir(big)))

    print("--- 6. 故障隔离：留档炸了不许把写入带崩 ---")
    original = ahr.record_current

    def boom(*args, **kwargs):
        raise RuntimeError("故意让留档崩一次")

    ahr.record_current = boom
    try:
        broken = call(tk3, "write_text_file", "crash.txt", "照样要写进去")
    finally:
        ahr.record_current = original
    check("§6 留档异常时写入仍然成功",
          (work3 / "crash.txt").read_text(encoding="utf-8") == "照样要写进去")
    check("§6 留档失败如实说，不假装留了档", "留档失败" in broken, broken[:200])

    print("--- 7. 删除不接版本历史（W4 对分片 §5-c 后半的决策）---")
    before_delete = len(version_blobs(work3 / "report.md"))
    call(tk3, "write_text_file", "todel.txt", "要删的")
    del_blob_count = len(version_blobs(work3 / "todel.txt"))
    deleted = call(tk3, "delete_text_file", "todel.txt", "自测清理")
    check("§7 删除那次没有产生新版本", del_blob_count == 0 and
          len(version_blobs(work3 / "todel.txt")) == 0,
          f"删除前 {del_blob_count} 版 / 删除后 {len(version_blobs(work3 / 'todel.txt'))} 版")
    check("§7 删除有自己的去处（回执不提版本历史）", "版本历史" not in deleted, deleted[:200])
    server_src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    trash_body = server_src.split("def _to_trash", 1)[1].split("def delete_text_file", 1)[0]
    check("§7 `_to_trash` 里确实没调 record_current（两套恢复来源不双写）",
          "record_current" not in trash_body, trash_body[:200])
    check("§7 报告那份的历史在删除动作前后没变",
          len(version_blobs(work3 / "report.md")) == before_delete,
          f"{before_delete} → {len(version_blobs(work3 / 'report.md'))}")

    print("--- 8. 数据根隔离：一切留档都落在临时根里 ---")
    check("§8 引擎数据根就是临时根",
          str(data_paths.engine_home()) == str(Path(TEMP_HOME).resolve()),
          f"{data_paths.engine_home()} vs {Path(TEMP_HOME).resolve()}")
    root = Path(TEMP_HOME) / ahr.VERSIONS_DIRNAME
    check("§8 历史目录落在临时数据根下", root.is_dir(), str(root))
    listed = sorted(p.name for p in root.iterdir())
    check("§8 只有真留下档的产物才占一棵目录（report.md 与 notes.txt 两份）",
          len(listed) == 2, str(listed))
    check("§8 两份产物各一棵目录，都在临时数据根下",
          ahr.artifact_dir(work3 / "report.md").is_dir() and
          ahr.artifact_dir(work3 / "notes.txt").is_dir() and
          all((root / n).is_dir() for n in listed), str(listed))
    user_home = Path.home() / ".tongtianwork" / ahr.VERSIONS_DIRNAME
    check("§8 本轮没有往用户真实数据根写东西",
          not user_home.exists() or str(data_paths.engine_home()) != str(user_home.parent),
          str(user_home))

    print("--- 9. 形状与反向钉（不许把引擎半边读成两条通道都好了）---")
    body = server_src.split("def build_toolkit", 1)[1].split("\ndef ", 1)[0]
    check("§9 两个写入口各自在落盘前调一次留档",
          body.count("_capture_before_write(") == 3, str(body.count("_capture_before_write(")))
    check("§9 装配现场把句柄喂进 extra", '"artifact_history": ahr' in server_src)
    check("§9 留档早于 p.write_text（顺序错就是留了新版而不是旧版）",
          body.index("_capture_before_write(p, \"write_text_file\")") <
          body.index("p.write_text(content"))
    check("§9 未点亮时直接返回空串（写入路径零成本）",
          "if not history_sink:\n            return \"\"" in body, "缺早退")
    rust = (PROJECT_ROOT / "src-tauri" / "src" / "harness" / "tools.rs").read_text(encoding="utf-8")
    check("§9 Rust 内置执行体的写路径本轮**没接**留档（禁区，不许读成两边都好了）",
          "artifact_history" not in rust and "record_current" not in rust,
          "Rust 侧出现了留档调用——说明有人接了，请连同本断言一起更新")
    lane_src = (PROJECT_ROOT / "engine" / "toolkit_knowledge.py").read_text(encoding="utf-8")
    check("§9 knowledge 车道仍未偷偷注册版本历史工具（等 W2 接线）",
          "list_artifact_versions" not in lane_src, lane_src[:200])

    total = len(FAILURES)
    print(f"\nengine artifact history wiring selftest: "
          f"{'ALL PASS' if not total else str(total) + ' FAILED'}")
    for name in FAILURES:
        print(f"[FAILED] {name}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
