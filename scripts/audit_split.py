"""划分质量只读审计：在不改动任何数据的前提下检查泄漏风险信号。

用法：
    python scripts/audit_split.py --data-dir <隔离数据目录>
        [--split <划分文件>] [--output <报告json>]

审计项（全部只读，输出只含计数与方法，不打印原始用户文本）：
1. 两侧样本与类别计数；
2. 正文精确重复：同侧与跨侧数量（跨侧重复 = 明确泄漏信号）；
3. 近重复分组跨侧：按划分同款组键（清洗后前 50 字符）检查是否有组
   同时出现在两侧（按构造应为 0）；
4. 更细粒度前缀（前 16 字符）跨侧分桶：估计组键未能覆盖的近似变体
   跨侧规模（上界信号，不代表全部为泄漏）；
5. 组内标签冲突：同组键但人工结论不同的消息数量；
6. 评论跨侧引用：评论正文前缀与另一侧消息组键相同的数量（近似信号）；
7. 内容长度分布（最小/中位/最大）与样本 ID 抽查清单。

边界说明：
- 只读打开数据库（SQLite mode=ro）；不写入、不重划分、不重训练；
- 审计发现疑似泄漏时，应保留当前实验为旧版本并如实说明局限，
  不得静默重划分后把新结果当作同一次实验。
"""

import argparse
import json
import os
import random
import re
import sqlite3
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

GROUP_KEY_LENGTH = 50
FINE_PREFIX_LENGTH = 16
SAMPLE_PER_SIDE = 5
_URL_RE = re.compile(r"https?://\S+")


def _group_key(content):
    text = _URL_RE.sub("", str(content or ""))
    text = re.sub(r"\s+", "", text)
    return text[:GROUP_KEY_LENGTH].lower()


def _fine_prefix(content):
    text = _URL_RE.sub("", str(content or ""))
    text = re.sub(r"\s+", "", text)
    return text[:FINE_PREFIX_LENGTH].lower()


def _refuse_business_dirs(data_dir):
    """与导入工具同一业务目录识别规则：只读也不指向业务目录。"""
    target = Path(os.path.expanduser(str(data_dir))).resolve(strict=False)
    protected = [Path(PROJECT_ROOT / "database").resolve(strict=False)]
    env_dir = (os.getenv("FAKENGIN_DATA_DIR") or "").strip()
    if env_dir:
        env_resolved = Path(os.path.expanduser(env_dir)).resolve(strict=False)
        if env_resolved != target and (env_resolved / "fakengin.db").exists():
            protected.append(env_resolved)
    for business in protected:
        if target == business or business in target.parents \
                or target in business.parents:
            raise ValueError(
                "拒绝审计业务数据目录（{}）：请指向隔离的数据目录".format(business))
    return target


