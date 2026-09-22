"""在线采集编排：受限子进程获取解析 → schema 校验 → 幂等去重 → 受控导入。

流程（run_collection，一次性任务，无定时调度）：
1. 只接受 allowlist 中的来源 ID；同来源以文件锁互斥（跨进程生效），
   已有任务进行中时本次直接失败并留痕；
2. 获取与解析在受资源限制的独立子进程执行（collect_isolated）：
   子进程不持有模型密钥与业务库访问权，输出经 schema 校验后才被采用；
   同来源请求间隔与全局请求预算由子进程经独立状态库跨进程维护；
3. collected_items 以（来源 ID + 外部 ID）幂等去重，消息正文再做一次
   内容级去重；整批导入在一个事务内完成；
4. 有效消息以“未校验”进入现有消息流程；是否加入检测队列由调用方
   显式选择（默认不触发大量模型请求）；
5. collection_runs 记录状态、请求与条目计数、错误摘要，失败不产生
   假消息、不破坏已有数据。

ETag / Last-Modified 条件请求：304 时不重复解析与入库。
"""

import fcntl
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager

from . import collect_fetch, collect_isolated, collect_sources, db, detection, newsdata

logger = logging.getLogger("fakengin.collect")

RUN_FIELDS = [
    "id", "source_id", "status", "started_at", "finished_at", "requests",
    "items_fetched", "items_new", "items_duplicate", "items_rejected",
    "messages_imported", "not_modified", "error",
]


class SourceBusyError(Exception):
    """该来源已有采集任务正在进行（文件锁被占用）。"""


def state_dir():
    """跨进程采集状态目录（间隔状态、来源锁），与业务库文件分离。"""
    return db.DATABASE_DIR / "collect_state"


@contextmanager
def _source_run_lock(source_id):
    """同来源互斥：非阻塞文件锁，进程退出（含崩溃）自动释放。"""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", source_id) or "unknown"
    fd = os.open(str(directory / "lock_{}.lock".format(safe_id)),
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SourceBusyError("该来源已有采集任务正在进行，请稍后再试")
        yield
    finally:
        os.close(fd)


def run_collection(source_id, enqueue_detection=False):
    """执行一次采集（获取与解析在受限子进程中完成）。

    返回本次 collection_runs 记录 dict。enqueue_detection=True 时，
    新导入的消息加入检测队列（显式选择，默认关闭——避免一次采集
    触发大量模型请求）。
    """
    source = collect_sources.get_source(source_id)
    run_id = _start_run(source_id)
    try:
        with _source_run_lock(source_id):
            result = collect_isolated.run_isolated_collection(
                source,
                conditional_headers=_conditional_headers(source_id),
                state_dir=state_dir(),
                min_interval=collect_fetch.MIN_INTERVAL_SECONDS,
                request_timeout=collect_fetch.REQUEST_TOTAL_TIMEOUT,
                task_timeout=collect_fetch.TASK_TIMEOUT,
                retry_backoff=collect_fetch.RETRY_BACKOFF_SECONDS,
            )
    except SourceBusyError as exc:
        _finish_run(run_id, status="failed", error=str(exc)[:500])
        return get_run(run_id)
    except collect_isolated.CollectionError as exc:
        _finish_run(run_id, status="failed", error=str(exc)[:500])
        return get_run(run_id)
    except Exception as exc:  # 兜底：任何异常都不让任务停留在 running
        logger.warning("采集任务 %s 异常：%s", source_id, type(exc).__name__)
        _finish_run(run_id, status="failed",
                    error="{}：{}".format(type(exc).__name__, str(exc))[:400])
        return get_run(run_id)

    if result["status"] != "succeeded":
        # 子进程内部失败（网络、解析、限额等）：错误摘要原样留痕
        _finish_run(run_id, requests=result["requests"], status="failed",
                    error=result["error"][:500])
        return get_run(run_id)

    if result["not_modified"]:
        _finish_run(run_id, requests=result["requests"], not_modified=1)
        return get_run(run_id)

    try:
        items, rejections = result["items"], result["rejections"]
        counts, message_ids = _import_items(source, items)
        _store_conditional_headers(source_id, result)
    except Exception as exc:
        logger.warning("采集任务 %s 入库异常：%s", source_id, type(exc).__name__)
        _finish_run(run_id, requests=result["requests"], status="failed",
                    error="入库失败（{}：{}），本次结果已丢弃".format(
                        type(exc).__name__, str(exc))[:400])
        return get_run(run_id)

    if enqueue_detection and message_ids:
        detection.enqueue(message_ids)

    _finish_run(
        run_id, requests=result["requests"],
        items_fetched=len(items),
        items_new=counts["new"],
        items_duplicate=counts["duplicate"],
        items_rejected=len(rejections),
        messages_imported=counts["imported"],
    )
    if rejections:
        logger.info("采集 %s 拒绝 %d 条：%s", source_id, len(rejections),
                    "; ".join(rejections[:3]))
    return get_run(run_id)


# ------------------------------------------------------------- 导入

def _import_items(source, items):
    """整批一个事务：collected_items 幂等去重 + 消息内容级去重 + 入库。"""
    counts = {"new": 0, "duplicate": 0, "imported": 0}
    message_ids = []
    now = db.now_string()
    with db.db_conn() as conn:
        for item in items:
            try:
                cursor = conn.execute(
                    "INSERT INTO collected_items (source_id, external_id, title, "
                    "content, link, published_at, collected_at, truncated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (source["id"], item["external_id"], item["title"],
                     item["content"], item["link"], item["published_at"],
                     now, 1 if item.get("truncated") else 0),
                )
            except sqlite3.IntegrityError:
                # （来源 ID + 外部 ID）已存在：幂等跳过
                counts["duplicate"] += 1
                continue
            counts["new"] += 1
            # 内容级去重：同一正文已作为消息存在时保留条目记录、不重复建消息
            exists = conn.execute(
                "SELECT 1 FROM messages WHERE content = ? LIMIT 1",
                (item["content"],),
            ).fetchone()
            if exists is not None:
                counts["duplicate"] += 1
                continue
            message_id = newsdata.insert_message_in_tx(
                conn,
                newsdata.normalize_row({
                    "content": item["content"],
                    "source": source.get("import_source") or source["name"],
                    "publish_time": item["published_at"],
                }),
                {},
            )
            conn.execute(
                "UPDATE collected_items SET message_id = ? WHERE id = ?",
                (message_id, cursor.lastrowid),
            )
            counts["imported"] += 1
            message_ids.append(message_id)
        if counts["imported"]:
            newsdata._bump_data_version(conn)
    return counts, message_ids


