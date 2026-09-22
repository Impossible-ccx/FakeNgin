"""训练脚本的外部划分文件严格校验测试（小型合成数据，不启动长训练）。"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from webapp import db, newsdata

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = PROJECT_ROOT / "scripts" / "train_tfidf_rnn.py"
    spec = importlib.util.spec_from_file_location("train_tfidf_rnn_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_labeled(n_fake=6, n_real=6):
    """写入带结论的消息，返回 [(id, nature)]。"""
    rows = []
    for i in range(n_fake):
        mid = newsdata.append_message(
            {"content": "网传谣言样例{:02d}请勿轻信".format(i), "nature": "虚假"})
        rows.append((mid, "虚假"))
    for i in range(n_real):
        mid = newsdata.append_message(
            {"content": "官方通报样例{:02d}已核实".format(i), "nature": "真实"})
        rows.append((mid, "真实"))
    return rows


def _split_dict(train, test, version=2, fingerprint=None):
    split = {"format": "fakengin-split", "version": version,
             "train": train, "test": test,
             "meta": {"source": "合成划分（测试）"}}
    if fingerprint:
        split["meta"]["dataset_fingerprint"] = fingerprint
    return split


def _write_split(tmp_path, split, name="split.json"):
    path = tmp_path / name
    path.write_text(json.dumps(split), encoding="utf-8")
    return path


def _fingerprint_for(ids, rows_by_id):
    return newsdata.dataset_fingerprint(
        [{"id": mid, "label": 1 if rows_by_id[mid] == "虚假" else 0,
          "content": newsdata.get_message(mid)["content"]} for mid in ids])


def _run_cli(args, data_dir):
    env = {k: v for k, v in __import__("os").environ.items()
           if k != "FAKENGIN_DATA_DIR"}
    env["FAKENGIN_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable] + [str(a) for a in args],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(PROJECT_ROOT))


# ------------------------------------------------------------- 结构校验

def test_split_rejects_intersection_duplicate_empty_and_types(fresh_data_dir,
                                                              tmp_path):
    module = _load_script()
    rows = _seed_labeled(4, 4)
    ids = [mid for mid, _ in rows]
    cases = {
        "intersection": _split_dict(ids[:4], ids[2:5], version=1),
        "duplicate": _split_dict(ids[:3] + [ids[0]], ids[3:6], version=1),
        "empty-test": _split_dict(ids[:6], [], version=1),
        "bad-type": _split_dict([str(mid) for mid in ids[:6]], ids[6:], version=1),
        "bool-id": _split_dict([True] + ids[1:6], ids[6:], version=1),
        "bad-format": {"format": "other", "version": 1, "train": ids, "test": ids},
        "bad-version": _split_dict(ids[:6], ids[6:], version=99),
    }
    for name, split in cases.items():
        path = _write_split(tmp_path, split, name + ".json")
        with pytest.raises(module.SplitFileError):
            module._load_split_file(path), name


def test_split_rejects_unknown_ids(fresh_data_dir, tmp_path):
    module = _load_script()
    rows = _seed_labeled(4, 4)
    train, test = _mixed_sides(rows)
    path = _write_split(
        tmp_path, _split_dict(train + [max(mid for mid, _ in rows) + 100],
                              test, version=1))
    split, train_ids, test_ids, sha = module._load_split_file(path)
    labeled = module._load_labeled_rows()
    with pytest.raises(module.SplitFileError, match="不存在"):
        module._validate_split(split, train_ids, test_ids, labeled, "sequence")


def test_split_rejects_missing_class(fresh_data_dir, tmp_path):
    module = _load_script()
    rows = _seed_labeled(4, 4)
    fake_ids = [mid for mid, n in rows if n == "虚假"]
    real_ids = [mid for mid, n in rows if n == "真实"]
    # 训练侧只有虚假类
    path = _write_split(tmp_path, _split_dict(fake_ids, real_ids, version=1))
    assert fake_ids and real_ids
    split, train_ids, test_ids, _ = module._load_split_file(path)
    with pytest.raises(module.SplitFileError, match="训练侧缺少类别"):
        module._validate_split(split, train_ids, test_ids,
                               module._load_labeled_rows(), "sequence")


def test_split_v2_fingerprint_mismatch_rejected(fresh_data_dir, tmp_path):
    """版本 2 划分：内容被改动后指纹不匹配，拒绝训练。"""
    module = _load_script()
    rows = _seed_labeled(4, 4)
    train, test = _mixed_sides(rows)
    rows_by_id = dict(rows)
    good_fp = _fingerprint_for(train + test, rows_by_id)
    path = _write_split(
        tmp_path, _split_dict(train, test, version=2, fingerprint=good_fp))
    split, train_ids, test_ids, _ = module._load_split_file(path)
    labeled = module._load_labeled_rows()
    _pairs, _test, report, info = module._validate_split(
        split, train_ids, test_ids, labeled, "sequence")
    assert info["dataset_fingerprint"] == good_fp

    # 修改一条消息内容 → 指纹变化 → 拒绝
    changed = train[0]
    current = newsdata.get_message(changed)
    newsdata.update_message(changed, current["version"],
                            {"content": "被改动的正文"})
    with pytest.raises(module.SplitFileError, match="指纹不匹配"):
        module._validate_split(split, train_ids, test_ids,
                               module._load_labeled_rows(), "sequence")


def _mixed_sides(rows):
    """按类别各半分到两侧，保证两侧类别均衡。"""
    fakes = [mid for mid, n in rows if n == "虚假"]
    reals = [mid for mid, n in rows if n == "真实"]
    half = min(len(fakes), len(reals)) // 2
    train, test = [], []
    for i in range(half):
        train += [fakes[i], reals[i]]
        test += [fakes[half + i], reals[half + i]]
    return train, test


def test_split_v1_skips_fingerprint_with_warning(fresh_data_dir, tmp_path):
    module = _load_script()
    rows = _seed_labeled(4, 4)
    train, test = _mixed_sides(rows)
    path = _write_split(tmp_path, _split_dict(train, test, version=1))
    split, train_ids, test_ids, _ = module._load_split_file(path)
    _pairs, _test, report, _info = module._validate_split(
        split, train_ids, test_ids, module._load_labeled_rows(), "sequence")
    assert any("跳过身份校验" in line for line in report)


def test_split_reports_uncovered_labeled_rows(fresh_data_dir, tmp_path):
    """覆盖不全时明确报告排除数量，不静默丢弃。"""
    module = _load_script()
    rows = _seed_labeled(6, 6)
    train_all, test_all = _mixed_sides(rows)
    covered = train_all[:2] + test_all[:2] + [train_all[2], test_all[3]]
    train, test = covered[:4], covered[4:]
    rows_by_id = dict(rows)
    fp = _fingerprint_for(covered, rows_by_id)
    path = _write_split(
        tmp_path, _split_dict(train, test, version=2, fingerprint=fp))
    split, train_ids, test_ids, _ = module._load_split_file(path)
    _pairs, _test, report, info = module._validate_split(
        split, train_ids, test_ids, module._load_labeled_rows(), "sequence")
    assert info["uncovered_labeled_rows"] == 6
    assert any("6 条带结论消息不在划分" in line for line in report)


# ------------------------------------------------------------- CLI 行为

def test_cli_content_input_mode_trains_and_records(fresh_data_dir, tmp_path):
    """--input content（正文消融）可通过明确参数复现并写入工件。"""
    rows = _seed_labeled(8, 8)
    train, test = _mixed_sides(rows)
    rows_by_id = dict(rows)
    fp = _fingerprint_for(train + test, rows_by_id)
    path = _write_split(
        tmp_path, _split_dict(train, test, version=2, fingerprint=fp))
    result = _run_cli(
        ["scripts/train_tfidf_rnn.py", "--split-file", path,
         "--input", "content", "--epochs", "5", "--hidden", "4"],
        fresh_data_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "仅正文（消融口径）" in result.stdout
    artifact = json.loads(
        (fresh_data_dir / "tfidf_rnn.json").read_text(encoding="utf-8"))
    assert artifact["input_mode"] == "content"
    assert artifact["stats"]["split"]["input_mode"] == "content"
    assert artifact["stats"]["split"]["sha256"]
    assert artifact["stats"]["split"]["dataset_fingerprint"] == fp


def test_cli_rejects_bad_split_and_keeps_old_artifact(fresh_data_dir, tmp_path):
    """校验失败时不写工件、不动已有数据。"""
    rows = _seed_labeled(4, 4)
    train, test = _mixed_sides(rows)
    bad = _write_split(
        tmp_path, _split_dict(train + [test[0]], test, version=1),
        name="bad.json")  # test[0] 出现在两侧
    result = _run_cli(
        ["scripts/train_tfidf_rnn.py", "--split-file", bad,
         "--epochs", "5", "--hidden", "4"],
        fresh_data_dir)
    assert result.returncode == 1
    assert "交集" in result.stdout
    assert not (fresh_data_dir / "tfidf_rnn.json").exists()
