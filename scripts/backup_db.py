"""备份 SQLite 数据库（在线一致性快照，含 WAL 模式）。

用法：
    python scripts/backup_db.py [备份目录]

默认写到 <数据目录同级>/database_backup_<时间戳>/fakengin.db。
使用 SQLite backup API，应用运行中执行也能得到一致快照。

恢复方法：
    1. 停止应用（docker compose down 或停止本地进程）；
    2. 删除数据目录中的 fakengin.db、fakengin.db-wal、fakengin.db-shm；
    3. 把备份的 fakengin.db 复制回数据目录；
    4. 重新启动应用（搜索索引会按数据版本自动重建）。
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from webapp import db  # noqa: E402


def main():
    if len(sys.argv) > 2:
        print("用法：python scripts/backup_db.py [备份目录]")
        return 1
    target_dir = Path(sys.argv[1]) if len(sys.argv) == 2 else (
        db.DATABASE_DIR.parent /
        "database_backup_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S")))
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / db.DATABASE_FILE.name

    source = sqlite3.connect(str(db.DATABASE_FILE))
    dest = sqlite3.connect(str(target_file))
    try:
        with dest:
            source.backup(dest)
    finally:
        source.close()
        dest.close()
    print("备份完成：{}".format(target_file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
