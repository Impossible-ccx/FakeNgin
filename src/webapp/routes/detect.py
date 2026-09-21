"""谣言检测系统页。"""

from flask import render_template

from . import main


@main.route("/detect")
def detect():
    return render_template("detect.html")
