"""已完成检测报告的浏览、详情与 JSON / CSV / 文本导出。"""

import csv
import io
import json

from flask import Response, abort, current_app, render_template, request

from .. import reports
from . import main


PAGE_SIZE = 20
_DECISION_LABELS = {
    "majority": "多数投票",
    "mean_fallback": "平均分兜底",
    "single": "单模型检测",
    "unavailable": "暂无有效结果",
}


@main.route("/history")
def history():
    page = request.args.get("page", 1, type=int)
    query = request.args.get("q", "").strip()
    try:
        context = reports.list_reports(page=page, page_size=PAGE_SIZE, query=query)
    except Exception:
        current_app.logger.warning("Failed to read risk report history")
        return render_template(
            "history.html", records=[], page=1, total=0, total_pages=1,
            query=query, error="历史记录暂时无法读取，请稍后重试。",
        ), 503
    return render_template("history.html", **context)


def _find_report(report_id):
    try:
        record = reports.get_report(report_id)
    except Exception:
        current_app.logger.warning("Failed to read risk report")
        abort(503, description="历史记录暂时无法读取，请稍后重试。")
    if record is None:
        abort(404)
    return record


@main.route("/history/<report_id>")
def report(report_id):
    record = _find_report(report_id)
    return render_template(
        "report.html", record=record, result=record["result"],
        error=None, history_error=None, message=record["message"],
    )


def _csv_cell(value):
    """保留文字内容，并阻止电子表格把消息、理由等字段当成公式执行。"""
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.lstrip()
        if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def _csv_export(record):
    result = record["result"]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "report_id", "created_at_utc", "prompt_version", "message", "mode",
        "final_level", "final_label", "decision_method", "mean_score",
        "warning", "fallback_reason", "votes_low", "votes_medium", "votes_high", "has_failures",
        "selected_count", "success_count", "agreement_count", "majority_required",
        "total_elapsed_seconds", "model_id", "model_name", "status", "score",
        "level", "label", "reason", "error", "elapsed_seconds",
    ])
    common = [
        record["id"], record["created_at"], record["prompt_version"], record["message"],
        result["mode"], result["level"], result["label"], result["decision_method"],
        result["mean_score"], result["warning"], result["fallback_reason"],
        result["votes"]["low"], result["votes"]["medium"], result["votes"]["high"], result["has_failures"],
        result["selected_count"], result["success_count"],
        result["agreement_count"], result["majority_required"], result["elapsed_seconds"],
    ]
    for member in result["members"]:
        values = common + [
            member["id"], member["display_name"], member["status"], member["score"],
            member["level"], member["label"], member["reason"], member["error"],
            member["elapsed_seconds"],
        ]
        writer.writerow([_csv_cell(value) for value in values])
    return output.getvalue().encode("utf-8-sig")


def _text_export(record):
    result = record["result"]
    lines = [
        "FakeNgin 检测报告",
        "报告编号：" + record["id"],
        "完成时间（UTC）：" + record["created_at"],
        "提示词版本：" + record["prompt_version"],
        "",
        "消息原文：",
        record["message"],
        "",
        "最终等级：" + result["label"],
        "决策方式：" + _DECISION_LABELS.get(result["decision_method"], result["decision_method"]),
        "有效模型：{} / {}".format(result["success_count"], result["selected_count"]),
        "各等级票数：低风险 {} / 中风险 {} / 高风险 {}".format(
            result["votes"]["low"], result["votes"]["medium"], result["votes"]["high"],
        ),
        "多数门槛：{} 票".format(result["majority_required"]),
        "总耗时：{} 秒".format(result["elapsed_seconds"]),
        "结果说明：" + result["warning"],
    ]
    if result["mean_score"] is not None:
        lines.append("有效分数均值：{} / 100".format(result["mean_score"]))
    if result["fallback_reason"]:
        lines.append("使用均值的原因：" + result["fallback_reason"])
    for index, member in enumerate(result["members"], 1):
        lines.extend([
            "",
            "{}. {} ({})".format(index, member["display_name"], member["id"]),
            "状态：{} / {}".format(member["status"], member["label"]),
            "风险分：{}".format("—" if member["score"] is None else str(member["score"]) + " / 100"),
            "耗时：{} 秒".format(member["elapsed_seconds"]),
        ])
        if member["reason"]:
            lines.append("说明：" + member["reason"])
        if member["error"]:
            lines.append("错误：" + member["error"])
    return "\n".join(lines) + "\n"


@main.route("/history/<report_id>/export")
def export_report(report_id):
    record = _find_report(report_id)
    export_format = request.args.get("format", "json").lower()
    if export_format == "json":
        content = json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2)
        content_type = "application/json; charset=utf-8"
    elif export_format == "csv":
        content = _csv_export(record)
        content_type = "text/csv; charset=utf-8"
    elif export_format == "txt":
        content = _text_export(record)
        content_type = "text/plain; charset=utf-8"
    else:
        abort(400, description="不支持的导出格式")
    return Response(
        content, content_type=content_type,
        headers={
            "Content-Disposition": 'attachment; filename="fakengin-report-{}.{}"'.format(record["id"], export_format),
            "X-Content-Type-Options": "nosniff",
        },
    )
