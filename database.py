"""
Shared SQLite database functions for subscriber management.
Used by both app.py (signup/unsubscribe) and financial_brief.py (send to all).
"""

import sqlite3
import secrets
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "subscribers.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the subscribers table if it doesn't exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                email      TEXT    UNIQUE NOT NULL,
                token      TEXT    UNIQUE NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                active     INTEGER DEFAULT 1
            )
        """)
        conn.commit()


def add_subscriber(name: str, email: str) -> dict:
    """Add a new subscriber. Returns {'success': True} or {'error': '...'}."""
    token = secrets.token_urlsafe(32)
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO subscribers (name, email, token) VALUES (?, ?, ?)",
                (name.strip(), email.strip().lower(), token)
            )
            conn.commit()
        return {"success": True}
    except sqlite3.IntegrityError:
        return {"error": "כתובת המייל כבר רשומה במערכת."}


def get_active_subscribers() -> list:
    """Return all active subscribers as a list of dicts."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT name, email, token FROM subscribers WHERE active = 1"
        ).fetchall()
    return [dict(row) for row in rows]


def unsubscribe(token: str) -> bool:
    """Deactivate a subscriber by their token. Returns True if found."""
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscribers SET active = 0 WHERE token = ? AND active = 1",
            (token,)
        )
        conn.commit()
    return cursor.rowcount > 0


def seed_owner(name: str, email: str):
    """Add the owner's email to DB on first run if not already present."""
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM subscribers WHERE email = ?", (email.lower(),)
        ).fetchone()
    if not exists:
        add_subscriber(name, email)
