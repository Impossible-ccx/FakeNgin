"""newsdata 消息数据访问层。

database/newsdata/ 下的所有 csv 视为同一张逻辑表，格式一致：
    content, nature, fake_probability, source, publish_time, process_time,
    risk_score, risk_model, risk_reason, risk_prompt_version

风险字段与历史真假分类字段分开；旧 CSV 读取时补空列，不推断旧分数含义。

读取时在内存中为每行附加 _file（来源文件名）、_row（文件内行号）、
_signature（内容指纹），用于确保人工修改/删除命中的确实是目标文件的目标行。
这三个字段不会写回 csv。
"""

import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

import pandas as pd

from .db import DATABASE_DIR

NEWSDATA_DIR = DATABASE_DIR / "newsdata"
MANUAL_FILE = "manual.csv"

COLUMNS = [
    "content",
    "nature",
    "fake_probability",
    "source",
    "publish_time",
    "process_time",
    "risk_score",
    "risk_model",
    "risk_reason",
    "risk_prompt_version",
]
RISK_COLUMNS = {"risk_score", "risk_model", "risk_reason", "risk_prompt_version"}
NATURES = ["虚假", "真实", "中立", "未校验"]
VERIFY_NATURES = ["虚假", "真实", "中立"]
DEFAULT_NATURE = "未校验"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TIME_INPUT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
]

META_COLUMNS = ["_file", "_row", "_signature"]

# CSV 是唯一的数据来源；缓存只保存读到的快照，不持久化额外状态。
# 版本同时包含文件身份与纳秒时间，覆盖外部编辑和同名文件的原子替换。
_TABLE_CACHE_LIMIT = 32
_ALL_CACHE_LIMIT = 8
_SNAPSHOT_ATTEMPTS = 3
_CACHE_LOCK = RLock()
_TABLE_CACHE = OrderedDict()
_ALL_CACHE = OrderedDict()


@dataclass
class _TableSnapshot:
    frame: pd.DataFrame
    signatures: list | None = None


def _stat_version(stat):
    return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
            stat.st_dev, stat.st_ino)


def clear_cache():
    """清空内存快照（运维及测试可用）；不触碰任何 CSV。"""
    with _CACHE_LOCK:
        _TABLE_CACHE.clear()
        _ALL_CACHE.clear()


def dataset_fingerprint():
    """返回当前 CSV 集合的可哈希版本键，供搜索等派生缓存使用。"""
    with _CACHE_LOCK:
        directory = Path(NEWSDATA_DIR).resolve()
        for _ in range(_SNAPSHOT_ATTEMPTS):
            try:
                files = tuple(
                    (path.name, str(path.resolve()), _stat_version(path.stat()))
                    for path in sorted(directory.glob("*.csv"))
                )
                return str(directory), files
            except FileNotFoundError:
                # 外部进程正在删除或替换文件，重新获取完整文件清单。
                continue
        raise OSError("消息文件正在变化，请稍后重试")


