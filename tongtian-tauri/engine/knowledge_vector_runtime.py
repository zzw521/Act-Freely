"""知识库混合检索：可选向量召回 + 重排 + 扫描件状态（缺陷 D-09）。

`knowledge_runtime` 只有 FTS5 词法召回，同义表达、多词稀疏查询的命中率有限。
本模块补三件事，全部**可离线**：

1. 嵌入供应商（`resolve_provider`）：按「本机有没有真向量模型」挑一个可用的。
   - 装了 sentence-transformers → 用语义向量；
   - 配了 OpenAI 兼容 embeddings 端点（环境变量，不落盘密钥）→ 用语义向量；
   - 两者都没有 → 退回**特征散列词面向量**（纯 Python，零依赖）。
     它只是把词面相似度搬进余弦空间，**不做同义替换**，因此 `semantic=False`，
     每一句对外播报的说明都写清「本机没有语义模型」——不许静默假装向量在场。
2. 融合与混合打分（`fuse_candidates` + `hybrid_score`）：词法与向量两条召回并成
   一份候选集，按「词法/覆盖/标题 + 向量余弦 + 扩展覆盖」打分；同一文档最多
   保留两段的老规则不变，来源多样性由它负责，不另造一套。
3. 局部上下文扩展（`expansion_terms`）：从**当前库自己的正文**里取与查询词高频
   共现的词补进打分。这是不依赖任何模型也能改善排序的唯一路径，
   代价是会引入噪声，因此扩展词权重单独一档，且必须在说明里点名扩展了什么。

铁律沿用 `knowledge_runtime`：索引是派生物，任何一步失败都退回词法召回，
但「为什么退回」「扩展了什么」「哪些文档是扫描件」必须作为可读事实返回。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# ── 预算与常量 ─────────────────────────────────────────────────

#: 特征散列词面向量的维度；与任何模型无关，换维度即索引签名变化
HASH_DIMENSION = 512
#: 单次建向量索引最多处理的段落数（超出的段落本轮不建向量，如实说明）
MAX_VECTOR_CHUNKS = 20000
#: 单段送入嵌入供应商的字符上限
MAX_EMBED_CHARS = 1200
#: 一次查询送入嵌入的文本条数上限
MAX_EMBED_BATCH = 256
#: 向量召回取多少候选进重排（与词法候选合并，不是最终返回数）
VECTOR_RECALL_LIMIT = 40
#: 混合打分里各特征权重；词法/覆盖/标题沿用 knowledge_runtime 的三档，
#: 向量与扩展单独一档且刻意偏低——语义分数不可跨供应商比较，压太高会 destabilize 排序
VECTOR_WEIGHT = 0.15
EXPANSION_WEIGHT = 0.10
#: 局部扩展最多补几个词
MAX_EXPANSION_TERMS = 6
#: 参与扩展统计的候选段落数
EXPANSION_CANDIDATE_CHUNKS = 24
#: 扩展词至少在多少候选段落里与查询词共现才允许入选
MIN_CO_OCCURRENCE = 2

VECTOR_TABLE = "vec_rows"
VECTOR_META_TABLE = "vec_meta"
PROVIDER_META_KEY = "provider"

EMBED_BASE_URL_ENV = "TT_EMBEDDING_BASE_URL"
EMBED_MODEL_ENV = "TT_EMBEDDING_MODEL"
EMBED_API_KEY_ENV = "TT_EMBEDDING_API_KEY"

_SCANNED_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
# 抽取正文里几乎没有汉字/字母数字 → 判定为「没有可检索文本」
_MEANINGFUL_RE = re.compile(r"[A-Za-z0-9一-鿿]")


# ── 嵌入供应商 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbeddingProvider:
    """一个可用的嵌入来源。`key` 写进索引签名，换供应商即整库重建向量。"""

    key: str
    label: str
    semantic: bool
    dimension: int
    source: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def note(self) -> str:
        if self.semantic:
            return f"向量召回启用：{self.label}（语义模型，{self.dimension} 维）"
        return (
            f"向量召回仅为词面相似度：{self.label}（本机没有可用的语义嵌入模型，"
            f"同义替换仍靠局部上下文扩展，{self.dimension} 维）"
        )


class HashedNgramProvider(EmbeddingProvider):
    """特征散列（hashing trick）词面向量：中文二元组 + 英文词，取对数次词频后 L2 归一。

    零依赖、完全离线、同一输入永远同一向量（可复现），所以拿它做默认值。
    它把「词面重叠」搬到余弦空间，能缓解稀疏多词查询的排序抖动，
    但**不会**把「住宿」和「酒店」判成相似——所以 semantic=False，说明必须写清。
    """

    def __init__(self) -> None:
        super().__init__(
            key=f"hash-ngram-{HASH_DIMENSION}",
            label="内置特征散列词面向量",
            semantic=False,
            dimension=HASH_DIMENSION,
            source="builtin",
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hashed_vector(text, self.dimension) for text in texts]


class SentenceTransformerProvider(EmbeddingProvider):
    """本地 sentence-transformers 模型；缺包时 `build()` 返回 None，由调用方降级。"""

    def __init__(self, model_name: str, encode_fn) -> None:
        super().__init__(
            key=f"sbert:{model_name}",
            label=f"本地语义模型 {model_name}",
            semantic=True,
            dimension=0,
            source="sentence-transformers",
        )
        self._encode = encode_fn
        self._model_name = model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._encode([_clip(t) for t in texts])
        out = [[float(x) for x in v] for v in vectors]
        if out and self.dimension == 0:
            object.__setattr__(self, "dimension", len(out[0]))
        return out


class OpenAICompatProvider(EmbeddingProvider):
    """OpenAI 兼容 /v1/embeddings 端点（自建或第三方）。

    只在环境变量显式配置时启用；密钥只从 `TT_EMBEDDING_API_KEY` 读，
    不写进任何配置文件，也不回显在说明文本里。这是网络链路，
    默认不选它——离线优先。
    """

    def __init__(self, base_url: str, model: str) -> None:
        super().__init__(
            key=f"openai-compat:{model}",
            label=f"外部 embeddings 端点 {model}",
            semantic=True,
            dimension=0,
            source="remote",
        )
        self.base_url = base_url.rstrip("/")
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        import urllib.error
        import urllib.request

        payload = json.dumps({
            "model": self.model,
            "input": [_clip(t) for t in texts],
            "encoding_format": "float",
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = str(os.environ.get(EMBED_API_KEY_ENV) or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/embeddings", data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"embeddings 端点调用失败：{_reason(exc)}") from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise RuntimeError("embeddings 端点返回的向量条数与请求不一致")
        out = [[float(x) for x in row.get("embedding") or []] for row in rows]
        if out and self.dimension == 0:
            object.__setattr__(self, "dimension", len(out[0]))
        return out


def _reason(exc: BaseException) -> str:
    return " ".join(str(exc or "").split())[:160]


def _clip(text: str) -> str:
    body = str(text or "")
    return body if len(body) <= MAX_EMBED_CHARS else body[:MAX_EMBED_CHARS]


def _hashed_vector(text: str, dimension: int) -> list[float]:
    """词元 → 对数次词频 → 带符号散列进固定维度 → L2 归一"""
    from knowledge_runtime import index_tokens  # 同源分词，避免两套词元

    counts: dict[str, int] = {}
    for token in index_tokens(text).split():
        counts[token] = counts.get(token, 0) + 1
    vector = [0.0] * dimension
    for token, count in counts.items():
        bucket = _hash_token(token) % dimension
        # 带符号散列：不同词元撞进同一桶时相互抵消，期望无偏
        sign = 1.0 if (_hash_token(token) // dimension) % 2 == 0 else -1.0
        vector[bucket] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0.0:
        return vector
    return [v / norm for v in vector]


def _hash_token(token: str) -> int:
    # 固定多项式滚动哈希：不依赖 PYTHONHASHSEED，跨进程可复现
    value = 0
    for ch in token:
        value = (value * 1315423911 + ord(ch)) & 0x7FFFFFFF
    return value


def cosine(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def resolve_provider(prefer: str = "") -> tuple[EmbeddingProvider | None, list[str]]:
    """按「离线优先」挑一个可用的嵌入供应商；返回 (供应商, 如实说明)。

    `prefer` 可以是 `builtin` / `local` / `remote` / `lexical` 或空（自动）。
    任何一步不可用都写进说明而不是抛异常：调用方据此降级成词法召回。
    选中的供应商只要不是语义模型，就必须把「这不是语义召回」写进说明——
    向量在场与不在场，模型和用户看到的措辞不能一样。
    """
    notes: list[str] = []
    want = str(prefer or "").strip().lower()
    if want in ("lexical", "off", "none"):
        return None, ["按配置关闭向量召回，仅使用词法检索"]

    chosen: EmbeddingProvider | None = None
    if want in ("", "auto", "local"):
        chosen = _sentence_transformer_provider(notes)
        if chosen is None and want == "local":
            notes.append("本机没有可用的本地语义模型，改用内置词面向量")

    if chosen is None and want in ("", "auto", "remote"):
        chosen = _remote_provider(notes)

    if chosen is None:
        if want == "remote":
            notes.append(
                f"未配置 embeddings 端点（需要 {EMBED_BASE_URL_ENV} 与 {EMBED_MODEL_ENV}），"
                "改用内置词面向量"
            )
        chosen = HashedNgramProvider()

    notes.append(chosen.note())
    return chosen, notes



def _sentence_transformer_provider(notes: list[str]) -> EmbeddingProvider | None:
    model_name = str(os.environ.get("TT_EMBEDDING_LOCAL_MODEL") or "").strip()
    if not model_name:
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:  # noqa: BLE001
        notes.append(f"未安装 sentence-transformers，无法使用本地模型「{model_name}」")
        return None
    try:
        model = SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"本地模型「{model_name}」加载失败：{_reason(exc)}")
        return None
    notes.append(
        f"本地语义模型「{model_name}」已加载；首次编码需在离线模型缓存内完成，不会联网下载"
    )
    return SentenceTransformerProvider(model_name, model.encode)


def _remote_provider(notes: list[str]) -> EmbeddingProvider | None:
    base_url = str(os.environ.get(EMBED_BASE_URL_ENV) or "").strip()
    model = str(os.environ.get(EMBED_MODEL_ENV) or "").strip()
    if not base_url or not model:
        return None
    if not base_url.startswith(("http://", "https://")):
        notes.append(f"embeddings 端点地址不合法（需以 http/https 开头）：{_reason(base_url)}")
        return None
    return OpenAICompatProvider(base_url, model)


# ── 向量索引（与词法索引同一个 sqlite 文件） ────────────────────


def _vector_table_ready(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {VECTOR_TABLE}"
            "(doc TEXT, seq INTEGER, provider TEXT, vec TEXT, PRIMARY KEY(doc, seq, provider))"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {VECTOR_META_TABLE}(key TEXT PRIMARY KEY, value TEXT)"
        )
        return True
    except sqlite3.OperationalError:
        return False


def _stored_provider(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            f"SELECT value FROM {VECTOR_META_TABLE} WHERE key=?", (PROVIDER_META_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0]) if row else ""


def sync_vector_index(
    conn: sqlite3.Connection, provider: EmbeddingProvider
) -> tuple[bool, list[str]]:
    """让向量表与当前供应商一致；返回（向量可用吗, 说明）。

    供应商变化（换模型/换维度）时整表重建：不同模型的余弦不可比，混用会给出假排序。
    """
    notes: list[str] = []
    if not _vector_table_ready(conn):
        return False, ["本机 SQLite 无法建立向量表，检索退回词法召回"]
    stored = _stored_provider(conn)
    if stored != provider.key:
        if stored:
            notes.append(f"嵌入来源由「{stored}」变为「{provider.key}」，整库向量重建")
        try:
            with conn:
                conn.execute(f"DELETE FROM {VECTOR_TABLE}")
                conn.execute(
                    f"INSERT OR REPLACE INTO {VECTOR_META_TABLE}(key, value) VALUES(?,?)",
                    (PROVIDER_META_KEY, provider.key),
                )
        except sqlite3.OperationalError as exc:
            return False, [f"向量索引重建失败，退回词法召回：{_reason(exc)}"]
    try:
        pending = conn.execute(
            f"SELECT p.doc, p.seq, p.body FROM plain_chunks p "
            f"LEFT JOIN {VECTOR_TABLE} v ON v.doc=p.doc AND v.seq=p.seq AND v.provider=? "
            "WHERE v.doc IS NULL",
            (provider.key,),
        ).fetchall()
        # 文档删除或重新切块后，向量表里会留下词法索引已经没有的段落；
        # 不清掉就会命中一篇正文已经不存在的「幽灵结果」，出处指向空文档。
        with conn:
            pruned = conn.execute(
                f"DELETE FROM {VECTOR_TABLE} "
                f"WHERE NOT EXISTS (SELECT 1 FROM plain_chunks p "
                f"WHERE p.doc = {VECTOR_TABLE}.doc AND p.seq = {VECTOR_TABLE}.seq)"
            ).rowcount
    except sqlite3.OperationalError as exc:
        return False, [f"向量索引读取失败，退回词法召回：{_reason(exc)}"]
    if pruned:
        notes.append(f"清理了 {pruned} 段已不存在于正文索引的向量")
    if not pending:
        return True, notes
    if len(pending) > MAX_VECTOR_CHUNKS:
        notes.append(
            f"待建向量的段落 {len(pending)} 段超过上限 {MAX_VECTOR_CHUNKS}，"
            "本库暂不建向量索引，检索退回词法召回"
        )
        return False, notes
    try:
        vectors = _embed_in_batches(provider, [str(r[2]) for r in pending])
    except Exception as exc:  # noqa: BLE001
        notes.append(f"嵌入计算失败，本轮退回词法召回：{_reason(exc)}")
        return False, notes
    try:
        with conn:
            for (doc, seq, _body), vector in zip(pending, vectors):
                conn.execute(
                    f"INSERT OR REPLACE INTO {VECTOR_TABLE}(doc, seq, provider, vec) VALUES(?,?,?,?)",
                    (str(doc), int(seq), provider.key, json.dumps(vector)),
                )
    except sqlite3.OperationalError as exc:
        return False, [f"向量索引落盘失败，退回词法召回：{_reason(exc)}"]
    notes.append(f"向量索引补齐 {len(pending)} 段")
    return True, notes


def _embed_in_batches(provider: EmbeddingProvider, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for start in range(0, len(texts), MAX_EMBED_BATCH):
        out.extend(provider.embed(texts[start:start + MAX_EMBED_BATCH]))
    if len(out) != len(texts):
        raise RuntimeError(
            f"嵌入返回条数不符（期望 {len(texts)}，实得 {len(out)}）"
        )
    return out


def recall_vectors(
    conn: sqlite3.Connection, provider: EmbeddingProvider, query_text: str,
    limit: int = VECTOR_RECALL_LIMIT,
) -> tuple[dict[tuple[str, int], dict], list[str]]:
    """在已打开的词法索引连接上做向量召回；连接由调用方持有与关闭。

    返回 `{(doc, seq): {"body", "sim", "vector"}}` 与说明。任何一步不可用
    都只返回空候选加说明——向量召回是加分项，永远不能把检索本身弄失败。
    """
    notes: list[str] = []
    usable, sync_notes = sync_vector_index(conn, provider)
    notes.extend(sync_notes)
    if not usable:
        return {}, notes
    try:
        query_vectors = provider.embed([query_text])
    except Exception as exc:  # noqa: BLE001
        return {}, notes + [f"查询嵌入失败，本轮退回词法召回：{_reason(exc)}"]
    if not query_vectors or not query_vectors[0]:
        return {}, notes + ["查询嵌入为空，本轮退回词法召回"]
    query_vector = query_vectors[0]
    try:
        rows = conn.execute(
            f"SELECT doc, seq, vec FROM {VECTOR_TABLE} WHERE provider=?",
            (provider.key,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return {}, notes + [f"向量索引读取失败，退回词法召回：{_reason(exc)}"]
    if not rows:
        return {}, notes + ["向量索引为空，本库本轮只有词法召回"]
    scored = []
    for doc, seq, raw in rows:
        sim = cosine(query_vector, _decode_vec(raw))
        if sim > 0.0:
            scored.append((str(doc), int(seq), sim))
    scored.sort(key=lambda r: (-r[2], r[0], r[1]))
    scored = scored[:limit]
    bodies = _bodies_for(conn, [(doc, seq) for doc, seq, _s in scored])
    vectors = {(doc, seq): _decode_vec(raw) for doc, seq, raw in rows}
    out = {}
    for doc, seq, sim in scored:
        body = bodies.get((doc, seq), "")
        if not body.strip():
            # 向量在、正文不在：这一段检索不出来，不能带着空正文冒充命中
            continue
        out[(doc, seq)] = {
            "body": body,
            "sim": sim,
            "vector": vectors.get((doc, seq), []),
        }
    return out, notes


def search_vectors(
    base_dir: Path, provider: EmbeddingProvider, query_text: str,
    limit: int = VECTOR_RECALL_LIMIT,
) -> tuple[list[tuple[str, int, str, float]], list[str]]:
    """独立入口（自测与单库探查用）：打开索引 → 向量召回 → 关闭连接。"""
    from knowledge_runtime import ensure_index

    conn, notes = ensure_index(base_dir)
    if conn is None:
        return [], notes
    try:
        found, recall_notes = recall_vectors(conn, provider, query_text, limit)
        notes.extend(recall_notes)
        rows = [
            (doc, seq, str(item["body"]), float(item["sim"]))
            for (doc, seq), item in found.items()
        ]
        rows.sort(key=lambda r: (-r[3], r[0], r[1]))
        return rows, notes
    finally:
        conn.close()


def _decode_vec(raw) -> list[float]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [float(x) for x in data if isinstance(x, (int, float))]


def _bodies_for(conn: sqlite3.Connection, rows) -> dict[tuple[str, int], str]:
    if not rows:
        return {}
    marks = " OR ".join("(doc=? AND seq=?)" for _ in rows)
    params = [v for doc, seq in rows for v in (doc, seq)]
    try:
        fetched = conn.execute(
            f"SELECT doc, seq, body FROM plain_chunks WHERE {marks}", params
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(str(r[0]), int(r[1])): str(r[2]) for r in fetched}


# ── 局部上下文扩展（无模型也能补同义） ──────────────────────────


def expansion_terms(query_tokens: list[str], candidate_bodies: list[str]) -> list[str]:
    """从候选段落里挑与查询词高频共现的词，作为查询扩展。

    只用当前知识库自己的正文，不联网、不需要模型；这就是「同义查询命中率
    相对基线提升」在本机唯一可兑现的来源。候选段不足时返回空列表——
    宁可不扩展，也不拿一两段话的噪声当词典。
    """
    wanted = {t for t in query_tokens if len(t) >= 2}
    if not wanted or len(candidate_bodies) < 2:
        return []
    from knowledge_runtime import index_tokens

    co: dict[str, int] = {}
    docs_with_query: int = 0
    for body in candidate_bodies:
        tokens = set(index_tokens(body).split())
        if not (wanted & tokens):
            continue
        docs_with_query += 1
        for token in tokens:
            if token in wanted or len(token) < 2:
                continue
            co[token] = co.get(token, 0) + 1
    if docs_with_query < 2:
        return []
    ranked = [
        (count, term)
        for term, count in co.items()
        if count >= min(MIN_CO_OCCURRENCE, docs_with_query)
    ]
    ranked.sort(key=lambda pair: (-pair[0], pair[1]))
    return [term for _count, term in ranked[:MAX_EXPANSION_TERMS]]


# ── 融合与重排 ─────────────────────────────────────────────────


def fuse_candidates(
    lexical: list[tuple[str, int, str, float]],
    vector: dict[tuple[str, int], dict],
) -> dict[tuple[str, int], dict]:
    """两条召回并成一份候选集：{(doc,seq): {doc,seq,body,lex,vec,vector}}

    两边都命中的段落取各自最大分；只有一边命中的保留另一边。
    """
    merged: dict[tuple[str, int], dict] = {}
    for doc, seq, body, lex in lexical:
        merged[(doc, seq)] = {
            "doc": doc, "seq": seq, "body": body,
            "lex": float(lex), "vec": 0.0, "vector": [],
        }
    for key, item in vector.items():
        slot = merged.get(key)
        if slot is None:
            merged[key] = {
                "doc": key[0], "seq": key[1], "body": item.get("body") or "",
                "lex": 0.0, "vec": float(item.get("sim") or 0.0),
                "vector": list(item.get("vector") or []),
            }
            continue
        slot["vec"] = max(slot["vec"], float(item.get("sim") or 0.0))
        if not slot["vector"]:
            slot["vector"] = list(item.get("vector") or [])
        if not slot["body"]:
            slot["body"] = item.get("body") or ""
    return merged


def hybrid_score(
    slot: dict, lex_peak: float, query_tokens: list[str],
    expanded_tokens: list[str], coverage_fn,
) -> float:
    """混合打分：词法/覆盖/标题三档保持 knowledge_runtime 原权重，
    向量余弦与扩展覆盖各按自己一档加入。"""
    from knowledge_runtime import COVERAGE_WEIGHT, LEXICAL_WEIGHT, TITLE_WEIGHT

    body = str(slot.get("body") or "")
    doc = str(slot.get("doc") or "")
    score = (
        LEXICAL_WEIGHT * (max(0.0, float(slot.get("lex") or 0.0)) / (lex_peak or 1.0))
        + COVERAGE_WEIGHT * coverage_fn(query_tokens, body, doc)
        + TITLE_WEIGHT * coverage_fn(query_tokens, doc)
        + VECTOR_WEIGHT * max(0.0, min(1.0, float(slot.get("vec") or 0.0)))
    )
    if expanded_tokens:
        score += EXPANSION_WEIGHT * coverage_fn(expanded_tokens, body)
    return score


# ── 扫描件状态 ─────────────────────────────────────────────────


def scanned_documents(base_dir: Path) -> list[str]:
    """docs/ 里有原始文件但 text/ 里抽不出正文 → 如实列为「未提取到文本」。

    扫描件检索不到不是 bug，但必须让用户和模型知道是哪几篇、为什么，
    而不是让模型以为库里没有相关内容。
    """
    dir_path = Path(base_dir)
    docs_dir = dir_path / "docs"
    text_dir = dir_path / "text"
    if not docs_dir.is_dir():
        return []
    out: list[str] = []
    try:
        entries = sorted(docs_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        stem = entry.name
        extracted = text_dir / f"{stem}.txt"
        if extracted.is_file():
            try:
                body = extracted.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(_MEANINGFUL_RE.findall(body)) >= 8:
                continue
        elif not _is_document_like(entry.suffix):
            continue
        out.append(stem)
    return out


def _is_document_like(suffix: str) -> bool:
    return str(suffix or "").lower() in _SCANNED_SUFFIXES


def scanned_note(base_dir: Path, limit: int = 5) -> str:
    names = scanned_documents(base_dir)
    if not names:
        return ""
    shown = "、".join(names[:limit])
    more = f" 等 {len(names)} 篇" if len(names) > limit else ""
    return (
        f"知识库「{Path(base_dir).name}」里 {shown}{more} 没有可检索的文本"
        "（多为扫描件或图片型 PDF，本机未做 OCR）；这几篇的内容检索不到，"
        "需要时请安装 OCR 或直接读取原文件"
    )
