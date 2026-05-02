"""
Shared SQLite database functions for subscriber management.
Used by both app.py (signup/unsubscribe) and financial_brief.py (send to all).
"""

import sqlite3
import secrets
import json
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv(
    "FINANCIAL_BRIEF_DB_PATH",
    os.path.join(os.path.dirname(__file__), "subscribers.db")
)


def get_connection():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables if they don't exist."""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_time TEXT    NOT NULL,
                data_json     TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_daily_backup (
                id            INTEGER PRIMARY KEY,
                name          TEXT    NOT NULL,
                email         TEXT    UNIQUE NOT NULL,
                token         TEXT    UNIQUE NOT NULL,
                created_at    DATETIME,
                active        INTEGER DEFAULT 1,
                snapshot_date TEXT    NOT NULL,
                snapshot_time TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_backup_meta (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                last_snapshot_date TEXT    NOT NULL,
                snapshot_time      TEXT    NOT NULL,
                subscriber_count   INTEGER NOT NULL,
                active_count       INTEGER NOT NULL
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


# ── Subscriber Backup Snapshot ────────────────────────────────────────────────

def snapshot_subscribers_once_daily(force: bool = False) -> dict:
    """Overwrite the subscriber backup table at most once per day.

    The backup is intentionally a single current snapshot, not a history table.
    To avoid replacing a good backup with a damaged/deleted subscriber table, the
    snapshot is skipped if the live table has fewer rows than the current backup.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().isoformat(timespec="seconds")

    with get_connection() as conn:
        meta = conn.execute(
            "SELECT last_snapshot_date, subscriber_count FROM subscriber_backup_meta WHERE id = 1"
        ).fetchone()
        if meta and meta["last_snapshot_date"] == today and not force:
            return {"created": False, "reason": "already_snapshotted_today"}

        current_count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        current_active = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE active = 1"
        ).fetchone()[0]
        backup_count = conn.execute(
            "SELECT COUNT(*) FROM subscriber_daily_backup"
        ).fetchone()[0]

        if backup_count and current_count < backup_count and not force:
            return {
                "created": False,
                "reason": "current_table_smaller_than_backup",
                "current_count": current_count,
                "backup_count": backup_count,
            }

        conn.execute("DELETE FROM subscriber_daily_backup")
        conn.execute(
            """
            INSERT INTO subscriber_daily_backup
                (id, name, email, token, created_at, active, snapshot_date, snapshot_time)
            SELECT id, name, email, token, created_at, active, ?, ?
            FROM subscribers
            ORDER BY id
            """,
            (today, now)
        )
        conn.execute(
            """
            INSERT INTO subscriber_backup_meta
                (id, last_snapshot_date, snapshot_time, subscriber_count, active_count)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_snapshot_date = excluded.last_snapshot_date,
                snapshot_time = excluded.snapshot_time,
                subscriber_count = excluded.subscriber_count,
                active_count = excluded.active_count
            """,
            (today, now, current_count, current_active)
        )
        conn.commit()

    return {
        "created": True,
        "subscriber_count": current_count,
        "active_count": current_active,
        "snapshot_date": today,
    }


def restore_subscribers_from_backup() -> dict:
    """Restore the live subscribers table from the current backup snapshot."""
    with get_connection() as conn:
        backup_count = conn.execute(
            "SELECT COUNT(*) FROM subscriber_daily_backup"
        ).fetchone()[0]
        if backup_count == 0:
            return {"restored": False, "reason": "no_backup_available"}

        conn.execute("DELETE FROM subscribers")
        conn.execute(
            """
            INSERT INTO subscribers (id, name, email, token, created_at, active)
            SELECT id, name, email, token, created_at, active
            FROM subscriber_daily_backup
            ORDER BY id
            """
        )
        conn.commit()

    return {"restored": True, "subscriber_count": backup_count}


# ── Market Snapshot Storage ────────────────────────────────────────────────────

def save_market_snapshot(snapshot: dict):
    """Save a market data snapshot (called 5x/day by gather_data.py)."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO market_snapshots (snapshot_time, data_json) VALUES (?, ?)",
            (datetime.utcnow().isoformat(), json.dumps(snapshot, ensure_ascii=False))
        )
        conn.commit()


def get_snapshots_last_24h() -> list:
    """Return all snapshots from the last 24 hours, oldest first."""
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT snapshot_time, data_json FROM market_snapshots "
            "WHERE snapshot_time > ? ORDER BY snapshot_time ASC",
            (cutoff,)
        ).fetchall()
    return [{"time": row["snapshot_time"], "data": json.loads(row["data_json"])} for row in rows]


def cleanup_old_snapshots(days: int = 7):
    """Delete snapshots older than N days to keep the DB lean."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM market_snapshots WHERE snapshot_time < ?", (cutoff,))
        conn.commit()
