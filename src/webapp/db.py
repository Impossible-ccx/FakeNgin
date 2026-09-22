"""SQLite 数据访问层。

业务数据存放于 database/fakengin.db；CSV 仅保留为导入/导出格式
（见 newsdata.import_csv / export_csv 与 scripts/migrate_csv_to_sqlite.py）。

- 连接：每次操作短连接，WAL 模式 + busy_timeout，写操作用事务包裹。
- 迁移：PRAGMA user_version 记录库结构版本，按 MIGRATIONS 列表顺序升级。
- 数据目录可用环境变量 FAKENGIN_DATA_DIR 指向其他位置（测试用临时目录）。
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

import secrets

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_DIR = Path(os.getenv("FAKENGIN_DATA_DIR") or PROJECT_ROOT / "database")
DATABASE_FILE = DATABASE_DIR / "fakengin.db"

ADMIN_PASSWORD_FILE = "admin_initial_password.txt"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 不再内置固定默认口令：首次初始化时优先取 FAKENGIN_ADMIN_PASSWORD，
# 未设置则生成随机口令并写入 ADMIN_PASSWORD_FILE（仅管理员可读），不打印到日志。
DEFAULT_ADMIN_USERNAME = "admin"

SESSION_TTL_DAYS = 3

MIGRATIONS = {
    1: [
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            nature TEXT NOT NULL DEFAULT '未校验'
                CHECK (nature IN ('虚假', '真实', '中立', '未校验')),
            fake_probability REAL
                CHECK (fake_probability IS NULL
                       OR (fake_probability >= 0 AND fake_probability <= 100)),
            source TEXT NOT NULL DEFAULT '',
            publish_time TEXT NOT NULL DEFAULT '',
            process_time TEXT NOT NULL DEFAULT '',
            image_ref TEXT NOT NULL DEFAULT '',
            legacy_probability INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        )
        """,
        "CREATE INDEX idx_messages_publish_time ON messages(publish_time)",
        "CREATE INDEX idx_messages_nature ON messages(nature)",
        """
        CREATE TABLE comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            publish_time TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_comments_message ON comments(message_id)",
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'reviewer',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ],
    2: [
        """
        CREATE TABLE detection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            input_version INTEGER NOT NULL DEFAULT 0,
            content_digest TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')),
            probability REAL,
            reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        "CREATE INDEX idx_detection_message ON detection_runs(message_id)",
        "CREATE INDEX idx_detection_status ON detection_runs(status)",
        """
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            detection_run_id INTEGER REFERENCES detection_runs(id),
            reviewer TEXT NOT NULL,
            conclusion TEXT NOT NULL
                CHECK (conclusion IN ('虚假', '真实', '中立', '证据不足')),
            evidence TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_reviews_message ON reviews(message_id)",
    ],
    # “证据不足”加入消息性质；SQLite 无法修改 CHECK，需重建表并保留数据。
    3: [
        """
        CREATE TABLE messages_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            nature TEXT NOT NULL DEFAULT '未校验'
                CHECK (nature IN ('虚假', '真实', '中立', '证据不足', '未校验')),
            fake_probability REAL
                CHECK (fake_probability IS NULL
                       OR (fake_probability >= 0 AND fake_probability <= 100)),
            source TEXT NOT NULL DEFAULT '',
            publish_time TEXT NOT NULL DEFAULT '',
            process_time TEXT NOT NULL DEFAULT '',
            image_ref TEXT NOT NULL DEFAULT '',
            legacy_probability INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        INSERT INTO messages_new
            (id, content, nature, fake_probability, source, publish_time,
             process_time, image_ref, legacy_probability, created_at,
             updated_at, version)
        SELECT id, content, nature, fake_probability, source, publish_time,
               process_time, image_ref, legacy_probability, created_at,
               updated_at, version
        FROM messages
        """,
        "DROP TABLE messages",
        "ALTER TABLE messages_new RENAME TO messages",
        "CREATE INDEX idx_messages_publish_time ON messages(publish_time)",
        "CREATE INDEX idx_messages_nature ON messages(nature)",
    ],
    # 检测任务所有权：多进程部署时凭 worker 心跳区分“仍在执行”与“已死进程遗留”，
    # 避免 recover_interrupted 误伤其他活跃进程的任务。
    4: [
        "ALTER TABLE detection_runs ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''",
        """
        CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_detection_worker ON detection_runs(worker_id)",
    ],
    # 在线采集：运行记录与外部条目（幂等去重键 = 来源 + 外部 ID）。
    5: [
        """
        CREATE TABLE collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            requests INTEGER NOT NULL DEFAULT 0,
            items_fetched INTEGER NOT NULL DEFAULT 0,
            items_new INTEGER NOT NULL DEFAULT 0,
            items_duplicate INTEGER NOT NULL DEFAULT 0,
            items_rejected INTEGER NOT NULL DEFAULT 0,
            messages_imported INTEGER NOT NULL DEFAULT 0,
            not_modified INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX idx_collection_runs_source ON collection_runs(source_id)",
        """
        CREATE TABLE collected_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            link TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            collected_at TEXT NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            UNIQUE (source_id, external_id)
        )
        """,
        "CREATE INDEX idx_collected_items_message ON collected_items(message_id)",
    ],
}


def now_string():
    return datetime.now().strftime(TIME_FORMAT)


@contextmanager
def db_conn():
    """短连接上下文；写操作自动提交，异常自动回滚。"""
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- 迁移

def init_db():
    """建库并按版本执行迁移；重复调用与多进程并发调用均安全。

    多进程（如 gunicorn 多 worker）并发首次初始化时，另一进程可能已
    完成迁移或播种：迁移冲突则重读 user_version 重试，管理员重复插入
    视为初始化成功。
    """
    last_error = None
    for _attempt in range(3):
        try:
            _init_db_once()
            return
        except sqlite3.IntegrityError as exc:
            if "users.username" in str(exc):
                return
            raise
        except sqlite3.OperationalError as exc:
            last_error = exc
    raise last_error


def _init_db_once():
    # 新数据目录：先创建目录再连接，否则报 unable to open database file
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    # 手动事务控制：BEGIN EXCLUSIVE 把多进程并发初始化串行化——
    # 后到进程拿到锁后重读 user_version，看到已完成的迁移就直接跳过，
    # 不会与进行中的迁移互相冲突（“table already exists”竞态）。
    conn.isolation_level = None
    try:
        # PRAGMA foreign_keys 在事务内是空操作，必须在 BEGIN 之前设置；
        # 迁移期间关闭外键检查（表重建迁移需要保留子表数据）
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN EXCLUSIVE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        max_version = max(MIGRATIONS) if MIGRATIONS else 0
        if version < max_version:
            for target in sorted(MIGRATIONS):
                if target > version:
                    for sql in MIGRATIONS[target]:
                        conn.execute(sql)
                    conn.execute("PRAGMA user_version = {}".format(int(target)))
        _seed_default_admin(conn)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _seed_default_admin(conn):
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
        return
    password = os.getenv("FAKENGIN_ADMIN_PASSWORD", "").strip()
    generated = password or secrets.token_urlsafe(12)
    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (
            DEFAULT_ADMIN_USERNAME,
            generate_password_hash(generated),
            "admin",
            now_string(),
        ),
    )
    if not password:
        # 只有插入成功的进程才写口令文件，保证文件与库中哈希一致
        password_file = DATABASE_DIR / ADMIN_PASSWORD_FILE
        DATABASE_DIR.mkdir(parents=True, exist_ok=True)
        password_file.write_text(
            "账户：{}\n初始密码：{}\n（首次初始化生成；登录后请修改并删除本文件）\n".format(
                DEFAULT_ADMIN_USERNAME, generated),
            encoding="utf-8")
        password_file.chmod(0o600)
        print("已生成管理员初始密码文件：{}".format(password_file))


def get_meta(conn, key, default=""):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- users

def find_user(username):
    """按账户名查找用户，返回 dict 或 None。"""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def verify_user(username, password):
    """校验账户口令（哈希比对），成功返回用户 dict，失败返回 None。"""
    user = find_user(username)
    if user is None:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


def create_user(username, password, role="reviewer"):
    """创建用户；密码保存哈希。用户名重复抛 ValueError。"""
    if find_user(username) is not None:
        raise ValueError("账户已存在")
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, now_string()),
        )


def update_password(username, new_password):
    """修改指定用户密码（哈希存储）。用户不存在抛 ValueError。"""
    if find_user(username) is None:
        raise ValueError("账户不存在")
    with db_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(new_password), username),
        )
        if cursor.rowcount != 1:
            raise ValueError("密码修改失败")


# ------------------------------------------------------------- sessions

def add_session(session_id, username):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, username, created_at) VALUES (?, ?, ?)",
            (session_id, username, now_string()),
        )


def remove_session(session_id):
    with db_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def find_session(session_id):
    """按 session_id 查找登录状态，返回 dict 或 None。"""
    if not session_id:
        return None
    with db_conn() as conn:
        row = conn.execute(
            "SELECT session_id, username, created_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None
