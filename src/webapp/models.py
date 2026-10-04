"""网页中的稳定模型名称，不改变实际推理和报告标识。"""

from checkmodel.ensemble import RISK_MODELS, get_risk_models

_NUMBERS = {model["id"]: index for index, model in enumerate(RISK_MODELS, 1)}


def model_label(member, index=None):
    model_id = member.get("id") if isinstance(member, dict) else member
    number = _NUMBERS.get(model_id, index)
    return "分析模型 {}".format(number) if number is not None else "分析模型"


def list_web_models():
    return [{**model, "display_name": model_label(model["id"]),
             "description": "独立分析消息，给出风险评分和判断理由。"}
            for model in get_risk_models()]


def validate_web_source(source=None):
    if source not in (None, "local"):
        raise ValueError("请选择当前网站支持的本地检测服务")
    return "local"
