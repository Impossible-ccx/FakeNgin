"""谣言检测模型工厂。

手动维护 MODEL_MODULES 列表以登记模型；对每个模型文件，导入其
MODEL_CLASS 并以 name 注册。模型实例在首次使用时惰性创建并调用
initialize()。
"""

import importlib

MODEL_MODULES = [
    "template_model"
]

_instances = {}


def _load_module_class(module_name):
    module = importlib.import_module("." + module_name, __name__)
    model_class = getattr(module, "MODEL_CLASS", None)
    if model_class is None:
        raise ValueError("模型文件 {} 未定义 MODEL_CLASS".format(module_name))
    return model_class


def get_models():
    """返回全部模型的元信息（不实例化、不初始化）。"""
    models = []
    for module_name in MODEL_MODULES:
        model_class = _load_module_class(module_name)
        models.append({
            "id": model_class.name,
            "display_name": model_class.display_name,
            "description": model_class.description,
        })
    return models


def get_model(model_id):
    """按 id 获取模型实例，首次调用时初始化并缓存。"""
    if model_id in _instances:
        return _instances[model_id]

    for module_name in MODEL_MODULES:
        model_class = _load_module_class(module_name)
        if model_class.name == model_id:
            instance = model_class()
            instance.initialize()
            _instances[model_id] = instance
            return instance

    raise KeyError(model_id)
