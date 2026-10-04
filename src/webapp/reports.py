"""独立 CSV 文件保存完整风险报告，不修改原有 CSV 数据和真假标签。"""

from datetime import datetime, timezone
from pathlib import Path
import uuid

from checkmodel.ollama_base import PROMPT_VERSION

from . import db, file_store


def _directory():
    return Path(db.DATABASE_DIR) / "risk_reports"


def save_report(message, model_ids, result, prompt_version=PROMPT_VERSION):
    """只保存已完成的一次检测；全部成员失败的完整报告同样保留。"""
    record = {
        "id": str(uuid.uuid4()),
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "model_ids": list(model_ids),
        "result": result,
        "prompt_version": prompt_version,
    }
    path = file_store.uuid_path(_directory(), record["id"])
    if not file_store.write_csv(path, record, overwrite=False):
        raise RuntimeError("Report identifier already exists")
    return file_store.read_csv(path)


def get_report(report_id):
    """按报告 UUID 读取完整记录，不存在或无效 ID 返回 None。"""
    try:
        path = file_store.uuid_path(_directory(), report_id)
    except ValueError:
        return None
    return file_store.read_csv(path)


def list_reports(page=1, page_size=20, query=""):
    """按完成时间倒序分页，可按消息原文搜索；缓存随文件变化失效。"""
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    query = (query or "").strip()
    needle = query.casefold()
    records = [
        record for record in file_store.iter_records(_directory())
        if not needle or needle in record["message"].casefold()
    ]
    records.sort(key=lambda record: (record["created_at"], record["id"]), reverse=True)
    total = len(records)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    return {
        "records": records[offset:offset + page_size],
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "query": query,
    }
