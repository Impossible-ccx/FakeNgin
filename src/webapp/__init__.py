"""FakeNgin web 应用工厂。"""

from pathlib import Path

from flask import Flask
from dotenv import load_dotenv

from . import auth, batches, db, newsdata
from .models import model_label
from .presentation import highlight_keyword

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"

SECRET_KEY = "fakengin-secret-key"


def create_app():
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    app = Flask(
        __name__,
        static_folder=str(WEB_DIR / "static"),
        template_folder=str(WEB_DIR / "templates"),
    )
    app.secret_key = SECRET_KEY
    app.add_template_filter(model_label, "model_label")
    app.add_template_filter(highlight_keyword, "highlight_keyword")

    db.ensure_database()
    newsdata.ensure_newsdata()
    batches.initialize()

    from .routes import main

    app.register_blueprint(main)

    @app.context_processor
    def inject_user():
        return {"current_user": auth.get_current_user()}

    return app
