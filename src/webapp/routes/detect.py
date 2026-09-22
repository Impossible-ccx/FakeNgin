"""谣言检测系统页。

自由文本检测默认“不保存”；勾选“保存并提交复核”时，检测成功后把消息
入库并记录一次已完成的检测任务，进入人工复核队列（需要登录）。
"""

import time

import checkmodel
from checkmodel.base import CheckError
from flask import abort, flash, redirect, render_template, request, url_for

from .. import auth, detection, newsdata, ratelimit
from . import main

WARNING_HIGH = 70
WARNING_MEDIUM = 40
MAX_MESSAGE_LENGTH = 5000

# 匿名/公开检测入口限流：每次检测都消耗一次模型调用
DETECT_LIMIT = 10
DETECT_WINDOW_SECONDS = 60


def _warning(percentage):
    if percentage >= WARNING_HIGH:
        return "high", "高度疑似谣言，请谨慎对待并核实来源。"
    if percentage >= WARNING_MEDIUM:
        return "medium", "存在谣言可能，建议进一步核实。"
    return "low", "暂未发现明显的谣言特征。"


@main.route("/detect", methods=["GET", "POST"])
def detect():
    models = checkmodel.get_models()
    default_model = models[0]["id"] if models else ""
    selected = request.values.get("model", default_model)

    message = ""
    result = None

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        save_mode = request.form.get("save_for_review") == "1"
        user = auth.get_current_user()
        limit_key = "detect:{}".format(request.remote_addr or "?")
        if save_mode and (user is None or user.get("role") not in ("admin", "reviewer")):
            # 保存入口在模型调用与写入之前做服务端角色校验；
            # 未登录或 viewer 角色勾选“保存并提交复核”一律拒绝。
            # 不勾选保存的匿名检测行为保持不变。
            abort(403, description="保存并提交复核需要 admin 或 reviewer 权限")
        if not message:
            flash("请输入消息内容", "error")
        elif len(message) > MAX_MESSAGE_LENGTH:
            flash("消息内容过长（上限 {} 字）".format(MAX_MESSAGE_LENGTH), "error")
        elif not ratelimit.allowed(limit_key, DETECT_LIMIT, DETECT_WINDOW_SECONDS):
            abort(429, description="检测请求过于频繁，请稍后再试")
        elif not selected:
            flash("当前没有可用模型，请检查模型配置后再试", "error")
        else:
            try:
                model = checkmodel.get_model(selected)
            except KeyError:
                # 页面加载后模型才变为不可用：重新探测一次再判定。
                checkmodel.reprobe()
                models = checkmodel.get_models()
                try:
                    model = checkmodel.get_model(selected)
                except KeyError:
                    model = None
                if model is None:
                    flash("所选模型当前不可用，请重新选择或稍后再试", "error")
            if model is not None:
                # 到达模型调用才计入限流窗口
                ratelimit.record(limit_key)
                started = time.time()
                try:
                    probability, extra_info = model.check(message)
                except CheckError as exc:
                    flash(str(exc), "error")
                except Exception:
                    flash("检测失败，请稍后重试", "error")
                else:
                    duration_ms = max(1, int((time.time() - started) * 1000))
                    percentage = max(0, min(100, int(round(float(probability)))))
                    level, warning = _warning(percentage)
                    result = {
                        "percentage": percentage,
                        "level": level,
                        "warning": warning,
                        "extra_info": extra_info,
                    }
                    if save_mode:
                        _save_for_review(message, selected, model,
                                         float(probability), extra_info,
                                         duration_ms)

    selected_label = next(
        (m["display_name"] for m in models if m["id"] == selected),
        selected,
    )

    return render_template(
        "detect.html",
        models=models,
        model_status=checkmodel.get_model_status(),
        selected=selected,
        selected_label=selected_label,
        message=message,
        result=result,
        current_user=auth.get_current_user(),
    )


def _save_for_review(message, model_id, model, probability, reason, duration_ms):
    """把自由文本检测结果入库并提交复核（检测任务记录为已完成）。

    自由文本没有评论，序列模型的实际输入就是正文一条；input_kind 与
    模型版本标识的记录口径与检测队列路径一致。
    """
    try:
        message_id = newsdata.append_message({
            "content": message,
            "source": "检测页提交",
        })
    except ValueError as exc:
        flash(str(exc), "error")
        return None
    uses_comments = bool(getattr(model, "uses_comments", False))
    model_version = str(getattr(model, "model_version", "") or "")[:200]
    with newsdata.db.db_conn() as conn:
        conn.execute(
            "INSERT INTO detection_runs (message_id, model_id, model_name, "
            "input_version, content_digest, input_kind, model_version, "
            "status, probability, reason, duration_ms, created_at, "
            "started_at, finished_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?)",
            (
                message_id, model_id, getattr(model, "display_name", model_id),
                detection.input_digest(message, [], uses_comments),
                "sequence" if uses_comments else "content", model_version,
                probability, reason, duration_ms,
                newsdata.db.now_string(), newsdata.db.now_string(),
                newsdata.db.now_string(),
            ),
        )
    flash("已保存为消息 #{} 并加入复核队列".format(message_id), "success")
    return message_id


@main.route("/detect/reprobe", methods=["POST"])
def detect_reprobe():
    """重新探测全部模型，模型服务恢复后无需重启应用。"""
    count = checkmodel.reprobe()
    if count:
        flash("重新探测完成，发现 {} 个可用模型".format(count), "success")
    else:
        flash("重新探测完成，仍没有可用模型，请检查模型配置或服务状态", "error")
    return redirect(url_for("main.detect"))
