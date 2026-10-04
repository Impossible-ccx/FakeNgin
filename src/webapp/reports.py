"""独立保存完整风险检测报告，不修改原有 CSV 数据和真假标签。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from checkmodel.ollama_base import PROMPT_VERSION

from . import db


@contextmanager
def _connection():
    # 每次读取当前数据库目录，兼容运行配置和测试中的临时目录。
    database_directory = Path(db.DATABASE_DIR)
    database_directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_directory / "risk_reports.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS risk_reports (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    model_ids_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    prompt_version TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS risk_reports_created_at
                ON risk_reports (created_at DESC, id DESC)
            """)
            yield connection
    finally:
        connection.close()


def _decode_record(row):
    return {
        "id": row["id"],
        "message": row["message"],
        "created_at": row["created_at"],
        "model_ids": json.loads(row["model_ids_json"]),
        "result": json.loads(row["result_json"]),
        "prompt_version": row["prompt_version"],
    }


def save_report(message, model_ids, result, prompt_version=PROMPT_VERSION):
    """只保存已完成的一次检测；全部成员失败的完整报告同样保留。"""
    model_ids_json = json.dumps(list(model_ids), ensure_ascii=False, allow_nan=False)
    result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
    record_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with _connection() as connection:
        connection.execute(
            "INSERT INTO risk_reports "
            "(id, created_at, message, model_ids_json, result_json, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, created_at, message, model_ids_json, result_json, prompt_version),
        )
    return {
        "id": record_id,
        "message": message,
        "created_at": created_at,
        "model_ids": json.loads(model_ids_json),
        "result": json.loads(result_json),
        "prompt_version": prompt_version,
    }


def get_report(report_id):
    """按报告 ID 读取一条完整记录，不存在返回 None。"""
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM risk_reports WHERE id = ?", (report_id,),
        ).fetchone()
    return _decode_record(row) if row is not None else None


def list_reports(page=1, page_size=20, query=""):
    """按完成时间倒序分页，可按消息原文搜索。"""
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    query = (query or "").strip()
    where = ""
    parameters = []
    if query:
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = " WHERE message LIKE ? ESCAPE '\\'"
        parameters.append("%" + escaped_query + "%")

    with _connection() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM risk_reports" + where, parameters,
        ).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = connection.execute(
            "SELECT * FROM risk_reports" + where +
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            parameters + [page_size, (page - 1) * page_size],
        ).fetchall()

    return {
        "records": [_decode_record(row) for row in rows],
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "query": query,
    }
