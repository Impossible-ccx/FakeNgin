"""本轮缺陷修复的回归测试。

覆盖：新数据目录初始化（缺失/空/已有/权限/并发）、检测任务所有权与恢复、
已处理队列口径、风险评分展示口径、跨消息审核与版本校验、登录限流、
修改密码、Secure Cookie。
"""

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from webapp import db, detection, newsdata, ratelimit, reviews

SRC = Path(__file__).resolve().parents[1] / "src"
_CSRF_RE = re.compile(r'name="_csrf_token" value="([0-9a-f]+)"')


def _login(client, username="admin", password="admin"):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def _install_fake_model(behavior=None):
    from test_detection_queue import _install_fake_model as install
    return install(behavior)


# ------------------------------------------------------------- 初始化缺陷

def test_init_db_creates_missing_directory(tmp_path, monkeypatch):
    """数据目录不存在时初始化应创建目录，而不是报 unable to open database file。"""
    target = tmp_path / "nested" / "data"
    monkeypatch.setattr(db, "DATABASE_DIR", target)
    monkeypatch.setattr(db, "DATABASE_FILE", target / "fakengin.db")
    db.init_db()
    assert (target / "fakengin.db").exists()

    # 已有空目录 / 已有库：重复初始化幂等
    db.init_db()
    assert db.find_user("admin") is not None


