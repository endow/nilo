from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from nilo.cli import main
from nilo.mcp_server import call_tool
from nilo.store import Store


class StatusSurfaceRegressionTests(unittest.TestCase):
    def build_completion_review_project(self, root: Path, db: Path) -> str:
        proposal = root / "status_surface_proposal.md"
        proposal.write_text(
            """# Status Surface Baseline

## Intent
状態表示の経路をリファクタする前に、現在のCLIとMCPの互換surfaceを固定する。

## Success Criteria
- accepted roadmap commitment が project summary に残る
- completion_needs_review task が active task として見える
""",
            encoding="utf-8",
        )

        adopt_output = io.StringIO()
        with redirect_stdout(io.StringIO()):
            main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
        with redirect_stdout(adopt_output):
            main(
                [
                    "--db",
                    str(db),
                    "roadmap",
                    "adopt",
                    "--project",
                    "project_test",
                    "--file",
                    str(proposal),
                    "--reason",
                    "baseline accepted",
                    "--actor",
                    "human",
                    "--human-confirm",
                    "--decision-note",
                    "test human decision",
                ]
            )
        with redirect_stdout(io.StringIO()):
            main(
                [
                    "--db",
                    str(db),
                    "task",
                    "create",
                    "--project",
                    "project_test",
                    "--id",
                    "task_surface",
                    "--title",
                    "Status surface task",
                    "--type",
                    "implementation",
                    "--risk",
                    "medium",
                ]
            )
        store = Store(db)
        try:
            store.update("tasks", "task_surface", {"status": "completion_needs_review"})
        finally:
            store.close()

        for line in adopt_output.getvalue().splitlines():
            if line.startswith("accepted_commitment: "):
                return line.split(": ", 1)[1]
        raise AssertionError("accepted commitment id not found in roadmap status")

    def test_cli_status_next_and_project_summary_preserve_completion_review_surface(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                commitment_id = self.build_completion_review_project(root, db)

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "status", "--ai", "--project", "project_test"])

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next", "--project", "project_test"])

                summary_output = io.StringIO()
                with redirect_stdout(summary_output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            finally:
                os.chdir(previous_cwd)

        status_body = status_output.getvalue()
        self.assertIn("active_task: task_surface [completion_needs_review] Status surface task", status_body)
        self.assertIn("latest_verification: status=missing", status_body)
        self.assertIn("latest_review: status=not_run outcome=not_run verdict=none freshness=none orphan_findings=false unresolved=0", status_body)
        self.assertNotIn("detail_commands:", status_body)
        self.assertIn("required_commands:", status_body)

        next_body = next_output.getvalue()
        self.assertIn("タスク: task_surface", next_body)
        self.assertIn("状態: 完了記録の確認が必要", next_body)
        self.assertIn("最新のタスク状態を確認してください。", next_body)

        summary = json.loads(summary_output.getvalue())
        self.assertEqual(summary["roadmap_position"], "accepted commitment: Status Surface Baseline")
        self.assertEqual(summary["work_state"], "完了記録の確認が必要です。")
        self.assertEqual(summary["current_phase"], "implementation")
        self.assertEqual(summary["roadmap_commitments"][0]["id"], commitment_id)
        self.assertEqual(summary["active_tasks"][0]["id"], "task_surface")
        self.assertEqual(summary["active_tasks"][0]["status"], "completion_needs_review")
        self.assertEqual(summary["active_tasks"][0]["latest_verification_run"], "none")
        self.assertEqual(summary["unexecuted_verifications"], [])

    def test_mcp_status_tools_share_expected_project_state_fields(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                commitment_id = self.build_completion_review_project(root, db)

                project_status = call_tool("get_project_status", {"project_id": "project_test"}, db)
                project_summary = call_tool("get_project_summary", {"project_id": "project_test"}, db)
                agent_context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                next_step = call_tool("get_next_step", {"project_id": "project_test"}, db)
                compact_status = call_tool("get_status", {"project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        for result in [project_status, project_summary, agent_context, next_step, compact_status]:
            self.assertEqual(result["project_id"], "project_test")

        self.assertEqual(project_status["roadmap_position"], "accepted commitment: Status Surface Baseline")
        self.assertEqual(project_summary["roadmap_commitments"][0]["id"], commitment_id)
        self.assertEqual(project_status["active_tasks"][0]["id"], "task_surface")
        self.assertEqual(project_summary["active_tasks"][0]["status"], "completion_needs_review")
        self.assertEqual(project_status["next_actions"], project_summary["next_actions"])
        self.assertEqual(agent_context["next_actions"], project_summary["next_actions"])
        self.assertEqual(next_step["roadmap_position"], project_summary["roadmap_position"])

        self.assertEqual(agent_context["active_tasks"][0]["id"], "task_surface")
        self.assertTrue(agent_context["active_tasks"][0]["write_context_token"].startswith("task:task_surface:"))
        self.assertEqual(agent_context["next_step"]["action_id"], "continue_active_task")
        self.assertEqual(agent_context["next_step"]["task_status"], "completion_needs_review")
        self.assertFalse(agent_context["next_step"]["requires_explicit_human_intent"])
        self.assertTrue(agent_context["next_step"]["safe_for_ai"])
        self.assertEqual(next_step["next_step"], agent_context["next_step"])

        self.assertTrue(compact_status["compact"])
        self.assertEqual(compact_status["active_task"]["id"], "task_surface")
        self.assertEqual(compact_status["active_task"]["status"], "completion_needs_review")
        self.assertEqual(compact_status["latest_verification"]["status"], "missing")
        self.assertEqual(compact_status["write_context_token"], agent_context["write_context_token"])


if __name__ == "__main__":
    unittest.main()
