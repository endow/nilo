from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nilo.cli import main
from nilo.store import Store
from nilo.task_analytics import command_duration_summaries
from nilo.timeutil import now_iso


class TaskAnalyticsTests(unittest.TestCase):
    def test_duration_summary_latest_uses_datetime_order_with_offsets(self) -> None:
        summaries = command_duration_summaries(
            {
                "pytest": [
                    {"seconds": 1.0, "created_at": "2026-01-01T10:00:00+09:00", "started_at": "2026-01-01T10:00:00+09:00"},
                    {"seconds": 2.0, "created_at": "2026-01-01T02:00:00+00:00", "started_at": "2026-01-01T02:00:00+00:00"},
                ],
            },
            {"pytest": [10.0]},
        )

        self.assertEqual(summaries[0]["latest_seconds"], 2.0)

    def test_task_analytics_project_json_counts_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_analytics_db(db)

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "task", "analytics", "--project", "project_test", "--format", "json"])

            data = json.loads(output.getvalue())
            self.assertEqual(data["project_id"], "project_test")
            self.assertEqual(data["summary"]["task_count"], 3)
            self.assertEqual(data["summary"]["completed_count"], 1)
            self.assertEqual(data["summary"]["completed_with_reservations_count"], 1)
            self.assertEqual(data["summary"]["human_confirmed_completion_count"], 1)
            self.assertEqual(data["summary"]["completed_with_verification_count"], 1)
            self.assertEqual(data["summary"]["completed_with_review_count"], 1)
            self.assertEqual(data["summary"]["open_failure_task_count"], 1)
            self.assertEqual(data["summary"]["open_blocking_review_finding_task_count"], 1)
            self.assertEqual(data["summary"]["overdrive_task_count"], 1)
            self.assertEqual(data["verification"]["run_count"], 3)
            self.assertEqual(data["verification"]["passed_count"], 1)
            self.assertEqual(data["verification"]["failed_count"], 1)
            self.assertEqual(data["verification"]["timed_out_count"], 1)
            unittest_command = next(item for item in data["verification"]["duration_commands"] if item["command"] == "python -m unittest")
            self.assertEqual(unittest_command["run_count"], 2)
            self.assertEqual(unittest_command["max_seconds"], 9.5)
            self.assertEqual(unittest_command["latest_seconds"], 9.5)
            self.assertTrue(unittest_command["timeout_may_be_short"])
            self.assertEqual(data["review"]["request_count"], 1)
            self.assertEqual(data["review"]["result_count"], 1)
            self.assertEqual(data["review"]["verdict_counts"], {"changes_requested": 1})
            self.assertEqual(data["review"]["finding_severity_counts"], {"high": 1, "medium": 1})
            self.assertEqual(data["review"]["blocking_finding_count"], 2)
            self.assertEqual(data["review"]["open_finding_count"], 1)
            self.assertEqual(data["review"]["resolved_finding_count"], 1)
            self.assertEqual(len(data["review"]["open_blocking_findings"]), 1)
            self.assertEqual(data["failure"]["category_counts"], {"evidence_missing": 2})
            self.assertEqual(data["failure"]["severity_counts"], {"medium": 2})
            self.assertEqual(data["task_design"]["task_type_counts"]["implementation"], 2)
            self.assertEqual(data["task_design"]["risk_level_counts"]["high"], 1)

            since_output = io.StringIO()
            with redirect_stdout(since_output):
                main(["--db", str(db), "task", "analytics", "--project", "project_test", "--since", "30d", "--format", "json"])
            since_data = json.loads(since_output.getvalue())
            self.assertEqual(since_data["summary"]["task_count"], 2)
            self.assertNotIn("documentation", since_data["task_design"]["task_type_counts"])

            naive_since_output = io.StringIO()
            with redirect_stdout(naive_since_output):
                main(["--db", str(db), "task", "analytics", "--project", "project_test", "--since", "2026-01-01", "--format", "json"])
            naive_since_data = json.loads(naive_since_output.getvalue())
            self.assertEqual(naive_since_data["summary"]["task_count"], 2)

    def test_task_analytics_human_output_and_single_task(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_analytics_db(db)

            project_output = io.StringIO()
            with redirect_stdout(project_output):
                main(["--db", str(db), "task", "analytics", "--project", "project_test", "--since", "30d"])
            project_body = project_output.getvalue()
            self.assertIn("Task analytics: project_test (since 30d)", project_body)
            self.assertIn("総評:", project_body)
            self.assertIn("検証:", project_body)
            self.assertIn("レビュー:", project_body)
            self.assertIn("失敗:", project_body)
            self.assertIn("作業設計:", project_body)
            self.assertIn("同一 command の所要時間:", project_body)
            self.assertIn("timeout短すぎ候補", project_body)

            task_output = io.StringIO()
            with redirect_stdout(task_output):
                main(["--db", str(db), "task", "analytics", "--task", "task_completed"])
            task_body = task_output.getvalue()
            self.assertIn("Task analytics: task_completed", task_body)
            self.assertIn("完了/遷移:", task_body)
            self.assertIn("reservations=yes", task_body)
            self.assertIn("human_confirmed=yes", task_body)

    def seed_analytics_db(self, db: Path) -> None:
        now = now_iso()
        old = "2000-01-01T00:00:00+00:00"
        with redirect_stdout(io.StringIO()):
            main(["--db", str(db), "project", "create", "Project Test", "--id", "project_test"])
        store = Store(db)
        try:
            for task in [
                {
                    "id": "task_completed",
                    "project_id": "project_test",
                    "title": "Completed analytics task",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "implementation",
                    "risk_level": "high",
                    "requires_understanding_check": 1,
                    "roadmap_commitment_id": "commitment_test",
                    "roadmap_item_id": "",
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "mode": "overdrive",
                    "base_commit": None,
                    "created_at": now,
                },
                {
                    "id": "task_open",
                    "project_id": "project_test",
                    "title": "Open analytics task",
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
                {
                    "id": "task_old",
                    "project_id": "project_test",
                    "title": "Old analytics task",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "documentation",
                    "risk_level": "low",
                    "requires_understanding_check": 0,
                    "roadmap_commitment_id": "",
                    "roadmap_item_id": "",
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "mode": "normal",
                    "base_commit": None,
                    "created_at": old,
                },
            ]:
                store.insert("tasks", task)
            store.insert(
                "task_completions",
                {
                    "id": "completion_completed",
                    "task_id": "task_completed",
                    "actor": "human",
                    "completed_by": "human",
                    "completed_snapshot": {},
                    "completion_note": "",
                    "accepted_verification_run_ids": ["verification_passed"],
                    "accepted_review_result_ids": ["review_result_changes"],
                    "human_decision_note": "accepted",
                    "completed_with_reservations": 1,
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
            self.insert_verification(store, "verification_passed", "task_completed", "python -m unittest", 0, 0, "2099-01-01T00:00:01+00:00")
            self.insert_verification(store, "verification_failed", "task_open", "python -m unittest", 1, 0, "2099-01-01T00:00:02+00:00")
            self.insert_verification(store, "verification_timeout", "task_open", "pytest", None, 1, now)
            store.insert(
                "review_requests",
                {
                    "id": "review_request_test",
                    "task_id": "task_completed",
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
                    "id": "review_result_changes",
                    "task_id": "task_completed",
                    "review_request_id": "review_request_test",
                    "reviewer": "reviewer",
                    "verdict": "changes_requested",
                    "summary": "needs changes",
                    "based_on_event_id": "",
                    "based_on_snapshot": {},
                    "body_md": "body",
                    "created_at": now,
                },
            )
            store.insert(
                "review_findings",
                {
                    "id": "finding_blocking",
                    "task_id": "task_completed",
                    "review_request_id": "review_request_test",
                    "review_result_id": "review_result_changes",
                    "title": "Fix this",
                    "severity": "high",
                    "status": "unresolved",
                    "file_path": "src/example.py",
                    "line": "1",
                    "blocking": 1,
                    "description": "desc",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            store.insert(
                "review_findings",
                {
                    "id": "finding_accepted_risk",
                    "task_id": "task_completed",
                    "review_request_id": "review_request_test",
                    "review_result_id": "review_result_changes",
                    "title": "Accepted risk",
                    "severity": "medium",
                    "status": "accepted-risk",
                    "file_path": "src/example.py",
                    "line": "2",
                    "blocking": 1,
                    "description": "desc",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            store.insert(
                "review_finding_updates",
                {
                    "id": "finding_update",
                    "finding_id": "finding_blocking",
                    "task_id": "task_completed",
                    "previous_status": "unresolved",
                    "new_status": "addressed",
                    "reason": "fixed",
                    "actor": "ai",
                    "decision_source": "",
                    "human_confirmed": 0,
                    "created_at": now,
                },
            )
            self.insert_failure(store, "failure_one", "task_completed", now)
            self.insert_failure(store, "failure_two", "task_completed", now)
        finally:
            store.close()

    def insert_verification(self, store: Store, row_id: str, task_id: str, command: str, exit_code: int | None, timed_out: int, created_at: str) -> None:
        finished_at_by_id = {
            "verification_passed": "2099-01-01T00:00:02.500000+00:00",
            "verification_failed": "2099-01-01T00:00:09.500000+00:00",
            "verification_timeout": "2099-01-01T00:00:01+00:00",
        }
        store.insert(
            "verification_runs",
            {
                "id": row_id,
                "task_id": task_id,
                "evidence_check_id": "",
                "source": "nilo_executed",
                "command": command,
                "cwd": "",
                "stdout": "",
                "stderr": "",
                "exit_code": exit_code,
                "timed_out": timed_out,
                "timeout_seconds": 1.0,
                "git_head": "",
                "git_status_porcelain": " M src/example.py" if row_id == "verification_failed" else "",
                "git_diff_hash": "",
                "working_tree_dirty": 1 if row_id == "verification_failed" else 0,
                "observed_paths": [],
                "metadata": {"verification_mode": "targeted"},
                "started_at": "2099-01-01T00:00:00+00:00",
                "finished_at": finished_at_by_id.get(row_id, created_at),
                "created_at": created_at,
            },
        )

    def insert_failure(self, store: Store, row_id: str, task_id: str, created_at: str) -> None:
        store.insert(
            "failure_logs",
            {
                "id": row_id,
                "project_id": "project_test",
                "task_id": task_id,
                "report_id": "",
                "category": "evidence_missing",
                "message": "missing",
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
                "created_at": created_at,
            },
        )


if __name__ == "__main__":
    unittest.main()
