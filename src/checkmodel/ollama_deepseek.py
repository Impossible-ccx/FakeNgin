"""基于 Ollama 本地 LLM（deepseek-r1）的谣言检测模型。

ollama 为可选依赖：运行环境未安装或模型不存在时，detect() 返回 False，
该模型会被工厂过滤；check() 失败会重试，最终抛出 CheckError。
更换参数规模只需修改 MODEL_NAME（如 deepseek-r1:14b）。
"""

import json
import re

from .base import CheckError, CheckModel

MODEL_NAME = "deepseek-r1:7b"
REQUEST_TIMEOUT = 120  # R1 会先生成推理链，耗时高于普通模型，超时放宽
MAX_ATTEMPTS = 2

# DeepSeek-R1 官方建议不使用 system 提示，所有指令放在用户消息中。
PROMPT_TEMPLATE = (
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


class Ollama_DeepSeek(CheckModel):
    name = "deepseek_r1"
    display_name = "DeepSeek-R1 (Ollama)"
    description = "本地 Ollama 大模型 deepseek-r1，根据消息内容给出虚假概率与理由。"
    model_name = MODEL_NAME

    def initialize(self):
        self.model_name = MODEL_NAME

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
        response = client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(message=message)}],
            format="json",
            options={"temperature": 0.6},
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

    def _parse_content(self, content):
        """剔除 R1 可能残留在正文中的思考段，再提取 JSON 对象。"""
        text = str(content)
        text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S)
        # 少数 ollama 版本会把 R1 的思考结束标记直接留在正文中
        text = text.split("<｜end▁of▁thinking｜>")[-1]
        match = re.search(r"\{.*\}", text, flags=re.S)
        return match.group(0) if match else text


MODEL_CLASS = Ollama_DeepSeek
