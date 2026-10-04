"""自动风险检测：单模型分析和等权等级投票，不写入真假标签或人工队列。

检测方式除「多模型投票 / 单模型分析」外，还提供「分类器检测」：
使用 score_kind 为 probability 的分类模型输出虚假概率（与语言风险分含义不同）。
"""

from time import perf_counter

from flask import render_template, request

import checkmodel
from checkmodel.base import CheckError
from checkmodel.ensemble import (
    MAX_MESSAGE_LENGTH, get_risk_models, run_risk_check,
)

from . import main

WARNING_HIGH = 70
WARNING_MEDIUM = 40


def warning_for(percentage):
    """按虚假概率给出警告等级（用于分类器检测模式）。"""
    if percentage >= WARNING_HIGH:
        return "high", "高度疑似虚假信息，请谨慎对待并核实来源。"
    if percentage >= WARNING_MEDIUM:
        return "medium", "存在虚假可能，建议进一步核实。"
    return "low", "暂未发现明显的虚假特征。"


def get_classifier_models():
    """列出全部可用的真假分类模型（score_kind 为 probability）。"""
    return [
        {
            "id": model["id"],
            "display_name": model["display_name"],
            "description": model["description"],
            "available": model["available"],
        }
        for model in checkmodel.get_registered_models()
        if model.get("score_kind") == "probability" and model["available"]
    ]


def _run_classifier(message, model_id, classifier_models):
    """运行真假分类器，返回结果 dict；无效输入抛 ValueError。"""
    if not message:
        raise ValueError("请输入消息内容")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError("消息内容不能超过 {} 个字符".format(MAX_MESSAGE_LENGTH))
    if model_id not in {model["id"] for model in classifier_models}:
        raise ValueError("请选择可用的分类模型")

    started = perf_counter()
    try:
        probability, extra_info = checkmodel.get_model(model_id).check(message)
    except CheckError as exc:
        raise ValueError(str(exc))
    except Exception:
        raise ValueError("检测失败，请稍后重试")

    percentage = max(0, min(100, int(round(float(probability)))))
    level, warning = warning_for(percentage)
    label = next(m["display_name"] for m in classifier_models if m["id"] == model_id)
    return {
        "mode": "classifier",
        "percentage": percentage,
        "level": level,
        "warning": warning,
        "extra_info": extra_info,
        "model_label": label,
        "elapsed_seconds": round(perf_counter() - started, 3),
    }


def _context(form=None):
    models = get_risk_models()
    classifier_models = get_classifier_models()
    context = {
        "models": models,
        "classifier_models": classifier_models,
        "mode": "vote",
        "selected_ids": [model["id"] for model in models],
        "classifier_selected": classifier_models[0]["id"] if classifier_models else "",
        "message": "",
        "result": None,
        "error": None,
        "max_message_length": MAX_MESSAGE_LENGTH,
    }
    if form is None:
        return context

    message = form.get("message", "").strip()
    mode = form.get("mode", "vote")
    selected_ids = form.getlist("models")
    classifier_selected = form.get("classifier_model", context["classifier_selected"])
    context.update(
        message=message,
        mode=mode,
        selected_ids=selected_ids,
        classifier_selected=classifier_selected,
    )
    try:
        if mode == "classifier":
            context["result"] = _run_classifier(message, classifier_selected, classifier_models)
        else:
            context["result"] = run_risk_check(message, selected_ids, mode=mode)
    except ValueError as exc:
        context["error"] = str(exc)
    return context


@main.route("/detect", methods=["GET", "POST"])
def detect():
    context = _context(request.form if request.method == "POST" else None)
    return render_template("detect.html", **context)


@main.route("/detect/check", methods=["POST"])
def detect_check():
    """返回结果片段；无 JavaScript 的表单可回退到 /detect。"""
    return render_template("_detect_result.html", **_context(request.form))
