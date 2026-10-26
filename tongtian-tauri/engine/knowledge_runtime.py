"""本地知识库检索（RAG）——「文档在磁盘，索引是派生物」

Rust 每次提交任务随 `runtime.knowledge` 下发启用中的知识库：

    knowledge = { bases: [{id, name, description, dir, docCount}], error? }

每个知识库目录由 Rust 维护：

    {dir}/docs/   原始文件（事实来源）
    {dir}/text/   Rust 抽取好的纯文本（{文件名}.txt）
    {dir}/.index.sqlite   本模块生成的全文索引（删掉可随时重建）

本模块负责三件事：

1. 增量建索引：按 text/ 的 mtime + size 判定变更，只重建变化的文档；
   docs/ 里没有正文的旧文件如实留下说明，不假装检索得到。
2. 检索：SQLite FTS5 + 中文字符二元组 + 英文词元，返回带出处的片段。
3. 工具：注册 `search_knowledge`（片段检索）与 `read_knowledge_document`
   （按出处读整篇、可分段续读），system prompt 同时列出知识库清单与引用要求。
   没有可用知识库时**不注册工具**，也不写进提示词。

铁律：索引是派生物的派生物，任何索引异常都不能影响任务本身；
但「检索失败」「没有命中」「知识库没有文档」必须作为可读事实返回给模型与用户，
不能静默返回空结果让模型自由发挥。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

INDEX_FILE = ".index.sqlite"
MAX_CHUNK_CHARS = 900
CHUNK_OVERLAP = 120
MAX_TOP_K = 10
DEFAULT_TOP_K = 5
MAX_QUERY_CHARS = 200
MAX_EXCERPT_CHARS = 360
# 同一文档最多返回几段：避免一份长文档把其它来源挤掉
MAX_CHUNKS_PER_DOC = 2
# 排序权重：查询词覆盖优先于词频，文档名匹配单独计分
# （三者之和为 1，跨知识库合并排序时分数可比）
LEXICAL_WEIGHT = 0.2
COVERAGE_WEIGHT = 0.5
TITLE_WEIGHT = 0.3
# 命中片段只是入口：允许模型按需读取整篇正文，分页上限防止一次灌满上下文
DEFAULT_READ_CHARS = 8000
MAX_READ_CHARS = 20000
MIN_READ_CHARS = 500
MAX_DOC_CANDIDATES = 8

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")


def _brief(text: str, limit: int = 160) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


# ── 分词 ──────────────────────────────────────────────────────


def index_tokens(text: str) -> str:
    """索引侧词元：英文/数字词 + 中文单字 + 中文二元组

    中文没有空格，FTS5 的 unicode61 会把整句当成一个词；这里先切成
    「单字 + 相邻二元组」再由 FTS5 建倒排，查询侧同样处理，命中率与精度都可接受。
    """
    out: list[str] = [m.group(0).lower() for m in WORD_RE.finditer(text)]
    for m in CJK_RE.finditer(text):
        run = m.group(0)
        out.extend(run)
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(out)


def query_tokens(text: str) -> list[str]:
    words = [m.group(0).lower() for m in WORD_RE.finditer(text)]
    grams: list[str] = []
    for m in CJK_RE.finditer(text):
        run = m.group(0)
        if len(run) == 1:
            grams.append(run)
        else:
            grams.extend(run[i:i + 2] for i in range(len(run) - 1))
    seen: set[str] = set()
    out: list[str] = []
    for token in [*words, *grams]:
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


# ── 切块 ──────────────────────────────────────────────────────


def chunk_text(text: str) -> list[str]:
    """按段落聚合到 ~900 字，超长段落再按窗口切并保留少量重叠"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", normalized) if b.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > MAX_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            step = MAX_CHUNK_CHARS - CHUNK_OVERLAP
            for start in range(0, len(block), step):
                piece = block[start:start + MAX_CHUNK_CHARS]
                if piece.strip():
                    chunks.append(piece.strip())
                if start + MAX_CHUNK_CHARS >= len(block):
                    break
            continue
        if not current:
            current = block
        elif len(current) + 2 + len(block) <= MAX_CHUNK_CHARS:
            current = f"{current}\n\n{block}"
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


# ── 索引 ──────────────────────────────────────────────────────


def _db_path(base_dir: Path) -> Path:
    return Path(base_dir) / INDEX_FILE


