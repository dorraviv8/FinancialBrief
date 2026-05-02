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


if __name__ == "__main__":
    unittest.main()
