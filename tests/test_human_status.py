from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from nilo.cli import main
from nilo.human_status import human_task_status
from nilo.mcp_server import call_tool
from nilo.project_logic import project_work_state
from nilo.roadmap_render import human_roadmap_work_state_text


class HumanStatusTests(unittest.TestCase):
    def test_human_task_status_translates_major_machine_statuses(self) -> None:
        expected = {
            "planned": "作業指示の作成待ちです。",
            "instruction_generated": "AI が作業中です。",
            "agent_reported": "作業報告が届いています。",
            "evidence_submitted": "証跡が提出されています。",
            "verification_passed": "検証は成功しています。",
            "verification_failed": "検証に失敗しています。",
            "verification_timed_out": "検証がタイムアウトしています。",
            "review_requested": "レビュー依頼中です。",
            "review_reviewer_unavailable": "レビュー担当の起動待ちです。",
            "review_claimed": "レビュー担当が依頼を受け取りました。",
            "review_in_progress": "レビュー中です。",
            "review_stale": "レビューが停止しています。",
            "review_approved": "レビューは承認されています。",
            "review_commented": "レビューコメントがあります。",
            "review_changes_requested": "レビューで修正が必要です。",
            "needs_human_review": "人間の確認待ちです。",
            "completed_by_user": "このタスクは人間が完了として受け入れました。",
            "completed_by_ai": "このタスクはAIが完了として記録しました。",
        }

        for machine_status, state in expected.items():
            with self.subTest(machine_status=machine_status):
                human = human_task_status(machine_status)
                self.assertEqual(human["state"], state)
                self.assertEqual(human["machine_status"], machine_status)
                self.assertNotIn(machine_status, human["state"])
                self.assertNotIn(machine_status, human["summary"])

    def test_unknown_status_returns_warning_without_raising(self) -> None:
        human = human_task_status("new_internal_state")

        self.assertEqual(human["state"], "状態を確認してください。")
        self.assertEqual(human["severity"], "warning")
        self.assertEqual(human["machine_status"], "new_internal_state")
        self.assertIn("new_internal_state", human["summary"])

    def test_project_work_state_returns_natural_japanese(self) -> None:
        cases = [
            ({}, "作業中のタスクはありません。"),
            ({"task_review": "review_changes_requested"}, "レビューで修正が必要です。"),
            ({"task_reviewer": "review_reviewer_unavailable"}, "レビュー担当の起動待ちです。"),
            ({"task_stale": "review_stale"}, "レビューが停止しています。"),
            ({"task_commented": "review_commented"}, "レビュー結果の確認待ちです。"),
            ({"task_timeout": "verification_timed_out"}, "検証がタイムアウトしています。"),
            ({"task_failed": "verification_failed"}, "検証に失敗しています。"),
            ({"task_verified": "verification_passed"}, "人間の完了判断待ちです。"),
            ({"task_reported": "agent_reported"}, "検証待ちです。"),
            ({"task_instruction": "instruction_generated"}, "作業報告待ちです。"),
        ]

        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                tasks = [
                    {"id": task_id, "status": status, "task_type": "implementation"}
                    for task_id, status in statuses.items()
                ]
                state = project_work_state(tasks, statuses)
                self.assertEqual(state, expected)
                self.assertNotIn("review_changes_requested", state)
                self.assertNotIn("reviewer unavailable", state)
                self.assertNotIn("acceptance review", state)
                self.assertNotIn("implementation/report", state)
                self.assertNotIn("human review", state)
                self.assertNotIn("active task", state)

    def test_completion_statuses_are_distinct(self) -> None:
        by_user = human_task_status("completed_by_user")
        by_ai = human_task_status("completed_by_ai")

        self.assertIn("人間", by_user["state"])
        self.assertIn("AI", by_ai["state"])
        self.assertNotEqual(by_user["state"], by_ai["state"])

    def test_roadmap_work_state_translates_new_human_project_states(self) -> None:
        cases = {
            "レビュー結果の確認待ちです。": "waiting for review comment triage",
            "レビューが停止しています。": "review stalled",
            "検証に失敗しています。": "verification failed",
            "検証がタイムアウトしています。": "verification timed out",
        }

        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(human_roadmap_work_state_text(source, "en"), expected)
                self.assertNotEqual(human_roadmap_work_state_text(source, "en"), source)

    def test_get_task_status_adds_human_status_without_removing_machine_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Human status task"])
                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                result = call_tool("get_task_status", {"task_id": task_id}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["human_status"]["machine_status"], "planned")
        self.assertEqual(result["human_status"]["state"], "作業指示の作成待ちです。")


if __name__ == "__main__":
    unittest.main()