def _scan_texts(text_dir: Path) -> dict[str, tuple[float, int]]:
    out: dict[str, tuple[float, int]] = {}
    try:
        entries = list(text_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            if not entry.is_file() or entry.suffix.lower() != ".txt":
                continue
            st = entry.stat()
            out[entry.name[:-4]] = (st.st_mtime, st.st_size)
        except OSError:
            continue
    return out


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files(name TEXT PRIMARY KEY, mtime REAL, size INTEGER)"
    )
    return conn


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
            "doc UNINDEXED, seq UNINDEXED, body UNINDEXED, tokens)"
        )
        return True
    except sqlite3.OperationalError:
        return False


def ensure_index(base_dir: Path) -> tuple[sqlite3.Connection | None, list[str]]:
    """确保索引与 text/ 一致；返回连接与需要向用户说明的事实。

    连接由调用方关闭。索引失败不抛异常：检索会退回逐块匹配，
    但原因必须交给调用方播报。
    """
    dir_path = Path(base_dir)
    text_dir = dir_path / "text"
    notes: list[str] = []
    if not text_dir.is_dir():
        return None, ["知识库缺少 text 目录，无法建立索引"]
    try:
        conn = _connect(_db_path(dir_path))
    except Exception as exc:  # noqa: BLE001
        return None, [f"知识库索引库打开失败：{_brief(exc)}"]
    fts = _fts_available(conn)
    if not fts:
        notes.append("本机 SQLite 不支持 FTS5，检索退化为逐块匹配（结果仍可用，速度略慢）")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS plain_chunks(doc TEXT, seq INTEGER, body TEXT)")
    except sqlite3.OperationalError:
        pass

    current = _scan_texts(text_dir)
    try:
        known = {
            row[0]: (float(row[1] or 0), int(row[2] or 0))
            for row in conn.execute("SELECT name, mtime, size FROM files")
        }
    except sqlite3.OperationalError:
        known = {}

    changed = [name for name, sig in current.items() if known.get(name) != sig]
    removed = [name for name in known if name not in current]
    if not changed and not removed:
        return conn, notes

    try:
        with conn:
            for name in removed:
                conn.execute("DELETE FROM plain_chunks WHERE doc=?", (name,))
                if fts:
                    conn.execute("DELETE FROM chunks WHERE doc=?", (name,))
                conn.execute("DELETE FROM files WHERE name=?", (name,))
            for name in changed:
                path = text_dir / f"{name}.txt"
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    notes.append(f"知识库文档「{name}」正文读不到：{_brief(exc)}")
                    continue
                chunks = chunk_text(text)
                conn.execute("DELETE FROM plain_chunks WHERE doc=?", (name,))
                if fts:
                    conn.execute("DELETE FROM chunks WHERE doc=?", (name,))
                if not chunks:
                    notes.append(f"知识库文档「{name}」正文为空，未建立索引")
                for seq, body in enumerate(chunks):
                    conn.execute(
                        "INSERT INTO plain_chunks(doc, seq, body) VALUES(?,?,?)",
                        (name, seq, body),
                    )
                    if fts:
                        conn.execute(
                            "INSERT INTO chunks(doc, seq, body, tokens) VALUES(?,?,?,?)",
                            (name, seq, body, index_tokens(body)),
                        )
                conn.execute(
                    "INSERT OR REPLACE INTO files(name, mtime, size) VALUES(?,?,?)",
                    (name, current[name][0], current[name][1]),
                )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"知识库索引更新失败，检索可能不完整：{_brief(exc)}")
    return conn, notes


def _token_set(text: str) -> set[str]:
    return set(query_tokens(text))


def _coverage(tokens: list[str], *texts: str) -> float:
    """查询词被覆盖的比例：避免一个高频词重复出现就压过真正回答了问题的段落"""
    wanted = set(tokens)
    if not wanted:
        return 0.0
    have: set[str] = set()
    for text in texts:
        have.update(_token_set(text))
    return len(wanted & have) / len(wanted)


