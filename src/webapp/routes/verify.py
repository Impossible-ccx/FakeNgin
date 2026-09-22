"""人工校验系统页及消息的增删改、检测、审核、跳过等操作。

- 消息以稳定 ID 标识；表单携带 version 做乐观并发校验，多用户并发修改
  不会静默覆盖（后提交者收到“消息已被其他操作修改”提示）。
- 检测通过 detection_runs 队列异步执行；人工审核生成独立 reviews 历史。
"""

from flask import flash, redirect, render_template, request, session, url_for

import checkmodel

from .. import auth, db, detection, newsdata, reviews
from . import main

PAGE_SIZE = 20
QUEUE_FILTERS = ("pending", "done", "all")


def _selected_model_id():
    """表单里的检测模型选择；不在可用列表时退回默认（空 = 首个可用）。"""
    model_id = (request.form.get("model") or "").strip()
    if model_id and model_id not in {m["id"] for m in checkmodel.get_models()}:
        return ""
    return model_id


@main.route("/verify")
@auth.login_required
def verify():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    queue = request.args.get("queue", "all")
    if queue not in QUEUE_FILTERS:
        queue = "all"

    if queue == "pending":
        total = newsdata.count_messages(nature=newsdata.DEFAULT_NATURE)
    elif queue == "done":
        # “已处理” = 全部 - 未校验；列表与计数使用同一排除条件
        total = newsdata.count_messages(nature_not=newsdata.DEFAULT_NATURE)
    else:
        total = newsdata.count_messages()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages

    if queue == "pending":
        rows = newsdata.list_messages_filtered(
            nature=newsdata.DEFAULT_NATURE,
            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    elif queue == "done":
        rows = newsdata.list_messages_filtered(
            nature_not=newsdata.DEFAULT_NATURE,
            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    else:
        rows = newsdata.list_messages_filtered(
            limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    detection.attach_latest_runs(rows)

    pending_rows = [row for row in newsdata.load_all()
                    if row["nature"] == newsdata.DEFAULT_NATURE]
    detection.attach_latest_runs(pending_rows)
    check_row, check_state = _next_check(pending_rows)

    return render_template(
        "verify.html",
        rows=rows,
        nature_options=newsdata.NATURES,
        review_options=reviews.REVIEW_OPTIONS,
        check_row=check_row,
        check_state=check_state,
        page=page,
        total=total,
        total_pages=total_pages,
        queue=queue,
        queue_counts=detection.queue_counts(),
        models=checkmodel.get_models(),
        now=db.now_string(),
    )


@main.route("/verify/add", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_add():
    try:
        newsdata.append_message(newsdata.normalize_row(request.form))
        flash("消息已添加", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", page=_page_arg(), queue=request.form.get("queue", "all")))


@main.route("/verify/update", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_update():
    try:
        message_id = request.form.get("id", type=int)
        version = request.form.get("version", type=int)
        if message_id is None or version is None:
            raise ValueError("缺少目标消息标识")
        data = newsdata.normalize_row(request.form)
        newsdata.update_message(message_id, version, data)
        flash("消息已更新；若修改了正文，此前的检测结果将标记为过期", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", page=_page_arg(), queue=request.form.get("queue", "all")))


@main.route("/verify/delete", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_delete():
    try:
        message_id = request.form.get("id", type=int)
        version = request.form.get("version", type=int)
        if message_id is None or version is None:
            raise ValueError("缺少目标消息标识")
        newsdata.delete_message(message_id, version)
        flash("消息已删除", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", page=_page_arg(), queue=request.form.get("queue", "all")))


@main.route("/verify/detect", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_detect():
    """对单条消息发起检测。"""
    message_id = request.form.get("id", type=int)
    if message_id is None:
        flash("缺少目标消息标识", "error")
        return redirect(url_for("main.verify"))
    created = detection.enqueue([message_id], model_id=_selected_model_id())
    if created:
        flash("已加入检测队列，稍后自动执行", "success")
    else:
        flash("该消息已有检测任务在队列中，请等待完成", "error")
    return redirect(url_for("main.verify", queue=request.form.get("queue", "all")))


@main.route("/verify/detect_batch", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_detect_batch():
    """对全部未校验消息批量发起检测。"""
    pending_ids = [row["id"] for row in newsdata.load_all()
                   if row["nature"] == newsdata.DEFAULT_NATURE]
    created = detection.enqueue(pending_ids, model_id=_selected_model_id())
    skipped = len(pending_ids) - created
    message = "已加入检测队列 {} 条".format(created)
    if skipped:
        message += "；{} 条已有任务在队列中，自动跳过".format(skipped)
    flash(message, "success" if created else "error")
    return redirect(url_for("main.verify", queue=request.form.get("queue", "all")))


@main.route("/verify/retry/<int:run_id>", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_retry(run_id):
    """重试失败或中断的检测任务（新建任务，保留原失败记录）。"""
    try:
        created = detection.retry_run(run_id)
        if created:
            flash("已重新加入检测队列", "success")
        else:
            flash("该消息已有检测任务在队列中", "error")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", queue=request.form.get("queue", "all")))


@main.route("/verify/review", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_review():
    """保存人工审核记录（审核人、结论、证据），并更新消息当前性质。"""
    try:
        message_id = request.form.get("id", type=int)
        if message_id is None:
            raise ValueError("缺少目标消息标识")
        user = auth.get_current_user()
        reviews.add_review(
            message_id,
            reviewer=user["username"],
            conclusion=request.form.get("conclusion", ""),
            evidence=request.form.get("evidence", ""),
            note=request.form.get("note", ""),
            detection_run_id=request.form.get("detection_run_id", type=int),
            expected_version=request.form.get("version", type=int),
        )
        flash("审核记录已保存", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", queue=request.form.get("queue", "all")))


@main.route("/verify/skip", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_skip():
    key = request.form.get("id", "")
    skip_keys = session.get("verify_skip", [])
    if key and key not in skip_keys:
        skip_keys.append(key)
    session["verify_skip"] = skip_keys
    flash("已跳过当前消息", "success")
    return redirect(url_for("main.verify", queue=request.form.get("queue", "all")))


@main.route("/verify/reset_skip", methods=["POST"])
@auth.role_required("admin", "reviewer")
def verify_reset_skip():
    session["verify_skip"] = []
    flash("已重置跳过列表", "success")
    return redirect(url_for("main.verify"))


# ------------------------------------------------------------- helpers

def _skip_key(row):
    return str(row["id"])


def _next_check(pending_rows):
    """返回 (待校验消息, 状态)。状态为 ready / all_skipped / all_verified。"""
    current_keys = {_skip_key(row) for row in pending_rows}
    skip_keys = [key for key in session.get("verify_skip", []) if key in current_keys]
    session["verify_skip"] = skip_keys

    if not pending_rows:
        return None, "all_verified"

    skip_set = set(skip_keys)
    for row in pending_rows:
        if _skip_key(row) not in skip_set:
            return row, "ready"
    return None, "all_skipped"


def _page_arg():
    page = request.form.get("page", 1, type=int)
    return page if page and page > 0 else 1
