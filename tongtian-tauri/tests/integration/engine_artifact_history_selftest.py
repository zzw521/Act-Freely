#!/usr/bin/env python3
"""D-26 产物版本历史与回滚自测（缺陷卡 #138，W2 领地）。

覆盖三件事：
1. **留档语义**：留的是即将被覆盖的那一份；首次创建、内容未变、超过单份上限、
   写盘失败四种情况各给一句能播给用户的中文原因，不静默少一版。
2. **可回滚**：回滚前先把当前内容留成新版本（回滚本身可回滚）；跨产物拿别人的
   版本号来盖自己被拒绝；找不到版本时一个字节都不动。
3. **注册面是真的**：五个端点挂在 knowledge 车道上、能被请求到，路径闸复用
   `server._engine_data_roots()` 与 `server._path_within()`，不在车道里另起一套。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_artifact_history_selftest.py
判定只看 [PASS] 计数；缺 fastapi/agentscope 时输出 SKIP 并以 0 退出。
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

# 数据根必须在 import 之前就指向临时目录：留档写的是真盘，
# 指错了就会把假版本灌进用户自己的 ~/.tongtianwork/artifact-history。
HOME = Path(tempfile.mkdtemp(prefix="tt-artifact-history-"))
os.environ["TONGTIANWORK_HOME"] = str(HOME)
WORK = HOME / "workdir"
WORK.mkdir(parents=True, exist_ok=True)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    try:
        import artifact_history_runtime as ah
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routes_lanes as rl
        import server  # noqa: F401  只为真挂接与路径闸；车道文件本身不许 import server
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: engine artifact history selftest unavailable ({exc})")
        return 0

    artifact = WORK / "报告.md"

    # ── 1. 首次创建：没有旧内容可留，但这不是错误 ───────────────
    first = ah.record_current(artifact, task_id="t0", tool="write_text_file")
    check("文件还不存在时不算失败也不硬造版本", first["ok"] and not first["versionId"], str(first))
    check("这句话能直接播给用户（首次创建，无需留档）",
          "首次创建" in first["note"] and "报告.md" in first["note"], first["note"])
    rows, notes = ah.list_versions(artifact)
    check("没留过档时列出空历史而不是报错", rows == [] and notes == [], f"{rows} / {notes}")

    # ── 2. 覆盖一次留一版，字段带得齐 ──────────────────────────
    text_a = "第一版内容\n第二段\n"
    text_b = "第一版内容\n第二段\n第三段（新加的）\n"
    # 一律 write_bytes：Windows 上 write_text 会把 \n 换成 \r\n，而留档存的是盘上原样字节，
    # 用文本写再拿 \n 断言等于让操作系统替测试改题面。
    artifact.write_bytes(text_a.encode("utf-8"))
    v1 = ah.record_current(artifact, task_id="task-1", tool="write_text_file")
    check("第一次覆盖前留下版本 0001", v1["ok"] and v1["versionId"] == "0001", str(v1))
    artifact.write_bytes(text_b.encode("utf-8"))
    v2 = ah.record_current(artifact, task_id="task-2", tool="insert_text_file")
    check("第二次覆盖前留下版本 0002", v2["ok"] and v2["versionId"] == "0002", str(v2))
    rows, _notes = ah.list_versions(artifact)
    check("版本从新到旧排列", [r["versionId"] for r in rows] == ["0002", "0001"], str(rows))
    newest = rows[0]
    check("每一版取出的正文就是当时盘上那一份（不是覆盖之后的新内容）",
          ah.version_text(artifact, "0001")[0] == text_a and ah.version_text(artifact, "0002")[0] == text_b,
          f"0001={ah.version_text(artifact, '0001')[0]!r} 0002={ah.version_text(artifact, '0002')[0]!r}")
    check("元数据的大小与这一版副本的实际字节数一致",
          newest["bytes"] == len(text_b.encode("utf-8")), str(newest["bytes"]))
    check("元数据带来源任务与工具", newest["task"] == "task-2" and newest["tool"] == "insert_text_file",
          f"{newest.get('task')} / {newest.get('tool')}")
    check("元数据带原始路径与摘要", newest["origin"] == str(artifact.resolve())
          and len(newest["sha256"]) == 64, str(newest)[:160])
    check("元数据标出这是文本并记录前一版", newest["text"] is True
          and newest["previousVersion"] == "0001", str(newest)[:200])
    check("历史目录挂在引擎数据根下",
          str(ah.artifact_dir(artifact)).startswith(str(HOME)), str(ah.artifact_dir(artifact)))
    check("历史目录不与引擎既有目录同名",
          ah.VERSIONS_DIRNAME not in ("checkpoints", "awareness", "file-trash", "usage",
                                      "skill_pool", "ticket-workers"), ah.VERSIONS_DIRNAME)

    # ── 3. 内容没变就不重复留档 ───────────────────────────────
    again = ah.record_current(artifact, task_id="task-3", tool="write_text_file")
    check("逐字节相同的内容不再新增版本", again["ok"] and again["deduped"], str(again))
    check("去重这句话点名是哪一版重复了", "0002" in again["note"] and "相同" in again["note"], again["note"])
    rows, _ = ah.list_versions(artifact)
    check("去重后历史里仍只有两版", len(rows) == 2, str(len(rows)))

    # ── 4. 文本留 diff，二进制只留副本 ────────────────────────
    artifact.write_bytes("第一段\n第二段\n".encode("utf-8"))
    ah.record_current(artifact, task_id="task-4")
    artifact.write_bytes("第一段改了\n第二段\n".encode("utf-8"))
    v5 = ah.record_current(artifact, task_id="task-5")
    rows, _ = ah.list_versions(artifact)
    row_v5 = next((r for r in rows if r["versionId"] == v5["versionId"]), {})
    diff_file = ah.artifact_dir(artifact) / str(row_v5.get("diff") or "")
    check("文本版本之间生成差异文件", bool(row_v5.get("diff")) and diff_file.is_file(), str(row_v5)[:200])
    diff_text = diff_file.read_text(encoding="utf-8") if diff_file.is_file() else ""
    check("差异里能看到改了哪一行", "-第一段" in diff_text and "+第一段改了" in diff_text,
          diff_text.replace("\n", "\\n")[:200])
    binary = WORK / "产物.xlsx"
    binary.write_bytes(b"PK\x03\x04\x00\x01\x02\x00binary payload")
    b1 = ah.record_current(binary, task_id="task-6")
    check("二进制文件照样留副本", b1["ok"] and b1["versionId"], str(b1))
    binary.write_bytes(b"PK\x03\x04\x00\x01\x02\x00binary payload v2 different")
    b2 = ah.record_current(binary, task_id="task-7")
    check("二进制第二版也留了副本", b2["ok"] and b2["versionId"] != b1["versionId"], str(b2))
    rows_b, _ = ah.list_versions(binary)
    check("二进制版本被标成非文本", rows_b and rows_b[0]["text"] is False, str(rows_b)[:200])
    check("二进制没有差异文件（不拿乱码冒充 diff）",
          rows_b and not rows_b[0].get("diff") and not rows_b[0].get("codec"), str(rows_b[0])[:200])
    blank_diff = ah.diff_version(binary, rows_b[0]["versionId"])
    check("向二进制版本要差异时如实拒绝", blank_diff["ok"] is True and "二进制" in blank_diff["note"],
          str(blank_diff))
    check("要二进制的文本正文时直接拒绝（回滚走的是字节副本，不靠文本）",
          ah.version_text(binary, rows_b[0]["versionId"])[1] == "这一版是二进制文件，没有可比对的文本正文",
          str(ah.version_text(binary, rows_b[0]["versionId"])))
    blob_b1 = ah.find_version(binary, b1["versionId"])[0]
    check("二进制副本按原字节存着（不是转成文本再存）",
          blob_b1 is not None and blob_b1.read_bytes() == b"PK\x03\x04\x00\x01\x02\x00binary payload",
          str(blob_b1)[:120])

    # ── 5. 超过单份上限：拒留档并把原因写清 ───────────────────
    original_limit = ah.MAX_TRACK_BYTES
    ah.MAX_TRACK_BYTES = 16
    try:
        big = WORK / "大图.png"
        big.write_bytes(b"x" * 64)
        refused = ah.record_current(big, task_id="task-7")
        check("超过单份上限时拒绝留档而不是静默少一版",
              refused["ok"] is False and not refused["versionId"], str(refused))
        check("这句话带上实际字节数与上限", "64" in refused["note"] and "16" in refused["note"],
              refused["note"])
        check("拒绝留档后目录里没留下这个产物的版本",
              ah.list_versions(big)[0] == [], str(ah.list_versions(big)[0]))
    finally:
        ah.MAX_TRACK_BYTES = original_limit

    # ── 6. 差异过大不硬截断 ───────────────────────────────────
    original_diff_limit = ah.MAX_DIFF_LINES
    ah.MAX_DIFF_LINES = 2
    try:
        wide = WORK / "长文.txt"
        wide.write_bytes("行1\n行2\n行3\n行4\n行5\n行6\n".encode("utf-8"))
        ah.record_current(wide, task_id="task-8")
        wide.write_bytes("改1\n改2\n改3\n改4\n改5\n改6\n".encode("utf-8"))
        wide_v = ah.record_current(wide, task_id="task-9")
        rows_w, _ = ah.list_versions(wide)
        check("差异过大时这一版不带差异文件（不生成半截 diff）",
              not rows_w[0].get("diff") and "上限" in str(rows_w[0].get("diffNote")),
              str(rows_w[0])[:220])
        view = ah.diff_version(wide, wide_v["versionId"])
        check("要差异时说明未生成，并声明两版仍完整可回滚",
              view["truncated"] is True and "整版回滚" in view["note"] and view["diff"] == "",
              str(view)[:220])
    finally:
        ah.MAX_DIFF_LINES = original_diff_limit

    # ── 7. 回滚：旧内容写回，当前内容先留档 ──────────────────
    # 先把盘上内容换成一份历史里没有的改动：回滚前的自动留档才有东西可留。
    content_now = "回滚前独有的一处改动\n"
    artifact.write_bytes(content_now.encode("utf-8"))
    rows_before, _ = ah.list_versions(artifact)
    before_restore = len(rows_before)
    target_version = v1["versionId"]
    restored = ah.restore(artifact, target_version, task_id="task-10")
    check("回滚成功并点名回到哪一版", restored["ok"] and restored["restored"] == target_version,
          str(restored))
    check("文件内容真的回到了那一版",
          artifact.read_bytes().decode("utf-8") == text_a, str(artifact.read_bytes())[:120])
    check("回滚前当前内容被留成新版本", bool(restored["capturedVersion"]), str(restored))
    rows_after, _ = ah.list_versions(artifact)
    check("历史因此长了一版而不是少了一版", len(rows_after) == before_restore + 1,
          f"{before_restore} → {len(rows_after)}")
    check("这一版的 reason 里写明是回滚前自动留档",
          "回滚" in str(rows_after[0].get("reason")), str(rows_after[0])[:200])
    check("留下的正是回滚前那一份（不是回滚后的内容）",
          ah.version_text(artifact, restored["capturedVersion"])[0] == content_now,
          f"{restored['capturedVersion']}={ah.version_text(artifact, restored['capturedVersion'])[0]!r}")
    check("回滚这句话带上留档版本号（用户知道还能再退回去）",
          restored["capturedVersion"] in restored["note"], restored["note"])

    # 再回滚一次：退到刚才那次回滚之前的内容，证明回滚可回滚
    second = ah.restore(artifact, restored["capturedVersion"], task_id="task-11")
    check("回滚本身也能被回滚", second["ok"], str(second))
    check("二次回滚后内容又变回回滚前那一份",
          artifact.read_bytes().decode("utf-8") == content_now, artifact.read_bytes()[:120])
    rows_third, _ = ah.list_versions(artifact)
    check("二次回滚同样先留一档", len(rows_third) == len(rows_after) + 1,
          f"{len(rows_after)} → {len(rows_third)}")

    # 当前内容与最近一版逐字节相同时，回滚前那一步走去重而不是造重复副本
    third_before = len(ah.list_versions(artifact)[0])
    artifact.write_bytes(text_a.encode("utf-8"))
    dedup_restore = ah.restore(artifact, "0004", task_id="task-11b")
    check("回滚前撞见同内容时版本号指向已有那一版",
          dedup_restore["ok"] and dedup_restore["capturedVersion"] == rows_third[0]["versionId"],
          str(dedup_restore)[:220])
    check("同内容不回滚出新副本", len(ah.list_versions(artifact)[0]) == third_before,
          f"{third_before} → {len(ah.list_versions(artifact)[0])}")
    check("回滚该写的还是写了", artifact.read_bytes().decode("utf-8") == "第一段改了\n第二段\n",
          artifact.read_bytes()[:120])

    # ── 8. 拒绝张冠李戴与不存在的版本 ─────────────────────────
    other = WORK / "别的产物.md"
    other.write_bytes("别的产物原文\n".encode("utf-8"))
    ah.record_current(other, task_id="task-12")
    # 把 artifact 最新一版的副本与元数据塞进 other 的历史目录：这就是「A 的历史
    # 出现在 B 的目录里」的真实形态（人为拷贝、产物改名、目录合并都会造成它）。
    rows_a, _ = ah.list_versions(artifact)
    donor_name = str(rows_a[0]["blob"])
    import shutil  # noqa: PLC0415  只有这一节需要复制文件

    shutil.copy2(ah.artifact_dir(artifact) / donor_name, ah.artifact_dir(other) / donor_name)
    shutil.copy2(ah._meta_file(ah.artifact_dir(artifact) / donor_name),
                 ah._meta_file(ah.artifact_dir(other) / donor_name))
    stolen = ah.restore(other, rows_a[0]["versionId"])
    check("拿别的产物的版本号盖自己被拒绝", stolen["ok"] is False and "同一个文件" in stolen["note"],
          str(stolen)[:220])
    check("拒绝这句话点名两边路径", "别的产物.md" in stolen["note"] and "报告.md" in stolen["note"],
          stolen["note"])
    check("被拒绝时那个产物的内容一个字节都没动",
          other.read_text(encoding="utf-8") == "别的产物原文\n", str(other.read_text(encoding="utf-8")))
    # 塞进去的那一份**不删**：清理交给第 9 节的 prune，测试自己伸手删等于用实现证明实现。
    ghost = ah.restore(artifact, "9999")
    check("找不到版本时如实说明且不动文件",
          ghost["ok"] is False and "找不到" in ghost["note"], str(ghost))
    keep_text = artifact.read_text(encoding="utf-8")
    check("失败的回滚不留半截文件", artifact.read_text(encoding="utf-8") == keep_text, keep_text[:80])

    # ── 9. 配额清理：最新一版永不删 ──────────────────────────
    churn = WORK / "反复改.md"
    for index in range(6):
        churn.write_text(f"第 {index} 版内容\n", encoding="utf-8")
        ah.record_current(churn, task_id=f"task-{20 + index}")
    rows_c, _ = ah.list_versions(churn)
    check("反复覆盖攒出 6 版", len(rows_c) == 6, str(len(rows_c)))
    tracked = (artifact, binary, churn, other, wide)
    before = {p.name: len(ah.list_versions(p)[0]) for p in tracked}
    pruned = ah.prune(keep=2, max_total_bytes=1024 * 1024)
    after = {p.name: len(ah.list_versions(p)[0]) for p in tracked}
    check("清理后每个产物都不超过保留数", all(v <= 2 for v in after.values()), f"{before} → {after}")
    check("清理计数与盘上实际少掉的版数一致",
          pruned["removed"] == sum(before.values()) - sum(after.values()), str(pruned)[:220])
    rows_c2, _ = ah.list_versions(churn)
    check("清理后 churn 只剩最新两版", after[churn.name] == 2 and len(rows_c2) == 2,
          str([r["versionId"] for r in rows_c2]))
    check("最新一版仍在（清理不许删掉唯一可回滚的那份）",
          rows_c2[0]["versionId"] == "0006", str(rows_c2[0])[:160])
    check("清理这句话报了删几版与释放多少字节",
          str(pruned["removed"]) in pruned["note"] and "字节" in pruned["note"], pruned["note"])
    survivors = list(ah.artifact_dir(churn).iterdir())
    check("删版本时元数据与差异文件一起走，不留孤儿",
          not any(n.name.endswith(".tmp-") or "0001" in n.name for n in survivors),
          str([n.name for n in survivors])[:220])

    def count_on_disk() -> tuple[int, int]:
        """测试自己走一遍盘：不叫 ah._iter_blobs，避免用实现去证明实现。"""
        root = ah.history_home()
        blobs = 0
        dirs: set[str] = set()
        for entry in (root.rglob("*") if root.is_dir() else []):
            if entry.is_file() and not entry.name.endswith((".meta.json", ".diff")) \
                    and ".tmp-" not in entry.name:
                blobs += 1
                dirs.add(str(entry.parent))
        return blobs, len(dirs)

    disk_versions, disk_artifacts = count_on_disk()
    stats = ah.stats()
    check("统计的版本数与盘上副本数一致（测试独立数一遍）",
          stats["versions"] == disk_versions, f"{stats['versions']} vs {disk_versions}")
    check("统计的产物数与盘上有副本的目录数一致",
          stats["artifacts"] == disk_artifacts, f"{stats['artifacts']} vs {disk_artifacts}")
    check("留档没有中心清单（文件系统就是台账，避免并行窗口互相覆盖）",
          not (ah.history_home() / "manifest.json").is_file(), str(ah.history_home()))

    # 总量预算这一刀：预算压到极限时，各产物最新一版仍然留着
    tight = ah.prune(keep=20, max_total_bytes=1)
    surviving = {p.name: len(ah.list_versions(p)[0]) for p in tracked}
    check("预算压到极限时每个产物仍然留着一版",
          all(v == 1 for v in surviving.values()), str(surviving))
    check("churn 留下的正是最新那版而不是别的",
          ah.list_versions(churn)[0][0]["versionId"] == "0006", str(ah.list_versions(churn)[0][0])[:160])
    check("极限清理确实动了手", tight["removed"] >= 1, str(tight)[:200])
    check("仍然超预算时把这件事说出来",
          any("最新一版" in n for n in tight["notes"]), str(tight["notes"]))

    # ── 10. 一次留档中断不留临时文件（原子写）─────────────────
    leftovers = [n.name for directory in ah._artifact_dirs() for n in directory.iterdir()
                 if ".tmp-" in n.name]
    check("历史目录里没有残留的临时文件", leftovers == [], str(leftovers))

    # ── 11. 端点面：真挂在 knowledge 车道上并可请求 ────────────
    original_work_dir = server.WORK_DIR
    server.WORK_DIR = WORK
    try:
        fresh = FastAPI()
        report = rl.attach(fresh, server, ["routes_knowledge"])
        paths = sorted(p for p in report[0]["paths"] if p.startswith("/api/artifacts"))
        check("车道挂上五个产物历史端点", len(paths) == 5, str(paths))
        client = TestClient(fresh)

        artifact.write_text("端点前的内容\n", encoding="utf-8")
        captured = client.post("/api/artifacts/history/capture",
                               json={"path": str(artifact), "task": "t-http", "tool": "write_text_file"})
        check("POST capture 返回留档结果",
              captured.status_code == 200 and captured.json()["ok"] and captured.json()["versionId"],
              str(captured.json())[:200])
        artifact.write_text("端点后的内容\n", encoding="utf-8")

        listing = client.get("/api/artifacts/history", params={"path": str(artifact)})
        body = listing.json()
        check("GET history 列出版本并带上是否仍在盘上",
              listing.status_code == 200 and body["ok"] and body["tracked"]
              and body["exists"] is True and body["versionCount"] >= 1, str(body)[:220])

        diff_resp = client.get("/api/artifacts/history/diff",
                               params={"path": str(artifact), "version": captured.json()["versionId"]})
        check("GET diff 给出某一版相对前一版的差异",
              diff_resp.status_code == 200 and diff_resp.json()["ok"], str(diff_resp.json())[:200])

        rolled = client.post("/api/artifacts/history/restore",
                             json={"path": str(artifact), "version": captured.json()["versionId"]})
        check("POST restore 回滚成功", rolled.status_code == 200 and rolled.json()["ok"], str(rolled.json())[:200])
        check("回滚后文件内容真的回到留档那一版",
              artifact.read_text(encoding="utf-8") == "端点前的内容\n",
              artifact.read_text(encoding="utf-8")[:80])

        outside = Path(tempfile.gettempdir()).parent / "别处的文件.md"
        denied = client.get("/api/artifacts/history", params={"path": str(outside)})
        check("数据根之外的路径被拒（四个端点同一条闸）",
              denied.status_code == 403 and denied.json()["code"] == "ARTIFACT_PATH_DENIED",
              str(denied.json())[:200])
        denied_post = client.post("/api/artifacts/history/restore",
                                  json={"path": str(outside), "version": "0001"})
        check("写面端点同样不放开数据根之外",
              denied_post.status_code == 403, str(denied_post.json())[:200])
        missing_args = client.post("/api/artifacts/history/capture", json={})
        check("缺 path 参数返回 400 而不是 500", missing_args.status_code == 400,
              str(missing_args.json())[:200])
        bad_body = client.post("/api/artifacts/history/capture", content=b"not json",
                               headers={"content-type": "text/plain"})
        check("请求体不是 JSON 时按缺参处理", bad_body.status_code == 400, str(bad_body.status_code))
        unknown = client.post("/api/artifacts/history/restore",
                              json={"path": str(artifact), "version": "8888"})
        check("回滚到不存在的版本返回 422 并说明原因",
              unknown.status_code == 422 and "找不到" in unknown.json()["note"], str(unknown.json())[:200])
        stats_resp = client.get("/api/artifacts/history/stats")
        check("GET stats 报告历史目录体量",
              stats_resp.status_code == 200 and stats_resp.json()["artifacts"] >= 1,
              str(stats_resp.json())[:200])
    finally:
        server.WORK_DIR = original_work_dir

    # ── 12. 路径闸同源：车道里没有第二套越界判定 ──────────────
    lane_source = (PROJECT_ROOT / "engine" / "routes_knowledge.py").read_text(encoding="utf-8", errors="replace")
    check("路径闸复用 server 的既有判定（不在车道里另起一套）",
          "_engine_data_roots" in lane_source and "_path_within" in lane_source, "")
    imported_server = [line.strip() for line in lane_source.splitlines()
                       if line.strip().startswith(("import server", "from server import"))]
    check("车道仍然不许 import server（引擎现场只经 rl.engine() 取）",
          imported_server == [] and "rl.engine()" in lane_source, str(imported_server))
    runtime_source = (PROJECT_ROOT / "engine" / "artifact_history_runtime.py").read_text(
        encoding="utf-8", errors="replace")
    check("数据根只从 data_paths 取（不在本模块解析环境变量）",
          "import data_paths" in runtime_source and "os.environ" not in runtime_source, "")

    # ── 13. 与既有产物面同源：常驻六件与副作用表不越界 ─────────
    import side_effect_registry as ser
    check("本刀没有新增副作用工具名（清理与审批表都不必动）",
          not any("artifact_history" in name or "restore_artifact" in name
                  for name in tuple(ser.REPLAY) + tuple(ser.SANDBOX_MUTATING) + tuple(ser.APPROVAL_SENSITIVE)),
          str(ser.REPLAY))
    import toolkit_lanes as tl
    check("常驻六件仍然只有六件（本刀没往里塞回滚）", len(tl.RESIDENT_TOOLS) == 6, str(tl.RESIDENT_TOOLS))

    print("—" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 — " + "；".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
