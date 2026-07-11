from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nilo.snapshot import current_git_snapshot, snapshot_columns
from nilo.project_logic import fast_project_tasks_and_recorded_statuses
from nilo.store import Store
from nilo.task_logic import projected_task_status
from nilo.timeutil import now_iso
from nilo.transitions import (
    TransitionError,
    accept_roadmap_revision,
    complete_task,
    create_task_from_todo,
    import_agent_report,
    import_review_result,
    record_verification_run,
    record_outcome_decision,
    resolve_failure,
    update_review_finding,
)


def project_row() -> dict:
    return {
        "id": "project_test",
        "name": "Test",
        "tech_stack": [],
        "rules": [],
        "default_completion_criteria": [],
        "available_models": [],
        "fallback_models": [],
        "requires_local_execution": False,
        "created_at": now_iso(),
    }


def task_row(task_id: str = "task_test") -> dict:
    return {
        "id": task_id,
        "project_id": "project_test",
        "title": "Do work",
        "description": "",
        "acceptance_criteria": [],
        "parent_task_id": None,
        "split_index": None,
        "task_type": "implementation",
        "risk_level": "medium",
        "requires_understanding_check": False,
        "roadmap_commitment_id": "",
        "roadmap_item_id": "",
        "status": "planned",
        "assigned_model_profile": "",
        "degradation_mode": "normal",
        "mode": "normal",
        "base_commit": None,
        "created_at": now_iso(),
    }


def verification_row(task_id: str, cwd: Path, *, exit_code: int | None = 0, timed_out: bool = False, source: str = "nilo_executed") -> dict:
    snapshot = current_git_snapshot(cwd)
    return {
        "id": "verification_test",
        "task_id": task_id,
        "evidence_check_id": None,
        "source": source,
        "command": "python -m pytest",
        "cwd": str(cwd),
        "stdout": "",
        "stderr": "",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_seconds": 1.0,
        **snapshot_columns(snapshot),
        "metadata": {},
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "created_at": now_iso(),
    }


