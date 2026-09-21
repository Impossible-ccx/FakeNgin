"""首页。"""

import checkmodel
from flask import render_template

from .. import db, detection, newsdata
from . import main


@main.route("/")
def index():
    total = newsdata.count_messages()
    pending = newsdata.count_messages(nature=newsdata.DEFAULT_NATURE)
    with db.db_conn() as conn:
        runs = conn.execute("SELECT COUNT(*) AS n FROM detection_runs").fetchone()["n"]
    stats = {
        "messages": total,
        "pending_reviews": pending,
        "reviewed": total - pending,
        "detection_runs": runs,
        "models": len(checkmodel.get_models()),
    }
    return render_template("index.html", stats=stats)
