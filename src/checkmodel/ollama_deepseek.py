"""基于 Ollama 本地 LLM（deepseek-r1）的谣言检测模型。

ollama 为可选依赖：运行环境未安装或模型不存在时，detect() 返回 False，
该模型会被工厂过滤；check() 失败会重试，最终抛出 CheckError。
更换参数规模只需修改 MODEL_NAME（如 deepseek-r1:14b）。
"""

import re

from .ollama_base import OllamaModel

MODEL_NAME = "deepseek-r1:7b"
REQUEST_TIMEOUT = 120  # R1 会先生成推理链，耗时高于普通模型


class Ollama_DeepSeek(OllamaModel):
    name = "deepseek_r1"
    display_name = "DeepSeek-R1 (Ollama)"
    description = "按相同风险标准分析消息，返回风险评分和简短理由。"
    model_name = MODEL_NAME
    timeout = REQUEST_TIMEOUT
    temperature = 0.6
    # DeepSeek-R1 官方建议不使用 system 提示，所有指令放在用户消息中。
    system_prompt = ""

    def _clean_content(self, content):
        """剔除 R1 可能残留在正文中的思考段。"""
        text = str(content)
        text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S)
        # 少数 ollama 版本会把 R1 的思考结束标记直接留在正文中
        return text.split("<｜end▁of▁thinking｜>")[-1]


MODEL_CLASS = Ollama_DeepSeek