# ------------------------------------------------------------- 运行记录

def _start_run(source_id):
    with db.db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO collection_runs (source_id, status, started_at) "
            "VALUES (?, 'running', ?)",
            (source_id, db.now_string()),
        )
        return cursor.lastrowid


def _finish_run(run_id, requests=0, status="succeeded", **counts):
    fields = {
        "items_fetched": counts.get("items_fetched", 0),
        "items_new": counts.get("items_new", 0),
        "items_duplicate": counts.get("items_duplicate", 0),
        "items_rejected": counts.get("items_rejected", 0),
        "messages_imported": counts.get("messages_imported", 0),
        "not_modified": counts.get("not_modified", 0),
    }
    sets = ", ".join(
        ["status = ?", "finished_at = ?", "requests = ?", "error = ?"] +
        ["{} = ?".format(key) for key in fields])
    params = [status, db.now_string(), requests, counts.get("error", "")]
    params.extend(fields.values())
    params.append(run_id)
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE collection_runs SET {} WHERE id = ?".format(sets), params)


def get_run(run_id):
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM collection_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    return {key: record[key] for key in RUN_FIELDS}


def recent_runs(limit=10, source_id=None):
    """最近的采集运行记录，新 → 旧。"""
    where = "" if source_id is None else "WHERE source_id = ?"
    params = () if source_id is None else (source_id,)
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM collection_runs {} ORDER BY id DESC LIMIT ?".format(where),
            params + (limit,),
        ).fetchall()
    records = [dict(row) for row in rows]
    return [{key: record[key] for key in RUN_FIELDS} for record in records]


def items_for_run(run_id):
    """某次运行采集到的条目（按 collected_at 匹配最近一批，供展示追溯）。"""
    with db.db_conn() as conn:
        row = conn.execute(
            "SELECT started_at FROM collection_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            "SELECT id, source_id, external_id, title, link, published_at, "
            "collected_at, truncated, message_id FROM collected_items "
            "WHERE source_id = (SELECT source_id FROM collection_runs WHERE id = ?) "
            "AND collected_at >= ? ORDER BY id DESC LIMIT 50",
            (run_id, row["started_at"]),
        ).fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------------- 条件请求缓存

def _conditional_headers(source_id):
    with db.db_conn() as conn:
        etag = db.get_meta(conn, "collect:etag:{}".format(source_id), "")
        last_modified = db.get_meta(conn, "collect:lmod:{}".format(source_id), "")
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def _store_conditional_headers(source_id, result):
    etag = result.get("etag", "")
    last_modified = result.get("last_modified", "")
    if not (etag or last_modified):
        return
    with db.db_conn() as conn:
        if etag:
            db.set_meta(conn, "collect:etag:{}".format(source_id), etag)
        if last_modified:
            db.set_meta(conn, "collect:lmod:{}".format(source_id),
                        last_modified)
