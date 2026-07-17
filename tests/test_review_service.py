from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nilo.review_adapter_registry import ReviewAdapterRegistry, ReviewAdapterResolutionError
from nilo.review_ports import (
    ReviewCancelResult,
    ReviewDispatcherCapabilities,
    ReviewDispatchHandle,
    ReviewDispatchOutcome,
    ReviewDispatchRequest,
)
from nilo.review_service import ReviewService
from nilo.store import Store
from nilo.transitions import TransitionError


@dataclass
class FakeDispatcher:
    adapter_kind: str = "fake"
    outcome: ReviewDispatchOutcome = ReviewDispatchOutcome(status="running", external_id="external-1")
    available: bool = True
    seen_request: ReviewDispatchRequest | None = None

    @property
    def kind(self) -> str:
        return self.adapter_kind

    def capabilities(self) -> ReviewDispatcherCapabilities:
        return ReviewDispatcherCapabilities(frozenset({"review"}), available=self.available, supports_poll=True, supports_cancel=True)

    def dispatch(self, request: ReviewDispatchRequest) -> ReviewDispatchOutcome:
        self.seen_request = request
        return self.outcome

    def poll(self, handle: ReviewDispatchHandle) -> ReviewDispatchOutcome:
        return self.outcome

    def cancel(self, handle: ReviewDispatchHandle) -> ReviewCancelResult:
        outcome = ReviewDispatchOutcome(status="cancelled", external_id=handle.external_id)
        return ReviewCancelResult(True, outcome)


class ReviewServiceTest(unittest.TestCase):
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

    def service(self, adapter: FakeDispatcher | None = None) -> ReviewService:
        registry = ReviewAdapterRegistry()
        if adapter:
            registry.register(adapter, reviewers=["reviewer"], aliases={"alias": "reviewer"})
        return ReviewService(self.store, registry, cwd=self.root)

    def test_request_is_saved_before_dispatch_and_attempt_is_recorded(self) -> None:
        adapter = FakeDispatcher()
        service = self.service(adapter)
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="alias", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        outcome = service.dispatch_review(request["id"])
        attempts = self.store.list_where("review_attempts", "review_request_id=?", (request["id"],))
        self.assertEqual(outcome.status, "running")
        self.assertIsNotNone(adapter.seen_request)
        self.assertEqual(attempts[0]["attempt_number"], 1)
        self.assertEqual(attempts[0]["idempotency_key"], f"{request['id']}:1")
        self.assertEqual(self.store.get("review_requests", request["id"])["status"], "running")

    def test_retry_increments_attempt_and_preserves_request_identity(self) -> None:
        service = self.service(FakeDispatcher(outcome=ReviewDispatchOutcome(status="failed")))
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="reviewer", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        service.dispatch_review(request["id"])
        service.dispatch_review(request["id"])
        attempts = sorted(
            self.store.list_where("review_attempts", "review_request_id=?", (request["id"],)),
            key=lambda row: row["attempt_number"],
        )
        self.assertEqual([row["attempt_number"] for row in attempts], [1, 2])
        self.assertEqual([row["idempotency_key"] for row in attempts], [f"{request['id']}:1", f"{request['id']}:2"])

    def test_unavailable_adapter_does_not_complete_request(self) -> None:
        service = self.service()
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="reviewer", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        outcome = service.dispatch_review(request["id"])
        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(self.store.get("review_requests", request["id"])["status"], "reviewer_unavailable")

    def test_manual_import_remains_available_after_adapter_unavailable(self) -> None:
        service = self.service()
        request = service.request_review(task_id="task_test", requester="ai", reviewer="reviewer", reason="test")
        service.dispatch_review(request["id"])
        result = service.import_result(
            request["id"],
            body_md="# ReviewResult\n\n## Verdict\n\napproved\n\n## Summary\n\nLooks good.\n\n## Findings\n\nNone.\n",
        )
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(self.store.get("review_requests", request["id"])["status"], "completed")

    def test_completed_without_payload_is_normalized_to_invalid_response(self) -> None:
        service = self.service(FakeDispatcher(outcome=ReviewDispatchOutcome(status="completed")))
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="reviewer", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        outcome = service.dispatch_review(request["id"])
        attempt = self.store.list_where("review_attempts", "review_request_id=?", (request["id"],))[0]
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error.code.value, "invalid_response")
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(self.store.get("review_requests", request["id"])["status"], "failed")

    def test_prompt_path_must_stay_inside_repository(self) -> None:
        service = self.service(FakeDispatcher())
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="reviewer", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        with self.assertRaises(ValueError):
            service.dispatch_review(request["id"], prompt_path=self.root.parent / "outside.md")

    def test_stale_adapter_result_is_recorded_as_stale(self) -> None:
        outcome = ReviewDispatchOutcome(status="completed", result_payload={"body_md": "# ReviewResult"})
        service = self.service(FakeDispatcher(outcome=outcome))
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="reviewer", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        with patch(
            "nilo.review_service.transition_import_review_result",
            side_effect=TransitionError("stale_review_snapshot", "snapshot changed"),
        ), self.assertRaises(TransitionError):
            service.dispatch_review(request["id"])
        attempt = self.store.list_where("review_attempts", "review_request_id=?", (request["id"],))[0]
        self.assertEqual(attempt["status"], "stale")
        self.assertEqual(self.store.get("review_requests", request["id"])["status"], "stale")

    def test_cancel_updates_attempt_and_request(self) -> None:
        service = self.service(FakeDispatcher())
        request = service.request_review(
            task_id="task_test", requester="ai", reviewer="reviewer", reason="test", snapshot={"git_head": "head", "git_diff_hash": "diff"}
        )
        service.dispatch_review(request["id"])
        result = service.cancel_review(request["id"])
        self.assertTrue(result.cancelled)
        self.assertEqual(self.store.get("review_requests", request["id"])["status"], "cancelled")

    def test_registry_requires_capability_and_availability(self) -> None:
        registry = ReviewAdapterRegistry()
        registry.register(FakeDispatcher(available=False), reviewers=["reviewer"])
        with self.assertRaises(ReviewAdapterResolutionError):
            registry.resolve("reviewer", "review")


if __name__ == "__main__":
    unittest.main()
