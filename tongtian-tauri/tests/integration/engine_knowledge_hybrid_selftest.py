#!/usr/bin/env python3
"""知识库混合检索自测（缺陷 D-09）

锁住「离线优先 → 如实降级 → 向量并集 → 重排 → 扫描件状态」这条链：
1. 嵌入供应商在没有任何语义模型时退回内置词面向量，并且**说明里写清不是语义模型**
2. 向量表随正文索引增量构建；换供应商整表重建；正文删掉后向量里的孤儿段被清掉
3. 正文已经不存在的段落不许以「命中」的形式返回（空正文幽灵结果）
4. MMR 把同一文档的近重复段压掉，让结果集换来源
5. 局部上下文扩展只用库内正文，候选不足时宁可不扩展
6. 每条命中带 retrieval 出处；lexical 模式下必须一条向量都不走
7. docs/ 里有原件但 text/ 抽不出正文 → 如实播报「未提取到文本（多为扫描件）」
8. 嵌入调用失败时检索照样出词法结果，并把降级原因带回来

用法: python tests/integration/engine_knowledge_hybrid_selftest.py
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
import knowledge_vector_runtime as kv  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_base(home: Path, kb_id: str, docs: dict[str, str], raw: dict[str, str] | None = None) -> Path:
    base = home / "knowledge" / kb_id
    (base / "docs").mkdir(parents=True)
    (base / "text").mkdir(parents=True)
    for doc_name, text in docs.items():
        (base / "docs" / doc_name).write_text(text, encoding="utf-8")
        (base / "text" / f"{doc_name}.txt").write_text(text, encoding="utf-8")
    for doc_name, text in (raw or {}).items():
        # 只有原件、没有抽取正文 —— 扫描件形状
        (base / "docs" / doc_name).write_text(text, encoding="utf-8")
    return base


class BoomProvider(kv.EmbeddingProvider):
    """嵌入调用直接失败的供应商：验证降级路径而不是验证成功路径。"""

    def __init__(self):
        super().__init__(key="boom", label="总会失败的模型", semantic=True,
                         dimension=8, source="test")

    def embed(self, texts):
        raise RuntimeError("上游返回 503")


class ConstProvider(kv.EmbeddingProvider):
    """固定向量的供应商：用来验证「换供应商即重建」而不掺杂相似度语义。"""

    def __init__(self, key: str):
        super().__init__(key=key, label=f"常量向量 {key}", semantic=False,
                         dimension=4, source="test")

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def main():
    home = Path(tempfile.mkdtemp(prefix="tt-kb-hybrid-"))
    env_backup = {k: os.environ.get(k) for k in (
        kv.EMBED_BASE_URL_ENV, kv.EMBED_MODEL_ENV, kv.EMBED_API_KEY_ENV,
        "TT_EMBEDDING_LOCAL_MODEL", kn.RETRIEVAL_ENV, "TONGTIANWORK_HOME",
    )}
    try:
        for key in env_backup:
            os.environ.pop(key, None)

        # ── 1. 离线优先与如实降级 ──────────────────────────────
        provider, notes = kv.resolve_provider()
        check("无外部配置时选到内置词面向量",
              isinstance(provider, kv.HashedNgramProvider), str(provider))
        check("内置向量被标为非语义", provider is not None and provider.semantic is False)
        joined = "；".join(notes)
        check("说明里点名本机没有语义模型", "没有可用的语义嵌入模型" in joined, joined)
        check("说明里给出维度", f"{kv.HASH_DIMENSION} 维" in joined, joined)
        check("默认降级文案不含密钥形态的字符串", not api_key_leak(joined), joined)

        off, off_notes = kv.resolve_provider("lexical")
        check("配置关闭时不返回供应商", off is None, str(off))
        check("关闭时如实说明", any("关闭向量召回" in n for n in off_notes), str(off_notes))

        remote, remote_notes = kv.resolve_provider("remote")
        check("未配端点时按声明降级而不是报错", isinstance(remote, kv.HashedNgramProvider), str(remote))
        check("降级原因点名缺哪个环境变量",
              any(kv.EMBED_BASE_URL_ENV in n for n in remote_notes), str(remote_notes))

        os.environ[kv.EMBED_BASE_URL_ENV] = "not-a-url"
        os.environ[kv.EMBED_MODEL_ENV] = "demo"
        bad, bad_notes = kv.resolve_provider("remote")
        check("端点地址不合法时拒绝使用并降级",
              isinstance(bad, kv.HashedNgramProvider)
              and any("不合法" in n for n in bad_notes), str(bad_notes))
        os.environ.pop(kv.EMBED_BASE_URL_ENV)
        os.environ.pop(kv.EMBED_MODEL_ENV)

        # ── 2. 词面向量的确定性 ────────────────────────────────
        vec_a = provider.embed(["差旅住宿每晚上限 600 元"])
        vec_b = provider.embed(["差旅住宿每晚上限 600 元"])
        check("同一输入两次嵌入完全一致（可复现）", vec_a == vec_b, str(vec_a)[:120])
        check("维度等于声明值", len(vec_a[0]) == provider.dimension, str(len(vec_a[0])))
        norm = sum(v * v for v in vec_a[0]) ** 0.5
        check("向量已 L2 归一", abs(norm - 1.0) < 1e-6, str(norm))
        check("自身余弦为 1", abs(kv.cosine(vec_a[0], vec_b[0]) - 1.0) < 1e-6)
        check("不同文本余弦低于自身",
              kv.cosine(vec_a[0], provider.embed(["年假规定与调休申请"])[0]) < 0.99,
              str(kv.cosine(vec_a[0], provider.embed(["年假规定与调休申请"])[0])))
        check("空文本不产生伪命中", kv.cosine(provider.embed([""])[0], vec_a[0]) == 0.0)
        check("维度不一致的向量判为不可比", kv.cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0)

        # ── 3. 向量索引增量、重建与孤儿清理 ────────────────────
        base = make_base(home, "kb-h", {
            "差旅报销标准.md": "差旅报销标准\n\n一线城市住宿每晚上限 600 元，二线城市 400 元。\n\n超标需直属主管审批。",
            "年假规定.md": "年假规定\n\n入职满一年享 5 天年假，满十年 10 天。",
        })
        conn, _ = kn.ensure_index(base)
        usable, build_notes = kv.sync_vector_index(conn, provider)
        check("首轮建起向量索引", usable is True, str(build_notes))
        count_1 = conn.execute(f"SELECT COUNT(*) FROM {kv.VECTOR_TABLE}").fetchone()[0]
        usable2, _ = kv.sync_vector_index(conn, provider)
        count_2 = conn.execute(f"SELECT COUNT(*) FROM {kv.VECTOR_TABLE}").fetchone()[0]
        check("重复同步不重复建向量", count_1 == count_2 and count_1 > 0, f"{count_1}/{count_2}")
        stored = kv._stored_provider(conn)
        check("索引里记下所用供应商签名", stored == provider.key, f"{stored} != {provider.key}")

        const = ConstProvider("const-v1")
        kv.sync_vector_index(conn, const)
        check("换供应商后旧向量整表清空",
              conn.execute(
                  f"SELECT COUNT(*) FROM {kv.VECTOR_TABLE} WHERE provider=?", (provider.key,)
              ).fetchone()[0] == 0,
              str(conn.execute(f"SELECT provider, COUNT(*) FROM {kv.VECTOR_TABLE} GROUP BY provider").fetchall()))
        check("新供应商签名落进元数据", kv._stored_provider(conn) == const.key, kv._stored_provider(conn))

        # 孤儿清理：正文索引删掉一段后，向量表不能留着它
        kv.sync_vector_index(conn, provider)
        with conn:
            conn.execute("DELETE FROM plain_chunks WHERE doc='年假规定.md'")
        before = conn.execute(f"SELECT COUNT(*) FROM {kv.VECTOR_TABLE}").fetchone()[0]
        prune_notes = kv.sync_vector_index(conn, provider)[1]
        ghosts = conn.execute(
            f"SELECT COUNT(*) FROM {kv.VECTOR_TABLE} v WHERE NOT EXISTS "
            "(SELECT 1 FROM plain_chunks p WHERE p.doc=v.doc AND p.seq=v.seq)"
        ).fetchone()[0]
        check("正文删除后向量孤儿被清掉", ghosts == 0 and before > 0,
              f"before={before} ghosts={ghosts}")
        check("清理孤儿这件事有可读说明",
              any("已不存在于正文索引的向量" in n for n in prune_notes), str(prune_notes))
        conn.close()

        # 空正文不冒充命中
        conn, _ = kn.ensure_index(base)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO plain_chunks(doc, seq, body) VALUES('空段.md', 0, '   ')"
            )
        kv.sync_vector_index(conn, provider)
        found, empty_notes = kv.recall_vectors(conn, provider, "住宿 上限 发票")
        check("正文为空的多段落不进向量结果",
              ("空段.md", 0) not in found, str(list(found.keys())))
        check("向量结果里每条都带可展示正文",
              all((item.get("body") or "").strip() for item in found.values()),
              str(list(found.keys())))
        conn.close()

        # ── 4. 端到端混合检索：出处标记与模式开关 ──────────────
        hybrid_base = make_base(home, "kb-x", {
            "差旅报销标准.md": "差旅报销标准\n\n一线城市住宿每晚上限 600 元，二线城市 400 元。",
            "年假规定.md": "年假规定\n\n入职满一年享 5 天年假，满十年 10 天。",
        })
        results, notes = kn.search_base(hybrid_base, "一线城市住宿上限", retrieval="hybrid")
        check("混合检索命中并带 retrieval 字段",
              bool(results) and all("retrieval" in r for r in results), str(results[:1]))
        check("命中来源被如实标注",
              all(r["retrieval"] in ("lexical", "vector", "hybrid") for r in results),
              str([r.get("retrieval") for r in results]))

        results_lex, notes_lex = kn.search_base(hybrid_base, "一线城市住宿上限", retrieval="lexical")
        check("lexical 模式不产生向量命中",
              all(r["retrieval"] == "lexical" for r in results_lex),
              str([r.get("retrieval") for r in results_lex]))
        check("lexical 模式没有向量说明",
              not any("向量召回启用" in n or "词面向量" in n for n in notes_lex), str(notes_lex))
        check("两种模式都能命中同一问题",
              bool(results_lex) and bool(results)
              and results[0]["doc"] == results_lex[0]["doc"] == "差旅报销标准.md",
              f"{[r['doc'] for r in results[:2]]} vs {[r['doc'] for r in results_lex[:2]]}")

        os.environ[kn.RETRIEVAL_ENV] = "lexical"
        results_env, _ = kn.search_base(hybrid_base, "一线城市住宿上限")
        check("环境变量能整体关闭混合", all(r["retrieval"] == "lexical" for r in results_env),
              str([r.get("retrieval") for r in results_env]))
        os.environ[kn.RETRIEVAL_ENV] = "nonsense"
        check("非法模式值退回默认而不是报错", kn.retrieval_mode() == kn.DEFAULT_RETRIEVAL,
              kn.retrieval_mode())
        os.environ.pop(kn.RETRIEVAL_ENV)

        # ── 5. 混合排序的性质：同文档上限与可复现 ──────────────
        long_doc = "\n\n".join(
            [f"住宿每晚上限 600 元，超标需直属主管审批。片段{i}。" for i in range(8)]
        )
        dup_base = make_base(home, "kb-dup", {
            "住宿细则.md": long_doc,
            "结算口径.md": "住宿每晚上限 600 元，需直属主管审批。部门统一结算，发票齐全后入账。",
        })
        pool_results, _ = kn.search_base(dup_base, "住宿每晚上限 超标审批", top_k=5, retrieval="hybrid")
        per_doc = {}
        for row in pool_results:
            per_doc[row["doc"]] = per_doc.get(row["doc"], 0) + 1
        check("一份长文档吃不掉整个结果集",
              bool(pool_results) and max(per_doc.values()) <= kn.MAX_CHUNKS_PER_DOC,
              str(per_doc))
        check("混合检索两次给出同样的顺序（可复现）",
              [(r["doc"], r["seq"]) for r in pool_results]
              == [(r["doc"], r["seq"]) for r in kn.search_base(
                  dup_base, "住宿每晚上限 超标审批", top_k=5, retrieval="hybrid")[0]],
              str([(r["doc"], r["seq"]) for r in pool_results]))
        fused = kv.fuse_candidates(
            [("甲.md", 0, "住宿每晚上限 600 元。", 5.0), ("甲.md", 1, "超标需审批。", 3.0)],
            {("乙.md", 0): {"body": "部门统一结算。", "sim": 0.42, "vector": [0.1, 0.2]}},
        )
        check("两条召回并集后候选不丢", len(fused) == 3, str(list(fused.keys())))
        check("并集只有一边命中的段落保留另一边分数",
              fused[("甲.md", 0)]["lex"] == 5.0 and fused[("甲.md", 0)]["vec"] == 0.0
              and fused[("乙.md", 0)]["vec"] == 0.42, str(fused[("乙.md", 0)]))
        check("同一段落两边都命中时两个分都在",
              kv.fuse_candidates(
                  [("丙.md", 0, "正文", 2.0)],
                  {("丙.md", 0): {"body": "正文", "sim": 0.9, "vector": []}},
              )[("丙.md", 0)]["lex"] == 2.0)
        peak_scores = kv.hybrid_score(
            {"doc": "甲.md", "body": "住宿每晚上限 600 元。", "lex": 5.0, "vec": 0.5},
            5.0, kn.query_tokens("住宿 上限"), [], kn._coverage,
        )
        zero_vec = kv.hybrid_score(
            {"doc": "甲.md", "body": "住宿每晚上限 600 元。", "lex": 5.0, "vec": 0.0},
            5.0, kn.query_tokens("住宿 上限"), [], kn._coverage,
        )
        check("向量分只加分不改词法档", peak_scores > zero_vec, f"{peak_scores} vs {zero_vec}")
        check("融合后没有候选时混合让位给词法", kv.fuse_candidates([], {}) == {})

        # ── 6. 局部上下文扩展的边界 ────────────────────────────
        expanded = kv.expansion_terms(["住宿", "发票"], [
            "住宿每晚上限 600 元，需附发票原件。",
            "住宿与交通都按每晚上限计算，需附发票后入账。",
        ])
        check("扩展词来自库内共现", bool(expanded), str(expanded))
        check("扩展词确实是两段共有的说法",
              {"每晚", "上限", "需附"} <= set(expanded), str(expanded))
        check("扩展词不与查询词重复", not (set(expanded) & {"住宿", "发票"}), str(expanded))
        check("扩展词数量有上限", len(expanded) <= kv.MAX_EXPANSION_TERMS, str(expanded))
        check("候选只有一段时宁可不扩展",
              kv.expansion_terms(["住宿"], ["住宿每晚上限 600 元"]) == [])
        check("单字查询词不做扩展", kv.expansion_terms(["卡"], ["住宿每晚上限 600 元", "住宿需发票"]) == [])
        check("候选里完全没有查询词时不扩展",
              kv.expansion_terms(["住宿"], ["年假规定五条", "调休申请流程"]) == [])

        # ── 7. 扫描件状态 ──────────────────────────────────────
        scan_base = make_base(
            home, "kb-scan",
            {"可检索.md": "差旅住宿每晚上限 600 元。"},
            raw={
                "扫描合同.pdf": "",                        # 抽不出一个字
                "扫描发票.png": "图",                       # 几乎没有可检索字符
                "手写的笔记.txt": "这一篇有正文所以不算扫描件，正文要够长才不被判空。",
            },
        )
        scanned = kv.scanned_documents(scan_base)
        check("抽不出正文的原件被点名", set(scanned) == {"扫描合同.pdf", "扫描发票.png"}, str(scanned))
        note = kv.scanned_note(scan_base)
        check("扫描件说明写明未做 OCR", "OCR" in note and "扫描合同.pdf" in note, note)
        check("没有扫描件时说明为空字符串",
              kv.scanned_note(make_base(home, "kb-clean", {"a.md": "正文内容足够长的一段。"})) == "")

        runtime = kn.KnowledgeRuntime.from_runtime({"knowledge": {"bases": [{
            "id": "kb-scan", "name": "扫描件库", "dir": str(scan_base), "docCount": 1,
        }]}})
        scan_result = runtime.search("住宿上限")
        check("运行时把扫描件状态带回给用户",
              any("扫描" in n and "OCR" in n for n in scan_result["notes"]),
              str(scan_result["notes"]))
        check("扫描件播报用的是库名而不是目录名",
              any("扫描件库" in n for n in scan_result["notes"]), str(scan_result["notes"]))

        # ── 8. 嵌入失败退回词法，检索本身不失败 ────────────────
        boom_results, boom_notes = kn.search_base(
            hybrid_base, "一线城市住宿上限", retrieval="hybrid", provider=BoomProvider()
        )
        check("嵌入失败仍能出词法结果", bool(boom_results), str(boom_results[:1]))
        check("嵌入失败原因被带回",
              any("退回词法召回" in n and "503" in n for n in boom_notes), str(boom_notes))
        check("降级后来源仍标词法",
              all(r["retrieval"] == "lexical" for r in boom_results),
              str([r.get("retrieval") for r in boom_results]))

        empty_provider = kv.EmbeddingProvider(
            key="empty", label="总给空向量", semantic=False, dimension=4, source="test")
        empty_results, empty_notes = kn.search_base(
            hybrid_base, "一线城市住宿上限", retrieval="hybrid", provider=empty_provider)
        check("供应商没实现 embed 时检索不崩", bool(empty_results), str(empty_results[:1]))

        # ── 9. 命中率对照（如实打印，不当成承诺） ──────────────
        bench_base = make_base(home, "kb-bench", {
            "差旅标准.md": "一线城市住宿每晚上限 600 元，二线城市 400 元，超标需直属主管审批。",
            "结算流程.md": "差旅费用凭发票结算，发票齐全后由部门统一入账，住宿部分随差旅标准执行。",
            "年假规定.md": "入职满一年享 5 天年假，满十年 10 天。",
        })
        queries = {
            "住宿每晚上限多少": "差旅标准.md",
            "发票怎么入账": "结算流程.md",
            "年假有几天": "年假规定.md",
            "超标了找谁审批": "差旅标准.md",
        }
        hit_lex = hit_mix = 0
        lines = []
        for query, expect in queries.items():
            lex, _ = kn.search_base(bench_base, query, retrieval="lexical")
            mix, _ = kn.search_base(bench_base, query, retrieval="hybrid")
            top_lex = lex[0]["doc"] if lex else ""
            top_mix = mix[0]["doc"] if mix else ""
            hit_lex += top_lex == expect
            hit_mix += top_mix == expect
            lines.append(f"{query}: lexical={top_lex or '空'} hybrid={top_mix or '空'} 期望={expect}")
        check("混合检索首条命中率不低于词法基线", hit_mix >= hit_lex,
              f"lexical={hit_lex}/{len(queries)} hybrid={hit_mix}/{len(queries)} || " + " | ".join(lines))
        print("[BENCH] 首条命中率 词法 {}/{} · 混合 {}/{}".format(
            hit_lex, len(queries), hit_mix, len(queries)))
        print("[BENCH] 明细：" + " | ".join(lines))

        # ── 10. 向量段落上限（大库不许把引擎拖死） ─────────────
        big_base = make_base(home, "kb-big", {"a.md": "住宿每晚上限 600 元。"})
        conn, _ = kn.ensure_index(big_base)
        with conn:
            for i in range(30):
                conn.execute(
                    "INSERT OR REPLACE INTO plain_chunks(doc, seq, body) VALUES(?,?,?)",
                    (f"批量{i}.md", 0, "住宿每晚上限 600 元，需审批。"),
                )
        conn.close()

        original_cap = kv.MAX_VECTOR_CHUNKS
        kv.MAX_VECTOR_CHUNKS = 10
        try:
            results_big, notes_big = kn.search_base(big_base, "住宿 上限", retrieval="hybrid")
        finally:
            kv.MAX_VECTOR_CHUNKS = original_cap
        check("待建段超过上限时如实拒建", any("超过上限" in n for n in notes_big), str(notes_big))
        check("拒建向量后检索仍出词法结果", bool(results_big), str(results_big[:1]))
        check("拒建后每条命中都标词法",
              all(r["retrieval"] == "lexical" for r in results_big),
              str([r.get("retrieval") for r in results_big]))
        check("上限恢复回原值", kv.MAX_VECTOR_CHUNKS == original_cap, str(kv.MAX_VECTOR_CHUNKS))
        restored, _ = kn.search_base(big_base, "住宿 上限", retrieval="hybrid")
        check("恢复正常上限后同一次查询照样有结果", bool(restored), str(restored[:1]))

        # ── 11. 嵌入文本与批次上限 ─────────────────────────────
        clipped = kv._clip("字" * (kv.MAX_EMBED_CHARS + 500))
        check("送入嵌入的正文被截到上限", len(clipped) == kv.MAX_EMBED_CHARS, str(len(clipped)))
        check("短正文不被动过", kv._clip("住宿") == "住宿")
        many = ["住宿每晚上限 600 元 片段%d" % i for i in range(kv.MAX_EMBED_BATCH + 7)]
        vectors = kv._embed_in_batches(provider, many)
        check("批量嵌入条数与输入一致", len(vectors) == len(many), f"{len(vectors)}/{len(many)}")

        # ── 12. 索引损坏时不影响任务本身 ───────────────────────
        broken = make_base(home, "kb-broken", {"a.md": "住宿每晚上限 600 元。"})
        (broken / kn.INDEX_FILE).write_text("这不是 sqlite 文件", encoding="utf-8")
        broken_results, broken_notes = kn.search_base(broken, "住宿上限", retrieval="hybrid")
        check("索引文件损坏时如实说明而不是崩", isinstance(broken_notes, list), str(broken_notes))

        # ── 13. 端点面：检索口径自述挂在 knowledge 车道上 ───────
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            import routes_lanes as rl
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP: 端点面未跑（{exc}）")
        else:
            # 指向临时目录**再** import server：引擎主模块在 import 期就会解析数据根
            os.environ["TONGTIANWORK_HOME"] = str(home)
            import server

            fresh = FastAPI()
            report = rl.attach(fresh, server, ["routes_knowledge"])
            kb_paths = sorted(p for row in report for p in row["paths"]
                              if p.startswith("/api/knowledge"))
            check("车道挂上检索口径自述端点", kb_paths == ["/api/knowledge/retrieval"], str(kb_paths))
            client = TestClient(fresh)
            listing_before = sorted(p.name for p in home.iterdir())

            off = client.get("/api/knowledge/retrieval")
            info = off.json()
            check("默认口径是混合检索",
                  off.status_code == 200 and info["ok"] and info["mode"] == kn.DEFAULT_RETRIEVAL,
                  str(info)[:200])
            check("本机没有语义模型时如实说这不是语义召回",
                  info["semantic"] is False and any("语义" in n for n in info["notes"]),
                  str(info["notes"])[:240])
            check("自述点名实际选中的来源与维度",
                  bool(info["provider"]) and bool(info["source"]) and info["dimension"] > 0,
                  str(info)[:220])
            check("说明里不许带出密钥形态",
                  not api_key_leak("；".join(info["notes"])), str(info["notes"])[:200])

            os.environ[kn.RETRIEVAL_ENV] = "lexical"
            lex = client.get("/api/knowledge/retrieval").json()
            check("按配置关闭向量召回时点名环境变量并声明没用上",
                  lex["mode"] == "lexical" and lex["vectorRecall"] is False
                  and any(kn.RETRIEVAL_ENV in n for n in lex["notes"])
                  and any("没有被用上" in n for n in lex["notes"]), str(lex["notes"])[:240])
            os.environ.pop(kn.RETRIEVAL_ENV, None)

            probe = client.get("/api/knowledge/retrieval", params={"prefer": "remote"}).json()
            check("指定远端却没配端点时说明未配置并退回内置词面向量",
                  any("未配置" in n for n in probe["notes"]) and probe["semantic"] is False,
                  str(probe["notes"])[:240])
            check("这条 GET 只自述：不改配置也不往数据根写东西",
                  kn.retrieval_mode() == kn.DEFAULT_RETRIEVAL
                  and sorted(p.name for p in home.iterdir()) == listing_before,
                  str(sorted(p.name for p in home.iterdir()))[:200])
            unknown = client.get("/api/knowledge/retrieval", params={"prefer": "quantum"}).json()
            check("认不出的 prefer 不报错，按自动选择回答并说明",
                  unknown["ok"] and unknown["mode"] == kn.DEFAULT_RETRIEVAL
                  and bool(unknown["provider"]), str(unknown)[:220])

    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil
        shutil.rmtree(home, ignore_errors=True)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} 项 — " + "；".join(FAILURES))
        return 1
    print("\nALL PASS")
    return 0


def api_key_leak(text: str) -> bool:
    """说明文本里不许出现密钥形态的字符串"""
    import re
    return bool(re.search(r"(api[_-]?key|Bearer|sk-[A-Za-z0-9]{6,})", text or "", re.I))


if __name__ == "__main__":
    sys.exit(main())
