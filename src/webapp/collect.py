"""在线采集编排：受限获取 → 解析校验 → 幂等去重 → 受控导入，全程留痕。

流程（run_collection，一次性任务，无定时调度）：
1. 只接受 allowlist 中的来源 ID；
2. collect_fetch 按边界执行请求（限流、预算、截止时间、体积上限）；
3. collect_parse 解析并规范化条目，非法内容跳过并记录原因；
4. collected_items 以（来源 ID + 外部 ID）幂等去重，消息正文再做一次
   内容级去重；整批导入在一个事务内完成；
5. 有效消息以“未校验”进入现有消息流程；是否加入检测队列由调用方
   显式选择（默认不触发大量模型请求）；
6. collection_runs 记录状态、请求与条目计数、错误摘要，失败不产生
   假消息、不破坏已有数据。

ETag / Last-Modified 条件请求：304 时不重复解析与入库。
"""

import logging
import sqlite3
import time

from . import collect_fetch, collect_parse, collect_sources, db, detection, newsdata

logger = logging.getLogger("fakengin.collect")

RUN_FIELDS = [
    "id", "source_id", "status", "started_at", "finished_at", "requests",
    "items_fetched", "items_new", "items_duplicate", "items_rejected",
    "messages_imported", "not_modified", "error",
]


def run_collection(source_id, enqueue_detection=False):
    """执行一次采集。返回本次 collection_runs 记录 dict。

    enqueue_detection=True 时，新导入的消息加入检测队列（显式选择，
    默认关闭——避免一次采集触发大量模型请求）。
    """
    source = collect_sources.get_source(source_id)
    run_id = _start_run(source_id)
    budget = collect_fetch.FetchBudget()
    deadline = time.monotonic() + collect_fetch.TASK_TIMEOUT
    try:
        result = collect_fetch.fetch_source(
            source, budget=budget, deadline=deadline,
            conditional_headers=_conditional_headers(source_id))

        if result.not_modified:
            _finish_run(run_id, requests=budget.used, not_modified=1)
            return get_run(run_id)

        items, rejections = collect_parse.parse_items(source, result.body)
        counts, message_ids = _import_items(source, items)
        _store_conditional_headers(source_id, result)

        if enqueue_detection and message_ids:
            detection.enqueue(message_ids)

        _finish_run(
            run_id, requests=budget.used,
            items_fetched=len(items),
            items_new=counts["new"],
            items_duplicate=counts["duplicate"],
            items_rejected=len(rejections),
            messages_imported=counts["imported"],
        )
        if rejections:
            logger.info("采集 %s 拒绝 %d 条：%s", source_id, len(rejections),
                        "; ".join(rejections[:3]))
    except collect_fetch.FetchError as exc:
        # 失败：记录可定位的错误摘要，不伪造成功、不产生假消息
        _finish_run(run_id, requests=budget.used, status="failed",
                    error=str(exc)[:500])
    except collect_parse.ParseRejected as exc:
        _finish_run(run_id, requests=budget.used, status="failed",
                    error="内容解析被拒绝：{}".format(str(exc))[:500])
    except Exception as exc:  # 兜底：任何异常都不让任务停留在 running
        logger.warning("采集任务 %s 异常：%s", source_id, type(exc).__name__)
        _finish_run(run_id, requests=budget.used, status="failed",
                    error="{}：{}".format(type(exc).__name__, str(exc))[:400])
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
    if not (result.etag or result.last_modified):
        return
    with db.db_conn() as conn:
        if result.etag:
            db.set_meta(conn, "collect:etag:{}".format(source_id), result.etag)
        if result.last_modified:
            db.set_meta(conn, "collect:lmod:{}".format(source_id),
                        result.last_modified)
