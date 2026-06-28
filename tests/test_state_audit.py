from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nilo.state_audit import doctor_state
from nilo.store import Store
from nilo.timeutil import now_iso


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


def task_row() -> dict:
    return {
        "id": "task_test",
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


class StateAuditTests(unittest.TestCase):
    def test_doctor_state_reports_invalid_schema_invariants(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                now = now_iso()
                store.insert("projects", project_row())
                store.insert("tasks", {**task_row(), "status": "bad_status"})
                store.insert(
                    "task_completions",
                    {
                        "id": "completion_test",
                        "task_id": "task_test",
                        "actor": "robot",
                        "completed_by": "robot",
                        "completed_snapshot": {},
                        "completion_note": "done",
                        "accepted_verification_run_ids": [],
                        "accepted_review_result_ids": [],
                        "human_decision_note": "",
                        "completed_with_reservations": False,
                        "decision_source": "",
                        "human_confirmed": 3,
                        "completed_at": now,
                        "reason": "done",
                        "created_at": now,
                    },
                )
                store.insert(
                    "transition_events",
                    {
                        "id": "transition_test",
                        "transition": "",
                        "entity_type": "task",
                        "entity_id": "task_test",
                        "actor": "",
                        "decision_source": "",
                        "human_confirmed": 2,
                        "reason": "",
                        "previous_state": "",
                        "new_state": "",
                        "related_ids": {},
                        "snapshot": {},
                        "warnings": [],
                        "created_at": now,
                    },
                )
                data = doctor_state(store, "project_test", cwd=Path.cwd())
                codes = {finding["code"] for finding in data["findings"]}
                self.assertIn("task_invalid_status", codes)
                self.assertIn("completion_invalid_actor", codes)
                self.assertIn("completion_invalid_human_confirmed", codes)
                self.assertIn("completion_transition_event_missing", codes)
                self.assertIn("transition_event_empty_required_field", codes)
                self.assertIn("transition_event_invalid_actor", codes)
                self.assertIn("transition_event_invalid_human_confirmed", codes)
            finally:
                store.close()

    def test_doctor_state_audits_review_request_transition_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                now = now_iso()
                store.insert("projects", project_row())
                store.insert("tasks", task_row())
                store.insert(
                    "review_requests",
                    {
                        "id": "review_request_test",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "completed",
                        "reason": "review",
                        "based_on_event_id": "",
                        "based_on_snapshot": {},
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                store.insert(
                    "transition_events",
                    {
                        "id": "review_transition_test",
                        "transition": "import_review_result",
                        "entity_type": "review_request",
                        "entity_id": "review_request_test",
                        "actor": "",
                        "decision_source": "",
                        "human_confirmed": 2,
                        "reason": "",
                        "previous_state": "claimed",
                        "new_state": "completed",
                        "related_ids": {},
                        "snapshot": {},
                        "warnings": [],
                        "created_at": now,
                    },
                )
                data = doctor_state(store, "project_test", cwd=Path.cwd())
                codes = {finding["code"] for finding in data["findings"]}
                self.assertIn("transition_event_invalid_actor", codes)
                self.assertIn("transition_event_invalid_human_confirmed", codes)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
