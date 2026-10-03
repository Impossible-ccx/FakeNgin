"""本地持久化批任务：逐条、逐模型执行，重启后不自动重复调用。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from checkmodel.ensemble import get_risk_models, iter_risk_check, validate_risk_request

from . import dataset, db, reports

ACTIVE_STATUSES = ("queued", "running", "cancelling")
_lock = threading.RLock()
_initialized_paths = set()
_workers = {}
_job_cloud_models = {}


def _release_cloud_model(job_id):
    with _lock:
        _job_cloud_models.pop(job_id, None)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _directory():
    return str(Path(db.DATABASE_DIR).resolve())


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def _connection(directory=None):
    directory = Path(directory or _directory())
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "risk_batches.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS risk_batches (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dataset_reports (
                    file TEXT NOT NULL, row_number INTEGER NOT NULL,
                    signature TEXT NOT NULL, report_id TEXT NOT NULL,
                    label TEXT NOT NULL, level TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (file, row_number, signature)
                )
            """)
            yield connection
    finally:
        connection.close()


def _decode(row):
    if row is None:
        return None
    job = json.loads(row["payload_json"])
    job.update(status=row["status"], updated_at=row["updated_at"], cancel_requested=bool(row["cancel_requested"]))
    return job


def get_batch(job_id, directory=None):
    with _connection(directory) as connection:
        return _decode(connection.execute("SELECT * FROM risk_batches WHERE id = ?", (job_id,)).fetchone())


def list_batches(limit=5):
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM risk_batches ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(20, int(limit))),),
        ).fetchall()
    return [_decode(row) for row in rows]


def _counts(job):
    complete = [item for item in job["items"] if item["status"] in ("completed", "failed")]
    job["completed_count"] = len(complete)
    job["saved_count"] = sum(bool(item["record_id"]) for item in complete)
    job["failed_count"] = sum(item["status"] == "failed" or not (item["result"] or {}).get("success_count") for item in complete)
    job["save_failed_count"] = sum(bool(item["history_error"]) for item in complete)


def _save_job(job, directory=None):
    _counts(job)
    job["updated_at"] = _now()
    # Cancellation has its own column so a worker's older snapshot cannot erase it.
    with _connection(directory) as connection:
        connection.execute(
            "UPDATE risk_batches SET status = CASE "
            "WHEN cancel_requested = 1 AND ? IN ('queued', 'running') THEN 'cancelling' ELSE ? END, "
            "updated_at = ?, payload_json = ? WHERE id = ?",
            (job["status"], job["status"], job["updated_at"], _json(job), job["id"]),
        )


def initialize():
    """每个进程、每个数据库路径初始化一次；旧活跃任务标中断，绝不重跑。"""
    directory = _directory()
    with _lock:
        if directory in _initialized_paths:
            return
        worker = _workers.get(directory)
        if worker is not None and worker.is_alive():
            _initialized_paths.add(directory)
            return
        with _connection(directory) as connection:
            rows = connection.execute(
                "SELECT * FROM risk_batches WHERE status IN ('queued', 'running', 'cancelling')",
            ).fetchall()
            for row in rows:
                job = _decode(row)
                job["status"] = "interrupted"
                job["error"] = "服务已重启，任务已中断；已保存的报告仍可查看。"
                job["current_index"] = None
                for item in job["items"]:
                    if item["status"] in ("pending", "running"):
                        item["status"] = "interrupted"
                        for member in item["members"]:
                            if member.get("status") == "running":
                                member["status"] = "interrupted"
                now = _now()
                connection.execute(
                    "UPDATE risk_batches SET status = 'interrupted', updated_at = ?, payload_json = ? WHERE id = ?",
                    (now, _json(job), job["id"]),
                )
                _job_cloud_models.pop(job["id"], None)
        _initialized_paths.add(directory)


