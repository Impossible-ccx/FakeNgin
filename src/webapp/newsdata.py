"""newsdata 消息数据访问层。

database/newsdata/ 下的所有 csv 视为同一张逻辑表，格式一致：
    content, nature, fake_probability, source, publish_time, process_time

读取时在内存中为每行附加 _file（来源文件名）、_row（文件内行号）、
_signature（内容指纹），用于确保人工修改/删除命中的确实是目标文件的目标行。
这三个字段不会写回 csv。
"""

import hashlib
from datetime import datetime
from pathlib import Path

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
]
NATURES = ["虚假", "真实", "未校验"]
VERIFY_NATURES = ["虚假", "真实"]
DEFAULT_NATURE = "未校验"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TIME_INPUT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
]

META_COLUMNS = ["_file", "_row", "_signature"]


# ------------------------------------------------------------ 基础读写

def ensure_newsdata():
    """确保 newsdata 目录与 manual.csv 存在。"""
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


def _read_table(name):
    df = pd.read_csv(_table_path(name), dtype=str).fillna("")
    return df.reindex(columns=COLUMNS).fillna("").reset_index(drop=True)


def _write_path(path, df):
    """原子写入：先写临时文件再替换，避免中途损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, columns=COLUMNS)
    tmp.replace(path)


def _signature(row):
    raw = "\x1f".join(str(row[col]) for col in COLUMNS)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# -------------------------------------------------------------- 聚合读取

def load_all():
    """把所有 newsdata 表聚合成一张逻辑表，附加 _file/_row/_signature。"""
    frames = []
    for name in list_tables():
        df = _read_table(name)
        df[META_COLUMNS[0]] = name
        df[META_COLUMNS[1]] = df.index
        df[META_COLUMNS[2]] = [_signature(row) for _, row in df.iterrows()]
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=COLUMNS + META_COLUMNS)
    return pd.concat(frames, ignore_index=True)


# ------------------------------------------------------------------ 写入

def append_message(data):
    """人工添加消息，写入 manual.csv。"""
    path = NEWSDATA_DIR / MANUAL_FILE
    if path.exists():
        df = _read_table(MANUAL_FILE)
    else:
        df = pd.DataFrame(columns=COLUMNS)

    row = {col: data.get(col, "") for col in COLUMNS}
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _write_path(path, df)


def _locate(name, row, signature):
    """校验目标文件与目标行，返回 (path, df, row)。"""
    path = _table_path(name)
    df = _read_table(name)
    if row < 0 or row >= len(df):
        raise ValueError("消息不存在，可能已被删除")
    if signature and signature != _signature(df.iloc[row]):
        raise ValueError("消息已发生变化，请刷新后重试")
    return path, df


def update_message(name, row, signature, data):
    path, df = _locate(name, row, signature)
    for col in COLUMNS:
        if col in data:
            df.at[row, col] = data[col]
    _write_path(path, df)


def delete_message(name, row, signature):
    path, df = _locate(name, row, signature)
    df = df.drop(index=row).reset_index(drop=True)
    _write_path(path, df)


# -------------------------------------------------------------- 值规范化

def normalize_row(form):
    """把表单数据规范化为一行标准消息。表单缺省字段按空处理。"""
    return {col: normalize_value(col, form.get(col, "")) for col in COLUMNS}


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
