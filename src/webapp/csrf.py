"""轻量 CSRF 防护：会话令牌 + 所有 POST 表单校验。

- 令牌存于 Flask session（由 SECRET_KEY 签名），每会话一个。
- 模板通过上下文变量 csrf_token 输出隐藏字段。
- before_request 对所有 POST 请求校验 _csrf_token，缺失或不匹配返回 400。
"""

import secrets

from flask import abort, request, session

SESSION_KEY = "_csrf_token"
FORM_FIELD = "_csrf_token"


def get_token():
    """取当前会话的 CSRF 令牌，无则生成。"""
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        session[SESSION_KEY] = token
    return token


def install(app):
    """注册上下文变量与请求校验。"""

    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": get_token()}

    @app.before_request
    def _verify_csrf():
        if request.method != "POST":
            return None
        token = session.get(SESSION_KEY, "")
        sent = request.form.get(FORM_FIELD, "")
        if not token or not sent or not secrets.compare_digest(token, sent):
            abort(400, description="CSRF 校验失败，请刷新页面后重试")
        return None
