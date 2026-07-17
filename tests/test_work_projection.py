from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from nilo.work_projection import (
    Blocker,
    CompletionState,
    EvidenceState,
    NextAction,
    NextActionCode,
    ReviewState,
    WorkPhase,
    WorkProjection,
    project_work_projection,
    task_work_projection,
)
from nilo.cli import main
from nilo.store import Store


SNAPSHOT = {
    "git_available": True,
    "git_head": "abc123",
    "git_diff_hash": "diff123",
    "working_tree_dirty": True,
}


class WorkProjectionTests(TestCase):
    def task_store(self, *, result: dict | None = None, report: dict | None = None) -> MagicMock:
        store = MagicMock()
        task = {"id": "task_test", "project_id": "project_test", "status": "agent_reported"}
        store.get.side_effect = lambda table, row_id: task if table == "tasks" and row_id == "task_test" else None
        rows = {
            "verification_runs": None,
            "agent_reports": report,
            "review_requests": None,
            "review_results": result,
        }
        store.latest_for_task.side_effect = lambda table, _task_id: rows[table]
        return store

    def project_task(self, store: MagicMock, *, evidence: str = "current"):
        with (
            patch("nilo.project_logic.projected_task_status", return_value="review_changes_requested"),
            patch("nilo.work_projection.active_task_completion", return_value=None),
            patch("nilo.work_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.work_projection.commit_aware_evidence_status", return_value=evidence),
        ):
            return task_work_projection(store, "task_test", current_snapshot=SNAPSHOT)

    def test_nonapproved_review_never_becomes_approved_or_completable(self) -> None:
        projection = self.project_task(
            self.task_store(result={"id": "result", "verdict": "changes_requested", "based_on_snapshot": SNAPSHOT})
        )

        self.assertEqual(projection.phase, WorkPhase.WORKING)
        self.assertEqual(projection.next_action.code, NextActionCode.CONTINUE_WORK)
        self.assertEqual(projection.review_state, ReviewState.CHANGES_REQUESTED)
        self.assertEqual(projection.completion_state, CompletionState.NOT_READY)

    def test_missing_evidence_precedes_review_verdict_without_agent_report(self) -> None:
        projection = self.project_task(
            self.task_store(result={"id": "result", "verdict": "approved", "based_on_snapshot": SNAPSHOT}), evidence="missing"
        )

        self.assertEqual(projection.phase, WorkPhase.VERIFYING)
        self.assertEqual(projection.next_action.code, NextActionCode.RUN_VERIFICATION)
        self.assertEqual(projection.evidence_state, EvidenceState.MISSING)

    def test_fresh_approved_review_with_current_evidence_awaits_human(self) -> None:
        projection = self.project_task(
            self.task_store(result={"id": "result", "verdict": "approved", "based_on_snapshot": SNAPSHOT})
        )

        self.assertEqual(projection.phase, WorkPhase.AWAITING_HUMAN)
        self.assertEqual(projection.next_action.code, NextActionCode.ACCEPT_COMPLETION)
        self.assertEqual(projection.review_state, ReviewState.APPROVED)

    def test_roadmap_commitment_precedes_todo_intake(self) -> None:
        store = MagicMock()
        store.get.return_value = {"id": "project_test"}
        store.list_where.return_value = [{"id": "todo_old", "status": "ready"}]
        with (
            patch("nilo.project_logic.roadmap_prioritized_project_active_tasks", return_value=([], [{"id": "roadmap"}])),
            patch("nilo.project_logic.pending_roadmap_revisions", return_value=[]),
            patch("nilo.project_logic.selected_roadmap_commitment", return_value={"id": "roadmap"}),
            patch("nilo.project_logic.roadmap_commitment_assessment", return_value={"status": "task_plan_required"}),
        ):
            projection = project_work_projection(
                store, "project_test", current_snapshot=SNAPSHOT, tasks=[], statuses={}
            )

        self.assertEqual(projection.scope, "roadmap")
        self.assertEqual(projection.next_action.code, NextActionCode.CREATE_TASK)

    def test_satisfied_roadmap_does_not_create_another_task(self) -> None:
        store = MagicMock()
        store.get.return_value = {"id": "project_test"}
        with (
            patch("nilo.project_logic.roadmap_prioritized_project_active_tasks", return_value=([], [{"id": "roadmap"}])),
            patch("nilo.project_logic.pending_roadmap_revisions", return_value=[]),
            patch("nilo.project_logic.selected_roadmap_commitment", return_value={"id": "roadmap"}),
            patch("nilo.project_logic.roadmap_commitment_assessment", return_value={"status": "evidence_present"}),
        ):
            projection = project_work_projection(
                store, "project_test", current_snapshot=SNAPSHOT, tasks=[], statuses={}
            )

        self.assertEqual(projection.phase, WorkPhase.COMPLETED)
        self.assertEqual(projection.next_action.code, NextActionCode.NONE)

    def test_todo_intake_uses_fifo_order(self) -> None:
        store = MagicMock()
        store.get.return_value = {"id": "project_test"}
        store.list_where.return_value = [
            {"id": "todo_new", "status": "ready"},
            {"id": "todo_old", "status": "ready"},
        ]
        with (
            patch("nilo.project_logic.roadmap_prioritized_project_active_tasks", return_value=([], [])),
            patch("nilo.project_logic.pending_roadmap_revisions", return_value=[]),
            patch("nilo.project_logic.accepted_roadmap_commitments", return_value=[]),
        ):
            projection = project_work_projection(
                store, "project_test", current_snapshot=SNAPSHOT, tasks=[], statuses={}
            )

        self.assertEqual(projection.next_action.code, NextActionCode.TRIAGE_TODO)
        self.assertEqual(projection.next_action.todo_id, "todo_old")

    def test_todo_intake_prioritizes_ready_over_older_deferred(self) -> None:
        store = MagicMock()
        store.get.return_value = {"id": "project_test"}
        store.list_where.return_value = [
            {"id": "todo_ready", "status": "ready"},
            {"id": "todo_deferred", "status": "deferred"},
        ]
        with (
            patch("nilo.project_logic.roadmap_prioritized_project_active_tasks", return_value=([], [])),
            patch("nilo.project_logic.pending_roadmap_revisions", return_value=[]),
            patch("nilo.project_logic.accepted_roadmap_commitments", return_value=[]),
        ):
            projection = project_work_projection(
                store, "project_test", current_snapshot=SNAPSHOT, tasks=[], statuses={}
            )

        self.assertEqual(projection.next_action.todo_id, "todo_ready")

    def test_fast_snapshot_does_not_hide_nonapproved_verdict(self) -> None:
        fast_snapshot = {**SNAPSHOT, "git_diff_hash": "__not_computed__", "git_diff_hash_computed": False}
        store = self.task_store(result={"id": "result", "verdict": "changes_requested", "based_on_snapshot": SNAPSHOT})
        with (
            patch("nilo.project_logic.projected_task_status", return_value="review_changes_requested"),
            patch("nilo.work_projection.active_task_completion", return_value=None),
            patch("nilo.work_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.work_projection.commit_aware_evidence_status", return_value="current"),
        ):
            projection = task_work_projection(store, "task_test", current_snapshot=fast_snapshot)

        self.assertEqual(projection.review_state, ReviewState.CHANGES_REQUESTED)
        self.assertEqual(projection.next_action.code, NextActionCode.CONTINUE_WORK)

    def test_fast_snapshot_never_accepts_approved_review(self) -> None:
        fast_snapshot = {**SNAPSHOT, "git_diff_hash": "__not_computed__", "git_diff_hash_computed": False}
        store = self.task_store(result={"id": "result", "verdict": "approved", "based_on_snapshot": SNAPSHOT})
        with (
            patch("nilo.project_logic.projected_task_status", return_value="review_approved"),
            patch("nilo.work_projection.active_task_completion", return_value=None),
            patch("nilo.work_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.work_projection.commit_aware_evidence_status", return_value="current"),
        ):
            projection = task_work_projection(store, "task_test", current_snapshot=fast_snapshot)

        self.assertEqual(projection.phase, WorkPhase.BLOCKED)
        self.assertEqual(projection.next_action.code, NextActionCode.REASSESS_STATE)

    def test_withdrawn_review_request_is_not_pending(self) -> None:
        store = self.task_store(report={"id": "report"})
        store.latest_for_task.side_effect = lambda table, _task_id: {
            "verification_runs": None,
            "agent_reports": {"id": "report"},
            "review_requests": {"id": "request", "status": "withdrawn"},
            "review_results": None,
        }[table]
        with (
            patch("nilo.project_logic.projected_task_status", return_value="agent_reported"),
            patch("nilo.work_projection.active_task_completion", return_value=None),
            patch("nilo.work_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.work_projection.commit_aware_evidence_status", return_value="missing"),
        ):
            projection = task_work_projection(store, "task_test", current_snapshot=SNAPSHOT)

        self.assertEqual(projection.next_action.code, NextActionCode.RUN_VERIFICATION)
        self.assertNotEqual(projection.review_state, ReviewState.IN_PROGRESS)

    def test_fast_recorded_evidence_is_unknown_not_current(self) -> None:
        store = self.task_store(report={"id": "report"})
        with (
            patch("nilo.project_logic.projected_task_status", return_value="agent_reported"),
            patch("nilo.work_projection.active_task_completion", return_value=None),
            patch("nilo.work_projection.unresolved_review_findings", return_value=[]),
            patch("nilo.work_projection.commit_aware_evidence_status", return_value="recorded"),
        ):
            projection = task_work_projection(store, "task_test", current_snapshot=SNAPSHOT)

        self.assertEqual(projection.evidence_state, EvidenceState.UNKNOWN)
        self.assertEqual(projection.next_action.code, NextActionCode.RERUN_VERIFICATION)

    def test_work_does_not_create_new_task_while_an_active_task_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main([
                    "--db", str(db), "task", "create", "--project", "project_test",
                    "--id", "task_active", "--title", "Active task",
                ])
            output = io.StringIO()
            blocked_projection = WorkProjection(
                "project_test", "task", "task_active", WorkPhase.REVIEWING,
                NextAction(NextActionCode.RESOLVE_REVIEW_FINDINGS, task_id="task_active"),
                None, EvidenceState.CURRENT, ReviewState.FINDINGS_OPEN, CompletionState.NOT_READY,
            )
            with (
                patch("nilo.work_projection.project_work_projection", return_value=blocked_projection),
                redirect_stdout(output),
            ):
                main([
                    "--db", str(db), "work", "New task", "--intent", "change",
                    "--project", "project_test",
                ])
            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
            finally:
                store.close()

        self.assertIn("stopped: work_projection:resolve_review_findings", output.getvalue())
        self.assertEqual([task["id"] for task in tasks], ["task_active"])

    def test_explicit_work_stops_when_roadmap_evidence_needs_reassessment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
            output = io.StringIO()
            roadmap_projection = WorkProjection(
                "project_test", "roadmap", None, WorkPhase.BLOCKED,
                NextAction(NextActionCode.REASSESS_STATE, roadmap_id="roadmap_old"),
                Blocker("roadmap_evidence_incomplete", "old roadmap evidence"),
                EvidenceState.UNKNOWN, ReviewState.NOT_REQUIRED, CompletionState.NOT_READY,
            )
            with (
                patch("nilo.work_projection.project_work_projection", return_value=roadmap_projection),
                redirect_stdout(output),
            ):
                main([
                    "--db", str(db), "work", "Explicit follow-up", "--intent", "change",
                    "--project", "project_test",
                ])
            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
            finally:
                store.close()

        self.assertIn("stopped: work_projection:reassess_state", output.getvalue())
        self.assertEqual(len(tasks), 0)

    def test_explicit_work_stops_for_non_roadmap_project_state_blocker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
            output = io.StringIO()
            inconsistent_projection = WorkProjection(
                "project_test", "project", None, WorkPhase.BLOCKED,
                NextAction(NextActionCode.REASSESS_STATE),
                Blocker("project_state_inconsistent", "inconsistent project state"),
                EvidenceState.UNKNOWN, ReviewState.UNKNOWN, CompletionState.UNKNOWN,
            )
            with (
                patch("nilo.work_projection.project_work_projection", return_value=inconsistent_projection),
                redirect_stdout(output),
            ):
                main([
                    "--db", str(db), "work", "Explicit follow-up", "--intent", "change",
                    "--project", "project_test",
                ])
            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
            finally:
                store.close()

        self.assertIn("stopped: work_projection:reassess_state", output.getvalue())
        self.assertEqual(tasks, [])
