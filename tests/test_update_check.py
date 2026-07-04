from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.cli import TOP_LEVEL_COMMANDS, main, should_show_update_notice
from nilo.instruction import build_instruction
from nilo.update_check import (
    CommandResult,
    auto_update_notice,
    check_for_update,
    load_state,
    reset_update_check_state,
    run_command,
    save_state,
    should_auto_check,
    state_path,
    UpdateCheckResult,
)


class FakeTagRunner:
    def __init__(
        self,
        repo: Path,
        *,
        current: str = "0.3.1",
        latest: str = "0.3.2",
        not_git: bool = False,
        no_tags: bool = False,
        fetch_fails: bool = False,
        current_is_ancestor: bool = True,
        git_missing: bool = False,
        package_not_git: bool = False,
    ) -> None:
        self.repo = repo
        self.current = current
        self.latest = latest
        self.not_git = not_git
        self.no_tags = no_tags
        self.fetch_fails = fetch_fails
        self.current_is_ancestor = current_is_ancestor
        self.git_missing = git_missing
        self.package_not_git = package_not_git
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> CommandResult:
        self.commands.append(command)
        if self.git_missing:
            return CommandResult(127, "", "git executable not found")
        if command == ["git", "rev-parse", "--show-toplevel"]:
            if self.not_git:
                return CommandResult(128, "", "fatal: not a git repository")
            if self.package_not_git and cwd != Path.cwd():
                return CommandResult(128, "", "fatal: not a git repository")
            return CommandResult(0, str(self.repo), "")
        if command == ["git", "fetch", "--tags", "--quiet"]:
            if self.fetch_fails:
                return CommandResult(1, "", "network down")
            return CommandResult(0, "", "")
        if command == ["git", "describe", "--tags", "--abbrev=0"]:
            if self.no_tags:
                return CommandResult(128, "", "fatal: No names found")
            return CommandResult(0, self.current, "")
        if command == ["git", "describe", "--tags", "--abbrev=0", "origin/main"]:
            if self.no_tags:
                return CommandResult(128, "", "fatal: No names found")
            return CommandResult(0, self.latest, "")
        if command == ["git", "merge-base", "--is-ancestor", self.current, self.latest]:
            return CommandResult(0 if self.current_is_ancestor else 1, "", "")
        return CommandResult(1, "", f"unexpected command: {command}")


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class UpdateCheckTests(unittest.TestCase):
    def test_update_check_detects_available_tag_update_and_records_state(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            runner = FakeTagRunner(Path(directory))
            with patch("nilo.update_check.Path.home", return_value=home):
                result = check_for_update(run=runner)
                state = load_state()

        self.assertTrue(result.update_available)
        self.assertEqual(result.current_version, "0.3.1")
        self.assertEqual(result.latest_version, "0.3.2")
        self.assertIn(["git", "fetch", "--tags", "--quiet"], runner.commands)
        self.assertEqual(state["lastStatus"], "update_available")
        self.assertEqual(state["lastCurrentVersion"], "0.3.1")
        self.assertEqual(state["lastLatestVersion"], "0.3.2")

    def test_update_check_up_to_date(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeTagRunner(Path(directory), current="0.3.2", latest="0.3.2")
            result = check_for_update(run=runner, record_checked=False)

        self.assertEqual(result.status, "up_to_date")
        self.assertFalse(result.update_available)

    def test_update_check_does_not_report_local_ahead_as_update(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeTagRunner(Path(directory), current="0.3.3", latest="0.3.2", current_is_ancestor=False)
            result = check_for_update(run=runner, record_checked=False)

        self.assertEqual(result.status, "up_to_date")
        self.assertFalse(result.update_available)

    def test_update_check_skips_non_git_without_fetch(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeTagRunner(Path(directory), not_git=True)
            result = check_for_update(run=runner, record_checked=False)

        self.assertEqual(result.status, "skipped")
        self.assertFalse(any(command[:2] == ["git", "fetch"] for command in runner.commands))

    def test_update_check_uses_current_directory_nilo_checkout_when_installation_is_not_git(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "pyproject.toml").write_text('[project]\nname = "nilo"\n', encoding="utf-8")
            runner = FakeTagRunner(repo, package_not_git=True)

            with patch("pathlib.Path.cwd", return_value=repo):
                result = check_for_update(run=runner, record_checked=False)

        self.assertTrue(result.update_available)
        self.assertIn(["git", "fetch", "--tags", "--quiet"], runner.commands)

    def test_update_check_skips_when_git_executable_is_missing(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeTagRunner(Path(directory), git_missing=True)
            result = check_for_update(run=runner, record_checked=False)

        self.assertEqual(result.status, "skipped")
        self.assertIn("git executable not found", result.reason)

    def test_run_command_converts_oserror_to_command_result(self) -> None:
        with patch("nilo.command_runner.subprocess.run", side_effect=FileNotFoundError("git missing")):
            result = run_command(["git", "status"], Path.cwd())

        self.assertEqual(result.returncode, 127)
        self.assertIn("git missing", result.stderr)

    def test_update_check_skips_missing_tags_without_raising(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeTagRunner(Path(directory), no_tags=True)
            result = check_for_update(run=runner, record_checked=False)

        self.assertEqual(result.status, "skipped")
        self.assertIn("No names found", result.reason)

    def test_auto_notice_prints_once_per_version_and_throttles_fetch(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            runner = FakeTagRunner(Path(directory))
            with patch.dict(os.environ, {"CI": ""}), patch("nilo.update_check.Path.home", return_value=home), patch("sys.stdout", TtyStringIO()):
                first = auto_update_notice(run=runner)
                second = auto_update_notice(run=runner)
                state = load_state()

        self.assertIn("Nilo の更新があります: 0.3.1 -> 0.3.2", first)
        self.assertEqual(second, "")
        self.assertEqual(state["lastNotifiedVersion"], "0.3.2")
        self.assertEqual(runner.commands.count(["git", "fetch", "--tags", "--quiet"]), 1)

    def test_auto_notice_suppressed_by_env_ci_and_non_tty(self) -> None:
        with TemporaryDirectory() as directory:
            runner = FakeTagRunner(Path(directory))
            with patch.dict(os.environ, {"NILO_NO_UPDATE_CHECK": "1"}), patch("sys.stdout", TtyStringIO()):
                self.assertEqual(auto_update_notice(run=runner), "")
            with patch.dict(os.environ, {"CI": "true"}), patch("sys.stdout", TtyStringIO()):
                self.assertEqual(auto_update_notice(run=runner), "")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(auto_update_notice(run=runner), "")

        self.assertEqual(runner.commands, [])

    def test_should_auto_check_uses_one_day_interval(self) -> None:
        now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
        recent = {"lastCheckedAt": (now - timedelta(hours=23)).isoformat()}
        stale = {"lastCheckedAt": (now - timedelta(days=1, minutes=1)).isoformat()}

        self.assertFalse(should_auto_check(recent, now=now))
        self.assertTrue(should_auto_check(stale, now=now))

    def test_update_check_command_only_checks_and_does_not_upgrade(self) -> None:
        output = io.StringIO()
        with patch("nilo.cli_handlers.workflow.check_for_update") as check, patch("nilo.cli_handlers.workflow.is_disabled", return_value=False):
            check.return_value = UpdateCheckResult("update_available", "0.3.1", "0.3.2")
            with redirect_stdout(output):
                main(["update-check"])

        self.assertIn("Nilo update available: 0.3.1 -> 0.3.2", output.getvalue())

    def test_update_check_command_respects_disabled_env(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {"NILO_NO_UPDATE_CHECK": "1"}), patch("nilo.cli_handlers.workflow.check_for_update") as check:
            with redirect_stdout(output):
                main(["update-check"])

        check.assert_not_called()
        self.assertIn("Nilo update check skipped: disabled by NILO_NO_UPDATE_CHECK.", output.getvalue())

    def test_lightweight_agent_workflow_commands_do_not_call_auto_notice(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            output = TtyStringIO()
            with patch("nilo.cli.auto_update_notice", return_value="Nilo の更新があります: 0.3.1 -> 0.3.2\n更新するには: nilo upgrade") as notice:
                with patch("sys.stdout", output):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    notice.reset_mock()
                    output.seek(0)
                    output.truncate(0)
                    main(["--db", str(db), "status", "--project", "project_test"])
                    main(["--db", str(db), "next", "--project", "project_test"])
                    main(["--db", str(db), "queue", "--project", "project_test"])

        notice.assert_not_called()
        self.assertNotIn("Nilo の更新があります: 0.3.1 -> 0.3.2", output.getvalue())

    def test_doctor_command_prints_auto_notice_after_command(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            output = TtyStringIO()
            with patch("nilo.cli.auto_update_notice", return_value="Nilo の更新があります: 0.3.1 -> 0.3.2\n更新するには: nilo upgrade"):
                with patch("sys.stdout", output):
                    main(["--db", str(db), "doctor"])

        self.assertIn("Nilo の更新があります: 0.3.1 -> 0.3.2", output.getvalue())

    def test_machine_readable_commands_do_not_print_auto_notice(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            output = TtyStringIO()
            with patch("nilo.cli.auto_update_notice", return_value="Nilo の更新があります: 0.3.1 -> 0.3.2\n更新するには: nilo upgrade") as notice:
                with patch("sys.stdout", output):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "nilo"])
                    main(["--db", str(db), "status", "--project", "nilo", "--json"])
                    main(["--db", str(db), "status", "--project", "nilo", "--ai"])

        notice.assert_not_called()
        self.assertNotIn("Nilo の更新があります", output.getvalue())

    def test_update_notice_command_filter_only_allows_doctor_and_update_check(self) -> None:
        for command in TOP_LEVEL_COMMANDS - {"doctor", "update-check"}:
            with self.subTest(command=command):
                self.assertFalse(should_show_update_notice(type("Args", (), {"command": command})()))

        self.assertTrue(should_show_update_notice(type("Args", (), {"command": "doctor"})()))
        self.assertTrue(should_show_update_notice(type("Args", (), {"command": "update-check"})()))

    def test_instruction_note_uses_cached_update_and_forbids_auto_upgrade(self) -> None:
        project = {
            "id": "project_test",
            "name": "Project",
            "tech_stack": [],
            "default_completion_criteria": [],
            "rules": [],
        }
        task = {
            "id": "task_test",
            "title": "Do work",
            "description": "",
            "acceptance_criteria": [],
            "degradation_mode": "normal",
            "task_type": "implementation",
            "risk_level": "medium",
            "requires_understanding_check": False,
        }
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with patch.dict(os.environ, {"CI": ""}), patch("nilo.update_check.Path.home", return_value=home), patch("nilo.update_check.repo_root_from_package", return_value=(None, "")):
                save_state(
                    {
                        "lastCheckedAt": "2026-06-24T12:00:00+00:00",
                        "lastNotifiedVersion": "",
                        "snoozeUntil": None,
                        "lastStatus": "update_available",
                        "lastCurrentVersion": "0.3.1",
                        "lastLatestVersion": "0.3.2",
                    }
                )
                body, _ = build_instruction(project, task)

        self.assertIn("Nilo の更新があります: 0.3.1 -> 0.3.2", body)
        self.assertIn("AI agent は自動で `nilo upgrade` を実行してはいけません。", body)

    def test_instruction_note_is_suppressed_when_checkout_reached_cached_latest(self) -> None:
        project = {
            "id": "project_test",
            "name": "Project",
            "tech_stack": [],
            "default_completion_criteria": [],
            "rules": [],
        }
        task = {
            "id": "task_test",
            "title": "Do work",
            "description": "",
            "acceptance_criteria": [],
            "degradation_mode": "normal",
            "task_type": "implementation",
            "risk_level": "medium",
            "requires_understanding_check": False,
        }
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with (
                patch("nilo.update_check.Path.home", return_value=home),
                patch("nilo.update_check.repo_root_from_package", return_value=(Path(directory), "")),
                patch("nilo.update_check.git_latest_tag", return_value=("0.3.2", "")),
            ):
                save_state(
                    {
                        "lastCheckedAt": "2026-06-24T12:00:00+00:00",
                        "lastNotifiedVersion": "",
                        "lastStatus": "update_available",
                        "lastCurrentVersion": "0.3.1",
                        "lastLatestVersion": "0.3.2",
                    }
                )
                body, _ = build_instruction(project, task)

        self.assertNotIn("Nilo の更新があります: 0.3.1 -> 0.3.2", body)

    def test_reset_update_check_state_removes_state_file(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with patch("nilo.update_check.Path.home", return_value=home):
                save_state({"lastCheckedAt": "2026-06-24T12:00:00+00:00", "lastNotifiedVersion": "0.3.2"})
                self.assertTrue(state_path().exists())
                reset_update_check_state()
                self.assertFalse(state_path().exists())


if __name__ == "__main__":
    unittest.main()
