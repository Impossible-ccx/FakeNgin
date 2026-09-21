"""谣言数据展示页。"""

from flask import render_template, request

from .. import newsdata
from . import main

PAGE_SIZE = 20


@main.route("/data")
def data():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    query = request.args.get("q", "").strip()

    message_table = newsdata.load_all()
    total = len(message_table)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages

    start = (page - 1) * PAGE_SIZE
    rows = message_table.iloc[start:start + PAGE_SIZE].to_dict("records")
    search_results = newsdata.search_messages(query, limit=3) if query else []

    return render_template(
        "data.html",
        rows=rows,
        query=query,
        search_results=search_results,
        page=page,
        total=total,
        total_pages=total_pages,
    )
