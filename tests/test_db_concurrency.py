from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from chabo.db import Database


class TestDatabaseConnectionAndConcurrency(unittest.TestCase):
    def test_connect_context_manager_closes_connection_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.sqlite3"))
            db.init()

            with db.connect() as conn:
                row = conn.execute("PRAGMA busy_timeout").fetchone()
                self.assertEqual(int(row[0]), 30000)

            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_connect_context_manager_closes_connection_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "test.sqlite3"))
            db.init()

            leaked_conn: sqlite3.Connection | None = None
            with self.assertRaises(RuntimeError):
                with db.connect() as conn:
                    leaked_conn = conn
                    conn.execute(
                        "INSERT INTO accounts (id, telegram_user_id, role, display_name) VALUES ('a1', '1001', 'advertiser', 'Adv')"
                    )
                    raise RuntimeError("boom")

            self.assertIsNotNone(leaked_conn)
            with self.assertRaises(sqlite3.ProgrammingError):
                leaked_conn.execute("SELECT 1")  # type: ignore[union-attr]

            with db.connect() as verify_conn:
                count = verify_conn.execute("SELECT COUNT(*) FROM accounts WHERE id = 'a1'").fetchone()[0]
                self.assertEqual(count, 0)

    def test_concurrent_transactions_succeed_under_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(str(Path(tmpdir) / "concurrent.sqlite3"))
            db.init()

            errors: list[Exception] = []

            def worker(idx: int) -> None:
                try:
                    with db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO accounts (id, telegram_user_id, role, display_name) VALUES (?, ?, 'advertiser', ?)",
                            (f"acc_{idx}", f"tg_{idx}", f"User {idx}"),
                        )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            with db.connect() as conn:
                total = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
                self.assertEqual(total, 16)


if __name__ == "__main__":
    unittest.main()
