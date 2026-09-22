"""SQLite 消息存储层回归测试：稳定 ID、乐观并发、事务原子性、导入导出。"""

import pytest

from webapp import db, newsdata


def _msg(content, **kwargs):
    row = {"content": content, "source": "测试数据（合成）"}
    row.update(kwargs)
    return row


def test_append_returns_stable_ids(app):
    id_a = newsdata.append_message(_msg("第一条"))
    id_b = newsdata.append_message(_msg("第二条"))
    assert id_a != id_b and id_b == id_a + 1
    assert newsdata.get_message(id_a)["content"] == "第一条"


def test_empty_content_rejected(app):
    with pytest.raises(ValueError):
        newsdata.append_message(_msg("   "))


def test_update_with_stale_version_rejected(app):
    """多用户并发：旧版本号提交被拒绝，不静默覆盖新修改。"""
    message_id = newsdata.append_message(_msg("原始内容"))
    version = newsdata.get_message(message_id)["version"]

    newsdata.update_message(message_id, version, _msg("用户A的修改"))
    with pytest.raises(ValueError, match="已被其他操作修改"):
        newsdata.update_message(message_id, version, _msg("用户B的旧版本修改"))

    assert newsdata.get_message(message_id)["content"] == "用户A的修改"


def test_delete_with_stale_version_rejected(app):
    message_id = newsdata.append_message(_msg("待删除"))
    with pytest.raises(ValueError):
        newsdata.delete_message(message_id, 999)
    assert newsdata.get_message(message_id) is not None

    version = newsdata.get_message(message_id)["version"]
    newsdata.delete_message(message_id, version)
    assert newsdata.get_message(message_id) is None
    with pytest.raises(ValueError):
        newsdata.delete_message(message_id, version)


def test_failed_transaction_leaves_no_partial_record(app):
    """写事务中途失败时回滚，不留半条记录。"""
    before = newsdata.count_messages()
    with pytest.raises(RuntimeError):
        with db.db_conn() as conn:
            conn.execute(
                "INSERT INTO messages (content, created_at, updated_at) VALUES (?, ?, ?)",
                ("事务中写入", db.now_string(), db.now_string()),
            )
            raise RuntimeError("模拟写入中断")
    assert newsdata.count_messages() == before


def test_time_ordering_missing_time_last(app):
    newsdata.append_message(_msg("没有时间"))
    newsdata.append_message(_msg("较早", publish_time="2026-01-01 08:00:00"))
    newsdata.append_message(_msg("较晚", publish_time="2026-02-01 08:00:00"))

    rows = newsdata.load_all()
    assert [row["content"] for row in rows] == ["较晚", "较早", "没有时间"]

    paged = newsdata.list_messages(2, 0)
    assert [row["content"] for row in paged] == ["较晚", "较早"]


def _write_csv(path, rows):
    import csv
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=newsdata.COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_import_csv_counts_and_duplicates(tmp_path, app):
    csv_path = tmp_path / "batch.csv"
    _write_csv(csv_path, [
        {"content": "消息一", "nature": "", "fake_probability": "", "source": "s",
         "publish_time": "2026-01-01 08:00:00", "process_time": ""},
        {"content": "消息二", "nature": "虚假", "fake_probability": "88.5", "source": "s",
         "publish_time": "", "process_time": ""},
        {"content": "消息一", "nature": "", "fake_probability": "", "source": "s",
         "publish_time": "", "process_time": ""},  # 文件内重复
    ])
    stats = newsdata.import_csv(csv_path)
    assert stats["imported"] == 2 and stats["duplicates"] == 1 and not stats["errors"]

    # 库内重复：再次导入全部跳过
    stats_again = newsdata.import_csv(csv_path)
    assert stats_again["imported"] == 0 and stats_again["duplicates"] == 3


def test_import_csv_atomic_on_error(tmp_path, app):
    """任一行非法 → 整个文件不导入，并给出带行号的错误清单。"""
    good = newsdata.append_message(_msg("已有消息"))
    csv_path = tmp_path / "bad.csv"
    _write_csv(csv_path, [
        {"content": "合法行", "nature": "", "fake_probability": "", "source": "",
         "publish_time": "", "process_time": ""},
        {"content": "时间非法", "nature": "", "fake_probability": "", "source": "",
         "publish_time": "明天上午", "process_time": ""},
        {"content": "概率非法", "nature": "", "fake_probability": "abc", "source": "",
         "publish_time": "", "process_time": ""},
    ])

    stats = newsdata.import_csv(csv_path)
    assert stats["imported"] == 0
    assert [e["row"] for e in stats["errors"]] == [3, 4]
    assert newsdata.count_messages() == 1
    assert newsdata.get_message(good) is not None


def test_import_csv_missing_column_rejected(tmp_path, app):
    bad = tmp_path / "bad_header.csv"
    bad.write_text("content,nature\n消息,真实\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必需列"):
        newsdata.import_csv(bad)


def test_export_csv_roundtrip(tmp_path, app):
    newsdata.append_message(_msg("导出一条", fake_probability="72.5",
                                 publish_time="2026-01-01 08:00:00"))
    export_path = tmp_path / "export.csv"
    count = newsdata.export_csv(export_path)
    assert count == 1

    text = export_path.read_text(encoding="utf-8")
    assert "导出一条" in text and "72.50" in text

    # 导出的文件可直接再导入（去重后为 0 条新增）
    stats = newsdata.import_csv(export_path)
    assert stats["imported"] == 0 and stats["duplicates"] == 1


def test_probability_validation(app):
    # 超范围按既有行为钳制到 0-100；非有限数值拒绝
    clamped_id = newsdata.append_message(_msg("超范围被钳制", fake_probability="150"))
    assert newsdata.get_message(clamped_id)["fake_probability"] == 100.0
    with pytest.raises(ValueError):
        newsdata.append_message(_msg("非有限值", fake_probability="nan"))
    with pytest.raises(ValueError):
        newsdata.append_message(_msg("非数字", fake_probability="abc"))

    message_id = newsdata.append_message(_msg("零分也是有效值", fake_probability="0"))
    assert newsdata.get_message(message_id)["fake_probability"] == 0.0


def test_comments_table(app):
    message_id = newsdata.append_message(_msg("主消息"))
    comment_id = newsdata.add_comment(message_id, "评论一", publish_time="2026-01-01 09:00:00")
    reply_id = newsdata.add_comment(message_id, "回复评论一", parent_id=comment_id,
                                    publish_time="2026-01-01 10:00:00")

    with pytest.raises(ValueError):
        newsdata.add_comment(message_id, "")

    comments = newsdata.list_comments(message_id)
    assert [c["id"] for c in comments] == [comment_id, reply_id]
    assert comments[1]["parent_id"] == comment_id
