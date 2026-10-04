"""安全的页面文本呈现。"""

import re

from markupsafe import Markup, escape


def highlight_keyword(value, query):
    """只为字面匹配添加高亮，原文和关键字始终按普通文本处理。"""
    text = "" if value is None else str(value)
    keyword = str(query or "").strip()
    if not keyword:
        return escape(text)
    folded = text.casefold()
    positions = [index for index, character in enumerate(text) for _ in character.casefold()]
    parts = []
    start = 0
    for match in re.finditer(re.escape(keyword.casefold()), folded):
        match_start = positions[match.start()]
        match_end = positions[match.end() - 1] + 1
        if match_start < start:
            continue
        parts.append(escape(text[start:match_start]))
        parts.append(Markup("<mark>{}</mark>").format(escape(text[match_start:match_end])))
        start = match_end
    parts.append(escape(text[start:]))
    return Markup("").join(parts)
