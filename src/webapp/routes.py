"""页面路由。"""

from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from . import auth, db

main = Blueprint("main", __name__)


def _safe_next(target):
    if target and target.startswith("/"):
        return target
    return None


@main.route("/")
def index():
    return render_template("index.html")


@main.route("/data")
def data():
    return render_template("data.html")


@main.route("/detect")
def detect():
    return render_template("detect.html")


@main.route("/verify")
@auth.login_required
def verify():
    return render_template("verify.html")


@main.route("/login", methods=["GET", "POST"])
def login():
    error = None
    username = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = db.find_user(username)
        if user is None or user["password"] != password:
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
