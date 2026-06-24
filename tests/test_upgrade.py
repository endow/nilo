from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.backup import BackupError
from nilo.cli import main
from nilo.upgrade import CommandResult, backup_database, run_upgrade
from tests.backup_helpers import make_sqlite_db


class FakeGitRunner:
    def __init__(self, repo: Path, *, dirty: bool = False, remote_rev: str = "a" * 40, fail_pull: bool = False, not_git: bool = False) -> None:
        self.repo = repo
        self.dirty = dirty
        self.remote_rev = remote_rev
        self.fail_pull = fail_pull
        self.not_git = not_git
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> CommandResult:
        self.commands.append(command)
        if command == ["git", "rev-parse", "--show-toplevel"]:
            if self.not_git:
                return CommandResult(128, "", "fatal: not a git repository")
            return CommandResult(0, str(self.repo), "")
        if command == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return CommandResult(0, "main", "")
        if command == ["git", "status", "--porcelain"]:
            return CommandResult(0, " M src/nilo/cli.py" if self.dirty else "", "")
        if command == ["git", "fetch"]:
            return CommandResult(0, "", "")
        if command == ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]:
            return CommandResult(0, "origin/main", "")
        if command == ["git", "rev-parse", "HEAD"]:
            return CommandResult(0, "a" * 40, "")
        if command == ["git", "rev-parse", "origin/main"]:
            return CommandResult(0, self.remote_rev, "")
        if command == ["git", "pull", "--ff-only"]:
            if self.fail_pull:
                return CommandResult(1, "", "fatal: Not possible to fast-forward")
            return CommandResult(0, "updated", "")
        if command[:4] == [sys.executable, "-m", "pip", "install"]:
            return CommandResult(0, "installed", "")
        if command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"]:
            return CommandResult(0, "migrated", "")
        return CommandResult(1, "", f"unexpected command: {command}")

    def command_was_run(self, expected: list[str]) -> bool:
        return expected in self.commands

    def command_prefix_was_run(self, expected: list[str]) -> bool:
        return any(command[: len(expected)] == expected for command in self.commands)


