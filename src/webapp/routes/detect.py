"""自动风险检测：单模型分析和等权等级投票，不写入真假标签或人工队列。"""

import json

from flask import Response, current_app, jsonify, render_template, request, stream_with_context, url_for

from checkmodel.ensemble import (
    MAX_MESSAGE_LENGTH, iter_risk_check, run_risk_check, validate_risk_request,
)
from . import main
from .. import reports
from ..models import list_web_models, model_label


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
    models = list_web_models()
    selected_ids = [model["id"] for model in models][:3]
    context = {
        "models": models,
        "model_labels": {model["id"]: model_label(model["id"]) for model in models},
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


@main.route("/detect/stream", methods=["POST"])
def detect_stream():
    """根据真实模型事件发送 NDJSON，连接中断时不继续调用下一模型。"""
    mode = request.form.get("mode", "vote")
    try:
        message, model_ids = validate_risk_request(
            request.form.get("message", ""), request.form.getlist("models"), mode,
        )
    except ValueError as exc:
        return jsonify(type="error", message=str(exc)), 400

    def events():
        checks = iter_risk_check(message, model_ids, mode)
        try:
            for event in checks:
                if "member" in event:
                    event = {**event, "member": {
                        **event["member"], "ui_display_name": model_label(event["member"]),
                    }}
                if event["type"] == "complete":
                    context = _save_completed(dict(
                        message=message, selected_ids=model_ids, mode=mode,
                        result=event["result"], error=None,
                    ))
                    record = context["record"]
                    event = {
                        "type": "complete", "html": render_template("_detect_result.html", **context),
                        "record_id": record["id"] if record else None,
                        "history_url": url_for("main.report", report_id=record["id"]) if record else None,
                        "history_error": context["history_error"],
                    }
                yield json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        except GeneratorExit:
            raise
        except Exception:
            current_app.logger.warning("Risk detection stream interrupted")
            yield json.dumps({"type": "error", "message": "检测连接中断，请稍后重试。"}, ensure_ascii=False) + "\n"
        finally:
            checks.close()

    return Response(
        stream_with_context(events()), content_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )
