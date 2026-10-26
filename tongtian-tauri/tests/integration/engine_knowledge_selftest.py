#!/usr/bin/env python3
"""
本地知识库 RAG 自测

锁住「知识库落盘 → 建索引 → 检索工具 → 模型可见引用」这条链：
1. 中文二元组与英文词元都能检索到，并返回出处标记
2. 增量索引：正文变化后命中新内容；文档移除后不再命中
3. 没有可用知识库时不注册工具、不写提示词
4. /api/knowledge/search 只接受本机知识库根目录下的库
5. 检索工具真的注册进 Toolkit，返回带引用的片段

用法: python tests/integration/engine_knowledge_selftest.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

import knowledge_runtime as kn  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_base(home: Path, kb_id: str, name: str, docs: dict[str, str]) -> Path:
    base = home / "knowledge" / kb_id
    (base / "docs").mkdir(parents=True)
    (base / "text").mkdir(parents=True)
    now = 1_700_000_000_000
    (base / "meta.json").write_text(json.dumps({
        "id": kb_id,
        "name": name,
        "description": "测试库",
        "enabled": True,
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": None,
    }, ensure_ascii=False), encoding="utf-8")
    for doc_name, text in docs.items():
        (base / "docs" / doc_name).write_text(text, encoding="utf-8")
        (base / "text" / f"{doc_name}.txt").write_text(text, encoding="utf-8")
    return base


def main():
    home = Path(tempfile.mkdtemp(prefix="tt-kb-selftest-"))
    try:
        # ── 切块与分词 ────────────────────────────────────────
        text = "差旅报销标准\n\n一线城市住宿每晚上限 600 元。" + "说明。" * 400
        chunks = kn.chunk_text(text)
        check("长文被切成多块且每块不超上限", len(chunks) > 1 and all(len(c) <= kn.MAX_CHUNK_CHARS for c in chunks), str([len(c) for c in chunks]))
        tokens = kn.index_tokens("差旅报销 600 元 Business Trip")
        for expected in ("差旅", "报销", "business", "trip", "600"):
            check(f"索引包含词元 {expected}", expected in tokens.split(), tokens[:200])

        base = make_base(home, "kb-travel", "差旅制度", {
            "差旅报销标准.md": "差旅报销标准\n\n一线城市住宿每晚上限 600 元，二线城市 400 元。\n\n超标需直属主管审批。",
            "年假规定.md": "年假规定\n\n入职满一年享 5 天年假，满十年 10 天。",
        })
        db = base / kn.INDEX_FILE
        check("索引文件尚未生成", not db.exists())

        results, notes = kn.search_base(base, "一线城市住宿标准")
        check("中文检索命中", bool(results), str(notes))
        check("命中差旅文档", results and results[0]["doc"] == "差旅报销标准.md", str(results[:1]))
        check("片段包含 600 元", results and "600" in results[0]["text"], str(results[:1]))
        check("索引已落盘", db.exists())

        results, _ = kn.search_base(base, "annual leave")
        check("英文无命中的诚实结果", results == [], str(results))
        results, _ = kn.search_base(base, "年假")
        check("第二篇文档可检索", results and results[0]["doc"] == "年假规定.md", str(results[:1]))

        # 增量：改正文后命中新值；改 mtime 让签名变化
        changed = base / "text" / "差旅报销标准.md.txt"
        changed.write_text("差旅报销标准\n\n一线城市住宿每晚上限 800 元。", encoding="utf-8")
        stat = changed.stat()
        os.utime(changed, (stat.st_atime, stat.st_mtime + 5))
        results, _ = kn.search_base(base, "一线城市住宿上限")
        check("正文变化后命中新内容", results and "800" in results[0]["text"], str(results[:1]))
        check("旧内容已被替换", all("600" not in r["text"] for r in results), str(results[:2]))

        # 文档移除：索引里不应再命中
        (base / "text" / "年假规定.md.txt").unlink()
        (base / "docs" / "年假规定.md").unlink()
        results, _ = kn.search_base(base, "年假")
        check("移除的文档不再命中", results == [], str(results))

        # ── 检索质量：标题召回与覆盖度排序 ────────────────────
        rank_base = make_base(home, "kb-rank", "排序测试", {
            "季度复盘2026.md": "营收同比增长 12%，华东贡献最大。",
            "住宿说明.md": "住宿 " * 120 + "常见问题",
            "差旅标准.md": "住宿每晚上限 600 元，标准按城市分级。",
        })
        results, _ = kn.search_base(rank_base, "季度复盘")
        check("文档名参与召回（正文不含查询词也能命中）",
              bool(results) and results[0]["doc"] == "季度复盘2026.md",
              str([(r["doc"], r["score"]) for r in results]))
        results, _ = kn.search_base(rank_base, "住宿 标准")
        check("多词覆盖优先于单词重复",
              bool(results) and results[0]["doc"] == "差旅标准.md",
              str([(r["doc"], r["score"]) for r in results]))

        # ── 运行时与工具注册 ──────────────────────────────────
        runtime = kn.KnowledgeRuntime.from_runtime({
            "knowledge": {"bases": [{
                "id": "kb-travel", "name": "差旅制度", "description": "报销与差旅",
                "dir": str(base), "docCount": 1,
            }]},
        })
        check("运行时可用", runtime.ready())
        hint = runtime.system_hint()
        check("提示词列出知识库与引用要求", "差旅制度" in hint and "search_knowledge" in hint, hint)
        check("提示词说明可读全文", "read_knowledge_document" in hint, hint)
        result = runtime.search("住宿标准")
        check("检索结果带出处", result["results"] and "【差旅制度 / 差旅报销标准.md #1】" in result["results"][0]["citation"], str(result["results"][:1]))

        disabled = kn.KnowledgeRuntime.from_runtime({"knowledge": {"bases": []}})
        check("没有库时不可用且不注册", not disabled.ready() and disabled.system_hint() == "")

        # ── 读全文：分页、候选、跨库同名、路径校验 ────────────
        long_text = "开头。\n\n" + "中间内容。" * 3000 + "结尾。"
        (rank_base / "docs" / "长文档.md").write_text(long_text, encoding="utf-8")
        (rank_base / "text" / "长文档.md.txt").write_text(long_text, encoding="utf-8")
        dup_base = make_base(home, "kb-dup", "重复库", {
            "季度复盘2026.md": "另一份季度复盘，口径不同。",
        })
        read_runtime = kn.KnowledgeRuntime.from_runtime({"knowledge": {"bases": [
            {"id": "kb-rank", "name": "排序测试", "dir": str(rank_base), "docCount": 4},
            {"id": "kb-dup", "name": "重复库", "dir": str(dup_base), "docCount": 1},
        ]}})
        page = read_runtime.read_document("长文档.md", max_chars=1000)
        check("读全文返回分页信息",
              page["text"] == long_text[:1000]
              and page["totalChars"] == len(long_text)
              and page["nextOffset"] == 1000,
              str({k: page.get(k) for k in ("offset", "nextOffset", "totalChars")}))
        page2 = read_runtime.read_document("长文档.md", offset=page["nextOffset"], max_chars=1000)
        check("续读从上次位置继续", page2["offset"] == 1000 and page2["text"] == long_text[1000:2000], str(page2.get("offset")))
        tail = read_runtime.read_document("长文档.md", offset=len(long_text) - 100, max_chars=1000)
        check("末页没有 nextOffset", tail["nextOffset"] is None and tail["text"] == long_text[-100:], str(tail.get("nextOffset")))
        check(".txt 副档名按同一篇读",
              read_runtime.read_document("长文档.md.txt", max_chars=500)["doc"] == "长文档.md")
        bad = read_runtime.read_document("../../meta.json")
        check("路径穿越被拒绝", bad["text"] == "" and any("不合法" in n for n in bad["notes"]), str(bad["notes"]))
        missing = read_runtime.read_document("季度复盘2025")
        check("缺失文档列出候选而不是编造",
              missing["text"] == "" and any("季度复盘2026.md" in n for n in missing["notes"]),
              str(missing["notes"]))
        ambiguous = read_runtime.read_document("季度复盘2026.md")
        check("同名文档跨库时要求指定库",
              ambiguous["text"] == "" and any("多个知识库" in n for n in ambiguous["notes"]),
              str(ambiguous["notes"]))
        chosen = read_runtime.read_document("季度复盘2026.md", base_id="kb-dup")
        check("指定知识库后读到正确正文", "另一份季度复盘" in chosen["text"], chosen["text"][:80])
        beyond = read_runtime.read_document("长文档.md", offset=len(long_text) + 5)
        check("offset 越界如实说明",
              beyond["text"] == "" and any("超出文档" in n for n in beyond["notes"]), str(beyond["notes"]))

        class FakeToolkit:
            def __init__(self):
                self.fns = []

            def register_tool_function(self, fn):
                self.fns.append(fn)

        toolkit = FakeToolkit()
        runtime.install(toolkit)
        check("检索与读全文工具都注册",
              [getattr(f, "__name__", "") for f in toolkit.fns]
              == ["search_knowledge", "read_knowledge_document"],
              str(toolkit.fns))
        empty_toolkit = FakeToolkit()
        disabled.install(empty_toolkit)
        check("无可用库时不注册工具", empty_toolkit.fns == [])

        try:
            resp = toolkit.fns[0]("住宿每晚上限")
            body = "\n".join(
                b.get("text", "") for b in resp.content if isinstance(b, dict)
            )
            check("工具返回带引用的片段", "【差旅制度" in body and "800" in body, body[:300])

            read_toolkit = FakeToolkit()
            read_runtime.install(read_toolkit)
            read_resp = read_toolkit.fns[1]("长文档.md", max_chars=600)
            read_body = "\n".join(
                b.get("text", "") for b in read_resp.content if isinstance(b, dict)
            )
            check("读全文工具返回分页与续读提示",
                  "第 1–600 字" in read_body and "offset=600" in read_body,
                  read_body[:200])
            miss_resp = read_toolkit.fns[1]("季度复盘2025")
            miss_body = "\n".join(
                b.get("text", "") for b in miss_resp.content if isinstance(b, dict)
            )
            check("读全文工具缺失时给候选", "没有读到" in miss_body and "季度复盘2026.md" in miss_body,
                  miss_body[:200])
        except Exception as exc:
            print(f"SKIP: agentscope ToolResponse unavailable ({exc})")

        # ── 执行面装配 ────────────────────────────────────────
        try:
            from agentscope.model import ChatModelBase, ChatResponse
            from agentscope.formatter import DashScopeChatFormatter
            import server as engine_server

            class DummyModel(ChatModelBase):
                def __init__(self):
                    super().__init__(model_name="fake-knowledge-model", stream=False)

                async def __call__(self, messages, *args, **kwargs):
                    return ChatResponse(content=[{"type": "text", "text": "ok"}], usage=None)

            engine_server.build_model_and_formatter = (
                lambda *a, **k: (DummyModel(), DashScopeChatFormatter())
            )
            knowledge_runtime = kn.KnowledgeRuntime.from_runtime({"knowledge": {"bases": [{
                "id": "kb-rank", "name": "排序测试", "description": "",
                "dir": str(rank_base), "docCount": 4,
            }]}})
            agent = engine_server.build_agent(
                None, home, None, runtime={"skills": []}, knowledge=knowledge_runtime,
            )
            tool_names = set(agent.toolkit.tools.keys())
            check("执行面同时注册检索与读全文工具",
                  {"search_knowledge", "read_knowledge_document"} <= tool_names,
                  str(sorted(tool_names)))
            check("执行面提示词要求按需读全文",
                  "read_knowledge_document" in (getattr(agent, "sys_prompt", "") or ""))
        except Exception as exc:
            print(f"SKIP: engine agent wiring unavailable ({exc})")

        # ── 路由自测 ──────────────────────────────────────────
        try:
            from fastapi.testclient import TestClient
            import server as engine_server

            os.environ["TONGTIANWORK_HOME"] = str(home)
            client = TestClient(engine_server.app)
            resp = client.post("/api/knowledge/search", json={
                "dir": str(base), "name": "差旅制度", "query": "住宿标准", "topK": 3,
            })
            data = resp.json()
            check("检索接口 200", resp.status_code == 200, str(data))
            check("接口返回命中与出处", data.get("ok") and data.get("results") and "citation" in data["results"][0], str(data))

            outside = home / "outside"
            outside.mkdir()
            (outside / "meta.json").write_text("{}", encoding="utf-8")
            resp = client.post("/api/knowledge/search", json={
                "dir": str(outside), "query": "任意",
            })
            check("根目录外拒绝检索", resp.status_code == 400, str(resp.json()))
            resp = client.post("/api/knowledge/search", json={"dir": str(base), "query": ""})
            check("空检索词被拒绝", resp.status_code == 400, str(resp.json()))
        except Exception as exc:
            print(f"SKIP: engine route selftest unavailable ({exc})")
    finally:
        os.environ.pop("TONGTIANWORK_HOME", None)
        import shutil
        shutil.rmtree(home, ignore_errors=True)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} 项 — " + "；".join(FAILURES))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