class UpgradeTests(unittest.TestCase):
    def test_cli_exposes_upgrade_command(self) -> None:
        with patch("nilo.cli_handlers.workflow.run_upgrade", return_value=0) as upgrade:
            main(["upgrade", "--dry-run"])

        upgrade.assert_called_once_with(dry_run=True, db_path=None)

    def test_cli_upgrade_propagates_nonzero_exit(self) -> None:
        with patch("nilo.cli_handlers.workflow.run_upgrade", return_value=1):
            with self.assertRaises(SystemExit) as context:
                main(["upgrade"])

        self.assertEqual(context.exception.code, 1)

    def test_version_flag_prints_version(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as context, redirect_stdout(output):
            main(["--version"])

        self.assertEqual(context.exception.code, 0)
        self.assertIn("nilo ", output.getvalue())

    def test_upgrade_stops_when_installation_is_not_git_checkout(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeGitRunner(Path(directory), not_git=True)
            output = io.StringIO()

            with redirect_stdout(output):
                code = run_upgrade(run=runner)

        self.assertEqual(code, 1)
        self.assertIn("does not appear to be an editable git checkout", output.getvalue())
        self.assertFalse(runner.command_was_run(["git", "pull", "--ff-only"]))

    def test_upgrade_stops_when_local_changes_exist(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeGitRunner(Path(directory), dirty=True)
            output = io.StringIO()

            with redirect_stdout(output):
                code = run_upgrade(run=runner)

        self.assertEqual(code, 1)
        self.assertIn("Local changes detected.", output.getvalue())
        self.assertFalse(runner.command_was_run(["git", "pull", "--ff-only"]))

    def test_upgrade_already_up_to_date_fetches_without_pull_reinstall_or_migration(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeGitRunner(Path(directory), remote_rev="a" * 40)
            output = io.StringIO()

            with redirect_stdout(output):
                code = run_upgrade(run=runner)

        self.assertEqual(code, 0)
        self.assertIn("Already up to date.", output.getvalue())
        self.assertIn("Nilo is up to date with origin/main.", output.getvalue())
        self.assertNotIn("Nilo is already", output.getvalue())
        self.assertNotIn("Current version:", output.getvalue())
        self.assertTrue(runner.command_was_run(["git", "fetch"]))
        self.assertFalse(runner.command_was_run(["git", "pull", "--ff-only"]))
        self.assertFalse(runner.command_prefix_was_run([sys.executable, "-m", "pip", "install"]))
        self.assertFalse(any(command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"] for command in runner.commands))

    def test_upgrade_with_updates_pulls_reinstalls_backs_up_database_and_migrates(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            db = repo / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            runner = FakeGitRunner(repo, remote_rev="b" * 40)
            output = io.StringIO()

            with redirect_stdout(output):
                code = run_upgrade(db_path=db, run=runner)

            backups = list((repo / ".nilo" / "backups").glob("nilo-*.db"))
            metas = list((repo / ".nilo" / "backups").glob("nilo-*.db.meta.json"))
            meta = json.loads(metas[0].read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertTrue(runner.command_was_run(["git", "pull", "--ff-only"]))
        self.assertTrue(runner.command_prefix_was_run([sys.executable, "-m", "pip", "install"]))
        self.assertTrue(any(command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"] for command in runner.commands))
        self.assertEqual(len(backups), 1)
        self.assertEqual(len(metas), 1)
        self.assertEqual(meta["reason"], "before-upgrade")
        self.assertIn("Nilo was updated from aaaaaaaaaaaa to bbbbbbbbbbbb.", output.getvalue())

    def test_upgrade_default_db_path_is_passed_to_migration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "nilo"
            project = root / "project"
            repo.mkdir()
            project.mkdir()
            db = project / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            runner = FakeGitRunner(repo, remote_rev="b" * 40)
            output = io.StringIO()

            with patch("nilo.upgrade.default_db_path", return_value=db), redirect_stdout(output):
                code = run_upgrade(run=runner)

            migrate_commands = [
                command
                for command in runner.commands
                if command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"]
            ]
            backups = list((project / ".nilo" / "backups").glob("nilo-*.db"))
            metas = list((project / ".nilo" / "backups").glob("nilo-*.db.meta.json"))

        self.assertEqual(code, 0)
        self.assertEqual(len(backups), 1)
        self.assertEqual(len(metas), 1)
        self.assertEqual(len(migrate_commands), 1)
        self.assertEqual(migrate_commands[0], [sys.executable, "-m", "nilo", "--db", str(db.resolve()), "migrate", "--apply"])

    def test_upgrade_stops_after_pull_failure_before_reinstall_and_migration(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeGitRunner(Path(directory), remote_rev="b" * 40, fail_pull=True)
            output = io.StringIO()

            with redirect_stdout(output):
                code = run_upgrade(run=runner)

        self.assertEqual(code, 1)
        self.assertIn("Upgrade failed: git pull --ff-only did not complete.", output.getvalue())
        self.assertFalse(runner.command_prefix_was_run([sys.executable, "-m", "pip", "install"]))
        self.assertFalse(any(command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"] for command in runner.commands))

    def test_upgrade_stops_when_database_backup_fails_before_migration(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            db = repo / ".nilo" / "nilo.db"
            make_sqlite_db(db)
            runner = FakeGitRunner(repo, remote_rev="b" * 40)
            output = io.StringIO()

            with patch("nilo.upgrade.backup_database", side_effect=OSError("permission denied")), redirect_stdout(output):
                code = run_upgrade(db_path=db, run=runner)

        self.assertEqual(code, 1)
        self.assertIn("Upgrade stopped: database backup failed.", output.getvalue())
        self.assertFalse(any(command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"] for command in runner.commands))

    def test_backup_database_translates_backup_error_to_os_error(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / ".nilo" / "nilo.db"
            make_sqlite_db(db)

            with patch("nilo.upgrade.create_backup", side_effect=BackupError("integrity failed")):
                with self.assertRaisesRegex(OSError, "integrity failed"):
                    backup_database(db)

    def test_dry_run_does_not_pull_reinstall_or_migrate(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeGitRunner(Path(directory), remote_rev="b" * 40)
            output = io.StringIO()

            with redirect_stdout(output):
                code = run_upgrade(dry_run=True, run=runner)

        self.assertEqual(code, 0)
        self.assertIn("Dry run: would run:", output.getvalue())
        self.assertFalse(runner.command_was_run(["git", "pull", "--ff-only"]))
        self.assertFalse(runner.command_prefix_was_run([sys.executable, "-m", "pip", "install"]))
        self.assertFalse(any(command[:3] == [sys.executable, "-m", "nilo"] and command[-2:] == ["migrate", "--apply"] for command in runner.commands))


if __name__ == "__main__":
    unittest.main()
