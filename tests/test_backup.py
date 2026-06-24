from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.backup import (
    BackupError,
    cleanup_backup_files,
    create_backup,
    export_backup_files,
    load_backup_records,
    prune_backup_records,
    reserve_backup_path,
    render_post_command,
    restore_backup,
    restore_encrypted_backup,
    sha256_file,
    sqlite_sidecar_paths,
    truncate_output,
)
from nilo.cli import main
from tests.backup_helpers import make_sqlite_db


def fake_age_run(args, stdout=None, stderr=None, check=False):
    if "-r" in args:
        source = Path(args[-1])
        assert stdout is not None
        stdout.write(source.read_bytes()[::-1])
        return subprocess.CompletedProcess(args, 0, b"", b"")
    if "-d" in args:
        source = Path(args[-1])
        assert stdout is not None
        stdout.write(source.read_bytes()[::-1])
        return subprocess.CompletedProcess(args, 0, b"", b"")
    return subprocess.CompletedProcess(args, 1, b"", b"unsupported fake age command")


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

    def test_create_backup_exports_db_and_meta_with_verified_sha256(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "exported"
            make_sqlite_db(db)

            result = create_backup(db, reason="manual", cwd=root, export_dir=export_dir)

            exported = result.meta["exported_to"]
            exported_backup = root / exported["backup_path"]
            exported_meta = root / exported["meta_path"]
            exported_meta_json = json.loads(exported_meta.read_text(encoding="utf-8"))
            backup = sqlite3.connect(exported_backup)
            try:
                rows = backup.execute("SELECT body FROM notes").fetchall()
            finally:
                backup.close()

            self.assertEqual(rows, [("committed",)])
            self.assertTrue(exported_backup.exists())
            self.assertTrue(exported_meta.exists())
            self.assertEqual(exported["sha256"], result.meta["sha256"])
            self.assertEqual(sha256_file(exported_backup), result.meta["sha256"])
            self.assertEqual(exported_meta_json["exported_to"], exported)
            self.assertEqual(exported_meta_json["backup_path"], exported["backup_path"])

    def test_create_backup_keeps_local_backup_when_export_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "exported"
            make_sqlite_db(db)

            with patch("nilo.backup.shutil.copy2", side_effect=OSError("export failed")):
                with self.assertRaisesRegex(OSError, "export failed"):
                    create_backup(db, reason="manual", cwd=root, export_dir=export_dir)

            local_backups = list((root / ".nilo" / "backups").glob("nilo-*.db"))
            local_metas = list((root / ".nilo" / "backups").glob("nilo-*.db.meta.json"))
            exported_files = list(export_dir.glob("*"))

            self.assertEqual(len(local_backups), 1)
            self.assertEqual(len(local_metas), 1)
            self.assertEqual(exported_files, [])

    def test_export_backup_files_cleans_export_on_sha256_mismatch_and_keeps_local_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "exported"
            make_sqlite_db(db)
            result = create_backup(db, reason="manual", cwd=root)
            bad_meta = dict(result.meta)
            bad_meta["sha256"] = "0" * 64

            with self.assertRaisesRegex(BackupError, "export sha256 mismatch"):
                export_backup_files(result.backup_path, result.meta_path, bad_meta, export_dir, root)

            self.assertTrue(result.backup_path.exists())
            self.assertTrue(result.meta_path.exists())
            self.assertEqual(list(export_dir.glob("*")), [])

    def test_backup_cli_exports_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "cloud backups"
            make_sqlite_db(db)
            output = io.StringIO()

            with redirect_stdout(output):
                main(["--db", str(db), "backup", "--export", str(export_dir)])

            exported_backups = list(export_dir.glob("nilo-*.db"))
            exported_metas = list(export_dir.glob("nilo-*.db.meta.json"))

            self.assertEqual(len(exported_backups), 1)
            self.assertEqual(len(exported_metas), 1)
            self.assertIn("exported_to:", output.getvalue())
            self.assertIn("exported_meta:", output.getvalue())

    def test_create_backup_encrypts_with_age_and_removes_plaintext_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.backup.shutil.which", return_value="age") as which, patch("nilo.backup.run", side_effect=fake_age_run):
                result = create_backup(db, reason="manual", cwd=root, encrypt=True, recipient="age1recipient")

            meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
            plaintext_backup = Path(str(result.backup_path).removesuffix(".age"))
            plaintext_meta = plaintext_backup.with_suffix(plaintext_backup.suffix + ".meta.json")

            self.assertTrue(result.backup_path.name.endswith(".db.age"))
            self.assertFalse(plaintext_backup.exists())
            self.assertFalse(plaintext_meta.exists())
            self.assertTrue(meta["encrypted"])
            self.assertEqual(meta["encryption"]["tool"], "age")
            self.assertEqual(meta["encryption"]["recipient"], "age1recipient")
            self.assertEqual(meta["sha256"], meta["encryption"]["ciphertext_sha256"])
            self.assertRegex(meta["encryption"]["plaintext_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(sha256_file(result.backup_path), meta["encryption"]["ciphertext_sha256"])
            which.assert_called_once_with("age")

    def test_create_backup_encrypt_rejects_missing_age_without_plaintext_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.backup.shutil.which", return_value=None):
                with self.assertRaisesRegex(BackupError, "age command not found"):
                    create_backup(db, reason="manual", cwd=root, encrypt=True, recipient="age1recipient")

            self.assertFalse((root / ".nilo" / "backups").exists())

    def test_backup_cli_encrypt_requires_recipient(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with self.assertRaisesRegex(SystemExit, "age recipient is required"):
                main(["--db", str(db), "backup", "--encrypt"])

            self.assertFalse((root / ".nilo" / "backups").exists())

    def test_backup_cli_recipient_requires_encrypt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with self.assertRaisesRegex(SystemExit, "--recipient requires --encrypt"):
                main(["--db", str(db), "backup", "--recipient", "age1recipient"])

            self.assertFalse((root / ".nilo" / "backups").exists())

    def test_backup_cli_encrypt_exports_encrypted_artifacts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "encrypted exports"
            make_sqlite_db(db)
            output = io.StringIO()

            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                with redirect_stdout(output):
                    main(["--db", str(db), "backup", "--encrypt", "--recipient", "age1recipient", "--export", str(export_dir)])

            exported_backups = list(export_dir.glob("nilo-*.db.age"))
            exported_metas = list(export_dir.glob("nilo-*.db.age.meta.json"))

            self.assertEqual(len(exported_backups), 1)
            self.assertEqual(len(exported_metas), 1)
            self.assertIn("encrypted: true", output.getvalue())
            self.assertIn("ciphertext_sha256:", output.getvalue())

    def test_backup_cli_encrypt_uses_configured_age_recipient(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            config = root / ".nilo" / "config.toml"
            config.write_text('[backup]\nage_recipient = "age1configured"\n', encoding="utf-8")
            output = io.StringIO()

            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                with redirect_stdout(output):
                    main(["--db", str(db), "backup", "--encrypt"])

            meta_path = next((root / ".nilo" / "backups").glob("nilo-*.db.age.meta.json"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

            self.assertIn("encrypted: true", output.getvalue())
            self.assertEqual(meta["encryption"]["recipient"], "age1configured")

    def test_backup_cli_save_recipient_updates_config_after_success(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            output = io.StringIO()

            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                with redirect_stdout(output):
                    main(["--db", str(db), "backup", "--encrypt", "--recipient", "age1saved", "--save-recipient"])

            config = (root / ".nilo" / "config.toml").read_text(encoding="utf-8")

            self.assertIn('[backup]', config)
            self.assertIn('age_recipient = "age1saved"', config)
            self.assertIn("saved_recipient:", output.getvalue())

    def test_backup_cli_save_recipient_does_not_overwrite_similar_config_key(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            config_path = root / ".nilo" / "config.toml"
            config_path.write_text('[backup]\nage_recipient_backup = "keep-me"\n', encoding="utf-8")
            output = io.StringIO()

            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                with redirect_stdout(output):
                    main(["--db", str(db), "backup", "--encrypt", "--recipient", "age1saved", "--save-recipient"])

            config = config_path.read_text(encoding="utf-8")

            self.assertIn('age_recipient_backup = "keep-me"', config)
            self.assertIn('age_recipient = "age1saved"', config)
            self.assertIn("saved_recipient:", output.getvalue())

    def test_backup_cli_save_recipient_requires_encrypt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with self.assertRaisesRegex(SystemExit, "--save-recipient requires --encrypt"):
                main(["--db", str(db), "backup", "--save-recipient"])

            self.assertFalse((root / ".nilo" / "config.toml").exists())

    def test_export_backup_files_uses_collision_suffix(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "exported"
            make_sqlite_db(db)
            result = create_backup(db, reason="manual", cwd=root)
            export_dir.mkdir()
            collision = export_dir / result.backup_path.name
            collision.write_bytes(b"existing")
            collision.with_suffix(collision.suffix + ".meta.json").write_text("{}", encoding="utf-8")

            updated_meta = export_backup_files(result.backup_path, result.meta_path, result.meta, export_dir, root)

            exported = updated_meta["exported_to"]
            exported_backup = root / exported["backup_path"]
            self.assertTrue(exported_backup.name.endswith("-01.db"))
            self.assertEqual(sha256_file(exported_backup), result.meta["sha256"])

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

    def test_backups_prune_keeps_newest_prunable_and_protects_safety_backups_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            old_daily = create_backup(db, reason="daily", cwd=root)
            new_daily = create_backup(db, reason="daily", cwd=root)
            manual = create_backup(db, reason="manual", cwd=root)
            before_upgrade = create_backup(db, reason="before-upgrade", cwd=root)
            before_migration = create_backup(db, reason="before-migration", cwd=root)
            before_restore = create_backup(db, reason="before-restore", cwd=root)
            for result, created_at in (
                (old_daily, "2026-01-01T00:00:00+00:00"),
                (new_daily, "2026-01-02T00:00:00+00:00"),
                (manual, "2026-01-03T00:00:00+00:00"),
                (before_upgrade, "2026-01-04T00:00:00+00:00"),
                (before_migration, "2026-01-05T00:00:00+00:00"),
                (before_restore, "2026-01-06T00:00:00+00:00"),
            ):
                meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
                meta["created_at"] = created_at
                result.meta_path.write_text(json.dumps(meta), encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                main(["--db", str(db), "backups", "prune", "--keep", "1"])

            self.assertFalse(old_daily.backup_path.exists())
            self.assertFalse(old_daily.meta_path.exists())
            self.assertTrue(new_daily.backup_path.exists())
            self.assertTrue(manual.backup_path.exists())
            self.assertTrue(before_upgrade.backup_path.exists())
            self.assertTrue(before_migration.backup_path.exists())
            self.assertTrue(before_restore.backup_path.exists())
            self.assertIn("pruned: 1", output.getvalue())
            self.assertIn("protected: 4", output.getvalue())

    def test_backups_prune_include_reason_can_explicitly_prune_protected_reason(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            old_upgrade = create_backup(db, reason="before-upgrade", cwd=root)
            new_upgrade = create_backup(db, reason="before-upgrade", cwd=root)

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "backups", "prune", "--keep", "1", "--include-reason", "before-upgrade"])

            self.assertFalse(old_upgrade.backup_path.exists())
            self.assertFalse(old_upgrade.meta_path.exists())
            self.assertTrue(new_upgrade.backup_path.exists())

    def test_backups_prune_dry_run_does_not_delete_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            old_daily = create_backup(db, reason="daily", cwd=root)
            new_daily = create_backup(db, reason="daily", cwd=root)
            output = io.StringIO()

            with redirect_stdout(output):
                main(["--db", str(db), "backups", "prune", "--keep", "1", "--dry-run"])

            self.assertTrue(old_daily.backup_path.exists())
            self.assertTrue(old_daily.meta_path.exists())
            self.assertTrue(new_daily.backup_path.exists())
            self.assertIn("would_prune: 1", output.getvalue())

    def test_backups_prune_keep_zero_prunes_all_prunable_records(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            first = create_backup(db, reason="daily", cwd=root)
            second = create_backup(db, reason="other", cwd=root)

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "backups", "prune", "--keep", "0"])

            self.assertFalse(first.backup_path.exists())
            self.assertFalse(second.backup_path.exists())

    def test_prune_backup_records_rejects_invalid_reason(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with self.assertRaisesRegex(BackupError, "invalid backup reason"):
                prune_backup_records(db, keep=1, include_reasons={"invalid"})

    def test_create_backup_runs_post_command_and_records_result_in_meta(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.backup.run", return_value=subprocess.CompletedProcess(["sync-tool"], 0, "copied\n", "")) as command:
                result = create_backup(
                    db,
                    reason="daily",
                    cwd=root,
                    post_command=["sync-tool", "copy", "{backup_path}", "{meta_path}", "{reason}", "{sha256}", "{encrypted}"],
                )

            meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
            argv = command.call_args.args[0]

            self.assertEqual(argv[0:3], ["sync-tool", "copy", result.meta["backup_path"]])
            self.assertEqual(argv[3], meta["post_command"]["argv"][3])
            self.assertTrue(argv[3].endswith(".db.meta.json"))
            self.assertEqual(argv[4], "daily")
            self.assertEqual(argv[5], result.meta["sha256"])
            self.assertEqual(argv[6], "false")
            self.assertEqual(meta["post_command"]["argv"], argv)
            self.assertEqual(meta["post_command"]["returncode"], 0)
            self.assertTrue(meta["post_command"]["success"])
            self.assertEqual(meta["post_command"]["stdout"], "copied\n")

    def test_create_backup_records_post_command_result_in_exported_meta(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            export_dir = root / "exports"
            make_sqlite_db(db)

            with patch("nilo.backup.run", return_value=subprocess.CompletedProcess(["sync-tool"], 0, "copied\n", "")):
                result = create_backup(db, cwd=root, export_dir=export_dir, post_command=["sync-tool", "{exported_meta_path}"])

            exported_meta_path = root / result.meta["exported_to"]["meta_path"]
            exported_meta = json.loads(exported_meta_path.read_text(encoding="utf-8"))

            self.assertEqual(exported_meta["post_command"], result.meta["post_command"])
            self.assertEqual(exported_meta["backup_path"], result.meta["exported_to"]["backup_path"])

    def test_create_backup_records_failed_post_command_before_raising(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.backup.run", return_value=subprocess.CompletedProcess(["sync-tool"], 23, "", "denied")):
                with self.assertRaisesRegex(BackupError, "post_command failed with exit code 23"):
                    create_backup(db, cwd=root, post_command=["sync-tool", "{backup_path}"])

            meta_path = next((root / ".nilo" / "backups").glob("*.meta.json"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

            self.assertEqual(meta["post_command"]["returncode"], 23)
            self.assertFalse(meta["post_command"]["success"])
            self.assertEqual(meta["post_command"]["stderr"], "denied")

    def test_create_backup_records_post_command_start_failure_before_raising(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.backup.run", side_effect=OSError("missing command")):
                with self.assertRaisesRegex(BackupError, "post_command failed to start"):
                    create_backup(db, cwd=root, post_command=["missing-command"])

            meta_path = next((root / ".nilo" / "backups").glob("*.meta.json"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))

            self.assertIsNone(meta["post_command"]["returncode"])
            self.assertFalse(meta["post_command"]["success"])
            self.assertIn("missing command", meta["post_command"]["stderr"])

    def test_render_post_command_rejects_unsupported_template_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            result = create_backup(db, cwd=root)

            with self.assertRaisesRegex(BackupError, "unsupported post_command template token"):
                render_post_command(["sync-tool", "{unknown}"], result.backup_path, result.meta_path, result.meta, root)

    def test_truncate_output_limits_post_command_output(self) -> None:
        self.assertEqual(truncate_output("short"), "short")
        self.assertTrue(truncate_output("x" * 9000).endswith("\n[truncated]"))

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

    def test_restore_encrypted_backup_decrypts_verifies_and_removes_temp_plaintext(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db, body="old")
            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                backup = create_backup(db, reason="manual", cwd=root, encrypt=True, recipient="age1recipient")
            db.unlink()
            make_sqlite_db(db, body="new")

            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                result = restore_encrypted_backup(backup.backup_path, db, cwd=root)

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
            self.assertEqual(result.backup_path, backup.backup_path)
            self.assertEqual(list(db.parent.glob("*.decrypt.tmp")), [])

    def test_restore_cli_requires_decrypt_for_encrypted_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db, body="old")
            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                backup = create_backup(db, reason="manual", cwd=root, encrypt=True, recipient="age1recipient")

            with self.assertRaisesRegex(SystemExit, "encrypted backup requires restore --decrypt"):
                main(["--db", str(db), "restore", str(backup.backup_path)])

    def test_restore_cli_decrypt_restores_encrypted_backup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            make_sqlite_db(db, body="old")
            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                backup = create_backup(db, reason="manual", cwd=root, encrypt=True, recipient="age1recipient")
            db.unlink()
            make_sqlite_db(db, body="new")
            output = io.StringIO()

            with patch("nilo.backup.shutil.which", return_value="age"), patch("nilo.backup.run", side_effect=fake_age_run):
                with redirect_stdout(output):
                    main(["--db", str(db), "restore", "--decrypt", str(backup.backup_path)])

            restored = sqlite3.connect(db)
            try:
                rows = restored.execute("SELECT body FROM notes").fetchall()
            finally:
                restored.close()

            self.assertEqual(rows, [("old",)])
            self.assertIn("from:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