def _remember(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _invalidate_path(path):
    resolved = str(path.resolve())
    directory = str(path.parent.resolve())
    for key in list(_TABLE_CACHE):
        if key[0] == resolved:
            del _TABLE_CACHE[key]
    for key in list(_ALL_CACHE):
        if key[0] == directory or any(item[1] == resolved for item in key[1]):
            del _ALL_CACHE[key]


def _table_snapshot(path):
    """在缓存锁内读取稳定快照，绝不把旧内容存到新文件的版本键下。"""
    resolved = str(path.resolve())
    version = _stat_version(path.stat())
    key = (resolved, version)
    cached = _TABLE_CACHE.get(key)
    if cached is not None:
        _TABLE_CACHE.move_to_end(key)
        return cached

    for _ in range(_SNAPSHOT_ATTEMPTS):
        try:
            path_before = _stat_version(path.stat())
            with path.open("rb") as source:
                before = _stat_version(os.fstat(source.fileno()))
                frame = pd.read_csv(source, dtype=str).fillna("")
                after = _stat_version(os.fstat(source.fileno()))
            current = _stat_version(path.stat())
        except FileNotFoundError:
            continue
        # Windows 的 fstat 与 Path.stat 对 ctime 的解释可能不同；
        # 分别比较打开句柄和路径的前后版本，再比较共同的文件身份字段。
        handle_identity = (after[0], *after[2:])
        path_identity = (current[0], *current[2:])
        if before != after or path_before != current or handle_identity != path_identity:
            continue
        frame = frame.reindex(columns=COLUMNS).fillna("").reset_index(drop=True)
        snapshot = _TableSnapshot(frame)
        # 每个实际文件仅留一个版本，避免频繁编辑挤占缓存。
        for old_key in list(_TABLE_CACHE):
            if old_key[0] == resolved:
                del _TABLE_CACHE[old_key]
        _remember(_TABLE_CACHE, (resolved, current), snapshot, _TABLE_CACHE_LIMIT)
        return snapshot
    raise OSError("消息文件正在变化，请稍后重试")


# ------------------------------------------------------------ 基础读写

def ensure_newsdata():
    """确保 newsdata 目录与 manual.csv 存在。"""
    with _CACHE_LOCK:
        NEWSDATA_DIR.mkdir(parents=True, exist_ok=True)
        if not (NEWSDATA_DIR / MANUAL_FILE).exists():
            _write_path(
                NEWSDATA_DIR / MANUAL_FILE,
                pd.DataFrame(columns=COLUMNS),
            )


def list_tables():
    if not NEWSDATA_DIR.exists():
        return []
    return sorted(p.name for p in NEWSDATA_DIR.glob("*.csv"))


def _table_path(name):
    """校验 name 为 newsdata 下已存在的 csv 文件名，防路径穿越。"""
    if not name or Path(name).name != name or not name.lower().endswith(".csv"):
        raise ValueError("非法的消息表名")
    path = NEWSDATA_DIR / name
    if not path.exists():
        raise ValueError("消息表不存在")
    return path


def read_table(name):
    with _CACHE_LOCK:
        return _table_snapshot(_table_path(name)).frame.copy(deep=True)


def _write_path(path, df):
    """原子写入：先写临时文件再替换，避免中途损坏。"""
    with _CACHE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                    dir=path.parent, prefix=f".{path.name}.",
                                    suffix=".tmp", delete=False) as output:
                tmp = Path(output.name)
                df.to_csv(output, index=False, columns=COLUMNS)
            tmp.replace(path)
            _invalidate_path(path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def write_table(name, df):
    """整表写回（供批量脚本使用），name 需为已存在的表名。"""
    _write_path(_table_path(name), df)


def _signature(row):
    raw = "\x1f".join(str(row[col]) for col in COLUMNS)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def signature(row):
    """公开的内容指纹计算（供搜索等模块复用）。"""
    return _signature(row)


# -------------------------------------------------------------- 聚合读取

def load_all():
    """把所有 newsdata 表聚合成一张逻辑表，附加 _file/_row/_signature。"""
    with _CACHE_LOCK:
        for _ in range(_SNAPSHOT_ATTEMPTS):
            fingerprint = dataset_fingerprint()
            cached = _ALL_CACHE.get(fingerprint)
            if cached is not None:
                _ALL_CACHE.move_to_end(fingerprint)
                return cached.copy(deep=True)
            frames = []
            try:
                for name, _, _version in fingerprint[1]:
                    snapshot = _table_snapshot(_table_path(name))
                    if snapshot.signatures is None:
                        snapshot.signatures = [_signature(row) for _, row in snapshot.frame.iterrows()]
                    frame = snapshot.frame.copy(deep=True)
                    frame[META_COLUMNS[0]] = name
                    frame[META_COLUMNS[1]] = frame.index
                    frame[META_COLUMNS[2]] = snapshot.signatures
                    frames.append(frame)
            except (FileNotFoundError, ValueError):
                # 只重试文件集变化；CSV 格式错误仍应原样报告。
                if dataset_fingerprint() == fingerprint:
                    raise
                continue
            if dataset_fingerprint() != fingerprint:
                continue
            result = (pd.concat(frames, ignore_index=True) if frames
                      else pd.DataFrame(columns=COLUMNS + META_COLUMNS))
            _remember(_ALL_CACHE, fingerprint, result, _ALL_CACHE_LIMIT)
            return result.copy(deep=True)
        raise OSError("消息文件正在变化，请稍后重试")


# ------------------------------------------------------------------ 搜索

def search_messages(query, limit=3):
    """按查询返回高相关消息（转发到 bm25 模块）。"""
    from .bm25 import search

    return search(query, limit)


# ------------------------------------------------------------------ 写入

def append_message(data):
    """人工添加消息，写入 manual.csv。"""
    with _CACHE_LOCK:
        path = NEWSDATA_DIR / MANUAL_FILE
        if path.exists():
            df = read_table(MANUAL_FILE)
        else:
            df = pd.DataFrame(columns=COLUMNS)

        row = {col: data.get(col, "") for col in COLUMNS}
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        _write_path(path, df)


def _locate(name, row, signature):
    """校验目标文件与目标行，返回 (path, df, row)。"""
    path = _table_path(name)
    df = read_table(name)
    if row < 0 or row >= len(df):
        raise ValueError("消息不存在，可能已被删除")
    if signature and signature != _signature(df.iloc[row]):
        raise ValueError("消息已发生变化，请刷新后重试")
    return path, df


def update_message(name, row, signature, data):
    with _CACHE_LOCK:
        path, df = _locate(name, row, signature)
        content_changed = "content" in data and data["content"] != df.at[row, "content"]
        for col in COLUMNS:
            if col in data:
                df.at[row, col] = data[col]
        if content_changed:
            # 风险结果只适用于原文；正文修改后允许后续批处理重新评分。
            for col in RISK_COLUMNS:
                df.at[row, col] = ""
        _write_path(path, df)


def delete_message(name, row, signature):
    with _CACHE_LOCK:
        path, df = _locate(name, row, signature)
        df = df.drop(index=row).reset_index(drop=True)
        _write_path(path, df)


# -------------------------------------------------------------- 值规范化

def normalize_row(form):
    """把表单数据规范化为一行标准消息。表单缺省字段按空处理。"""
    # 旧的消息编辑表单不负责机器风险字段，避免一次编辑清空已有风险结果。
    return {
        col: normalize_value(col, form.get(col, ""))
        for col in COLUMNS if col not in RISK_COLUMNS
    }


def normalize_value(field, value):
    value = (value or "").strip()
    if field == "nature":
        return value if value in NATURES else DEFAULT_NATURE
    if field == "fake_probability":
        if value == "":
            return ""
        try:
            num = float(value)
        except ValueError:
            raise ValueError("虚假概率必须是数字")
        num = max(0.0, min(100.0, num))
        return f"{num:.2f}"
    if field in ("publish_time", "process_time"):
        return _normalize_time(value)
    return value


def _normalize_time(value):
    if not value:
        return ""
    for fmt in TIME_INPUT_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime(TIME_FORMAT)
        except ValueError:
            continue
    raise ValueError("时间格式不正确")


def now_string():
    return datetime.now().strftime(TIME_FORMAT)
