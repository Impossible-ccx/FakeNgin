"""Cookie + Session 登录状态管理。

登录成功后生成 session_id 并写入 sessions.csv，Cookie 中只保存 session_id。
"""

import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import abort, redirect, request, url_for

from . import db

SESSION_COOKIE = "session_id"
SESSION_TTL = timedelta(days=3)  # 登录凭证有效期 3 天
SESSION_MAX_AGE = int(SESSION_TTL.total_seconds())  # Cookie 也 3 天过期
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def login_user(username):
    """创建登录状态并返回 session_id。"""
    session_id = uuid.uuid4().hex
    db.add_session(session_id, username)
    return session_id


def logout_user(session_id):
    if session_id:
        db.remove_session(session_id)


def get_current_user():
    """从 Cookie 解析当前登录用户，未登录或已过期返回 None。"""
    session_id = request.cookies.get(SESSION_COOKIE)
    session = db.find_session(session_id)
    if session is None:
        return None
    if _is_expired(session):
        db.remove_session(session_id)
        return None
    user = db.find_user(session["username"])
    if user is None:
        return None
    return user


def _is_expired(session):
    created_at = session.get("created_at", "")
    try:
        created = datetime.strptime(created_at, TIME_FORMAT)
    except (ValueError, TypeError):
        return True
    return datetime.now() >= created + SESSION_TTL


def set_session_cookie(response, session_id):
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE)
    return response


def role_required(*roles):
    """登录且角色匹配才可访问；写接口服务端强制校验，不依赖前端隐藏按钮。"""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = get_current_user()
            if user is None:
                return redirect(url_for("main.login", next=request.path))
            if roles and user.get("role") not in roles:
                abort(403, description="当前账户角色无权执行该操作")
            return view(*args, **kwargs)
        return wrapped
    return decorator


def login_required(view):
    """未登录时重定向到登录页，登录后回跳原页面。"""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if get_current_user() is None:
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped
