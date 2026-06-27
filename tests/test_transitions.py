from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nilo.snapshot import current_git_snapshot, snapshot_columns
from nilo.store import Store
from nilo.task_logic import projected_task_status
from nilo.timeutil import now_iso
from nilo.transitions import TransitionError, complete_task, import_review_result, update_review_finding


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
