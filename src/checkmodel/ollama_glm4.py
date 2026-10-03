"""基于 Ollama 本地 LLM（glm4:9b）的谣言检测模型。

ollama 为可选依赖：运行环境未安装或模型不存在时，detect() 返回 False，
该模型会被工厂过滤；check() 失败会重试，最终抛出 CheckError。
"""

from .ollama_base import OllamaModel

MODEL_NAME = "glm4:9b"


class Ollama_GLM4(OllamaModel):
    name = "glm4_9b"
    display_name = "GLM-4-9B (Ollama)"
    description = "按相同风险标准独立评估消息，返回风险评分和理由。"
    model_name = MODEL_NAME
    temperature = 0


MODEL_CLASS = Ollama_GLM4
