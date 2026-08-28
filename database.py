"""
Shared SQLite database functions for subscriber management.
Used by both app.py (signup/unsubscribe) and financial_brief.py (send to all).
"""

import sqlite3
import secrets
import json
import os
import hashlib
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv(
    "FINANCIAL_BRIEF_DB_PATH",
    os.path.join(os.path.dirname(__file__), "subscribers.db")
)


class ClosingConnection(sqlite3.Connection):
    """SQLite context manager that also closes the connection on exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_connection():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, factory=ClosingConnection)
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_close_history (
                trade_date      TEXT NOT NULL,
                instrument_name TEXT NOT NULL,
                close_value     REAL NOT NULL,
                PRIMARY KEY (trade_date, instrument_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_score_predictions (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date             TEXT NOT NULL,
                sector_name            TEXT NOT NULL,
                graph_score            INTEGER NOT NULL,
                news_adjustment        INTEGER NOT NULL,
                calibration_adjustment INTEGER NOT NULL DEFAULT 0,
                final_score            INTEGER NOT NULL,
                sector_close           REAL NOT NULL,
                benchmark_close        REAL NOT NULL,
                created_at             TEXT NOT NULL,
                UNIQUE (trade_date, sector_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_score_outcomes (
                prediction_id       INTEGER NOT NULL,
                horizon_sessions    INTEGER NOT NULL,
                outcome_date        TEXT NOT NULL,
                sector_return_pct   REAL NOT NULL,
                benchmark_return_pct REAL NOT NULL,
                excess_return_pct   REAL NOT NULL,
                outperformed        INTEGER NOT NULL,
                PRIMARY KEY (prediction_id, horizon_sessions),
                FOREIGN KEY (prediction_id) REFERENCES sector_score_predictions(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sector_outcomes_horizon
            ON sector_score_outcomes (horizon_sessions, outperformed)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sector_predictions_score
            ON sector_score_predictions (final_score, sector_name)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS briefing_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date       TEXT UNIQUE NOT NULL,
                market_close_date TEXT,
                as_of             TEXT,
                content_hash      TEXT NOT NULL,
                brief_text        TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'ready',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                completed_at      TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_deliveries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL,
                recipient_email TEXT NOT NULL,
                status          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                message_id      TEXT NOT NULL,
                last_error      TEXT,
                claimed_at      TEXT,
                sent_at         TEXT,
                UNIQUE (run_id, recipient_email),
                FOREIGN KEY (run_id) REFERENCES briefing_runs(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_deliveries_status
            ON email_deliveries (run_id, status)
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


# ── Sector Score Calibration ─────────────────────────────────────────────────────

CALIBRATION_HORIZONS = (10, 20, 30)
CALIBRATION_MIN_DISPLAY_CASES = 20
CALIBRATION_MIN_ADJUSTMENT_CASES = 30


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_market_close_history(data: dict) -> dict:
    """Upsert official TASE closes used to settle predictions by trading session."""
    instruments = {
        "ת״א-125": data.get("indices", {}).get("ת״א-125", {}),
        **data.get("sectors", {}),
    }
    rows = []
    for name, instrument in instruments.items():
        for point in instrument.get("history_closes", []):
            trade_date = point.get("date")
            close_value = _safe_float(point.get("close"))
            if trade_date and close_value is not None and close_value > 0:
                rows.append((trade_date, name, close_value))

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO market_close_history (trade_date, instrument_name, close_value)
            VALUES (?, ?, ?)
            ON CONFLICT(trade_date, instrument_name) DO UPDATE SET
                close_value = excluded.close_value
            """,
            rows,
        )
        conn.commit()
    return {"close_rows_upserted": len(rows)}


