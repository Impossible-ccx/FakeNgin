"""账户登录 / 登出 / 修改密码。"""

from urllib.parse import urlsplit

from flask import flash, make_response, redirect, render_template, request, url_for

from .. import auth, db, ratelimit
from . import main

# 登录失败限流：同一用户名 + 来源 IP 在窗口内最多失败这么多次
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300
MIN_PASSWORD_LENGTH = 8


def _safe_next(target):
    r"""只接受站内路径，其余一律回首页。

    浏览器把 Location 中的反斜杠当作斜杠处理，因此先归一化再拒绝
    协议相对地址（//host、/\host），防止外部回跳。
    """
    if not target or not target.startswith("/"):
        return None
    normalized = target.replace("\\", "/")
    if normalized.startswith("//"):
        return None
    if urlsplit(normalized).netloc:
        return None
    return target


@main.route("/login", methods=["GET", "POST"])
def login():
    error = None
    username = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        limit_key = "login:{}:{}".format(username, request.remote_addr or "?")
        if not ratelimit.allowed(limit_key, LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS):
            error = "登录失败次数过多，请 {} 秒后再试".format(
                ratelimit.retry_after(limit_key, LOGIN_WINDOW_SECONDS))
        else:
            user = db.verify_user(username, password)
            if user is None:
                ratelimit.record(limit_key)
                error = "账户或密码错误"
            else:
                session_id = auth.login_user(user["username"])
                target = _safe_next(request.args.get("next"))
                response = make_response(redirect(target or url_for("main.index")))
                return auth.set_session_cookie(response, session_id)

    return render_template("login.html", error=error, username=username)


@main.route("/logout")
def logout():
    session_id = request.cookies.get(auth.SESSION_COOKIE)
    auth.logout_user(session_id)
    response = make_response(redirect(url_for("main.index")))
    return auth.clear_session_cookie(response)


@main.route("/account/password", methods=["GET", "POST"])
@auth.login_required
def account_password():
    """修改当前登录账户的密码：需验证原密码，新密码哈希后保存。"""
    user = auth.get_current_user()
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if db.verify_user(user["username"], current) is None:
            flash("原密码不正确", "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash("新密码至少 {} 位".format(MIN_PASSWORD_LENGTH), "error")
        elif new != confirm:
            flash("两次输入的新密码不一致", "error")
        else:
            db.update_password(user["username"], new)
            flash("密码已修改", "success")
            return redirect(url_for("main.index"))
    return render_template("account_password.html", user=user)
