"""谣言数据展示页。"""

from flask import render_template

from . import main


@main.route("/data")
def data():
    return render_template("data.html")