def _open_readonly(db_path):
    if not db_path.exists():
        raise ValueError("数据库不存在：{}".format(db_path))
    conn = sqlite3.connect(
        "file:{}?mode=ro".format(db_path.as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_split(path):
    split = json.loads(Path(path).read_text(encoding="utf-8"))
    if split.get("format") != "fakengin-split":
        raise ValueError("划分文件格式不正确：{}".format(path))
    return split


def run_audit(data_dir, split_path):
    data_dir = _refuse_business_dirs(data_dir)
    split = _load_split(split_path)
    train_ids = set(split["train"])
    test_ids = set(split["test"])
    if train_ids & test_ids:
        raise ValueError("划分文件 train/test 存在交集，先修复划分")

    conn = _open_readonly(data_dir / "fakengin.db")
    try:
        messages = conn.execute(
            "SELECT id, content, nature FROM messages "
            "WHERE nature IN ('虚假', '真实')").fetchall()
        comment_count, comment_refs = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT message_id) FROM comments"
        ).fetchone()
    finally:
        conn.close()

    by_id = {row["id"]: row for row in messages}
    unknown = (train_ids | test_ids) - set(by_id)
    if unknown:
        raise ValueError("划分包含 {} 个库中不存在的 ID，数据与划分不匹配".format(
            len(unknown)))

    def _side(mid):
        return "train" if mid in train_ids else "test"

    # 1. 计数
    counts = {
        "labeled_messages": len(messages),
        "train": len(train_ids), "test": len(test_ids),
        "train_fake": sum(1 for m in train_ids if by_id[m]["nature"] == "虚假"),
        "train_real": sum(1 for m in train_ids if by_id[m]["nature"] == "真实"),
        "test_fake": sum(1 for m in test_ids if by_id[m]["nature"] == "虚假"),
        "test_real": sum(1 for m in test_ids if by_id[m]["nature"] == "真实"),
        "comments_total": comment_count,
        "messages_with_comments": comment_refs,
    }

    # 2. 正文精确重复
    content_map = {}
    for row in messages:
        content_map.setdefault(row["content"], []).append(row["id"])
    dup_groups = [ids for ids in content_map.values() if len(ids) > 1]
    dup_within = sum(len(ids) - 1 for ids in dup_groups)
    dup_cross = sum(
        1 for ids in dup_groups
        if len({_side(mid) for mid in ids}) > 1)
    dup_cross_messages = sum(
        len(ids) for ids in dup_groups if len({_side(mid) for mid in ids}) > 1)

    # 3. 组键跨侧（按构造应为 0）
    group_map = {}
    for row in messages:
        group_map.setdefault(_group_key(row["content"]), []).append(row["id"])
    groups_cross = [g for g in group_map.values()
                    if len({_side(mid) for mid in g}) > 1]
    # 5. 组内标签冲突
    label_conflicts = sum(
        1 for g in group_map.values()
        if len({by_id[mid]["nature"] for mid in g}) > 1)

    # 4. 细粒度前缀跨侧分桶（近似变体上界信号）
    fine_map = {}
    for row in messages:
        fine_map.setdefault(_fine_prefix(row["content"]), []).append(row["id"])
    fine_cross_buckets = [g for g in fine_map.values()
                          if len({_side(mid) for mid in g}) > 1]
    fine_cross_messages = sum(len(g) for g in fine_cross_buckets)

    # 6. 评论跨侧引用（评论正文前缀命中另一侧消息的组键）
    group_key_to_side = {key: {_side(mid) for mid in g}
                         for key, g in group_map.items()}
    conn = _open_readonly(data_dir / "fakengin.db")
    try:
        comments = conn.execute("SELECT message_id, content FROM comments").fetchall()
    finally:
        conn.close()
    comment_cross_refs = 0
    for comment in comments:
        parent_side = _side(comment["message_id"]) \
            if comment["message_id"] in by_id else None
        if parent_side is None:
            continue
        key = _group_key(comment["content"])
        sides = group_key_to_side.get(key)
        if sides and sides - {parent_side}:
            comment_cross_refs += 1

    # 7. 长度分布与抽查
    def _lengths(ids):
        values = sorted(len(by_id[mid]["content"]) for mid in ids)
        if not values:
            return {"min": 0, "median": 0, "max": 0}
        return {"min": values[0], "median": statistics.median(values),
                "max": values[-1]}

    rng = random.Random(20260921)
    sample = {
        "train": sorted(rng.sample(sorted(train_ids),
                                   min(SAMPLE_PER_SIDE, len(train_ids)))),
        "test": sorted(rng.sample(sorted(test_ids),
                                  min(SAMPLE_PER_SIDE, len(test_ids)))),
    }

    return {
        "data_dir": str(data_dir),
        "split_file": str(split_path),
        "counts": counts,
        "exact_duplicates": {
            "within_side_extra_messages": dup_within,
            "cross_side_groups": dup_cross,
            "cross_side_messages": dup_cross_messages,
        },
        "group_key_cross_side": {
            "groups": len(groups_cross),
            "messages": sum(len(g) for g in groups_cross),
            "note": "组键=清洗后正文前50字符；非零表示近重复分组未生效",
        },
        "label_conflicts_within_groups": label_conflicts,
        "fine_prefix_cross_side": {
            "buckets": len(fine_cross_buckets),
            "messages": fine_cross_messages,
            "note": "前16字符分桶的跨侧上界信号；非零提示组键之外的近似变体，"
                    "需人工抽查确认",
        },
        "comment_cross_side_references": comment_cross_refs,
        "content_length": {"train": _lengths(train_ids),
                           "test": _lengths(test_ids)},
        "sample_ids_for_manual_check": sample,
        "methods": {
            "group_key": "清洗（去URL/空白）后正文前50字符小写",
            "fine_prefix": "清洗后正文前16字符小写",
            "database_access": "SQLite 只读模式（mode=ro）",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="划分质量只读审计")
    parser.add_argument("--data-dir", required=True, help="隔离数据目录")
    parser.add_argument("--split", default="",
                        help="划分文件（默认 <data-dir>/weibo16_split.json）")
    parser.add_argument("--output", default="", help="审计报告 JSON 输出路径")
    args = parser.parse_args(argv)

    try:
        split_path = Path(args.split) if args.split else \
            Path(args.data_dir) / "weibo16_split.json"
        report = run_audit(args.data_dir, split_path)
    except ValueError as exc:
        print(str(exc))
        return 1

    lines = [
        "划分只读审计：{}（划分 {}）".format(report["data_dir"],
                                        report["split_file"]),
        "样本：训练 {}（虚假 {} / 真实 {}），测试 {}（虚假 {} / 真实 {}）".format(
            report["counts"]["train"], report["counts"]["train_fake"],
            report["counts"]["train_real"], report["counts"]["test"],
            report["counts"]["test_fake"], report["counts"]["test_real"]),
        "正文精确重复：同侧多出的重复 {} 条；跨侧重复组 {}（涉及 {} 条）".format(
            report["exact_duplicates"]["within_side_extra_messages"],
            report["exact_duplicates"]["cross_side_groups"],
            report["exact_duplicates"]["cross_side_messages"]),
        "近重复组键跨侧：{} 组（{} 条）——按构造应为 0".format(
            report["group_key_cross_side"]["groups"],
            report["group_key_cross_side"]["messages"]),
        "组内标签冲突：{} 组".format(report["label_conflicts_within_groups"]),
        "细粒度前缀跨侧：{} 桶（{} 条，上界信号，需人工抽查）".format(
            report["fine_prefix_cross_side"]["buckets"],
            report["fine_prefix_cross_side"]["messages"]),
        "评论跨侧引用：{} 条".format(report["comment_cross_side_references"]),
        "内容长度：训练 {}，测试 {}".format(
            report["content_length"]["train"], report["content_length"]["test"]),
    ]
    print("\n".join(lines))

    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("报告已写入：{}".format(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
