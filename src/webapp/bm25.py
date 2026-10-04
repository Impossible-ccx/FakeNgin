"""BM25 搜索与索引缓存。

本模块封装了搜索相关的全部逻辑：jieba 分词、BM25 打分，以及按文件增量的
索引缓存（IDF 等统计量持久化到 database/searchindex/ 下的 csv）。

消息数据通过 newsdata 模块的公开接口获取：
    newsdata.list_tables() / newsdata.read_table(name)
"""

import hashlib
import json
import math
import shutil
from collections import OrderedDict, defaultdict
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading

import jieba
import pandas as pd

from . import newsdata
from .db import DATABASE_DIR

SEARCH_INDEX_DIR = DATABASE_DIR / "searchindex"

INDEX_VERSION = 2
BM25_K1 = 1.5
BM25_B = 0.75

GLOBAL_META_FILE = SEARCH_INDEX_DIR / "meta.csv"
GLOBAL_TERMS_FILE = SEARCH_INDEX_DIR / "terms.csv"

GLOBAL_META_COLUMNS = ["doc_count", "avgdl", "updated_at", "version", "index_fingerprint"]
GLOBAL_TERMS_COLUMNS = ["term", "df", "idf"]
FILE_META_COLUMNS = [
    "fingerprint",
    "doc_count",
    "total_length",
    "mtime_ns",
    "ctime_ns",
    "device",
    "inode",
    "size",
    "source_file",
    "version",
]
POSTING_COLUMNS = ["doc_id", "term", "tf"]
DOC_LENGTH_COLUMNS = ["doc_id", "length"]
TERM_COLUMNS = ["term", "df"]


# ------------------------------------------------------------ csv 读写

from .db import _read_csv as _read_index_csv

_cache_lock = threading.RLock()
_csv_cache = OrderedDict()
_snapshots = OrderedDict()
_rankings = OrderedDict()


def clear_cache():
    """清理派生索引的进程内缓存，不删除消息或磁盘索引。"""
    with _cache_lock:
        _csv_cache.clear()
        _snapshots.clear()
        _rankings.clear()
        _query_tokens.cache_clear()


def _stat_key(path):
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_dev, stat.st_ino)


def _read_csv(path, columns):
    """索引 CSV 同样按文件版本缓存，反复查询无需反复反序列化倒排表。"""
    path = Path(path).resolve()
    if not path.exists():
        return pd.DataFrame(columns=columns)
    with _cache_lock:
        for _ in range(3):
            key = (str(path), _stat_key(path), tuple(columns))
            if key in _csv_cache:
                _csv_cache.move_to_end(key)
                return _csv_cache[key].copy(deep=True)
            frame = _read_index_csv(path, columns)
            if _stat_key(path) != key[1]:
                continue
            for old_key in list(_csv_cache):
                if old_key[0] == str(path):
                    del _csv_cache[old_key]
            _csv_cache[key] = frame
            while len(_csv_cache) > 128:
                _csv_cache.popitem(last=False)
            return frame.copy(deep=True)
        raise OSError("Search index changed while reading")


