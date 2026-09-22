"""Weibo16 真实标注数据集导入（隔离数据目录）。

数据集：Ma et al., "Detecting Rumors from Microblogs with Recurrent Neural
Networks", IJCAI 2016（rumdect 包）。4,664 个事件：谣言 2,313 / 非谣言 2,351；
每个事件的源帖与转发在 Weibo/<eid>.json 中，转发自带 parent（父帖 mid）、
时间戳 t 与正文。学术研究用途，不再分发原始数据。

安全边界（任何目标修改之前完成校验，失败即拒绝）：
- 必须用 --data-dir 显式指定隔离数据目录（或已设置 FAKENGIN_DATA_DIR）。
  目标按解析后的绝对路径校验：不得指向、位于业务数据目录（项目 database/
  或启动时已含 fakengin.db 的 FAKENGIN_DATA_DIR）之内，也不得包含业务目录；
  已存在的目标必须是空目录——只数 messages 表不足以判定安全，任何既有
  用户、会话或业务记录都视为非空。
- 参数范围先校验：max_comments/test_fraction/limit 拒绝负数、非有限值与
  无意义比例，防止静默改变导入语义。
- zip 输入按解压预算校验：成员数、单文件与总未压缩大小、压缩比、路径
  穿越/绝对路径/符号链接/非白名单类型全部先检后解，实际解压时累计字节，
  超限立即中止；解压用受控临时目录，成功或失败均清理，不自动展开嵌套归档。
- 每事件转发按时间升序截断导入（--max-comments，默认 20）。导入的是截断
  后的部分回复结构，不宣称完整保留原始传播树。
- 导入同时生成"近重复分组"划分文件 weibo16_split.json：清洗后正文前
  50 字符为组键，整组进入同一侧，按类别分层——降低（不是消除）同一谣言
  变体跨训练/测试的近重复泄漏风险。

用法：
    python scripts/import_weibo16.py <rumdect解压目录或zip> --data-dir <空目录>
        [--max-comments 20] [--test-fraction 0.2] [--seed 20260921] [--limit N]
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import stat as stat_module
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import db, newsdata  # noqa: E402

DATASET_SOURCE = "Weibo16 数据集（Ma et al., IJCAI 2016）"
SPLIT_FILE_NAME = "weibo16_split.json"
GROUP_KEY_LENGTH = 50
MAX_TEXT_LENGTH = 5000
MAX_COMMENTS_CAP = 10000
_URL_RE = re.compile(r"https?://\S+")

# zip 解压预算：依据 rumdect 包真实规模（约 4,664 个事件 JSON + Weibo.txt，
# 解压后约 1-2 GiB）设定并留出余量；超限拒绝而不是冒险解压。
ZIP_MAX_MEMBERS = 6000
ZIP_MAX_MEMBER_BYTES = 64 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 200
ZIP_ALLOWED_SUFFIXES = {".json", ".txt"}
ZIP_READ_CHUNK = 1024 * 1024

# 业务记录表：任何一张有数据都说明这不是空库（users 除外——init_db 会播种
# 默认 admin，只有超出默认 admin 的用户才算业务数据）。
BUSINESS_TABLES = ("messages", "comments", "reviews", "sessions",
                   "detection_runs", "collected_items", "collection_runs")


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


# ------------------------------------------------------------ 参数校验

def _validate_count(name, value, low, high):
    """整数范围校验：负数切片等非法值一律拒绝，不静默纠正。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("{} 必须是整数（当前 {!r}）".format(name, value))
    if not low <= value <= high:
        raise ValueError("{} 必须在 {}-{} 之间（当前 {}）".format(name, low, high, value))


