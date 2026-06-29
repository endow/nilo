from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nilo.snapshot import UNCOMPUTED_DIFF_HASH, evidence_status, review_result_status
from nilo.verification import run_local_verification


class VerificationTests(unittest.TestCase):
    def test_run_local_verification_uses_argv_for_simple_command(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed) as run_mock, patch(
            "nilo.verification.current_git_snapshot",
            return_value={
                "git_head": "abc123",
                "git_diff_hash": UNCOMPUTED_DIFF_HASH,
                "working_tree_dirty": False,
                "git_status_porcelain": "",
                "observed_paths": [],
                "git_available": True,
                "snapshot_policy": {"max_file_bytes": 1000000},
                "snapshot_excluded_paths": [{"path": "dist/app.js", "reason": "ignored"}],
                "snapshot_hashed_paths": ["src/nilo/app.py"],
                "snapshot_large_paths": [],
                "snapshot_binary_paths": [],
                "snapshot_mode": "fast",
                "git_diff_hash_computed": False,
            },
        ):
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["source"], "nilo_executed")
        self.assertEqual(result["git_diff_hash"], UNCOMPUTED_DIFF_HASH)
        self.assertEqual(result["metadata"]["execution_mode"], "argv")
        self.assertEqual(result["metadata"]["snapshot_policy"], {"max_file_bytes": 1000000})
        self.assertEqual(result["metadata"]["snapshot_excluded_paths"], [{"path": "dist/app.js", "reason": "ignored"}])
        self.assertEqual(result["metadata"]["snapshot_hashed_paths"], ["src/nilo/app.py"])
        self.assertEqual(result["metadata"]["snapshot_mode"], "fast")
        self.assertFalse(result["metadata"]["git_diff_hash_computed"])
        self.assertEqual(run_mock.call_args_list[0].kwargs["shell"], False)
        self.assertEqual(run_mock.call_args_list[0].args[0], ["python", "-m", "unittest"])

    def test_run_local_verification_can_request_full_snapshot(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed), patch(
            "nilo.verification.current_git_snapshot",
            return_value={
                "git_head": "abc123",
                "git_diff_hash": "diffhash",
                "working_tree_dirty": False,
                "git_status_porcelain": "",
                "observed_paths": [],
                "git_available": True,
                "snapshot_mode": "full",
                "git_diff_hash_computed": True,
            },
        ) as snapshot_mock:
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10, snapshot_mode="full")

        snapshot_mock.assert_called_once_with(Path.cwd(), mode="full")
        self.assertEqual(result["git_diff_hash"], "diffhash")
        self.assertEqual(result["metadata"]["snapshot_mode"], "full")
        self.assertTrue(result["metadata"]["git_diff_hash_computed"])

    def test_run_local_verification_audit_snapshot_keeps_audit_metadata(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed), patch(
            "nilo.verification.current_git_snapshot",
            return_value={
                "git_head": "abc123",
                "git_diff_hash": "diffhash",
                "working_tree_dirty": False,
                "git_status_porcelain": "",
                "observed_paths": [],
                "git_available": True,
                "snapshot_mode": "full",
                "git_diff_hash_computed": True,
            },
        ) as snapshot_mock:
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10, snapshot_mode="audit")

        snapshot_mock.assert_called_once_with(Path.cwd(), mode="full")
        self.assertEqual(result["metadata"]["snapshot_mode"], "audit")
        self.assertEqual(result["metadata"]["requested_snapshot_mode"], "audit")
        self.assertTrue(result["metadata"]["git_diff_hash_computed"])

    def test_run_local_verification_none_snapshot_records_command_only_git_fields(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed), patch(
            "nilo.verification.current_git_snapshot",
            side_effect=AssertionError("snapshot should not run"),
        ):
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10, snapshot_mode="none")

        self.assertIsNone(result["git_head"])
        self.assertEqual(result["git_diff_hash"], "")
        self.assertFalse(result["working_tree_dirty"])
        self.assertEqual(result["metadata"]["snapshot_mode"], "none")
        self.assertFalse(result["metadata"]["git_diff_hash_computed"])

    def test_run_local_verification_default_fast_snapshot_uses_uncomputed_hash(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed), patch(
            "nilo.verification.current_git_snapshot",
            return_value={
                "git_head": "abc123",
                "git_diff_hash": UNCOMPUTED_DIFF_HASH,
                "working_tree_dirty": False,
                "git_status_porcelain": "",
                "observed_paths": [],
                "git_available": True,
                "snapshot_mode": "fast",
                "git_diff_hash_computed": False,
            },
        ) as snapshot_mock:
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10)

        snapshot_mock.assert_called_once_with(Path.cwd(), mode="fast")
        self.assertEqual(result["git_diff_hash"], UNCOMPUTED_DIFF_HASH)
        self.assertEqual(result["metadata"]["snapshot_mode"], "fast")
        self.assertFalse(result["metadata"]["git_diff_hash_computed"])

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
