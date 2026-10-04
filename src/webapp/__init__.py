"""FakeNgin web 应用工厂。"""

from pathlib import Path

from flask import Flask

from . import auth, db, newsdata

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"

SECRET_KEY = "fakengin-secret-key"


def create_app():
    app = Flask(
        __name__,
        static_folder=str(WEB_DIR / "static"),
        template_folder=str(WEB_DIR / "templates"),
    )
    app.secret_key = SECRET_KEY
    from .presentation import highlight_keyword

    app.jinja_env.filters["highlight_keyword"] = highlight_keyword
    from .models import model_label
    app.jinja_env.filters["model_label"] = model_label

    db.ensure_database()
    newsdata.ensure_newsdata()
    from . import batches
    batches.initialize()

    from .routes import main
    from .routes import history  # noqa: F401; registers the report routes

    app.register_blueprint(main)

    @app.context_processor
    def inject_user():
        return {"current_user": auth.get_current_user()}

    return app
