"""人工校验与消息维护；登录后操作，始终核对原行指纹。"""

import math
import re
import threading

from flask import flash, redirect, render_template, request, session, url_for

from checkmodel.ensemble import MAX_MESSAGE_LENGTH
from .. import auth, newsdata
from . import main

PAGE_SIZE = 20
_mutation_lock = threading.RLock()


@main.route("/verify")
def verify():
    # Keep a selected message and filter when a visitor signs in from a deep link.
    if auth.get_current_user() is None:
        return redirect(url_for("main.login", next=request.full_path.rstrip("?")))
    page = max(1, request.args.get("page", 1, type=int))
    message_table = newsdata.load_all()
    total = len(message_table)
    pending_mask = ~message_table["nature"].isin(newsdata.VERIFY_NATURES)
    unverified_count = int(pending_mask.sum())
    status = _status_arg(request.args)
    filtered = message_table if status == "all" else message_table[pending_mask if status == "pending" else ~pending_mask]
    focus_row = None
    focus_error = None
    focus_index = None
    if any(key in request.args for key in ("file", "row", "signature")):
        try:
            name, row, signature, _ = _target(request.args, prefix="")
            match = message_table[
                (message_table["_file"] == name) & (message_table["_row"] == row)
                & (message_table["_signature"] == signature)
            ]
            if match.empty:
                raise ValueError("消息已发生变化，请刷新后重试")
            focus_index = match.index[0]
            focus_row = match.iloc[0].to_dict()
            if focus_index not in filtered.index:
                status, filtered = "all", message_table
            position = filtered.index.get_loc(focus_index)
            page = int(position) // PAGE_SIZE + 1
        except ValueError as exc:
            focus_error = str(exc)
    filtered_total = len(filtered)
    total_pages = max(1, (filtered_total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    selected = filtered.iloc[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    rows = selected.to_dict("records")
    for index, row in zip(selected.index, rows):
        row["verify_anchor"] = "verify-row-{}".format(index)
        row["is_focused"] = index == focus_index
    check_row, check_state = _next_check(message_table)
    if focus_row is not None:
        check_row, check_state = focus_row, "ready"
    return render_template(
        "verify.html", rows=rows, nature_options=newsdata.NATURES,
        verify_natures=newsdata.VERIFY_NATURES, check_row=check_row, check_state=check_state,
        page=page, total=total, total_pages=total_pages, now=newsdata.now_string(),
        max_message_length=MAX_MESSAGE_LENGTH,
        unverified_count=unverified_count, reviewed_count=total - unverified_count,
        filtered_total=filtered_total, status=status, focus_row=focus_row, focus_error=focus_error,
        focus_anchor="verify-row-{}".format(focus_index) if focus_index is not None else None,
        skipped_count=len(_stored_skips()),
    )


@main.route("/verify/add", methods=["POST"])
@auth.login_required
def verify_add():
    try:
        data = _editable_data()
        with _mutation_lock:
            newsdata.append_message(data)
        flash("消息已添加", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return _return_to_verify()


@main.route("/verify/update", methods=["POST"])
@auth.login_required
def verify_update():
    try:
        with _mutation_lock:
            name, row, signature, existing = _target()
            data = _editable_data(existing)
            newsdata.update_message(name, row, signature, data)
        _remove_skip(_skip_key({"_file": name, "_row": row, "_signature": signature}))
        flash("消息已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return _return_to_verify()


@main.route("/verify/delete", methods=["POST"])
@auth.login_required
def verify_delete():
    try:
        with _mutation_lock:
            name, row, signature, _ = _target()
            newsdata.delete_message(name, row, signature)
        _remove_skip(_skip_key({"_file": name, "_row": row, "_signature": signature}))
        flash("消息已删除", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return _return_to_verify()


@main.route("/verify/check", methods=["POST"])
@auth.login_required
def verify_check():
    try:
        nature = request.form.get("nature", "").strip()
        if nature not in newsdata.VERIFY_NATURES:
            raise ValueError("请选择真实、虚假或中立")
        with _mutation_lock:
            name, row, signature, _ = _target()
            newsdata.update_message(name, row, signature, {
                "nature": nature, "process_time": newsdata.now_string(),
            })
        _remove_skip(_skip_key({"_file": name, "_row": row, "_signature": signature}))
        flash("校验完成", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return _return_to_verify(queue=True)


@main.route("/verify/skip", methods=["POST"])
@auth.login_required
def verify_skip():
    try:
        with _mutation_lock:
            name, row, signature, existing = _target()
        if existing["nature"] in newsdata.VERIFY_NATURES:
            raise ValueError("这条消息已经校验，请刷新页面")
        key = _skip_key({"_file": name, "_row": row, "_signature": signature})
        skip_keys = _stored_skips()
        if key not in skip_keys:
            skip_keys.append(key)
        session["verify_skip"] = skip_keys
        flash("已跳过当前消息", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return _return_to_verify(queue=True)


@main.route("/verify/reset_skip", methods=["POST"])
@auth.login_required
def verify_reset_skip():
    session["verify_skip"] = []
    flash("已重置跳过列表", "success")
    return _return_to_verify(queue=True)


def _editable_data(existing=None):
    content = request.form.get("content", "").strip()
    if not content:
        raise ValueError("请输入消息内容")
    if len(content) > MAX_MESSAGE_LENGTH:
        raise ValueError("消息内容不能超过 {} 个字符".format(MAX_MESSAGE_LENGTH))
    values = {**(existing or {}), **request.form.to_dict()}
    values["content"] = content
    if len(values.get("source", "").strip()) > 200:
        raise ValueError("消息来源不能超过 200 个字符")
    nature = values.get("nature", newsdata.DEFAULT_NATURE).strip()
    if nature not in newsdata.NATURES:
        raise ValueError("请选择有效的消息性质")
    values["nature"] = nature
    legacy_score = values.get("fake_probability", "").strip()
    if legacy_score:
        try:
            numeric_score = float(legacy_score)
        except (ValueError, OverflowError):
            raise ValueError("历史分类分数必须是 0 到 100 的有限数值") from None
        if not math.isfinite(numeric_score) or not 0 <= numeric_score <= 100:
            raise ValueError("历史分类分数必须是 0 到 100 的有限数值")
    # normalize_row intentionally excludes every risk_* field, including forged form input.
    return newsdata.normalize_row(values)


def _target(values=None, prefix="_"):
    values = request.form if values is None else values
    name = values.get(prefix + "file", "")
    row = values.get(prefix + "row", type=int)
    signature = values.get(prefix + "signature", "")
    if row is None or row < 0:
        raise ValueError("缺少有效的目标消息位置")
    if not name or "/" in name or "\\" in name:
        raise ValueError("非法的消息表名")
    if re.fullmatch(r"[0-9a-f]{16}", signature) is None:
        raise ValueError("缺少有效的消息指纹，请刷新页面后重试")
    _, table = newsdata._locate(name, row, signature)
    return name, row, signature, table.iloc[row].to_dict()


def _skip_key(row):
    # Include the row number: two identical messages must remain separately skippable.
    return "{}:{}:{}".format(row["_file"], row["_row"], row["_signature"])


def _stored_skips():
    values = session.get("verify_skip", [])
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _next_check(message_table):
    """返回待校验消息和 ready/all_skipped/all_verified；空标签也可以校验。"""
    unverified = message_table[~message_table["nature"].isin(newsdata.VERIFY_NATURES)]
    current_keys = {_skip_key(row) for _, row in unverified.iterrows()}
    skip_keys = [key for key in _stored_skips() if key in current_keys]
    session["verify_skip"] = skip_keys
    if unverified.empty:
        return None, "all_verified"
    skip_set = set(skip_keys)
    for _, row in unverified.iterrows():
        if _skip_key(row) not in skip_set:
            return row.to_dict(), "ready"
    return None, "all_skipped"


def _remove_skip(key):
    session["verify_skip"] = [value for value in _stored_skips() if value != key]


def _page_arg():
    page = request.form.get("page", 1, type=int)
    return page if page and page > 0 else 1


def _status_arg(values):
    value = values.get("status", "all")
    return value if value in ("all", "pending", "reviewed") else "all"


def _return_to_verify(queue=False):
    # Drop the old focus fingerprint after any action; continue with a fresh queue.
    return redirect(url_for("main.verify", page=1 if queue else _page_arg(), status=_status_arg(request.form)))
