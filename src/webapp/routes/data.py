"""谣言数据展示页：列表（时间倒序、滚动加载）、可分页筛选搜索、词云、详情页。"""

from flask import jsonify, render_template, request

from .. import auth, detection, keywords, newsdata, reviews
from ..bm25 import search_ranked_ids
from . import main

PAGE_SIZE = 20
SEARCH_PAGE_SIZE = 20
SEARCH_HARD_LIMIT = 500


@main.route("/data")
def data():
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    filters = _search_filters()
    spage = request.args.get("spage", 1, type=int)
    if spage < 1:
        spage = 1

    total = newsdata.count_messages()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages

    rows = newsdata.list_messages(PAGE_SIZE, (page - 1) * PAGE_SIZE)
    detection.attach_latest_runs(rows)
    search = _filtered_search(filters, page=spage) if filters["q"] else None
    cloud, cloud_meta = keywords.top_keywords(window_days=filters["window"])

    return render_template(
        "data.html",
        rows=rows,
        page=page,
        total=total,
        total_pages=total_pages,
        filters=filters,
        search=search,
        sources=newsdata.list_sources(),
        nature_options=newsdata.NATURES,
        cloud=cloud,
        cloud_meta=cloud_meta,
    )


@main.route("/data/more")
def data_more():
    """滚动追加加载：返回下一页消息 JSON。"""
    offset = request.args.get("offset", 0, type=int)
    if offset < 0:
        offset = 0
    rows = newsdata.list_messages(PAGE_SIZE, offset)
    detection.attach_latest_runs(rows)
    return jsonify({
        "rows": [_row_brief(row) for row in rows],
        "has_more": offset + len(rows) < newsdata.count_messages(),
    })


@main.route("/data/search")
def data_search():
    """搜索结果分页（含筛选），供页面翻页链接。"""
    page = request.args.get("spage", 1, type=int)
    if page < 1:
        page = 1
    filters = _search_filters()
    if not filters["q"]:
        return jsonify({"rows": [], "total": 0})
    search = _filtered_search(filters, page=page)
    return jsonify({
        "rows": [_row_brief(row) for row in search["rows"]],
        "total": search["total"],
    })


@main.route("/data/message/<int:message_id>")
def message_detail(message_id):
    message = newsdata.get_message(message_id)
    if message is None:
        return render_template("message_detail.html", message=None), 404

    runs = detection.runs_for_message(message_id)
    for run in runs:
        run["stale"] = detection.is_stale(run, message["content"])
    history = reviews.reviews_for_message(message_id)
    comments = newsdata.list_comments(message_id)

    return render_template(
        "message_detail.html",
        message=message,
        runs=runs,
        reviews=history,
        comments=_comment_tree(comments),
        current_user=auth.get_current_user(),
    )


# ------------------------------------------------------------- helpers

def _search_filters():
    """解析并规范化搜索筛选参数。"""
    from datetime import datetime

    q = request.args.get("q", "").strip()
    nature = request.args.get("nature", "").strip()
    if nature not in newsdata.NATURES:
        nature = ""
    source = request.args.get("source", "").strip()
    time_from = request.args.get("time_from", "").strip()
    time_to = request.args.get("time_to", "").strip()
    try:
        window = int(request.args.get("window", 30))
    except ValueError:
        window = 30
    if window <= 0:
        window = 30
    return {
        "q": q, "nature": nature, "source": source,
        "time_from": _valid_date(time_from), "time_to": _valid_date(time_to),
        "window": window,
    }


def _valid_date(value):
    if not value:
        return ""
    from datetime import datetime

    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return ""


def _match_filters(row, filters):
    """时间/性质/来源过滤（BM25 相关度排序保持不变）。"""
    if filters["nature"] and row["nature"] != filters["nature"]:
        return False
    if filters["source"] and filters["source"] not in (row["source"] or ""):
        return False
    if filters["time_from"] and (row["publish_time"] or "") < filters["time_from"]:
        return False
    if filters["time_to"] and (row["publish_time"] or "") > filters["time_to"] + " 23:59:59":
        return False
    return True


def _filtered_search(filters, page):
    ids = search_ranked_ids(filters["q"], limit=SEARCH_HARD_LIMIT)
    rows = newsdata.get_messages_by_ids(ids)
    rows = [row for row in rows if _match_filters(row, filters)]
    total = len(rows)
    total_pages = max(1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * SEARCH_PAGE_SIZE
    page_rows = rows[start:start + SEARCH_PAGE_SIZE]
    detection.attach_latest_runs(page_rows)
    return {
        "rows": page_rows,
        "page": page,
        "total": total,
        "total_pages": total_pages,
    }


def _row_brief(row):
    """滚动加载 / 搜索翻页的 JSON 行；与页面同一风险评分展示口径。"""
    run = row.get("latest_run")
    latest_run = None
    if run is not None:
        latest_run = {
            "status": run["status"],
            "probability": run["probability"],
        }
    return {
        "id": row["id"],
        "content": row["content"],
        "nature": row["nature"],
        "fake_probability": row["fake_probability"],
        "legacy_probability": row["legacy_probability"],
        "latest_run": latest_run,
        "run_stale": bool(row.get("run_stale")),
        "source": row["source"],
        "publish_time": row["publish_time"],
        "detail_url": "/data/message/{}".format(row["id"]),
    }


def _comment_tree(comments):
    """把评论列表整理为树：children 挂到父评论下，按时间排序。"""
    nodes = {row["id"]: {**row, "children": []} for row in comments}
    roots = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        if parent is not None and parent["id"] != node["id"]:
            parent["children"].append(node)
        else:
            roots.append(node)
    return roots
