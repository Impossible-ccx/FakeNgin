"""基于 Ollama 本地 LLM（qwen2.5:7b）的谣言检测模型。

ollama 为可选依赖：运行环境未安装或模型不存在时，detect() 返回 False，
该模型会被工厂过滤；check() 失败会重试，最终抛出 CheckError。
"""

from .ollama_base import OllamaModel

MODEL_NAME = "qwen2.5:7b"


class Ollama_Qwen25(OllamaModel):
    name = "qwen2.5_7b"
    display_name = "Qwen2.5-7B (Ollama)"
    description = "评估消息的语言、来源描述与传播风险，返回风险评分和理由。"
    model_name = MODEL_NAME
    temperature = 0


MODEL_CLASS = Ollama_Qwen25
