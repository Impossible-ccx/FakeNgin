"""个人 API 设置；敏感输入只接受同源 JSON，不回显密钥。"""

from urllib.parse import urlsplit

from flask import after_this_request, jsonify, make_response, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from checkmodel.deepseek_api import DEFAULT_MODEL
from .. import api_credentials
from . import main


def _same_origin():
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    claimed = request.headers.get("Origin") or request.headers.get("Referer")
    if not claimed:
        return False
    try:
        parsed, target = urlsplit(claimed), urlsplit(request.host_url)
        return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) == (
            target.scheme, target.hostname, target.port or (443 if target.scheme == "https" else 80)
        ) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _mutation_error():
    if not _same_origin():
        return jsonify(error="请在本站设置页面提交 API 配置。"), 403
    if not request.is_json:
        return jsonify(error="请启用网页脚本后提交 API 配置。"), 400
    return None


def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@main.route("/settings/api", methods=["GET", "POST"])
def api_settings():
    after_this_request(_private)
    if request.method == "GET":
        state = api_credentials.get_state()
        if request.accept_mimetypes.best == "application/json":
            return _private(jsonify(credentials=state))
        return _private(make_response(render_template(
            "api_settings.html", deepseek_api_state=state, api_models=api_credentials.API_MODELS,
        )))
    failure = _mutation_error()
    if failure is not None:
        return failure
    request.max_content_length = 8192
    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("API 配置请求格式无效")
        token, state = api_credentials.save_credentials(payload.get("api_key"), payload.get("model_name", DEFAULT_MODEL))
    except RequestEntityTooLarge:
        return jsonify(error="API 配置内容过长。"), 413
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="API 配置暂时无法保存，请稍后重试。"), 500
    response = _private(jsonify(credentials=state))
    # Session cookie intentionally outlives the vault TTL within this browser session:
    # an expired token must remain distinguishable from never choosing personal credentials.
    response.set_cookie(api_credentials.COOKIE_NAME, token, httponly=True,
                        secure=request.is_secure, samesite="Strict", path="/")
    return response


@main.route("/settings/api/clear", methods=["POST"])
def api_settings_clear():
    after_this_request(_private)
    failure = _mutation_error()
    if failure is not None:
        return failure
    api_credentials.clear_credentials()
    response = _private(jsonify(credentials=api_credentials.default_state()))
    response.delete_cookie(api_credentials.COOKIE_NAME, path="/", secure=request.is_secure,
                           httponly=True, samesite="Strict")
    return response
