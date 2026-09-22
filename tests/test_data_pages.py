"""数据展示页阶段验收：筛选分页搜索、词云、滚动加载、详情页。"""

from webapp import detection, keywords, newsdata, reviews


def _seed(app_seed_rows=0):
    pass


def _add_messages():
    id_a = newsdata.append_message({
        "content": "网传本市自来水将停供三天，请储水",
        "source": "微博", "publish_time": "2026-09-01 08:00:00",
    })
    id_b = newsdata.append_message({
        "content": "市气象台发布暴雨蓝色预警",
        "source": "官方通报", "publish_time": "2026-09-02 08:00:00",
    })
    id_c = newsdata.append_message({
        "content": "自来水水质检测报告公布",
        "source": "微博", "publish_time": "2026-09-03 08:00:00",
    })
    return id_a, id_b, id_c


def test_search_filters(app):
    id_a, id_b, id_c = _add_messages()

    # 来源筛选
    page = app.get("/data?q=自来水&source=微博")
    text = page.get_data(as_text=True)
    assert "共 2 条结果" in text

    # 性质筛选：先把一条标为“真实”
    row = newsdata.get_message(id_b)
    newsdata.update_message(id_b, row["version"], {"nature": "真实"})
    page = app.get("/data?q=暴雨&nature=真实")
    assert "共 1 条结果" in page.get_data(as_text=True)
    page = app.get("/data?q=暴雨&nature=虚假")
    assert "暂无相关结果" in page.get_data(as_text=True)

    # 时间筛选
    page = app.get("/data?q=自来水&time_from=2026-09-02")
    text = page.get_data(as_text=True)
    assert "共 1 条结果" in text


def test_search_pagination(app):
    for i in range(25):
        newsdata.append_message({"content": "关键词苹果相关消息第{}条".format(i)})
    page = app.get("/data?q=苹果")
    assert "共 25 条结果（第 1 / 2 页）" in page.get_data(as_text=True)
    page = app.get("/data?q=苹果&spage=2")
    text = page.get_data(as_text=True)
    assert "第 2 / 2 页" in text


def test_load_more_endpoint(app):
    for i in range(8):
        newsdata.append_message({"content": "加载测试消息第{}条".format(i)})

    response = app.get("/data/more?offset=6")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["rows"]) == 2
    assert data["has_more"] is False

    response = app.get("/data/more?offset=0")
    data = response.get_json()
    assert len(data["rows"]) == 20 or (len(data["rows"]) == 8 and data["has_more"] is False)


def test_wordcloud_terms_and_stopwords(app):
    for content in ["苹果发布会明天举行", "苹果发布会新品曝光", "香蕉价格大涨"] * 1:
        newsdata.append_message({"content": content,
                                 "publish_time": "2026-09-10 08:00:00"})

    cloud, meta = keywords.top_keywords(window_days=30)
    terms = [item["term"] for item in cloud]
    assert terms, "应产出关键词"
    assert "苹果" in " ".join(terms)
    assert "的" not in terms and "了" not in terms  # 停用词过滤
    assert meta["scope"] == "window"


def test_wordcloud_cache_invalidates_on_write(app):
    newsdata.append_message({"content": "首发消息内容", "publish_time": "2026-09-10 08:00:00"})
    cloud1, _ = keywords.top_keywords()
    terms1 = " ".join(item["term"] for item in cloud1)
    assert "特别" not in terms1

    newsdata.append_message({"content": "新增消息包含特别关键词",
                             "publish_time": "2026-09-11 08:00:00"})
    cloud2, _ = keywords.top_keywords()
    assert "特别" in " ".join(item["term"] for item in cloud2), "消息变更后词云应更新"


def test_detail_page_shows_history(app):
    from unittest.mock import patch

    from webapp import detection

    message_id = newsdata.append_message({"content": "详情页测试消息"})

    import checkmodel
    checkmodel._ensure_loaded()
    from test_detection_queue import FakeModel
    fake = FakeModel({"default": (77.0, "详情页测试理由")})
    with patch.dict(checkmodel._instances, {fake.name: fake}), \
            patch.dict(checkmodel._available, {fake.name: True}):
        detection.enqueue([message_id], model_id=fake.name)
        detection.run_pending()
    reviews.add_review(message_id, reviewer="alice", conclusion="证据不足",
                       evidence="证据链接", detection_run_id=1)

    page = app.get("/data/message/{}".format(message_id))
    text = page.get_data(as_text=True)
    assert "详情页测试消息" in text
    assert "77.0" in text
    assert "详情页测试理由" in text
    assert "alice" in text
    assert "证据不足" in text
    assert "人工审核记录" in text
    assert "模型风险评分仅为自动预警" in text


def test_detail_page_404(app):
    assert app.get("/data/message/99999").status_code == 404


def test_home_page_stats(app):
    _add_messages()
    page = app.get("/")
    text = page.get_data(as_text=True)
    assert "平台状态" in text
    assert "消息总数" in text
    assert "风险评分" in text  # 模型评分与人工结论的区分说明
