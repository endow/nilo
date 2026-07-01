from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nilo.cli import main
from nilo.store import Store
from nilo.timeutil import now_iso
from nilo.view_model import analytics, overview, task_detail, tasks, timeline, todos


class ViewModelTests(unittest.TestCase):
    def test_view_model_returns_project_state_without_raw_initial_logs(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_view_db(db)

            overview_data = overview(db, "project_test")
            self.assertEqual(overview_data["project"]["id"], "project_test")
            self.assertEqual(overview_data["summary"]["open_tasks"], 1)
            self.assertEqual(overview_data["summary"]["completed_tasks"], 1)
            self.assertEqual(overview_data["summary"]["open_failure_logs"], 1)
            self.assertEqual(overview_data["latest_verification"]["status"], "passed")

            analytics_data = analytics(db, "project_test")
            self.assertEqual(analytics_data["summary"]["task_count"], 2)

            tasks_data = tasks(db, "project_test")
            self.assertEqual(len(tasks_data["tasks"]), 2)
            self.assertEqual(tasks_data["pagination"]["total"], 2)
            open_task = next(task for task in tasks_data["tasks"] if task["id"] == "task_open")
            self.assertEqual(open_task["review"]["open_blocking_findings"], 1)

            paged_tasks = tasks(db, "project_test", page=1, page_size=1)
            self.assertEqual(len(paged_tasks["tasks"]), 1)
            self.assertEqual(paged_tasks["pagination"]["total_pages"], 2)

            open_tasks = tasks(db, "project_test", status="open")
            self.assertEqual(open_tasks["pagination"]["total"], overview_data["summary"]["open_tasks"])
            failure_tasks = tasks(db, "project_test", open_failures=True)
            self.assertEqual(failure_tasks["pagination"]["total"], overview_data["summary"]["open_failure_logs"])
            finding_tasks = tasks(db, "project_test", open_findings=True)
            self.assertEqual(finding_tasks["pagination"]["total"], overview_data["summary"]["open_blocking_findings"])

            detail = task_detail(db, "project_test", "task_done")
            self.assertEqual(detail["task"]["title"], "Done task")
            self.assertEqual(detail["verification_history"][0]["stdout"]["preview"], "x" * 600)
            self.assertTrue(detail["verification_history"][0]["stdout"]["truncated"])
            self.assertNotIn("stdout", detail["accepted_verification_runs"][0])

            timeline_data = timeline(db, "project_test")
            event_types = {event["type"] for event in timeline_data["events"]}
            self.assertIn("task_created", event_types)
            self.assertIn("verification_recorded", event_types)
            self.assertIn("review_imported", event_types)
            self.assertIn("failure_logged", event_types)

            todos_data = todos(db, "project_test")
            self.assertEqual(todos_data["summary"]["open"], 1)
            self.assertEqual(overview_data["summary"]["open_todos"], 1)
            self.assertEqual(todos_data["todos"][0]["title"], "Check the view")

    def test_task_detail_is_scoped_to_project(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_view_db(db)
            now = now_iso()
            store = Store(db)
            try:
                store.insert(
                    "projects",
                    {
                        "id": "other_project",
                        "name": "Other Project",
                        "tech_stack": [],
                        "rules": [],
                        "default_completion_criteria": [],
                        "available_models": [],
                        "fallback_models": [],
                        "requires_local_execution": 0,
                        "created_at": now,
                    },
                )
                store.insert(
                    "tasks",
                    {
                        "id": "task_other",
                        "project_id": "other_project",
                        "title": "Other task",
                        "description": "",
                        "acceptance_criteria": [],
                        "parent_task_id": None,
                        "split_index": None,
                        "task_type": "implementation",
                        "risk_level": "medium",
                        "requires_understanding_check": 0,
                        "roadmap_commitment_id": "",
                        "roadmap_item_id": "",
                        "status": "planned",
                        "assigned_model_profile": "",
                        "degradation_mode": "normal",
                        "mode": "normal",
                        "base_commit": None,
                        "created_at": now,
                    },
                )
            finally:
                store.close()

            with self.assertRaises(KeyError):
                task_detail(db, "project_test", "task_other")

    def test_view_format_json_prints_overview(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_view_db(db)

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "view", "--project", "project_test", "--format", "json"])

            data = json.loads(output.getvalue())
            self.assertEqual(data["project"]["id"], "project_test")
            self.assertEqual(data["summary"]["open_tasks"], 1)

    def test_status_and_next_still_work_with_view_registered(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_view_db(db)

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "status", "--project", "project_test"])
            self.assertIn("project_test", status_output.getvalue())

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            self.assertIn("次の作業", next_output.getvalue())

    def seed_view_db(self, db: Path) -> None:
        now = now_iso()
        with redirect_stdout(io.StringIO()):
            main(["--db", str(db), "project", "create", "Project Test", "--id", "project_test"])
        store = Store(db)
        try:
            for row in [
                {
                    "id": "task_done",
                    "project_id": "project_test",
                    "title": "Done task",
                    "description": "description",
                    "acceptance_criteria": ["works"],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "implementation",
                    "risk_level": "medium",
                    "requires_understanding_check": 0,
                    "roadmap_commitment_id": "",
                    "roadmap_item_id": "",
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "mode": "normal",
                    "base_commit": None,
                    "created_at": now,
                },
                {
                    "id": "task_open",
                    "project_id": "project_test",
                    "title": "Open task",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "documentation",
                    "risk_level": "low",
                    "requires_understanding_check": 0,
                    "roadmap_commitment_id": "commitment_one",
                    "roadmap_item_id": "",
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "mode": "overdrive",
                    "base_commit": None,
                    "created_at": now,
                },
            ]:
                store.insert("tasks", row)
            store.insert(
                "task_completions",
                {
                    "id": "completion_done",
                    "task_id": "task_done",
                    "actor": "human",
                    "completed_by": "human",
                    "completed_snapshot": {},
                    "completion_note": "",
                    "accepted_verification_run_ids": ["verification_done"],
                    "accepted_review_result_ids": ["review_result_done"],
                    "human_decision_note": "accepted",
                    "completed_with_reservations": 0,
                    "decision_source": "",
                    "human_confirmed": 1,
                    "completed_at": now,
                    "invalidated_at": "",
                    "invalidated_by": "",
                    "invalidation_reason": "",
                    "reason": "done",
                    "created_at": now,
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_done",
                    "task_id": "task_done",
                    "evidence_check_id": "",
                    "source": "nilo_executed",
                    "command": "python -m unittest",
                    "cwd": "",
                    "stdout": "x" * 700,
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": 0,
                    "timeout_seconds": 1.0,
                    "git_head": "",
                    "git_status_porcelain": "",
                    "git_diff_hash": "",
                    "working_tree_dirty": 0,
                    "observed_paths": [],
                    "metadata": {},
                    "started_at": now,
                    "finished_at": now,
                    "created_at": now,
                },
            )
            store.insert(
                "review_requests",
                {
                    "id": "review_request_done",
                    "task_id": "task_done",
                    "requester": "ai",
                    "reviewer": "reviewer",
                    "status": "completed",
                    "reason": "review",
                    "based_on_event_id": "",
                    "based_on_snapshot": {},
                    "withdrawn_reason": "",
                    "withdrawn_actor": "",
                    "withdrawn_at": "",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            store.insert(
                "review_results",
                {
                    "id": "review_result_done",
                    "task_id": "task_done",
                    "review_request_id": "review_request_done",
                    "reviewer": "reviewer",
                    "verdict": "approved",
                    "summary": "good",
                    "based_on_event_id": "",
                    "based_on_snapshot": {},
                    "body_md": "long body",
                    "created_at": now,
                },
            )
            store.insert(
                "review_findings",
                {
                    "id": "finding_open",
                    "task_id": "task_open",
                    "review_request_id": "",
                    "review_result_id": "",
                    "title": "Blocking",
                    "severity": "high",
                    "status": "unresolved",
                    "file_path": "src/example.py",
                    "line": "1",
                    "blocking": 1,
                    "description": "fix",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            store.insert(
                "failure_logs",
                {
                    "id": "failure_open",
                    "project_id": "project_test",
                    "task_id": "task_open",
                    "report_id": "",
                    "category": "test",
                    "message": "failed once",
                    "severity": "medium",
                    "source": "",
                    "actor": "",
                    "related_id": "",
                    "snapshot": {},
                    "status": "open",
                    "resolved_at": "",
                    "resolved_by": "",
                    "resolution_note": "",
                    "resolution_source": "",
                    "human_confirmed": 0,
                    "decision_note": "",
                    "created_at": now,
                },
            )
            store.insert(
                "todos",
                {
                    "id": "todo_open",
                    "project_id": "project_test",
                    "title": "Check the view",
                    "kind": "follow_up",
                    "status": "open",
                    "description": "Look at the browser.",
                    "acceptance_hint": "Looks readable",
                    "priority": "normal",
                    "source_type": "user_message",
                    "source_task_id": "",
                    "roadmap_commitment_id": "",
                    "roadmap_revision_id": "",
                    "converted_task_id": "",
                    "actor": "",
                    "decision_source": "",
                    "superseded_by_type": "",
                    "superseded_by_id": "",
                    "created_at": now,
                    "triaged_at": "",
                    "triage_reason": "",
                },
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
