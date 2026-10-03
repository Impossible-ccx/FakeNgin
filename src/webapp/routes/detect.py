"""自动风险检测、真实逐模型进度，以及完整检测报告的保存。"""

import json

from flask import Response, current_app, jsonify, render_template, request, stream_with_context, url_for

from checkmodel.ensemble import (
    DEEPSEEK_SOURCES, MAX_MESSAGE_LENGTH, RISK_MODELS, get_risk_models, iter_risk_check,
    run_risk_check, validate_risk_request,
)
from .. import reports
from . import main


def _save_result(message, model_ids, result):
    try:
        return reports.save_report(message, model_ids, result), None
    except Exception:
        current_app.logger.warning("Failed to save risk report")
        return None, "检测已完成，但历史记录保存失败。当前结果仍可查看，请稍后重试。"


def _context(form=None):
    requested_source = form.get("deepseek_source") if form is not None else None
    source_error = requested_source is not None and requested_source not in DEEPSEEK_SOURCES
    models = get_risk_models(None if source_error else requested_source)
    deepseek = next(model for model in models if model["id"] == "deepseek_r1")
    context = {
        "models": models,
        "mode": "vote",
        "selected_ids": [model["id"] for model in RISK_MODELS],
        "message": "",
        "result": None,
        "error": None,
        "record": None,
        "history_error": None,
        "max_message_length": MAX_MESSAGE_LENGTH,
        "deepseek_source": deepseek["source"],
        "response_status": 200,
    }
    if form is None:
        return context

    message = form.get("message", "").strip()
    mode = form.get("mode", "vote")
    selected_ids = form.getlist("models")
    context.update(message=message, mode=mode, selected_ids=selected_ids)
    if source_error:
        context.update(error="请选择有效的 DeepSeek 来源：云端 API 或本地 Ollama", response_status=400)
        return context
    try:
        context["result"] = run_risk_check(message, selected_ids, mode=mode, deepseek_source=requested_source)
    except ValueError as exc:
        context["error"] = str(exc)
    else:
        context["record"], context["history_error"] = _save_result(
            message, selected_ids, context["result"],
        )
    return context


@main.route("/detect", methods=["GET", "POST"])
def detect():
    context = _context(request.form if request.method == "POST" else None)
    return render_template("detect.html", **context), context["response_status"]


@main.route("/detect/check", methods=["POST"])
def detect_check():
    """返回结果片段；无 JavaScript 的表单可回退到 /detect。"""
    context = _context(request.form)
    return render_template("_detect_result.html", **context), context["response_status"]


@main.route("/detect/stream", methods=["POST"])
def detect_stream():
    """按真实推理完成情况发送 NDJSON；输入错误在开始流式响应前返回。"""
    mode = request.form.get("mode", "vote")
    deepseek_source = request.form.get("deepseek_source")
    try:
        message, model_ids = validate_risk_request(
            request.form.get("message", ""), request.form.getlist("models"), mode, deepseek_source,
        )
    except ValueError as exc:
        return jsonify(type="error", message=str(exc)), 400

    def events():
        try:
            for event in iter_risk_check(message, model_ids, mode, deepseek_source):
                if event["type"] == "complete":
                    result = event["result"]
                    record, history_error = _save_result(message, model_ids, result)
                    html = render_template(
                        "_detect_result.html", result=result, record=record,
                        error=None, history_error=history_error, message=message,
                        mode=mode, selected_ids=model_ids, deepseek_source=deepseek_source,
                    )
                    event = {
                        "type": "complete",
                        "html": html,
                        "record_id": record["id"] if record else None,
                        "history_url": url_for("main.report", report_id=record["id"]) if record else None,
                        "history_error": history_error,
                    }
                yield json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        except GeneratorExit:
            # 浏览器关闭连接时不继续模型调用，也不伪造完成或保存部分结果。
            raise
        except Exception:
            current_app.logger.warning("Risk detection stream interrupted")
            yield json.dumps({
                "type": "error",
                "message": "检测连接中断，请稍后重试。",
            }, ensure_ascii=False, allow_nan=False) + "\n"

    return Response(
        stream_with_context(events()),
        content_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )
