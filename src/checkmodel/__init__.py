"""谣言检测模型工厂。

手动维护 MODEL_MODULES 列表以登记模型；首次加载时对每个模型执行
实例化 -> detect() -> initialize()，仅保留可用模型。
"""

import importlib
from . import base

MODEL_MODULES = [
    "template_model",
    "ollama_qwen25",
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
            instance = model_class()
            available = bool(instance.detect())
            if(available):
                instance.initialize()
        except Exception:
            available = False
            instance = None
        if available:
            _instances[model_class.name] = instance
            _available[model_class.name] = True
    _loaded = True


def get_models():
    """返回全部可用模型的元信息。"""
    _ensure_loaded()
    models = []
    for module_name in MODEL_MODULES:
        try:
            model_class = _load_module_class(module_name)
        except Exception:
            continue
        if _available.get(model_class.name):
            models.append({
                "id": model_class.name,
                "display_name": model_class.display_name,
                "description": model_class.description,
            })
    return models


def get_model(model_id) -> base.CheckModel:
    """按 id 获取可用模型实例，不存在或不可用时抛出 KeyError。"""
    _ensure_loaded()
    if model_id not in _instances:
        raise KeyError(model_id)
    return _instances[model_id]