def _excerpt(body: str, tokens: list[str]) -> str:
    """取命中词最密集的窗口，而不是简单截取第一个命中词前后的内容"""
    text = " ".join(body.split())
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    lower = text.lower()
    positions: list[int] = []
    for token in tokens:
        needle = token.lower()
        start = 0
        while len(positions) < 200:
            pos = lower.find(needle, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + max(1, len(needle))
    if not positions:
        return text[:MAX_EXCERPT_CHARS] + "…"
    # 多字词更能代表命中；单字只用于定位候选窗口
    strong = [t for t in set(tokens) if len(t) >= 2]
    best_start = max(0, positions[0] - MAX_EXCERPT_CHARS // 4)
    best_score = -1
    for pos in sorted(set(positions)):
        start = max(0, min(pos - MAX_EXCERPT_CHARS // 4, len(text) - MAX_EXCERPT_CHARS))
        window = lower[start:start + MAX_EXCERPT_CHARS]
        score = sum(len(t) for t in strong if t in window)
        if score > best_score:
            best_score = score
            best_start = start
    end = min(len(text), best_start + MAX_EXCERPT_CHARS)
    return ("…" if best_start > 0 else "") + text[best_start:end] + ("…" if end < len(text) else "")


def _plain_search(conn: sqlite3.Connection, tokens: list[str], limit: int) -> list[tuple[str, int, str, float]]:
    rows = conn.execute("SELECT doc, seq, body FROM plain_chunks").fetchall()
    scored = []
    for doc, seq, body in rows:
        lower = str(body).lower()
        score = sum(lower.count(t.lower()) for t in tokens)
        if score:
            scored.append((doc, int(seq), str(body), float(score)))
    scored.sort(key=lambda r: (-r[3], r[0], r[1]))
    return scored[:limit]


def _title_docs(conn: sqlite3.Connection, tokens: list[str]) -> list[str]:
    """文件名/标题就能回答的查询（如直接搜「季度复盘」）：词法索引只覆盖正文，这里补召回"""
    wanted = set(tokens)
    try:
        names = [str(row[0]) for row in conn.execute("SELECT DISTINCT doc FROM plain_chunks")]
    except sqlite3.OperationalError:
        return []
    out = []
    for name in names:
        if _coverage(tokens, name) >= 0.5 and wanted & _token_set(name):
            out.append(name)
    return out


def _title_chunks(conn: sqlite3.Connection, tokens: list[str], limit: int) -> list[tuple[str, int, str, float]]:
    docs = _title_docs(conn, tokens)
    if not docs:
        return []
    marks = ",".join("?" for _ in docs)
    try:
        rows = conn.execute(
            f"SELECT doc, seq, body FROM plain_chunks WHERE doc IN ({marks}) ORDER BY doc, seq",
            docs,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(r[0]), int(r[1]), str(r[2]), 0.0) for r in rows[:limit]]


def _rank_rows(rows: list[tuple[str, int, str, float]], tokens: list[str]) -> list[tuple[str, int, str, float]]:
    """词法分归一后与查询覆盖、标题匹配加权；同分按文档名/段序稳定排序"""
    if not rows:
        return []
    peak = max((row[3] for row in rows), default=0.0) or 1.0
    ranked = []
    for doc, seq, body, lexical in rows:
        score = (
            LEXICAL_WEIGHT * (max(0.0, lexical) / peak)
            + COVERAGE_WEIGHT * _coverage(tokens, body, doc)
            + TITLE_WEIGHT * _coverage(tokens, doc)
        )
        ranked.append((score, doc, seq, body))
    ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [(doc, seq, body, score) for score, doc, seq, body in ranked]


def search_base(base_dir: Path, query: str, top_k: int = DEFAULT_TOP_K) -> tuple[list[dict], list[str]]:
    """单库检索：返回 [{doc, seq, text, score, citation}] 与说明"""
    tokens = query_tokens(query)
    if not tokens:
        return [], ["检索词里没有可用的中英文内容"]
    conn, notes = ensure_index(base_dir)
    if conn is None:
        return [], notes
    limit = max(top_k * 6, 12)
    candidates: dict[tuple[str, int], tuple[str, int, str, float]] = {}
    try:
        fts = _fts_available(conn)
        rows: list[tuple[str, int, str, float]] = []
        if fts:
            match = " OR ".join(f'"{t}"' for t in tokens)
            try:
                cur = conn.execute(
                    "SELECT doc, seq, body, bm25(chunks) AS rank FROM chunks "
                    "WHERE tokens MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
                rows = [(r[0], int(r[1]), r[2], -float(r[3])) for r in cur.fetchall()]
            except sqlite3.OperationalError as exc:
                notes.append(f"全文索引查询失败，改用逐块匹配：{_brief(exc)}")
        if not rows:
            rows = _plain_search(conn, tokens, limit)
        for doc, seq, body, score in rows:
            candidates[(doc, seq)] = (doc, seq, body, score)
        # 标题命中补回正文索引召回不到的段落
        for doc, seq, body, _score in _title_chunks(conn, tokens, limit):
            candidates.setdefault((doc, seq), (doc, seq, body, 0.0))
    finally:
        conn.close()

    ranked = _rank_rows(list(candidates.values()), tokens)
    # 同一文档最多 MAX_CHUNKS_PER_DOC 段，保证来源多样
    results: list[dict] = []
    per_doc: dict[str, int] = {}
    for doc, seq, body, score in ranked:
        if per_doc.get(doc, 0) >= MAX_CHUNKS_PER_DOC:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        results.append({
            "doc": doc,
            "seq": seq,
            "text": _excerpt(str(body), tokens),
            "score": round(score, 4),
        })
        if len(results) >= top_k:
            break
    return results, notes


def _doc_names(base_dir: Path) -> list[str]:
    text_dir = Path(base_dir) / "text"
    try:
        names = [
            entry.name[:-4]
            for entry in text_dir.iterdir()
            if entry.is_file() and entry.suffix.lower() == ".txt"
        ]
    except OSError:
        return []
    return sorted(names)


def _resolve_doc(base_dir: Path, doc: str) -> tuple[Path | None, str]:
    """只接受 text/ 下的单层文件名；返回 (路径, 去掉 .txt 副档名后的文档名)"""
    raw = str(doc or "").strip()
    if not raw or raw in (".", "..") or any(ch in raw for ch in "/\\\x00"):
        return None, raw
    text_dir = (Path(base_dir) / "text").resolve()
    candidates = [raw]
    if raw.lower().endswith(".txt"):
        candidates.append(raw[:-4])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            path = (text_dir / f"{candidate}.txt").resolve()
        except OSError:
            continue
        if path.parent != text_dir or not path.is_file():
            continue
        return path, candidate
    return None, raw


def _doc_candidates(base_dir: Path, query: str) -> list[str]:
    names = _doc_names(base_dir)
    tokens = query_tokens(query)
    if not tokens:
        return names[:MAX_DOC_CANDIDATES]
    scored = [(_coverage(tokens, name), name) for name in names]
    scored = [pair for pair in scored if pair[0] > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    picked = [name for _score, name in scored[:MAX_DOC_CANDIDATES]]
    return picked or names[:MAX_DOC_CANDIDATES]


# ── 运行时 ────────────────────────────────────────────────────


@dataclass
class KnowledgeBase:
    id: str
    name: str
    description: str
    dir: str
    doc_count: int
    error: str = ""


@dataclass
class KnowledgeRuntime:
    bases: list[KnowledgeBase] = field(default_factory=list)
    error: str = ""

    @classmethod
    def from_runtime(cls, runtime: dict | None) -> "KnowledgeRuntime":
        block = (runtime or {}).get("knowledge") if isinstance(runtime, dict) else None
        if isinstance(block, str):
            try:
                block = json.loads(block)
            except Exception:  # noqa: BLE001
                return cls(error=f"知识库配置不是合法 JSON：{_brief(block)}")
        if not isinstance(block, dict):
            return cls()
        if str(block.get("error") or "").strip():
            return cls(error=str(block["error"]).strip())
        raw = block.get("bases")
        if not isinstance(raw, list):
            return cls()
        bases: list[KnowledgeBase] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            bid = str(item.get("id") or "").strip()
            name = str(item.get("name") or bid).strip() or bid
            path = str(item.get("dir") or "").strip()
            if not bid or not path:
                continue
            error = ""
            if not Path(path).is_dir():
                error = f"知识库目录不存在：{path}"
            bases.append(KnowledgeBase(
                id=bid,
                name=name,
                description=str(item.get("description") or "").strip(),
                dir=path,
                doc_count=max(0, int(item.get("docCount") or 0)),
                error=error,
            ))
        return cls(bases=bases)

    def ready(self) -> bool:
        return bool(self.bases) and any(not b.error for b in self.bases)

    def notes(self) -> list[str]:
        if self.error:
            return [f"知识库未进入本轮执行面：{self.error}"]
        out: list[str] = []
        for base in self.bases:
            if base.error:
                out.append(f"知识库「{base.name}」不可用：{base.error}")
            elif base.doc_count == 0:
                out.append(f"知识库「{base.name}」尚无文档，检索不会命中")
        return out

    def system_hint(self) -> str:
        usable = [b for b in self.bases if not b.error]
        if not usable:
            return ""
        lines = [
            f"- {b.name}（{b.id}）：{b.description or '（无描述）'}，{b.doc_count} 篇文档"
            for b in usable
        ]
        return (
            "\n\n## 本地知识库\n"
            "用户启用了以下本地知识库。回答涉及这些资料时，先用 search_knowledge 检索，"
            "再依据返回片段作答；片段不足以回答或需要核对上下文时，用 read_knowledge_document "
            "读取该篇正文（长篇可分段续读）。引用时保留返回里的出处标记"
            "（片段形如【知识库 / 文档 #序号】，整篇形如【知识库 / 文档】）。"
            "检索没有命中就直说知识库里没有，不要用常识冒充库内资料。\n\n"
            + "\n".join(lines)
        )

    def _select_bases(self, base_id: str) -> tuple[list[KnowledgeBase], str]:
        targets = [b for b in self.bases if not b.error]
        key = str(base_id or "").strip().lower()
        if key:
            targets = [
                b for b in targets
                if b.id.lower() == key or b.name.lower() == key
            ]
            if not targets:
                return [], f"没有名为「{base_id}」的知识库"
        if not targets:
            return [], "当前没有可检索的知识库"
        return targets, ""

    def search(self, query: str, base_id: str = "", top_k: int = DEFAULT_TOP_K) -> dict:
        text = str(query or "").strip()
        if not text:
            return {"results": [], "notes": ["检索词为空"]}
        if len(text) > MAX_QUERY_CHARS:
            text = text[:MAX_QUERY_CHARS]
        try:
            k = int(top_k)
        except Exception:  # noqa: BLE001
            k = DEFAULT_TOP_K
        k = min(max(k, 1), MAX_TOP_K)
        targets, error = self._select_bases(base_id)
        if error:
            return {"results": [], "notes": [error]}

        results: list[dict] = []
        notes: list[str] = []
        for base in targets:
            found, base_notes = search_base(Path(base.dir), text, k)
            notes.extend(f"{base.name}：{n}" for n in base_notes)
            for row in found:
                row.update({
                    "baseId": base.id,
                    "baseName": base.name,
                    "citation": f"【{base.name} / {row['doc']} #{row['seq'] + 1}】",
                })
                results.append(row)
        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        results = results[:k]
        if not results:
            notes.append("知识库中没有命中该问题的内容")
        return {"results": results, "notes": notes}

    def read_document(
        self,
        doc: str,
        base_id: str = "",
        offset: int = 0,
        max_chars: int = DEFAULT_READ_CHARS,
    ) -> dict:
        """读取整篇正文（按字符分页）；找不到时如实给出候选文档，不做模糊猜测。"""
        raw = str(doc or "").strip()
        if not raw:
            return {"doc": "", "text": "", "notes": ["文档名为空"]}
        if raw in (".", "..") or any(ch in raw for ch in "/\\\x00"):
            return {"doc": raw, "text": "", "notes": ["文档名不合法：只能是知识库内的文件名"]}
        try:
            start = max(0, int(offset))
        except Exception:  # noqa: BLE001
            start = 0
        try:
            size = int(max_chars)
        except Exception:  # noqa: BLE001
            size = DEFAULT_READ_CHARS
        size = min(max(size, MIN_READ_CHARS), MAX_READ_CHARS)
        targets, error = self._select_bases(base_id)
        if error:
            return {"doc": raw, "text": "", "notes": [error]}

        matches: list[tuple[KnowledgeBase, Path, str]] = []
        for base in targets:
            path, name = _resolve_doc(Path(base.dir), raw)
            if path is not None:
                matches.append((base, path, name))
        if not matches:
            notes = []
            for base in targets:
                names = _doc_candidates(Path(base.dir), raw)
                if names:
                    notes.append(
                        f"知识库「{base.name}」没有名为「{raw}」的文档；可选："
                        + "、".join(names)
                    )
                else:
                    notes.append(f"知识库「{base.name}」没有可读取的文档")
            return {"doc": raw, "text": "", "notes": notes}
        if len(matches) > 1:
            where = "、".join(f"「{base.name}」（{base.id}）" for base, _p, _n in matches)
            return {
                "doc": raw,
                "text": "",
                "notes": [f"同名文档出现在多个知识库：{where}；请用 base 指定其中一个"],
            }

        base, path, name = matches[0]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"doc": name, "text": "", "notes": [f"文档「{name}」正文读取失败：{_brief(exc)}"]}
        total = len(text)
        if total == 0:
            return {"doc": name, "text": "", "notes": [f"文档「{name}」正文为空"]}
        if start >= total:
            return {
                "doc": name,
                "text": "",
                "notes": [f"offset {start} 超出文档「{name}」长度（共 {total} 字）"],
            }
        end = min(total, start + size)
        return {
            "baseId": base.id,
            "baseName": base.name,
            "doc": name,
            "offset": start,
            "nextOffset": end if end < total else None,
            "totalChars": total,
            "text": text[start:end],
            "citation": f"【{base.name} / {name}】",
            "notes": [],
        }

    def install(self, toolkit) -> None:
        """有可用知识库才注册工具；注册即代表提示词里也已列出清单"""
        if not self.ready():
            return
        from agentscope.tool import ToolResponse
        from agentscope.message import TextBlock

        def search_knowledge(query: str, base: str = "", top_k: int = DEFAULT_TOP_K):
            """在用户启用的本地知识库中检索与问题相关的片段（带出处）。

            返回的是片段，不是整篇文档；需要更多上下文时用
            read_knowledge_document 按出处里的文档名读取全文。

            Args:
                query (`str`):
                    检索词或问题，中英文均可。
                base (`str`, optional):
                    只检索某个知识库 id 或名称；留空表示全部启用中的知识库。
                top_k (`int`, optional):
                    返回片段数，默认 5，最多 10。
            """
            result = self.search(query, base, top_k)
            rows = result.get("results") or []
            notes = result.get("notes") or []
            if not rows:
                body = "没有检索到相关内容。\n" + (
                    "说明：" + "；".join(notes) if notes else ""
                )
                return ToolResponse(content=[TextBlock(type="text", text=body)])
            parts = [f"共 {len(rows)} 条相关片段："]
            for i, row in enumerate(rows, 1):
                parts.append(
                    f"{i}. {row['citation']}\n{row['text']}"
                )
            if notes:
                parts.append("说明：" + "；".join(notes))
            return ToolResponse(content=[TextBlock(type="text", text="\n\n".join(parts))])

        def read_knowledge_document(
            doc: str,
            base: str = "",
            offset: int = 0,
            max_chars: int = DEFAULT_READ_CHARS,
        ):
            """读取知识库中某篇文档的正文；长篇按字符分段返回。

            文档名来自 search_knowledge 的出处；未命中时会列出可选的文档名。
            不要用它遍历整库：先检索，确认相关后再读整篇。

            Args:
                doc (`str`):
                    文档名，例如「差旅报销标准.md」。
                base (`str`, optional):
                    知识库 id 或名称；同名文档出现在多个库时必须指定。
                offset (`int`, optional):
                    从第几个字符开始，默认 0；续读时用上次返回的 nextOffset。
                max_chars (`int`, optional):
                    本次最多返回多少字符，默认 8000，最多 20000。
            """
            result = self.read_document(doc, base, offset, max_chars)
            notes = result.get("notes") or []
            text = str(result.get("text") or "")
            if not text:
                body = "没有读到文档内容。\n" + (
                    "说明：" + "；".join(notes) if notes else ""
                )
                return ToolResponse(content=[TextBlock(type="text", text=body)])
            start = int(result.get("offset") or 0)
            end = start + len(text)
            total = int(result.get("totalChars") or end)
            parts = [
                f"{result.get('citation', '')} 共 {total} 字，本次返回第 {start + 1}–{end} 字。",
                text,
            ]
            next_offset = result.get("nextOffset")
            if next_offset is not None:
                parts.append(f"（还有 {total - end} 字未读完；用 offset={next_offset} 继续）")
            if notes:
                parts.append("说明：" + "；".join(notes))
            return ToolResponse(content=[TextBlock(type="text", text="\n\n".join(parts))])

        toolkit.register_tool_function(search_knowledge)
        toolkit.register_tool_function(read_knowledge_document)