class TransitionTests(unittest.TestCase):
    def make_store(self, root: Path) -> Store:
        store = Store(root / "nilo.db")
        store.insert("projects", project_row())
        store.insert("tasks", task_row())
        return store

    def test_rejected_outcome_cancels_active_recipe_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                store.insert(
                    "recipe_runs",
                    {
                        "id": "recipe_test",
                        "project_id": "project_test",
                        "task_id": "task_test",
                        "recipe_name": "release",
                        "status": "active",
                        "current_step": "run_required_checks",
                        "completed_steps": ["commit"],
                        "pending_steps": ["run_required_checks", "tag"],
                        "pending_public_operations": [{"operation": "create_tag", "target": "v1.0.0"}],
                        "metadata": {},
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )

                record_outcome_decision(store, "task_test", decision="rejected", actor="human", reason="中止", cwd=root)

                run = store.get("recipe_runs", "recipe_test")
                self.assertEqual(run["status"], "cancelled")
                self.assertEqual(run["current_step"], "cancelled")
                self.assertEqual(run["pending_steps"], [])
                self.assertEqual(run["pending_public_operations"], [])
                self.assertEqual(run["metadata"]["cancellation_reason"], "中止")
            finally:
                store.close()

    def test_ai_completion_rejects_missing_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                with self.assertRaises(TransitionError) as ctx:
                    complete_task(store, "task_test", actor="ai", reason="done", cwd=root)
                self.assertEqual(ctx.exception.code, "verification_missing")
            finally:
                store.close()

    def test_human_completion_requires_confirm_and_note(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                with self.assertRaises(TransitionError) as ctx:
                    complete_task(store, "task_test", actor="human", reason="done", decision_source="human_interactive", cwd=root)
                self.assertEqual(ctx.exception.code, "human_confirm_required")
                with self.assertRaises(TransitionError) as note_ctx:
                    complete_task(
                        store,
                        "task_test",
                        actor="human",
                        reason="done",
                        human_confirm=True,
                        decision_source="human_interactive",
                        cwd=root,
                    )
                self.assertEqual(note_ctx.exception.code, "decision_note_required")
            finally:
                store.close()

    def test_ai_completion_with_current_evidence_creates_transition_event(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                cwd = Path.cwd()
                store.insert("verification_runs", verification_row("task_test", cwd))
                result = complete_task(store, "task_test", actor="ai", reason="verified", cwd=cwd)
                self.assertIn("task_completion", result.created_ids)
                self.assertEqual(projected_task_status(store, store.get("tasks", "task_test")), "completed_by_ai")
                events = store.list_where("transition_events", "transition='complete_task'")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["entity_id"], "task_test")
            finally:
                store.close()

    def test_first_verification_records_missing_task_base_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                row = verification_row("task_test", Path.cwd())
                self.assertIsNone(store.get("tasks", "task_test")["base_commit"])

                record_verification_run(store, "task_test", row=row)

                self.assertEqual(store.get("tasks", "task_test")["base_commit"], row["git_head"])
            finally:
                store.close()

    def test_failed_report_does_not_project_agent_reported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                result = import_agent_report(
                    store,
                    store.get("tasks", "task_test"),
                    "report body",
                    "ai",
                    Path.cwd(),
                    lambda *_: ("needs_human_review", ["git metadata warning: missing"], {}),
                )

                self.assertEqual(result.new_status, "needs_human_review")
                self.assertEqual(projected_task_status(store, store.get("tasks", "task_test")), "needs_human_review")
                self.assertIsNotNone(store.latest_for_task("agent_reports", "task_test"))
            finally:
                store.close()

    def test_report_after_completion_does_not_reopen_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                cwd = Path.cwd()
                store.insert("verification_runs", verification_row("task_test", cwd))
                complete_task(store, "task_test", actor="human", reason="done", human_confirm=True, decision_source="human_interactive", decision_note="accepted", cwd=cwd)

                import_agent_report(
                    store,
                    store.get("tasks", "task_test"),
                    "report body",
                    "ai",
                    cwd,
                    lambda *_: ("evidence_submitted", [], {}),
                )

                self.assertEqual(projected_task_status(store, store.get("tasks", "task_test")), "completed_by_user")
                _, statuses = fast_project_tasks_and_recorded_statuses(store, "project_test")
                self.assertEqual(statuses["task_test"], "completed_by_user")
            finally:
                store.close()

    def test_store_transaction_rolls_back_on_ordinary_exception(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "nilo.db")
            try:
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.insert("projects", project_row())
                        raise RuntimeError("boom")
                self.assertIsNone(store.get("projects", "project_test"))
                store.insert("projects", project_row())
                self.assertIsNotNone(store.get("projects", "project_test"))
            finally:
                store.close()

    def test_completion_rolls_back_when_post_write_audit_raises_transition_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                cwd = Path.cwd()
                store.insert("verification_runs", verification_row("task_test", cwd))
                with patch(
                    "nilo.state_audit.audit_task",
                    return_value=[{"severity": "error", "code": "completion_forced_test_error"}],
                ):
                    with self.assertRaises(TransitionError) as ctx:
                        complete_task(store, "task_test", actor="ai", reason="verified", cwd=cwd)
                self.assertEqual(ctx.exception.code, "completion_audit_failed")
                self.assertEqual(store.list_where("task_completions", "task_id=?", ("task_test",)), [])
                self.assertEqual(store.list_where("transition_events", "transition='complete_task'"), [])
            finally:
                store.close()

    def test_accepted_risk_by_ai_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                store.insert(
                    "review_findings",
                    {
                        "id": "finding_test",
                        "task_id": "task_test",
                        "review_request_id": "review_test",
                        "review_result_id": "result_test",
                        "title": "Risk",
                        "severity": "high",
                        "status": "unresolved",
                        "file_path": "",
                        "line": "",
                        "blocking": True,
                        "description": "",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
                with self.assertRaises(TransitionError) as ctx:
                    update_review_finding(store, "finding_test", status="accepted-risk", reason="risk accepted", actor="ai")
                self.assertEqual(ctx.exception.code, "human_only")
            finally:
                store.close()

    def test_task_completion_rejects_stale_expected_event_id(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                cwd = Path.cwd()
                store.insert("verification_runs", verification_row("task_test", cwd))
                with self.assertRaises(TransitionError) as ctx:
                    complete_task(store, "task_test", actor="ai", reason="verified", cwd=cwd, expected_task_event_id="old_event")
                self.assertEqual(ctx.exception.code, "stale_task_context")
                self.assertEqual(store.list_where("task_completions", "task_id=?", ("task_test",)), [])
            finally:
                store.close()

    def test_review_finding_update_rejects_stale_expected_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                store.insert(
                    "review_findings",
                    {
                        "id": "finding_test",
                        "task_id": "task_test",
                        "review_request_id": "review_test",
                        "review_result_id": "result_test",
                        "title": "Risk",
                        "severity": "high",
                        "status": "addressed",
                        "file_path": "",
                        "line": "",
                        "blocking": True,
                        "description": "",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
                with self.assertRaises(TransitionError) as ctx:
                    update_review_finding(store, "finding_test", status="unresolved", reason="stale", actor="ai", expected_status="unresolved")
                self.assertEqual(ctx.exception.code, "stale_review_finding")
                self.assertEqual(store.get("review_findings", "finding_test")["status"], "addressed")
            finally:
                store.close()

    def test_todo_conversion_rejects_stale_expected_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                now = now_iso()
                store.insert(
                    "todos",
                    {
                        "id": "todo_test",
                        "project_id": "project_test",
                        "title": "Do later",
                        "kind": "task",
                        "status": "blocked",
                        "description": "",
                        "acceptance_hint": "",
                        "priority": "medium",
                        "source_type": "",
                        "source_task_id": "",
                        "roadmap_commitment_id": "",
                        "roadmap_revision_id": "",
                        "converted_task_id": "",
                        "created_at": now,
                        "triaged_at": "",
                        "triage_reason": "",
                    },
                )
                with self.assertRaises(TransitionError) as ctx:
                    create_task_from_todo(store, "todo_test", task=task_row("task_from_todo"), actor="ai", expected_todo_status="ready")
                self.assertEqual(ctx.exception.code, "stale_todo")
                self.assertIsNone(store.get("tasks", "task_from_todo"))
            finally:
                store.close()

    def test_failure_resolution_rejects_stale_expected_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                store.insert("verification_runs", verification_row("task_test", Path.cwd()))
                store.insert(
                    "failure_logs",
                    {
                        "id": "failure_test",
                        "project_id": "project_test",
                        "task_id": "task_test",
                        "report_id": "",
                        "category": "test",
                        "message": "failed",
                        "severity": "high",
                        "source": "",
                        "actor": "",
                        "related_id": "",
                        "snapshot": {},
                        "status": "resolved",
                        "resolved_at": "",
                        "resolved_by": "",
                        "resolution_note": "",
                        "decision_note": "",
                        "created_at": now_iso(),
                    },
                )
                with self.assertRaises(TransitionError) as ctx:
                    resolve_failure(store, "failure_test", actor="ai", reason="resolved", expected_status="open")
                self.assertEqual(ctx.exception.code, "stale_failure")
            finally:
                store.close()

    def test_review_import_rejects_stale_request_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                now = now_iso()
                store.insert(
                    "review_requests",
                    {
                        "id": "review_test",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "claimed",
                        "reason": "review",
                        "based_on_event_id": "task_test",
                        "based_on_snapshot": {"git_head": "stale", "git_diff_hash": "stale", "working_tree_dirty": True},
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                latest = store.latest_task_status_event("task_test")
                body = "# ReviewResult\n\n## Verdict\napproved\n\n## Summary\nok\n\n## Findings\nnone\n"
                with self.assertRaises(TransitionError) as ctx:
                    import_review_result(
                        store,
                        "task_test",
                        "review_test",
                        body_md=body,
                        reviewer="claude-code",
                        last_seen_event_id=latest["event_id"],
                    )
                self.assertEqual(ctx.exception.code, "stale_review_snapshot")
            finally:
                store.close()

    def test_review_import_rolls_back_result_findings_and_request_update(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                now = now_iso()
                snapshot = current_git_snapshot(Path.cwd())
                store.insert(
                    "review_requests",
                    {
                        "id": "review_test",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "claimed",
                        "reason": "review",
                        "based_on_event_id": "task_test",
                        "based_on_snapshot": snapshot,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                latest = store.latest_task_status_event("task_test")
                body = (
                    "# ReviewResult\n\n"
                    "## Verdict\nchanges_requested\n\n"
                    "## Summary\nneeds work\n\n"
                    "## Findings\n"
                    "- severity: high\n"
                    "  title: Bug\n"
                    "  file: src/nilo/transitions.py\n"
                    "  line: 1\n"
                    "  blocking: true\n"
                    "  description: fix it\n"
                )
                with patch("nilo.transitions._event", side_effect=TransitionError("event_failed", "event failed")):
                    with self.assertRaises(TransitionError):
                        import_review_result(
                            store,
                            "task_test",
                            "review_test",
                            body_md=body,
                            reviewer="claude-code",
                            last_seen_event_id=latest["event_id"],
                            cwd=Path.cwd(),
                        )
                self.assertEqual(store.list_where("review_results", "review_request_id=?", ("review_test",)), [])
                self.assertEqual(store.list_where("review_findings", "review_request_id=?", ("review_test",)), [])
                self.assertEqual(store.get("review_requests", "review_test")["status"], "claimed")
            finally:
                store.close()

    def test_roadmap_accept_rolls_back_revision_and_commitment_updates(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                now = now_iso()
                store.insert(
                    "roadmap_commitments",
                    {
                        "id": "commitment_test",
                        "project_id": "project_test",
                        "title": "Plan",
                        "intent": "",
                        "success_criteria": [],
                        "non_goals": [],
                        "autonomy_scope": "",
                        "review_gates": [],
                        "evidence_policy": "",
                        "status": "proposed",
                        "accepted_by": "",
                        "accepted_at": "",
                        "created_at": now,
                    },
                )
                store.insert(
                    "roadmap_revisions",
                    {
                        "id": "revision_test",
                        "project_id": "project_test",
                        "proposed_commitment_id": "commitment_test",
                        "status": "pending",
                        "body_md": "",
                        "source_path": "",
                        "reason": "",
                        "decided_by": "",
                        "accepted_at": "",
                        "created_at": now,
                    },
                )
                with patch("nilo.transitions._event", side_effect=TransitionError("event_failed", "event failed")):
                    with self.assertRaises(TransitionError):
                        accept_roadmap_revision(
                            store,
                            "revision_test",
                            actor="human",
                            reason="approve",
                            decision_note="approve",
                            human_confirm=True,
                        )
                self.assertEqual(store.get("roadmap_revisions", "revision_test")["status"], "pending")
                self.assertEqual(store.get("roadmap_commitments", "commitment_test")["status"], "proposed")
            finally:
                store.close()

    def test_todo_conversion_rolls_back_task_insert_and_todo_update(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                now = now_iso()
                store.insert(
                    "todos",
                    {
                        "id": "todo_test",
                        "project_id": "project_test",
                        "title": "Do later",
                        "kind": "task",
                        "status": "ready",
                        "description": "",
                        "acceptance_hint": "",
                        "priority": "medium",
                        "source_type": "",
                        "source_task_id": "",
                        "roadmap_commitment_id": "",
                        "roadmap_revision_id": "",
                        "converted_task_id": "",
                        "created_at": now,
                        "triaged_at": "",
                        "triage_reason": "",
                    },
                )
                task = task_row("task_from_todo")
                with patch("nilo.transitions._event", side_effect=TransitionError("event_failed", "event failed")):
                    with self.assertRaises(TransitionError):
                        create_task_from_todo(store, "todo_test", task=task, actor="ai", reason="convert")
                self.assertIsNone(store.get("tasks", "task_from_todo"))
                self.assertEqual(store.get("todos", "todo_test")["status"], "ready")
            finally:
                store.close()

    def test_human_can_complete_non_implementation_without_verification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "nilo.db")
            try:
                store.insert("projects", project_row())
                row = task_row("task_research")
                row["task_type"] = "research"
                store.insert("tasks", row)
                result = complete_task(
                    store,
                    "task_research",
                    actor="human",
                    reason="research accepted",
                    human_confirm=True,
                    decision_source="human_interactive",
                    decision_note="Human reviewed and accepted the research result.",
                    cwd=root,
                )
                self.assertIn("task_completion", result.created_ids)
                self.assertEqual(projected_task_status(store, store.get("tasks", "task_research")), "completed_by_user")
            finally:
                store.close()

    def test_projected_status_uses_supplied_snapshot_to_detect_stale_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            try:
                cwd = Path.cwd()
                store.insert("verification_runs", verification_row("task_test", cwd))
                complete_task(store, "task_test", actor="ai", reason="verified", cwd=cwd)
                stale_snapshot = current_git_snapshot(cwd)
                stale_snapshot["git_diff_hash"] = "different"
                status = projected_task_status(store, store.get("tasks", "task_test"), current_snapshot=stale_snapshot)
                self.assertEqual(status, "completion_needs_review")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