def _write_csv(frame, path, columns):
    """独立临时文件原子替换，避免两个索引请求共用同一个 .tmp。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                prefix="." + path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            frame.to_csv(output, index=False, columns=columns)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------- 分词

def _tokenize(text):
    """jieba 搜索引擎模式分词，过滤空白与纯标点，ASCII 统一小写。"""
    tokens = []
    for token in jieba.cut_for_search(str(text).casefold()):
        token = token.strip().casefold()
        if token and any(ch.isalnum() for ch in token):
            tokens.append(token)
    return tokens


@lru_cache(maxsize=128)
def _query_tokens(query):
    return tuple(_tokenize(query))


def _content_fingerprint(contents):
    raw = "\x1f".join(str(content) for content in contents)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _search_texts(table):
    return (str(content) + " " + str(source) for content, source in zip(table["content"], table["source"]))


def _stat_values(stat):
    return dict(mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns,
                size=stat.st_size, device=stat.st_dev, inode=stat.st_ino)


def _same_stat(meta, stat):
    return all(str(meta.get(key)) == str(value) for key, value in _stat_values(stat).items())


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

    for doc_id, content in enumerate(_search_texts(table)):
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
        pd.DataFrame(postings, columns=POSTING_COLUMNS),base / "postings.csv",
        POSTING_COLUMNS,
    )
    _write_csv(
        
        pd.DataFrame(
            [{"doc_id": i, "length": n} for i, n in enumerate(lengths)],
            columns=DOC_LENGTH_COLUMNS,
        ),base / "doc_lengths.csv",
        DOC_LENGTH_COLUMNS,
    )
    _write_csv(
        pd.DataFrame(
            [{"term": term, "df": count} for term, count in sorted(term_df.items())],
            columns=TERM_COLUMNS,
        ),base / "terms.csv",
        TERM_COLUMNS,
    )

    meta = pd.DataFrame([{
        "fingerprint": fingerprint,
        "doc_count": len(table),
        "total_length": int(sum(lengths)),
        **_stat_values(stat),
        "source_file": name,
        "version": INDEX_VERSION,
    }], columns=FILE_META_COLUMNS)
    _write_csv(meta,base / "meta.csv",  FILE_META_COLUMNS)


def _update_file_meta_stat(name, meta, stat):
    """内容未变、仅文件属性变化时，只刷新 mtime/size。"""
    updated = dict(meta)
    updated.update(_stat_values(stat))
    _write_csv(
        pd.DataFrame([updated], columns=FILE_META_COLUMNS),_file_dir(name) / "meta.csv",
        FILE_META_COLUMNS,
    )


def _prune_index(valid_names):
    """删除已不存在的表对应的缓存目录，返回是否有删除。"""
    if not SEARCH_INDEX_DIR.exists():
        return False
    valid = set(valid_names)
    removed = False
    root = SEARCH_INDEX_DIR.resolve()
    for entry in SEARCH_INDEX_DIR.iterdir():
        if (entry.is_dir() and entry.name not in valid
                and entry.resolve().is_relative_to(root)
                and (entry / "meta.csv").is_file()
                and (entry / "postings.csv").is_file()):
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


def _index_fingerprint(names):
    """全局统计绑定实际的每表索引，部分写入失败后下次查询能重建。"""
    versions = []
    for name in names:
        meta = _read_file_meta(name) or {}
        versions.append((name, meta.get("fingerprint"), meta.get("doc_count"),
                         meta.get("total_length"), meta.get("version")))
    return hashlib.sha1(json.dumps(versions, ensure_ascii=False).encode("utf-8")).hexdigest()


def _refresh_global_terms(names, index_fingerprint):
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
    _write_csv(grouped[GLOBAL_TERMS_COLUMNS],GLOBAL_TERMS_FILE,  GLOBAL_TERMS_COLUMNS)

    avgdl = (total_length / doc_count) if doc_count else 0.0
    meta_df = pd.DataFrame([{
        "doc_count": doc_count,
        "avgdl": f"{avgdl:.6f}",
        "updated_at": newsdata.now_string(),
        "version": INDEX_VERSION,
        "index_fingerprint": index_fingerprint,
    }], columns=GLOBAL_META_COLUMNS)
    _write_csv( meta_df,GLOBAL_META_FILE, GLOBAL_META_COLUMNS)


# --------------------------------------------------------------- 搜索

def _build_snapshot():
    """只更新有变化文件的磁盘索引，再构造复用的内存倒排表。"""
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
        version_ok = (meta is not None and _as_int(meta.get("version")) == INDEX_VERSION
                      and all((_file_dir(name) / filename).is_file()
                              for filename in ("postings.csv", "doc_lengths.csv", "terms.csv")))
        if version_ok and _same_stat(meta, stat):
            continue

        table = newsdata.read_table(name)
        fingerprint = _content_fingerprint(_search_texts(table))
        if version_ok and meta.get("fingerprint") == fingerprint:
            _update_file_meta_stat(name, meta, stat)
        else:
            _build_file_index(name, table, fingerprint, stat)
            needs_global_refresh = True

    global_meta = _read_global_meta()
    index_fingerprint = _index_fingerprint(names)
    if (needs_global_refresh or global_meta is None
            or global_meta.get("index_fingerprint") != index_fingerprint
            or not GLOBAL_TERMS_FILE.is_file()):
        _refresh_global_terms(names, index_fingerprint)
        global_meta = _read_global_meta()
    if global_meta is None:
        return {"avgdl": 0, "idf": {}, "postings": {}}

    doc_count = _as_int(global_meta.get("doc_count"))
    try:
        avgdl = float(global_meta.get("avgdl") or 0)
    except (TypeError, ValueError):
        avgdl = 0.0
    if doc_count <= 0 or avgdl <= 0:
        return {"avgdl": 0, "idf": {}, "postings": {}}

    terms = _load_global_terms()
    if terms.empty:
        return {"avgdl": 0, "idf": {}, "postings": {}}
    idf_map = {}
    for _, row in terms.iterrows():
        try:
            idf_map[row["term"]] = float(row["idf"])
        except (TypeError, ValueError):
            continue

    by_term = defaultdict(list)
    for name in names:
        postings = _read_csv(_file_dir(name) / "postings.csv", POSTING_COLUMNS)
        if postings.empty:
            continue

        lengths = _read_csv(_file_dir(name) / "doc_lengths.csv", DOC_LENGTH_COLUMNS)
        length_map = {
            _as_int(doc_id): _as_int(length)
            for doc_id, length in zip(lengths["doc_id"], lengths["length"])
        }

        for doc_id, term, tf in zip(postings["doc_id"], postings["term"], postings["tf"]):
            by_term[term].append((name, _as_int(doc_id), float(tf), length_map.get(_as_int(doc_id), 0)))
    return {"avgdl": avgdl, "idf": idf_map, "postings": dict(by_term)}


def ranked_references(query):
    """返回全部匹配的文件/行号，复用原 BM25 索引并缓存查询排序。"""
    query = str(query).strip().casefold()
    tokens = _query_tokens(query)
    if not tokens:
        return []
    with _cache_lock:
        for _ in range(3):
            version = newsdata.dataset_fingerprint()
            key = (str(SEARCH_INDEX_DIR.resolve()), INDEX_VERSION, version)
            if key not in _snapshots:
                snapshot = _build_snapshot()
                if newsdata.dataset_fingerprint() != version:
                    continue
                _snapshots[key] = snapshot
                while len(_snapshots) > 4:
                    _snapshots.popitem(last=False)
            _snapshots.move_to_end(key)
            query_key = (key, tokens)
            if query_key not in _rankings:
                snapshot = _snapshots[key]
                scores = defaultdict(float)
                avgdl = snapshot["avgdl"]
                for token in set(tokens):
                    idf = snapshot["idf"].get(token, 0)
                    for name, doc_id, tf, dl in snapshot["postings"].get(token, ()):
                        denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
                        if denominator:
                            scores[(name, doc_id)] += idf * tf * (BM25_K1 + 1) / denominator
                ranked = tuple(reference for reference, score in sorted(
                    scores.items(), key=lambda item: (-item[1], item[0][0], item[0][1]),
                ) if score > 0)
                _rankings[query_key] = ranked
                while len(_rankings) > 128:
                    _rankings.popitem(last=False)
            _rankings.move_to_end(query_key)
            return list(_rankings[query_key])
        raise RuntimeError("Dataset changed while rebuilding search index")


def search(query, limit=3):
    """兼容 main 的相关性检索入口，默认仍返回最高相关的三条消息。"""
    if limit <= 0:
        return []
    ranked = ranked_references(query)[:limit]

    hit_files = {}
    for name, _doc_id in ranked:
        if name not in hit_files:
            hit_files[name] = newsdata.read_table(name)

    results = []
    for name, doc_id in ranked:
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
