"""消息数据访问层。

消息存放在 SQLite messages 表（稳定自增 ID），comments 表用于评论/回复树数据。
CSV 仅为导入、导出格式：列 content, nature, fake_probability, source,
publish_time, process_time（image_ref 可选）。

行字段：
    id, content, nature, fake_probability(可空), source, publish_time,
    process_time, image_ref, legacy_probability(1=历史导入、来源未知),
    created_at, updated_at, version(乐观并发版本号)
"""

import csv
import math
from datetime import datetime

from . import db

NATURES = ["虚假", "真实", "中立", "证据不足", "未校验"]
# 旧字段：人工校验选项已迁移至 reviews.REVIEW_OPTIONS
VERIFY_NATURES = ["虚假", "真实", "证据不足"]
DEFAULT_NATURE = "未校验"

COLUMNS = [
    "content",
    "nature",
    "fake_probability",
    "source",
    "publish_time",
    "process_time",
]

TIME_FORMAT = db.TIME_FORMAT
TIME_INPUT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
]

MESSAGE_FIELDS = [
    "id", "content", "nature", "fake_probability", "source",
    "publish_time", "process_time", "image_ref",
    "legacy_probability", "created_at", "updated_at", "version",
]

DATA_VERSION_KEY = "messages_data_version"

MAX_CONTENT_LENGTH = 5000
MAX_SOURCE_LENGTH = 200


def ensure_newsdata():
    """建库、执行迁移（幂等）。"""
    db.init_db()


def search_messages(query, limit=3):
    """按查询返回高相关消息（转发到 bm25 模块）。"""
    from . import bm25

    return bm25.search(query, limit)


# ------------------------------------------------------------- 基础读取

def _row_to_dict(row):
    record = dict(row)
    return {key: record[key] for key in MESSAGE_FIELDS}


def get_message(message_id):
    """按稳定 ID 查消息，返回 dict 或 None。"""
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_messages_by_ids(message_ids):
    """按给定 ID 顺序返回消息（缺失 ID 跳过）。"""
    ids = list(message_ids)
    if not ids:
        return []
    with db.db_conn() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT * FROM messages WHERE id IN ({})".format(placeholders), ids
        ).fetchall()
    by_id = {row["id"]: _row_to_dict(row) for row in rows}
    return [by_id[mid] for mid in ids if mid in by_id]


