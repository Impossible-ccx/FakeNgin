"""谣言检测系统页，以及异步检测接口和结果保存接口。"""

import checkmodel
from checkmodel.base import CheckError
from flask import flash, redirect, render_template, request, url_for

from .. import auth, newsdata
from . import main

WARNING_HIGH = 70
WARNING_MEDIUM = 40


def _warning(percentage):
    if percentage >= WARNING_HIGH:
        return "high", "高度疑似谣言，请谨慎对待并核实来源。"
    if percentage >= WARNING_MEDIUM:
        return "medium", "存在谣言可能，建议进一步核实。"
    return "low", "暂未发现明显的谣言特征。"


def _run_check(form):
    """执行一次检测，返回 (message, selected, result, error)，二者至多一个非空。"""
    models = checkmodel.get_models()
    default_model = models[0]["id"] if models else ""
    message = form.get("message", "").strip()
    selected = form.get("model", default_model)

    if not message:
        return message, selected, None, "请输入消息内容"
    if not selected:
        return message, selected, None, "请选择检测模型"
    try:
        model = checkmodel.get_model(selected)
    except KeyError:
        return message, selected, None, "所选模型不存在或不可用"

    try:
        probability, extra_info = model.check(message)
    except CheckError as exc:
        return message, selected, None, str(exc)
    except Exception:
        return message, selected, None, "检测失败，请稍后重试"

    percentage = max(0, min(100, int(round(float(probability)))))
    level, warning = _warning(percentage)
    result = {
        "percentage": percentage,
        "level": level,
        "warning": warning,
        "extra_info": extra_info,
    }
    return message, selected, result, None


def _selected_label(selected):
    return next(
        (m["display_name"] for m in checkmodel.get_models() if m["id"] == selected),
        selected,
    )


@main.route("/detect", methods=["GET", "POST"])
def detect():
    models = checkmodel.get_models()
    default_model = models[0]["id"] if models else ""
    selected = request.values.get("model", default_model)
    message = ""
    result = None
    error = None

    if request.method == "POST":
        message, selected, result, error = _run_check(request.form)

    return render_template(
        "detect.html",
        models=models,
        selected=selected,
        selected_label=_selected_label(selected),
        message=message,
        result=result,
        error=error,
    )


@main.route("/detect/check", methods=["POST"])
def detect_check():
    """异步检测接口：返回渲染好的结果片段，由页面脚本插入。"""
    message, selected, result, error = _run_check(request.form)
    return render_template(
        "_detect_result.html",
        message=message,
        selected=selected,
        selected_label=_selected_label(selected),
        result=result,
        error=error,
    )


@main.route("/detect/save", methods=["POST"])
@auth.login_required
def detect_save():
    """把检测结果保存到数据集（manual.csv），进入未校验状态。"""
    message = request.form.get("message", "").strip()
    try:
        if not message:
            raise ValueError("没有可保存的消息")
        try:
            probability = float(request.form.get("probability", ""))
        except ValueError:
            raise ValueError("虚假概率不正确")
        probability = max(0.0, min(100.0, probability))
        model_id = request.form.get("model", "")
        label = _selected_label(model_id) if model_id else ""
        source = "检测系统" if not label else "检测系统（{}）".format(label)

        newsdata.append_message({
            "content": message,
            "nature": newsdata.DEFAULT_NATURE,
            "fake_probability": "{:.2f}".format(probability),
            "source": source,
            "publish_time": "",
            "process_time": newsdata.now_string(),
        })
        flash("已保存到数据集，可在人工校验系统中复核", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("main.detect"))
