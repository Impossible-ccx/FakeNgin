"""BM25 搜索与索引缓存。

消息数据来自 SQLite（稳定 ID），索引缓存持久化到 database/searchindex/ 下的
csv：postings/doc_lengths/terms/meta。doc_id 即 messages.id。

失效与重建策略：任何消息写入都会递增 messages_data_version（见 newsdata），
meta 中记录水印 "data_version:doc_count"；不一致即全量重建（课程规模数据量下
重建成本低），INDEX_VERSION 变化也会触发重建。
"""

import hashlib
import math
import shutil
from collections import defaultdict

import jieba
import pandas as pd

from . import db, newsdata

SEARCH_INDEX_DIR = db.DATABASE_DIR / "searchindex"

INDEX_VERSION = 2
BM25_K1 = 1.5
BM25_B = 0.75

META_COLUMNS = ["watermark", "doc_count", "total_length", "version"]
POSTING_COLUMNS = ["doc_id", "term", "tf"]
DOC_LENGTH_COLUMNS = ["doc_id", "length"]
TERM_COLUMNS = ["term", "df"]


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------- 分词

def _tokenize(text):
    """jieba 搜索引擎模式分词，过滤空白与纯标点，ASCII 统一小写。"""
    tokens = []
    for token in jieba.cut_for_search(str(text)):
        token = token.strip().lower()
        if token and any(ch.isalnum() for ch in token):
            tokens.append(token)
    return tokens


# ----------------------------------------------------------- 索引缓存

def _prune_legacy_dirs():
    """删除旧版按 CSV 文件分目录的缓存。"""
    if not SEARCH_INDEX_DIR.exists():
        return
    for entry in SEARCH_INDEX_DIR.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _read_meta():
    path = SEARCH_INDEX_DIR / "meta.csv"
    if not path.exists():
        return None
    meta = pd.read_csv(path, dtype=str).fillna("")
    if meta.empty:
        return None
    row = meta.iloc[0].to_dict()
    if _as_int(row.get("version")) != INDEX_VERSION:
        return None
    return row


def _current_watermark(conn):
    data_version = newsdata.data_version()
    doc_count = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    return "{}:{}".format(data_version, doc_count)


def _build_index(conn):
    """全量分词并写入索引缓存；与读取同一事务保证一致性。"""
    SEARCH_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _prune_legacy_dirs()

    rows = conn.execute("SELECT id, content FROM messages ORDER BY id").fetchall()
    postings = []
    term_df = defaultdict(int)
    lengths = []
    total_length = 0

    for row in rows:
        doc_id = row["id"]
        tokens = _tokenize(row["content"])
        lengths.append({"doc_id": doc_id, "length": len(tokens)})
        total_length += len(tokens)
        tf = defaultdict(int)
        for token in tokens:
            tf[token] += 1
        for term, count in tf.items():
            postings.append({"doc_id": doc_id, "term": term, "tf": count})
        for term in tf:
            term_df[term] += 1

    doc_count = len(rows)
    pd.DataFrame(postings, columns=POSTING_COLUMNS).to_csv(
        SEARCH_INDEX_DIR / "postings.csv", index=False)
    pd.DataFrame(lengths, columns=DOC_LENGTH_COLUMNS).to_csv(
        SEARCH_INDEX_DIR / "doc_lengths.csv", index=False)
    pd.DataFrame(
        [{"term": term, "df": count} for term, count in sorted(term_df.items())],
        columns=TERM_COLUMNS,
    ).to_csv(SEARCH_INDEX_DIR / "terms.csv", index=False)

    watermark = _current_watermark(conn)
    avgdl = (total_length / doc_count) if doc_count else 0.0
    pd.DataFrame([{
        "watermark": watermark,
        "doc_count": doc_count,
        "total_length": total_length,
        "avgdl": "{:.6f}".format(avgdl),
        "version": INDEX_VERSION,
    }], columns=META_COLUMNS + ["avgdl"]).to_csv(
        SEARCH_INDEX_DIR / "meta.csv", index=False)


def _ensure_index():
    """索引与数据水印一致时复用缓存，否则重建。"""
    SEARCH_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    with db.db_conn() as conn:
        meta = _read_meta()
        if meta is not None and meta.get("watermark") == _current_watermark(conn):
            return meta
        _build_index(conn)
        meta = _read_meta()
    if meta is None:
        return None
    return meta


# --------------------------------------------------------------- 搜索

def _idf(doc_count, df):
    if doc_count <= 0:
        return 0.0
    return math.log(1 + (doc_count - df + 0.5) / (df + 0.5))


def search_ranked_ids(query, limit=500):
    """BM25 检索，返回按相关度排序的消息 ID 列表（供筛选分页复用）。"""
    tokens = _tokenize(query)
    if not tokens or limit <= 0:
        return []

    meta = _ensure_index()
    if meta is None:
        return []

    doc_count = _as_int(meta.get("doc_count"))
    try:
        avgdl = float(meta.get("avgdl") or 0)
    except (TypeError, ValueError):
        avgdl = 0.0
    if doc_count <= 0 or avgdl <= 0:
        return []

    terms_path = SEARCH_INDEX_DIR / "terms.csv"
    terms = pd.read_csv(terms_path, dtype=str).fillna("") if terms_path.exists() else pd.DataFrame()
    if terms.empty:
        return []

    query_terms = set(tokens)
    idf_map = {}
    for _, row in terms.iterrows():
        term = row["term"]
        if term in query_terms:
            idf_map[term] = _idf(doc_count, _as_int(row["df"]))
    if not idf_map:
        return []

    postings_path = SEARCH_INDEX_DIR / "postings.csv"
    postings = pd.read_csv(postings_path, dtype=str).fillna("") if postings_path.exists() else pd.DataFrame()
    if postings.empty:
        return []
    postings = postings[postings["term"].isin(query_terms)]
    if postings.empty:
        return []

    lengths_path = SEARCH_INDEX_DIR / "doc_lengths.csv"
    lengths = pd.read_csv(lengths_path, dtype=str).fillna("") if lengths_path.exists() else pd.DataFrame(columns=DOC_LENGTH_COLUMNS)
    length_map = {
        _as_int(doc_id): _as_int(length)
        for doc_id, length in zip(lengths["doc_id"], lengths["length"])
    }

    scores = defaultdict(float)
    for doc_id, term, tf in zip(postings["doc_id"], postings["term"], postings["tf"]):
        idf = idf_map.get(term)
        if idf is None:
            continue
        tf_value = float(tf)
        doc_id = _as_int(doc_id)
        dl = length_map.get(doc_id, 0)
        denominator = tf_value + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
        if denominator == 0:
            continue
        scores[doc_id] += idf * tf_value * (BM25_K1 + 1) / denominator

    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return [doc_id for doc_id, _ in ranked[:limit]]


def search(query, limit=3):
    """BM25 检索，返回按相关度排序的消息 dict 列表（含 id）。"""
    ranked_ids = search_ranked_ids(query, limit)
    results = []
    for message_id in ranked_ids:
        message = newsdata.get_message(message_id)
        if message is not None:
            results.append(message)
    return results
