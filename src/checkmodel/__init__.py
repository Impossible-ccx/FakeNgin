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
_source_instances = {}
_source_metadata = {}
_loaded = False

_SOURCE_SPECS = {
    "deepseek_r1": (
        {"source": "cloud", "source_label": "云端 API", "module": "deepseek_api",
         "display_name": "DeepSeek（云端 API）", "description": "使用已配置的 DeepSeek 云端 API。"},
        {"source": "local", "source_label": "本地 Ollama", "module": "ollama_deepseek",
         "display_name": "DeepSeek-R1 (Ollama)", "description": "使用本地 Ollama 的 DeepSeek-R1 模型。"},
    ),
}
_SOURCE_BY_MODULE = {
    spec["module"]: (model_id, spec)
    for model_id, specs in _SOURCE_SPECS.items() for spec in specs
}


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
    _instances.clear()
    _available.clear()
    _source_instances.clear()
    _source_metadata.clear()
    for module_name in MODEL_MODULES:
        source_info = _SOURCE_BY_MODULE.get(module_name)
        model_class = None
        instance = None
        try:
            model_class = _load_module_class(module_name)
            # 双来源分别探测和保存，但默认模型列表仍只保留一个逻辑席位。
            if model_class.name in _instances and source_info is None:
                continue
            instance = model_class()
            available = bool(instance.detect())
            if(available):
                instance.initialize()
        except Exception:
            print("模型模块 {} 加载失败，请检查配置".format(module_name), file=sys.stderr)
            available = False
        if source_info is not None:
            model_id, spec = source_info
            metadata = {key: value for key, value in spec.items() if key != "module"}
            metadata["available"] = available
            if instance is not None:
                metadata.update(display_name=instance.display_name, description=instance.description)
            _source_metadata[(model_id, spec["source"])] = metadata
            if available:
                instance.source = spec["source"]
                instance.source_label = spec["source_label"]
                _source_instances[(model_id, spec["source"])] = instance
        if available:
            _instances.setdefault(model_class.name, instance)
            _available[model_class.name] = True
    # 显式来源永不回退；只有未指定来源的旧调用使用此默认优先次序。
    for model_id, specs in _SOURCE_SPECS.items():
        for spec in specs:
            instance = _source_instances.get((model_id, spec["source"]))
            if instance is not None:
                _instances[model_id] = instance
                _available[model_id] = True
                break
    _loaded = True


def get_models():
    """返回全部可用模型的元信息。"""
    _ensure_loaded()
    return [
        {"id": model_id, "display_name": instance.display_name, "description": instance.description}
        for model_id, instance in _instances.items()
        if _available.get(model_id)
    ]


def get_model_sources(model_id):
    """返回逻辑模型所有来源的名称和可用性，不增加投票席位。"""
    _ensure_loaded()
    return [
        dict(_source_metadata.get(
            (model_id, spec["source"]),
            {**{key: value for key, value in spec.items() if key != "module"}, "available": False},
        ))
        for spec in _SOURCE_SPECS.get(model_id, ())
    ]


def get_model(model_id, source=None) -> base.CheckModel:
    """按逻辑 ID 和可选来源取模型；显式指定的来源不可用时不回退。"""
    _ensure_loaded()
    if source is not None:
        if model_id not in _SOURCE_SPECS or source not in ("cloud", "local"):
            raise KeyError((model_id, source))
        instance = _source_instances.get((model_id, source))
        if instance is None:
            raise KeyError((model_id, source))
        return instance
    if model_id not in _instances:
        raise KeyError(model_id)
    return _instances[model_id]