def test_init_db_permission_error_not_swallowed(tmp_path, monkeypatch):
    """权限不足时错误按原样抛出（可定位），不吞掉也不伪装成功。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    if os.access(blocked, os.W_OK):
        pytest.skip("当前环境对 0o500 目录仍有写权限（如 root）")
    monkeypatch.setattr(db, "DATABASE_DIR", blocked / "sub")
    monkeypatch.setattr(db, "DATABASE_FILE", blocked / "sub" / "fakengin.db")
    with pytest.raises(PermissionError):
        db.init_db()
    blocked.chmod(0o700)


def test_multiprocess_init_db_creates_single_admin(tmp_path):
    """多进程并发首次初始化：全部成功且 admin 只有一个。"""
    code = (
        "import sys; sys.path.insert(0, {src!r}); "
        "from webapp import db; db.init_db()"
    ).format(src=str(SRC))
    env = dict(os.environ)
    env["FAKENGIN_DATA_DIR"] = str(tmp_path / "data")
    env["FAKENGIN_ADMIN_PASSWORD"] = "admin"
    for key in [k for k in env if k.startswith("MODEL_API_")]:
        del env[key]

    procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(4)]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    env_file = tmp_path / "data" / "fakengin.db"
    conn = __import__("sqlite3").connect(str(env_file))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'admin'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ------------------------------------------------------- 任务所有权与恢复

def test_recover_spares_live_worker_and_reaps_stale(fresh_data_dir):
    """活跃 worker 的任务不被误伤；心跳超时后才回收。"""
    message_id = newsdata.append_message({"content": "活跃任务"})
    detection.enqueue([message_id])
    run_id = detection.runs_for_message(message_id)[0]["id"]
    live = "w-live-test"
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE detection_runs SET status = 'running', worker_id = ? WHERE id = ?",
            (live, run_id))
        conn.execute(
            "INSERT INTO workers (worker_id, started_at, heartbeat_at) VALUES (?, ?, ?)",
            (live, db.now_string(), db.now_string()))

    # 活跃心跳：恢复不碰
    assert detection.recover_interrupted() == 0
    assert detection.runs_for_message(message_id)[0]["status"] == "running"

    # 心跳超时：回收为 interrupted，可重试
    stale = (datetime.now() - timedelta(seconds=detection.WORKER_STALE_SECONDS + 10))
    with db.db_conn() as conn:
        conn.execute("UPDATE workers SET heartbeat_at = ? WHERE worker_id = ?",
                     (stale.strftime(db.TIME_FORMAT), live))
    assert detection.recover_interrupted() == 1
    assert detection.runs_for_message(message_id)[0]["status"] == "interrupted"


def test_recover_reaps_unknown_and_missing_worker(fresh_data_dir):
    """空 worker_id（旧版本遗留）与 worker 记录缺失的任务都会被回收。"""
    m1 = newsdata.append_message({"content": "旧版遗留"})
    m2 = newsdata.append_message({"content": "记录缺失"})
    detection.enqueue([m1])
    detection.enqueue([m2])
    run1 = detection.runs_for_message(m1)[0]["id"]
    run2 = detection.runs_for_message(m2)[0]["id"]
    with db.db_conn() as conn:
        conn.execute("UPDATE detection_runs SET status='running', worker_id='' WHERE id=?",
                     (run1,))
        conn.execute(
            "UPDATE detection_runs SET status='running', worker_id='w-gone' WHERE id=?",
            (run2,))

    assert detection.recover_interrupted() == 2
    assert detection.runs_for_message(m1)[0]["status"] == "interrupted"
    assert detection.runs_for_message(m2)[0]["status"] == "interrupted"


def test_execute_run_respects_foreign_ownership(fresh_data_dir):
    """其他进程认领的 running 任务不会被再次执行；空归属可接管。"""
    message_id = newsdata.append_message({"content": "他人任务"})
    patches, fake = _install_fake_model()
    detection.enqueue([message_id], model_id=fake.name)
    run_id = detection.runs_for_message(message_id)[0]["id"]
    with db.db_conn() as conn:
        conn.execute(
            "UPDATE detection_runs SET status='running', worker_id='w-other' WHERE id=?",
            (run_id,))
    with patches[0], patches[1]:
        assert detection.execute_run(run_id) is None
        assert fake.calls == []

    with db.db_conn() as conn:
        conn.execute("UPDATE detection_runs SET worker_id='' WHERE id=?", (run_id,))
    with patches[0], patches[1]:
        assert detection.execute_run(run_id) == "succeeded"


def test_claim_is_atomic_between_threads(fresh_data_dir):
    """并发认领：一个任务只被一个线程执行一次。"""
    contents = ["并发消息 {}".format(i) for i in range(6)]
    ids = [newsdata.append_message({"content": c}) for c in contents]
    patches, fake = _install_fake_model()
    for mid in ids:
        detection.enqueue([mid], model_id=fake.name)

    executed = []

    def worker():
        while True:
            run_id = detection._claim_one()
            if run_id is None:
                return
            with patches[0], patches[1]:
                detection.execute_run(run_id)
            executed.append(run_id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(executed) == len(set(executed)) == 6
    assert len(fake.calls) == 6
    for mid in ids:
        assert detection.runs_for_message(mid)[0]["status"] == "succeeded"


# ------------------------------------------------------------- 队列口径

def test_done_queue_count_matches_list(app):
    """“已处理”队列的计数与列表使用同一排除条件，未校验消息不再混入。"""
    _login(app)
    newsdata.append_message({"content": "已处理消息甲", "nature": "虚假"})
    newsdata.append_message({"content": "已处理消息乙", "nature": "真实"})
    newsdata.append_message({"content": "待处理消息丙"})

    page = app.get("/verify?queue=done")
    text = page.get_data(as_text=True)
    assert "共 2 条" in text
    assert "已处理消息甲" in text
    assert "已处理消息乙" in text

    page = app.get("/verify?queue=pending")
    text = page.get_data(as_text=True)
    assert "共 1 条" in text


def test_list_messages_filtered_nature_not(fresh_data_dir):
    newsdata.append_message({"content": "甲", "nature": "虚假"})
    newsdata.append_message({"content": "乙"})
    rows = newsdata.list_messages_filtered(nature_not=newsdata.DEFAULT_NATURE)
    assert [r["content"] for r in rows] == ["甲"]
    assert newsdata.count_messages(nature_not=newsdata.DEFAULT_NATURE) == 1


# ------------------------------------------------------------- 展示口径

def test_data_pages_show_detection_result_not_legacy_field(app):
    """检测成功后数据页/滚动加载显示最新检测结果，而不是旧 fake_probability。"""
    _login(app)
    legacy_id = newsdata.append_message(
        {"content": "历史导入消息", "nature": "未校验", "fake_probability": 10.0})
    with db.db_conn() as conn:
        conn.execute("UPDATE messages SET legacy_probability = 1 WHERE id = ?",
                     (legacy_id,))
    detected_id = newsdata.append_message(
        {"content": "模型检测消息", "fake_probability": 5.0})
    patches, fake = _install_fake_model({"模型检测消息": (66.0, "高度可疑")})
    detection.enqueue([detected_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.run_pending()

    page = app.get("/data")
    text = page.get_data(as_text=True)
    assert "66.0%" in text          # 最新检测结果
    assert "5.0%" not in text       # 旧 fake_probability 不再冒充检测结果
    assert "历史导入·来源未知" in text  # 历史数值明确标注来源

    # 滚动加载 JSON 同口径
    payload = json.loads(app.get("/data/more?offset=0").get_data(as_text=True))
    by_id = {row["id"]: row for row in payload["rows"]}
    assert by_id[detected_id]["latest_run"]["status"] == "succeeded"
    assert by_id[detected_id]["latest_run"]["probability"] == 66.0
    assert by_id[legacy_id]["latest_run"] is None
    assert by_id[legacy_id]["legacy_probability"] == 1

    # 详情页保持检测历史 + 过期标记口径
    page = app.get("/data/message/{}".format(detected_id))
    assert "66.0%" in page.get_data(as_text=True)


def test_data_page_marks_stale_run(app):
    """正文修改后列表把旧结果标记为过期，仍显示历史评分。"""
    _login(app)
    message_id = newsdata.append_message({"content": "原始正文"})
    patches, fake = _install_fake_model({"原始正文": (80.0, "旧理由")})
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.run_pending()
    current = newsdata.get_message(message_id)
    newsdata.update_message(message_id, current["version"], {"content": "修改后正文"})

    page = app.get("/data")
    text = page.get_data(as_text=True)
    assert "80.0%" in text
    assert "输入已修改" in text


# ------------------------------------------------------------- 审核一致性

def test_review_rejects_foreign_run_and_stale_version(app):
    """审核关联必须属于该消息；版本变化后旧页面提交被拒绝。"""
    _login(app)
    m1 = newsdata.append_message({"content": "消息一"})
    m2 = newsdata.append_message({"content": "消息二"})
    patches, fake = _install_fake_model()
    detection.enqueue([m1], model_id=fake.name)
    with patches[0], patches[1]:
        detection.run_pending()
    run_id = detection.runs_for_message(m1)[0]["id"]
    v2 = newsdata.get_message(m2)["version"]

    # 跨消息关联被拒
    page = app.post("/verify/review", data={
        "id": m2, "version": v2, "conclusion": "虚假",
        "detection_run_id": run_id,
    }, follow_redirects=True)
    assert "不属于该消息" in page.get_data(as_text=True)
    assert not reviews.reviews_for_message(m2)

    # 版本不匹配被拒（正文或属性已被其他操作修改）
    page = app.post("/verify/review", data={
        "id": m1, "version": v2 + 99, "conclusion": "虚假",
        "detection_run_id": run_id,
    }, follow_redirects=True)
    assert "已被其他操作修改" in page.get_data(as_text=True)
    assert not reviews.reviews_for_message(m1)

    # 正确版本 + 正确归属成功
    v1 = newsdata.get_message(m1)["version"]
    page = app.post("/verify/review", data={
        "id": m1, "version": v1, "conclusion": "证据不足",
        "detection_run_id": run_id,
    }, follow_redirects=True)
    assert "审核记录已保存" in page.get_data(as_text=True)
    history = reviews.reviews_for_message(m1)
    assert history and history[0]["detection_run_id"] == run_id


# ------------------------------------------------------------- 登录限流

@pytest.fixture()
def clean_ratelimit():
    ratelimit._events.clear()
    yield
    ratelimit._events.clear()


def test_login_rate_limit_blocks_after_failures(app, clean_ratelimit):
    """连续失败达到上限后，即使密码正确也暂时拒绝并给出等待提示。"""
    for _ in range(5):
        app.post("/login", data={"username": "admin", "password": "wrong"})
    page = app.post("/login", data={"username": "admin", "password": "admin"})
    text = page.get_data(as_text=True)
    assert "失败次数过多" in text
    assert "账户或密码错误" not in text

    # 其他用户不受影响
    db.create_user("other", "other-pass")
    page = app.post("/login", data={"username": "other", "password": "other-pass"},
                    follow_redirects=True)
    assert page.status_code == 200


def test_detect_rate_limit(app, clean_ratelimit):
    """公开检测入口限流：超过窗口上限后返回 429，不消耗模型。"""
    patches, fake = _install_fake_model()
    with patches[0], patches[1]:
        for _ in range(10):
            page = app.post("/detect", data={"message": "限流测试消息",
                                             "model": fake.name})
            assert page.status_code == 200
        assert len(fake.calls) == 10
        page = app.post("/detect", data={"message": "限流测试消息",
                                         "model": fake.name})
        assert page.status_code == 429
        assert len(fake.calls) == 10  # 被限流的请求没有到达模型


def test_session_expires_after_ttl(app, monkeypatch):
    """登录凭证超过 TTL 后失效：请求视为未登录，过期会话被清理。"""
    from datetime import datetime, timedelta

    from webapp import auth as auth_mod

    _login(app)
    session_id = None
    recorder = {}

    real_set_cookie = auth_mod.set_session_cookie

    def capture_cookie(response, sid):
        recorder["sid"] = sid
        return real_set_cookie(response, sid)

    monkeypatch.setattr(auth_mod, "set_session_cookie", capture_cookie)
    app.post("/logout")
    _login(app)
    session_id = recorder.get("sid")
    assert session_id

    # 把会话创建时间改到 TTL 之前
    expired = (datetime.now() - timedelta(days=auth_mod.SESSION_TTL.days + 1)
               ).strftime(db.TIME_FORMAT)
    with db.db_conn() as conn:
        conn.execute("UPDATE sessions SET created_at = ? WHERE session_id = ?",
                     (expired, session_id))

    page = app.get("/verify", follow_redirects=False)
    assert page.status_code == 302  # 已过期 → 重新登录
    with db.db_conn() as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE session_id = ?",
            (session_id,)).fetchone()["n"]
    assert left == 0  # 过期会话已清理


# ------------------------------------------------------------- 修改密码

def test_change_password_flow(app):
    _login(app)
    page = app.post("/account/password", data={
        "current_password": "错误的原密码",
        "new_password": "new-password-123",
        "confirm_password": "new-password-123",
    }, follow_redirects=True)
    assert "原密码不正确" in page.get_data(as_text=True)

    page = app.post("/account/password", data={
        "current_password": "admin",
        "new_password": "short",
        "confirm_password": "short",
    }, follow_redirects=True)
    assert "至少 8 位" in page.get_data(as_text=True)

    page = app.post("/account/password", data={
        "current_password": "admin",
        "new_password": "new-password-123",
        "confirm_password": "different-123",
    }, follow_redirects=True)
    assert "不一致" in page.get_data(as_text=True)

    page = app.post("/account/password", data={
        "current_password": "admin",
        "new_password": "new-password-123",
        "confirm_password": "new-password-123",
    }, follow_redirects=True)
    assert "密码已修改" in page.get_data(as_text=True)

    app.get("/logout")
    page = app.post("/login", data={"username": "admin", "password": "admin"})
    assert "账户或密码错误" in page.get_data(as_text=True)
    page = _login(app, password="new-password-123")
    assert page.status_code == 200


def test_change_password_requires_login(app):
    page = app.get("/account/password", follow_redirects=False)
    assert page.status_code == 302
    assert "/login" in page.headers["Location"]


# ------------------------------------------------------------- Secure Cookie

def test_session_cookie_secure_flag(fresh_data_dir, monkeypatch):
    monkeypatch.setenv("FAKENGIN_COOKIE_SECURE", "1")
    from webapp import create_app

    client = create_app().test_client()
    token = _CSRF_RE.search(
        client.get("/login").get_data(as_text=True)).group(1)
    resp = client.post("/login", data={
        "username": "admin", "password": "admin", "_csrf_token": token,
    })
    cookie = resp.headers.get("Set-Cookie", "")
    assert "session_id=" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie


# ------------------------------------------------------------- 表单结构

def _assert_no_nested_forms(html_text, page_name):
    """嵌套 form 会被 HTML 解析器提前闭合外层表单，真实浏览器中
    后续提交按钮落在表单外而点击无效——test client 发现不了，必须在
    渲染输出层面禁止。"""
    import re
    text = re.sub(r"\{#.*?#\}", "", html_text, flags=re.S)  # 模板注释不会出现
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    depth = 0
    for m in re.finditer(r"<form\b|</form>", text):
        if m.group(0) == "</form>":
            depth -= 1
            assert depth >= 0, "{}：出现多余的表单闭合标签".format(page_name)
        else:
            assert depth == 0, "{}：出现嵌套表单（真实浏览器中会破坏提交按钮）".format(
                page_name)
            depth += 1
    assert depth == 0, "{}：表单标签不配对".format(page_name)


def test_key_pages_have_no_nested_forms(app):
    """关键页面渲染输出不包含嵌套表单（检测页曾因嵌套 reprobe 表单
    导致真实浏览器中“检测”按钮失效）。"""
    _login(app)
    pages = {
        "数据页": "/data",
        "检测页": "/detect",
        "复核页": "/verify",
        "采集页": "/collect",
    }
    for name, url in pages.items():
        page = app.get(url)
        assert page.status_code == 200, (name, page.status_code)
        _assert_no_nested_forms(page.get_data(as_text=True), name)


def test_detect_submit_button_inside_form(app):
    """检测按钮必须位于 /detect 表单内（防嵌套表单缺陷回归）。"""
    _login(app)
    html = app.get("/detect").get_data(as_text=True)
    import re
    # 提取主表单区间（action 为 /detect 的表单）
    m = re.search(
        r'<form method="post" action="/detect">(.*?)</form>', html, re.S)
    assert m, "未找到 /detect 主表单"
    segment = m.group(1)
    assert 'name="message"' in segment
    assert 'name="save_for_review"' in segment
    assert re.search(r'<button type="submit"[^>]*>检测</button>', segment)
