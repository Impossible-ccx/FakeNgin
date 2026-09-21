"""关键词统计（词云数据）：真实消息分词 + 停用词 + 时间窗口。

- 仅统计消息正文（jieba 搜索引擎模式分词），过滤停用词、单字与纯数字。
- 时间窗口按发布时间；窗口内无消息时退回全部消息，并在结果中标注。
- 结果按数据版本缓存，消息变更后自动失效。
"""

import time
from collections import Counter

from . import bm25, newsdata

# 常用中文停用词与无分析价值的词（课程规模内置清单，够用即可）
STOPWORDS = {
    "的", "了", "和", "是", "就", "都", "而", "及", "与", "着", "或", "一个", "没有",
    "我们", "你们", "他们", "她们", "它们", "自己", "什么", "这个", "那个", "这些",
    "那些", "这样", "那样", "如何", "哪些", "还是", "不过", "但是", "然而", "因为",
    "所以", "如果", "虽然", "即使", "而且", "并且", "或者", "只是", "还有", "可以",
    "可能", "需要", "应该", "已经", "正在", "将要", "大家", "网友", "网传", "消息",
    "称", "日", "月", "年", "今日", "明天", "昨天", "近日", "目前", "现在", "时候",
    "出现", "发生", "进行", "表示", "显示", "发现", "认为", "指出", "介绍", "相关",
    "视频", "图片", "链接", "转发", "评论", "点赞", "请", "被", "把", "让", "向",
}


def top_keywords(window_days=30, limit=50, data_version=None):
    """返回 (keywords, meta)。

    keywords: [{"term", "count"}]（按频次降序）
    meta: {"window_days", "scope", "message_count"}
    scope: "window"（时间窗口内）或 "all"（窗口内无消息，退回全部）
    """
    from webapp import db

    version = newsdata.data_version()
    # 缓存键包含库路径：不同数据目录的相同版本号不冲突（测试隔离）
    cache_key = (str(db.DATABASE_FILE), version, window_days, limit)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    rows = newsdata.load_all()
    if not rows:
        return [], {"window_days": window_days, "scope": "all", "message_count": 0}

    scope_rows = [row for row in rows
                  if _in_window(row["publish_time"], window_days)]
    scope = "window"
    if not scope_rows:
        scope_rows = rows
        scope = "all"

    counter = Counter()
    for row in scope_rows:
        for token in bm25._tokenize(row["content"]):
            if len(token) < 2 or token in STOPWORDS or token.isdigit():
                continue
            counter[token] += 1

    keywords = [{"term": term, "count": count}
                for term, count in counter.most_common(limit)]
    meta = {"window_days": window_days, "scope": scope,
            "message_count": len(scope_rows)}

    _cache_put(cache_key, (keywords, meta))
    return keywords, meta


def _in_window(publish_time, window_days):
    if not publish_time:
        return False
    from datetime import datetime

    try:
        published = datetime.strptime(publish_time, newsdata.TIME_FORMAT)
    except ValueError:
        return False
    return (datetime.now() - published).days <= window_days


_CACHE = {}
_CACHE_MAX = 8


def _cache_get(key):
    return _CACHE.get(key)


def _cache_put(key, value):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = value