def create_batch(rows, model_ids, mode="vote", deepseek_source=None, cloud_model=None):
    """所有输入通过后才落任务；队列由一个后台线程顺序执行。"""
    _, model_ids = validate_risk_request("验证批量检测配置", model_ids, mode, deepseek_source)
    resolved = dataset.resolve_rows(rows)
    models = get_risk_models(deepseek_source, cloud_model=cloud_model)
    available_ids = {model["id"] for model in models if model["available"]}
    if not any(model_id in available_ids for model_id in model_ids):
        raise ValueError("所选模型当前均不可用，请连接相应来源后再开始批量检测")
    initialize()
    uses_deepseek = "deepseek_r1" in model_ids
    if uses_deepseek and deepseek_source is None:
        deepseek_source = next(model["source"] for model in models if model["id"] == "deepseek_r1")
    uses_cloud = uses_deepseek and deepseek_source == "cloud"
    captured_cloud = cloud_model is not None and uses_cloud
    credential_mode = getattr(cloud_model, "credential_mode", "missing") if captured_cloud else (
        "default" if uses_cloud else "local"
    )
    if credential_mode not in ("personal", "expired", "missing", "default", "local"):
        credential_mode = "missing"
    now = _now()
    job = {
        "id": str(uuid.uuid4()), "status": "queued", "created_at": now, "updated_at": now,
        "model_ids": model_ids, "mode": mode, "deepseek_source": deepseek_source,
        "credential_mode": credential_mode,
        "total": len(resolved), "completed_count": 0, "saved_count": 0, "failed_count": 0,
        "save_failed_count": 0, "current_index": None, "cancel_requested": False,
        "error": None, "items": [],
    }
    for index, row in enumerate(resolved):
        job["items"].append({
            **row, "index": index, "status": "pending", "members": [], "result": None,
            "record_id": None, "history_url": None, "history_error": None, "error": None,
        })
    with _lock:
        with _connection() as connection:
            connection.execute(
                "INSERT INTO risk_batches (id, status, created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (job["id"], job["status"], now, now, _json(job)),
            )
        if captured_cloud:
            _job_cloud_models[job["id"]] = cloud_model
    try:
        _start_worker()
    except Exception:
        _release_cloud_model(job["id"])
        job.update(status="interrupted", error="后台任务未能启动，请稍后重新提交。")
        _save_job(job)
        raise RuntimeError("Batch worker could not start") from None
    return job


def _cancel_requested(job_id, directory):
    with _connection(directory) as connection:
        row = connection.execute("SELECT cancel_requested FROM risk_batches WHERE id = ?", (job_id,)).fetchone()
    return row is not None and bool(row[0])


def cancel_batch(job_id):
    cancelled = False
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM risk_batches WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        if row["status"] in ACTIVE_STATUSES:
            job = _decode(row)
            status = "cancelling"
            if row["status"] == "queued":
                status = "cancelled"
                for item in job["items"]:
                    item["status"] = "cancelled"
            connection.execute(
                "UPDATE risk_batches SET cancel_requested = 1, status = ?, updated_at = ?, payload_json = ? WHERE id = ?",
                (status, _now(), _json(job), job_id),
            )
            cancelled = status == "cancelled"
    if cancelled:
        _release_cloud_model(job_id)
    return get_batch(job_id)


def _finish_cancelled(job, directory):
    job.update(status="cancelled", cancel_requested=True, current_index=None)
    for item in job["items"]:
        if item["status"] in ("pending", "running"):
            item["status"] = "cancelled"
            for member in item["members"]:
                if member.get("status") == "running":
                    member["status"] = "cancelled"
    _save_job(job, directory)


def _link_report(item, record, directory):
    with _connection(directory) as connection:
        connection.execute(
            "INSERT INTO dataset_reports (file, row_number, signature, report_id, label, level, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file, row_number, signature) DO UPDATE SET report_id=excluded.report_id, "
            "label=excluded.label, level=excluded.level, created_at=excluded.created_at",
            (item["file"], item["row"], item["signature"], record["id"],
             record["result"]["label"], record["result"]["level"], record["created_at"]),
        )


def latest_reports(references):
    """按文件、行号、内容指纹关联最新已保存报告，旧内容不冒充当前结果。"""
    found = {}
    with _connection() as connection:
        for reference in references:
            key = (reference["file"], reference["row"], reference["signature"])
            row = connection.execute(
                "SELECT * FROM dataset_reports WHERE file = ? AND row_number = ? AND signature = ?", key,
            ).fetchone()
            if row is not None:
                found[key] = {"id": row["report_id"], "history_url": "/history/" + row["report_id"],
                              "label": row["label"], "level": row["level"], "created_at": row["created_at"]}
    return found


