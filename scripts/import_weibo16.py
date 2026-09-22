"""Weibo16 真实标注数据集导入（隔离数据目录）。

数据集：Ma et al., "Detecting Rumors from Microblogs with Recurrent Neural
Networks", IJCAI 2016（rumdect 包）。4,664 个事件：谣言 2,313 / 非谣言 2,351；
每个事件的源帖与转发在 Weibo/<eid>.json 中，转发自带 parent（父帖 mid）、
时间戳 t 与正文。学术研究用途，不再分发原始数据。

安全边界：
- 必须用 --data-dir 显式指定隔离数据目录（或已设置 FAKENGIN_DATA_DIR），
  绝不写入默认业务库；目标库已有消息时拒绝导入，防止重复。
- 每事件转发按时间升序截断导入（--max-comments，默认 20）。
- 导入同时生成"近重复分组"划分文件 weibo16_split.json：清洗后正文前
  50 字符为组键，整组进入同一侧，按类别分层——防止同一谣言的变体
  同时出现在训练与测试造成泄漏。

用法：
    python scripts/import_weibo16.py <rumdect解压目录或zip> --data-dir <目录>
        [--max-comments 20] [--test-fraction 0.2] [--seed 20260921] [--limit N]
"""

import argparse
import json
import os
import random
import re
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import db, newsdata  # noqa: E402

DATASET_SOURCE = "Weibo16 数据集（Ma et al., IJCAI 2016）"
SPLIT_FILE_NAME = "weibo16_split.json"
GROUP_KEY_LENGTH = 50
MAX_TEXT_LENGTH = 5000
_URL_RE = re.compile(r"https?://\S+")


def clean_text(text):
    """去掉 URL 与首尾空白，截断到平台正文上限。"""
    text = _URL_RE.sub("", str(text or ""))
    text = " ".join(text.split())
    return text[:MAX_TEXT_LENGTH]


