"""模型登记、短期可用性缓存与按需初始化。

查询名单只执行轻量 detect()，不会加载权重。只有 get_model() 请求的
模型才执行 initialize()；失败探测在短期缓存到期或主动刷新后可恢复。
"""

import importlib
import logging
from threading import RLock
from time import monotonic

from . import base

MODEL_MODULES = [
    "template_model",
    "ollama_qwen25",
    "ollama_deepseek",
    "ollama_glm4",
    "roberta_classifier",
]

PROBE_CACHE_SECONDS = 5.0
logger = logging.getLogger(__name__)
_catalog_lock = RLock()
_states = {}
_module_errors = {}
# 仅保存初始化成功的实例；探测刷新不会清除此缓存。
_instances = {}


class _ModelState:
    def __init__(self, model_class):
        self.model_class = model_class
        self.lock = RLock()
        self.instance = None
        self.available = None
        self.checked_at = None
        self.initialized = False

    def metadata(self):
        return {
            "id": self.model_class.name,
            "display_name": self.model_class.display_name,
            "description": self.model_class.description,
            "score_kind": getattr(self.model_class, "score_kind", "probability"),
            "available": self.available,
        }


def _load_module_class(module_name):
    module = importlib.import_module("." + module_name, __name__)
    model_class = getattr(module, "MODEL_CLASS", None)
    if model_class is None:
        raise ValueError("模型文件 {} 未定义 MODEL_CLASS".format(module_name))
    return model_class


def _registered_states(refresh=False):
    """模块导入只读取类元信息，登记失败同样可以重试。"""
    with _catalog_lock:
        result = []
        for module_name in MODEL_MODULES:
            state = _states.get(module_name)
            if state is None:
                failed_at = _module_errors.get(module_name)
                if (not refresh and failed_at is not None
                        and monotonic() - failed_at < PROBE_CACHE_SECONDS):
                    continue
                try:
                    state = _ModelState(_load_module_class(module_name))
                except Exception as exc:
                    _module_errors[module_name] = monotonic()
                    logger.warning("模型模块 %s 登记失败：%s", module_name, type(exc).__name__)
                    continue
                _states[module_name] = state
                _module_errors.pop(module_name, None)
            result.append(state)
        return result


def _selected_states(model_ids=None, refresh=False):
    states = _registered_states(refresh=refresh)
    if model_ids is None:
        return states
    selected = set(model_ids)
    return [state for state in states if state.model_class.name in selected]


def _probe_locked(state, refresh_started=None):
    now = monotonic()
    if (state.checked_at is not None
            and now - state.checked_at < PROBE_CACHE_SECONDS
            and (refresh_started is None or state.checked_at >= refresh_started)):
        return state.available
    try:
        if state.instance is None:
            state.instance = state.model_class()
        refresh_detection = getattr(state.instance, "refresh_detection", None)
        if refresh_started is not None and callable(refresh_detection):
            available = bool(refresh_detection(refresh_started))
        else:
            available = bool(state.instance.detect())
    except Exception as exc:
        logger.warning("模型 %s 探测失败：%s", state.model_class.name, type(exc).__name__)
        available = False
    state.available = available
    state.checked_at = monotonic()
    return available


def get_cached_models(model_ids=None):
    """立即返回元信息快照；available=None 表示尚未探测，不等待探测锁。"""
    return [state.metadata() for state in _selected_states(model_ids)]


def get_registered_models(score_kind=None, refresh=False):
    """按 MODEL_MODULES 顺序返回登记元信息，不探测、不初始化模型。

    登记和可用性分开：本地服务暂时离线的模型仍然是合法模型。
    调用方可以按评分协议筛选候选，不需要再维护另一套模型名单。
    """
    models = []
    for state in _registered_states(refresh=refresh):
        metadata = state.metadata()
        metadata.pop("available", None)
        if score_kind is None or metadata["score_kind"] == score_kind:
            models.append(metadata)
    return models


def get_models(model_ids=None, refresh=False):
    """返回可用模型名单，不初始化权重；可筛选模型或主动刷新探测。"""
    refresh_started = monotonic() if refresh else None
    models = []
    for state in _selected_states(model_ids, refresh=refresh):
        with state.lock:
            if _probe_locked(state, refresh_started=refresh_started):
                models.append(state.metadata())
    return models


def get_model(model_id) -> base.CheckModel:
    """仅初始化所选模型；并发共享成功实例，失败在缓存到期后可重试。"""
    states = _selected_states([model_id])
    if not states:
        raise KeyError(model_id)
    state = states[0]
    with state.lock:
        if not _probe_locked(state):
            raise KeyError(model_id)
        if state.initialized:
            return state.instance
        try:
            state.instance.initialize()
        except Exception as exc:
            logger.warning("模型 %s 初始化失败：%s", model_id, type(exc).__name__)
            state.available = False
            state.checked_at = monotonic()
            # 不复用部分初始化对象；下次重新探测后构造干净实例。
            state.instance = None
            raise KeyError(model_id) from None
        state.initialized = True
        _instances[model_id] = state.instance
        return state.instance
