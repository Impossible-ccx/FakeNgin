"""独立关键字检索和真实数据词频，不触发模型检测或改写数据。"""

from flask import current_app, render_template, request

from .. import newsdata
from ..keywords import dataset_keywords
from . import main
from .data import PAGE_SIZE as DATA_PAGE_SIZE

PAGE_SIZE = 20
MAX_QUERY_LENGTH = 200


def _empty_context(query, error=None):
    return dict(
        query=query, rows=[], total_matches=0, total=0, page=1,
        total_pages=1, error=error, max_query_length=MAX_QUERY_LENGTH,
        keywords=[], keyword_sample_count=0,
    )


def search_context(query, page=1):
    query = query.strip()
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError("搜索关键字不能超过 200 个字符")
    table = newsdata.load_all()
    keywords, sample_count = dataset_keywords(table["content"].fillna("").astype(str))
    context = {
        **_empty_context(query), "total": len(table), "keywords": keywords,
        "keyword_sample_count": sample_count,
    }
    if not query:
        return context

    folded_query = query.casefold()
    matches = table[
        table["content"].fillna("").astype(str).str.casefold().str.contains(folded_query, regex=False)
        | table["source"].fillna("").astype(str).str.casefold().str.contains(folded_query, regex=False)
    ]
    total_matches = len(matches)
    total_pages = max(1, (total_matches + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    selected = matches.iloc[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    rows = selected.to_dict("records")
    for dataset_index, row in zip(selected.index, rows):
        row["dataset_page"] = int(dataset_index) // DATA_PAGE_SIZE + 1
        row["dataset_anchor"] = "dataset-row-{}".format(dataset_index)
    context.update(rows=rows, total_matches=total_matches, page=page, total_pages=total_pages)
    return context


@main.route("/search")
def search():
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    if len(query) > MAX_QUERY_LENGTH:
        return render_template("search.html", **_empty_context(query, "搜索关键字不能超过 200 个字符")), 400
    try:
        context = search_context(query, page)
    except Exception:
        current_app.logger.warning("Failed to search dataset")
        return render_template("search.html", **_empty_context(query, "数据暂时无法搜索，请稍后重试。")), 503
    return render_template("search.html", **context)