def list_sources():
    """去重后的来源列表（供筛选下拉）。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source FROM messages WHERE source != '' ORDER BY source"
        ).fetchall()
    return [row["source"] for row in rows]


def load_all():
    """全部消息（dict 列表），发布时间倒序；缺失时间排最后，同时间按 id 倒序。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY "
            "(publish_time = '') ASC, publish_time DESC, id DESC"
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_messages(limit, offset=0):
    """分页读取消息，排序规则同 load_all。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY "
            "(publish_time = '') ASC, publish_time DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def count_messages(nature=None, nature_not=None):
    """统计消息数；nature 指定时只统计该性质，nature_not 指定时排除该性质。"""
    with db.db_conn() as conn:
        if nature is None and nature_not is None:
            return conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        clauses, params = [], []
        if nature is not None:
            clauses.append("nature = ?")
            params.append(nature)
        if nature_not is not None:
            clauses.append("nature != ?")
            params.append(nature_not)
        return conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE {}".format(
                " AND ".join(clauses)),
            params,
        ).fetchone()["n"]


def list_messages_filtered(nature=None, limit=20, offset=0, nature_not=None):
    """分页读取消息；nature=None 为全部，指定时只返回该性质（如待复核队列）。

    nature_not 指定时排除该性质（如“已处理”队列 = 全部 - 未校验），
    与 count_messages 的口径保持一致。
    """
    clauses, params = [], []
    if nature is not None:
        clauses.append("nature = ?")
        params.append(nature)
    if nature_not is not None:
        clauses.append("nature != ?")
        params.append(nature_not)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages {} "
            "ORDER BY (publish_time = '') ASC, publish_time DESC, id DESC "
            "LIMIT ? OFFSET ?".format(where),
            params + [limit, offset],
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _bump_data_version(conn):
    version = int(db.get_meta(conn, DATA_VERSION_KEY, "0")) + 1
    db.set_meta(conn, DATA_VERSION_KEY, version)


def data_version():
    """消息数据版本号；搜索索引用它判断是否需要重建。"""
    with db.db_conn() as conn:
        return int(db.get_meta(conn, DATA_VERSION_KEY, "0"))


# ------------------------------------------------------------------ 写入

def append_message(data):
    """新增消息，返回稳定 ID。"""
    row = normalize_row(data)
    if not row["content"]:
        raise ValueError("消息内容不能为空")
    with db.db_conn() as conn:
        message_id = insert_message_in_tx(conn, row, data)
        _bump_data_version(conn)
        return message_id


def insert_message_in_tx(conn, row, data):
    """在已开启的事务连接内插入一条消息，返回稳定 ID。

    供采集导入等批量场景复用（整批一个事务）；row 为 normalize_row
    的结果，data 用于取 image_ref 等附加字段。不递增数据版本，
    由调用方在批次结束时统一执行。
    """
    if not row["content"]:
        raise ValueError("消息内容不能为空")
    now = db.now_string()
    cursor = conn.execute(
        "INSERT INTO messages (content, nature, fake_probability, source, "
        "publish_time, process_time, image_ref, legacy_probability, "
        "created_at, updated_at, version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1)",
        (
            row["content"], row["nature"], row["fake_probability"],
            row["source"], row["publish_time"], row["process_time"],
            str(data.get("image_ref", "") or ""), now, now,
        ),
    )
    return cursor.lastrowid


def update_message(message_id, expected_version, data):
    """按稳定 ID 更新；版本不一致或不存在时抛 ValueError，不静默覆盖。"""
    normalized = normalize_row(data)
    allowed = {key: normalized[key] for key in COLUMNS if key in data and normalized[key] is not None}
    if not allowed:
        raise ValueError("没有需要更新的字段")
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT version FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise ValueError("消息不存在，可能已被删除")
        if row["version"] != expected_version:
            raise ValueError("消息已被其他操作修改，请刷新后重试")
        sets = ", ".join("{} = ?".format(key) for key in allowed)
        params = list(allowed.values()) + [db.now_string(), expected_version + 1, message_id]
        cursor = conn.execute(
            "UPDATE messages SET {}, updated_at = ?, version = ? WHERE id = ?".format(sets),
            params,
        )
        if cursor.rowcount != 1:
            raise ValueError("消息更新失败，请刷新后重试")
        _bump_data_version(conn)


def delete_message(message_id, expected_version):
    """按稳定 ID 删除；版本不一致或不存在时抛 ValueError。"""
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT version FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise ValueError("消息不存在，可能已被删除")
        if row["version"] != expected_version:
            raise ValueError("消息已被其他操作修改，请刷新后重试")
        cursor = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        if cursor.rowcount != 1:
            raise ValueError("消息删除失败，请刷新后重试")
        _bump_data_version(conn)


# -------------------------------------------------------------- 值规范化

def normalize_row(form):
    """把表单/CSV 行数据规范化为标准字段。表单缺省字段按空处理。"""
    return {col: normalize_value(col, form.get(col, "")) for col in COLUMNS}


def normalize_value(field, value):
    value = "" if value is None else str(value).strip()
    if field == "content" and len(value) > MAX_CONTENT_LENGTH:
        raise ValueError("消息内容过长（上限 {} 字）".format(MAX_CONTENT_LENGTH))
    if field == "source" and len(value) > MAX_SOURCE_LENGTH:
        raise ValueError("来源过长（上限 {} 字）".format(MAX_SOURCE_LENGTH))
    if field == "nature":
        return value if value in NATURES else DEFAULT_NATURE
    if field == "fake_probability":
        if value == "":
            return None
        try:
            num = float(value)
        except ValueError:
            raise ValueError("虚假概率必须是数字")
        if not math.isfinite(num):
            raise ValueError("虚假概率必须是有限数值")
        num = max(0.0, min(100.0, num))
        return round(num, 2)
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


# ------------------------------------------------------------ CSV 导入导出

def import_csv(path):
    """把 CSV 导入 messages 表。

    校验：空正文、时间格式、概率数值；文件内与库内按正文去重（跳过并计数）。
    任一行有错则整个文件不导入（事务原子性），错误清单随统计返回。
    返回 {"imported", "duplicates", "errors": [{"row", "error"}]}。
    """
    records = []
    errors = []
    seen_contents = set()

    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [name.strip() for name in (reader.fieldnames or [])]
        missing = [col for col in COLUMNS if col not in header]
        if missing:
            raise ValueError("CSV 缺少必需列：{}".format("、".join(missing)))
        for line_no, raw in enumerate(reader, start=2):
            try:
                row = normalize_row(raw)
                if not row["content"]:
                    raise ValueError("消息内容不能为空")
                image_ref = (raw.get("image_ref") or "").strip()
                if row["content"] in seen_contents:
                    records.append(("dup", None, ""))
                    continue
                seen_contents.add(row["content"])
                records.append(("ok", row, image_ref))
            except ValueError as exc:
                errors.append({"row": line_no, "error": str(exc)})

    if errors:
        return {"imported": 0, "duplicates": 0, "errors": errors}

    imported = 0
    duplicates = 0
    now = db.now_string()
    with db.db_conn() as conn:
        existing = {row[0] for row in conn.execute("SELECT content FROM messages")}
        for status, row, image_ref in records:
            if status == "dup" or row["content"] in existing:
                duplicates += 1
                continue
            conn.execute(
                "INSERT INTO messages (content, nature, fake_probability, source, "
                "publish_time, process_time, image_ref, legacy_probability, "
                "created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1)",
                (
                    row["content"], row["nature"], row["fake_probability"],
                    row["source"], row["publish_time"], row["process_time"],
                    image_ref, now, now,
                ),
            )
            existing.add(row["content"])
            imported += 1
        _bump_data_version(conn)

    return {"imported": imported, "duplicates": duplicates, "errors": errors}


def export_csv(path):
    """导出全部消息为 CSV，返回导出条数。"""
    rows = load_all()
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([
                row["content"], row["nature"],
                "" if row["fake_probability"] is None else "{:.2f}".format(row["fake_probability"]),
                row["source"], row["publish_time"], row["process_time"],
            ])
    return len(rows)


# -------------------------------------------------------------- 评论数据

def add_comment(message_id, content, parent_id=None, publish_time=""):
    """写入一条评论（回复树节点）；消息必须存在，父评论须同属该消息。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("评论内容不能为空")
    now = db.now_string()
    with db.db_conn() as conn:
        if conn.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone() is None:
            raise ValueError("所属消息不存在")
        if parent_id is not None:
            parent = conn.execute(
                "SELECT id, message_id FROM comments WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None or parent["message_id"] != message_id:
                raise ValueError("父评论不存在或不属于该消息")
        cursor = conn.execute(
            "INSERT INTO comments (message_id, parent_id, content, publish_time, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, parent_id, content, _normalize_time(publish_time), now),
        )
        return cursor.lastrowid


def list_comments(message_id):
    """按发布时间返回某消息的评论（用于回复树/时间序列路线）。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT id, message_id, parent_id, content, publish_time, created_at "
            "FROM comments WHERE message_id = ? ORDER BY publish_time, id",
            (message_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------------ 评论 CSV 导入

COMMENT_COLUMNS = ["ref", "message_content", "parent_ref", "content", "publish_time"]


def import_comments_csv(path):
    """把评论 CSV 导入 comments 表（回复树 + 检测序列数据）。

    列：ref（本文件内唯一行标识）、message_content（所属消息正文，
    须与库内正文完全一致且唯一）、parent_ref（父评论 ref，空为顶层，
    须引用文件中更早的行且属同一消息）、content、publish_time。

    两阶段：先完成全部校验（解析、消息定位、父引用、重复判定），
    任一行有错则整文件不导入；全部通过后在同一事务内按文件顺序插入。
    库内已存在的 (所属消息, 正文, 时间) 视为重复跳过，重复行的子评论
    关联到库内已有父评论，重复导入不产生新记录。
    返回 {"imported", "duplicates", "errors": [{"row", "error"}]}。
    """
    rows = []
    errors = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [name.strip() for name in (reader.fieldnames or [])]
        missing = [col for col in COMMENT_COLUMNS if col not in header]
        if missing:
            raise ValueError("CSV 缺少必需列：{}".format("、".join(missing)))
        for line_no, raw in enumerate(reader, start=2):
            try:
                ref = (raw.get("ref") or "").strip()
                if not ref:
                    raise ValueError("ref 不能为空")
                content = (raw.get("content") or "").strip()
                if not content:
                    raise ValueError("评论内容不能为空")
                rows.append({
                    "row": line_no,
                    "ref": ref,
                    "message_content": (raw.get("message_content") or "").strip(),
                    "parent_ref": (raw.get("parent_ref") or "").strip(),
                    "content": content,
                    "publish_time": _normalize_time((raw.get("publish_time") or "").strip()),
                })
            except ValueError as exc:
                errors.append({"row": line_no, "error": str(exc)})

    seen_refs = set()
    for row in rows:
        if row["ref"] in seen_refs:
            errors.append({"row": row["row"], "error": "ref 重复：{}".format(row["ref"])})
        seen_refs.add(row["ref"])

    with db.db_conn() as conn:
        # 校验阶段：解析所属消息与父引用、判定重复，不写库
        ref_info = {}  # ref -> (message_id, 库内已有评论 ID 或 None)
        plan = []      # (row, message_id, parent_ref, 是否重复)
        for row in rows:
            found = conn.execute(
                "SELECT id FROM messages WHERE content = ?", (row["message_content"],)
            ).fetchall()
            if len(found) != 1:
                errors.append({"row": row["row"], "error": "所属消息不存在或不唯一"})
                continue
            message_id = found[0]["id"]
            if row["parent_ref"]:
                parent = ref_info.get(row["parent_ref"])
                if parent is None or parent[0] != message_id:
                    errors.append({
                        "row": row["row"],
                        "error": "父评论 ref 不存在、未在其之前定义或不属于同一消息",
                    })
                    continue
            existing = conn.execute(
                "SELECT id FROM comments WHERE message_id = ? AND content = ? "
                "AND publish_time = ?",
                (message_id, row["content"], row["publish_time"]),
            ).fetchone()
            ref_info[row["ref"]] = (message_id, existing["id"] if existing else None)
            plan.append((row, message_id, row["parent_ref"], existing is not None))

        if errors:
            return {"imported": 0, "duplicates": 0, "errors": errors}

        imported = 0
        duplicates = 0
        ref_to_dbid = {}
        now = db.now_string()
        for row, message_id, parent_ref, is_duplicate in plan:
            if is_duplicate:
                ref_to_dbid[row["ref"]] = ref_info[row["ref"]][1]
                duplicates += 1
                continue
            parent_id = ref_to_dbid.get(parent_ref) if parent_ref else None
            cursor = conn.execute(
                "INSERT INTO comments (message_id, parent_id, content, publish_time, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, parent_id, row["content"], row["publish_time"], now),
            )
            ref_to_dbid[row["ref"]] = cursor.lastrowid
            imported += 1

    return {"imported": imported, "duplicates": duplicates, "errors": errors}
