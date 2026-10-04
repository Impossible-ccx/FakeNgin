"""离线把旧风险 SQLite 文件迁移为 JSON；必须先停止网站，旧文件不删除。

先检查：python scripts/migrate_risk_storage.py --dry-run
再迁移：python scripts/migrate_risk_storage.py
指定目录：python scripts/migrate_risk_storage.py --database-dir /path/to/database
可重复执行；已存在的 JSON 一律保留，不覆盖后续检测结果或任务状态。
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import file_store  # noqa: E402; does not create or initialize an app


def _legacy_rows(path, table):
    if not path.exists():
        return
    # mode=ro 禁止创建新数据库或更改现有文件。
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
        ).fetchone()
        if exists:
            for row in connection.execute("SELECT * FROM " + table):
                yield dict(row)


def _report(row):
    return {
        "id": row["id"], "created_at": row["created_at"], "message": row["message"],
        "model_ids": json.loads(row["model_ids_json"]), "result": json.loads(row["result_json"]),
        "prompt_version": row["prompt_version"],
    }


def _job(row):
    job = json.loads(row["payload_json"])
    job.update(id=row["id"], created_at=row["created_at"], status=row["status"],
               updated_at=row["updated_at"], cancel_requested=bool(row["cancel_requested"]))
    if job["status"] in ("queued", "running", "cancelling"):
        # 迁移无法判断最后一次模型调用是否完成，明确中断并保留已完成结果。
        job.update(status="interrupted", current_index=None,
                   updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   error="存储已迁移，未完成任务已中断；已保存的报告仍可查看。")
        for item in job.get("items", []):
            if item["status"] in ("pending", "running"):
                item["status"] = "interrupted"
                for member in item.get("members", []):
                    if member.get("status") == "running":
                        member["status"] = "interrupted"
    return job


def _reference(row):
    return {
        "file": row["file"], "row": row["row_number"], "signature": row["signature"],
        "id": row["report_id"], "history_url": "/history/" + row["report_id"],
        "label": row["label"], "level": row["level"], "created_at": row["created_at"],
    }


def migrate_storage(directory, *, dry_run=False):
    """返回分类计数；不调用模型、不加载密钥、不改 CSV 和旧 SQLite。"""
    directory = Path(directory).resolve()
    result = {name: {"found": 0, "copied": 0, "skipped": 0} for name in ("reports", "batches", "references")}
    sources = (
        ("reports", "risk_reports.sqlite3", "risk_reports", "risk_reports", _report),
        ("batches", "risk_batches.sqlite3", "risk_batches", "risk_batches", _job),
        ("references", "risk_batches.sqlite3", "dataset_reports", "dataset_reports", _reference),
    )
    for category, source_name, table, target_name, decode in sources:
        for row in _legacy_rows(directory / source_name, table):
            record = decode(row)
            target = directory / target_name
            if category == "references":
                # 校验报告 ID，导出的链接不能包含任意路径。
                file_store.uuid_path(directory / "risk_reports", record["id"])
                path = file_store.key_path(target, [record["file"], record["row"], record["signature"]])
            else:
                path = file_store.uuid_path(target, record["id"])
            counts = result[category]
            counts["found"] += 1
            copied = not path.exists() if dry_run else file_store.write_json(path, record, overwrite=False)
            counts["copied" if copied else "skipped"] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description="离线迁移风险报告和批量任务，保留旧 SQLite 备份")
    parser.add_argument("--database-dir", type=Path, default=PROJECT_ROOT / "database")
    parser.add_argument("--dry-run", action="store_true", help="只报告将迁移/保留的记录数量，不写 JSON")
    options = parser.parse_args()
    result = migrate_storage(options.database_dir, dry_run=options.dry_run)
    print(json.dumps({"dry_run": options.dry_run, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
