"""BM25 搜索与索引缓存。

本模块封装了搜索相关的全部逻辑：jieba 分词、BM25 打分，以及按文件增量的
索引缓存（IDF 等统计量持久化到 database/searchindex/ 下的 csv）。

消息数据通过 newsdata 模块的公开接口获取：
    newsdata.list_tables() / newsdata.read_table(name)
"""

import hashlib
import math
import shutil
from collections import defaultdict

import jieba
import pandas as pd

from . import newsdata
from .db import DATABASE_DIR

SEARCH_INDEX_DIR = DATABASE_DIR / "searchindex"

INDEX_VERSION = 1
BM25_K1 = 1.5
BM25_B = 0.75

GLOBAL_META_FILE = SEARCH_INDEX_DIR / "meta.csv"
GLOBAL_TERMS_FILE = SEARCH_INDEX_DIR / "terms.csv"

GLOBAL_META_COLUMNS = ["doc_count", "avgdl", "updated_at", "version"]
GLOBAL_TERMS_COLUMNS = ["term", "df", "idf"]
FILE_META_COLUMNS = [
    "fingerprint",
    "doc_count",
    "total_length",
    "mtime_ns",
    "size",
    "source_file",
    "version",
]
POSTING_COLUMNS = ["doc_id", "term", "tf"]
DOC_LENGTH_COLUMNS = ["doc_id", "length"]
TERM_COLUMNS = ["term", "df"]


# ------------------------------------------------------------ csv 读写

from .db import _read_csv, _write_csv


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


def _content_fingerprint(contents):
    raw = "\x1f".join(str(content) for content in contents)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ----------------------------------------------------------- 每表缓存

def _file_dir(name):
    return SEARCH_INDEX_DIR / name


def _read_file_meta(name):
    meta = _read_csv(_file_dir(name) / "meta.csv", FILE_META_COLUMNS)
    if meta.empty:
        return None
    return meta.iloc[0].to_dict()


def _build_file_index(name, table, fingerprint, stat):
    """对单张表分词并写入其缓存。"""
    postings = []
    term_df = defaultdict(int)
    lengths = []

    for doc_id, content in enumerate(table["content"]):
        tokens = _tokenize(content)
        lengths.append(len(tokens))
        tf = defaultdict(int)
        for token in tokens:
            tf[token] += 1
        for term, count in tf.items():
            postings.append({"doc_id": doc_id, "term": term, "tf": count})
        for term in tf:
            term_df[term] += 1

    base = _file_dir(name)
    _write_csv(
        base / "postings.csv",
        pd.DataFrame(postings, columns=POSTING_COLUMNS),
        POSTING_COLUMNS,
    )
    _write_csv(
        base / "doc_lengths.csv",
        pd.DataFrame(
            [{"doc_id": i, "length": n} for i, n in enumerate(lengths)],
            columns=DOC_LENGTH_COLUMNS,
        ),
        DOC_LENGTH_COLUMNS,
    )
    _write_csv(
        base / "terms.csv",
        pd.DataFrame(
            [{"term": term, "df": count} for term, count in sorted(term_df.items())],
            columns=TERM_COLUMNS,
        ),
        TERM_COLUMNS,
    )

    meta = pd.DataFrame([{
        "fingerprint": fingerprint,
        "doc_count": len(table),
        "total_length": int(sum(lengths)),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "source_file": name,
        "version": INDEX_VERSION,
    }], columns=FILE_META_COLUMNS)
    _write_csv(base / "meta.csv", meta, FILE_META_COLUMNS)


def _update_file_meta_stat(name, meta, stat):
    """内容未变、仅文件属性变化时，只刷新 mtime/size。"""
    updated = dict(meta)
    updated["mtime_ns"] = stat.st_mtime_ns
    updated["size"] = stat.st_size
    _write_csv(
        _file_dir(name) / "meta.csv",
        pd.DataFrame([updated], columns=FILE_META_COLUMNS),
        FILE_META_COLUMNS,
    )


def _prune_index(valid_names):
    """删除已不存在的表对应的缓存目录，返回是否有删除。"""
    if not SEARCH_INDEX_DIR.exists():
        return False
    valid = set(valid_names)
    removed = False
    for entry in SEARCH_INDEX_DIR.iterdir():
        if entry.is_dir() and entry.name not in valid:
            shutil.rmtree(entry, ignore_errors=True)
            removed = True
    return removed


# ---------------------------------------------------------- 全局统计

def _idf(doc_count, df):
    if doc_count <= 0:
        return 0.0
    return math.log(1 + (doc_count - df + 0.5) / (df + 0.5))


def _read_global_meta():
    meta = _read_csv(GLOBAL_META_FILE, GLOBAL_META_COLUMNS)
    if meta.empty:
        return None
    row = meta.iloc[0].to_dict()
    if _as_int(row.get("version")) != INDEX_VERSION:
        return None
    return row


