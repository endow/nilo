from __future__ import annotations

import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest

from nilo.review_lifecycle import (
    insert_review_attempt,
    review_attempt_is_active,
    review_request_is_active,
    review_request_is_non_blocking,
    set_review_attempt_status,
    update_review_attempt,
)
from nilo.store import Store
from nilo.state_audit import audit_schema_invariants


class ReviewLifecycleTest(unittest.TestCase):
    def test_old_database_gains_review_attempts_without_changing_requests(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            store.insert(
                "review_requests",
                {
                    "id": "review_test",
                    "task_id": "task_test",
                    "requester": "codex",
                    "reviewer": "claude-code",
                    "status": "claimed",
                    "reason": "互換確認",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
            store.close()

            reopened = Store(db)
            try:
                self.assertEqual(reopened.get("review_requests", "review_test")["status"], "claimed")
                columns = {row["name"] for row in reopened.conn.execute("PRAGMA table_info(review_attempts)")}
                self.assertIn("idempotency_key", columns)
                self.assertIn("retry_after", columns)
            finally:
                reopened.close()

    def test_old_read_only_database_can_be_audited_without_attempt_table(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            writable = Store(db)
            writable.conn.execute("DROP TABLE review_attempts")
            writable.conn.commit()
            writable.close()

            store = Store(db, read_only=True)
            try:
                self.assertFalse(store.has_table("review_attempts"))
                self.assertEqual(audit_schema_invariants(store, "project_test"), [])
            finally:
                store.close()

    def test_missing_attempt_table_triggers_pre_migration_backup(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            store.conn.execute("DROP TABLE review_attempts")
            store.conn.commit()
            store.close()

            migrated = Store(db)
            try:
                self.assertTrue(migrated.has_table("review_attempts"))
            finally:
                migrated.close()
            self.assertTrue(list((db.parent / "backups").glob("*.db")))

    def test_request_status_classes_keep_deferred_non_blocking(self) -> None:
        self.assertTrue(review_request_is_active("running"))
        self.assertTrue(review_request_is_active("claimed"))
        self.assertFalse(review_request_is_active("deferred"))
        self.assertTrue(review_request_is_non_blocking("deferred"))
        self.assertTrue(review_request_is_non_blocking("failed"))

    def test_attempt_terminal_transition_records_finished_at_and_json(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                insert_review_attempt(
                    store,
                    {
                        "id": "attempt_test",
                        "task_id": "task_test",
                        "review_request_id": "review_test",
                        "reviewer": "claude-code",
                        "backend_kind": "claude_code",
                        "transport": "direct_cli",
                        "status": "running",
                        "attempt_number": 1,
                        "idempotency_key": "review_test:1",
                        "diagnostics": {"safe": True},
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                )
                updated = set_review_attempt_status(store, "attempt_test", "rate_limited", retry_after="2026-01-01T01:00:00+00:00")
                self.assertFalse(review_attempt_is_active(updated["status"]))
                self.assertEqual(updated["diagnostics"], {"safe": True})
                self.assertTrue(updated["finished_at"])
                with self.assertRaisesRegex(ValueError, "rate_limited -> running"):
                    set_review_attempt_status(store, "attempt_test", "running")
                with self.assertRaisesRegex(ValueError, "rate_limited -> running"):
                    update_review_attempt(store, "attempt_test", {"status": "running"})
            finally:
                store.close()

    def test_attempt_rejects_unknown_status(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                with self.assertRaisesRegex(ValueError, "invalid review attempt status"):
                    insert_review_attempt(store, {"status": "waiting_forever"})
            finally:
                store.close()

    def test_attempt_number_is_unique_within_request(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            base = {
                "task_id": "task_test",
                "review_request_id": "review_test",
                "reviewer": "claude-code",
                "backend_kind": "claude_code",
                "transport": "direct_cli",
                "status": "starting",
                "attempt_number": 1,
                "diagnostics": {},
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            try:
                insert_review_attempt(store, {**base, "id": "attempt_one", "idempotency_key": "key_one"})
                with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed"):
                    insert_review_attempt(store, {**base, "id": "attempt_two", "idempotency_key": "key_two"})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
