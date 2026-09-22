"""检测任务队列：持久化检测运行、后台执行、失败重试、过期判定。

- detection_runs 保存每次检测的状态（pending/running/succeeded/failed/interrupted）、
  模型、输入版本、正文摘要、评分、理由、错误与耗时；历史不覆盖。
- 正文被修改后，旧检测通过 content_digest 与当前正文比对动态标记为“过期”，
  不改写历史记录。
- 后台执行：每进程一个守护线程轮询 pending 任务（课程规模够用，不引入外部队列）。
- 多进程部署（gunicorn 多 worker）安全：任务认领 pending → running 为原子更新，
  重复认领不会发生；每个进程以 worker_id 注册并周期心跳，
  recover_interrupted 只回收“worker 记录缺失或心跳超时”的任务，
  不会误伤其他活跃进程正在执行的任务。
"""

import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import db, newsdata

logger = logging.getLogger("fakengin.detection")

TERMINAL_STATUSES = ("succeeded", "failed", "interrupted")
ACTIVE_STATUSES = ("pending", "running")

RUN_FIELDS = [
    "id", "message_id", "model_id", "model_name", "input_version",
    "content_digest", "status", "probability", "reason", "error",
    "duration_ms", "created_at", "started_at", "finished_at", "worker_id",
]

# worker 心跳超过该秒数视为进程已死，其 running 任务可被回收。
# 需大于单次检测可能的最长耗时（模型超时等），默认 5 分钟，可用环境变量调整。
WORKER_STALE_SECONDS = max(
    60, int(os.getenv("FAKENGIN_WORKER_STALE_SECONDS", "300") or 300))
HEARTBEAT_INTERVAL = 5.0
RECOVER_INTERVAL = 30.0

_worker_started = False
_worker_stop = threading.Event()
_WORKER_ID = None
_WORKER_ID_LOCK = threading.Lock()


def _worker_identity():
    """本进程的 worker 标识；惰性生成（含 PID），fork 后各进程不同。"""
    global _WORKER_ID
    with _WORKER_ID_LOCK:
        if _WORKER_ID is None:
            _WORKER_ID = "w-{}-{}".format(os.getpid(), uuid.uuid4().hex[:12])
        return _WORKER_ID


def _register_worker():
    with db.db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO workers (worker_id, started_at, heartbeat_at) "
            "VALUES (?, ?, ?)",
            (_worker_identity(), db.now_string(), db.now_string()),
        )


def _heartbeat():
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE workers SET heartbeat_at = ? WHERE worker_id = ?",
            (db.now_string(), _worker_identity()),
        )



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


def _invoke_model(model, content, comments):
    """调用模型检测；支持序列路线的模型传入正文 + 按时间排序的评论。

    评论序列即 PDF 的回复树路线（TF-IDF + 时间排序 + 特征矩阵 + RNN）；
    未实现 check_sequence 的模型（如远程 API）退化为单文本检测。
    """
    check_sequence = getattr(model, "check_sequence", None)
    if callable(check_sequence):
        return check_sequence(content, comments)
    return model.check(content)


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
        # 已被其他活跃进程认领的任务不重复执行（空 worker_id 视为可接管）
        if run["status"] == "running" and run["worker_id"] not in ("", _worker_identity()):
            return None
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
            "model_id = ?, model_name = ?, input_version = ?, content_digest = ?, "
            "worker_id = ? WHERE id = ?",
            (
                db.now_string(), model_id, getattr(model, "display_name", model_id),
                message["version"], content_digest(message["content"]),
                _worker_identity(), run_id,
            ),
        )

    try:
        comments = newsdata.list_comments(run["message_id"])
        probability, reason = _invoke_model(model, message["content"], comments)
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


def _claim_one():
    """原子认领一个 pending 任务（多进程部署时防止重复执行）。

    认领（pending → running）在同一事务内完成，并以 status='pending'
    作为更新条件；被其他进程抢先认领时 rowcount 为 0，返回 None。
    """
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT id FROM detection_runs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cursor = conn.execute(
            "UPDATE detection_runs SET status = 'running', started_at = ?, "
            "worker_id = ? WHERE id = ? AND status = 'pending'",
            (db.now_string(), _worker_identity(), row["id"]),
        )
        if cursor.rowcount != 1:
            return None
        return row["id"]


def run_pending(limit=10):
    """按创建顺序认领并执行 pending 任务，返回执行数。"""
    executed = 0
    while executed < limit:
        run_id = _claim_one()
        if run_id is None:
            break
        execute_run(run_id)
        executed += 1
    return executed


def recover_interrupted():
    """把已死进程遗留的 running 任务标记为中断，返回回收数。

    判定“已死”的依据（满足其一）：
    - worker_id 为空（旧版本遗留，无从判定归属）；
    - workers 表中无该 worker 记录；
    - worker 心跳超过 WORKER_STALE_SECONDS 未更新。

    活跃 worker 正在执行的任务不受影响，因此多进程部署下可安全地
    在启动时和运行中周期调用。同时清理心跳超时的 worker 注册记录。
    """
    cutoff = (datetime.now() - timedelta(seconds=WORKER_STALE_SECONDS)).strftime(
        db.TIME_FORMAT)
    with db.db_conn() as conn:
        cursor = conn.execute(
            "UPDATE detection_runs SET status = 'interrupted', "
            "error = '执行进程已停止，检测中断，可重试', finished_at = ? "
            "WHERE status = 'running' AND (worker_id = '' OR worker_id NOT IN "
            "(SELECT worker_id FROM workers WHERE heartbeat_at >= ?))",
            (db.now_string(), cutoff),
        )
        recovered = cursor.rowcount
        conn.execute("DELETE FROM workers WHERE heartbeat_at < ?", (cutoff,))
        return recovered


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


def attach_latest_runs(rows):
    """给消息 dict 列表附加 latest_run / run_stale 字段。

    列表、搜索、滚动加载、详情和复核页共用同一展示口径：
    最新检测任务的状态与结果（含过期标记），而不是 messages.fake_probability
    （该字段只反映人工录入或旧 CSV 导入的数值）。
    """
    runs = latest_runs()
    for row in rows:
        run = runs.get(row["id"])
        row["latest_run"] = run
        row["run_stale"] = bool(run) and is_stale(run, row["content"])
    return rows


# ------------------------------------------------------------ 后台线程

def start_worker(poll_interval=2.0):
    """启动后台执行：注册 worker、心跳线程与检测轮询线程；重复调用安全。"""
    global _worker_started
    if _worker_started:
        return

    _register_worker()

    def heartbeat_loop():
        while not _worker_stop.is_set():
            try:
                _heartbeat()
            except Exception:
                pass
            _worker_stop.wait(HEARTBEAT_INTERVAL)

    def loop():
        last_recover = 0.0
        while not _worker_stop.is_set():
            executed = 0
            try:
                executed = run_pending()
                now = time.monotonic()
                if now - last_recover >= RECOVER_INTERVAL:
                    recover_interrupted()
                    last_recover = now
            except Exception:
                executed = 0
            if not executed:
                _worker_stop.wait(poll_interval)

    _worker_stop.clear()
    threading.Thread(target=heartbeat_loop, name="detection-heartbeat",
                     daemon=True).start()
    thread = threading.Thread(target=loop, name="detection-worker", daemon=True)
    thread.start()
    _worker_started = True
