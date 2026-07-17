from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import patch

from nilo.review_adapters.local_cli import LocalCliReviewAdapter
from nilo.review_adapters.mcp import McpReviewAdapter
from nilo.review_adapters.noop import NoopReviewAdapter
from nilo.review_ports import ReviewDispatchHandle, ReviewDispatchRequest, SnapshotRef


ROOT = Path(__file__).parents[1]


def dispatch_request() -> ReviewDispatchRequest:
    return ReviewDispatchRequest(
        request_id="review_test",
        task_id="task_test",
        reviewer="reviewer",
        snapshot=SnapshotRef(git_head="head", git_diff_hash="diff"),
        prompt_text="review this",
        timeout_seconds=1,
    )


class ReviewAdapterContractTest(unittest.TestCase):
    def test_mcp_contract_accepts_runs_and_cancels(self) -> None:
        adapter = McpReviewAdapter(available=True)
        accepted = adapter.dispatch(dispatch_request())
        handle = ReviewDispatchHandle("review_test", adapter.kind, accepted.external_id)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(adapter.poll(handle).status, "running")
        self.assertTrue(adapter.cancel(handle).cancelled)

    def test_unavailable_contract_is_normalized(self) -> None:
        outcome = NoopReviewAdapter().dispatch(dispatch_request())
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.error.code.value, "unavailable")

    def test_local_cli_completed_timeout_invalid_and_masked_diagnostics(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = LocalCliReviewAdapter(kind="local", command=lambda _request: ["reviewer"], cwd=Path(directory))
            completed = subprocess.CompletedProcess(["reviewer"], 0, stdout="# ReviewResult\n", stderr="")
            with patch("nilo.review_adapters.local_cli.subprocess.run", return_value=completed):
                self.assertEqual(adapter.dispatch(dispatch_request()).status, "completed")
            empty = subprocess.CompletedProcess(["reviewer"], 0, stdout="", stderr="")
            with patch("nilo.review_adapters.local_cli.subprocess.run", return_value=empty):
                invalid = adapter.dispatch(dispatch_request())
            self.assertEqual(invalid.error.code.value, "invalid_response")
            secret = "sk-" + "a" * 24
            failed = subprocess.CompletedProcess(["reviewer"], 1, stdout="", stderr=secret)
            with patch("nilo.review_adapters.local_cli.subprocess.run", return_value=failed):
                failure = adapter.dispatch(dispatch_request())
            self.assertNotIn(secret, str(failure.diagnostics))
            auth = subprocess.CompletedProcess(["reviewer"], 1, stdout="", stderr="Unauthorized: invalid API key")
            with patch("nilo.review_adapters.local_cli.subprocess.run", return_value=auth):
                auth_failure = adapter.dispatch(dispatch_request())
            self.assertEqual(auth_failure.error.code.value, "auth_required")
            with patch("nilo.review_adapters.local_cli.subprocess.run", side_effect=subprocess.TimeoutExpired("reviewer", 1)):
                timed_out = adapter.dispatch(dispatch_request())
            self.assertEqual(timed_out.status, "timed_out")
            self.assertEqual(timed_out.error.code.value, "timeout")


class ReviewBoundaryTest(unittest.TestCase):
    def test_core_review_modules_do_not_import_process_or_vendor_modules(self) -> None:
        for relative in ("review_ports.py", "review_service.py", "review_adapter_registry.py"):
            tree = ast.parse((ROOT / "src" / "nilo" / relative).read_text(encoding="utf-8"))
            imports = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            self.assertNotIn("subprocess", imports, relative)
            source = (ROOT / "src" / "nilo" / relative).read_text(encoding="utf-8")
            self.assertNotIn("claude_cli", source, relative)
            self.assertNotIn("codex", source, relative)

    def test_workflow_status_and_work_do_not_import_review_adapters(self) -> None:
        for relative in ("workflow_context.py", "project_status.py", "work_projection.py", "work_service.py"):
            source = (ROOT / "src" / "nilo" / relative).read_text(encoding="utf-8")
            self.assertNotIn("review_adapters", source, relative)
            self.assertNotIn("claude_cli_review", source, relative)

    def test_cli_handler_does_not_update_review_attempt_table(self) -> None:
        source = (ROOT / "src" / "nilo" / "cli_handlers" / "quality.py").read_text(encoding="utf-8")
        self.assertNotIn("review_attempts", source)
        self.assertNotIn("insert_review_attempt", source)

    def test_adapter_does_not_create_task_completion(self) -> None:
        for path in (ROOT / "src" / "nilo" / "review_adapters").glob("*.py"):
            self.assertNotIn("TaskCompletion", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
