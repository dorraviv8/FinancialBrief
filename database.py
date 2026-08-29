"""
Shared SQLite database functions for subscriber management.
Used by both app.py (signup/unsubscribe) and financial_brief.py (send to all).
"""

import sqlite3
import secrets
import json
import os
import hashlib
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

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


def _ensure_column(conn, table: str, column: str, definition: str) -> None:
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_v2_feature_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date      TEXT NOT NULL,
                sector_name     TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                features_json   TEXT NOT NULL,
                sector_close    REAL NOT NULL,
                benchmark_close REAL NOT NULL,
                created_at      TEXT NOT NULL,
                UNIQUE (trade_date, sector_name, feature_version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_v2_backtest_samples (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_date          TEXT NOT NULL,
                sector_name          TEXT NOT NULL,
                horizon_sessions     INTEGER NOT NULL,
                feature_version      TEXT NOT NULL,
                features_json        TEXT NOT NULL,
                outcome_date         TEXT NOT NULL,
                sector_return_pct    REAL NOT NULL,
                benchmark_return_pct REAL NOT NULL,
                excess_return_pct    REAL NOT NULL,
                positive             INTEGER NOT NULL,
                outperformed         INTEGER NOT NULL,
                max_drawdown_pct     REAL NOT NULL,
                created_at           TEXT NOT NULL,
                UNIQUE (
                    sample_date, sector_name, horizon_sessions, feature_version
                )
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_v2_predictions (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date                  TEXT NOT NULL,
                sector_name                 TEXT NOT NULL,
                horizon_sessions            INTEGER NOT NULL,
                model_version               TEXT NOT NULL,
                feature_version             TEXT NOT NULL,
                score                       INTEGER NOT NULL,
                legacy_score                INTEGER NOT NULL,
                probability_positive_pct    REAL NOT NULL,
                probability_outperform_pct  REAL NOT NULL,
                risk_quality                INTEGER NOT NULL,
                confidence_pct              INTEGER NOT NULL,
                expected_excess_return_pct  REAL NOT NULL,
                expected_excess_low_pct     REAL,
                expected_excess_high_pct    REAL,
                news_adjustment              INTEGER NOT NULL,
                active_model                 TEXT NOT NULL,
                features_json                TEXT NOT NULL,
                catalyst_json                TEXT NOT NULL,
                sector_close                 REAL NOT NULL,
                benchmark_close              REAL NOT NULL,
                created_at                   TEXT NOT NULL,
                UNIQUE (
                    trade_date, sector_name, horizon_sessions, model_version
                )
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_v2_outcomes (
                prediction_id        INTEGER PRIMARY KEY,
                outcome_date         TEXT NOT NULL,
                sector_return_pct    REAL NOT NULL,
                benchmark_return_pct REAL NOT NULL,
                excess_return_pct    REAL NOT NULL,
                positive             INTEGER NOT NULL,
                outperformed         INTEGER NOT NULL,
                max_drawdown_pct     REAL NOT NULL,
                FOREIGN KEY (prediction_id) REFERENCES sector_v2_predictions(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_model_evaluations (
                evaluation_date TEXT NOT NULL,
                model_version   TEXT NOT NULL,
                horizon_sessions INTEGER NOT NULL,
                metrics_json    TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (evaluation_date, model_version, horizon_sessions)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_v2_predictions_outcome
            ON sector_v2_predictions (horizon_sessions, trade_date, sector_name)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_v2_backtest_horizon
            ON sector_v2_backtest_samples (horizon_sessions, sample_date)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_news (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint        TEXT UNIQUE NOT NULL,
                source             TEXT NOT NULL,
                reliability        TEXT NOT NULL,
                title              TEXT NOT NULL,
                summary            TEXT,
                link               TEXT,
                companies_json     TEXT NOT NULL DEFAULT '[]',
                published          TEXT,
                first_seen_at      TEXT NOT NULL,
                last_seen_at       TEXT NOT NULL,
                selected_for_report TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_weekly_news_published
            ON weekly_news (published, selected_for_report)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_collection_state (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                last_collected_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fx_rate_history (
                rate_date       TEXT NOT NULL,
                currency        TEXT NOT NULL,
                ils_rate        REAL NOT NULL,
                unit            REAL,
                official_update TEXT,
                observed_at     TEXT NOT NULL,
                PRIMARY KEY (rate_date, currency)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fx_rate_history_currency
            ON fx_rate_history (currency, rate_date)
        """)
        _ensure_column(conn, "briefing_runs", "report_type", "TEXT NOT NULL DEFAULT 'daily'")
        _ensure_column(conn, "briefing_runs", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "briefing_runs", "qa_status", "TEXT NOT NULL DEFAULT 'legacy'")
        _ensure_column(conn, "briefing_runs", "prepared_at", "TEXT")
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


# ── Weekly source ledger ──────────────────────────────────────────────────────

def _news_fingerprint(item: dict) -> str:
    normalized_title = " ".join(
        re.sub(r"[^0-9a-zA-Zא-ת]+", " ", str(item.get("title") or "").lower()).split()
    )
    identity = f"{item.get('source') or ''}\0{normalized_title}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def save_weekly_news(
    items: list[dict],
    collected_at: str | None = None,
    mark_collection: bool = True,
) -> dict:
    """Upsert source-linked articles so Sunday can rank the full trading week."""
    now = collected_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for item in items:
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        if not title or not source:
            continue
        rows.append((
            _news_fingerprint(item),
            source,
            str(item.get("reliability") or "financial_press"),
            title,
            str(item.get("summary") or "").strip()[:1200] or None,
            item.get("link"),
            json.dumps(item.get("companies") or [], ensure_ascii=False),
            item.get("published"),
            now,
            now,
        ))
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO weekly_news
                (fingerprint, source, reliability, title, summary, link,
                 companies_json, published, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                summary = CASE
                    WHEN excluded.summary IS NOT NULL THEN excluded.summary
                    ELSE weekly_news.summary
                END,
                link = COALESCE(excluded.link, weekly_news.link),
                companies_json = excluded.companies_json,
                published = COALESCE(excluded.published, weekly_news.published),
                last_seen_at = excluded.last_seen_at
            """,
            rows,
        )
        if mark_collection:
            conn.execute(
                """
                INSERT INTO news_collection_state (id, last_collected_at)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET last_collected_at = excluded.last_collected_at
                """,
                (now,),
            )
        conn.commit()
    return {"news_upserted": len(rows), "collected_at": now}


def should_collect_weekly_news(minimum_hours: float = 4.0) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_collected_at FROM news_collection_state WHERE id = 1"
        ).fetchone()
    if not row:
        return True
    try:
        last = datetime.fromisoformat(row["last_collected_at"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=minimum_hours)


def _parse_utc_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_weekly_news(window_start, window_end) -> list[dict]:
    """Return deduplicated articles published inside an inclusive local-date window."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM weekly_news ORDER BY published, first_seen_at"
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        published = _parse_utc_datetime(item.get("published"))
        observed = _parse_utc_datetime(item.get("first_seen_at"))
        timestamp = published or observed
        if timestamp is None:
            continue
        local_date = timestamp.astimezone(ISRAEL_TZ).date()
        if window_start <= local_date <= window_end:
            try:
                item["companies"] = json.loads(item.pop("companies_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["companies"] = []
            items.append(item)
    return items


def finalize_weekly_news(
    report_date: str,
    window_start,
    window_end,
    selected_ids: list[int],
) -> dict:
    """Keep selected evidence and discard unused articles after successful delivery."""
    candidates = get_weekly_news(window_start, window_end)
    candidate_ids = {int(item["id"]) for item in candidates}
    selected = candidate_ids.intersection(int(item_id) for item_id in selected_ids)
    unused = candidate_ids - selected
    with get_connection() as conn:
        if selected:
            placeholders = ",".join("?" for _ in selected)
            conn.execute(
                f"UPDATE weekly_news SET selected_for_report = ? WHERE id IN ({placeholders})",
                (report_date, *sorted(selected)),
            )
        if unused:
            placeholders = ",".join("?" for _ in unused)
            conn.execute(
                f"DELETE FROM weekly_news WHERE id IN ({placeholders})",
                tuple(sorted(unused)),
            )
        selected_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        orphan_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        conn.execute(
            "DELETE FROM weekly_news WHERE selected_for_report IS NOT NULL AND published < ?",
            (selected_cutoff,),
        )
        conn.execute(
            "DELETE FROM weekly_news WHERE selected_for_report IS NULL AND first_seen_at < ?",
            (orphan_cutoff,),
        )
        conn.commit()
    return {"selected_retained": len(selected), "unused_deleted": len(unused)}


def _rate_date(rate: dict, observed_at: str) -> str | None:
    for value in (rate.get("last_update"), observed_at):
        parsed = _parse_utc_datetime(value)
        if parsed is not None:
            return parsed.astimezone(ISRAEL_TZ).date().isoformat()
    return None


def record_fx_rates(data: dict, observed_at: str | None = None) -> dict:
    observed = observed_at or data.get("as_of") or datetime.now(timezone.utc).isoformat()
    rows = []
    for currency in ("USD", "EUR"):
        rate = data.get("exchange_rates", {}).get("rates", {}).get(currency, {})
        rate_date = _rate_date(rate, observed)
        value = _safe_float(rate.get("ils_rate"))
        if rate_date and value is not None and value > 0:
            rows.append((
                rate_date,
                currency,
                value,
                _safe_float(rate.get("unit")),
                rate.get("last_update"),
                observed,
            ))
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO fx_rate_history
                (rate_date, currency, ils_rate, unit, official_update, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rate_date, currency) DO UPDATE SET
                ils_rate = excluded.ils_rate,
                unit = excluded.unit,
                official_update = excluded.official_update,
                observed_at = excluded.observed_at
            """,
            rows,
        )
        conn.commit()
    return {"fx_rates_upserted": len(rows)}


def get_weekly_fx_rates(window_start, window_end) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT rate_date, currency, ils_rate, unit, official_update
            FROM fx_rate_history
            WHERE rate_date BETWEEN ? AND ? AND currency IN ('USD', 'EUR')
            ORDER BY currency, rate_date
            """,
            (window_start.isoformat(), window_end.isoformat()),
        ).fetchall()
        snapshots = conn.execute(
            "SELECT snapshot_time, data_json FROM market_snapshots ORDER BY snapshot_time"
        ).fetchall()
    grouped_map = {"USD": {}, "EUR": {}}
    for row in rows:
        grouped_map[row["currency"]][row["rate_date"]] = dict(row)
    # Compatibility backfill: deployments that predate fx_rate_history still
    # have up to ten days of official rates inside lightweight snapshots.
    for snapshot in snapshots:
        try:
            data = json.loads(snapshot["data_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for currency in ("USD", "EUR"):
            rate = data.get("exchange_rates", {}).get("rates", {}).get(currency, {})
            rate_date = _rate_date(rate, snapshot["snapshot_time"])
            value = _safe_float(rate.get("ils_rate"))
            if (
                rate_date
                and window_start.isoformat() <= rate_date <= window_end.isoformat()
                and value is not None
                and value > 0
            ):
                grouped_map[currency].setdefault(rate_date, {
                    "rate_date": rate_date,
                    "currency": currency,
                    "ils_rate": value,
                    "unit": _safe_float(rate.get("unit")),
                    "official_update": rate.get("last_update"),
                })
    result = {}
    for currency, point_map in grouped_map.items():
        points = [point_map[key] for key in sorted(point_map)]
        if not points:
            result[currency] = {"available": False}
            continue
        first, last = points[0], points[-1]
        change = (
            ((last["ils_rate"] / first["ils_rate"]) - 1) * 100
            if first["ils_rate"]
            else None
        )
        result[currency] = {
            "available": True,
            "start_date": first["rate_date"],
            "start_rate": first["ils_rate"],
            "end_date": last["rate_date"],
            "end_rate": last["ils_rate"],
            "change_pct": round(change, 4) if change is not None else None,
        }
    return result


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
            int(score.get("legacy_final_score", score["final_score"])),
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
    report_type: str = "daily",
    metadata: dict | None = None,
    qa_status: str = "legacy",
) -> dict:
    """Persist one immutable generated briefing per Israel report date."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content_hash = hashlib.sha256(brief_text.encode("utf-8")).hexdigest()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO briefing_runs
                (report_date, market_close_date, as_of, content_hash,
                 brief_text, status, created_at, updated_at,
                 report_type, metadata_json, qa_status, prepared_at)
            VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?)
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
                report_type,
                json.dumps(metadata or {}, ensure_ascii=False),
                qa_status,
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


# ── Recommendation V2 feature ledger and evaluation ─────────────────────────

def record_v2_recommendation_bundle(
    data: dict,
    bundle: dict,
    legacy_scores: dict,
    active_model: str,
) -> dict:
    """Persist inputs, historical audit cases, evaluations and live forecasts."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    benchmark = data.get("indices", {}).get("ת״א-125", {})
    benchmark_close = _safe_float(
        benchmark.get("trend", {}).get("last_close")
        or benchmark.get("last_value")
    )
    if benchmark_close is None:
        return {
            "v2_features_upserted": 0,
            "v2_predictions_upserted": 0,
            "reason": "benchmark_close_missing",
        }

    feature_rows = []
    prediction_rows = []
    for sector_name, recommendation in bundle.get("sectors", {}).items():
        sector = data.get("sectors", {}).get(sector_name, {})
        sector_close = _safe_float(
            sector.get("trend", {}).get("last_close")
            or sector.get("last_value")
        )
        features = recommendation.get("features", {})
        trade_date = features.get("data_date")
        if not trade_date or sector_close is None:
            continue
        features_json = json.dumps(
            features, ensure_ascii=False, separators=(",", ":")
        )
        feature_rows.append((
            trade_date,
            sector_name,
            bundle["feature_version"],
            features_json,
            sector_close,
            benchmark_close,
            now,
        ))
        legacy_item = legacy_scores.get(sector_name, {})
        legacy_score = int(legacy_item.get(
            "legacy_final_score", legacy_item.get("final_score", 50)
        ))
        catalyst_json = json.dumps(
            recommendation.get("catalyst", {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for horizon, forecast in recommendation.get("horizons", {}).items():
            prediction_rows.append((
                trade_date,
                sector_name,
                int(horizon),
                bundle["model_version"],
                bundle["feature_version"],
                int(forecast["final_score"]),
                legacy_score,
                float(forecast["probability_positive_pct"]),
                float(forecast["probability_outperform_pct"]),
                int(forecast["risk_quality"]),
                int(forecast["confidence_pct"]),
                float(forecast["expected_excess_return_pct"]),
                _safe_float(forecast.get("expected_excess_low_pct")),
                _safe_float(forecast.get("expected_excess_high_pct")),
                int(forecast.get("news_catalyst_adjustment", 0)),
                active_model,
                features_json,
                catalyst_json,
                sector_close,
                benchmark_close,
                now,
            ))

    backtest_rows = []
    for sample in bundle.get("historical_samples", []):
        features_json = json.dumps(
            sample["features"], ensure_ascii=False, separators=(",", ":")
        )
        for horizon, outcome in sample.get("outcomes", {}).items():
            backtest_rows.append((
                sample["sample_date"],
                sample["sector_name"],
                int(horizon),
                bundle["feature_version"],
                features_json,
                outcome["outcome_date"],
                float(outcome["sector_return_pct"]),
                float(outcome["benchmark_return_pct"]),
                float(outcome["excess_return_pct"]),
                int(outcome["positive"]),
                int(outcome["outperformed"]),
                float(outcome["max_drawdown_pct"]),
                now,
            ))

    evaluation_date = max(
        (row[0] for row in feature_rows),
        default=datetime.now(timezone.utc).date().isoformat(),
    )
    evaluation_rows = [
        (
            evaluation_date,
            bundle["model_version"],
            int(horizon),
            json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
            now,
        )
        for horizon, metrics in bundle.get("evaluation", {}).items()
    ]

    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO sector_v2_feature_snapshots
                (trade_date, sector_name, feature_version, features_json,
                 sector_close, benchmark_close, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, sector_name, feature_version) DO UPDATE SET
                features_json = excluded.features_json,
                sector_close = excluded.sector_close,
                benchmark_close = excluded.benchmark_close,
                created_at = excluded.created_at
            """,
            feature_rows,
        )
        conn.executemany(
            """
            INSERT INTO sector_v2_backtest_samples
                (sample_date, sector_name, horizon_sessions, feature_version,
                 features_json, outcome_date, sector_return_pct,
                 benchmark_return_pct, excess_return_pct, positive,
                 outperformed, max_drawdown_pct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                sample_date, sector_name, horizon_sessions, feature_version
            ) DO UPDATE SET
                features_json = excluded.features_json,
                outcome_date = excluded.outcome_date,
                sector_return_pct = excluded.sector_return_pct,
                benchmark_return_pct = excluded.benchmark_return_pct,
                excess_return_pct = excluded.excess_return_pct,
                positive = excluded.positive,
                outperformed = excluded.outperformed,
                max_drawdown_pct = excluded.max_drawdown_pct,
                created_at = excluded.created_at
            """,
            backtest_rows,
        )
        conn.executemany(
            """
            INSERT INTO sector_v2_predictions
                (trade_date, sector_name, horizon_sessions, model_version,
                 feature_version, score, legacy_score,
                 probability_positive_pct, probability_outperform_pct,
                 risk_quality, confidence_pct, expected_excess_return_pct,
                 expected_excess_low_pct, expected_excess_high_pct,
                 news_adjustment, active_model, features_json, catalyst_json,
                 sector_close, benchmark_close, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                trade_date, sector_name, horizon_sessions, model_version
            ) DO UPDATE SET
                score = excluded.score,
                legacy_score = excluded.legacy_score,
                probability_positive_pct = excluded.probability_positive_pct,
                probability_outperform_pct = excluded.probability_outperform_pct,
                risk_quality = excluded.risk_quality,
                confidence_pct = excluded.confidence_pct,
                expected_excess_return_pct = excluded.expected_excess_return_pct,
                expected_excess_low_pct = excluded.expected_excess_low_pct,
                expected_excess_high_pct = excluded.expected_excess_high_pct,
                news_adjustment = excluded.news_adjustment,
                active_model = excluded.active_model,
                features_json = excluded.features_json,
                catalyst_json = excluded.catalyst_json,
                sector_close = excluded.sector_close,
                benchmark_close = excluded.benchmark_close,
                created_at = excluded.created_at
            """,
            prediction_rows,
        )
        conn.executemany(
            """
            INSERT INTO recommendation_model_evaluations
                (evaluation_date, model_version, horizon_sessions,
                 metrics_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(
                evaluation_date, model_version, horizon_sessions
            ) DO UPDATE SET
                metrics_json = excluded.metrics_json,
                created_at = excluded.created_at
            """,
            evaluation_rows,
        )
        conn.commit()
    return {
        "v2_features_upserted": len(feature_rows),
        "v2_backtest_rows_upserted": len(backtest_rows),
        "v2_predictions_upserted": len(prediction_rows),
        "v2_evaluations_upserted": len(evaluation_rows),
    }


def settle_v2_prediction_outcomes() -> dict:
    """Settle live V2 forecasts after their exact TASE-session horizon."""
    settled = 0
    with get_connection() as conn:
        predictions = conn.execute(
            """
            SELECT p.id, p.trade_date, p.sector_name, p.horizon_sessions,
                   p.sector_close, p.benchmark_close
            FROM sector_v2_predictions p
            LEFT JOIN sector_v2_outcomes o ON o.prediction_id = p.id
            WHERE o.prediction_id IS NULL
            ORDER BY p.trade_date, p.sector_name, p.horizon_sessions
            """
        ).fetchall()
        for prediction in predictions:
            target = conn.execute(
                """
                SELECT trade_date, close_value
                FROM market_close_history
                WHERE instrument_name = ? AND trade_date > ?
                ORDER BY trade_date
                LIMIT 1 OFFSET ?
                """,
                (
                    prediction["sector_name"],
                    prediction["trade_date"],
                    prediction["horizon_sessions"] - 1,
                ),
            ).fetchone()
            if target is None:
                continue
            benchmark_target = conn.execute(
                """
                SELECT close_value FROM market_close_history
                WHERE instrument_name = 'ת״א-125' AND trade_date = ?
                """,
                (target["trade_date"],),
            ).fetchone()
            if benchmark_target is None:
                continue
            path = conn.execute(
                """
                SELECT close_value FROM market_close_history
                WHERE instrument_name = ? AND trade_date > ? AND trade_date <= ?
                ORDER BY trade_date
                """,
                (
                    prediction["sector_name"],
                    prediction["trade_date"],
                    target["trade_date"],
                ),
            ).fetchall()
            values = [prediction["sector_close"]] + [row[0] for row in path]
            peak = values[0]
            max_drawdown = 0.0
            for value in values:
                peak = max(peak, value)
                max_drawdown = min(max_drawdown, ((value / peak) - 1) * 100)
            sector_return = (
                (target["close_value"] / prediction["sector_close"]) - 1
            ) * 100
            benchmark_return = (
                (benchmark_target["close_value"] / prediction["benchmark_close"]) - 1
            ) * 100
            excess_return = sector_return - benchmark_return
            conn.execute(
                """
                INSERT INTO sector_v2_outcomes
                    (prediction_id, outcome_date, sector_return_pct,
                     benchmark_return_pct, excess_return_pct, positive,
                     outperformed, max_drawdown_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction["id"],
                    target["trade_date"],
                    round(sector_return, 4),
                    round(benchmark_return, 4),
                    round(excess_return, 4),
                    int(sector_return > 0),
                    int(excess_return > 0),
                    round(max_drawdown, 4),
                ),
            )
            settled += 1
        conn.commit()
    return {"v2_outcomes_settled": settled}


def get_v2_live_comparison(horizon: int = 20) -> dict:
    """Compare matured V2 probabilities with the legacy score out of sample."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.probability_outperform_pct, p.legacy_score,
                   o.outperformed, o.excess_return_pct
            FROM sector_v2_predictions p
            JOIN sector_v2_outcomes o ON o.prediction_id = p.id
            WHERE p.horizon_sessions = ?
            """,
            (horizon,),
        ).fetchall()
    if not rows:
        return {
            "sample_size": 0,
            "eligible_for_promotion": False,
            "active_model": "v1",
        }
    v2_brier = sum(
        ((row["probability_outperform_pct"] / 100) - row["outperformed"]) ** 2
        for row in rows
    ) / len(rows)
    legacy_brier = sum(
        ((row["legacy_score"] / 100) - row["outperformed"]) ** 2
        for row in rows
    ) / len(rows)
    v2_accuracy = sum(
        (row["probability_outperform_pct"] >= 50) == bool(row["outperformed"])
        for row in rows
    ) / len(rows)
    legacy_accuracy = sum(
        (row["legacy_score"] >= 50) == bool(row["outperformed"])
        for row in rows
    ) / len(rows)
    eligible = (
        len(rows) >= 60
        and v2_brier <= legacy_brier * 0.95
        and v2_accuracy >= legacy_accuracy
    )
    return {
        "sample_size": len(rows),
        "v2_brier": round(v2_brier, 4),
        "legacy_brier": round(legacy_brier, 4),
        "v2_accuracy_pct": round(v2_accuracy * 100, 1),
        "legacy_accuracy_pct": round(legacy_accuracy * 100, 1),
        "avg_excess_return_pct": round(
            sum(row["excess_return_pct"] for row in rows) / len(rows), 2
        ),
        "eligible_for_promotion": eligible,
        "active_model": "v2" if eligible else "v1",
    }


def choose_active_recommendation_model(configured_mode: str) -> dict:
    """Resolve shadow/manual/automatic promotion without hiding the evidence."""
    mode = (configured_mode or "shadow").lower()
    comparison = get_v2_live_comparison()
    if mode == "active":
        active_model = "v2"
    elif mode == "auto" and comparison.get("eligible_for_promotion"):
        active_model = "v2"
    else:
        active_model = "v1"
    return {
        "configured_mode": mode,
        "active_model": active_model,
        "comparison": comparison,
    }
