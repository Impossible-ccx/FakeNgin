"""WSGI / 生产入口（gunicorn 加载本模块的 app）。

多 worker 部署下会话与 CSRF 依赖固定的 FLASK_SECRET_KEY：各 worker
各自生成随机密钥会使登录态随机失效。该要求由程序强制执行——密钥
缺失时直接拒绝启动，而不是只在部署文档里提示。

本地开发请用 `python src/app.py`（开发入口允许回退随机密钥并告警）。
"""

import os
import sys

if not (os.getenv("FLASK_SECRET_KEY") or "").strip():
    sys.exit(
        "FLASK_SECRET_KEY 未设置：WSGI/生产入口必须配置固定会话密钥。\n"
        "生成方式：python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "本地开发请改用：python src/app.py")

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    # debug 仅在显式设置 FLASK_DEBUG=1 时开启（开发环境用）
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)
