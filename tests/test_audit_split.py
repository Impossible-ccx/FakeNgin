"""划分只读审计工具测试（合成数据，不依赖真实数据包）。"""

import importlib.util
import json
from pathlib import Path

import pytest

from webapp import newsdata

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = PROJECT_ROOT / "scripts" / "audit_split.py"
    spec = importlib.util.spec_from_file_location("audit_split", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_split(tmp_path, train_ids, test_ids):
    split = {"format": "fakengin-split", "version": 1,
             "train": train_ids, "test": test_ids, "meta": {}}
    path = tmp_path / "weibo16_split.json"
    path.write_text(json.dumps(split), encoding="utf-8")
    return path


def test_audit_detects_cross_side_signals(fresh_data_dir, tmp_path):
    """审计工具能发现：跨侧重复、细粒度近似、组内标签冲突与评论引用。"""
    module = _load_script()

    prefix50 = "超长前缀" * 13  # 52 字符，超过组键长度
    prefix16 = "细粒度前缀" * 4  # 20 字符，超过细前缀长度
    ids = {}
    ids["a"] = newsdata.append_message({"content": prefix50 + "甲", "nature": "虚假"})
    # 与 a 同组键（前 50 相同）不同侧 → 组键跨侧
    ids["b"] = newsdata.append_message({"content": prefix50 + "乙", "nature": "真实"})
    # 与 a 精确重复、同侧 → 同侧重复
    ids["c"] = newsdata.append_message({"content": prefix50 + "甲", "nature": "虚假"})
    # 与 d 共享前 16 字符但 50 内分叉、不同侧 → 细前缀跨侧
    ids["d"] = newsdata.append_message({"content": prefix16 + "丁", "nature": "虚假"})
    ids["e"] = newsdata.append_message({"content": prefix16 + "戊", "nature": "真实"})
    # 组内标签冲突：f 与 a 同组键（前 50 相同）但标签不同、同侧
    ids["f"] = newsdata.append_message({"content": prefix50 + "己", "nature": "真实"})
    # 普通消息与评论引用：g 的评论正文与另一侧消息 a 内容相同
    ids["g"] = newsdata.append_message({"content": "普通消息内容", "nature": "真实"})
    newsdata.add_comment(ids["g"], prefix50 + "甲",
                         publish_time="2026-09-01 10:00:00")

    # 分侧：a,c,f 训练；b,d,e,g 测试（制造上述跨侧信号）
    split_path = _write_split(
        tmp_path, [ids["a"], ids["c"], ids["f"]],
        [ids["b"], ids["d"], ids["e"], ids["g"]])
    report = module.run_audit(fresh_data_dir, split_path)

    assert report["counts"]["labeled_messages"] == 7
    assert report["counts"]["train"] == 3 and report["counts"]["test"] == 4
    # 精确重复跨侧：a 与 b? 内容不同；a 与 c 同侧。精确跨侧 = 0 组
    assert report["exact_duplicates"]["cross_side_groups"] == 0
    assert report["exact_duplicates"]["within_side_extra_messages"] == 1  # a/c
    # 组键跨侧：a,c,f（前 50 相同）跨两侧 → 非零
    assert report["group_key_cross_side"]["groups"] >= 1
    # 组内标签冲突：a/c 虚假 vs f 真实，同组
    assert report["label_conflicts_within_groups"] >= 1
    # 细前缀跨侧：d/e 共享前 16、不同侧
    assert report["fine_prefix_cross_side"]["buckets"] >= 1
    assert report["fine_prefix_cross_side"]["messages"] >= 2
    # 评论引用：g（测试侧）的评论正文命中训练侧消息 a 的组键
    assert report["comment_cross_side_references"] >= 1
    # 报告不含原始文本（只含计数与方法）
    serialized = json.dumps(report, ensure_ascii=False)
    assert "超长前缀" not in serialized and "普通消息内容" not in serialized


def test_audit_rejects_intersect_and_unknown(fresh_data_dir, tmp_path):
    module = _load_script()
    mid = newsdata.append_message({"content": "唯一消息", "nature": "虚假"})
    dup_path = _write_split(tmp_path, [mid], [mid])
    with pytest.raises(ValueError, match="交集"):
        module.run_audit(fresh_data_dir, dup_path)
    unknown_path = _write_split(tmp_path, [mid], [mid + 100])
    with pytest.raises(ValueError, match="不存在"):
        module.run_audit(fresh_data_dir, unknown_path)


def test_audit_rejects_business_dir(tmp_path, monkeypatch):
    module = _load_script()
    fake_root = tmp_path / "fakeproj"
    business = fake_root / "database"
    business.mkdir(parents=True)
    (business / "fakengin.db").write_bytes(b"db")
    monkeypatch.setattr(module, "PROJECT_ROOT", fake_root)
    with pytest.raises(ValueError, match="业务"):
        module.run_audit(business, tmp_path / "split.json")


def test_audit_is_readonly(fresh_data_dir, tmp_path):
    """审计不改动数据库：两次审计结果一致，无 journal/wal 残留。"""
    module = _load_script()
    a = newsdata.append_message({"content": "训练消息", "nature": "虚假"})
    b = newsdata.append_message({"content": "测试消息", "nature": "真实"})
    split_path = _write_split(tmp_path, [a], [b])
    db_file = fresh_data_dir / "fakengin.db"
    size_before = db_file.stat().st_size

    first = module.run_audit(fresh_data_dir, split_path)
    second = module.run_audit(fresh_data_dir, split_path)
    assert first["counts"] == second["counts"]
    assert db_file.stat().st_size == size_before
    assert not list(fresh_data_dir.glob("fakengin.db-journal"))
