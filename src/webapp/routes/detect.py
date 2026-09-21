"""谣言检测系统页。"""

import checkmodel
from flask import flash, render_template, request

from . import main

WARNING_HIGH = 70
WARNING_MEDIUM = 40


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
        if not message:
            flash("请输入消息内容", "error")
        elif not selected:
            flash("请选择检测模型", "error")
        else:
            try:
                model = checkmodel.get_model(selected)
            except KeyError:
                flash("所选模型不存在", "error")
            else:
                probability, extra_info = model.check(message)
                percentage = max(0, min(100, int(round(float(probability)))))
                level, warning = _warning(percentage)
                result = {
                    "percentage": percentage,
                    "level": level,
                    "warning": warning,
                    "extra_info": extra_info,
                }

    selected_label = next(
        (m["display_name"] for m in models if m["id"] == selected),
        selected,
    )

    return render_template(
        "detect.html",
        models=models,
        selected=selected,
        selected_label=selected_label,
        message=message,
        result=result,
    )
