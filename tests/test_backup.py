from __future__ import annotations

import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.backup import BackupError, cleanup_backup_files, create_backup, load_backup_records, reserve_backup_path, restore_backup, sha256_file, sqlite_sidecar_paths
from nilo.cli import main
from tests.backup_helpers import make_sqlite_db


class BackupTests(unittest.TestCase):
    def test_create_backup_writes_db_and_meta_with_integrity_and_sha256(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            conn = make_sqlite_db(db, keep_open=True)
            try:
                result = create_backup(db, reason="manual", cwd=root)
            finally:
                assert conn is not None
                conn.close()

            meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
            backup = sqlite3.connect(result.backup_path)
            try:
                rows = backup.execute("SELECT body FROM notes").fetchall()
            finally:
                backup.close()
            backup_exists = result.backup_path.exists()
            meta_exists = result.meta_path.exists()
            backup_sha = sha256_file(result.backup_path)
            backup_size = result.backup_path.stat().st_size

            self.assertEqual(rows, [("committed",)])
            self.assertTrue(backup_exists)
            self.assertTrue(meta_exists)
            self.assertEqual(meta["reason"], "manual")
            self.assertEqual(meta["integrity_check"], "ok")
            self.assertEqual(meta["sha256"], backup_sha)
            self.assertEqual(meta["db_size_bytes"], backup_size)

    def test_backup_cli_creates_local_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            conn = make_sqlite_db(db, keep_open=True)
            previous_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with redirect_stdout(output):
                    main(["--db", str(db), "backup", "--reason", "daily"])
            finally:
                assert conn is not None
                conn.close()
                os.chdir(previous_cwd)

            backups = list((root / ".nilo" / "backups").glob("nilo-*.db"))
            metas = list((root / ".nilo" / "backups").glob("nilo-*.db.meta.json"))
            meta = json.loads(metas[0].read_text(encoding="utf-8"))

        self.assertEqual(len(backups), 1)
        self.assertEqual(len(metas), 1)
        self.assertEqual(meta["reason"], "daily")
        self.assertIn("integrity_check: ok", output.getvalue())

    def test_create_backup_rejects_invalid_reason(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with self.assertRaisesRegex(BackupError, "invalid backup reason"):
                create_backup(db, reason="invalid")

    def test_create_backup_rejects_missing_database(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / ".nilo" / "missing.db"

            with self.assertRaisesRegex(BackupError, "database not found"):
                create_backup(db)

    def test_create_backup_cleans_reserved_file_when_post_copy_step_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.backup.integrity_check", side_effect=BackupError("forced integrity failure")):
                with self.assertRaisesRegex(BackupError, "forced integrity failure"):
                    create_backup(db, cwd=root)

            self.assertEqual(list((root / ".nilo" / "backups").glob("*.db")), [])
            self.assertEqual(list((root / ".nilo" / "backups").glob("*.meta.json")), [])

    def test_create_backup_cleans_reserved_file_when_backup_api_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_text("not sqlite", encoding="utf-8")

            with self.assertRaisesRegex(BackupError, "database backup failed"):
                create_backup(db, cwd=root)

            self.assertEqual(list((root / ".nilo" / "backups").glob("*.db")), [])
            self.assertEqual(list((root / ".nilo" / "backups").glob("*.meta.json")), [])

    def test_reserve_backup_path_uses_collision_suffix(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            first = reserve_backup_path(db)
            second = reserve_backup_path(db)
            try:
                self.assertNotEqual(first, second)
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
            finally:
                cleanup_backup_files(first)
                cleanup_backup_files(second)

    def test_load_backup_records_reads_meta_in_reverse_created_order(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            first = create_backup(db, reason="manual", cwd=root)
            second = create_backup(db, reason="daily", cwd=root)
            first_meta = json.loads(first.meta_path.read_text(encoding="utf-8"))
            second_meta = json.loads(second.meta_path.read_text(encoding="utf-8"))
            first_meta["created_at"] = "2026-01-01T00:00:00+00:00"
            second_meta["created_at"] = "2026-01-02T00:00:00+00:00"
            first.meta_path.write_text(json.dumps(first_meta), encoding="utf-8")
            second.meta_path.write_text(json.dumps(second_meta), encoding="utf-8")

            records = load_backup_records(db)

            self.assertEqual([record.meta["reason"] for record in records], ["daily", "manual"])
            self.assertEqual(records[0].backup_path, second.backup_path)
            self.assertEqual(records[1].backup_path, first.backup_path)
            self.assertTrue(records[0].db_exists)

    def test_backups_cli_lists_existing_backups(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            create_backup(db, reason="daily", cwd=root)
            output = io.StringIO()

            with redirect_stdout(output):
                main(["--db", str(db), "backups"])

            text = output.getvalue()
            self.assertIn("created_at", text)
            self.assertIn("daily", text)
            self.assertIn("ok", text)
            self.assertIn("present", text)

    def test_backups_cli_marks_orphaned_meta_as_missing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            backup = create_backup(db, reason="daily", cwd=root)
            backup.backup_path.unlink()
            output = io.StringIO()

            with redirect_stdout(output):
                main(["--db", str(db), "backups"])

            self.assertIn("missing", output.getvalue())

    def test_restore_backup_verifies_backup_and_creates_before_restore_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db, body="old")
            backup = create_backup(db, reason="manual", cwd=root)
            db.unlink()
            make_sqlite_db(db, body="new")
            for sidecar in sqlite_sidecar_paths(db):
                sidecar.write_bytes(b"stale sidecar")

            result = restore_backup(backup.backup_path, db, cwd=root)

            restored = sqlite3.connect(db)
            before = sqlite3.connect(result.before_restore.backup_path) if result.before_restore else None
            try:
                restored_rows = restored.execute("SELECT body FROM notes").fetchall()
                before_rows = before.execute("SELECT body FROM notes").fetchall() if before else []
            finally:
                restored.close()
                if before is not None:
                    before.close()

            self.assertEqual(restored_rows, [("old",)])
            self.assertEqual(before_rows, [("new",)])
            self.assertEqual(result.integrity_check, "ok")
            self.assertFalse(any(sidecar.exists() for sidecar in sqlite_sidecar_paths(db)))

    def test_restore_backup_rejects_sha256_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db, body="old")
            backup = create_backup(db, reason="manual", cwd=root)
            make_sqlite_db(db, body="new")
            meta = json.loads(backup.meta_path.read_text(encoding="utf-8"))
            meta["sha256"] = "0" * 64
            backup.meta_path.write_text(json.dumps(meta), encoding="utf-8")

            with self.assertRaisesRegex(BackupError, "sha256 mismatch"):
                restore_backup(backup.backup_path, db, cwd=root)

    def test_restore_cli_restores_database(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db, body="old")
            backup = create_backup(db, reason="manual", cwd=root)
            db.unlink()
            make_sqlite_db(db, body="new")
            output = io.StringIO()

            with redirect_stdout(output):
                main(["--db", str(db), "restore", str(backup.backup_path)])

            restored = sqlite3.connect(db)
            try:
                rows = restored.execute("SELECT body FROM notes").fetchall()
            finally:
                restored.close()

            self.assertEqual(rows, [("old",)])
            self.assertIn("before_restore_backup:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