def record_sector_score_predictions(data: dict, sector_scores: dict) -> dict:
    """Store one prediction per sector and official closing date."""
    benchmark = data.get("indices", {}).get("ת״א-125", {})
    benchmark_close = _safe_float(
        benchmark.get("trend", {}).get("last_close") or benchmark.get("last_value")
    )
    if benchmark_close is None:
        return {"predictions_upserted": 0, "reason": "benchmark_close_missing"}

    rows = []
    for name, score in sector_scores.items():
        sector = data.get("sectors", {}).get(name, {})
        trend = sector.get("trend", {})
        trade_date = trend.get("as_of")
        sector_close = _safe_float(trend.get("last_close") or sector.get("last_value"))
        if not trade_date or sector_close is None:
            continue
        rows.append((
            trade_date,
            name,
            int(score["graph_score"]),
            int(score["news_adjustment"]),
            int(score.get("calibration_adjustment", 0)),
            int(score["final_score"]),
            sector_close,
            benchmark_close,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ))

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_score_predictions
                (trade_date, sector_name, graph_score, news_adjustment,
                 calibration_adjustment, final_score, sector_close,
                 benchmark_close, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, sector_name) DO UPDATE SET
                graph_score = excluded.graph_score,
                news_adjustment = excluded.news_adjustment,
                calibration_adjustment = excluded.calibration_adjustment,
                final_score = excluded.final_score,
                sector_close = excluded.sector_close,
                benchmark_close = excluded.benchmark_close,
                created_at = excluded.created_at
            """,
            rows,
        )
        conn.commit()
    return {"predictions_upserted": len(rows)}


def get_latest_sector_scores() -> dict:
    """Return the most recent stored score for each sector."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.trade_date, p.sector_name, p.graph_score,
                   p.news_adjustment, p.calibration_adjustment, p.final_score
            FROM sector_score_predictions p
            JOIN (
                SELECT sector_name, MAX(trade_date) AS latest_trade_date
                FROM sector_score_predictions
                GROUP BY sector_name
            ) latest
              ON latest.sector_name = p.sector_name
             AND latest.latest_trade_date = p.trade_date
            ORDER BY p.sector_name
            """
        ).fetchall()
    return {row["sector_name"]: dict(row) for row in rows}


# ── Idempotent Briefing Delivery ───────────────────────────────────────────────

def get_briefing_run(report_date: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM briefing_runs WHERE report_date = ?",
            (report_date,),
        ).fetchone()
    return dict(row) if row else None


def save_briefing_run(
    report_date: str,
    market_close_date: str | None,
    as_of: str | None,
    brief_text: str,
) -> dict:
    """Persist one immutable generated briefing per Israel report date."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content_hash = hashlib.sha256(brief_text.encode("utf-8")).hexdigest()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO briefing_runs
                (report_date, market_close_date, as_of, content_hash,
                 brief_text, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'ready', ?, ?)
            ON CONFLICT(report_date) DO NOTHING
            """,
            (
                report_date,
                market_close_date,
                as_of,
                content_hash,
                brief_text,
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM briefing_runs WHERE report_date = ?",
            (report_date,),
        ).fetchone()
    return dict(row)


def claim_email_delivery(
    run_id: int,
    recipient_email: str,
    message_id: str,
    force: bool = False,
) -> dict:
    """Atomically claim a recipient unless this run was already delivered."""
    email = recipient_email.strip().lower()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT status, attempts
            FROM email_deliveries
            WHERE run_id = ? AND recipient_email = ?
            """,
            (run_id, email),
        ).fetchone()
        if row is None:
            attempt = 1
            conn.execute(
                """
                INSERT INTO email_deliveries
                    (run_id, recipient_email, status, attempts,
                     message_id, claimed_at)
                VALUES (?, ?, 'sending', ?, ?, ?)
                """,
                (run_id, email, attempt, message_id, now),
            )
            conn.commit()
            return {"send": True, "attempt": attempt, "reason": "new"}

        if row["status"] == "sent" and not force:
            conn.commit()
            return {"send": False, "attempt": row["attempts"], "reason": "already_sent"}
        if row["status"] == "sending" and not force:
            conn.commit()
            return {
                "send": False,
                "attempt": row["attempts"],
                "reason": "delivery_in_progress_or_ambiguous",
            }

        attempt = int(row["attempts"] or 0) + 1
        conn.execute(
            """
            UPDATE email_deliveries
            SET status = 'sending', attempts = ?, message_id = ?,
                last_error = NULL, claimed_at = ?, sent_at = NULL
            WHERE run_id = ? AND recipient_email = ?
            """,
            (attempt, message_id, now, run_id, email),
        )
        conn.commit()
    return {
        "send": True,
        "attempt": attempt,
        "reason": "forced" if force else "retry_failed",
    }


def mark_email_delivery_sent(run_id: int, recipient_email: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE email_deliveries
            SET status = 'sent', sent_at = ?, last_error = NULL
            WHERE run_id = ? AND recipient_email = ?
            """,
            (now, run_id, recipient_email.strip().lower()),
        )
        conn.commit()


def mark_email_delivery_failed(
    run_id: int,
    recipient_email: str,
    error: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE email_deliveries
            SET status = 'failed', last_error = ?
            WHERE run_id = ? AND recipient_email = ?
            """,
            (str(error)[:1000], run_id, recipient_email.strip().lower()),
        )
        conn.commit()


def finalize_briefing_run(run_id: int) -> dict:
    """Update aggregate run status from persistent recipient outcomes."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM email_deliveries WHERE run_id = ? GROUP BY status
            """,
            (run_id,),
        ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        if counts.get("failed"):
            status = "partial"
        elif counts.get("sending"):
            status = "sending"
        else:
            status = "completed"
        conn.execute(
            """
            UPDATE briefing_runs
            SET status = ?, updated_at = ?,
                completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END
            WHERE id = ?
            """,
            (status, now, status, now, run_id),
        )
        conn.commit()
    return {"status": status, "delivery_counts": counts}


