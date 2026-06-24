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

from nilo.backup import BackupError, cleanup_backup_files, create_backup, reserve_backup_path, sha256_file
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


if __name__ == "__main__":
    unittest.main()
