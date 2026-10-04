"""基于 Ollama 本地 LLM（qwen2.5:7b）的谣言检测模型。

ollama 为可选依赖：运行环境未安装或模型不存在时，detect() 返回 False，
该模型会被工厂过滤；check() 失败会重试，最终抛出 CheckError。
"""

from .ollama_base import OllamaModel

MODEL_NAME = "qwen2.5:7b"


class Ollama_Qwen25(OllamaModel):
    name = "qwen2.5_7b"
    display_name = "Qwen2.5-7B (Ollama)"
    description = "本地 Ollama 大模型 qwen2.5:7b，根据消息内容给出虚假概率与理由。"
    model_name = MODEL_NAME
    temperature = 0.5


MODEL_CLASS = Ollama_Qwen25