def settle_sector_score_outcomes() -> dict:
    """Settle due 2/4/6-week predictions against TA-125 total price return."""
    settled = 0
    with get_connection() as conn:
        predictions = conn.execute(
            """
            SELECT id, trade_date, sector_name, sector_close, benchmark_close
            FROM sector_score_predictions
            ORDER BY trade_date, sector_name
            """
        ).fetchall()
        existing = {
            (row["prediction_id"], row["horizon_sessions"])
            for row in conn.execute(
                "SELECT prediction_id, horizon_sessions FROM sector_score_outcomes"
            ).fetchall()
        }

        for prediction in predictions:
            for horizon in CALIBRATION_HORIZONS:
                if (prediction["id"], horizon) in existing:
                    continue
                target = conn.execute(
                    """
                    SELECT trade_date, close_value
                    FROM market_close_history
                    WHERE instrument_name = ? AND trade_date > ?
                    ORDER BY trade_date
                    LIMIT 1 OFFSET ?
                    """,
                    (prediction["sector_name"], prediction["trade_date"], horizon - 1),
                ).fetchone()
                if target is None:
                    continue
                benchmark_target = conn.execute(
                    """
                    SELECT close_value
                    FROM market_close_history
                    WHERE instrument_name = 'ת״א-125' AND trade_date = ?
                    """,
                    (target["trade_date"],),
                ).fetchone()
                if benchmark_target is None:
                    continue

                sector_return = (
                    (target["close_value"] / prediction["sector_close"]) - 1
                ) * 100
                benchmark_return = (
                    (benchmark_target["close_value"] / prediction["benchmark_close"]) - 1
                ) * 100
                excess_return = sector_return - benchmark_return
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sector_score_outcomes
                        (prediction_id, horizon_sessions, outcome_date,
                         sector_return_pct, benchmark_return_pct,
                         excess_return_pct, outperformed)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prediction["id"],
                        horizon,
                        target["trade_date"],
                        round(sector_return, 4),
                        round(benchmark_return, 4),
                        round(excess_return, 4),
                        int(excess_return > 0),
                    ),
                )
                settled += 1
        conn.commit()
    return {"outcomes_settled": settled}


def _confidence_label(sample_size: int) -> str:
    if sample_size >= 100:
        return "גבוהה"
    if sample_size >= 50:
        return "בינונית"
    if sample_size >= CALIBRATION_MIN_DISPLAY_CASES:
        return "ראשונית"
    return "עדיין אין מספיק מקרים"


def get_sector_score_calibration(graph_scores: dict[str, int]) -> dict:
    """Return comparable-case evidence and a bounded historical adjustment.

    Comparable cases pool all Israeli sectors whose recorded final score was
    within ten points of today's graph score. This produces useful evidence
    sooner while keeping the matching rule explicit and deterministic.
    """
    calibration = {}
    with get_connection() as conn:
        for sector_name, score in graph_scores.items():
            horizon_stats = {}
            for horizon in CALIBRATION_HORIZONS:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS sample_size,
                           AVG(o.outperformed) * 100.0 AS hit_rate_pct,
                           AVG(o.excess_return_pct) AS avg_excess_return_pct
                    FROM sector_score_outcomes o
                    JOIN sector_score_predictions p ON p.id = o.prediction_id
                    WHERE o.horizon_sessions = ?
                      AND p.final_score BETWEEN ? AND ?
                    """,
                    (horizon, max(0, score - 10), min(100, score + 10)),
                ).fetchone()
                sample_size = int(row["sample_size"] or 0)
                horizon_stats[horizon] = {
                    "sample_size": sample_size,
                    "hit_rate_pct": (
                        round(float(row["hit_rate_pct"]), 1)
                        if row["hit_rate_pct"] is not None
                        else None
                    ),
                    "avg_excess_return_pct": (
                        round(float(row["avg_excess_return_pct"]), 2)
                        if row["avg_excess_return_pct"] is not None
                        else None
                    ),
                    "confidence": _confidence_label(sample_size),
                }

            preferred = next(
                (
                    horizon_stats[horizon]
                    for horizon in (20, 10, 30)
                    if horizon_stats[horizon]["sample_size"]
                    >= CALIBRATION_MIN_ADJUSTMENT_CASES
                ),
                None,
            )
            adjustment = 0
            if preferred:
                hit_rate_edge = (preferred["hit_rate_pct"] - 50) / 10
                excess_edge = preferred["avg_excess_return_pct"] / 2
                adjustment = max(-5, min(5, round(hit_rate_edge + excess_edge)))

            calibration[sector_name] = {
                "adjustment": adjustment,
                "horizons": horizon_stats,
                "matching_rule": "סקטורים עם ציון היסטורי בטווח של 10 נקודות",
                "minimum_cases_for_adjustment": CALIBRATION_MIN_ADJUSTMENT_CASES,
            }
    return calibration
