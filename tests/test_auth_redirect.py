"""登录回跳安全回归测试：外部回跳被拒绝，站内回跳保留。"""

EXTERNAL_TARGETS = [
    "//example.org",
    "/\\example.org",
    "https://evil.org",
    "http://evil.org/path",
    "javascript:alert(1)",
]


def login(client, next_target):
    return client.post(
        "/login?next=" + next_target,
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )


def test_default_admin_can_login(app):
    response = app.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_external_next_targets_rejected(app):
    for target in EXTERNAL_TARGETS:
        response = login(app, target)
        location = response.headers.get("Location", "")
        host = location.split("//")[-1].split("/")[0]
        assert "example.org" not in location and "evil.org" not in location, (
            "next={!r} 被重定向到外部 {}".format(target, location)
        )
        assert host not in ("example.org", "evil.org")
        assert location.startswith("/"), "外部回跳必须回退到站内首页"


def test_internal_next_target_kept(app):
    response = login(app, "/verify")
    assert response.status_code == 302
    assert response.headers["Location"] == "/verify"


def test_wrong_password_rejected(app):
    response = app.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "账户或密码错误" in response.get_data(as_text=True)
