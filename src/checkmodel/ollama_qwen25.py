"""基于 Ollama 本地 LLM（qwen2.5:7b）的谣言检测模型。

ollama 为可选依赖：运行环境未安装或模型不存在时，detect() 返回 False，
该模型会被工厂过滤；check() 失败会重试，最终抛出 CheckError。
"""

import json
import os

from .base import CheckError, CheckModel

MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
REQUEST_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))
MAX_ATTEMPTS = 2

SYSTEM_PROMPT = (
    "你是一名严谨的谣言检测助手。请评估给定消息为谣言（虚假信息）的可能性，"
    "并且只输出 JSON。"
)


class Ollama_Qwen25(CheckModel):
    name = "qwen2.5_7b"
    display_name = "Qwen2.5-7B (Ollama)"
    description = "本地 Ollama 大模型 qwen2.5:7b，根据消息内容给出虚假概率与理由。"
    model_name = MODEL_NAME

    def __init__(self):
        self.display_name = MODEL_NAME + " (Ollama)"
        self.description = "本地 Ollama 模型，根据消息内容给出风险评分与理由。"
        self._unavailable_reason = ""

    def initialize(self):
        self.model_name = MODEL_NAME

    def unavailable_reason(self):
        return self._unavailable_reason or super().unavailable_reason()

    def detect(self):
        try:
            import ollama
        except ImportError:
            self._unavailable_reason = "未安装 ollama Python 包"
            return False
        try:
            client = self._client(ollama)
            names = self._installed_models(client)
        except Exception as exc:
            self._unavailable_reason = "无法连接 Ollama 服务（{}）".format(
                type(exc).__name__)
            return False
        if not any(name == self.model_name for name in names):
            self._unavailable_reason = "Ollama 服务上未找到模型 {}".format(self.model_name)
            return False
        return True

    def check(self, message):
        try:
            import ollama
        except ImportError:
            raise CheckError("未安装 ollama，无法使用该模型")

        client = self._client(ollama)
        for _ in range(MAX_ATTEMPTS):
            try:
                return self._request(client, message)
            except Exception:
                continue
        raise CheckError("模型请求失败，请稍后重试")

    # ---------------------------------------------------------- 内部

    def _client(self, ollama):
        return ollama.Client(timeout=REQUEST_TIMEOUT)

    def _installed_models(self, client):
        response = client.list()
        models = getattr(response, "models", None)
        if models is None and isinstance(response, dict):
            models = response.get("models", [])

        names = []
        for item in models or []:
            if isinstance(item, dict):
                name = item.get("model") or item.get("name")
            else:
                name = getattr(item, "model", None) or getattr(item, "name", None)
            if name:
                names.append(name)
        return names

    def _request(self, client, message):
        prompt = (
            "请判断下面这条消息为谣言（虚假信息）的可能性，给出 0 到 100 的整数虚假概率"
            "（越接近 100 表示越可能是谣言），并给出简要中文理由。\n\n"
            "消息：\n" + message + "\n\n"
            '只输出 JSON，格式为：{"probability": <0-100 的整数>, "reason": "<简要理由>"}'
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = client.chat(
            model=self.model_name,
            messages=messages,
            format="json",
            options={"temperature": 0},
        )
        data = json.loads(self._extract_content(response))
        probability = max(0.0, min(100.0, float(data["probability"])))
        reason = str(data.get("reason", "")).strip()
        return probability, reason

    def _extract_content(self, response):
        message = getattr(response, "message", None)
        if message is None and isinstance(response, dict):
            message = response.get("message")
        if isinstance(message, dict):
            return message.get("content", "")
        return getattr(message, "content", "")


MODEL_CLASS = Ollama_Qwen25
