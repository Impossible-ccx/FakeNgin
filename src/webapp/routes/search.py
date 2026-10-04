"""独立关键字检索和真实数据词频，不触发模型检测或改写数据。"""

from collections import OrderedDict
import threading

from flask import current_app, render_template, request

from .. import batches, bm25, newsdata
from ..keywords import MAX_SAMPLE_MESSAGES, dataset_keywords
from . import main
from .data import PAGE_SIZE as DATA_PAGE_SIZE

PAGE_SIZE = 20
MAX_QUERY_LENGTH = 200
SEARCH_MODES = ("related", "literal")
_cache_lock = threading.RLock()
_match_cache = OrderedDict()
_position_cache = OrderedDict()


def _empty_context(query, error=None, mode="related"):
    return dict(
        query=query, rows=[], total_matches=0, total=0, page=1,
        total_pages=1, error=error, max_query_length=MAX_QUERY_LENGTH,
        keywords=[], keyword_sample_count=0, mode=mode,
    )


def clear_cache():
    with _cache_lock:
        _match_cache.clear()
        _position_cache.clear()


def _matching_indices(table, query, mode, version):
    key = (version, query.casefold(), mode)
    with _cache_lock:
        if key in _match_cache:
            _match_cache.move_to_end(key)
            return _match_cache[key]
        if mode == "literal":
            folded_query = query.casefold()
            mask = (
                table["content"].fillna("").astype(str).str.casefold().str.contains(folded_query, regex=False)
                | table["source"].fillna("").astype(str).str.casefold().str.contains(folded_query, regex=False)
            )
            indices = tuple(table.index[mask])
        else:
            if version not in _position_cache:
                _position_cache[version] = {
                    (name, int(row)): int(index)
                    for index, name, row in zip(table.index, table["_file"], table["_row"])
                }
                while len(_position_cache) > 4:
                    _position_cache.popitem(last=False)
            positions = _position_cache[version]
            indices = tuple(positions[reference] for reference in bm25.ranked_references(query) if reference in positions)
        _match_cache[key] = indices
        while len(_match_cache) > 128:
            _match_cache.popitem(last=False)
        return indices


def search_context(query, page=1, mode="related"):
    query = query.strip()
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError("搜索关键字不能超过 200 个字符")
    if mode not in SEARCH_MODES:
        raise ValueError("请选择有效的查找方式")
    for _ in range(3):
        version = newsdata.dataset_fingerprint()
        table = newsdata.load_all()
        indices = _matching_indices(table, query, mode, version) if query else ()
        if newsdata.dataset_fingerprint() == version:
            break
    else:
        raise OSError("消息数据正在更新，请稍后搜索")
    keywords, sample_count = dataset_keywords(table["content"].iloc[:MAX_SAMPLE_MESSAGES])
    context = {
        **_empty_context(query, mode=mode), "total": len(table), "keywords": keywords,
        "keyword_sample_count": sample_count,
    }
    if not query:
        return context

    total_matches = len(indices)
    total_pages = max(1, (total_matches + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    selected = table.loc[list(indices[(page - 1) * PAGE_SIZE:page * PAGE_SIZE])]
    rows = selected.to_dict("records")
    references = [dict(file=row["_file"], row=int(row["_row"]), signature=row["_signature"]) for row in rows]
    latest = batches.latest_reports(references)
    for dataset_index, row in zip(selected.index, rows):
        row["dataset_page"] = int(dataset_index) // DATA_PAGE_SIZE + 1
        row["dataset_anchor"] = "dataset-row-{}".format(dataset_index)
        row["latest_report"] = latest.get((row["_file"], int(row["_row"]), row["_signature"]))
    context.update(rows=rows, total_matches=total_matches, page=page, total_pages=total_pages)
    return context


@main.route("/search")
def search():
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    mode = request.args.get("mode", "related")
    if len(query) > MAX_QUERY_LENGTH:
        return render_template("search.html", **_empty_context(query, "搜索关键字不能超过 200 个字符", mode="related")), 400
    if mode not in SEARCH_MODES:
        return render_template("search.html", **_empty_context(query, "请选择有效的查找方式")), 400
    try:
        context = search_context(query, page, mode)
    except Exception:
        current_app.logger.warning("Failed to search dataset")
        return render_template("search.html", **_empty_context(query, "数据暂时无法搜索，请稍后重试。", mode)), 503
    return render_template("search.html", **context)
