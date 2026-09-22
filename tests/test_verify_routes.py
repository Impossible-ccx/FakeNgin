"""人工校验页路由测试：登录后增删改查、版本冲突提示、校验与跳过。"""

from webapp import detection, newsdata, reviews


def _login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=True,
    )


def test_verify_requires_login(app):
    response = app.get("/verify", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_add_update_delete_through_routes(app):
    _login(app)

    app.post("/verify/add", data={
        "content": "路由添加的消息", "source": "测试", "nature": "未校验",
        "publish_time": "2026-01-01 08:00:00",
    })
    assert newsdata.count_messages() == 1
    row = newsdata.load_all()[0]
    assert row["content"] == "路由添加的消息"

    # 旧版本号更新被拒绝并提示
    page = app.post("/verify/update", data={
        "id": str(row["id"]), "version": str(row["version"] + 5),
        "content": "过期修改", "source": "", "nature": "未校验",
    }, follow_redirects=True)
    assert "已被其他操作修改" in page.get_data(as_text=True)

    # 正确版本更新成功
    page = app.post("/verify/update", data={
        "id": str(row["id"]), "version": str(row["version"]),
        "content": "新内容", "source": "测试", "nature": "未校验",
    }, follow_redirects=True)
    assert "消息已更新" in page.get_data(as_text=True)
    assert newsdata.get_message(row["id"])["content"] == "新内容"

    # 删除
    current = newsdata.get_message(row["id"])
    page = app.post("/verify/delete", data={
        "id": str(row["id"]), "version": str(current["version"]),
    }, follow_redirects=True)
    assert "消息已删除" in page.get_data(as_text=True)
    assert newsdata.count_messages() == 0


def test_review_and_skip_flow(app):
    _login(app)
    message_id = newsdata.append_message({"content": "待校验消息"})

    page = app.get("/verify")
    text = page.get_data(as_text=True)
    assert "待校验消息" in text

    # 审核为“真实”（保存审核人、结论、证据）
    row = newsdata.get_message(message_id)
    page = app.post("/verify/review", data={
        "id": str(message_id), "version": str(row["version"]),
        "conclusion": "真实", "evidence": "官方通报链接",
    }, follow_redirects=True)
    assert "审核记录已保存" in page.get_data(as_text=True)
    assert newsdata.get_message(message_id)["nature"] == "真实"
    history = reviews.reviews_for_message(message_id)
    assert len(history) == 1
    assert history[0]["reviewer"] == "admin"
    assert history[0]["evidence"] == "官方通报链接"

    # 全部校验完成后提示
    page = app.get("/verify")
    assert "所有消息都已完成校验" in page.get_data(as_text=True)


def test_skip_message(app):
    _login(app)
    message_id = newsdata.append_message({"content": "被跳过的消息"})

    page = app.post("/verify/skip", data={"id": str(message_id)}, follow_redirects=True)
    assert "已跳过当前消息" in page.get_data(as_text=True)
    page = app.get("/verify")
    assert "未校验消息均已被跳过" in page.get_data(as_text=True)

    page = app.post("/verify/reset_skip", follow_redirects=True)
    assert "已重置跳过列表" in page.get_data(as_text=True)


def test_search_page_shows_result(app):
    newsdata.append_message({"content": "网传本市自来水将停供三天"})
    newsdata.append_message({"content": "市气象台发布暴雨预警"})

    page = app.get("/data?q=自来水")
    text = page.get_data(as_text=True)
    assert "自来水" in text
    assert "暴雨预警" not in text.split("全部消息")[0], "搜索结果不应混入无关消息"

    page = app.get("/data?q=不存在的词xyz")
    assert "暂无相关结果" in page.get_data(as_text=True)


def _install_fake_model():
    """让路由层模型工厂返回假模型（不触发真实推理）。"""
    import checkmodel
    from unittest.mock import patch
    from test_detection_queue import FakeModel

    checkmodel._ensure_loaded()
    fake = FakeModel({"default": (66.0, "路由测试理由")})
    patches = (
        patch.dict(checkmodel._instances, {fake.name: fake}, clear=False),
        patch.dict(checkmodel._available, {fake.name: True}, clear=False),
        patch.object(checkmodel, "get_models",
                     return_value=[{"id": fake.name, "display_name": fake.display_name,
                                    "description": "测试"}]),
    )
    return patches, fake


def test_batch_detect_route_executes_queue(app):
    _login(app)
    id_a = newsdata.append_message({"content": "批量消息一"})
    id_b = newsdata.append_message({"content": "批量消息二"})

    patches, fake = _install_fake_model()
    with patches[0], patches[1], patches[2]:
        page = app.post("/verify/detect_batch", data={"queue": "all"},
                        follow_redirects=True)
        assert "已加入检测队列 2 条" in page.get_data(as_text=True)

        # 等待后台线程执行（conftest 创建的 app 已启动 worker）
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            statuses = [detection.runs_for_message(i)[0]["status"]
                        for i in (id_a, id_b)]
            if all(s == "succeeded" for s in statuses):
                break
            time.sleep(0.05)

    runs_a = detection.runs_for_message(id_a)
    assert runs_a[0]["status"] == "succeeded"
    assert runs_a[0]["probability"] == 66.0

    # 页面展示模型风险评分
    page = app.get("/verify?queue=all")
    assert "66.0%" in page.get_data(as_text=True)


def test_detect_page_save_for_review(app):
    patches, fake = _install_fake_model()
    with patches[0], patches[1], patches[2]:
        app.post("/login", data={"username": "admin", "password": "admin"})

        # 不勾选保存：不产生消息
        app.post("/detect", data={"message": "只检测不保存", "model": fake.name})
        assert newsdata.count_messages() == 0

        # 勾选保存：产生消息 + 已完成的检测记录
        page = app.post("/detect", data={
            "message": "保存并提交复核的消息", "model": fake.name,
            "save_for_review": "1",
        }, follow_redirects=True)
        assert newsdata.count_messages() == 1
        saved = newsdata.load_all()[0]
        assert saved["content"] == "保存并提交复核的消息"
        runs = detection.runs_for_message(saved["id"])
        assert runs and runs[0]["status"] == "succeeded"
        assert "加入复核队列" in page.get_data(as_text=True)


def test_detect_page_save_requires_role(app):
    """保存入口在模型调用前做服务端角色校验：匿名与 viewer 一律 403。"""
    patches, fake = _install_fake_model()
    with patches[0], patches[1], patches[2]:
        # 匿名勾选保存：不执行检测、不产生消息
        page = app.post("/detect", data={
            "message": "未登录的保存请求", "model": fake.name,
            "save_for_review": "1",
        })
        assert page.status_code == 403
        assert newsdata.count_messages() == 0
        # 假模型从未被调用（权限判定发生在模型调用之前）
        assert fake.calls == []

        # viewer 角色同样拒绝
        from webapp import db as webdb
        webdb.create_user("viewer1", "viewer-pass", role="viewer")
        app.post("/login", data={"username": "viewer1", "password": "viewer-pass"})
        page = app.post("/detect", data={
            "message": "viewer 的保存请求", "model": fake.name,
            "save_for_review": "1",
        })
        assert page.status_code == 403
        assert newsdata.count_messages() == 0
        assert fake.calls == []

        # viewer 不勾选保存：保留既有匿名检测行为
        page = app.post("/detect", data={
            "message": "viewer 只检测", "model": fake.name,
        }, follow_redirects=True)
        assert page.status_code == 200
        assert fake.calls == ["viewer 只检测"]
        assert newsdata.count_messages() == 0
