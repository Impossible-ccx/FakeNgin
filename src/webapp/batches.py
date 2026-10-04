"""CSV 文件持久化批任务：逐条、逐模型执行，重启不重复调用。"""

from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid

from checkmodel.ensemble import iter_risk_check, validate_risk_request

from . import dataset, db, file_store, models as web_models, reports

ACTIVE_STATUSES = ("queued", "running", "cancelling")
_lock = threading.RLock()
_initialized_paths = set()
_workers = {}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _directory():
    return str(Path(db.DATABASE_DIR).resolve())


def _job_directory(directory=None):
    return Path(directory or _directory()) / "risk_batches"


def _reference_path(reference, directory=None):
    return file_store.key_path(Path(directory or _directory()) / "dataset_reports",
                               [reference["file"], reference["row"], reference["signature"]])


def get_batch(job_id, directory=None):
    try:
        path = file_store.uuid_path(_job_directory(directory), job_id)
    except ValueError:
        return None
    return file_store.read_csv(path)


def list_batches(limit=5):
    jobs = list(file_store.iter_records(_job_directory()))
    jobs.sort(key=lambda job: (job["created_at"], job["id"]), reverse=True)
    return jobs[:max(1, min(20, int(limit)))]


def _counts(job):
    complete = [item for item in job["items"] if item["status"] in ("completed", "failed")]
    job["completed_count"] = len(complete)
    job["saved_count"] = sum(bool(item["record_id"]) for item in complete)
    job["failed_count"] = sum(item["status"] == "failed" or not (item["result"] or {}).get("success_count") for item in complete)
    job["save_failed_count"] = sum(bool(item["history_error"]) for item in complete)


def _save_job(job, directory=None):
    _counts(job)
    path = file_store.uuid_path(_job_directory(directory), job["id"])
    with file_store.locked(path.parent):
        current = file_store.read_csv(path)
        if current is None:
            raise ValueError("Unknown batch job")
        # 取消标记只能从 False 变为 True；旧 worker 快照不能覆盖它。
        job["cancel_requested"] = bool(current.get("cancel_requested") or job.get("cancel_requested"))
        if current["status"] in ("cancelled", "interrupted", "completed") and job["status"] in ACTIVE_STATUSES:
            return
        if current["status"] == "interrupted":
            return
        if job["cancel_requested"] and job["status"] in ("queued", "running"):
            job["status"] = "cancelling"
        job["updated_at"] = _now()
        file_store.write_csv(path, job)


def initialize():
    """每个进程、每个目录初始化一次；旧活跃任务标中断，绝不重跑。"""
    directory = _directory()
    with _lock:
        if directory in _initialized_paths:
            return
        worker = _workers.get(directory)
        if worker is not None and worker.is_alive():
            _initialized_paths.add(directory)
            return
        with file_store.locked(_job_directory(directory)):
            for job in file_store.iter_records(_job_directory(directory)):
                if job["status"] not in ACTIVE_STATUSES:
                    continue
                job["status"] = "interrupted"
                job["error"] = "服务已重启，任务已中断；已保存的报告仍可查看。"
                job["current_index"] = None
                for item in job["items"]:
                    if item["status"] in ("pending", "running"):
                        item["status"] = "interrupted"
                        for member in item["members"]:
                            if member.get("status") == "running":
                                member["status"] = "interrupted"
                job["updated_at"] = _now()
                file_store.write_csv(file_store.uuid_path(_job_directory(directory), job["id"]), job)
        _initialized_paths.add(directory)


