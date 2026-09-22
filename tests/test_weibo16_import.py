"""Weibo16 导入与近重复分组划分测试（合成小样本，不依赖 607MB 原始数据包）。"""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

from webapp import db, newsdata

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 两个谣言事件共享的超长前缀（>50 字符），用于验证近重复分组
_SHARED_PREFIX = "这是一条用来测试近重复分组策略的超长前缀正文内容请勿截断使用"


def _load_script():
    path = PROJECT_ROOT / "scripts" / "import_weibo16.py"
    spec = importlib.util.spec_from_file_location("import_weibo16", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_dataset(root, n_events=12):
    """构造仿 Weibo16 结构的数据集：n_events 个事件（前半谣言、后半非谣言）。

    每个事件：源帖 + 5 条转发（回复链、外部 parent、空正文、晚时间各一）。
    事件 0/1 为同前缀的近重复对。
    """
    weibo_dir = Path(root) / "Weibo"
    weibo_dir.mkdir(parents=True)
    lines = []
    for i in range(n_events):
        eid = 1000 + i
        label = 1 if i < n_events // 2 else 0
        if i in (0, 1):
            content = _SHARED_PREFIX + "事件{:02d}独有结尾".format(i)
        elif label == 1:
            content = "网传谣言样例正文{:02d}请勿轻信转发".format(i)
        else:
            content = "官方通报样例正文{:02d}属实已核实".format(i)
        posts = [
            {"mid": "src{}".format(i), "text": content + " http://t.cn/abc",
             "t": 1300000000 + i},
            {"mid": "r1_{}".format(i), "parent": "src{}".format(i),
             "text": "转发评论一{}".format(i), "t": 1300000100 + i},
            {"mid": "r2_{}".format(i), "parent": "r1_{}".format(i),
             "text": "回复转发评论一{}".format(i), "t": 1300000200 + i},
            {"mid": "r3_{}".format(i), "parent": "outside_{}".format(i),
             "text": "外部父节点评论{}".format(i), "t": 1300000300 + i},
            {"mid": "r4_{}".format(i), "parent": "src{}".format(i),
             "text": "   ", "t": 1300000400 + i},
            {"mid": "r5_{}".format(i), "parent": "src{}".format(i),
             "text": "最晚时间评论{}".format(i), "t": 1300009900 + i},
        ]
        (weibo_dir / "{}.json".format(eid)).write_text(
            json.dumps(posts, ensure_ascii=False), encoding="utf-8")
        lines.append("eid:{}\tlabel:{}\t{}".format(eid, label, eid))
    (Path(root) / "Weibo.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(root)


def _run_script(args, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "FAKENGIN_DATA_DIR"}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable] + [str(a) for a in args],
        capture_output=True, text=True, env=env, timeout=180, cwd=str(PROJECT_ROOT))


# ------------------------------------------------------------ 纯函数

def test_clean_text_strips_urls_and_whitespace():
    module = _load_script()
    text = module.clean_text("  正文 http://t.cn/abc 尾部  ")
    assert text == "正文 尾部"
    assert module.clean_text("   ") == ""


def test_build_split_keeps_near_duplicates_together():
    module = _load_script()
    records = [
        {"message_id": 1, "eid": 1, "label": 1, "content": "甲" * 60 + "一"},
        {"message_id": 2, "eid": 2, "label": 1, "content": "甲" * 60 + "二"},
        {"message_id": 3, "eid": 3, "label": 1, "content": "谣言三"},
        {"message_id": 4, "eid": 4, "label": 0, "content": "官方四"},
        {"message_id": 5, "eid": 5, "label": 0, "content": "官方五"},
        {"message_id": 6, "eid": 6, "label": 0, "content": "官方六"},
    ]
    split = module.build_split(records, test_fraction=0.5, seed=7)
    train, test = set(split["train"]), set(split["test"])
    assert not train & test
    assert train | test == {1, 2, 3, 4, 5, 6}
    # 近重复对（1/2）必须整组进入同一侧
    assert (1 in train) == (2 in train)
    assert (1 in test) == (2 in test)
    # 分层：训练侧两类都有
    train_labels = {r["label"] for r in records if r["message_id"] in train}
    assert train_labels == {0, 1}
    assert split["meta"]["counts"]["groups"] == 5


# ------------------------------------------------------------ 导入

