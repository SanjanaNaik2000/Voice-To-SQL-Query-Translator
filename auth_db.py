"""
auth_db.py  --  Authentication database module for DBChatPro 2.0

Uses a SEPARATE SQLite file (auth.db) completely isolated from the user's
target databases. Provides user management + activity logging for RBAC.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash
from flask_login import UserMixin

# ------------------------------------------------------------------ path
_HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_DB_PATH = os.path.join(_HERE, "auth.db")


# ================================================================== User model
class User(UserMixin):
    """Flask-Login compatible user object built from a DB row dict."""

    def __init__(self, user_dict: dict) -> None:
        self.id = str(user_dict["id"])
        self.username = user_dict["username"]
        self.role = user_dict["role"]
        self.is_active_flag = bool(user_dict.get("is_active", 1))

    @property
    def is_active(self) -> bool:
        return self.is_active_flag


# ================================================================== helpers
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


# ================================================================== init
def init_auth_db() -> None:
    """Create the users and activity_log tables if they do not exist yet."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'user',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active     INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER,
                username  TEXT,
                action    TEXT,
                detail    TEXT    DEFAULT '',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def create_default_admin() -> None:
    """Insert the default admin account if no admin exists yet."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        if row is None:
            pw_hash = generate_password_hash("Admin@123")
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                ("admin", pw_hash),
            )
            conn.commit()


# ================================================================== CRUD
def get_user_by_username(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return _row_to_dict(row)


def get_user_by_id(user_id: int | str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
    return _row_to_dict(row)


def create_user(username: str, password: str, role: str = "user") -> dict:
    """Insert a new user. Raises ValueError if the username is already taken."""
    if get_user_by_username(username) is not None:
        raise ValueError(f"Username '{username}' is already taken.")
    pw_hash = generate_password_hash(password)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, pw_hash, role),
        )
        conn.commit()
    return get_user_by_username(username)  # type: ignore[return-value]


def get_all_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_user_status(user_id: int | str, is_active: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (int(is_active), int(user_id)),
        )
        conn.commit()


def delete_user(user_id: int | str) -> None:
    """Delete a user. Silently refuses to delete the admin account."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row and row["role"] == "admin":
            return  # protect the admin account
        conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        conn.commit()


def verify_password(username: str, password: str) -> dict | None:
    """Return the user dict if credentials are valid, else None."""
    user = get_user_by_username(username)
    if user is None:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


# ================================================================== activity log
def log_activity(
    user_id: int | str | None,
    username: str,
    action: str,
    detail: str = "",
) -> None:
    """Append one entry to the activity log (fire-and-forget; never raises)."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO activity_log (user_id, username, action, detail) VALUES (?, ?, ?, ?)",
                (int(user_id) if user_id is not None else None, username, action, detail),
            )
            conn.commit()
    except Exception:
        pass  # logging must never crash the request


def get_activity_log(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