def _validate_fraction(name, value):
    """比例校验：拒绝 NaN/inf、0 与 1 之外的比例。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("{} 必须是数值（当前 {!r}）".format(name, value))
    if not math.isfinite(float(value)):
        raise ValueError("{} 必须是有限数值".format(name))
    if not 0.0 < float(value) < 1.0:
        raise ValueError("{} 必须在 (0, 1) 开区间内（当前 {}）".format(name, value))


def _resolve_strict(path):
    """展开 ~ 并解析为绝对路径（跟随符号链接，目标不存在也可解析）。"""
    return Path(os.path.expanduser(str(path))).resolve(strict=False)


def _validate_target_dir(data_dir):
    """导入目标隔离校验：在任何初始化、写文件或解压之前调用。

    拒绝：指向业务数据目录（项目 database/ 或启动时已存在 fakengin.db 的
    FAKENGIN_DATA_DIR）、位于业务目录之内或包含业务目录的任何路径（含
    符号链接与相对路径别名）；已存在但非空的目标目录。
    """
    target = _resolve_strict(data_dir)

    protected = [_resolve_strict(PROJECT_ROOT / "database")]
    env_dir = (os.getenv("FAKENGIN_DATA_DIR") or "").strip()
    if env_dir:
        env_resolved = _resolve_strict(env_dir)
        # 环境变量指向的目录若已有数据库文件，视为在用数据目录；
        # 与目标相同的情况由"目标必须为空"规则兜底（在用库必非空）。
        if env_resolved != target and (env_resolved / "fakengin.db").exists():
            protected.append(env_resolved)

    for business in protected:
        if target == business:
            raise ValueError(
                "拒绝导入：目标目录是业务数据目录（{}）。请换用全新的空目录，"
                "例如 /tmp 下的独立目录。".format(business))
        if business in target.parents:
            raise ValueError(
                "拒绝导入：目标目录位于业务数据目录（{}）之内。".format(business))
        if target in business.parents:
            raise ValueError(
                "拒绝导入：目标目录包含业务数据目录（{}）。".format(business))

    if target.exists():
        if not target.is_dir():
            raise ValueError("目标路径已存在且不是目录：{}".format(target))
        entries = list(target.iterdir())
        if entries:
            raise ValueError(
                "目标目录不是空目录（已有 {} 项：{}）。隔离导入只接受全新的空"
                "目录；如需重新导入，请先删除整个隔离目录。".format(
                    len(entries), "、".join(e.name for e in entries[:5])))
    return target


def _business_record_count(conn):
    """库内业务记录计数：任何业务表有数据或存在默认 admin 之外的用户即非空。"""
    total = 0
    for table in BUSINESS_TABLES:
        total += conn.execute("SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()["n"]
    extra_users = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE username != ?",
        (db.DEFAULT_ADMIN_USERNAME,)).fetchone()["n"]
    return total + extra_users


# ------------------------------------------------------------ 数据解析

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
    _validate_count("max_comments", max_comments, 0, MAX_COMMENTS_CAP)
    _validate_count("limit", limit, 0, 10 ** 9)
    events = parse_events(weibo_root)
    if limit > 0:
        events = events[:limit]

    records = []
    skipped = {"missing_file": 0, "empty_source": 0}
    comment_count = 0
    with db.db_conn() as conn:
        # 空库校验覆盖全部业务表（含用户与会话），不只数 messages；
        # init_db 播种的默认 admin 不算业务数据。
        existing = _business_record_count(conn)
        if existing:
            raise ValueError(
                "目标库已有 {} 条业务记录（消息/评论/用户/会话等）："
                "请换用空的隔离数据目录，避免污染既有数据".format(existing))
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


def _dataset_fingerprint(records):
    """数据集指纹：与 webapp.newsdata.dataset_fingerprint 同一公式，
    供训练侧核对划分文件与当前库内容一致，防止错库划分静默通过。"""
    return newsdata.dataset_fingerprint(
        [{"id": r["message_id"], "label": r["label"], "content": r["content"]}
         for r in records])


def build_split(records, test_fraction=0.2, seed=20260921):
    """按近重复分组 + 类别分层划分训练/测试，整组进同一侧。"""
    _validate_fraction("test_fraction", test_fraction)
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
        "version": 2,
        "train": [r["message_id"] for r in train],
        "test": [r["message_id"] for r in test],
        "meta": {
            "source": DATASET_SOURCE,
            "strategy": "近重复分组划分：清洗后正文前 50 字符为组键，整组进同一侧；"
                        "按类别分层。该策略降低近重复泄漏风险，不保证消除所有变体泄漏。",
            "importer_version": 2,
            "import_rules": "每事件转发按时间升序截断（max_comments），URL 清洗，"
                            "缺失父节点提升为顶层：导入为截断的部分回复结构",
            "dataset_fingerprint": _dataset_fingerprint(records),
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


# ------------------------------------------------------------ zip 解压

def _check_zip_member(info):
    """单个成员校验：路径安全、类型白名单、单文件大小与压缩比。"""
    name = info.filename or ""
    if not name:
        raise ValueError("zip 内存在空路径成员")
    if name.startswith(("/", "\\")):
        raise ValueError("zip 成员使用绝对路径：{}".format(name))
    head = name.split("/")[0]
    if ":" in head and re.match(r"^[A-Za-z]:", head):
        raise ValueError("zip 成员带 Windows 盘符：{}".format(name))
    parts = PurePosixPath(name).parts
    if ".." in parts:
        raise ValueError("zip 成员包含路径穿越（..）：{}".format(name))
    if "\\" in name:
        raise ValueError("zip 成员包含反斜杠：{}".format(name))
    # Unix 权限位在高 16 位：目录条目放行，符号链接、设备等非普通文件拒绝
    mode = info.external_attr >> 16
    file_kind = stat_module.S_IFMT(mode) if mode else 0
    if file_kind == stat_module.S_IFDIR or name.endswith("/"):
        return  # 目录条目
    if file_kind not in (0, stat_module.S_IFREG):
        raise ValueError("zip 成员不是普通文件（符号链接/设备等）：{}".format(name))
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in ZIP_ALLOWED_SUFFIXES:
        raise ValueError(
            "zip 成员类型不在白名单（{}，允许：{}）".format(
                name, "、".join(sorted(ZIP_ALLOWED_SUFFIXES))))
    if info.file_size > ZIP_MAX_MEMBER_BYTES:
        raise ValueError("zip 成员声明解压后 {} 字节，超过单文件上限".format(
            info.file_size))
    if info.compress_size > 0 and \
            info.file_size > info.compress_size * ZIP_MAX_COMPRESSION_RATIO:
        raise ValueError(
            "zip 成员压缩比超过 {}:1（{} 声明 {} 字节）".format(
                ZIP_MAX_COMPRESSION_RATIO, name, info.file_size))


def _extract_zip_capped(zip_path, target):
    """按预算解压：先全量校验成员，解压时累计实际字节，超限立即中止。"""
    try:
        bundle = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ValueError("zip 包损坏或不是有效压缩包：{}".format(exc))
    with bundle:
        try:
            infos = bundle.infolist()
        except zipfile.BadZipFile as exc:
            raise ValueError("zip 包目录损坏：{}".format(exc))
        if not infos:
            raise ValueError("zip 包是空的")
        if len(infos) > ZIP_MAX_MEMBERS:
            raise ValueError("zip 成员数 {} 超过上限 {}".format(
                len(infos), ZIP_MAX_MEMBERS))
        for info in infos:
            _check_zip_member(info)
        total_declared = sum(i.file_size for i in infos if not i.is_dir())
        if total_declared > ZIP_MAX_TOTAL_BYTES:
            raise ValueError("zip 声明解压总量 {} 字节，超过总预算".format(
                total_declared))

        total_actual = 0
        for info in infos:
            dest = target.joinpath(*PurePosixPath(info.filename).parts)
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with bundle.open(info) as src, open(dest, "wb") as out:
                    written = 0
                    while True:
                        chunk = src.read(ZIP_READ_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        total_actual += len(chunk)
                        # 实际累计字节为准，不信任声明值
                        if written > ZIP_MAX_MEMBER_BYTES:
                            raise ValueError(
                                "zip 成员实际解压超过单文件上限（已写 {} 字节）".format(written))
                        if total_actual > ZIP_MAX_TOTAL_BYTES:
                            raise ValueError(
                                "zip 实际解压总量超过 {} 字节预算".format(ZIP_MAX_TOTAL_BYTES))
                        out.write(chunk)
            except zipfile.BadZipFile as exc:
                raise ValueError("zip 包数据损坏（{}）：{}".format(info.filename, exc))
            if written != info.file_size:
                raise ValueError(
                    "zip 成员实际大小与声明不符（{}：实际 {}，声明 {}）".format(
                        info.filename, written, info.file_size))


def _resolve_weibo_root(source, extract_dir):
    """输入可以是解压目录或 zip 包；zip 解压到指定临时目录后返回其根。"""
    source = Path(source)
    if source.is_dir():
        if not (source / "Weibo.txt").exists():
            raise ValueError("目录中未找到 Weibo.txt：{}".format(source))
        return source
    if source.is_file() and source.suffix == ".zip":
        _extract_zip_capped(source, extract_dir)
        for candidate in (extract_dir, *extract_dir.iterdir()):
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
                        help="空的隔离数据目录（或预先设置 FAKENGIN_DATA_DIR）")
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

    # 校验顺序：先参数与目标隔离（此时目标与解压临时目录都未被触碰），
    # 再解压、初始化与导入。
    try:
        _validate_count("max-comments", args.max_comments, 0, MAX_COMMENTS_CAP)
        _validate_fraction("test-fraction", args.test_fraction)
        _validate_count("limit", args.limit, 0, 10 ** 9)
        target = _validate_target_dir(data_dir)
    except ValueError as exc:
        print(str(exc))
        return 1

    extract_dir = None
    try:
        extract_dir = Path(tempfile.mkdtemp(prefix="weibo16_extract_"))
        weibo_root = _resolve_weibo_root(args.source, extract_dir)
        _point_data_dir(target)
        db.init_db()
        records, stats = run_import(weibo_root, args.max_comments, args.limit)
        split = build_split(records, args.test_fraction, args.seed)
    except ValueError as exc:
        print(str(exc))
        return 1
    finally:
        if extract_dir is not None:
            # 成功或失败都清理解压临时目录，不留大体积残留
            shutil.rmtree(extract_dir, ignore_errors=True)

    split_path = target / SPLIT_FILE_NAME
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
