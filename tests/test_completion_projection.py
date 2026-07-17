from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

from nilo.completion_projection import CompletionStage, project_task_completion
from nilo.work_projection import EvidenceState


SNAPSHOT = {
    "git_available": True,
    "git_head": "abc123",
    "git_diff_hash": "diff123",
    "working_tree_dirty": True,
}


class CompletionProjectionTests(TestCase):
    def store(self, **rows: dict | None) -> MagicMock:
        store = MagicMock()
        store.latest_for_task.side_effect = lambda table, _task_id: rows.get(table)
        store.list_where.return_value = []
        return store

    def task(self, **changes: object) -> dict:
        return {
            "id": "task_test",
            "project_id": "project_test",
            "status": "completion_needs_review",
            **changes,
        }

    def project(self, store: MagicMock, task: dict | None = None, *, evidence: str = "missing"):
        with (
            patch("nilo.completion_projection.active_task_completion", return_value=None),
            patch("nilo.completion_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.completion_projection.commit_aware_evidence_status", return_value=evidence),
        ):
            return project_task_completion(store, task or self.task(), current_snapshot=SNAPSHOT)

    def test_historical_completion_review_without_current_fact_is_legacy_pending(self) -> None:
        with (
            patch("nilo.completion_projection.active_task_completion", return_value=None),
            patch("nilo.completion_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.completion_projection.commit_aware_evidence_status", return_value="missing"),
        ):
            projection = project_task_completion(
                self.store(),
                self.task(),
                current_snapshot=SNAPSHOT,
                explicit_current_task_ids=set(),
            )

        self.assertEqual(projection.stage, CompletionStage.LEGACY_PENDING)
        self.assertFalse(projection.is_current_work)
        self.assertFalse(projection.requires_human_action)

    def test_current_commitment_task_is_current(self) -> None:
        task = self.task(roadmap_commitment_id="commitment_current")
        with (
            patch("nilo.completion_projection.active_task_completion", return_value=None),
            patch("nilo.completion_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.completion_projection.commit_aware_evidence_status", return_value="missing"),
        ):
            projection = project_task_completion(
                self.store(),
                task,
                current_snapshot=SNAPSHOT,
                current_commitment_ids={"commitment_current"},
            )

        self.assertTrue(projection.is_current_work)
        self.assertEqual(projection.stage, CompletionStage.IN_PROGRESS)

    def test_report_without_verification_is_reported(self) -> None:
        projection = self.project(
            self.store(
                agent_reports={"id": "report"},
                instructions={"id": "instruction"},
            )
        )

        self.assertEqual(projection.stage, CompletionStage.REPORTED)

    def test_current_verification_reaches_verified(self) -> None:
        projection = self.project(
            self.store(verification_runs={"id": "verification", "exit_code": 0}),
            evidence="current",
        )

        self.assertEqual(projection.stage, CompletionStage.VERIFIED)
        self.assertEqual(projection.evidence_state, EvidenceState.CURRENT)
        self.assertFalse(projection.requires_human_action)

    def test_high_risk_current_verification_requires_human_acceptance(self) -> None:
        projection = self.project(
            self.store(verification_runs={"id": "verification", "exit_code": 0}),
            task=self.task(risk_level="high"),
            evidence="current",
        )

        self.assertEqual(projection.stage, CompletionStage.NEEDS_HUMAN_ACCEPTANCE)
        self.assertTrue(projection.requires_human_action)

    def test_approved_review_reaches_reviewed(self) -> None:
        result = {"id": "result", "verdict": "approved", "based_on_snapshot": SNAPSHOT}
        with patch("nilo.completion_projection.review_result_status", return_value="current"):
            projection = self.project(
                self.store(
                    verification_runs={"id": "verification", "exit_code": 0},
                    review_results=result,
                ),
                evidence="current",
            )

        self.assertEqual(projection.stage, CompletionStage.REVIEWED)

    def test_explicit_supersede_is_terminal(self) -> None:
        store = self.store()
        store.list_where.return_value = [
            {"id": "transition", "transition": "supersede_task", "new_state": "superseded"}
        ]
        projection = self.project(store)

        self.assertEqual(projection.stage, CompletionStage.SUPERSEDED)
        self.assertTrue(projection.is_terminal)

    def test_completion_with_open_finding_is_inconsistent(self) -> None:
        completion = {"id": "completion", "actor": "human", "human_decision_note": "承認"}
        with (
            patch("nilo.completion_projection.active_task_completion", return_value=completion),
            patch("nilo.completion_projection.unresolved_review_findings", return_value=[{"id": "finding"}]),
            patch("nilo.completion_projection.completion_structural_issues", return_value=[]),
            patch("nilo.completion_projection.commit_aware_evidence_status", return_value="current"),
        ):
            projection = project_task_completion(self.store(), self.task(), current_snapshot=SNAPSHOT)

        self.assertEqual(projection.stage, CompletionStage.INCONSISTENT)

    def test_completion_exposes_completed_snapshot(self) -> None:
        completion = {
            "id": "completion",
            "actor": "human",
            "human_decision_note": "承認",
            "completed_snapshot": SNAPSHOT,
        }
        with (
            patch("nilo.completion_projection.active_task_completion", return_value=completion),
            patch("nilo.completion_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.completion_projection.completion_structural_issues", return_value=[]),
            patch("nilo.completion_projection.commit_aware_evidence_status", return_value="current"),
        ):
            projection = project_task_completion(self.store(), self.task(), current_snapshot=SNAPSHOT)

        self.assertEqual(projection.stage, CompletionStage.ACCEPTED)
        self.assertEqual(projection.accepted_snapshot, SNAPSHOT)

    def test_requested_review_with_current_evidence_is_review_required(self) -> None:
        projection = self.project(
            self.store(
                verification_runs={"id": "verification", "exit_code": 0},
                review_requests={"id": "request", "status": "completed"},
            ),
            evidence="current",
        )

        self.assertEqual(projection.stage, CompletionStage.REVIEW_REQUIRED)
