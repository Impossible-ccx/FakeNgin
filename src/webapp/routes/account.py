"""账户登录 / 登出。"""

from urllib.parse import urlsplit

from flask import make_response, redirect, render_template, request, url_for

from .. import auth, db
from . import main


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

        user = db.verify_user(username, password)
        if user is None:
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
