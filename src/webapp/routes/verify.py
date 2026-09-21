"""人工校验系统页及消息的增删改、校验、跳过等操作。"""

from flask import flash, redirect, render_template, request, session, url_for

from .. import auth, newsdata
from . import main

PAGE_SIZE = 20


@main.route("/verify")
@auth.login_required
def verify():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    message_table = newsdata.load_all()
    total = len(message_table)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages

    start = (page - 1) * PAGE_SIZE
    rows = message_table.iloc[start:start + PAGE_SIZE].to_dict("records")
    check_row, check_state = _next_check(message_table)

    return render_template(
        "verify.html",
        rows=rows,
        nature_options=newsdata.NATURES,
        verify_natures=newsdata.VERIFY_NATURES,
        check_row=check_row,
        check_state=check_state,
        page=page,
        total=total,
        total_pages=total_pages,
        now=newsdata.now_string(),
    )


@main.route("/verify/add", methods=["POST"])
@auth.login_required
def verify_add():
    try:
        newsdata.append_message(newsdata.normalize_row(request.form))
        flash("消息已添加", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", page=_page_arg()))


@main.route("/verify/update", methods=["POST"])
@auth.login_required
def verify_update():
    try:
        row = request.form.get("_row", type=int)
        if row is None:
            raise ValueError("缺少目标消息位置")
        data = newsdata.normalize_row(request.form)
        newsdata.update_message(
            request.form.get("_file", ""),
            row,
            request.form.get("_signature", ""),
            data,
        )
        flash("消息已更新", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", page=_page_arg()))


@main.route("/verify/delete", methods=["POST"])
@auth.login_required
def verify_delete():
    try:
        row = request.form.get("_row", type=int)
        if row is None:
            raise ValueError("缺少目标消息位置")
        newsdata.delete_message(
            request.form.get("_file", ""),
            row,
            request.form.get("_signature", ""),
        )
        flash("消息已删除", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify", page=_page_arg()))


@main.route("/verify/check", methods=["POST"])
@auth.login_required
def verify_check():
    try:
        row = request.form.get("_row", type=int)
        if row is None:
            raise ValueError("缺少目标消息位置")
        file = request.form.get("_file", "")
        signature = request.form.get("_signature", "")
        nature = newsdata.normalize_value("nature", request.form.get("nature", ""))
        if nature not in newsdata.VERIFY_NATURES:
            raise ValueError("请选择消息性质")

        newsdata.update_message(file, row, signature, {
            "nature": nature,
            "process_time": newsdata.now_string(),
        })
        _remove_skip("{}:{}".format(file, signature))
        flash("校验完成", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.verify"))


@main.route("/verify/skip", methods=["POST"])
@auth.login_required
def verify_skip():
    key = "{}:{}".format(
        request.form.get("_file", ""),
        request.form.get("_signature", ""),
    )
    skip_keys = session.get("verify_skip", [])
    if key not in skip_keys:
        skip_keys.append(key)
    session["verify_skip"] = skip_keys
    flash("已跳过当前消息", "success")
    return redirect(url_for("main.verify"))


@main.route("/verify/reset_skip", methods=["POST"])
@auth.login_required
def verify_reset_skip():
    session["verify_skip"] = []
    flash("已重置跳过列表", "success")
    return redirect(url_for("main.verify"))


# ------------------------------------------------------------- helpers

def _skip_key(row):
    return "{}:{}".format(row["_file"], row["_signature"])


def _next_check(message_table):
    """返回 (待校验消息, 状态)。状态为 ready / all_skipped / all_verified。"""
    unverified = message_table[message_table["nature"] == newsdata.DEFAULT_NATURE]
    current_keys = {_skip_key(row) for _, row in unverified.iterrows()}
    skip_keys = [key for key in session.get("verify_skip", []) if key in current_keys]
    session["verify_skip"] = skip_keys

    if unverified.empty:
        return None, "all_verified"

    skip_set = set(skip_keys)
    for _, row in unverified.iterrows():
        if _skip_key(row) not in skip_set:
            return row.to_dict(), "ready"
    return None, "all_skipped"


def _remove_skip(key):
    skip_keys = session.get("verify_skip", [])
    if key in skip_keys:
        session["verify_skip"] = [k for k in skip_keys if k != key]


def _page_arg():
    page = request.form.get("page", 1, type=int)
    return page if page and page > 0 else 1
