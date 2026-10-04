"""网页中的稳定模型名称，不改变实际推理和报告标识。"""

import checkmodel
from checkmodel.ensemble import get_registered_risk_models, get_risk_models


def model_label(member, index=None):
    model_id = member.get("id") if isinstance(member, dict) else member
    numbers = {model["id"]: position for position, model in enumerate(get_registered_risk_models(), 1)}
    number = numbers.get(model_id, index)
    return "分析模型 {}".format(number) if number is not None else "分析模型"


def _web_metadata(models):
    return [{**model, "display_name": model_label(model["id"]),
             "description": "独立分析消息，给出风险评分和判断理由。",
             "needs_probe": model.get("available", True) is None}
            for model in models]


def list_web_models(cached_only=False, refresh=False):
    if cached_only:
        return list_cached_web_models()
    if refresh:
        return refresh_web_models()
    return _web_metadata(get_risk_models())


def list_cached_web_models():
    """用于普通页面渲染，不连接本地模型服务，也不读取模型权重。"""
    risk_ids = [model["id"] for model in get_registered_risk_models()]
    cached = checkmodel.get_cached_models(model_ids=risk_ids)
    return _web_metadata([
        model for model in cached
        if model.get("available") is not False and model.get("score_kind", "risk") == "risk"
    ])


def refresh_web_models():
    """仅在用户请求检测连接时刷新风险模型名单，分类器不参与探测。"""
    return _web_metadata(get_risk_models(refresh=True))


def validate_web_source(source=None):
    if source not in (None, "local"):
        raise ValueError("请选择当前网站支持的本地检测服务")
    return "local"
