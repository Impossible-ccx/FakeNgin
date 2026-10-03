"""谣言检测模型工厂。

手动维护 MODEL_MODULES 列表以登记模型；首次加载时对每个模型执行
实例化 -> detect() -> initialize()，仅保留可用模型。
"""

import importlib
import sys

from . import base

MODEL_MODULES = [
    "template_model",
    "ollama_qwen25",
    "deepseek_api",
    "ollama_deepseek",
    "ollama_glm4",
    "roberta_classifier",
]

_instances = {}
_available = {}
_loaded = False


def _load_module_class(module_name):
    module = importlib.import_module("." + module_name, __name__)
    model_class = getattr(module, "MODEL_CLASS", None)
    if model_class is None:
        raise ValueError("模型文件 {} 未定义 MODEL_CLASS".format(module_name))
    return model_class


def _ensure_loaded():
    """加载全部模型并执行 initialize + detect，仅登记可用模型。"""
    global _loaded
    if _loaded:
        return
    for module_name in MODEL_MODULES:
        try:
            model_class = _load_module_class(module_name)
            # 同一逻辑 ID 只登记首个可用实现：配置云端时不再增加一张本地票。
            if model_class.name in _instances:
                continue
            instance = model_class()
            available = bool(instance.detect())
            if(available):
                instance.initialize()
        except Exception:
            print("模型模块 {} 加载失败，请检查配置".format(module_name), file=sys.stderr)
            available = False
            instance = None
        if available:
            _instances[model_class.name] = instance
            _available[model_class.name] = True
    _loaded = True


def get_models():
    """返回全部可用模型的元信息。"""
    _ensure_loaded()
    return [
        {"id": model_id, "display_name": instance.display_name, "description": instance.description}
        for model_id, instance in _instances.items()
        if _available.get(model_id)
    ]


def get_model(model_id) -> base.CheckModel:
    """按 id 获取可用模型实例，不存在或不可用时抛出 KeyError。"""
    _ensure_loaded()
    if model_id not in _instances:
        raise KeyError(model_id)
    return _instances[model_id]
