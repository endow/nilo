from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nilo.verification import run_local_verification


class VerificationTests(unittest.TestCase):
    def test_run_local_verification_uses_argv_for_simple_command(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed) as run_mock, patch(
            "nilo.verification.working_tree_state",
            return_value={"working_tree_dirty": False, "working_tree_files": [], "working_tree_available": True},
        ), patch("nilo.verification.head_commit", return_value="abc123"):
            result = run_local_verification('"python" -m unittest', Path.cwd(), 10)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["source"], "nilo_executed")
        self.assertEqual(result["metadata"]["execution_mode"], "argv")
        self.assertEqual(run_mock.call_args_list[0].kwargs["shell"], False)
        self.assertEqual(run_mock.call_args_list[0].args[0], ["python", "-m", "unittest"])

    def test_run_local_verification_falls_back_to_shell_for_control_tokens(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        with patch("nilo.verification.subprocess.run", return_value=completed) as run_mock, patch(
            "nilo.verification.working_tree_state",
            return_value={"working_tree_dirty": False, "working_tree_files": [], "working_tree_available": True},
        ), patch("nilo.verification.head_commit", return_value="abc123"):
            result = run_local_verification("python -m unittest && python -m pytest", Path.cwd(), 10)

        self.assertEqual(result["metadata"]["execution_mode"], "shell")
        self.assertEqual(result["metadata"]["execution_reason"], "shell control token")
        self.assertEqual(run_mock.call_args_list[0].kwargs["shell"], True)
        self.assertEqual(run_mock.call_args_list[0].args[0], "python -m unittest && python -m pytest")


if __name__ == "__main__":
    unittest.main()
