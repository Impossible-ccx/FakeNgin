"""Ollama 系模型的公共实现。

新增 Ollama 模型时继承 OllamaModel，设置 name / display_name / description /
model_name 即可；可按需覆盖 prompt、system_prompt、temperature、timeout，
或重写 _clean_content 清理模型特有的输出包装。
"""

import json
import re

from .base import CheckError, CheckModel

DEFAULT_TIMEOUT = 60
MAX_ATTEMPTS = 2

DEFAULT_SYSTEM_PROMPT = (
    "你是一名严谨的谣言检测助手。请评估给定消息为谣言（虚假信息）的可能性，"
    "并且只输出 JSON。"
)

DEFAULT_PROMPT = (
    "请判断下面这条消息为谣言（虚假信息）的可能性，给出 0 到 100 的整数虚假概率"
    "谣言的可能性应该取决于其语言特征，如是否骇人听闻、是否诉诸专家等，而不是从事实层面分析"
    "也就是说，你实际上关心的是消息是谣言的风险，而不是实际上其是否真实。高风险信息会送校验程序"
    "对于具有谣言风险的消息，例如通知、科普等，大胆给出高风险预测。低风险预测更适合那些没有"
    "强烈情绪输出、信息输出的消息"
    "（越接近 100 表示越可能是谣言），并给出简要中文理由。对于输出的个位数，尽量保证在0-9间均匀分布"
    "，避免都是整5、整10分数\n\n"
    "消息：\n{message}\n\n"
    '只输出 JSON，格式为：{{"probability": <0-100 的整数>, "reason": "<简要理由>"}}'
)


class OllamaModel(CheckModel):
    model_name = ""
    timeout = DEFAULT_TIMEOUT
    temperature = 0.5
    system_prompt = DEFAULT_SYSTEM_PROMPT
    prompt = DEFAULT_PROMPT

    def detect(self):
        try:
            import ollama
        except ImportError:
            return False
        try:
            client = self._client(ollama)
            names = self._installed_models(client)
        except Exception:
            return False
        return any(name == self.model_name for name in names)

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
        return ollama.Client(timeout=self.timeout)

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
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({
            "role": "user",
            "content": self.prompt.format(message=message),
        })
        response = client.chat(
            model=self.model_name,
            messages=messages,
            format="json",
            options={"temperature": self.temperature},
        )
        data = json.loads(self._parse_content(self._extract_content(response)))
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

    def _clean_content(self, content):
        """子类可重写，用于清理模型特有的输出包装（如思考段）。"""
        return str(content)

    def _parse_content(self, content):
        """清洗后宽容提取 JSON 对象，容忍包在代码块或多余文字中的情况。"""
        text = self._clean_content(content)
        match = re.search(r"\{.*\}", text, flags=re.S)
        return match.group(0) if match else text
