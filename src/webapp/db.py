"""CSV 数据访问层。

所有服务器数据以 csv 格式存放于项目根目录的 database/ 中，使用 pandas 读写。
"""

from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_DIR = PROJECT_ROOT / "database"

USERS_FILE = DATABASE_DIR / "users.csv"
SESSIONS_FILE = DATABASE_DIR / "sessions.csv"

USERS_COLUMNS = ["username", "password", "role", "created_at"]
SESSIONS_COLUMNS = ["session_id", "username", "created_at"]

DEFAULT_USER = {
    "username": "admin",
    "password": "admin",
    "role": "admin",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_csv(path, columns):
    if not path.exists():
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, dtype=str).fillna("")


def _write_csv(df, path, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, columns=columns)


def ensure_database():
    """确保 database/ 及两张表存在，缺失时创建并写入测试用户。"""
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)

    if not USERS_FILE.exists():
        df = pd.DataFrame([{**DEFAULT_USER, "created_at": _now()}], columns=USERS_COLUMNS)
        _write_csv(df, USERS_FILE, USERS_COLUMNS)

    if not SESSIONS_FILE.exists():
        _write_csv(pd.DataFrame(columns=SESSIONS_COLUMNS), SESSIONS_FILE, SESSIONS_COLUMNS)


# ---------------------------------------------------------------- users

def read_users():
    return _read_csv(USERS_FILE, USERS_COLUMNS)


def find_user(username):
    """按账户名查找用户，返回 dict 或 None。"""
    users = read_users()
    match = users[users["username"] == username]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


# ------------------------------------------------------------- sessions

def read_sessions():
    return _read_csv(SESSIONS_FILE, SESSIONS_COLUMNS)


def find_session(session_id):
    """按 session_id 查找登录状态，返回 dict 或 None。"""
    if not session_id:
        return None
    sessions = read_sessions()
    match = sessions[sessions["session_id"] == session_id]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


def add_session(session_id, username):
    sessions = read_sessions()
    row = {"session_id": session_id, "username": username, "created_at": _now()}
    sessions = pd.concat([sessions, pd.DataFrame([row])], ignore_index=True)
    _write_csv(sessions, SESSIONS_FILE, SESSIONS_COLUMNS)


def remove_session(session_id):
    sessions = read_sessions()
    sessions = sessions[sessions["session_id"] != session_id]
    _write_csv(sessions, SESSIONS_FILE, SESSIONS_COLUMNS)