def _load_global_terms():
    return _read_csv(GLOBAL_TERMS_FILE, GLOBAL_TERMS_COLUMNS)


def _refresh_global_terms(names):
    """汇总各表 df，计算并缓存全局 df/idf。"""
    frames = []
    doc_count = 0
    total_length = 0

    for name in names:
        terms = _read_csv(_file_dir(name) / "terms.csv", TERM_COLUMNS)
        if not terms.empty:
            terms = terms.copy()
            terms["df"] = pd.to_numeric(terms["df"], errors="coerce").fillna(0)
            frames.append(terms[TERM_COLUMNS])
        meta = _read_file_meta(name)
        if meta and _as_int(meta.get("version")) == INDEX_VERSION:
            doc_count += _as_int(meta.get("doc_count"))
            total_length += _as_int(meta.get("total_length"))

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        combined["df"] = pd.to_numeric(combined["df"], errors="coerce").fillna(0)
        grouped = combined.groupby("term", as_index=False)["df"].sum()
    else:
        grouped = pd.DataFrame(columns=TERM_COLUMNS)

    grouped["idf"] = [_idf(doc_count, float(df)) for df in grouped["df"]]
    _write_csv(GLOBAL_TERMS_FILE, grouped[GLOBAL_TERMS_COLUMNS], GLOBAL_TERMS_COLUMNS)

    avgdl = (total_length / doc_count) if doc_count else 0.0
    meta_df = pd.DataFrame([{
        "doc_count": doc_count,
        "avgdl": f"{avgdl:.6f}",
        "updated_at": newsdata.now_string(),
        "version": INDEX_VERSION,
    }], columns=GLOBAL_META_COLUMNS)
    _write_csv(GLOBAL_META_FILE, meta_df, GLOBAL_META_COLUMNS)


# --------------------------------------------------------------- 搜索

def search(query, limit=3):
    tokens = _tokenize(query)
    if not tokens or limit <= 0:
        return []

    names = newsdata.list_tables()
    SEARCH_INDEX_DIR.mkdir(parents=True, exist_ok=True)

    needs_global_refresh = _prune_index(names)
    for name in names:
        path = newsdata.NEWSDATA_DIR / name
        try:
            stat = path.stat()
        except OSError:
            continue

        meta = _read_file_meta(name)
        version_ok = meta is not None and _as_int(meta.get("version")) == INDEX_VERSION
        if (version_ok
                and str(meta.get("mtime_ns")) == str(stat.st_mtime_ns)
                and str(meta.get("size")) == str(stat.st_size)):
            continue

        table = newsdata.read_table(name)
        fingerprint = _content_fingerprint(table["content"])
        if version_ok and meta.get("fingerprint") == fingerprint:
            _update_file_meta_stat(name, meta, stat)
        else:
            _build_file_index(name, table, fingerprint, stat)
            needs_global_refresh = True

    global_meta = _read_global_meta()
    if needs_global_refresh or global_meta is None:
        _refresh_global_terms(names)
        global_meta = _read_global_meta()
    if global_meta is None:
        return []

    doc_count = _as_int(global_meta.get("doc_count"))
    try:
        avgdl = float(global_meta.get("avgdl") or 0)
    except (TypeError, ValueError):
        avgdl = 0.0
    if doc_count <= 0 or avgdl <= 0:
        return []

    terms = _load_global_terms()
    if terms.empty:
        return []
    idf_map = {}
    for _, row in terms.iterrows():
        try:
            idf_map[row["term"]] = float(row["idf"])
        except (TypeError, ValueError):
            continue

    query_terms = set(tokens)
    scores = defaultdict(float)

    for name in names:
        postings = _read_csv(_file_dir(name) / "postings.csv", POSTING_COLUMNS)
        if postings.empty:
            continue
        postings = postings[postings["term"].isin(query_terms)]
        if postings.empty:
            continue

        lengths = _read_csv(_file_dir(name) / "doc_lengths.csv", DOC_LENGTH_COLUMNS)
        length_map = {
            _as_int(doc_id): _as_int(length)
            for doc_id, length in zip(lengths["doc_id"], lengths["length"])
        }

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
            scores[(name, doc_id)] += idf * tf_value * (BM25_K1 + 1) / denominator

    if not scores:
        return []

    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], item[0][0], item[0][1]),
    )[:limit]

    hit_files = {}
    for (name, _doc_id), _score in ranked:
        if name not in hit_files:
            hit_files[name] = newsdata.read_table(name)

    results = []
    for (name, doc_id), _score in ranked:
        table = hit_files[name]
        if doc_id < 0 or doc_id >= len(table):
            continue
        row = table.iloc[doc_id]
        record = row.to_dict()
        record["_file"] = name
        record["_row"] = doc_id
        record["_signature"] = newsdata.signature(row)
        results.append(record)
    return results
