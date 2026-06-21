from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nilo.snapshot import evidence_status, review_result_status
from nilo.verification import run_local_verification


class VerificationTests(unittest.TestCase):
    def test_run_local_verification_uses_argv_for_simple_command(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed) as run_mock, patch(
            "nilo.verification.current_git_snapshot",
            return_value={
                "git_head": "abc123",
                "git_diff_hash": "diffhash",
                "working_tree_dirty": False,
                "git_status_porcelain": "",
                "observed_paths": [],
                "git_available": True,
            },
        ):
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["source"], "nilo_executed")
        self.assertEqual(result["git_diff_hash"], "diffhash")
        self.assertEqual(result["metadata"]["execution_mode"], "argv")
        self.assertEqual(run_mock.call_args_list[0].kwargs["shell"], False)
        self.assertEqual(run_mock.call_args_list[0].args[0], ["python", "-m", "unittest"])

    def test_run_local_verification_falls_back_to_shell_for_control_tokens(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed) as run_mock, patch(
            "nilo.verification.current_git_snapshot",
            return_value={
                "git_head": "abc123",
                "git_diff_hash": "diffhash",
                "working_tree_dirty": False,
                "git_status_porcelain": "",
                "observed_paths": [],
                "git_available": True,
            },
        ):
            result = run_local_verification("python -m unittest && python -m pytest", Path.cwd(), 10)

        self.assertEqual(result["metadata"]["execution_mode"], "shell")
        self.assertEqual(result["metadata"]["execution_reason"], "shell control token")
        self.assertEqual(run_mock.call_args_list[0].kwargs["shell"], True)
        self.assertEqual(run_mock.call_args_list[0].args[0], "python -m unittest && python -m pytest")

    def test_evidence_status_uses_snapshot_match(self) -> None:
        current = {"git_head": "abc", "git_diff_hash": "hash1", "working_tree_dirty": True}
        run = {"git_head": "abc", "git_diff_hash": "hash1", "working_tree_dirty": True, "timed_out": False, "exit_code": 0}
        stale_run = {**run, "git_diff_hash": "hash2"}

        self.assertEqual(evidence_status(run, current), "current")
        self.assertEqual(evidence_status(stale_run, current), "stale")
        self.assertEqual(evidence_status({**run, "exit_code": 1}, current), "failed")
        self.assertEqual(evidence_status(None, current), "missing")

    def test_review_result_status_uses_based_on_snapshot(self) -> None:
        current = {"git_head": "abc", "git_diff_hash": "hash1", "working_tree_dirty": False}
        result = {"based_on_snapshot": dict(current)}
        stale = {"based_on_snapshot": {**current, "git_diff_hash": "hash2"}}

        self.assertEqual(review_result_status(result, current), "current")
        self.assertEqual(review_result_status(stale, current), "stale")


if __name__ == "__main__":
    unittest.main()
