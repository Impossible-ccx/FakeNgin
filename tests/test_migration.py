"""迁移工具端到端测试：旧 CSV → SQLite，验证一致性、备份与防覆盖。"""

import csv
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_csv_to_sqlite.py"


def _seed_old_csv(database_dir):
    newsdata_dir = database_dir / "newsdata"
    newsdata_dir.mkdir(parents=True, exist_ok=True)

    with open(newsdata_dir / "manual.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["content", "nature", "fake_probability", "source",
                         "publish_time", "process_time"])
        writer.writerow(["旧消息一（无概率）", "未校验", "", "旧采集",
                         "2026-01-01 08:00:00", ""])
        writer.writerow(["旧消息二（带历史概率）", "虚假", "82.30", "旧采集",
                         "2026-01-02 09:00:00", "2026-01-03 10:00:00"])

    with open(newsdata_dir / "爬取.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["content", "nature", "fake_probability", "source",
                         "publish_time", "process_time"])
        writer.writerow(["旧消息三", "真实", "", "微博爬取", "", ""])

    with open(database_dir / "users.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["username", "password", "role", "created_at"])
        writer.writerow(["admin", "admin", "admin", "2026-01-01 00:00:00"])
        writer.writerow(["alice", "alice-pass", "reviewer", "2026-01-01 00:00:00"])

    with open(database_dir / "sessions.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["session_id", "username", "created_at"])
        writer.writerow(["deadbeef", "admin", "2026-01-01 00:00:00"])

    return {"messages": 3, "users": 2, "sessions": 1}


def _run_script(data_dir, *extra):
    env = {"FAKENGIN_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin",
           "SYSTEMROOT": "C:\\Windows"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _db_rows(data_dir, sql):
    conn = sqlite3.connect(data_dir / "fakengin.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def test_migration_consistency_backup_and_no_overwrite(tmp_path):
    data_dir = tmp_path / "database"
    data_dir.mkdir()
    expected = _seed_old_csv(data_dir)

    original_files = sorted(p.name for p in data_dir.rglob("*.csv"))

    result = _run_script(data_dir)
    assert result.returncode == 0, result.stderr

    # 数量一致
    assert len(_db_rows(data_dir, "SELECT * FROM messages")) == expected["messages"]
    assert len(_db_rows(data_dir, "SELECT * FROM users")) == expected["users"]
    assert len(_db_rows(data_dir, "SELECT * FROM sessions")) == expected["sessions"]

    # 内容与性质保留；历史概率标记 legacy_probability=1
    rows = {row["content"]: row
            for row in _db_rows(data_dir, "SELECT * FROM messages")}
    assert rows["旧消息二（带历史概率）"]["nature"] == "虚假"
    assert abs(rows["旧消息二（带历史概率）"]["fake_probability"] - 82.3) < 1e-9
    assert rows["旧消息二（带历史概率）"]["legacy_probability"] == 1
    assert rows["旧消息一（无概率）"]["legacy_probability"] == 0
    assert rows["旧消息三"]["nature"] == "真实"

    # 明文密码导入后为哈希，且可校验
    alice = _db_rows(data_dir, "SELECT * FROM users WHERE username='alice'")[0]
    assert alice["password_hash"] != "alice-pass"
    assert alice["password_hash"].startswith(("scrypt", "pbkdf2"))

    # 原文件仍在且未修改；备份目录已建立
    assert sorted(p.name for p in data_dir.rglob("*.csv")) == original_files
    backups = list(tmp_path.glob("database_backup_*"))
    assert len(backups) == 1
    assert (backups[0] / "users.csv").exists()
    assert (backups[0] / "newsdata" / "manual.csv").exists()

    # 再次运行拒绝覆盖已存在的库
    result_again = _run_script(data_dir)
    assert result_again.returncode == 1
    assert "已存在" in result_again.stdout


def test_migration_source_file_tracked(tmp_path):
    data_dir = tmp_path / "database"
    data_dir.mkdir()
    _seed_old_csv(data_dir)

    result = _run_script(data_dir)
    assert result.returncode == 0, result.stderr

    meta = {row["key"]: row["value"]
            for row in _db_rows(data_dir, "SELECT * FROM meta")}
    assert meta["legacy_messages.manual.csv"] == "2"
    assert meta["legacy_messages.爬取.csv"] == "1"
