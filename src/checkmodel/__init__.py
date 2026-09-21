"""谣言检测模型工厂。

手动维护 MODEL_MODULES 列表以登记模型；首次加载时对每个模型执行
实例化 -> detect() -> initialize()，仅保留可用模型。
"""

import importlib

import config  # 加载本地 .env；进程环境变量优先

MODEL_MODULES = [
    "compatible_api",
    "template_model",
    "ollama_qwen25",
]

_instances = {}
_available = {}
_status = []
_loaded = False


def _load_module_class(module_name):
    module = importlib.import_module("." + module_name, __name__)
    model_class = getattr(module, "MODEL_CLASS", None)
    if model_class is None:
        raise ValueError("模型文件 {} 未定义 MODEL_CLASS".format(module_name))
    return model_class


def _unavailable_reason(instance):
    try:
        reason = instance.unavailable_reason()
    except Exception:
        reason = ""
    return str(reason).strip() or "模型在当前环境不可用"


def _probe_all():
    """探测全部模型，记录实例、可用性与不可用原因。"""
    global _instances, _available, _status, _loaded
    instances = {}
    available = {}
    status = []
    for module_name in MODEL_MODULES:
        entry = {"module": module_name, "available": False, "reason": ""}
        try:
            model_class = _load_module_class(module_name)
            instance = model_class()
            if instance.detect():
                instance.initialize()
                instances[model_class.name] = instance
                available[model_class.name] = True
                entry["available"] = True
            else:
                entry["reason"] = _unavailable_reason(instance)
        except Exception as exc:
            entry["reason"] = "{}: {}".format(type(exc).__name__, exc)
        status.append(entry)
    _instances, _available, _status = instances, available, status
    _loaded = True


def _ensure_loaded():
    if not _loaded:
        _probe_all()


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
            instance = _instances[model_class.name]
            models.append({
                "id": model_class.name,
                "display_name": instance.display_name,
                "description": instance.description,
            })
    return models


def get_model_status():
    """返回每个登记模块的可用性与不可用原因，供界面状态提示。"""
    _ensure_loaded()
    return [dict(entry) for entry in _status]


def reprobe():
    """重新探测全部模型；模型服务恢复后无需重启应用。返回可用模型数。"""
    global _loaded
    _loaded = False
    _ensure_loaded()
    return len(_available)


def get_model(model_id):
    """按 id 获取可用模型实例，不存在或不可用时抛出 KeyError。"""
    _ensure_loaded()
    if model_id not in _instances:
        raise KeyError(model_id)
    return _instances[model_id]
