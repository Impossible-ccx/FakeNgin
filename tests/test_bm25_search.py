"""BM25 搜索回归测试：真实 jieba 分词，稳定消息 ID，首次建索引与增删改失效。"""

import webapp.bm25 as bm25
from webapp import newsdata


def _msg(content, publish_time="2026-01-01 08:00:00"):
    return {
        "content": content,
        "publish_time": publish_time,
        "source": "回归测试（合成）",
    }


def test_first_search_builds_index_and_hits(app):
    id_a = newsdata.append_message(_msg("网传本市明天起自来水停供三天，请家家储水。"))
    newsdata.append_message(_msg("市气象台今日发布暴雨蓝色预警。"))

    hits = bm25.search("自来水 停供", limit=3)

    assert len(hits) == 1
    assert "自来水" in hits[0]["content"]
    assert hits[0]["id"] == id_a, "搜索结果必须携带稳定消息 ID"


def test_empty_query_and_no_hit_return_empty(app):
    assert bm25.search("", limit=3) == []
    assert bm25.search("   ", limit=3) == []

    newsdata.append_message(_msg("图书馆中秋假期闭馆三天。"))
    assert bm25.search("不存在的关键词组合xyz", limit=3) == []


def test_add_update_delete_refresh_index(app):
    id_first = newsdata.append_message(_msg("市科技馆本周六免费开放。"))
    assert bm25.search("科技馆", limit=3), "新增消息后立即可搜到"

    # 追加另一条含新关键词的消息
    id_second = newsdata.append_message(_msg("网传扫码送移动电源活动系骗局。"))
    hits = bm25.search("移动电源", limit=3)
    assert len(hits) == 1 and hits[0]["id"] == id_second

    # 更新正文后旧关键词不再命中，新关键词可命中
    second = newsdata.get_message(id_second)
    newsdata.update_message(
        id_second, second["version"],
        _msg("物业表示尚未收到任何停车费调整通知。", publish_time="2026-01-02 09:00:00"),
    )
    assert bm25.search("移动电源", limit=3) == []
    hits = bm25.search("停车费", limit=3)
    assert len(hits) == 1 and hits[0]["id"] == id_second

    # 删除后不再命中
    newsdata.delete_message(id_first, newsdata.get_message(id_first)["version"])
    assert bm25.search("科技馆", limit=3) == []


def test_index_persists_across_rebuild(app):
    """索引缓存命中路径与重建路径结果一致。"""
    message_id = newsdata.append_message(_msg("市气象台今日发布暴雨蓝色预警。"))
    first = bm25.search("暴雨", limit=3)
    assert len(first) == 1 and first[0]["id"] == message_id
    # 再次搜索走缓存命中路径
    second = bm25.search("暴雨", limit=3)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