def create_batch(rows, model_ids, mode="vote", deepseek_source="local"):
    """所有输入通过后才落任务；队列由一个后台线程顺序执行。"""
    deepseek_source = web_models.validate_web_source(deepseek_source)
    _, model_ids = validate_risk_request("验证批量检测配置", model_ids, mode)
    resolved = dataset.resolve_rows(rows)
    models = web_models.list_web_models()
    available_ids = {model["id"] for model in models if model["available"]}
    if not any(model_id in available_ids for model_id in model_ids):
        raise ValueError("所选模型当前均不可用，请连接相应来源后再开始批量检测")
    initialize()
    now = _now()
    job = {
        "id": str(uuid.uuid4()), "status": "queued", "created_at": now, "updated_at": now,
        "model_ids": model_ids, "mode": mode, "deepseek_source": deepseek_source,
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
        path = file_store.uuid_path(_job_directory(), job["id"])
        if not file_store.write_csv(path, job, overwrite=False):
            raise RuntimeError("Batch identifier already exists")
    try:
        _start_worker()
    except Exception:
        job.update(status="interrupted", error="后台任务未能启动，请稍后重新提交。")
        _save_job(job)
        raise RuntimeError("Batch worker could not start") from None
    return job


def _cancel_requested(job_id, directory):
    job = get_batch(job_id, directory)
    return job is None or bool(job.get("cancel_requested")) or job["status"] in ("cancelled", "interrupted")


def cancel_batch(job_id):
    try:
        path = file_store.uuid_path(_job_directory(), job_id)
    except ValueError:
        return None
    with file_store.locked(path.parent):
        job = file_store.read_csv(path)
        if job is None:
            return None
        if job["status"] in ACTIVE_STATUSES:
            if job["status"] == "queued":
                job["status"] = "cancelled"
                for item in job["items"]:
                    item["status"] = "cancelled"
            else:
                job["status"] = "cancelling"
            job.update(cancel_requested=True, updated_at=_now())
            file_store.write_csv(path, job)
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
    link = {key: item[key] for key in ("file", "row", "signature")}
    link.update(id=record["id"], history_url="/history/" + record["id"],
                label=record["result"]["label"], level=record["result"]["level"],
                created_at=record["created_at"])
    path = _reference_path(item, directory)
    with file_store.locked(path.parent):
        previous = file_store.read_csv(path)
        if previous is None or (record["created_at"], record["id"]) >= (previous["created_at"], previous["id"]):
            file_store.write_csv(path, link)


def latest_reports(references):
    """按文件、行号、内容指纹关联最新已保存报告，旧内容不冒充当前结果。"""
    found = {}
    for reference in references:
        key = (reference["file"], reference["row"], reference["signature"])
        link = file_store.read_csv(_reference_path(reference))
        if link is not None:
            found[key] = {name: link[name] for name in ("id", "history_url", "label", "level", "created_at")}
    return found


def run_batch(job_id, directory=None):
    """只执行已入队的本地任务；不恢复旧云端调用。"""
    directory = directory or _directory()
    with _lock:
        with file_store.locked(_job_directory(directory)):
            job = get_batch(job_id, directory)
            if job is None or job["status"] != "queued":
                return
            job.update(status="running", updated_at=_now())
            file_store.write_csv(file_store.uuid_path(_job_directory(directory), job_id), job)
    return _run_batch(job_id, directory)


def _run_batch(job_id, directory):
    """执行一个已入队任务；公开给后台 worker 与无网络的同步测试。"""
    job = get_batch(job_id, directory)
    if job.get("deepseek_source") not in (None, "local"):
        job.update(status="interrupted", current_index=None,
                   error="当前网页不再运行旧连接方式的任务，请重新选择分析模型创建任务。")
        for item in job["items"]:
            if item["status"] in ("pending", "running"):
                item["status"] = "interrupted"
                for member in item["members"]:
                    if member.get("status") == "running":
                        member["status"] = "interrupted"
        _save_job(job, directory)
        return
    job["deepseek_source"] = "local"
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
            iterator = iter_risk_check(item["message"], job["model_ids"], job["mode"])
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
                    event["member"]["ui_display_name"] = web_models.model_label(event["member"])
                    item["members"].append(event["member"])
                elif event["type"] == "member_complete":
                    event["member"]["ui_display_name"] = web_models.model_label(event["member"])
                    item["members"][-1] = event["member"]
                    finished_members += 1
                elif event["type"] == "complete":
                    result = event["result"]
                    result.update(batch_id=job_id, batch_index=item["index"], batch_total=job["total"],
                                  dataset_ref={key: item[key] for key in ("file", "row", "signature")})
                    ui_members = [
                        {**member, "ui_display_name": web_models.model_label(member)}
                        for member in result["members"]
                    ]
                    item.update(status="completed", result=result, members=ui_members)
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
            jobs = [job for job in file_store.iter_records(_job_directory(directory)) if job["status"] == "queued"]
            if not jobs:
                _workers.pop(directory, None)
                return
            job_id = min(jobs, key=lambda job: (job["created_at"], job["id"]))["id"]
        try:
            run_batch(job_id, directory)
        except Exception:
            # Avoid repeating a task whose latest model call may already have completed.
            try:
                job = get_batch(job_id, directory)
                job.update(status="interrupted", current_index=None,
                           error="任务执行中断；已保存的报告仍可查看，请检查本地存储后再操作。")
                _save_job(job, directory)
            except Exception:
                with _lock:
                    _workers.pop(directory, None)
                return
