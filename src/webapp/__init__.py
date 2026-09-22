"""FakeNgin web 应用工厂。"""

from pathlib import Path
import os
import secrets
import sys

from flask import Flask

import config  # 加载 .env，外部环境变量优先

from . import auth, csrf, db, detection

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"

SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("FLASK_SECRET_KEY") and \
        "gunicorn" in Path(sys.argv[0] or "").name:
    # 经 gunicorn 直接加载本模块（未走 src/app.py 入口）且未配置密钥：
    # 多 worker 会话将随机失效，启动即告警（src/app.py 入口会直接拒绝）。
    print("警告：gunicorn 部署未设置 FLASK_SECRET_KEY，会话可能随机失效",
          file=sys.stderr)


def create_app():
    app = Flask(
        __name__,
        static_folder=str(WEB_DIR / "static"),
        template_folder=str(WEB_DIR / "templates"),
    )
    app.secret_key = SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # 正式 HTTPS 部署时设置 FAKENGIN_COOKIE_SECURE=1
        SESSION_COOKIE_SECURE=os.getenv("FAKENGIN_COOKIE_SECURE") == "1",
    )

    csrf.install(app)

    db.init_db()
    detection.recover_interrupted()
    detection.start_worker()

    from .routes import main

    app.register_blueprint(main)

    @app.context_processor
    def inject_user():
        return {"current_user": auth.get_current_user()}

    return app
