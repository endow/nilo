from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nilo.review_coordinator import ErrorClass, ReviewBackendError, ReviewContext, ReviewExecutionOutput, ReviewExecutionPolicy, coordinate_review, coordinate_review_with_fallback
from nilo.store import Store


@dataclass
class FakeAdapter:
    reviewer: str = "fake"
    backend_kind: str = "other"
    transport: str = "direct_cli"
    ready: bool = True
    error: Exception | None = None
    finalize_error: Exception | None = None
    finalized: bool = False

    def readiness(self, context: ReviewContext) -> bool:
        return self.ready

    def execute(self, context: ReviewContext) -> ReviewExecutionOutput:
        if self.error:
            raise self.error
        return ReviewExecutionOutput("approved", {"token": "sk-" + "a" * 24})

    def finalize(self, store: Store, context: ReviewContext, output: ReviewExecutionOutput) -> None:
        if self.finalize_error:
            raise self.finalize_error
        self.finalized = True

    def cancel(self, attempt_id: str) -> None:
        return None


class ReviewCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "nilo.db")
        self.store.insert(
            "projects",
            {
                "id": "project_test", "name": "Test", "tech_stack": [], "rules": [], "default_completion_criteria": [],
                "available_models": [], "fallback_models": [], "requires_local_execution": False, "created_at": "2026-01-01T00:00:00+00:00",
            },
        )
        self.store.insert(
            "tasks",
            {
                "id": "task_test", "project_id": "project_test", "title": "Review", "status": "planned",
                "assigned_model_profile": "", "degradation_mode": "normal", "base_commit": None, "created_at": "2026-01-01T00:00:00+00:00",
            },
        )

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_success_completes_request_and_attempt_and_masks_diagnostics(self) -> None:
        adapter = FakeAdapter()
        result = coordinate_review(self.store, task_id="task_test", requester="codex", reason="test", adapter=adapter, cwd=self.root)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.review_request["status"], "completed")
        self.assertEqual(result.review_attempt["status"], "succeeded")
        self.assertNotIn("sk-", str(result.review_attempt["diagnostics"]))
        self.assertTrue(adapter.finalized)

    def test_rate_limit_defers_without_active_state(self) -> None:
        error = ReviewBackendError(
            ErrorClass.RATE_LIMITED,
            "rate limited",
            error_code="provider-sk-" + "a" * 24,
            retry_after="2026-01-01T01:00:00+00:00",
        )
        result = coordinate_review(self.store, task_id="task_test", requester="codex", reason="test", adapter=FakeAdapter(error=error), cwd=self.root)
        self.assertEqual(result.status, "deferred")
        self.assertEqual(result.review_attempt["status"], "rate_limited")
        self.assertEqual(result.review_attempt["retry_after"], "2026-01-01T01:00:00+00:00")
        self.assertNotIn("sk-", result.review_attempt["error_code"])

    def test_not_ready_and_unknown_error_fail_without_active_state(self) -> None:
        not_ready = coordinate_review(self.store, task_id="task_test", requester="codex", reason="test", adapter=FakeAdapter(ready=False), cwd=self.root)
        crashed = coordinate_review(self.store, task_id="task_test", requester="codex", reason="test", adapter=FakeAdapter(error=RuntimeError("boom")), cwd=self.root)
        self.assertEqual(not_ready.review_request["status"], "failed")
        self.assertEqual(not_ready.review_attempt["error_class"], "configuration")
        self.assertEqual(crashed.review_request["status"], "failed")
        self.assertEqual(crashed.review_attempt["error_class"], "unknown")
        active = self.store.list_where("review_requests", "status IN ('requested', 'running', 'claimed', 'in_progress', 'reviewer_unavailable')")
        self.assertEqual(active, [])

    def test_finalize_failure_rolls_back_and_fails_request(self) -> None:
        adapter = FakeAdapter(finalize_error=RuntimeError("invalid review result"))
        result = coordinate_review(self.store, task_id="task_test", requester="codex", reason="test", adapter=adapter, cwd=self.root)
        self.assertEqual(result.review_request["status"], "failed")
        self.assertEqual(result.review_attempt["status"], "failed")
        self.assertEqual(result.review_attempt["error_class"], "unknown")

    def test_explicit_fallback_reuses_request_and_keeps_attempt_history(self) -> None:
        first = FakeAdapter(reviewer="claude-code", error=ReviewBackendError(ErrorClass.RATE_LIMITED, "limited"))
        second = FakeAdapter(reviewer="codex")
        result = coordinate_review_with_fallback(
            self.store,
            task_id="task_test",
            requester="codex",
            reason="test",
            adapters=[first, second],
            policy=ReviewExecutionPolicy(fallback_reviewers=("codex",), max_attempts=2),
            cwd=self.root,
        )
        attempts = self.store.list_where("review_attempts", "review_request_id=?", (result.review_request["id"],))
        attempts.sort(key=lambda row: row["attempt_number"])
        self.assertEqual(result.status, "completed")
        self.assertEqual([row["attempt_number"] for row in attempts], [1, 2])
        self.assertEqual([row["reviewer"] for row in attempts], ["claude-code", "codex"])

    def test_fallback_requires_explicit_unique_reviewers_within_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit fallback"):
            coordinate_review_with_fallback(
                self.store,
                task_id="task_test",
                requester="codex",
                reason="test",
                adapters=[FakeAdapter(reviewer="claude-code"), FakeAdapter(reviewer="grok")],
                policy=ReviewExecutionPolicy(fallback_reviewers=(), max_attempts=2),
                cwd=self.root,
            )
        with self.assertRaisesRegex(ValueError, "cycle or duplicate"):
            coordinate_review_with_fallback(
                self.store,
                task_id="task_test",
                requester="codex",
                reason="test",
                adapters=[FakeAdapter(reviewer="claude-code"), FakeAdapter(reviewer="claude-code")],
                policy=ReviewExecutionPolicy(fallback_reviewers=("claude-code",), max_attempts=2),
                cwd=self.root,
            )
        with self.assertRaisesRegex(ValueError, "limit exceeded"):
            coordinate_review_with_fallback(
                self.store,
                task_id="task_test",
                requester="codex",
                reason="test",
                adapters=[FakeAdapter(reviewer="claude-code"), FakeAdapter(reviewer="codex")],
                policy=ReviewExecutionPolicy(fallback_reviewers=("codex",), max_attempts=1),
                cwd=self.root,
            )

    def test_completed_request_cannot_be_executed_twice(self) -> None:
        first = coordinate_review(self.store, task_id="task_test", requester="codex", reason="test", adapter=FakeAdapter(), cwd=self.root)
        with self.assertRaisesRegex(ValueError, "cannot be retried"):
            coordinate_review(
                self.store,
                task_id="task_test",
                requester="codex",
                reason="test",
                adapter=FakeAdapter(),
                cwd=self.root,
                existing_request_id=first.review_request["id"],
            )
        attempts = self.store.list_where("review_attempts", "review_request_id=?", (first.review_request["id"],))
        self.assertEqual(len(attempts), 1)


if __name__ == "__main__":
    unittest.main()