def test_run_import_creates_messages_and_comment_tree(fresh_data_dir, tmp_path):
    module = _load_script()
    root = _make_dataset(tmp_path / "in", n_events=6)

    records, stats = module.run_import(root, max_comments=20)
    assert stats["imported"] == 6
    assert stats["rumor"] == 3 and stats["nonrumor"] == 3
    assert stats["comments"] == 6 * 4  # 每事件 4 条有正文转发
    assert len(records) == 6

    rows = {r["id"]: r for r in newsdata.load_all()}
    assert len(rows) == 6
    assert rows[records[0]["message_id"]]["nature"] == "虚假"
    assert rows[records[5]["message_id"]]["nature"] == "真实"
    # URL 被清洗；来源与时间格式正确
    for row in rows.values():
        assert "http" not in row["content"]
        assert row["source"].startswith("Weibo16")
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", row["publish_time"])

    comments = newsdata.list_comments(records[0]["message_id"])
    assert [c["content"] for c in comments] == [
        "转发评论一0", "回复转发评论一0", "外部父节点评论0", "最晚时间评论0"]
    # 回复链：第二条挂在第一条下；外部 parent 与源帖 parent 都作顶层
    assert comments[1]["parent_id"] == comments[0]["id"]
    assert comments[2]["parent_id"] is None
    assert comments[3]["parent_id"] is None


def test_run_import_truncates_comments_by_time(fresh_data_dir, tmp_path):
    module = _load_script()
    root = _make_dataset(tmp_path / "in", n_events=2)
    _records, stats = module.run_import(root, max_comments=2)
    assert stats["comments"] == 2 * 2
    comments = newsdata.list_comments(_records[0]["message_id"])
    # 只保留最早的两条
    assert [c["content"] for c in comments] == ["转发评论一0", "回复转发评论一0"]


def test_run_import_refuses_non_empty_database(fresh_data_dir, tmp_path):
    module = _load_script()
    root = _make_dataset(tmp_path / "in", n_events=2)
    newsdata.append_message({"content": "库里已有消息"})
    with pytest.raises(ValueError, match="已有"):
        module.run_import(root)
    assert newsdata.count_messages() == 1


# ------------------------------------------------------------ CLI

def test_cli_requires_isolated_data_dir(tmp_path):
    root = _make_dataset(tmp_path / "in", n_events=2)
    result = _run_script(["scripts/import_weibo16.py", str(root)])
    assert result.returncode == 1
    assert "隔离" in result.stdout


