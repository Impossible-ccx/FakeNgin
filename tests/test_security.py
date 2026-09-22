"""安全收尾回归测试：CSRF、角色权限、输入上限、默认口令移除。"""

import re

import pytest

from webapp import db, newsdata


def test_post_without_csrf_rejected(app):
    raw_client = app._client  # 绕过 CSRFClient 包装，模拟跨站请求
    response = raw_client.post(
        "/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "CSRF" in response.get_data(as_text=True)


def test_role_required_for_write_endpoints(app):
    """无 reviewer/admin 角色的账户不能写（服务端校验，非前端隐藏）。"""
    client = app
    client.post("/login", data={"username": "admin", "password": "admin"})

    # 创建一个 viewer 角色账户并登录
    db.create_user("visitor", "visitor-pass", role="viewer")
    client.get("/logout")
    client.post("/login", data={"username": "visitor", "password": "visitor-pass"})

    response = client.post("/verify/add", data={
        "content": "越权消息", "source": "", "nature": "未校验",
    }, follow_redirects=False)
    assert response.status_code == 403
    assert newsdata.count_messages() == 0

    response = client.post("/verify/detect_batch", data={"queue": "all"},
                           follow_redirects=False)
    assert response.status_code == 403


def test_content_length_limit(app):
    client = app
    client.post("/login", data={"username": "admin", "password": "admin"})
    with pytest.raises(ValueError):
        newsdata.append_message({"content": "超" * (newsdata.MAX_CONTENT_LENGTH + 1)})

    # 正常长度可用
    message_id = newsdata.append_message({"content": "正" * newsdata.MAX_CONTENT_LENGTH})
    assert newsdata.get_message(message_id) is not None


def test_detect_message_length_limit(app):
    client = app
    client.post("/login", data={"username": "admin", "password": "admin"})
    page = client.post("/detect", data={"message": "超" * 6000}, follow_redirects=True)
    assert "消息内容过长" in page.get_data(as_text=True)


def test_no_hardcoded_default_password(fresh_data_dir, monkeypatch):
    """未设置 FAKENGIN_ADMIN_PASSWORD 时生成随机口令文件，而非固定弱口令。"""
    monkeypatch.delenv("FAKENGIN_ADMIN_PASSWORD", raising=False)
    # fixture 初始化时用了固定口令；删除库文件后按“无环境变量”全新初始化
    for suffix in ("", "-wal", "-shm"):
        path = fresh_data_dir / ("fakengin.db" + suffix)
        if path.exists():
            path.unlink()
    db.init_db()

    user = db.find_user("admin")
    assert user is not None
    # 随机口令文件已生成且权限 0600
    password_file = fresh_data_dir / "admin_initial_password.txt"
    assert password_file.exists()
    assert oct(password_file.stat().st_mode)[-3:] == "600"
    content = password_file.read_text(encoding="utf-8")
    match = re.search(r"初始密码：(.+)", content)
    assert match
    # 随机口令可登录，且不是已知的固定弱口令
    assert db.verify_user("admin", match.group(1)) is not None
    assert match.group(1) != "admin"


def test_passwords_never_stored_in_plaintext(fresh_data_dir):
    user = db.find_user("admin")
    assert user["password_hash"] != "admin"
    with db.db_conn() as conn:
        rows = conn.execute("SELECT password_hash FROM users").fetchall()
    for row in rows:
        assert not row["password_hash"].startswith("admin")
