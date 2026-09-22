"""人工审核记录：审核历史独立保存，关联检测任务，不覆盖历史。

- 每次人工校验生成一条 reviews 记录（审核人、结论、证据、说明、时间），
  并在同一事务内更新 messages.nature / process_time（当前处理状态）。
- 结论集合：虚假 / 真实 / 中立（旧标签，保留兼容） / 证据不足。
  旧数据中的“中立”与新选项“证据不足”语义接近，界面已不再默认提供“中立”。
"""

from . import db

CONCLUSIONS = ["虚假", "真实", "证据不足", "中立"]
# 人工校验界面提供的选项（“中立”仅为历史兼容保留在 CONCLUSIONS 中）
REVIEW_OPTIONS = ["虚假", "真实", "证据不足"]

REVIEW_FIELDS = [
    "id", "message_id", "detection_run_id", "reviewer",
    "conclusion", "evidence", "note", "created_at",
]


def add_review(message_id, reviewer, conclusion, evidence="", note="",
               detection_run_id=None, expected_version=None):
    """保存审核记录并更新消息当前性质；原子事务。返回记录 id。

    - reviewer 必须是已登录用户名（由路由层保证）。
    - conclusion 必须在 CONCLUSIONS 内。
    - detection_run_id 提供时必须存在且属于同一条消息（防跨消息关联）；
      失败/中断/已过期的检测也允许关联（审核人可能正是看到了失败结果），
      过期与否由展示层标记，不在写入层强制。
    - expected_version 提供时必须与当前消息版本一致；正文或属性被其他
      操作改动后，旧页面提交的审核会被拒绝，要求刷新后重审。
    """
    reviewer = (reviewer or "").strip()
    conclusion = (conclusion or "").strip()
    evidence = (evidence or "").strip()
    note = (note or "").strip()
    if not reviewer:
        raise ValueError("缺少审核人")
    if conclusion not in CONCLUSIONS:
        raise ValueError("审核结论无效")
    if detection_run_id is not None:
        try:
            detection_run_id = int(detection_run_id)
        except (TypeError, ValueError):
            raise ValueError("关联检测任务无效")

    now = db.now_string()
    with db.db_conn() as conn:
        message = conn.execute(
            "SELECT id, version FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if message is None:
            raise ValueError("消息不存在，可能已被删除")
        if expected_version is not None and message["version"] != expected_version:
            raise ValueError("消息已被其他操作修改，请刷新后重新审核")
        if detection_run_id is not None:
            run = conn.execute(
                "SELECT message_id FROM detection_runs WHERE id = ?",
                (detection_run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("关联检测任务不存在")
            if run["message_id"] != message_id:
                raise ValueError("关联检测任务不属于该消息")

        cursor = conn.execute(
            "INSERT INTO reviews (message_id, detection_run_id, reviewer, "
            "conclusion, evidence, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, detection_run_id, reviewer, conclusion,
             evidence, note, now),
        )
        # nature 若与当前一致则不变更内容字段，但仍记录审核历史
        conn.execute(
            "UPDATE messages SET nature = ?, process_time = ?, "
            "updated_at = ?, version = version + 1 WHERE id = ?",
            (conclusion, now, now, message_id),
        )
        data_version = int(db.get_meta(conn, "messages_data_version", "0")) + 1
        db.set_meta(conn, "messages_data_version", data_version)
        return cursor.lastrowid


def reviews_for_message(message_id):
    """某消息的审核历史，新→旧。"""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT id, message_id, detection_run_id, reviewer, conclusion, "
            "evidence, note, created_at FROM reviews WHERE message_id = ? "
            "ORDER BY id DESC",
            (message_id,),
        ).fetchall()
    records = [dict(row) for row in rows]
    return [{key: record[key] for key in REVIEW_FIELDS} for record in records]
