"""自动风险检测：单模型分析和等权等级投票，不写入真假标签或人工队列。"""

from flask import current_app, render_template, request

from checkmodel.ensemble import (
    MAX_MESSAGE_LENGTH, get_risk_models, run_risk_check,
)
from . import main
from .. import reports


def _save_completed(context):
    context.update(record=None, history_error=None)
    if context.get("result") is not None:
        try:
            context["record"] = reports.save_report(
                context["message"], context["selected_ids"], context["result"],
            )
        except Exception:
            current_app.logger.warning("Failed to save risk report")
            context["history_error"] = "检测已完成，但历史记录保存失败。当前结果仍可查看，请稍后重试。"
    return context


def _context(form=None):
    models = get_risk_models()
    selected_ids = [model["id"] for model in models]
    context = {
        "models": models,
        "mode": "vote" if len(selected_ids) > 1 else "single",
        "selected_ids": selected_ids,
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
    context.update(message=message, mode=mode, selected_ids=selected_ids)
    try:
        context["result"] = run_risk_check(message, selected_ids, mode=mode)
    except ValueError as exc:
        context["error"] = str(exc)
    return context


@main.route("/detect", methods=["GET", "POST"])
def detect():
    context = _save_completed(_context(request.form if request.method == "POST" else None))
    return render_template("detect.html", **context)


@main.route("/detect/check", methods=["POST"])
def detect_check():
    """返回结果片段；无 JavaScript 的表单可回退到 /detect。"""
    return render_template("_detect_result.html", **_save_completed(_context(request.form)))
