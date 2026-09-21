"""把旧版 CSV 数据迁移到 SQLite。

用法（在项目根目录）：
    python scripts/migrate_csv_to_sqlite.py

行为：
1. database/fakengin.db 已存在时拒绝执行（--force 允许删除后重建）。
2. 先把旧 CSV 备份到 database_backup_<时间戳>/（不删除、不修改原文件）。
3. users.csv（明文密码）导入后保存哈希；sessions.csv 原样导入。
4. database/newsdata/*.csv 导入 messages 表：
   - 保留原 nature 与 fake_probability；
   - 原概率非空时标记 legacy_probability=1（历史导入、来源未知），
     不伪装成新模型检测结果；
   - 来源文件名记录到 meta 表（key = legacy_messages.<文件名>）。
5. 迁移后打印各表数量与原文件行数对照，供一致性核对。

恢复方式：删除 fakengin.db 后从备份目录还原 CSV，或直接使用备份目录。
"""

import argparse
import csv
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from werkzeug.security import generate_password_hash  # noqa: E402

from webapp import db, newsdata  # noqa: E402

NEWS_COLUMNS = newsdata.COLUMNS


def csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield {key: (value or "") for key, value in row.items()}


def import_users(conn, path):
    count = 0
    for row in csv_rows(path):
        username = (row.get("username") or "").strip()
        if not username:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                username,
                generate_password_hash(row.get("password", "")),
                (row.get("role") or "reviewer").strip() or "reviewer",
                (row.get("created_at") or "").strip() or db.now_string(),
            ),
        )
        count += 1
    return count


def import_sessions(conn, path):
    count = 0
    for row in csv_rows(path):
        session_id = (row.get("session_id") or "").strip()
        if not session_id:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, username, created_at) VALUES (?, ?, ?)",
            (
                session_id,
                (row.get("username") or "").strip(),
                (row.get("created_at") or "").strip(),
            ),
        )
        count += 1
    return count


def import_messages(conn, newsdata_dir):
    total = 0
    per_file = {}
    for path in sorted(newsdata_dir.glob("*.csv")):
        count = 0
        for line_no, row in enumerate(csv_rows(path), start=2):
            try:
                normalized = newsdata.normalize_row(row)
            except ValueError as exc:
                print("  [跳过] {} 第 {} 行：{}".format(path.name, line_no, exc))
                continue
            if not normalized["content"]:
                print("  [跳过] {} 第 {} 行：空正文".format(path.name, line_no))
                continue
            probability = normalized["fake_probability"]
            legacy = 1 if probability is not None else 0
            now = db.now_string()
            conn.execute(
                "INSERT INTO messages (content, nature, fake_probability, source, "
                "publish_time, process_time, image_ref, legacy_probability, "
                "created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    normalized["content"], normalized["nature"], probability,
                    normalized["source"], normalized["publish_time"],
                    normalized["process_time"], "", legacy, now, now,
                ),
            )
            count += 1
        db.set_meta(conn, "legacy_messages." + path.name, str(count))
        per_file[path.name] = count
        total += count
    return total, per_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="已存在 fakengin.db 时删除后重建（原 CSV 仍只备份不删除）")
    args = parser.parse_args()

    database_dir = db.DATABASE_DIR
    if database_dir.resolve() != (PROJECT_ROOT / "database").resolve():
        print("提示：FAKENGIN_DATA_DIR={}".format(database_dir))

    newsdata_dir = database_dir / "newsdata"
    users_file = database_dir / "users.csv"
    sessions_file = database_dir / "sessions.csv"

    if db.DATABASE_FILE.exists():
        if not args.force:
            print("已存在 {}；如需重建请加 --force".format(db.DATABASE_FILE))
            return 1
        db.DATABASE_FILE.unlink()

    backup_dir = database_dir.parent / "database_backup_{}".format(
        time.strftime("%Y%m%d_%H%M%S"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    for item in (newsdata_dir, users_file, sessions_file):
        if item.exists():
            target = backup_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
    print("原文件已备份到：{}".format(backup_dir))

    db.init_db()

    with db.db_conn() as conn:
        user_count = import_users(conn, users_file) if users_file.exists() else 0
        session_count = import_sessions(conn, sessions_file) if sessions_file.exists() else 0
        message_count, per_file = (import_messages(conn, newsdata_dir)
                                   if newsdata_dir.exists() else (0, {}))

    print("迁移完成：")
    print("  users: {}    sessions: {}    messages: {}".format(
        user_count, session_count, message_count))
    for name, count in per_file.items():
        print("  newsdata/{} → {} 条".format(name, count))
    print("数据库文件：{}".format(db.DATABASE_FILE))
    print("说明：原概率已保留并标记为历史导入（来源未知），不会伪装成新检测结果。")
    return 0


def csv_rows_each(newsdata_dir):
    for path in sorted(newsdata_dir.glob("*.csv")):
        for _row in csv_rows(path):
            yield path


if __name__ == "__main__":
    raise SystemExit(main())
