"""Web model choices follow available local risk models without provider branding."""

from checkmodel.ensemble import RISK_MODELS, get_risk_models


_MODEL_NUMBERS = {model["id"]: index for index, model in enumerate(RISK_MODELS, 1)}


def model_label(member, index=None):
    """Render a stable generic label while retaining original IDs in stored reports."""
    model_id = member.get("id") if isinstance(member, dict) else member
    number = _MODEL_NUMBERS.get(model_id) if isinstance(model_id, str) else None
    number = number or index
    return "分析模型 {}".format(number) if number else "分析模型"


def list_web_models():
    """Only ready local risk models are offered; unavailable entries stay out of the UI."""
    return [
        {"id": model["id"], "display_name": model_label(model),
         "description": "独立分析消息内容，给出风险评分和判断理由。", "available": True}
        for model in get_risk_models("local") if model["available"]
    ]


def validate_web_source(source=None):
    """Cloud configuration is absent from the website; omitted source always means local."""
    if source not in (None, "local"):
        raise ValueError("请选择当前网站支持的本地检测服务")
    return "local"
