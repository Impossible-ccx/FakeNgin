"""DeepSeek 云端风险模型；只有显式调用 check 时才发送付费请求。"""

import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .base import CheckError, RiskAbstention
from .ollama_base import OllamaModel
from .ollama_deepseek import Ollama_DeepSeek


DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# 官方 Chat Completions 文档明确支持这两个模型的 thinking 开关。
NON_THINKING_MODELS = {"deepseek-flash", "deepseek-v4-pro"}


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # 不向重定向目标转发 Authorization。
        return None


class _CloudChatClient:
    """将云端回复适配到现有风险 JSON/schema 校验，避免两套解析规则。"""

    def __init__(self, model):
        self.model = model

    def chat(self, *, model, messages, format, options):
        content = self.model._post_chat(messages)
        return {"message": {"content": content}}


class DeepSeekAPI(OllamaModel):
    # 与本地 DeepSeek 共用逻辑 ID，由模型工厂优先选择云端，始终只有一票。
    name = "deepseek_r1"
    display_name = "DeepSeek（云端 API）"
    description = "通过 DeepSeek 云端接口评估消息风险，无需下载本地权重。"
    timeout = 60
    temperature = 0

    def __init__(self):
        self.initialize()

    def initialize(self):
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.model_name = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        if not self.base_url:
            self.base_url = DEFAULT_BASE_URL
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise CheckError("DeepSeek API 地址配置无效")
        self.display_name = "DeepSeek · {}（云端 API）".format(self.model_name)

    def detect(self):
        """仅检查密钥是否配置，不发送模型列表或推理探测请求。"""
        return bool(self._api_key)

    def _clean_content(self, content):
        """兼容返回在正文中的旧式思考包装，只解析最终风险 JSON。"""
        return Ollama_DeepSeek._clean_content(self, content)

    def check(self, message):
        if not self.detect():
            raise CheckError("尚未配置 DeepSeek API 密钥")
        try:
            # 复用同一提示词及 null、有限值、范围、reason/schema 校验。
            return self._request(_CloudChatClient(self), message)
        except (RiskAbstention, CheckError):
            raise
        except Exception:
            raise CheckError("DeepSeek 响应格式无效，请稍后重试") from None

    def _post_chat(self, messages):
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "response_format": {"type": "json_object"},
            "temperature": self.temperature,
            "max_tokens": 1024,
        }
        if self.model_name in NON_THINKING_MODELS:
            payload["thinking"] = {"type": "disabled"}
        try:
            request = Request(
                self.base_url + "/chat/completions",
                data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=self.timeout) as response:
                raw_response = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            messages_by_code = {
                400: "DeepSeek 请求参数无效，请检查模型及接口配置",
                401: "DeepSeek API 密钥无效或未获授权",
                402: "DeepSeek 账户额度不足",
                403: "DeepSeek 请求被拒绝，请检查账户权限",
                404: "DeepSeek 模型或接口不存在，请检查配置",
                429: "DeepSeek 请求受到限流，请稍后重试",
            }
            if 300 <= code < 400:
                error = "DeepSeek 接口发生重定向，请检查接口地址"
            elif code >= 500:
                error = "DeepSeek 服务暂时不可用，请稍后重试"
            else:
                error = messages_by_code.get(code, "DeepSeek 请求失败，请稍后重试")
            raise CheckError(error) from None
        except (TimeoutError, socket.timeout):
            raise CheckError("DeepSeek 请求超时，请稍后重试") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise CheckError("DeepSeek 请求超时，请稍后重试") from None
            raise CheckError("无法连接 DeepSeek，请检查网络后重试") from None
        except Exception:
            raise CheckError("DeepSeek 请求失败，请检查接口配置后重试") from None

        if len(raw_response) > MAX_RESPONSE_BYTES:
            raise CheckError("DeepSeek 响应超过长度限制")
        try:
            data = json.loads(raw_response.decode("utf-8"))
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise CheckError("DeepSeek 输出未完整返回，请重试")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise CheckError("DeepSeek 未返回有效的风险分析")
            return content
        except CheckError:
            raise
        except Exception:
            raise CheckError("DeepSeek 响应格式无效，请稍后重试") from None


MODEL_CLASS = DeepSeekAPI
