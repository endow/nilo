from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.cli import main
from nilo.mcp_server import call_tool
from nilo.project_boundary import ProjectBoundaryError, project_boundary_prompt, require_write_fence, resolve_project_boundary
from nilo.snapshot import compact_snapshot
from nilo.store import Store
from nilo.timeutil import now_iso


class ProjectBoundaryTests(unittest.TestCase):
    def stable_snapshot(self) -> dict:
        return {"git_head": "test-head", "git_diff_hash": "test-diff", "working_tree_dirty": True}

    def init_git(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def write_binding(
        self,
        root: Path,
        *,
        project_name: str | None = None,
        project_root: Path | None = None,
        repository_id: str | None = None,
        allow_self_modification: bool = False,
        tool_owner_repository: Path | None = None,
    ) -> None:
        data = {
            "project_name": project_name or root.name,
            "project_root": str((project_root or root).resolve()),
            "repository_id": repository_id or root.name,
            "allow_self_modification": allow_self_modification,
        }
        if tool_owner_repository is not None:
            data["tool_owner_repository"] = str(tool_owner_repository.resolve())
        path = root / ".nilo" / "project.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def create_project_task(self, db: Path, project_id: str, task_id: str = "task_test") -> None:
        store = Store(db)
        try:
            store.insert(
                "projects",
                {
                    "id": project_id,
                    "name": project_id,
                    "tech_stack": [],
                    "rules": [],
                    "default_completion_criteria": [],
                    "available_models": [],
                    "fallback_models": [],
                    "requires_local_execution": 0,
                    "created_at": now_iso(),
                },
            )
            store.insert(
                "tasks",
                {
                    "id": task_id,
                    "project_id": project_id,
                    "title": "Boundary task",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "documentation",
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
                },
            )
        finally:
            store.close()

    def test_init_generates_project_json(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            self.init_git(repo)
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with redirect_stdout(io.StringIO()):
                    main(["init"])
            finally:
                os.chdir(previous)

            data = json.loads((repo / ".nilo" / "project.json").read_text(encoding="utf-8"))

        self.assertEqual(data["repository_id"], "Chiffon")
        self.assertEqual(Path(data["project_root"]).resolve(), repo.resolve())
        self.assertFalse(data["allow_self_modification"])

    def test_status_and_next_include_project_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            self.init_git(repo)
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with redirect_stdout(io.StringIO()):
                    main(["init"])
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["status", "--ai", "--project", "Chiffon"])
                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["next", "--project", "Chiffon"])
            finally:
                os.chdir(previous)

        self.assertIn("Current project: Chiffon", status_output.getvalue())
        self.assertIn("Writable scope:", status_output.getvalue())
        self.assertIn("Self modification: disabled", status_output.getvalue())
        self.assertIn("Current project: Chiffon", next_output.getvalue())

    def test_project_boundary_prompt_separates_read_only_references_from_writes(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            self.init_git(repo)
            self.write_binding(repo, project_name="Chiffon", repository_id="Chiffon")
            boundary = resolve_project_boundary(repo, db_path=repo / ".nilo" / "nilo.db")
            prompt = project_boundary_prompt(boundary)

        self.assertIn("Forbidden: No writes outside the current writable repository.", prompt)
        self.assertIn("External files explicitly provided by the user may be read as read-only references.", prompt)
        self.assertIn("Do not modify sibling repositories, parent directories, or another project's .nilo database.", prompt)
        self.assertNotIn("No paths outside the writable repository", prompt)

    def test_project_root_mismatch_blocks_write_command(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            other = Path(directory) / "Other"
            self.init_git(repo)
            other.mkdir()
            self.write_binding(repo, project_root=other)
            db = repo / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()):
                        main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" -c "print(1)"'])
            finally:
                os.chdir(previous)

        self.assertIn("project binding mismatch", str(raised.exception).lower())

    def test_write_fence_blocks_db_outside_writable_repository(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            other = Path(directory) / "Other"
            self.init_git(repo)
            self.init_git(other)
            self.write_binding(repo, project_name="Chiffon", repository_id="Chiffon")
            external_db = other / ".nilo" / "nilo.db"
            boundary = resolve_project_boundary(repo, db_path=external_db)

            with self.assertRaises(ProjectBoundaryError) as raised:
                require_write_fence(boundary)

        self.assertEqual(raised.exception.code, "write_fence_violation")
        self.assertIn(str(external_db.resolve()), raised.exception.details["outside_write_targets"])

    def test_completion_invalidate_uses_project_write_fence(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            other = Path(directory) / "Other"
            self.init_git(repo)
            other.mkdir()
            self.write_binding(repo, project_name="Chiffon", project_root=other, repository_id="Chiffon")
            db = repo / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            store = Store(db)
            try:
                store.insert(
                    "task_completions",
                    {
                        "id": "completion_test",
                        "task_id": "task_test",
                        "actor": "human",
                        "completed_by": "human",
                        "completed_snapshot": {},
                        "completion_note": "done",
                        "accepted_verification_run_ids": [],
                        "accepted_review_result_ids": [],
                        "human_decision_note": "done",
                        "completed_with_reservations": 0,
                        "completed_at": now_iso(),
                        "reason": "done",
                        "created_at": now_iso(),
                    },
                )
            finally:
                store.close()
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()):
                        main(
                            [
                                "--db",
                                str(db),
                                "task",
                                "completion",
                                "invalidate",
                                "--completion",
                                "completion_test",
                                "--reason",
                                "bad completion",
                                "--actor",
                                "human",
                            ]
                        )
            finally:
                os.chdir(previous)

        self.assertIn("project binding mismatch", str(raised.exception).lower())

    def test_tool_owner_changes_do_not_block_unrelated_project_completion(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "Chiffon"
            owner = Path(directory) / "nilo"
            self.init_git(project)
            self.init_git(owner)
            self.write_binding(project, project_name="Chiffon", repository_id="Chiffon", tool_owner_repository=owner)
            (owner / "src.py").write_text("dirty\n", encoding="utf-8")
            db = project / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            previous = Path.cwd()
            try:
                os.chdir(project)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "done", "--actor", "human", "--human-confirm", "--decision-note", "test human decision"])
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                completions = store.list_where("task_completions", "task_id=?", ("task_test",))
                failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(len(completions), 1)
        self.assertEqual(failures, [])

    def test_tool_owner_changes_do_not_block_unrelated_project_check(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "Chiffon"
            owner = Path(directory) / "nilo"
            self.init_git(project)
            self.init_git(owner)
            self.write_binding(project, project_name="Chiffon", repository_id="Chiffon", tool_owner_repository=owner)
            (owner / "src.py").write_text("dirty\n", encoding="utf-8")
            db = project / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            previous = Path.cwd()
            try:
                os.chdir(project)
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "check",
                            "--task",
                            "task_test",
                            "--mode",
                            "targeted",
                            f'"{sys.executable}" -c "print(1)"',
                        ]
                    )
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                runs = store.list_where("verification_runs", "task_id=?", ("task_test",))
                failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(len(runs), 1)
        self.assertEqual(failures, [])

    def test_explicit_tool_owner_check_blocks_with_repository_identity(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "Chiffon"
            owner = Path(directory) / "nilo"
            self.init_git(project)
            self.init_git(owner)
            self.write_binding(project, project_name="Chiffon", repository_id="Chiffon", tool_owner_repository=owner)
            (owner / "src.py").write_text("dirty\n", encoding="utf-8")
            db = project / ".nilo" / "nilo.db"

            boundary = resolve_project_boundary(project, db_path=db)
            with self.assertRaises(ProjectBoundaryError) as raised:
                require_write_fence(boundary, include_tool_owner_repository=True)

        self.assertIn("target_project_root", str(raised.exception))
        self.assertIn("tool_owner_repository", str(raised.exception))
        self.assertIn(str(project.resolve()), raised.exception.details["inspected_repositories"])
        self.assertIn(str(owner.resolve()), raised.exception.details["inspected_repositories"])

    def test_self_modification_allowed_only_for_nilo_repository(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "nilo"
            self.init_git(repo)
            self.write_binding(repo, project_name="Nilo", repository_id="nilo", allow_self_modification=True)
            (repo / "src.py").write_text("dirty\n", encoding="utf-8")
            db = repo / ".nilo" / "nilo.db"
            self.create_project_task(db, "nilo")
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "done", "--actor", "human", "--human-confirm", "--decision-note", "test human decision"])
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                completion = store.latest_for_task("task_completions", "task_test")
            finally:
                store.close()

        self.assertIsNotNone(completion)

    def test_self_modification_allows_case_variant_nilo_repository_id(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Nilo"
            self.init_git(repo)
            self.write_binding(repo, project_name="Nilo", repository_id="Nilo", allow_self_modification=True)
            (repo / "src.py").write_text("dirty\n", encoding="utf-8")
            db = repo / ".nilo" / "nilo.db"
            self.create_project_task(db, "Nilo")
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "done", "--actor", "human", "--human-confirm", "--decision-note", "test human decision"])
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                completion = store.latest_for_task("task_completions", "task_test")
            finally:
                store.close()

        self.assertIsNotNone(completion)

    def test_missing_tool_owner_binding_does_not_infer_dev_checkout_on_load(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "Chiffon"
            owner = Path(directory) / "nilo"
            self.init_git(project)
            self.init_git(owner)
            self.write_binding(project, project_name="Chiffon", repository_id="Chiffon")
            (owner / "src.py").write_text("dirty\n", encoding="utf-8")
            db = project / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            previous = Path.cwd()
            try:
                os.chdir(project)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "done", "--actor", "human", "--human-confirm", "--decision-note", "test human decision"])
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                completion = store.latest_for_task("task_completions", "task_test")
            finally:
                store.close()

        self.assertIsNotNone(completion)

    def test_self_development_flag_rejected_outside_nilo_repository(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            self.init_git(repo)
            self.write_binding(repo, project_name="Chiffon", repository_id="Chiffon")
            db = repo / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            previous = Path.cwd()
            try:
                os.chdir(repo)
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()):
                        main(["--db", str(db), "start", "Self dev", "--project", "Chiffon", "--self-development"])
            finally:
                os.chdir(previous)

        self.assertIn("only available in the Nilo repository", str(raised.exception))

    def test_mcp_write_fence_ignores_tool_owner_changes_for_target_repository_verification(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "Chiffon"
            owner = Path(directory) / "nilo"
            self.init_git(project)
            self.init_git(owner)
            self.write_binding(project, project_name="Chiffon", repository_id="Chiffon", tool_owner_repository=owner)
            (owner / "src.py").write_text("dirty\n", encoding="utf-8")
            db = project / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            previous = Path.cwd()
            try:
                os.chdir(project)
                status = call_tool("get_agent_work_context", {"project_id": "Chiffon"}, db)
                token = status["active_tasks"][0]["write_context_token"]
                result = call_tool(
                    "record_verification_run",
                    {
                        "task_id": "task_test",
                        "context_token": token,
                        "command": "pytest",
                        "cwd": str(project),
                        "stdout": "passed",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                    },
                    db,
                )
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                runs = store.list_where("verification_runs", "task_id=?", ("task_test",))
                failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(result["task_id"], "task_test")
        self.assertEqual(len(runs), 1)
        self.assertEqual(failures, [])

    def test_mcp_review_result_import_ignores_unrelated_tool_owner_changes(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "Chiffon"
            owner = Path(directory) / "nilo"
            self.init_git(project)
            self.init_git(owner)
            self.write_binding(project, project_name="Chiffon", repository_id="Chiffon", tool_owner_repository=owner)
            (owner / "src.py").write_text("dirty\n", encoding="utf-8")
            db = project / ".nilo" / "nilo.db"
            self.create_project_task(db, "Chiffon")
            snapshot = self.stable_snapshot()
            request_snapshot = compact_snapshot(snapshot)
            store = Store(db)
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
                        "reason": "test",
                        "based_on_event_id": "",
                        "based_on_snapshot": request_snapshot,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            finally:
                store.close()
            previous = Path.cwd()
            try:
                os.chdir(project)
                status = call_tool("get_agent_work_context", {"project_id": "Chiffon"}, db)
                token = status["active_tasks"][0]["write_context_token"]
                with patch("nilo.transitions.current_git_snapshot", return_value=snapshot):
                    result = call_tool(
                        "import_review_result",
                        {
                            "task_id": "task_test",
                            "review_id": "review_test",
                            "reviewer": "claude-code",
                            "context_token": token,
                            "body_md": "# ReviewResult\n\n## Verdict\napproved\n\n## Summary\nok\n\n## Findings\nなし\n",
                        },
                        db,
                    )
            finally:
                os.chdir(previous)

            store = Store(db)
            try:
                results = store.list_where("review_results", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(result["task_id"], "task_test")
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
