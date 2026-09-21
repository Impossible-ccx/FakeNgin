"""页面路由包。

各页面的视图函数按页面拆分到独立模块，共用 main 蓝图，
因此 url_for 的端点名（main.index、main.verify 等）保持不变。
"""

from flask import Blueprint

main = Blueprint("main", __name__)

from . import account, data, detect, home, verify  # noqa: E402,F401