def test_cli_zip_input_and_split_file(tmp_path):
    root = _make_dataset(tmp_path / "in", n_events=12)
    zip_path = tmp_path / "rumdect_fake.zip"
    with zipfile.ZipFile(zip_path, "w") as bundle:
        for path in sorted(root.rglob("*")):
            bundle.write(path, path.relative_to(root.parent))
    data_dir = tmp_path / "data"

    result = _run_script([
        "scripts/import_weibo16.py", str(zip_path),
        "--data-dir", data_dir, "--test-fraction", "0.25",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "导入完成" in result.stdout

    split_path = data_dir / "weibo16_split.json"
    split = json.loads(split_path.read_text(encoding="utf-8"))
    train, test = set(split["train"]), set(split["test"])
    assert not train & test
    assert len(train | test) == 12
    assert len(test) >= 2

    # zip 解压出的临时目录与数据目录互不影响，消息与评论均已入库
    conn = sqlite3.connect(str(data_dir / "fakengin.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 48
    finally:
        conn.close()





def test_cli_end_to_end_train_with_split(tmp_path):
    """导入 → 按划分训练 → 测试折指标报告 的最小端到端（合成数据）。"""
    root = _make_dataset(tmp_path / "in", n_events=12)
    data_dir = tmp_path / "data"
    result = _run_script([
        "scripts/import_weibo16.py", str(root),
        "--data-dir", data_dir, "--test-fraction", "0.25",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    split_path = data_dir / "weibo16_split.json"

    result = _run_script([
        "scripts/train_tfidf_rnn.py",
        "--split-file", split_path,
        "--epochs", "40", "--hidden", "8",
    ], extra_env={"FAKENGIN_DATA_DIR": str(data_dir)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "外部划分" in result.stdout
    assert "测试集为外部划分" in result.stdout
    assert "混淆矩阵" in result.stdout
    artifact = json.loads(
        (data_dir / "tfidf_rnn.json").read_text(encoding="utf-8"))
    assert artifact["stats"]["test"]["confusion"]["tp"] + \
        artifact["stats"]["test"]["confusion"]["fn"] + \
        artifact["stats"]["test"]["confusion"]["fp"] + \
        artifact["stats"]["test"]["confusion"]["tn"] == \
        artifact["stats"]["test_samples"]
    assert artifact["stats"]["holdout"] is None
    assert "Weibo16" in artifact["stats"]["data_note"]


# ------------------------------------------------------------ 目标隔离校验

def _fake_business_root(tmp_path, monkeypatch, module):
    """构造假的业务目录（fake 项目根 + database/fakengin.db）并替换 PROJECT_ROOT。"""
    fake_root = tmp_path / "fakeproj"
    business = fake_root / "database"
    business.mkdir(parents=True)
    (business / "fakengin.db").write_bytes(b"business db marker")
    (business / "admin_initial_password.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(module, "PROJECT_ROOT", fake_root)
    return business


def test_validate_target_rejects_business_dir(tmp_path, monkeypatch):
    module = _load_script()
    business = _fake_business_root(tmp_path, monkeypatch, module)
    for candidate in (business, business / "sub", business.parent):
        with pytest.raises(ValueError, match="业务"):
            module._validate_target_dir(candidate)
    # 业务目录内容未被触碰
    assert (business / "fakengin.db").read_bytes() == b"business db marker"


def test_validate_target_rejects_symlink_alias(tmp_path, monkeypatch):
    module = _load_script()
    business = _fake_business_root(tmp_path, monkeypatch, module)
    alias = tmp_path / "alias"
    alias.symlink_to(business)
    with pytest.raises(ValueError, match="业务"):
        module._validate_target_dir(alias)
    # 相对路径与 .. 组合的别名同样拒绝
    with pytest.raises(ValueError, match="业务"):
        module._validate_target_dir(
            tmp_path / "fakeproj" / "database" / ".." / "database")


def test_validate_target_rejects_active_env_data_dir(tmp_path, monkeypatch):
    module = _load_script()
    _fake_business_root(tmp_path, monkeypatch, module)
    env_dir = tmp_path / "envdata"
    env_dir.mkdir()
    (env_dir / "fakengin.db").write_bytes(b"env db")
    monkeypatch.setenv("FAKENGIN_DATA_DIR", str(env_dir))
    # 目标位于环境变量指向的在用数据目录之内
    with pytest.raises(ValueError, match="业务"):
        module._validate_target_dir(env_dir / "sub")
    # 与在用目录相同（非空）也拒绝
    with pytest.raises(ValueError, match="空目录"):
        module._validate_target_dir(env_dir)


def test_validate_target_requires_empty_or_missing(tmp_path, monkeypatch):
    module = _load_script()
    _fake_business_root(tmp_path, monkeypatch, module)
    # 已存在但非空
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "keep.txt").write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="空目录"):
        module._validate_target_dir(dirty)
    assert (dirty / "keep.txt").read_text() == "data"
    # 已存在的空目录、不存在的目录均可
    empty = tmp_path / "empty"
    empty.mkdir()
    assert module._validate_target_dir(empty) == empty.resolve()
    fresh = tmp_path / "fresh" / "nested"
    assert module._validate_target_dir(fresh) == fresh.resolve()
    # 存在的普通文件不是合法目标
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="不是目录"):
        module._validate_target_dir(afile)


def test_cli_rejects_nonempty_target_and_bad_params(tmp_path):
    root = _make_dataset(tmp_path / "in", n_events=2)
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "keep.txt").write_text("data", encoding="utf-8")
    cases = [
        ([str(root), "--data-dir", str(dirty)], "空目录"),
        ([str(root), "--data-dir", str(tmp_path / "d2"), "--max-comments", "-1"], "max-comments"),
        ([str(root), "--data-dir", str(tmp_path / "d3"), "--test-fraction", "1.5"], "test-fraction"),
        ([str(root), "--data-dir", str(tmp_path / "d4"), "--test-fraction", "nan"], "有限数值"),
        ([str(root), "--data-dir", str(tmp_path / "d5"), "--limit", "-3"], "limit"),
    ]
    for args, keyword in cases:
        result = _run_script(["scripts/import_weibo16.py"] + args)
        assert result.returncode == 1, args
        assert keyword in result.stdout, (args, result.stdout)
    assert (dirty / "keep.txt").read_text() == "data"


def test_run_import_counts_users_and_sessions_not_only_messages(
        fresh_data_dir, tmp_path):
    """库里没有消息但已有其他业务记录（用户/会话）时同样拒绝导入。"""
    module = _load_script()
    root = _make_dataset(tmp_path / "in", n_events=2)
    from webapp import db as webdb
    webdb.create_user("reviewer1", "pass-123456", role="reviewer")
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, username, created_at) VALUES "
            "('sid-1', 'reviewer1', '2026-09-21 00:00:00')")
    with pytest.raises(ValueError, match="业务记录"):
        module.run_import(root)
    # 只有默认 admin、无会话、无消息的空库不受影响（init_db 正常状态）
    with db.db_conn() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM users WHERE username != 'admin'")
    records, stats = module.run_import(root)
    assert stats["imported"] == 2


# ------------------------------------------------------------ zip 解压预算

def _make_zip(path, entries):
    """entries: [(arcname, bytes 或 None 表示目录)]"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries:
            if data is None:
                bundle.writestr(name + "/", "")
            else:
                bundle.writestr(name, data)


def test_zip_high_compression_ratio_rejected(tmp_path):
    module = _load_script()
    zip_path = tmp_path / "bomb.zip"
    _make_zip(zip_path, [("Weibo.txt", b"id:1\tlabel:1\t1\n"),
                         ("Weibo/1.json", b"\0" * (2 * 1024 * 1024))])
    extract = tmp_path / "out"
    extract.mkdir()
    with pytest.raises(ValueError, match="压缩比"):
        module._extract_zip_capped(zip_path, extract)
    assert not (extract / "Weibo" / "1.json").exists()


def test_zip_member_count_and_total_budget(tmp_path, monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, "ZIP_MAX_MEMBERS", 5)
    zip_path = tmp_path / "many.zip"
    _make_zip(zip_path, [("f{}.txt".format(i), b"x" * 10) for i in range(6)])
    extract = tmp_path / "out"
    extract.mkdir()
    with pytest.raises(ValueError, match="成员数"):
        module._extract_zip_capped(zip_path, extract)

    # 总量预算：把上限压到很小，实际解压累计字节触发
    monkeypatch.setattr(module, "ZIP_MAX_MEMBERS", 100)
    monkeypatch.setattr(module, "ZIP_MAX_TOTAL_BYTES", 100)
    zip_path2 = tmp_path / "total.zip"
    _make_zip(zip_path2, [("a.txt", b"a" * 60), ("b.txt", b"b" * 60)])
    with pytest.raises(ValueError, match="预算"):
        module._extract_zip_capped(zip_path2, extract)


def test_zip_dangerous_members_rejected(tmp_path):
    module = _load_script()
    extract = tmp_path / "out"
    extract.mkdir()
    cases = {
        "traversal": [("../evil.txt", b"x")],
        "absolute": [("/etc/evil.txt", b"x")],
        "backslash": [("dir\\evil.txt", b"x")],
        "nested-archive": [("inner.zip", b"PK\x03\x04junk")],
        "disallowed-type": [("run.exe", b"MZ")],
    }
    for name, entries in cases.items():
        zip_path = tmp_path / "{}.zip".format(name)
        _make_zip(zip_path, entries)
        with pytest.raises(ValueError):
            module._extract_zip_capped(zip_path, extract)
    # 符号链接成员：手工设置 Unix 权限位
    import stat as stat_module
    zip_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zip_path, "w") as bundle:
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = (stat_module.S_IFLNK | 0o777) << 16
        bundle.writestr(info, "Weibo.txt")
    with pytest.raises(ValueError, match="不是普通文件"):
        module._extract_zip_capped(zip_path, extract)
    # 解压目录保持干净（拒绝发生在写入之前）
    assert list(extract.iterdir()) == []


def test_zip_corrupt_package_rejected_with_cleanup(tmp_path):
    module = _load_script()
    zip_path = tmp_path / "bad.zip"
    _make_zip(zip_path, [("Weibo.txt", b"id:1\tlabel:1\t1\n"),
                         ("Weibo/1.json", b"[]")])
    raw = zip_path.read_bytes()
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(raw[:len(raw) // 2])
    extract = tmp_path / "out"
    extract.mkdir()
    before = set(Path(tempfile.gettempdir()).glob("weibo16_extract_*"))
    with pytest.raises(ValueError):
        module._extract_zip_capped(truncated, extract)
    # CLI 路径：解压失败同样不留临时目录残留
    result = _run_script(["scripts/import_weibo16.py", str(truncated),
                          "--data-dir", tmp_path / "d1"])
    assert result.returncode == 1
    after = set(Path(tempfile.gettempdir()).glob("weibo16_extract_*"))
    assert before == after, "解压失败后残留临时目录：{}".format(after - before)


def test_cli_zip_success_leaves_no_extraction_leftovers(tmp_path):
    root = _make_dataset(tmp_path / "in", n_events=2)
    zip_path = tmp_path / "ok.zip"
    with zipfile.ZipFile(zip_path, "w") as bundle:
        for path in sorted(root.rglob("*")):
            bundle.write(path, path.relative_to(root.parent))
    before = set(Path(tempfile.gettempdir()).glob("weibo16_extract_*"))
    result = _run_script(["scripts/import_weibo16.py", str(zip_path),
                          "--data-dir", tmp_path / "data"])
    assert result.returncode == 0, result.stdout + result.stderr
    after = set(Path(tempfile.gettempdir()).glob("weibo16_extract_*"))
    assert before == after, "成功导入后残留解压临时目录：{}".format(after - before)