def _time_string(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime(db.TIME_FORMAT)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def parse_events(weibo_root):
    """读 Weibo.txt，返回 [(eid, label, post_ids)]，保持文件顺序。"""
    weibo_root = Path(weibo_root)
    events = []
    with open(weibo_root / "Weibo.txt", "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split("\t")
            if len(parts) < 3:
                continue
            try:
                eid = int(parts[0].split(":", 1)[1])
                label = int(parts[1].split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            post_ids = parts[2].split()
            if label in (0, 1) and post_ids:
                events.append((eid, label, post_ids))
    return events


def _load_event_posts(weibo_root, eid):
    path = Path(weibo_root) / "Weibo" / "{}.json".format(eid)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            posts = json.load(fh)
    except (OSError, ValueError):
        return []
    return posts if isinstance(posts, list) else []


def run_import(weibo_root, max_comments=20, limit=0):
    """把事件导入当前数据目录（须为空库），返回 (records, stats)。

    records 供划分使用：[{"message_id", "eid", "label", "content"}]。
    """
    events = parse_events(weibo_root)
    if limit > 0:
        events = events[:limit]

    records = []
    skipped = {"missing_file": 0, "empty_source": 0}
    comment_count = 0
    with db.db_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        if existing:
            raise ValueError(
                "目标库已有 {} 条消息：请换用空的隔离数据目录，避免重复导入".format(existing))
        for eid, label, _post_ids in events:
            posts = _load_event_posts(weibo_root, eid)
            if not posts:
                skipped["missing_file"] += 1
                continue
            source_text = clean_text(posts[0].get("text"))
            if not source_text:
                skipped["empty_source"] += 1
                continue
            row = newsdata.normalize_row({
                "content": source_text,
                "nature": "虚假" if label == 1 else "真实",
                "fake_probability": "",
                "source": DATASET_SOURCE,
                "publish_time": _time_string(posts[0].get("t")),
                "process_time": "",
            })
            message_id = newsdata.insert_message_in_tx(conn, row, {})

            # 转发按时间升序截断；文本清洗后为空的跳过
            reposts = []
            for post in posts[1:]:
                text = clean_text(post.get("text"))
                if not text:
                    continue
                try:
                    order = int(post.get("t") or 0)
                except (TypeError, ValueError):
                    order = 0
                reposts.append((order, post, text))
            reposts.sort(key=lambda item: item[0])

            now = db.now_string()
            mid_to_comment = {}
            for _order, post, text in reposts[:max_comments]:
                # parent 指向已导入转发则挂其下；指向源帖或未导入帖则作顶层评论
                parent_cid = mid_to_comment.get(str(post.get("parent") or ""))
                cursor = conn.execute(
                    "INSERT INTO comments (message_id, parent_id, content, "
                    "publish_time, created_at) VALUES (?, ?, ?, ?, ?)",
                    (message_id, parent_cid, text,
                     _time_string(post.get("t")), now),
                )
                mid_to_comment[str(post.get("mid"))] = cursor.lastrowid
                comment_count += 1
            records.append({
                "message_id": message_id, "eid": eid,
                "label": label, "content": source_text,
            })
        newsdata._bump_data_version(conn)

    stats = {
        "events": len(events),
        "imported": len(records),
        "comments": comment_count,
        "rumor": sum(1 for r in records if r["label"] == 1),
        "nonrumor": sum(1 for r in records if r["label"] == 0),
        "skipped": skipped,
    }
    return records, stats


def _group_key(content):
    """近重复组键：清洗后正文前 50 字符（去空白、小写）。"""
    text = _URL_RE.sub("", str(content or ""))
    text = re.sub(r"\s+", "", text)
    return text[:GROUP_KEY_LENGTH].lower()


def build_split(records, test_fraction=0.2, seed=20260921):
    """按近重复分组 + 类别分层划分训练/测试，整组进同一侧。"""
    groups = {}
    for record in records:
        groups.setdefault(_group_key(record["content"]), []).append(record)

    rng = random.Random(seed)
    train, test = [], []
    for label in (1, 0):
        label_groups = [g for g in groups.values() if g[0]["label"] == label]
        rng.shuffle(label_groups)
        class_total = sum(len(g) for g in label_groups)
        target = int(round(class_total * test_fraction)) if label_groups else 0
        taken = 0
        # 至少给训练侧留一组
        for index, group in enumerate(label_groups):
            if taken < target and index < len(label_groups) - 1:
                test.extend(group)
                taken += len(group)
            else:
                train.extend(group)

    return {
        "format": "fakengin-split",
        "version": 1,
        "train": [r["message_id"] for r in train],
        "test": [r["message_id"] for r in test],
        "meta": {
            "source": DATASET_SOURCE,
            "strategy": "近重复分组划分：清洗后正文前 50 字符为组键，整组进同一侧；按类别分层",
            "test_fraction": test_fraction,
            "seed": seed,
            "counts": {
                "events": len(records),
                "train": len(train),
                "test": len(test),
                "rumor": sum(1 for r in records if r["label"] == 1),
                "nonrumor": sum(1 for r in records if r["label"] == 0),
                "groups": len(groups),
            },
            "event_ids": {str(r["message_id"]): r["eid"] for r in records},
            "imported_at": db.now_string(),
        },
    }


def _resolve_weibo_root(source):
    """输入可以是解压目录或 zip 包；zip 解压到临时目录后返回其根。"""
    source = Path(source)
    if source.is_dir():
        if not (source / "Weibo.txt").exists():
            raise ValueError("目录中未找到 Weibo.txt：{}".format(source))
        return source
    if source.is_file() and source.suffix == ".zip":
        target = Path(tempfile.mkdtemp(prefix="weibo16_extract_"))
        with zipfile.ZipFile(source) as bundle:
            bundle.extractall(target)
        for candidate in (target, *target.iterdir()):
            if (candidate / "Weibo.txt").exists():
                return candidate
        raise ValueError("zip 中未找到 Weibo.txt")
    raise ValueError("输入必须是 rumdect 解压目录或 zip 包：{}".format(source))


def _point_data_dir(data_dir):
    """把数据层指向隔离目录（与测试夹具同一方式）。"""
    db.DATABASE_DIR = Path(data_dir)
    db.DATABASE_FILE = db.DATABASE_DIR / "fakengin.db"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Weibo16 数据集导入（隔离目录）")
    parser.add_argument("source", help="rumdect 解压目录或 zip 包路径")
    parser.add_argument("--data-dir", default="",
                        help="隔离数据目录（或预先设置 FAKENGIN_DATA_DIR）")
    parser.add_argument("--max-comments", type=int, default=20,
                        help="每事件导入的转发上限（按时间升序取最早，默认 20）")
    parser.add_argument("--test-fraction", type=float, default=0.2,
                        help="测试侧样本比例（按近重复分组整组划分，默认 0.2）")
    parser.add_argument("--seed", type=int, default=20260921, help="划分随机种子")
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 个事件（试跑用）")
    args = parser.parse_args(argv)

    data_dir = args.data_dir or (os.getenv("FAKENGIN_DATA_DIR") or "").strip()
    if not data_dir:
        print("请用 --data-dir 指定隔离数据目录（或设置 FAKENGIN_DATA_DIR），"
              "避免写入业务库。")
        return 1

    try:
        weibo_root = _resolve_weibo_root(args.source)
        _point_data_dir(data_dir)
        db.init_db()
        records, stats = run_import(weibo_root, args.max_comments, args.limit)
        split = build_split(records, args.test_fraction, args.seed)
    except ValueError as exc:
        print(str(exc))
        return 1

    split_path = Path(data_dir) / SPLIT_FILE_NAME
    split_path.write_text(
        json.dumps(split, ensure_ascii=False, indent=2), encoding="utf-8")

    print("导入完成：事件 {}（导入 {}，谣言 {} / 非谣言 {}），评论 {} 条".format(
        stats["events"], stats["imported"], stats["rumor"],
        stats["nonrumor"], stats["comments"]))
    if stats["skipped"]["missing_file"] or stats["skipped"]["empty_source"]:
        print("跳过：缺 JSON {} 个，源帖正文为空 {} 个".format(
            stats["skipped"]["missing_file"], stats["skipped"]["empty_source"]))
    counts = split["meta"]["counts"]
    print("划分（近重复分组，整组同侧）：训练 {} / 测试 {}（分组 {}，比例 {:.0%}）".format(
        counts["train"], counts["test"], counts["groups"], args.test_fraction))
    print("划分文件：{}".format(split_path))
    print("下一步：python scripts/train_tfidf_rnn.py --split-file {} "
          "--data-note {}".format(split_path, "\"Weibo16（Ma et al. IJCAI 2016）\""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
