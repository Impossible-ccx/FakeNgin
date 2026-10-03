"""浏览器个人 API 配置：密钥只留在进程内，cookie 仅保存随机引用。"""

from datetime import datetime, timezone
import secrets
import threading
import time

from flask import request

from checkmodel.base import CheckError
from checkmodel.deepseek_api import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekAPI

COOKIE_NAME = "fakengin_api_token"
CREDENTIAL_TTL_SECONDS = 8 * 60 * 60
API_MODELS = (
    {"id": "deepseek-flash", "display_name": "DeepSeek Flash"},
    {"id": "deepseek-v4-pro", "display_name": "DeepSeek V4 Pro"},
)
_vault = {}
_lock = threading.RLock()


class MissingCloudModel:
    """网页必须使用访问者自己的配置，无 cookie 也绝不能落到环境密钥。"""

    name = "deepseek_r1"
    source = "cloud"
    source_label = "云端 API"
    credential_mode = "missing"
    display_name = "DeepSeek（请配置我的 API）"
    description = "请先填写自己的 DeepSeek API 密钥，再使用云端检测。"
    model_name = DEFAULT_MODEL

    def detect(self):
        return False

    def check(self, message):
        raise CheckError("请先在 API 设置中填写自己的 DeepSeek API 密钥。")


class ExpiredCloudModel(MissingCloudModel):
    """无效个人 token 保持不可用，不允许使用环境中的服务器密钥。"""

    credential_mode = "expired"
    display_name = "DeepSeek（我的 API 已过期）"
    description = "个人 API 配置已失效，请重新填写自己的 API 密钥。"

    def check(self, message):
        raise CheckError("我的 API 配置已过期或服务已重启，请重新填写 API 密钥。")


def _public_state(mode, configured, model_name=DEFAULT_MODEL, display_name=None, expires_at=None):
    labels = {"personal": "我的 API", "expired": "请配置我的 API", "missing": "请配置我的 API"}
    messages = {
        "personal": "个人配置仅保留在服务内存中，8 小时后或服务重启后需要重新填写；保存不验证额度或有效性，修改或清除配置不影响已经启动的批量任务。",
        "expired": "个人配置已过期或服务已重启，请重新填写自己的 API 密钥。",
        "missing": "请填写自己的 DeepSeek API 密钥后再使用云端检测。",
    }
    return {"mode": mode, "configured": configured, "label": labels[mode],
            "server_available": False,
            "display_name": display_name or "DeepSeek · {}（云端 API）".format(model_name),
            "model_name": model_name, "expires_at": expires_at, "message": messages[mode]}


def _default_state():
    return _public_state("missing", False)


def _prune_expired(now):
    for token in list(_vault):
        if _vault[token]["expires_at"] <= now:
            _remove_token(token)


def _remove_token(token):
    entry = _vault.pop(token, None)
    if entry is not None and entry.get("timer") is not None:
        entry["timer"].cancel()


def _expire_token(token):
    # The callback captures only the opaque token, never a key or model reference.
    with _lock:
        _remove_token(token)


def resolve_cloud_model():
    """返回请求专属模型和可公开状态；模型对象不得传给模板或序列化。"""
    token = request.cookies.get(COOKIE_NAME)
    with _lock:
        _prune_expired(time.time())
        if COOKIE_NAME not in request.cookies:
            return MissingCloudModel(), _default_state()
        entry = _vault.get(token)
        if entry is None:
            return ExpiredCloudModel(), _public_state("expired", False)
        model = entry["model"]
        expires_at = datetime.fromtimestamp(entry["expires_at"], timezone.utc).isoformat().replace("+00:00", "Z")
        return model, _public_state("personal", True, model.model_name, model.display_name, expires_at)


def get_state():
    return resolve_cloud_model()[1]


def save_credentials(api_key, model_name=DEFAULT_MODEL):
    """仅校验格式并创建独立实例；不探测远端、不改环境、不保存原始 cookie。"""
    if not isinstance(api_key, str):
        raise ValueError("请输入 DeepSeek API 密钥")
    api_key = api_key.strip()
    if not api_key or len(api_key) > 4096 or any(not 33 <= ord(char) <= 126 for char in api_key):
        raise ValueError("API 密钥格式无效，请检查是否包含空格或换行")
    if not isinstance(model_name, str) or model_name not in {model["id"] for model in API_MODELS}:
        raise ValueError("请选择支持的 DeepSeek 模型")
    model = DeepSeekAPI(api_key=api_key, model_name=model_name, base_url=DEFAULT_BASE_URL)
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + CREDENTIAL_TTL_SECONDS
    timer = threading.Timer(CREDENTIAL_TTL_SECONDS, _expire_token, args=(token,))
    timer.daemon = True
    with _lock:
        _prune_expired(now)
        _vault[token] = {"model": model, "expires_at": expires_at, "timer": timer}
        try:
            timer.start()
        except Exception:
            _remove_token(token)
            raise RuntimeError("Could not schedule credential expiry") from None
        _remove_token(request.cookies.get(COOKIE_NAME))
    public_expiry = datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z")
    return token, _public_state("personal", True, model.model_name, model.display_name, public_expiry)


def clear_credentials():
    with _lock:
        _remove_token(request.cookies.get(COOKIE_NAME))


def default_state():
    """兼容旧调用名；清除个人配置后只能回到未配置状态。"""
    return _default_state()
