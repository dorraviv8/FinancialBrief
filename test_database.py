import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
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

    def test_delivery_claim_is_idempotent_forceable_and_retryable(self):
        briefing_run = database.save_briefing_run(
            "2026-08-28",
            "2026-08-27",
            "2026-08-28T07:00:00+03:00",
            "### דוח\nתוכן",
        )
        message_id = "<daily-test@example.com>"

        first = database.claim_email_delivery(
            briefing_run["id"], "Reader@Example.com", message_id
        )
        database.mark_email_delivery_sent(
            briefing_run["id"], "reader@example.com"
        )
        duplicate = database.claim_email_delivery(
            briefing_run["id"], "reader@example.com", message_id
        )
        forced = database.claim_email_delivery(
            briefing_run["id"], "reader@example.com", message_id, force=True
        )
        database.mark_email_delivery_failed(
            briefing_run["id"], "reader@example.com", "temporary SMTP failure"
        )
        retry = database.claim_email_delivery(
            briefing_run["id"], "reader@example.com", message_id
        )
        database.mark_email_delivery_sent(
            briefing_run["id"], "reader@example.com"
        )
        final = database.finalize_briefing_run(briefing_run["id"])

        self.assertTrue(first["send"])
        self.assertEqual(first["attempt"], 1)
        self.assertFalse(duplicate["send"])
        self.assertEqual(duplicate["reason"], "already_sent")
        self.assertTrue(forced["send"])
        self.assertEqual(forced["reason"], "forced")
        self.assertTrue(retry["send"])
        self.assertEqual(retry["attempt"], 3)
        self.assertEqual(retry["reason"], "retry_failed")
        self.assertEqual(final["status"], "completed")
        with database.get_connection() as conn:
            delivery = conn.execute(
                "SELECT status, attempts, message_id FROM email_deliveries"
            ).fetchone()
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(delivery["attempts"], 3)
        self.assertEqual(delivery["message_id"], message_id)

    def test_ambiguous_in_progress_delivery_is_not_automatically_retried(self):
        briefing_run = database.save_briefing_run(
            "2026-08-28", None, None, "### דוח\nתוכן"
        )

        first = database.claim_email_delivery(
            briefing_run["id"], "reader@example.com", "<test@example.com>"
        )
        second = database.claim_email_delivery(
            briefing_run["id"], "reader@example.com", "<test@example.com>"
        )

        self.assertTrue(first["send"])
        self.assertFalse(second["send"])
        self.assertEqual(
            second["reason"], "delivery_in_progress_or_ambiguous"
        )

    def test_v2_feature_ledger_predictions_and_live_settlement(self):
        horizons = {}
        for horizon in (10, 20, 30):
            horizons[horizon] = {
                "final_score": 68,
                "probability_positive_pct": 62,
                "probability_outperform_pct": 60,
                "risk_quality": 65,
                "confidence_pct": 58,
                "expected_excess_return_pct": 1.2,
                "expected_excess_low_pct": -0.5,
                "expected_excess_high_pct": 2.4,
                "news_catalyst_adjustment": 1,
            }
        bundle = {
            "model_version": "test-v2",
            "feature_version": "test-features",
            "historical_samples": [],
            "evaluation": {horizon: {"sample_count": 50} for horizon in (10, 20, 30)},
            "sectors": {
                "בנקים": {
                    "features": {
                        "data_date": "2026-01-01",
                        "relative_return_20d_pct": 2.0,
                    },
                    "catalyst": {"adjustment": 1},
                    "horizons": horizons,
                }
            },
        }
        data = {
            "indices": {"ת״א-125": {"trend": {"last_close": 100}}},
            "sectors": {
                "בנקים": {"trend": {"last_close": 100, "as_of": "2026-01-01"}}
            },
        }
        result = database.record_v2_recommendation_bundle(
            data,
            bundle,
            {"בנקים": {"legacy_final_score": 65}},
            "v1",
        )
        start = date(2026, 1, 1)
        with database.get_connection() as conn:
            for offset in range(1, 31):
                trade_date = (start + timedelta(days=offset)).isoformat()
                conn.execute(
                    "INSERT INTO market_close_history VALUES (?, ?, ?)",
                    (trade_date, "בנקים", 100 + offset),
                )
                conn.execute(
                    "INSERT INTO market_close_history VALUES (?, 'ת״א-125', ?)",
                    (trade_date, 100 + (offset / 2)),
                )
            conn.commit()

        settled = database.settle_v2_prediction_outcomes()
        comparison = database.get_v2_live_comparison()

        self.assertEqual(result["v2_features_upserted"], 1)
        self.assertEqual(result["v2_predictions_upserted"], 3)
        self.assertEqual(settled["v2_outcomes_settled"], 3)
        self.assertEqual(comparison["sample_size"], 1)
        self.assertFalse(comparison["eligible_for_promotion"])
        with database.get_connection() as conn:
            outcomes = conn.execute(
                "SELECT COUNT(*) FROM sector_v2_outcomes"
            ).fetchone()[0]
        self.assertEqual(outcomes, 3)


if __name__ == "__main__":
    unittest.main()
