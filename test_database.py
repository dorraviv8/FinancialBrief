import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database


class SubscriberBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "subscribers.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def _add_subscriber(self, name, email, token, active=1):
        with database.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (name, email, token, active)
                VALUES (?, ?, ?, ?)
                """,
                (name, email, token, active),
            )
            conn.commit()

    def test_snapshot_subscribers_once_daily_overwrites_previous_snapshot(self):
        self._add_subscriber("Dor", "dor@example.com", "token-1")
        first = database.snapshot_subscribers_once_daily(force=True)

        self._add_subscriber("Rina", "rina@example.com", "token-2")
        second = database.snapshot_subscribers_once_daily(force=True)

        with database.get_connection() as conn:
            backup_rows = conn.execute(
                "SELECT email FROM subscriber_daily_backup ORDER BY id"
            ).fetchall()
            meta = conn.execute(
                "SELECT subscriber_count, active_count FROM subscriber_backup_meta WHERE id = 1"
            ).fetchone()

        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertEqual([row["email"] for row in backup_rows], ["dor@example.com", "rina@example.com"])
        self.assertEqual(meta["subscriber_count"], 2)
        self.assertEqual(meta["active_count"], 2)

    def test_snapshot_runs_only_once_per_day_without_force(self):
        self._add_subscriber("Dor", "dor@example.com", "token-1")

        first = database.snapshot_subscribers_once_daily()
        second = database.snapshot_subscribers_once_daily()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["reason"], "already_snapshotted_today")

    def test_snapshot_refuses_to_overwrite_when_live_table_lost_rows(self):
        self._add_subscriber("Dor", "dor@example.com", "token-1")
        self._add_subscriber("Rina", "rina@example.com", "token-2")
        database.snapshot_subscribers_once_daily(force=True)

        with database.get_connection() as conn:
            conn.execute("DELETE FROM subscribers WHERE email = ?", ("rina@example.com",))
            conn.execute("UPDATE subscriber_backup_meta SET last_snapshot_date = ?", ("2000-01-01",))
            conn.commit()

        result = database.snapshot_subscribers_once_daily()

        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "current_table_smaller_than_backup")
        self.assertEqual(result["current_count"], 1)
        self.assertEqual(result["backup_count"], 2)

    def test_restore_subscribers_from_backup(self):
        self._add_subscriber("Dor", "dor@example.com", "token-1")
        self._add_subscriber("Rina", "rina@example.com", "token-2")
        database.snapshot_subscribers_once_daily(force=True)

        with database.get_connection() as conn:
            conn.execute("DELETE FROM subscribers")
            conn.commit()

        result = database.restore_subscribers_from_backup()

        with database.get_connection() as conn:
            emails = conn.execute("SELECT email FROM subscribers ORDER BY id").fetchall()

        self.assertTrue(result["restored"])
        self.assertEqual(result["subscriber_count"], 2)
        self.assertEqual([row["email"] for row in emails], ["dor@example.com", "rina@example.com"])

    def test_sector_predictions_are_settled_after_exact_trading_sessions(self):
        closes = [
            {"date": f"2026-01-{day:02d}", "close": 100 + day}
            for day in range(1, 32)
        ] + [
            {"date": f"2026-02-{day:02d}", "close": 131 + day}
            for day in range(1, 11)
        ]
        benchmark = [
            {"date": point["date"], "close": 200 + index}
            for index, point in enumerate(closes)
        ]
        data = {
            "indices": {
                "ת״א-125": {
                    "last_value": benchmark[0]["close"],
                    "trend": {"last_close": benchmark[0]["close"]},
                    "history_closes": benchmark,
                }
            },
            "sectors": {
                "בנקים": {
                    "last_value": closes[0]["close"],
                    "trend": {
                        "as_of": closes[0]["date"],
                        "last_close": closes[0]["close"],
                    },
                    "history_closes": closes,
                }
            },
        }
        score = {
            "בנקים": {
                "graph_score": 68,
                "news_adjustment": 0,
                "calibration_adjustment": 0,
                "final_score": 68,
            }
        }

        history_result = database.record_market_close_history(data)
        prediction_result = database.record_sector_score_predictions(data, score)
        outcome_result = database.settle_sector_score_outcomes()

        self.assertEqual(history_result["close_rows_upserted"], 82)
        self.assertEqual(prediction_result["predictions_upserted"], 1)
        self.assertEqual(outcome_result["outcomes_settled"], 3)
        with database.get_connection() as conn:
            outcomes = conn.execute(
                """
                SELECT horizon_sessions, outcome_date, excess_return_pct
                FROM sector_score_outcomes ORDER BY horizon_sessions
                """
            ).fetchall()
        self.assertEqual([row["horizon_sessions"] for row in outcomes], [10, 20, 30])
        self.assertEqual(outcomes[0]["outcome_date"], closes[10]["date"])

    def test_calibration_waits_for_sample_and_is_bounded(self):
        empty = database.get_sector_score_calibration({"בנקים": 65})
        self.assertEqual(empty["בנקים"]["adjustment"], 0)
        self.assertEqual(
            empty["בנקים"]["horizons"][20]["sample_size"], 0
        )

        with database.get_connection() as conn:
            for index in range(30):
                cursor = conn.execute(
                    """
                    INSERT INTO sector_score_predictions
                        (trade_date, sector_name, graph_score, news_adjustment,
                         calibration_adjustment, final_score, sector_close,
                         benchmark_close, created_at)
                    VALUES (?, ?, 65, 0, 0, 65, 100, 100, ?)
                    """,
                    (f"2025-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}", f"סקטור-{index}", "2026-01-01"),
                )
                conn.execute(
                    """
                    INSERT INTO sector_score_outcomes
                        (prediction_id, horizon_sessions, outcome_date,
                         sector_return_pct, benchmark_return_pct,
                         excess_return_pct, outperformed)
                    VALUES (?, 20, '2026-01-01', 10, 0, 10, 1)
                    """,
                    (cursor.lastrowid,),
                )
            conn.commit()

        calibrated = database.get_sector_score_calibration({"בנקים": 65})

        self.assertEqual(calibrated["בנקים"]["adjustment"], 5)
        self.assertEqual(
            calibrated["בנקים"]["horizons"][20]["sample_size"], 30
        )

    def test_latest_sector_scores_supports_reader_change_summary(self):
        with database.get_connection() as conn:
            for trade_date, score in (("2026-08-25", 64), ("2026-08-26", 68)):
                conn.execute(
                    """
                    INSERT INTO sector_score_predictions
                        (trade_date, sector_name, graph_score, news_adjustment,
                         calibration_adjustment, final_score, sector_close,
                         benchmark_close, created_at)
                    VALUES (?, 'בנקים', ?, 0, 0, ?, 100, 100, ?)
                    """,
                    (trade_date, score, score, trade_date),
                )
            conn.commit()

        latest = database.get_latest_sector_scores()

        self.assertEqual(latest["בנקים"]["trade_date"], "2026-08-26")
        self.assertEqual(latest["בנקים"]["final_score"], 68)


if __name__ == "__main__":
    unittest.main()