def run_batch(job_id, directory=None):
    """任务结束或异常退出时释放个人模型快照，不在持久化数据中留下密钥。"""
    directory = directory or _directory()
    with _lock:
        with _connection(directory) as connection:
            claimed = connection.execute(
                "UPDATE risk_batches SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (_now(), job_id),
            ).rowcount
        if not claimed:
            return
        cloud_model = _job_cloud_models.get(job_id)
    try:
        return _run_batch(job_id, directory, cloud_model)
    finally:
        _release_cloud_model(job_id)


def _run_batch(job_id, directory, cloud_model):
    """执行一个已入队任务；公开给后台 worker 与无网络的同步测试。"""
    job = get_batch(job_id, directory)
    if job.get("credential_mode") in ("personal", "expired", "missing") and cloud_model is None:
        # Every web cloud job captures an override, including unavailable sentinels.
        # A missing in-memory snapshot can never authorize use of a server-owned key.
        from .api_credentials import ExpiredCloudModel, MissingCloudModel
        cloud_model = MissingCloudModel() if job["credential_mode"] == "missing" else ExpiredCloudModel()
    for item in job["items"]:
        if _cancel_requested(job_id, directory):
            _finish_cancelled(job, directory)
            return
        item["status"] = "running"
        job["current_index"] = item["index"]
        _save_job(job, directory)
        iterator = None
        finished_members = 0
        try:
            iterator = iter_risk_check(item["message"], job["model_ids"], job["mode"], job["deepseek_source"],
                                       cloud_model=cloud_model)
            while True:
                # A full row can still produce its final aggregation without another model call.
                if _cancel_requested(job_id, directory) and finished_members < len(job["model_ids"]):
                    _finish_cancelled(job, directory)
                    return
                try:
                    event = next(iterator)
                except StopIteration:
                    if item["result"] is None:
                        raise RuntimeError("missing completed result")
                    break
                if event["type"] == "member_start":
                    item["members"].append(event["member"])
                elif event["type"] == "member_complete":
                    item["members"][-1] = event["member"]
                    finished_members += 1
                elif event["type"] == "complete":
                    result = event["result"]
                    result.update(batch_id=job_id, batch_index=item["index"], batch_total=job["total"],
                                  dataset_ref={key: item[key] for key in ("file", "row", "signature")})
                    item.update(status="completed", result=result, members=result["members"])
                    try:
                        record = reports.save_report(item["message"], job["model_ids"], result)
                    except Exception:
                        item["history_error"] = "本条结果已完成，但历史记录保存失败；可在本任务查看结果。"
                    else:
                        item.update(record_id=record["id"], history_url="/history/" + record["id"])
                        _link_report(item, record, directory)
                _save_job(job, directory)
        except Exception:
            if item["result"] is None:
                item.update(status="failed", error="本条检测发生错误，已继续处理后续消息。")
            else:
                item["error"] = "本条结果已完成，但数据关联更新失败。"
            _save_job(job, directory)
        finally:
            if iterator is not None:
                iterator.close()
    if _cancel_requested(job_id, directory):
        _finish_cancelled(job, directory)
    else:
        job.update(status="completed", current_index=None)
        _save_job(job, directory)


def _start_worker():
    directory = _directory()
    with _lock:
        current = _workers.get(directory)
        if current is not None and current.is_alive():
            return
        worker = threading.Thread(target=_worker_loop, args=(directory,), daemon=True, name="fakengin-batch")
        _workers[directory] = worker
        worker.start()


def _worker_loop(directory):
    while True:
        with _lock:
            with _connection(directory) as connection:
                row = connection.execute(
                    "SELECT id FROM risk_batches WHERE status = 'queued' "
                    "ORDER BY created_at, id LIMIT 1",
                ).fetchone()
            if row is None:
                _workers.pop(directory, None)
                return
        try:
            run_batch(row["id"], directory)
        except Exception:
            # Do not retry a task whose last model request may already have been billed.
            try:
                job = get_batch(row["id"], directory)
                job.update(status="interrupted", current_index=None,
                           error="任务执行中断；已保存的报告仍可查看，请检查本地存储后再操作。")
                _save_job(job, directory)
            except Exception:
                with _lock:
                    _workers.pop(directory, None)
                return
