"""独立的关键字搜索：按正文或来源做字面匹配，不触发检测或改写数据。"""

from flask import current_app, render_template, request

from .. import batches, newsdata
from . import main
from .data import PAGE_SIZE as DATA_PAGE_SIZE

PAGE_SIZE = 20
MAX_QUERY_LENGTH = 200


def _empty_context(query, error=None):
    return dict(
        query=query, rows=[], total_matches=0, total=0, page=1,
        total_pages=1, error=error, max_query_length=MAX_QUERY_LENGTH,
    )


def search_context(query, page=1):
    """保留原数据顺序及全数据页码；只关联内容指纹一致的已有报告。"""
    query = query.strip()
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError("搜索关键字不能超过 200 个字符")

    table = newsdata.load_all()
    total = len(table)
    if not query:
        return {**_empty_context(query), "total": total}

    folded_query = query.casefold()
    content = table["content"].fillna("").astype(str).str.casefold()
    source = table["source"].fillna("").astype(str).str.casefold()
    matches = table[
        content.str.contains(folded_query, regex=False)
        | source.str.contains(folded_query, regex=False)
    ]
    total_matches = len(matches)
    total_pages = max(1, (total_matches + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    selected = matches.iloc[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    rows = selected.to_dict("records")
    references = [
        {"file": row["_file"], "row": int(row["_row"]), "signature": row["_signature"]}
        for row in rows
    ]
    latest = batches.latest_reports(references) if references else {}
    for dataset_index, row in zip(selected.index, rows):
        row["dataset_page"] = int(dataset_index) // DATA_PAGE_SIZE + 1
        row["dataset_anchor"] = "dataset-row-{}".format(dataset_index)
        row["latest_report"] = latest.get((row["_file"], int(row["_row"]), row["_signature"]))

    return dict(
        query=query, rows=rows, total_matches=total_matches, total=total,
        page=page, total_pages=total_pages, error=None,
        max_query_length=MAX_QUERY_LENGTH,
    )


@main.route("/search")
def search():
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    if len(query) > MAX_QUERY_LENGTH:
        return render_template(
            "search.html", **_empty_context(query, "搜索关键字不能超过 200 个字符"),
        ), 400
    try:
        context = search_context(query, page)
    except Exception:
        current_app.logger.warning("Failed to search dataset")
        return render_template(
            "search.html", **_empty_context(query, "数据暂时无法搜索，请稍后重试。"),
        ), 503
    return render_template("search.html", **context)
