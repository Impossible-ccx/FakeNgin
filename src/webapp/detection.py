"""检测任务队列：持久化检测运行、后台执行、失败重试、过期判定。

- detection_runs 保存每次检测的状态（pending/running/succeeded/failed/interrupted）、
  模型、输入版本、正文摘要、评分、理由、错误与耗时；历史不覆盖。
- 正文被修改后，旧检测通过 content_digest 与当前正文比对动态标记为“过期”，
  不改写历史记录。
- 后台执行：单守护线程轮询 pending 任务（课程规模够用，不引入外部队列）。
  应用重启时 running → interrupted；pending 任务由新进程的工作线程继续执行。
"""

import hashlib
import logging
import threading
import time

from . import db

logger = logging.getLogger("fakengin.detection")

TERMINAL_STATUSES = ("succeeded", "failed", "interrupted")
ACTIVE_STATUSES = ("pending", "running")

RUN_FIELDS = [
    "id", "message_id", "model_id", "model_name", "input_version",
    "content_digest", "status", "probability", "reason", "error",
    "duration_ms", "created_at", "started_at", "finished_at",
]

_worker_started = False
_worker_stop = threading.Event()


def content_digest(content):
    return hashlib.sha1(str(content).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 入队

def enqueue(message_ids, model_id="", model_name=""):
    """为消息创建 pending 检测任务。

    已有 pending/running 任务的消息跳过（重复点击不会创建重复任务）。
    返回新建任务数。
    """
    message_ids = list(dict.fromkeys(int(mid) for mid in message_ids))
    if not message_ids:
        return 0
    created = 0
    now = db.now_string()
    with db.db_conn() as conn:
        active = {
            row["message_id"]
            for row in conn.execute(
                "SELECT DISTINCT message_id FROM detection_runs WHERE status IN (?, ?)",
                ACTIVE_STATUSES,
            )
        }
        for message_id in message_ids:
            if message_id in active:
                continue
            exists = conn.execute(
                "SELECT 1 FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if exists is None:
                continue
            conn.execute(
                "INSERT INTO detection_runs (message_id, model_id, model_name, "
                "status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (message_id, model_id, model_name, now),
            )
            created += 1
            active.add(message_id)
    return created


def retry_run(run_id):
    """为失败/中断的任务创建新的 pending 任务，原记录保留不动。"""
    with db.db_conn() as conn:
        run = conn.execute(
            "SELECT * FROM detection_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError("检测任务不存在")
        if run["status"] not in ("failed", "interrupted"):
            raise ValueError("只有失败或中断的任务可以重试")
    return enqueue([run["message_id"]], run["model_id"], run["model_name"])


# ---------------------------------------------------------------- 执行

def _pick_model(conn, run):
    """返回 (model, model_id, model_name)。运行任务未指定模型时用当前可用模型。"""
    import checkmodel

    model_id = run["model_id"]
    try:
        model = checkmodel.get_model(model_id)
        return model, model_id
    except KeyError:
        pass
    models = checkmodel.get_models()
    if not models:
        return None, ""
    return checkmodel.get_model(models[0]["id"]), models[0]["id"]


def execute_run(run_id):
    """执行单个任务：running → succeeded/failed。返回最终状态。"""
    import checkmodel

    started = time.time()
    with db.db_conn() as conn:
        run = conn.execute(
            "SELECT * FROM detection_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None or run["status"] not in ACTIVE_STATUSES:
            return None
        message = conn.execute(
            "SELECT id, content, version FROM messages WHERE id = ?",
            (run["message_id"],),
        ).fetchone()
        if message is None:
            conn.execute(
                "UPDATE detection_runs SET status = 'failed', error = '所属消息已删除', "
                "finished_at = ? WHERE id = ?",
                (db.now_string(), run_id),
            )
            return "failed"
        model, model_id = _pick_model(conn, run)
        if model is None:
            conn.execute(
                "UPDATE detection_runs SET status = 'failed', "
                "error = '没有可用模型，请检查模型配置或稍后重试', finished_at = ? WHERE id = ?",
                (db.now_string(), run_id),
            )
            return "failed"
        conn.execute(
            "UPDATE detection_runs SET status = 'running', started_at = ?, "
            "model_id = ?, model_name = ?, input_version = ?, content_digest = ? "
            "WHERE id = ?",
            (
                db.now_string(), model_id, getattr(model, "display_name", model_id),
                message["version"], content_digest(message["content"]), run_id,
            ),
        )

    try:
        probability, reason = model.check(message["content"])
        probability = max(0.0, min(100.0, float(probability)))
        status, error = "succeeded", ""
    except Exception as exc:
        probability, reason = None, ""
        status = "failed"
        error = str(exc) or type(exc).__name__
        # 日志只含任务 ID 与错误类型，不记录正文与配置值
        logger.warning("检测任务 %s 失败：%s", run_id, type(exc).__name__)

    duration_ms = int((time.time() - started) * 1000)
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE detection_runs SET status = ?, probability = ?, reason = ?, "
            "error = ?, duration_ms = ?, finished_at = ? WHERE id = ?",
            (status, probability, reason, error, duration_ms, db.now_string(), run_id),
        )
    return status


def run_pending(limit=10):
    """按创建顺序执行 pending 任务，返回执行数。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM detection_runs WHERE status = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    for row in rows:
        execute_run(row["id"])
    return len(rows)


def recover_interrupted():
    """应用启动时调用：上次进程遗留的 running 任务标记为中断。"""
    with db.db_conn() as conn:
        cursor = conn.execute(
            "UPDATE detection_runs SET status = 'interrupted', "
            "error = '应用重启导致检测中断，可重试', finished_at = ? "
            "WHERE status = 'running'",
            (db.now_string(),),
        )
        return cursor.rowcount


# ---------------------------------------------------------------- 查询

def _row_to_dict(row):
    record = dict(row)
    return {key: record[key] for key in RUN_FIELDS}


def latest_runs():
    """每条消息最新一次检测任务，按消息 id 索引。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT dr.* FROM detection_runs dr "
            "JOIN (SELECT message_id, MAX(id) AS max_id FROM detection_runs "
            "      GROUP BY message_id) latest "
            "  ON dr.id = latest.max_id"
        ).fetchall()
    return {row["message_id"]: _row_to_dict(row) for row in rows}


def runs_for_message(message_id):
    """某消息全部检测历史，新→旧。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM detection_runs WHERE message_id = ? ORDER BY id DESC",
            (message_id,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def queue_counts():
    """返回 {pending, running, failed_last_day...} 的队列概览。"""
    with db.db_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM detection_runs WHERE status = 'pending'"
        ).fetchone()["n"]
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM detection_runs WHERE status = 'running'"
        ).fetchone()["n"]
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM detection_runs WHERE status IN ('failed', 'interrupted')"
        ).fetchone()["n"]
    return {"pending": pending, "running": running, "failed": failed}


def is_stale(run, current_content):
    """正文变更后旧检测视为过期（只读判定，不改历史）。"""
    if not run.get("content_digest"):
        return False
    return run["content_digest"] != content_digest(current_content)


# ------------------------------------------------------------ 后台线程

def start_worker(poll_interval=2.0):
    """启动单守护线程轮询执行 pending 任务；重复调用安全。"""
    global _worker_started
    if _worker_started:
        return

    def loop():
        while not _worker_stop.is_set():
            try:
                executed = run_pending()
            except Exception:
                executed = 0
            if not executed:
                _worker_stop.wait(poll_interval)

    _worker_stop.clear()
    thread = threading.Thread(target=loop, name="detection-worker", daemon=True)
    thread.start()
    _worker_started = True
