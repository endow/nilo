from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.ai_context import render_ai_context_text
from nilo.cli import main
from nilo.mcp_identity import identity_matches_expected, mcp_identity
from nilo.mcp_server import HEADROOM_TOOL_METADATA, McpToolError, call_tool, handle_request, project_summary
from nilo.review_dispatcher import find_executable
from nilo.store import JSON_COLUMNS, Store
from nilo.timeutil import now_iso
from nilo.workspace_resolver import resolve_workspace_context


class McpServerTests(unittest.TestCase):
    def init_git_repo(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

    def create_project_db(self, root: Path, project_id: str) -> Path:
        db = root / ".nilo" / "nilo.db"
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
        finally:
            store.close()
        return db

    def test_mcp_identity_returns_current_repository_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo_identity"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            db = root / ".nilo" / "nilo.db"
            db.parent.mkdir()
            db.touch()

            previous_db = os.environ.pop("NILO_DB", None)
            try:
                identity = mcp_identity(root, db)
                default_identity = mcp_identity(root)
            finally:
                if previous_db is not None:
                    os.environ["NILO_DB"] = previous_db

        self.assertEqual(identity["git_root"], str(root.resolve()))
        self.assertEqual(identity["db_path"], str(db.resolve()))
        self.assertEqual(default_identity["db_path"], str(db.resolve()))
        self.assertEqual(identity["repository_name"], "repo_identity")
        self.assertEqual(identity["project_id"], "repo_identity")
        self.assertIn("git_head", identity)

    def test_resolve_workspace_context_project_root_uses_repo_db(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory) / "Chiffon"
            self.init_git_repo(repo)

            context = resolve_workspace_context(project_root=str(repo), default_cwd=Path(directory))

        self.assertEqual(context["repository_name"], "Chiffon")
        self.assertEqual(context["source"], "project_root")
        self.assertEqual(context["git_root"], str(repo.resolve()))
        self.assertEqual(context["db_path"], str((repo / ".nilo" / "nilo.db").resolve()))

    def test_project_root_identity_does_not_fake_git_root_for_non_git_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "not_git"
            root.mkdir()
            result = call_tool("mcp_ping", {"project_root": str(root)}, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["identity"]["repository_name"], "not_git")
        self.assertEqual(result["identity"]["project_root"], str(root.resolve()))
        self.assertEqual(result["identity"]["git_root"], "")
        self.assertEqual(result["identity"]["source"], "project_root")

    def test_mcp_project_root_overrides_nilo_db(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            repo_a = base / "repoA"
            repo_b = base / "repoB"
            self.init_git_repo(repo_a)
            self.init_git_repo(repo_b)
            db_a = self.create_project_db(repo_a, "repoA")
            self.create_project_db(repo_b, "repoB")

            previous_cwd = Path.cwd()
            previous_db = os.environ.get("NILO_DB")
            try:
                os.chdir(repo_a)
                os.environ["NILO_DB"] = str(db_a)
                result = call_tool("mcp_ping", {"project_root": str(repo_b)}, None)
            finally:
                os.chdir(previous_cwd)
                if previous_db is None:
                    os.environ.pop("NILO_DB", None)
                else:
                    os.environ["NILO_DB"] = previous_db

        self.assertTrue(result["ok"])
        self.assertEqual(result["identity"]["repository_name"], "repoB")
        self.assertEqual(result["identity"]["source"], "project_root")
        self.assertEqual(result["identity"]["db_path"], str((repo_b / ".nilo" / "nilo.db").resolve()))

    def test_mcp_project_root_missing_returns_tool_error(self) -> None:
        with TemporaryDirectory() as directory:
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "mcp_ping",
                        "arguments": {"project_root": str(Path(directory) / "missing")},
                    },
                },
                None,
            )

        result = response["result"]
        self.assertTrue(result["isError"])
        message = json.loads(result["content"][0]["text"])["error"]
        self.assertIn("project_root not found", message)

    def test_workspace_cli_add_list_show_remove(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            repo = Path(directory) / "Chiffon"
            self.init_git_repo(repo)
            db = self.create_project_db(repo, "Chiffon")
            with patch("nilo.workspace_resolver.Path.home", return_value=home):
                with redirect_stdout(io.StringIO()):
                    main(["workspace", "add", "Chiffon", "--root", str(repo)])
                list_output = io.StringIO()
                with redirect_stdout(list_output):
                    main(["workspace", "list"])
                show_output = io.StringIO()
                with redirect_stdout(show_output):
                    main(["workspace", "show", "Chiffon"])
                with redirect_stdout(io.StringIO()):
                    main(["workspace", "remove", "Chiffon"])
                after_output = io.StringIO()
                with redirect_stdout(after_output):
                    main(["workspace", "list"])
                db_still_exists = db.exists()

        self.assertIn("Chiffon", list_output.getvalue())
        self.assertIn(str(db.resolve()), show_output.getvalue())
        self.assertIn("- none", after_output.getvalue())
        self.assertTrue(db_still_exists)

    def test_mcp_workspace_resolves_registered_root(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            repo_a = base / "repoA"
            repo_b = base / "repoB"
            self.init_git_repo(repo_a)
            self.init_git_repo(repo_b)
            self.create_project_db(repo_a, "repoA")
            self.create_project_db(repo_b, "repoB")
            with patch("nilo.workspace_resolver.Path.home", return_value=home):
                with redirect_stdout(io.StringIO()):
                    main(["workspace", "add", "Chiffon", "--root", str(repo_b)])
                previous_cwd = Path.cwd()
                try:
                    os.chdir(repo_a)
                    result = call_tool("mcp_ping", {"workspace": "Chiffon"}, None)
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(result["identity"]["repository_name"], "repoB")
        self.assertEqual(result["identity"]["source"], "workspace")
        self.assertEqual(result["identity"]["db_path"], str((repo_b / ".nilo" / "nilo.db").resolve()))

    def test_project_root_takes_precedence_over_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            repo_b = base / "repoB"
            repo_c = base / "repoC"
            self.init_git_repo(repo_b)
            self.init_git_repo(repo_c)
            self.create_project_db(repo_b, "repoB")
            self.create_project_db(repo_c, "repoC")
            with patch("nilo.workspace_resolver.Path.home", return_value=home):
                with redirect_stdout(io.StringIO()):
                    main(["workspace", "add", "Chiffon", "--root", str(repo_b)])
                result = call_tool("mcp_ping", {"workspace": "Chiffon", "project_root": str(repo_c)}, None)

        self.assertEqual(result["identity"]["repository_name"], "repoC")
        self.assertEqual(result["identity"]["source"], "project_root")

    def test_workspace_not_found_returns_registered_list(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            repo_nilo = base / "nilo"
            repo_mgtool = base / "mgtool"
            self.init_git_repo(repo_nilo)
            self.init_git_repo(repo_mgtool)
            with patch("nilo.workspace_resolver.Path.home", return_value=home):
                with redirect_stdout(io.StringIO()):
                    main(["workspace", "add", "nilo", "--root", str(repo_nilo)])
                    main(["workspace", "add", "mgtool", "--root", str(repo_mgtool)])
                result = call_tool("mcp_ping", {"workspace": "Chiffon"}, None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "workspace_not_found")
        self.assertEqual(result["registered_workspaces"], ["mgtool", "nilo"])

    def test_guard_checks_resolved_project_root_identity(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            repo_a = base / "repoA"
            repo_b = base / "Chiffon"
            self.init_git_repo(repo_a)
            self.init_git_repo(repo_b)
            self.create_project_db(repo_b, "Chiffon")
            previous_cwd = Path.cwd()
            try:
                os.chdir(repo_a)
                result = call_tool("mcp_ping", {"project_root": str(repo_b), "expected_project": "Chiffon"}, None)
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(result["ok"])
        self.assertEqual(result["identity"]["repository_name"], "Chiffon")

    def test_identity_matches_expected_returns_true_for_matching_project(self) -> None:
        ok, reasons = identity_matches_expected({"project_id": "Other", "repository_name": "Chiffon", "git_root": ""}, expected_project="Chiffon")

        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_identity_matches_expected_returns_false_for_mismatched_project(self) -> None:
        ok, reasons = identity_matches_expected({"project_id": "nilo", "repository_name": "nilo", "git_root": ""}, expected_project="Chiffon")

        self.assertFalse(ok)
        self.assertIn("expected project Chiffon", reasons[0])
        self.assertIn("nilo", reasons[0])

    def test_identity_matches_expected_detects_git_root_mismatch(self) -> None:
        ok, reasons = identity_matches_expected({"project_id": "nilo", "repository_name": "nilo", "git_root": "/repo/nilo"}, expected_git_root="/repo/Chiffon")

        self.assertFalse(ok)
        self.assertIn("expected git root", reasons[0])
        self.assertIn("MCP git root", reasons[0])

    def test_mcp_identity_uses_bounded_git_metadata_calls(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            with patch(
                "nilo.mcp_identity.git_output",
                side_effect=[
                    (0, str(root), ""),
                    (0, "abc123", ""),
                    (0, " M src/nilo/mcp_identity.py", ""),
                ],
            ) as git_output:
                identity = mcp_identity(root, db)

        self.assertEqual(identity["git_root"], str(root.resolve()))
        self.assertEqual(identity["git_head"], "abc123")
        self.assertTrue(identity["working_tree_dirty"])
        self.assertEqual(git_output.call_count, 3)

    def test_mcp_stdio_hello_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            client: McpStdioClient | None = None
            try:
                client = McpStdioClient(db)
                response = client.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "hello-client"},
                    },
                )
                client.notify("notifications/initialized")
                tools = client.request("tools/list", {})
            finally:
                if client is not None:
                    client.close()

        self.assertEqual(response["result"]["serverInfo"]["name"], "nilo")
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertTrue(tools["result"]["tools"])

    def test_mcp_default_db_uses_workspace_root_when_cwd_is_elsewhere(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            outside = base / "outside"
            repo.mkdir()
            outside.mkdir()
            db = repo / ".nilo" / "nilo.db"
            store = Store(db)
            try:
                store.insert(
                    "projects",
                    {
                        "id": "project_test",
                        "name": "Project Test",
                        "tech_stack": [],
                        "rules": [],
                        "default_completion_criteria": [],
                        "available_models": [],
                        "fallback_models": [],
                        "requires_local_execution": 0,
                        "created_at": now_iso(),
                    },
                )
            finally:
                store.close()

            previous_cwd = Path.cwd()
            previous_workspace = os.environ.get("NILO_WORKSPACE_ROOT")
            try:
                os.chdir(outside)
                os.environ["NILO_WORKSPACE_ROOT"] = str(repo)
                result = call_tool("get_agent_work_context", {"project_id": "project_test"}, None)
            finally:
                os.chdir(previous_cwd)
                if previous_workspace is None:
                    os.environ.pop("NILO_WORKSPACE_ROOT", None)
                else:
                    os.environ["NILO_WORKSPACE_ROOT"] = previous_workspace

        self.assertEqual(result["project_id"], "project_test")

    def test_mcp_default_db_prefers_candidate_matching_requested_project(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            unrelated = base / "unrelated"
            outside = base / "outside"
            repo.mkdir()
            unrelated.mkdir()
            outside.mkdir()
            for db, project_id in (
                (repo / ".nilo" / "nilo.db", "project_test"),
                (unrelated / ".nilo" / "nilo.db", "other_project"),
            ):
                store = Store(db)
                try:
                    store.insert(
                        "projects",
                        {
                            "id": project_id,
                            "name": "Project Test",
                            "tech_stack": [],
                            "rules": [],
                            "default_completion_criteria": [],
                            "available_models": [],
                            "fallback_models": [],
                            "requires_local_execution": 0,
                            "created_at": now_iso(),
                        },
                    )
                finally:
                    store.close()

            previous_cwd = Path.cwd()
            previous_nilo_workspace = os.environ.get("NILO_WORKSPACE_ROOT")
            previous_workspace = os.environ.get("WORKSPACE_ROOT")
            try:
                os.chdir(outside)
                os.environ["NILO_WORKSPACE_ROOT"] = str(unrelated)
                os.environ["WORKSPACE_ROOT"] = str(repo)
                result = call_tool("get_agent_work_context", {"project_id": "project_test"}, None)
            finally:
                os.chdir(previous_cwd)
                if previous_nilo_workspace is None:
                    os.environ.pop("NILO_WORKSPACE_ROOT", None)
                else:
                    os.environ["NILO_WORKSPACE_ROOT"] = previous_nilo_workspace
                if previous_workspace is None:
                    os.environ.pop("WORKSPACE_ROOT", None)
                else:
                    os.environ["WORKSPACE_ROOT"] = previous_workspace

        self.assertEqual(result["project_id"], "project_test")

    def test_mcp_project_not_found_reports_db_path_without_transport_crash(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated_db = root / ".nilo" / "nilo.db"
            store = Store(unrelated_db)
            store.close()

            previous_cwd = Path.cwd()
            previous_workspace = os.environ.pop("NILO_WORKSPACE_ROOT", None)
            previous_db = os.environ.pop("NILO_DB", None)
            try:
                os.chdir(root)
                response = handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "get_agent_work_context",
                            "arguments": {"project_id": "project_test"},
                        },
                    },
                    None,
                )
            finally:
                os.chdir(previous_cwd)
                if previous_workspace is not None:
                    os.environ["NILO_WORKSPACE_ROOT"] = previous_workspace
                if previous_db is not None:
                    os.environ["NILO_DB"] = previous_db

        result = response["result"]
        self.assertTrue(result["isError"])
        message = json.loads(result["content"][0]["text"])["error"]
        self.assertIn("none matched the requested context", message)
        self.assertIn("project_id=project_test", message)
        self.assertIn(".nilo", message)
        self.assertIn("nilo.db", message)

    def test_mcp_dispatch_review_context_error_is_tool_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            store = Store(db)
            store.close()

            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "dispatch_review",
                        "arguments": {
                            "task_id": "task_missing",
                            "actor": "codex",
                            "reviewer": "claude-code",
                            "allow_cli_fallback": True,
                        },
                    },
                },
                db,
            )

        result = response["result"]
        self.assertTrue(result["isError"])
        message = json.loads(result["content"][0]["text"])["error"]
        self.assertIn("review dispatch failed during resolve_context", message)
        self.assertIn("task not found: task_missing", message)

    def test_mcp_dispatch_review_unexpected_error_is_tool_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / ".nilo" / "nilo.db"
            store = Store(db)
            store.close()

            with patch("nilo.mcp_server.dispatch_review", side_effect=RuntimeError("boom")):
                response = handle_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "dispatch_review",
                            "arguments": {
                                "task_id": "task_missing",
                                "actor": "codex",
                                "reviewer": "claude-code",
                                "allow_cli_fallback": True,
                            },
                        },
                    },
                    db,
                )

        result = response["result"]
        self.assertTrue(result["isError"])
        message = json.loads(result["content"][0]["text"])["error"]
        self.assertIn("review dispatch failed unexpectedly: RuntimeError: boom", message)

    def test_two_mcp_stdio_clients_complete_review_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            requester: McpStdioClient | None = None
            reviewer: McpStdioClient | None = None
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "MCP handoff task"])

                requester = McpStdioClient(db)
                reviewer = McpStdioClient(db)
                requester.initialize()
                reviewer.initialize()

                registered = reviewer.call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"worker_path": "test mcp stdio reviewer"},
                    },
                )
                context = requester.call_tool("get_agent_work_context", {"project_id": "project_test"})
                task_id = context["active_tasks"][0]["id"]
                requested = requester.call_tool(
                    "request_task_review",
                    {
                        "task_id": task_id,
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "reason": "verify mcp stdio handoff",
                        "context_token": context["write_context_token"],
                    },
                )
                claimed = reviewer.call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"})
                imported = reviewer.call_tool(
                    "import_review_result",
                    {
                        "task_id": task_id,
                        "review_id": claimed["review_id"],
                        "reviewer": "claude-code",
                        "last_seen_event_id": claimed["latest_event"]["event_id"],
                        "body_md": review_body(verdict="approved", summary="MCP stdio handoff completed.", findings=""),
                    },
                )
                review_status = requester.call_tool("get_review_status", {"task_id": task_id})
            finally:
                if requester is not None:
                    requester.close()
                if reviewer is not None:
                    reviewer.close()
                os.chdir(previous_cwd)

        self.assertEqual(registered["reviewer"]["reviewer"], "claude-code")
        self.assertEqual(requested["operation"], "request_task_review")
        self.assertEqual(requested["result"]["review_request"]["status"], "requested")
        self.assertTrue(claimed["claimed"])
        self.assertEqual(claimed["review_id"], requested["result"]["review_request"]["id"])
        self.assertIn("# Review Request", claimed["prompt_md"])
        self.assertEqual(imported["review_result"]["verdict"], "approved")
        self.assertEqual(review_status["review_requests"][0]["status"], "completed")
        self.assertEqual(review_status["review_results"][0]["reviewer"], "claude-code")

    def test_claude_code_e2e_requires_real_session_profile_and_nonce_echo(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            requester: McpStdioClient | None = None
            reviewer: McpStdioClient | None = None
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Claude Code E2E task"])

                requester = McpStdioClient(db)
                reviewer = McpStdioClient(db)
                requester.initialize(client_name="codex-test-client")
                reviewer.initialize(client_name="claude-code")

                reviewer.call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"worker_path": "nilo mcp reviewer-worker"},
                    },
                )
                doctor_before = requester.call_tool("mcp_doctor", {"project_id": "project_test"})
                synthetic = [
                    row
                    for row in doctor_before["reviewers"]
                    if row["reviewer"] == "claude-code" and row["evidence_profile"] == "synthetic_result_file_worker"
                ][0]

                reviewer.call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"worker_path": "claude-code-mcp-session", "dispatch_capable": True},
                    },
                )
                doctor_after = requester.call_tool("mcp_doctor", {"project_id": "project_test"})
                real_session = [
                    row
                    for row in doctor_after["reviewers"]
                    if row["reviewer"] == "claude-code" and row["evidence_profile"] == "claude_code_mcp_session"
                ][0]

                context = requester.call_tool("get_agent_work_context", {"project_id": "project_test"})
                task_id = context["active_tasks"][0]["id"]
                nonce = "nonce-claude-e2e-123"
                requested = requester.call_tool(
                    "request_task_review",
                    {
                        "task_id": task_id,
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "reason": f"claude-code e2e {nonce}",
                        "context_token": context["write_context_token"],
                    },
                )
                claimed = reviewer.call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"})
                imported = reviewer.call_tool(
                    "import_review_result",
                    {
                        "task_id": task_id,
                        "review_id": claimed["review_id"],
                        "reviewer": "claude-code",
                        "last_seen_event_id": claimed["latest_event"]["event_id"],
                        "body_md": review_body(verdict="approved", summary=f"Claude Code nonce echo: {nonce}", findings=""),
                    },
                )
                review_status = requester.call_tool("get_review_status", {"task_id": task_id})
            finally:
                if requester is not None:
                    requester.close()
                if reviewer is not None:
                    reviewer.close()
                os.chdir(previous_cwd)

        self.assertFalse(synthetic["claude_code_e2e_capable"])
        self.assertEqual(synthetic["evidence_profile"], "synthetic_result_file_worker")
        self.assertTrue(real_session["claude_code_e2e_capable"])
        self.assertEqual(requested["result"]["review_request"]["status"], "requested")
        self.assertEqual(claimed["review_id"], requested["result"]["review_request"]["id"])
        self.assertIn(nonce, claimed["prompt_md"])
        self.assertEqual(imported["review_result"]["verdict"], "approved")
        self.assertIn(nonce, imported["review_result"]["summary"])
        self.assertEqual(review_status["review_requests"][0]["status"], "completed")
        self.assertIn(nonce, review_status["review_results"][0]["body_md"])

    def test_reviewer_worker_process_claims_and_imports_via_mcp_stdio(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            result_file = root / "review_result.md"
            result_file.write_text(
                review_body(verdict="approved", summary="Reviewer worker imported this result.", findings=""),
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            requester: McpStdioClient | None = None
            worker: subprocess.Popen[str] | None = None
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Worker handoff task"])

                env = os.environ.copy()
                src_path = str(Path(__file__).resolve().parents[1] / "src")
                env["PYTHONPATH"] = src_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src_path
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from nilo.cli import main; main()",
                        "--db",
                        str(db),
                        "mcp",
                        "reviewer-worker",
                        "--reviewer",
                        "claude-code",
                        "--project",
                        "project_test",
                        "--result-file",
                        str(result_file),
                        "--wait-seconds",
                        "5",
                        "--poll-interval",
                        "0.05",
                    ],
                    cwd=str(root),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                requester = McpStdioClient(db)
                requester.initialize()
                self.wait_for_reviewer(requester, "claude-code")
                context = requester.call_tool("get_agent_work_context", {"project_id": "project_test"})
                task_id = context["active_tasks"][0]["id"]
                requested = requester.call_tool(
                    "request_task_review",
                    {
                        "task_id": task_id,
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "reason": "worker process mcp handoff",
                        "context_token": context["write_context_token"],
                    },
                )
                stdout, stderr = worker.communicate(timeout=10)
                review_status = requester.call_tool("get_review_status", {"task_id": task_id})
            finally:
                if requester is not None:
                    requester.close()
                if worker is not None and worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
                os.chdir(previous_cwd)

        self.assertEqual(requested["result"]["review_request"]["status"], "requested")
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertIn("review_request:", stdout)
        self.assertIn("verdict: approved", stdout)
        self.assertEqual(review_status["review_requests"][0]["status"], "completed")
        self.assertEqual(review_status["review_results"][0]["reviewer"], "claude-code")

    def wait_for_reviewer(self, client: "McpStdioClient", reviewer: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            doctor = client.call_tool("mcp_doctor", {"project_id": "project_test"})
            reviewers = {row["reviewer"]: row for row in doctor["reviewers"]}
            row = reviewers.get(reviewer)
            if row and row["availability"] == "available" and row["dispatch_capable"]:
                return
            time.sleep(0.05)
        self.fail(f"reviewer did not become available: {reviewer}")

    def test_mcp_server_imports_without_cli_import_order_dependency(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "from nilo.mcp_server import call_tool; print(call_tool.__name__)"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("call_tool", result.stdout)

    def test_taskization_tool_descriptions_steer_explicit_task_requests_to_tasks(self) -> None:
        response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"context": "advanced"}})
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}

        todo_description = tools["create_todo"]["description"]
        task_description = tools["create_task"]["description"]
        from_todo_description = tools["create_task_from_todo"]["description"]

        self.assertIn("Do NOT use this when the user explicitly asks to taskize work", todo_description)
        self.assertIn("create a Task", todo_description)
        self.assertIn("タスク化して", todo_description)
        self.assertIn("Create an executable Nilo Task", task_description)
        self.assertIn("taskize work", task_description)
        self.assertIn("タスク化して", task_description)
        self.assertIn("commitment_id is optional", task_description)
        self.assertIn("project's primary language", task_description)
        self.assertNotIn("commitment_id", tools["create_task"]["inputSchema"]["required"])
        self.assertIn("project's primary language", todo_description)
        self.assertIn("Use create_task for new concrete work", from_todo_description)
        self.assertIn("converting an already-created Todo", from_todo_description)
        self.assertIn("primary language policy", from_todo_description)

    def test_ai_context_includes_taskization_vocabulary_rules(self) -> None:
        body = render_ai_context_text(
            {
                "project_id": "project_test",
                "project_name": "Project Test",
                "current_task": None,
                "next_required_actions": [],
                "failure_summary": {},
            }
        )

        self.assertIn("語彙ルール", body)
        self.assertIn("「これをタスク化して」「Taskにして」「作業タスクを作って」", body)
        self.assertIn("Todo ではなく Task 作成を優先する", body)
        self.assertIn("create_todo=受付だけ", body)
        self.assertIn("補完できないほど曖昧な場合だけ Todo", body)

        active_body = render_ai_context_text(
            {
                "project_id": "project_test",
                "project_name": "Project Test",
                "current_task": {
                    "task": {"id": "task_test", "title": "Active", "state": "planned"},
                    "git": {"git_head": "", "git_diff_hash": "", "dirty": False},
                    "evidence": {"status": "missing"},
                    "review": {"unresolved_count": 0},
                    "completion": {"allowed": False, "blocking_reasons": ["evidence_missing"]},
                },
                "next_required_actions": [],
                "failure_summary": {},
            }
        )
        self.assertIn("タスク化=Task 作成、Todo=受付だけ", active_body)
        self.assertIn("ロードマップ承認待ちの応答ルール", active_body)
        self.assertIn("作業計画の確認・承認・Task 化", active_body)

    def test_tools_list_exposes_default_ai_tools(self) -> None:
        response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        self.assertIsNotNone(response)
        tools = response["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(
            names,
            {
                "get_status",
                "get_task_status",
                "record_verification",
                "request_review",
                "import_review_result",
                "register_reviewer",
                "claim_next_review",
                "request_task_review",
                "dispatch_review",
                "get_review_prompt",
                "get_review_template",
                "get_review_status",
                "mark_stale_review_requests",
            },
        )
        self.assertGreater(response["result"]["advanced_tool_count"], 0)
        descriptions = {tool["name"]: tool["description"] for tool in tools}
        self.assertLessEqual(max(len(description) for description in descriptions.values()), 80)
        self.assertNotIn("complete_task", names)
        self.assertNotIn("close_roadmap", names)
        self.assertNotIn("close_roadmap_commitment", names)
        self.assertNotIn("commit_changes", names)
        self.assertIn("dispatch_review", names)
        tool_by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(
            tool_by_name["import_review_result"]["metadata"],
            {
                "tool": "nilo_import_review_result",
                "compressible": False,
                "reason": "primary evidence / write payload",
            },
        )
        self.assertIsNot(tool_by_name["import_review_result"]["metadata"], HEADROOM_TOOL_METADATA[0])
        self.assertEqual(
            tool_by_name["request_task_review"]["metadata"],
            {
                "api_level": "low_level",
                "recommended_for": "manual review handoff only",
                "prefer_for_ai_review": "dispatch_review",
            },
        )
        self.assertEqual(
            tool_by_name["dispatch_review"]["metadata"],
            {
                "api_level": "high_level",
                "recommended_for": "normal AI-to-AI review",
                "workflow": "mcp request, start, claim, run, import, confirm",
                "cli_fallback": "disabled unless allow_cli_fallback=true",
            },
        )

    def test_tools_list_exposes_review_handoff_tools_unconditionally(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {"context": "review_handoff"},
            }
        )

        self.assertIsNotNone(response)
        tools = response["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue(
            {
                "get_status",
                "get_task_status",
                "record_verification",
                "request_review",
                "import_review_result",
            }.issubset(names)
        )
        self.assertTrue(
            {
                "register_reviewer",
                "claim_next_review",
                "request_task_review",
                "dispatch_review",
                "get_review_prompt",
                "get_review_template",
                "get_review_status",
                "mark_stale_review_requests",
            }.issubset(names)
        )
        self.assertEqual(response["result"]["default_tool_count"], 13)
        self.assertEqual(response["result"]["review_handoff_tool_count"], 8)

    def test_get_agent_work_context_returns_next_step_and_write_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Context task"])

                result = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["project_id"], "project_test")
        self.assertEqual(result["next_step"]["action_id"], "continue_active_task")
        self.assertTrue(result["next_step"]["safe_for_ai"])
        self.assertFalse(result["next_step"]["requires_explicit_human_intent"])
        self.assertEqual(len(result["active_tasks"]), 1)
        self.assertEqual(result["write_context_token"], result["active_tasks"][0]["write_context_token"])
        self.assertTrue(result["write_context_token"].startswith("task:"))
        self.assertIn("complete_task", result["human_gates"])

    def test_default_status_tools_expose_verification_context_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "Context token task"])

                status = call_tool("get_status", {"project_id": "project_test"}, db)
                task_status = call_tool("get_task_status", {"task_id": "task_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(status["compact"])
        self.assertEqual(status["active_task"]["id"], "task_test")
        self.assertIn("detail_commands", status)
        self.assertTrue(status["write_context_token"].startswith("task:task_test:"))
        self.assertEqual(status["write_context_token"], task_status["write_context_token"])
        self.assertEqual(status["latest_task_status_event_id"], task_status["latest_task_status_event_id"])
        self.assertTrue(task_status["write_context_token"].startswith("task:task_test:"))
        self.assertEqual(task_status["latest_task_status_event_id"], task_status["write_context_token"].split(":")[-1])

    def test_get_status_reports_missing_project_as_mcp_error(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with self.assertRaises(McpToolError) as raised:
                call_tool("get_status", {"project_id": "missing_project"}, db)

        self.assertIn("project not found: missing_project", str(raised.exception))

    def test_get_next_step_marks_behavior_changing_completion_as_human_gated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Passed task"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    created_at = now_iso()
                    store.insert(
                        "agent_reports",
                        {
                            "id": "report_passed",
                            "task_id": task_id,
                            "agent": "codex",
                            "claimed_status": "completed",
                            "changed_files": ["src/nilo/mcp_server.py"],
                            "body_md": report_body(["src/nilo/mcp_server.py"]),
                            "created_at": created_at,
                        },
                    )
                    store.insert(
                        "evidence_checks",
                        {
                            "id": "evidence_passed",
                            "task_id": task_id,
                            "report_id": "report_passed",
                            "status": "passed",
                            "issues": [],
                            "metadata": {},
                            "created_at": created_at,
                        },
                    )
                    store.insert(
                        "verification_runs",
                        {
                            "id": "verification_passed",
                            "task_id": task_id,
                            "evidence_check_id": "evidence_passed",
                            "source": "nilo_executed",
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "ok\n",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                            "timeout_seconds": 0.0,
                            "git_head": "",
                            "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                            "started_at": created_at,
                            "finished_at": created_at,
                            "created_at": created_at,
                        },
                    )
                finally:
                    store.close()

                result = call_tool("get_next_step", {"project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["next_step"]["task_status"], "verification_passed")
        self.assertFalse(result["next_step"]["safe_for_ai"])
        self.assertTrue(result["next_step"]["requires_explicit_human_intent"])

    def test_mcp_context_explains_stale_review_reviewer_heartbeat(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "MCP stale reviewer heartbeat"])
                store = Store(db)
                try:
                    store.insert(
                        "review_reviewers",
                        {
                            "id": "reviewer_stale_mcp_context",
                            "reviewer": "claude-code",
                            "status": "available",
                            "capabilities": ["review"],
                            "max_concurrent": 1,
                            "metadata": {"worker_path": "nilo mcp reviewer-claim"},
                            "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                            "created_at": "2000-01-01T00:00:00+00:00",
                            "updated_at": "2000-01-01T00:00:00+00:00",
                        },
                    )
                    store.insert(
                        "review_requests",
                        {
                            "id": "review_stale_mcp_context",
                            "task_id": "task_test",
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "status": "stale",
                            "reason": "stale reviewer heartbeat",
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                        },
                    )
                finally:
                    store.close()

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                next_step = call_tool("get_next_step", {"project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertIn("claude-code reviewer heartbeat is stale", context["next_actions"][0])
        self.assertIn("nilo mcp reviewer-claim", context["next_actions"][0])
        self.assertIn("claude-code reviewer heartbeat is stale", next_step["next_step"]["command_hint"])
        self.assertIn("import_review_result", next_step["next_step"]["command_hint"])

    def test_mcp_doctor_reports_safe_tool_surface_and_project_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])
                    main(["--db", str(db), "mcp", "reviewer-claim", "--reviewer", "codex", "--project", "project_test"])

                tool_result = call_tool("mcp_doctor", {"project_id": "project_test"}, db)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "mcp", "doctor", "--project", "project_test"])
            finally:
                os.chdir(previous_cwd)

        cli_result = json.loads(output.getvalue())
        self.assertTrue(tool_result["ok"])
        self.assertIn("identity", tool_result)
        self.assertIn("cwd", tool_result["identity"])
        self.assertIn("git_root", tool_result["identity"])
        self.assertIn("db_path", tool_result["identity"])
        self.assertIn("project_id", tool_result["identity"])
        self.assertIn("repository_name", tool_result["identity"])
        self.assertTrue(tool_result["expected_safe_tools_present"])
        self.assertEqual(tool_result["exposed_human_gated_tools"], [])
        self.assertEqual(cli_result["project_id"], "project_test")
        self.assertEqual(cli_result["db_path"], str(db.resolve()))
        self.assertIn("identity", cli_result)
        self.assertEqual(cli_result["identity"]["db_path"], str(db.resolve()))
        self.assertEqual(tool_result["claude_code_reviewer"]["reason"], "heartbeat_only")
        self.assertFalse(tool_result["claude_code_reviewer"]["ready"])
        reviewers = {row["reviewer"]: row for row in tool_result["reviewers"]}
        self.assertFalse(reviewers["claude-code"]["dispatch_capable"])
        self.assertEqual(reviewers["claude-code"]["availability"], "heartbeat_only")
        self.assertEqual(reviewers["claude-code"]["evidence_profile"], "heartbeat_only")
        self.assertFalse(reviewers["claude-code"]["claude_code_e2e_capable"])
        self.assertTrue(reviewers["codex"]["dispatch_capable"])
        self.assertEqual(reviewers["codex"]["availability"], "available")
        self.assertEqual(reviewers["codex"]["evidence_profile"], "manual_claim_worker")

    def test_get_project_status_returns_summary_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

                result = call_tool("get_project_status", {"project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["project_id"], "project_test")
        self.assertEqual(result["project_name"], "Nilo")
        self.assertEqual(result["active_tasks"], [])
        self.assertIn("next_actions", result)

    def test_project_summary_does_not_mask_internal_value_error_as_not_found(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                with patch("nilo.mcp_server.build_project_status", side_effect=ValueError("bad snapshot data")):
                    with self.assertRaisesRegex(ValueError, "bad snapshot data"):
                        project_summary(store, "project_test")
            finally:
                store.close()

    def test_mcp_ping_response_includes_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                result = call_tool("mcp_ping", {}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(result["ok"])
        self.assertEqual(result["server"]["name"], "nilo")
        self.assertIn("identity", result)
        self.assertEqual(result["identity"]["db_path"], str(db.resolve()))

    def test_mcp_expected_project_mismatch_allows_read_only_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                result = call_tool("get_status", {"project_id": "project_test", "expected_project": "Chiffon"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["project_id"], "project_test")
        self.assertIn("next_action", result)
        self.assertEqual(result["identity_mismatch"]["error"], "repository_mismatch")
        self.assertEqual(result["identity_mismatch"]["mode"], "read_only_external_reference")
        self.assertIn("actual", result["identity_mismatch"])

    def test_mcp_expected_project_mismatch_blocks_write_tool(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                result = call_tool(
                    "create_todo",
                    {
                        "project_id": "project_test",
                        "title": "outside write",
                        "kind": "ad_hoc",
                        "expected_project": "Chiffon",
                    },
                    db,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "repository_mismatch")
        self.assertEqual(result["expected"]["project"], "Chiffon")
        self.assertEqual(result["fallback"], "CLI fallback")
        self.assertEqual(result["fallback_commands"], ["nilo status --ai", "nilo next"])
        self.assertIn("actual", result)
        self.assertNotIn("project", result)
        self.assertNotIn("tasks", result)

    def test_mcp_write_fence_does_not_record_failure_to_external_db(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as external_directory:
                external_db = Path(external_directory) / "nilo.db"
                previous_cwd = Path.cwd()
                try:
                    root.mkdir()
                    os.chdir(root)
                    with redirect_stdout(io.StringIO()):
                        main(["--db", str(external_db), "project", "create", "Nilo", "--id", "project_test"])
                        main(["--db", str(external_db), "task", "create", "--project", "project_test", "--title", "External DB write"])
                    store = Store(external_db)
                    try:
                        task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    finally:
                        store.close()

                    result = call_tool(
                        "record_verification_run",
                        {
                            "task_id": task["id"],
                            "last_seen_event_id": "task:start",
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                        },
                        external_db,
                    )
                finally:
                    os.chdir(previous_cwd)

                store = Store(external_db)
                try:
                    failures = store.list_where("failure_logs", "task_id=?", (task["id"],))
                finally:
                    store.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "write_fence_violation")
        self.assertIn(str(external_db.resolve()), result["write_fence"]["outside_write_targets"])
        self.assertEqual(failures, [])

    def test_todo_mcp_tools_create_triage_start_and_promote_with_status_guards(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test", "--rule", "primary_language: en"])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": "project_test",
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["todo MCP can create work"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "accepted",
                            "accepted_by": "human",
                            "accepted_at": "2026-06-20T00:00:00+09:00",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_pending",
                            "project_id": "project_test",
                            "title": "Pending Commitment",
                            "intent": "test",
                            "success_criteria": ["pending commitment must not start todo work"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "pending",
                            "accepted_by": "",
                            "accepted_at": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                finally:
                    store.close()

                created = call_tool(
                    "create_todo",
                    {
                        "project_id": "project_test",
                        "title": "MCP ready work",
                        "kind": "follow_up",
                        "description": "Create a task from this.",
                        "acceptance_hint": "Task is created.",
                    },
                    db,
                )
                todo_id = created["todo"]["id"]
                self.assertEqual(created["todo"]["status"], "open")
                with self.assertRaises(McpToolError) as not_ready:
                    call_tool("create_task_from_todo", {"todo_id": todo_id, "type": "implementation", "risk": "medium"}, db)
                self.assertIn("todo is not startable: open", str(not_ready.exception))

                with self.assertRaises(McpToolError) as terminal_status:
                    call_tool(
                        "triage_todo",
                        {
                            "todo_id": todo_id,
                            "status": "converted_to_task",
                            "reason": "terminal status requires start side effects",
                            "context_token": created["context_token"],
                        },
                        db,
                    )
                self.assertIn("todo status is not triage-settable: converted_to_task", str(terminal_status.exception))

                triaged = call_tool(
                    "triage_todo",
                    {
                        "todo_id": todo_id,
                        "status": "ready",
                        "reason": "単発依頼として実行対象にする",
                        "commitment_id": "commitment_pending",
                        "context_token": created["context_token"],
                    },
                    db,
                )
                self.assertEqual(triaged["todo"]["status"], "ready")
                started = call_tool(
                    "create_task_from_todo",
                    {
                        "todo_id": todo_id,
                        "type": "documentation",
                        "risk": "low",
                        "context_token": triaged["context_token"],
                    },
                    db,
                )
                self.assertEqual(started["todo"]["status"], "converted_to_task")
                self.assertEqual(started["task"]["description"], "Create a task from this.")
                self.assertEqual(started["task"]["acceptance_criteria"], ["Task is created."])
                self.assertEqual(started["task"]["roadmap_commitment_id"], "commitment_pending")

                pending_ready = call_tool(
                    "create_todo",
                    {
                        "project_id": "project_test",
                        "title": "Pending commitment work",
                        "kind": "follow_up",
                    },
                    db,
                )
                store = Store(db)
                try:
                    store.update(
                        "todos",
                        pending_ready["todo"]["id"],
                        {"status": "ready", "roadmap_commitment_id": "commitment_pending"},
                    )
                finally:
                    store.close()
                pending_started = call_tool(
                    "create_task_from_todo",
                    {
                        "todo_id": pending_ready["todo"]["id"],
                        "type": "implementation",
                        "risk": "medium",
                        "context_token": f"todo:{pending_ready['todo']['id']}:ready",
                    },
                    db,
                )
                self.assertEqual(pending_started["todo"]["status"], "converted_to_task")
                self.assertEqual(pending_started["task"]["roadmap_commitment_id"], "commitment_pending")

                roadmap_todo = call_tool(
                    "create_todo",
                    {
                        "project_id": "project_test",
                        "title": "Needs roadmap",
                        "kind": "roadmap_candidate",
                        "description": "Needs policy.",
                        "acceptance_hint": "Proposal exists.",
                    },
                    db,
                )
                with self.assertRaises(McpToolError) as not_promotable:
                    call_tool(
                        "promote_todo_to_roadmap_proposal",
                        {"todo_id": roadmap_todo["todo"]["id"], "reason": "needs roadmap"},
                        db,
                    )
                self.assertIn("todo is not promotable: open", str(not_promotable.exception))
                requires_roadmap = call_tool(
                    "triage_todo",
                    {
                        "todo_id": roadmap_todo["todo"]["id"],
                        "status": "requires_roadmap",
                        "reason": "複数 task と成功条件定義が必要",
                        "context_token": roadmap_todo["context_token"],
                    },
                    db,
                )
                promoted = call_tool(
                    "promote_todo_to_roadmap_proposal",
                    {
                        "todo_id": roadmap_todo["todo"]["id"],
                        "reason": "needs accepted roadmap scope",
                        "context_token": requires_roadmap["context_token"],
                    },
                    db,
                )
                self.assertEqual(promoted["todo"]["status"], "superseded")
                self.assertEqual(promoted["roadmap_revision"]["status"], "pending")
                self.assertEqual(promoted["roadmap_revision"]["source_path"], f"todo:{roadmap_todo['todo']['id']}")

                listed = call_tool("list_todos", {"project_id": "project_test", "status": "converted_to_task"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual({todo["id"] for todo in listed["todos"]}, {todo_id, pending_ready["todo"]["id"]})

    def test_get_roadmap_status_returns_human_pending_plan_message(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# MCP Pending Plan

## Intent
MCP でも承認待ちの計画を人間向けに返す。

## Success Criteria
- 作業計画が返る
- 承認後に Task 化することが分かる
""",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            result = call_tool("get_roadmap_status", {"project_id": "project_test"}, db)

        self.assertIn("pending_roadmap_review_messages", result)
        self.assertEqual(len(result["pending_roadmap_review_messages"]), 1)
        message = result["pending_roadmap_review_messages"][0]
        self.assertIn("作業計画", message)
        self.assertIn("確認", message)
        self.assertIn("承認", message)
        self.assertIn("Task 化", message)
        self.assertIn("MCP Pending Plan", message)

    def test_get_task_status_and_instruction_are_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Read-only task"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    store.insert(
                        "instructions",
                        {
                            "id": "instruction_test",
                            "task_id": task_id,
                            "applied_rule_ids": [],
                            "degradation_mode": "normal",
                            "body_md": "# Existing instruction",
                            "report_format_md": "# Report",
                            "created_at": now_iso(),
                        },
                    )
                finally:
                    store.close()

                instruction_result = call_tool("get_instruction", {"task_id": task_id}, db)
                status_result = call_tool("get_task_status", {"task_id": task_id}, db)
                store = Store(db)
                try:
                    task_after = store.get("tasks", task_id)
                    instructions_after = store.list_where("instructions", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(instruction_result["instruction_exists"])
        self.assertEqual(instruction_result["instruction"]["id"], "instruction_test")
        self.assertEqual(task_after["base_commit"], task["base_commit"])
        self.assertEqual(len(instructions_after), 1)
        self.assertEqual(status_result["latest"]["instructions"]["id"], "instruction_test")

    def test_tools_call_unknown_tool_returns_tool_error_result(self) -> None:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "complete_task", "arguments": {}},
            }
        )

        self.assertIsNotNone(response)
        result = response["result"]
        self.assertTrue(result["isError"])
        body = json.loads(result["content"][0]["text"])
        self.assertEqual(body["error"], "unknown tool: complete_task")

    def test_import_agent_report_writes_through_existing_guard(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Report task"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    last_seen_event_id = store.latest_task_status_event(task_id)["event_id"]
                finally:
                    store.close()

                result = call_tool(
                    "import_agent_report",
                    {
                        "task_id": task_id,
                        "agent": "codex",
                        "last_seen_event_id": last_seen_event_id,
                        "body_md": report_body(["src/nilo/mcp_server.py"]),
                    },
                    db,
                )
                store = Store(db)
                try:
                    reports = store.list_where("agent_reports", "task_id=?", (task_id,))
                    checks = store.list_where("evidence_checks", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["report"]["agent"], "codex")
        self.assertEqual(result["evidence_status"]["report_id"], result["report"]["id"])
        self.assertEqual(result["previous_event"]["event_id"], last_seen_event_id)
        self.assertEqual(result["latest_event"]["event_id"], result["report"]["id"])
        self.assertEqual(len(reports), 1)
        self.assertEqual(checks, [])

    def test_import_agent_report_accepts_context_token_and_rejects_stale_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Token task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                context_token = context["active_tasks"][0]["write_context_token"]
                result = call_tool(
                    "import_agent_report",
                    {
                        "task_id": task_id,
                        "agent": "codex",
                        "context_token": context_token,
                        "body_md": report_body(["src/nilo/mcp_server.py"]),
                    },
                    db,
                )
                with self.assertRaises(McpToolError):
                    call_tool(
                        "record_verification_run",
                        {
                            "task_id": task_id,
                            "context_token": context_token,
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "ok\n",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                        },
                        db,
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["previous_event"]["event_id"], context_token.rsplit(":", 1)[1])

    def test_workflow_wrappers_return_refreshed_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Wrapper task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                report_result = call_tool(
                    "submit_agent_report",
                    {
                        "task_id": task_id,
                        "agent": "codex",
                        "context_token": context["write_context_token"],
                        "body_md": report_body(["src/nilo/mcp_server.py"]),
                    },
                    db,
                )
                report_token = report_result["refreshed_context"]["task_context"]["write_context_token"]
                verification_result = call_tool(
                    "record_test_result",
                    {
                        "task_id": task_id,
                        "context_token": report_token,
                        "command": "python -m unittest tests.test_mcp_server",
                        "cwd": str(root),
                        "stdout": "ok\n",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "mode": "quick",
                        "git_head": "agent-head",
                        "git_diff_hash": "agent-diff",
                        "working_tree_dirty": False,
                        "git_status_porcelain": "",
                        "observed_paths": ["src/agent.py"],
                        "metadata": {"working_tree_available": False},
                    },
                    db,
                )
                verification_token = verification_result["refreshed_context"]["task_context"]["write_context_token"]
                call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"transport": "mcp"},
                    },
                    db,
                )
                review_result = call_tool(
                    "request_task_review",
                    {
                        "task_id": task_id,
                        "requester": "codex",
                        "reviewer": "claude",
                        "reason": "wrapper review",
                        "context_token": verification_token,
                    },
                    db,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(report_result["operation"], "submit_agent_report")
        self.assertEqual(report_result["result"]["report"]["agent"], "codex")
        self.assertEqual(report_result["result"]["evidence_status"]["status"], "failed")
        self.assertEqual(report_result["refreshed_context"]["task_context"]["status"], "needs_human_review")
        self.assertEqual(verification_result["operation"], "record_test_result")
        self.assertEqual(verification_result["result"]["verification_run"]["source"], "agent_reported")
        self.assertEqual(review_result["operation"], "request_task_review")
        self.assertEqual(review_result["result"]["review_request"]["reviewer"], "claude-code")
        self.assertEqual(review_result["result"]["review_request"]["status"], "requested")
        self.assertEqual(review_result["refreshed_context"]["task_context"]["status"], "review_requested")

    def test_workflow_wrapper_rejects_stale_context_token_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Stale wrapper task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                call_tool(
                    "submit_agent_report",
                    {
                        "task_id": task_id,
                        "agent": "codex",
                        "context_token": context["write_context_token"],
                        "body_md": report_body(["src/nilo/mcp_server.py"]),
                    },
                    db,
                )
                with self.assertRaises(McpToolError):
                    call_tool(
                        "request_task_review",
                        {
                            "task_id": task_id,
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "reason": "stale wrapper review",
                            "context_token": context["write_context_token"],
                        },
                        db,
                    )
                store = Store(db)
                try:
                    reviews = store.list_where("review_requests", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(reviews, [])

    def test_request_task_review_allows_stale_claude_code_then_register_revives_and_claims(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Stale reviewer task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                store = Store(db)
                try:
                    store.insert(
                        "review_reviewers",
                        {
                            "id": "reviewer_stale",
                            "reviewer": "claude-code",
                            "status": "available",
                            "capabilities": ["review"],
                            "max_concurrent": 1,
                            "metadata": {"transport": "mcp"},
                            "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                            "created_at": "2000-01-01T00:00:00+00:00",
                            "updated_at": "2000-01-01T00:00:00+00:00",
                        },
                    )
                finally:
                    store.close()
                requested = call_tool(
                    "request_task_review",
                    {
                        "task_id": task_id,
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "reason": "stale reviewer review",
                        "context_token": context["write_context_token"],
                    },
                    db,
                )
                review_id = requested["result"]["review_request"]["id"]
                store = Store(db)
                try:
                    queued = store.get("review_requests", review_id)
                finally:
                    store.close()
                registered = call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {
                            "worker_path": "claude-code-mcp-session",
                            "dispatch_capable": True,
                            "source": "real Claude Code session",
                        },
                    },
                    db,
                )
                claim = call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(requested["operation"], "request_task_review")
        self.assertEqual(requested["result"]["review_request"]["reviewer"], "claude-code")
        self.assertEqual(requested["result"]["review_request"]["status"], "reviewer_unavailable")
        self.assertEqual(queued["status"], "reviewer_unavailable")
        self.assertEqual(requested["reviewer_availability"], "stale")
        self.assertTrue(requested["reviewer_dispatch_capable"])
        self.assertIn("claude-code reviewer is stale.", requested["next_action"])
        self.assertIn("register_reviewer", requested["next_action"])
        self.assertIn("claim_next_review", requested["next_action"])
        self.assertIn('reviewer="claude-code"', requested["claude_code_prompt"])
        self.assertIn('project_id="project_test"', requested["claude_code_prompt"])
        self.assertIn('"worker_path": "claude-code-mcp-session"', requested["claude_code_prompt"])
        self.assertIn('"dispatch_capable": true', requested["claude_code_prompt"])
        self.assertIn('"source": "real Claude Code session"', requested["claude_code_prompt"])
        self.assertEqual(registered["revived_review_requests"], [review_id])
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["review_id"], review_id)

    def test_request_task_review_queues_heartbeat_only_without_treating_available(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "CLI fake reviewer task"])
                    main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                requested = call_tool(
                    "request_task_review",
                    {
                        "task_id": task_id,
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "reason": "must require real worker",
                        "context_token": context["write_context_token"],
                    },
                    db,
                )
                with self.assertRaises(McpToolError) as raised:
                    call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(requested["result"]["review_request"]["status"], "reviewer_unavailable")
        self.assertEqual(requested["reviewer_availability"], "heartbeat_only")
        self.assertFalse(requested["reviewer_dispatch_capable"])
        self.assertIn("reviewer is not registered or available: claude-code", str(raised.exception))

    def test_request_task_review_rejects_unknown_reviewer_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Unknown reviewer task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                with self.assertRaises(McpToolError) as raised:
                    call_tool(
                        "request_task_review",
                        {
                            "task_id": task_id,
                            "requester": "codex",
                            "reviewer": "claude-cdoe",
                            "reason": "typo reviewer",
                            "context_token": context["write_context_token"],
                        },
                        db,
                    )
                store = Store(db)
                try:
                    reviews = store.list_where("review_requests", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(reviews, [])
        self.assertIn("reviewer is not registered or supported: claude-cdoe", str(raised.exception))

    def test_prepare_reviewer_reports_ready_false_and_true(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])

                heartbeat_only = call_tool(
                    "prepare_reviewer",
                    {"project_id": "project_test", "reviewer": "claude-code"},
                    db,
                )
                call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {
                            "worker_path": "claude-code-mcp-session",
                            "dispatch_capable": True,
                            "source": "real Claude Code session",
                        },
                    },
                    db,
                )
                ready = call_tool(
                    "prepare_reviewer",
                    {"project_id": "project_test", "reviewer": "claude-code"},
                    db,
                )
                codex_missing = call_tool(
                    "prepare_reviewer",
                    {"project_id": "project_test", "reviewer": "codex"},
                    db,
                )
                call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "codex",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {
                            "worker_path": "codex-mcp-session",
                            "dispatch_capable": True,
                            "source": "real Codex session",
                        },
                    },
                    db,
                )
                codex_ready = call_tool(
                    "prepare_reviewer",
                    {"project_id": "project_test", "reviewer": "codex"},
                    db,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertFalse(heartbeat_only["ready"])
        self.assertEqual(heartbeat_only["reason"], "heartbeat_only")
        self.assertFalse(heartbeat_only["dispatch_capable"])
        self.assertIn("register_reviewer", heartbeat_only["claude_code_prompt"])
        self.assertEqual(heartbeat_only["register_reviewer_json"]["metadata"]["dispatch_capable"], True)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["reason"], "available")
        self.assertTrue(ready["dispatch_capable"])
        self.assertFalse(codex_missing["ready"])
        self.assertEqual(codex_missing["reason"], "mcp_server_not_connected")
        self.assertIn("Open the Codex session", codex_missing["next_action"])
        self.assertEqual(codex_missing["register_reviewer_json"]["metadata"]["worker_path"], "codex-mcp-session")
        self.assertTrue(codex_ready["ready"])
        self.assertEqual(codex_ready["evidence_profile"], "codex_mcp_session")

    def test_register_reviewer_canonicalizes_alias_and_revives_unavailable_request(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Alias reviewer task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                requested = call_tool(
                    "request_review",
                    {
                        "task_id": task_id,
                        "from_actor": "codex",
                        "to_actor": "claude",
                        "reason": "alias reviewer review",
                        "context_token": context["write_context_token"],
                        "allow_unavailable": True,
                    },
                    db,
                )
                registered = call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"transport": "mcp"},
                    },
                    db,
                )
                claim = call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        review_id = requested["review_request"]["id"]
        self.assertEqual(requested["review_request"]["reviewer"], "claude-code")
        self.assertEqual(requested["review_request"]["status"], "reviewer_unavailable")
        self.assertEqual(registered["reviewer"]["reviewer"], "claude-code")
        self.assertEqual(registered["revived_review_requests"], [review_id])
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["review_id"], review_id)

    def test_claim_next_review_accepts_reviewer_alias(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Alias claim task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                requested = call_tool(
                    "request_review",
                    {
                        "task_id": task_id,
                        "from_actor": "codex",
                        "to_actor": "claude",
                        "reason": "alias claim review",
                        "context_token": context["write_context_token"],
                        "allow_unavailable": True,
                    },
                    db,
                )
                call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"transport": "mcp"},
                    },
                    db,
                )
                claim = call_tool("claim_next_review", {"reviewer": "claude", "project_id": "project_test"}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(requested["review_request"]["reviewer"], "claude-code")
        self.assertTrue(claim["claimed"])
        self.assertEqual(claim["reviewer"], "claude-code")
        self.assertEqual(claim["review_id"], requested["review_request"]["id"])

    def test_mcp_review_request_register_claim_stale_and_import_flow(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Dispatch review"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                requested = call_tool(
                    "request_review",
                    {
                        "task_id": task_id,
                        "from_actor": "codex",
                        "to_actor": "claude-code",
                        "reason": "dispatch review",
                        "context_token": context["write_context_token"],
                        "allow_unavailable": True,
                    },
                    db,
                )
                registered = call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"transport": "mcp"},
                    },
                    db,
                )
                claim = call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
                review_id = claim["review_id"]
                store = Store(db)
                try:
                    store.update("review_requests", review_id, {"updated_at": "2000-01-01T00:00:00+00:00"})
                finally:
                    store.close()
                stale = call_tool("mark_stale_review_requests", {"reviewer": "claude-code", "stale_after_seconds": 1}, db)
                stale_status = call_tool("get_project_status", {"project_id": "project_test"}, db)
                reclaim = call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
                imported = call_tool(
                    "import_review_result",
                    {
                        "task_id": task_id,
                        "review_id": review_id,
                        "reviewer": "claude-code",
                        "last_seen_event_id": reclaim["latest_event"]["event_id"],
                        "body_md": review_body(verdict="approved", summary="Looks good.", findings=""),
                    },
                    db,
                )
                store = Store(db)
                try:
                    request = store.get("review_requests", review_id)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(requested["review_request"]["status"], "reviewer_unavailable")
        self.assertEqual(registered["revived_review_requests"], [requested["review_request"]["id"]])
        self.assertTrue(claim["claimed"])
        self.assertIn("# Review Request", claim["prompt_md"])
        self.assertIn("# ReviewResult", claim["template_md"])
        self.assertEqual(stale["stale_review_requests"], [review_id])
        self.assertEqual(stale_status["active_tasks"][0]["status"], "review_stale")
        self.assertIn("retry stale review", stale_status["next_actions"][0])
        self.assertTrue(reclaim["claimed"])
        self.assertEqual(imported["review_result"]["verdict"], "approved")
        self.assertEqual(request["status"], "completed")

    def test_import_review_result_requires_claimed_matching_reviewer(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Strict import task"])

                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]
                requested = call_tool(
                    "request_review",
                    {
                        "task_id": task_id,
                        "from_actor": "codex",
                        "to_actor": "claude-code",
                        "reason": "strict import",
                        "context_token": context["write_context_token"],
                        "allow_unavailable": True,
                    },
                    db,
                )
                review_id = requested["review_request"]["id"]
                with self.assertRaises(McpToolError) as unclaimed:
                    call_tool(
                        "import_review_result",
                        {
                            "task_id": task_id,
                            "review_id": review_id,
                            "reviewer": "claude-code",
                            "last_seen_event_id": requested["latest_event"]["event_id"],
                            "body_md": review_body(),
                        },
                        db,
                    )
                call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"transport": "mcp"},
                    },
                    db,
                )
                claim = call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
                with self.assertRaises(McpToolError) as wrong_reviewer:
                    call_tool(
                        "import_review_result",
                        {
                            "task_id": task_id,
                            "review_id": review_id,
                            "reviewer": "codex",
                            "last_seen_event_id": claim["latest_event"]["event_id"],
                            "body_md": review_body(),
                        },
                        db,
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertIn("review request must be claimed or in_progress before import", str(unclaimed.exception))
        self.assertIn("reviewer mismatch", str(wrong_reviewer.exception))

    def test_import_agent_report_rejects_stale_event_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Stale task"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                finally:
                    store.close()

                with self.assertRaises(McpToolError):
                    call_tool(
                        "import_agent_report",
                        {
                            "task_id": task_id,
                            "agent": "codex",
                            "last_seen_event_id": "old_event",
                            "body_md": report_body(["src/nilo/mcp_server.py"]),
                        },
                        db,
                    )
                store = Store(db)
                try:
                    reports = store.list_where("agent_reports", "task_id=?", (task_id,))
                    checks = store.list_where("evidence_checks", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(reports, [])
        self.assertEqual(checks, [])

    def test_create_task_does_not_require_accepted_commitment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test", "--rule", "primary_language: en"])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_pending",
                            "project_id": "project_test",
                            "title": "Pending",
                            "intent": "",
                            "success_criteria": [],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "pending",
                            "accepted_by": "",
                            "accepted_at": "",
                            "created_at": now_iso(),
                        },
                    )
                finally:
                    store.close()

                result = call_tool(
                    "create_task",
                    {
                        "project_id": "project_test",
                        "title": "Reference note task",
                        "type": "implementation",
                        "risk": "medium",
                        "commitment_id": "commitment_pending",
                        "description": "pending commitment is only a reference note",
                        "acceptance": ["task creation is driven by a concrete request"],
                    },
                    db,
                )
                self.assertEqual(result["task"]["roadmap_commitment_id"], "commitment_pending")
            finally:
                os.chdir(previous_cwd)

    def test_mcp_create_task_rejects_primary_language_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "日本語プロジェクト", "--id", "project_test", "--rule", "primary_language: ja"])

                with self.assertRaises(McpToolError) as raised:
                    call_tool(
                        "create_task",
                        {
                            "project_id": "project_test",
                            "title": "Fix settings layout",
                            "type": "implementation",
                            "risk": "medium",
                            "description": "Fix the settings screen layout.",
                            "acceptance": ["Settings screen is readable."],
                        },
                        db,
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertIn("human-readable field language mismatch", str(raised.exception))
        self.assertIn("primary_language=ja", str(raised.exception))

    def test_mcp_create_task_allows_technical_token_only_title(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "日本語プロジェクト", "--id", "project_test", "--rule", "primary_language: ja"])

                result = call_tool(
                    "create_task",
                    {
                        "project_id": "project_test",
                        "title": "`nilo view`",
                        "type": "implementation",
                        "risk": "medium",
                        "description": "表示結果を確認する。",
                        "acceptance": ["`nilo view` の結果が確認できる。"],
                    },
                    db,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["task"]["title"], "`nilo view`")

    def test_create_task_records_accepted_commitment_link(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test", "--rule", "primary_language: en"])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_accepted",
                            "project_id": "project_test",
                            "title": "Accepted",
                            "intent": "",
                            "success_criteria": [],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "accepted",
                            "accepted_by": "ai",
                            "accepted_at": now_iso(),
                            "created_at": now_iso(),
                        },
                    )
                finally:
                    store.close()

                result = call_tool(
                    "create_task",
                    {
                        "project_id": "project_test",
                        "title": "MCP task",
                        "type": "implementation",
                        "risk": "medium",
                        "commitment_id": "commitment_accepted",
                        "description": "created through MCP",
                        "acceptance": ["task is linked to commitment"],
                        "roadmap_item_id": "roadmap_item_1",
                    },
                    db,
                )
                store = Store(db)
                try:
                    task = store.get("tasks", result["task"]["id"])
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(task["roadmap_commitment_id"], "commitment_accepted")
        self.assertEqual(task["roadmap_item_id"], "roadmap_item_1")
        self.assertEqual(result["latest_event"]["event_id"], task["id"])

    def test_record_verification_run_saves_agent_reported_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Verify from MCP"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    last_seen_event_id = store.latest_task_status_event(task_id)["event_id"]
                finally:
                    store.close()

                result = call_tool(
                    "record_verification_run",
                    {
                        "task_id": task_id,
                        "last_seen_event_id": last_seen_event_id,
                        "command": "python -m unittest tests.test_mcp_server",
                        "cwd": str(root),
                        "stdout": "ok\n",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "mode": "quick",
                        "git_head": "agent-head",
                        "git_diff_hash": "agent-diff",
                        "working_tree_dirty": False,
                        "git_status_porcelain": "",
                        "observed_paths": ["src/agent.py"],
                        "metadata": {"working_tree_available": False},
                    },
                    db,
                )
                store = Store(db)
                try:
                    run = store.latest_for_task("verification_runs", task_id)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(run["source"], "agent_reported")
        self.assertEqual(run["git_head"], "agent-head")
        self.assertEqual(run["git_diff_hash"], "agent-diff")
        self.assertFalse(run["working_tree_dirty"])
        self.assertEqual(run["observed_paths"], ["src/agent.py"])
        self.assertEqual(run["metadata"]["verification_mode"], "quick")
        self.assertEqual(result["verification_run"]["source"], "agent_reported")
        self.assertEqual(result["previous_event"]["event_id"], last_seen_event_id)
        self.assertEqual(result["latest_event"]["event_id"], run["id"])

    def test_record_verification_run_rejects_invalid_mode(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            try:
                previous_cwd = Path.cwd()
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Invalid mode"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    last_seen_event_id = store.latest_task_status_event(task_id)["event_id"]
                finally:
                    store.close()

                with self.assertRaises(McpToolError):
                    call_tool(
                        "record_verification_run",
                        {
                            "task_id": task_id,
                            "last_seen_event_id": last_seen_event_id,
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                            "mode": "smoke",
                        },
                        db,
                    )
            finally:
                os.chdir(previous_cwd)

    def test_record_verification_run_rejects_stale_event_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Stale verification"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                finally:
                    store.close()

                with self.assertRaises(McpToolError):
                    call_tool(
                        "record_verification_run",
                        {
                            "task_id": task_id,
                            "last_seen_event_id": "old_event",
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "ok\n",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                        },
                        db,
                    )
                store = Store(db)
                try:
                    runs = store.list_where("verification_runs", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(runs, [])

    def test_mcp_review_workflow_round_trips_request_prompt_result_and_finding_update(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Review through MCP"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    last_seen_event_id = store.latest_task_status_event(task_id)["event_id"]
                finally:
                    store.close()

                call_tool(
                    "register_reviewer",
                    {
                        "reviewer": "claude-code",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"transport": "mcp"},
                    },
                    db,
                )
                request_result = call_tool(
                    "request_review",
                    {
                        "task_id": task_id,
                        "from_actor": "codex",
                        "to_actor": "claude-code",
                        "reason": "MCP review",
                        "last_seen_event_id": last_seen_event_id,
                    },
                    db,
                )
                review_id = request_result["review_request"]["id"]
                prompt_result = call_tool("get_review_prompt", {"task_id": task_id, "review_id": review_id}, db)
                template_result = call_tool("get_review_template", {"review_id": review_id}, db)
                claim_result = call_tool("claim_next_review", {"reviewer": "claude-code", "project_id": "project_test"}, db)
                import_result = call_tool(
                    "import_review_result",
                    {
                        "task_id": task_id,
                        "review_id": review_id,
                        "reviewer": "claude-code",
                        "last_seen_event_id": claim_result["latest_event"]["event_id"],
                        "body_md": review_body(),
                    },
                    db,
                )
                finding_id = import_result["review_findings"][0]["id"]
                update_result = call_tool(
                    "update_review_finding",
                    {
                        "finding_id": finding_id,
                        "status": "addressed",
                        "reason": "fixed by follow-up",
                        "actor": "codex",
                        "last_seen_event_id": import_result["latest_event"]["event_id"],
                    },
                    db,
                )
                with self.assertRaises(McpToolError):
                    call_tool(
                        "update_review_finding",
                        {
                            "finding_id": finding_id,
                            "status": "accepted-risk",
                            "reason": "stale overwrite attempt",
                            "actor": "codex",
                            "last_seen_event_id": import_result["latest_event"]["event_id"],
                        },
                        db,
                    )
                review_status = call_tool("get_review_status", {"task_id": task_id}, db)
                store = Store(db)
                try:
                    request = store.get("review_requests", review_id)
                    result = store.latest_for_task("review_results", task_id)
                    finding = store.get("review_findings", finding_id)
                    updates = store.list_where("review_finding_updates", "finding_id=?", (finding_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertIn("# Review Request", prompt_result["body_md"])
        self.assertIn("# ReviewResult", template_result["body_md"])
        self.assertEqual(request["status"], "completed")
        self.assertEqual(result["reviewer"], "claude-code")
        self.assertEqual(result["verdict"], "changes_requested")
        self.assertEqual(finding["status"], "addressed")
        self.assertEqual(update_result["latest_event"]["source"], "review_finding_update")
        self.assertEqual(update_result["latest_event"]["event_id"], update_result["review_finding_update"]["id"])
        self.assertEqual(update_result["review_finding_update"]["previous_status"], "unresolved")
        self.assertEqual(updates[0]["reason"], "fixed by follow-up")
        self.assertEqual(review_status["review_findings"][0]["status"], "addressed")

    def test_import_review_result_rejects_stale_event_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Stale review"])
                store = Store(db)
                try:
                    task = store.list_where("tasks", "project_id=?", ("project_test",))[0]
                    task_id = task["id"]
                    created_at = now_iso()
                    store.insert(
                        "review_requests",
                        {
                            "id": "review_stale",
                            "task_id": task_id,
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "status": "requested",
                            "reason": "stale check",
                            "created_at": created_at,
                            "updated_at": created_at,
                        },
                    )
                finally:
                    store.close()

                with self.assertRaises(McpToolError):
                    call_tool(
                        "import_review_result",
                        {
                            "task_id": task_id,
                            "review_id": "review_stale",
                            "reviewer": "claude-code",
                            "last_seen_event_id": "old_event",
                            "body_md": review_body(),
                        },
                        db,
                    )
                store = Store(db)
                try:
                    results = store.list_where("review_results", "task_id=?", (task_id,))
                    findings = store.list_where("review_findings", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(results, [])
        self.assertEqual(findings, [])

    def test_dispatch_review_runs_fake_claude_code_reviewer_to_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_reviewer(root, verdict="changes_requested")
                config = write_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Dispatch task"])
                context = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                task_id = context["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                status = call_tool("get_review_status", {"task_id": task_id}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["actor"], "codex")
        self.assertEqual(result["reviewer"], "claude-code")
        self.assertEqual(result["verdict"], "changes_requested")
        self.assertEqual(result["blocking_findings"], 1)
        self.assertEqual(result["next_action"]["type"], "address_blocking_findings")
        self.assertEqual(status["review_requests"][0]["status"], "completed")
        self.assertEqual(status["review_results"][0]["reviewer"], "claude-code")

    def test_dispatch_review_runs_fake_codex_reviewer_to_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_reviewer(root, verdict="approved", findings="なし")
                config = write_reviewer_config(root, ["codex"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Reverse dispatch task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "claude-code",
                        "reviewer": "codex",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["actor"], "claude-code")
        self.assertEqual(result["reviewer"], "codex")
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(result["next_action"]["type"], "ready_to_complete_task")

    def test_dispatch_review_auto_start_false_returns_minimal_worker_command(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_reviewer(root)
                config = write_reviewer_config(root, ["claude-code"], auto_start=False)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Needs worker task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": False,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                store = Store(db)
                try:
                    requests = store.list_where("review_requests", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "needs_reviewer_worker")
        self.assertEqual(result["next_action"]["type"], "start_reviewer_worker")
        self.assertIn("command", result["next_action"])
        self.assertEqual(requests, [])

    def test_dispatch_review_auto_start_true_revives_stale_reviewer_and_completes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_reviewer(root, verdict="approved", findings="なし")
                config = write_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Stale dispatch task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]
                store = Store(db)
                try:
                    store.insert(
                        "review_reviewers",
                        {
                            "id": "reviewer_stale",
                            "reviewer": "claude-code",
                            "status": "available",
                            "capabilities": ["review"],
                            "max_concurrent": 1,
                            "metadata": {"dispatch_capable": True},
                            "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                            "created_at": "2000-01-01T00:00:00+00:00",
                            "updated_at": "2000-01-01T00:00:00+00:00",
                        },
                    )
                finally:
                    store.close()

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                status = call_tool("get_review_status", {"task_id": task_id}, db)
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(status["review_requests"][0]["status"], "completed")
        self.assertEqual(status["review_results"][0]["reviewer"], "claude-code")

    def test_dispatch_review_uses_mcp_reviewer_workflow_by_default_without_cli_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fake_claude_cli(bin_dir, root)
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                os.environ["PATH"] = str(bin_dir) + os.pathsep + previous_path
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Auto config dispatch task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                    },
                    db,
                )
                config_exists = (root / ".nilo" / "reviewers.toml").exists()
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

        self.assertEqual(result["operation"], "dispatch_review")
        self.assertEqual(result["mode"], "mcp_reviewer_workflow")
        self.assertEqual(result["status"], "reviewer_unavailable")
        self.assertEqual(result["reviewer"], "claude-code")
        self.assertEqual(result["reviewer_availability"], "missing")
        self.assertFalse(config_exists)
        self.assertIn("register_reviewer", result["next_action"])
        self.assertIn("claim_next_review", result["claude_code_prompt"])

    def test_dispatch_review_auto_configure_does_not_enable_cli_fallback_without_opt_in(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fake_claude_cli(bin_dir, root)
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                os.environ["PATH"] = str(bin_dir) + os.pathsep + previous_path
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Auto config dispatch task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "auto_configure": True,
                    },
                    db,
                )
                config_exists = (root / ".nilo" / "reviewers.toml").exists()
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

        self.assertEqual(result["operation"], "dispatch_review")
        self.assertEqual(result["mode"], "mcp_reviewer_workflow")
        self.assertEqual(result["status"], "reviewer_unavailable")
        self.assertFalse(config_exists)
        self.assertIn("register_reviewer", result["next_action"])

    def test_dispatch_review_auto_configures_claude_code_only_when_requested(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fake_claude_cli(bin_dir, root)
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                os.environ["PATH"] = str(bin_dir) + os.pathsep + previous_path
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Auto config dispatch task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "auto_configure": True,
                        "allow_cli_fallback": True,
                    },
                    db,
                )
                config_body = (root / ".nilo" / "reviewers.toml").read_text(encoding="utf-8")
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertIn("[reviewers.claude-code]", config_body)
        self.assertIn('command = "claude"', config_body)
        self.assertIn("local_cli_fallback = true", config_body)

    def test_dispatch_review_auto_configures_codex_only_when_requested(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            write_fake_codex_cli(bin_dir, root)
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                os.environ["PATH"] = str(bin_dir) + os.pathsep + previous_path
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Codex auto config dispatch task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "claude-code",
                        "reviewer": "codex",
                        "project_id": "project_test",
                        "auto_start": True,
                        "auto_configure": True,
                        "allow_cli_fallback": True,
                    },
                    db,
                )
                config_body = (root / ".nilo" / "reviewers.toml").read_text(encoding="utf-8")
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertIn("[reviewers.codex]", config_body)
        self.assertIn('command = "codex"', config_body)
        self.assertIn("local_cli_fallback = true", config_body)
        self.assertIn('"exec"', config_body)
        self.assertIn("codex reviewer", config_body)

    def test_dispatch_review_resolves_windows_cmd_shim_from_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "claude.cmd"
            shim.write_text("@echo off\r\n", encoding="utf-8")

            resolved = find_executable("claude", {"PATH": str(bin_dir)})

        self.assertEqual(Path(resolved or "").name.casefold(), "claude.cmd")

    def test_dispatch_review_resolves_windows_codex_cmd_shim_from_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "codex.cmd"
            shim.write_text("@echo off\r\n", encoding="utf-8")

            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("codex", {"PATH": str(bin_dir)})

        self.assertEqual(Path(resolved or "").name.casefold(), "codex.cmd")

    def test_dispatch_review_prompt_metadata_warns_on_secret_and_can_drop_raw_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            secret = "sk-" + "b" * 48
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_reviewer(root, verdict="approved", findings="なし")
                config = root / "reviewers.toml"
                config.write_text(
                    '[reviewers.claude-code]\n'
                    'kind = "agent"\n'
                    f"command = {json.dumps(sys.executable)}\n"
                    'args = ["fake_reviewer.py", "{prompt_file}"]\n'
                    'working_directory = "{repo_root}"\n'
                    'auto_start = true\n'
                    'timeout_seconds = 10\n'
                    'dispatch_capable = true\n'
                    'persist_prompt_file = false\n',
                    encoding="utf-8",
                )
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "create",
                            "--project",
                            "project_test",
                            "--title",
                            "Secret prompt task",
                            "--description",
                            f"Contains {secret}",
                        ]
                    )
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                prompt_files = list((root / ".nilo" / "reviews").glob("*_prompt.md"))
                metadata_files = list((root / ".nilo" / "reviews").glob("*_prompt.metadata.json"))
                metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(prompt_files, [])
        self.assertEqual(metadata["secret_detected"], True)
        self.assertIn("secret detected: openai_api_key", metadata["secret_warnings"])
        self.assertIn("temporary reviewer handoff", metadata["storage_scope"])
        self.assertIn("persist_prompt_file", metadata["raw_prompt_persistence"])

    def test_dispatch_review_masks_reviewer_process_output_in_payload_and_db(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                secret = "sk-" + "a" * 48
                script = root / "secret_reviewer.py"
                script.write_text(
                    "import sys\n"
                    f"print('stdout secret {secret}')\n"
                    f"print('stderr secret {secret}', file=sys.stderr)\n"
                    "raise SystemExit(7)\n",
                    encoding="utf-8",
                )
                config = root / "reviewers.toml"
                config.write_text(
                    '[reviewers.claude-code]\n'
                    'kind = "agent"\n'
                    f"command = {json.dumps(sys.executable)}\n"
                    'args = ["secret_reviewer.py", "{prompt_file}"]\n'
                    'working_directory = "{repo_root}"\n'
                    'auto_start = true\n'
                    'timeout_seconds = 10\n'
                    'dispatch_capable = true\n',
                    encoding="utf-8",
                )
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Secret output task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                store = Store(db)
                try:
                    dispatch = store.list_where("review_dispatches", "task_id=?", (task_id,))[0]
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_failed")
        self.assertNotIn(secret, result["stdout"])
        self.assertNotIn(secret, result["stderr"])
        self.assertNotIn(secret, dispatch["stdout"])
        self.assertNotIn(secret, dispatch["stderr"])
        self.assertIn("[MASKED:openai_api_key]", result["stdout"])
        self.assertIn("[MASKED:openai_api_key]", dispatch["stderr"])

    def test_dispatch_review_success_supersedes_older_pending_review_requests(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_reviewer(root, verdict="approved", findings="なし")
                config = write_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Supersede stale task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]
                store = Store(db)
                try:
                    store.insert(
                        "review_requests",
                        {
                            "id": "review_old",
                            "task_id": task_id,
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "status": "reviewer_unavailable",
                            "reason": "old review",
                            "created_at": "2000-01-01T00:00:00+00:00",
                            "updated_at": "2000-01-01T00:00:00+00:00",
                        },
                    )
                finally:
                    store.close()

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                status = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)
                store = Store(db)
                try:
                    old_review = store.get("review_requests", "review_old")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(old_review["status"], "superseded")
        self.assertNotIn("review_old", "\n".join(status["next_actions"]))

    def test_store_does_not_globally_json_decode_generic_args_column(self) -> None:
        self.assertNotIn("args", JSON_COLUMNS)

    def test_dispatch_review_missing_config_returns_needs_reviewer_config(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Missing config task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(root / "missing-reviewers.toml"),
                    },
                    db,
                )
                store = Store(db)
                try:
                    requests = store.list_where("review_requests", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "needs_reviewer_config")
        self.assertEqual(result["failure_stage"], "reviewer_config")
        self.assertEqual(result["command"], "")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["next_action"]["type"], "create_reviewer_config")
        self.assertEqual(requests, [])

    def test_dispatch_review_records_command_not_found_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                config = root / "reviewers.toml"
                config.write_text(
                    '[reviewers.claude-code]\n'
                    'kind = "agent"\n'
                    'command = "definitely-missing-nilo-reviewer"\n'
                    'args = ["{prompt_file}"]\n'
                    'working_directory = "{repo_root}"\n'
                    'auto_start = true\n'
                    'timeout_seconds = 10\n'
                    'dispatch_capable = true\n',
                    encoding="utf-8",
                )
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--title", "Missing command task"])
                task_id = call_tool("get_agent_work_context", {"project_id": "project_test"}, db)["active_tasks"][0]["id"]

                result = call_tool(
                    "dispatch_review",
                    {
                        "task_id": task_id,
                        "actor": "codex",
                        "reviewer": "claude-code",
                        "project_id": "project_test",
                        "auto_start": True,
                        "allow_cli_fallback": True,
                        "config_path": str(config),
                    },
                    db,
                )
                store = Store(db)
                try:
                    requests = store.list_where("review_requests", "task_id=?", (task_id,))
                    dispatches = store.list_where("review_dispatches", "task_id=?", (task_id,))
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "review_failed")
        self.assertEqual(result["failure_stage"], "command_resolution")
        self.assertEqual(result["next_action"]["type"], "fix_reviewer_command")
        self.assertIn("definitely-missing-nilo-reviewer", result["command"])
        self.assertIn("definitely-missing-nilo-reviewer", result["stderr"])
        self.assertEqual(requests, [])
        self.assertEqual(dispatches[0]["status"], "review_failed")


def report_body(changed_files: list[str]) -> str:
    changed = "\n".join(f"- {path}" for path in changed_files)
    return f"""# 完了報告

## 1. 実施内容
MCP report import を確認した。

## 2. 変更ファイル一覧
{changed}

## 3. 実行した検証
### テストコマンド
python -m unittest
### テスト結果
passed
### 型チェック
未実行。型チェック設定がないため。
### lint
未実行。lint設定がないため。

## 4. 未実行の検証（理由を記載）
なし。

## 5. 既知の問題 / 仕様から外れた判断
なし。

## 6. 人間に確認してほしい点
なし。
"""


def review_body(verdict: str = "changes_requested", summary: str = "MCP review found one issue.", findings: str | None = None) -> str:
    if findings is None:
        findings = """### F1
severity: high
status: unresolved
file: src/nilo/mcp_server.py
line: 12
blocking: true

Review finding from MCP.
"""
    return f"""# ReviewResult

## Verdict
{verdict}

## Summary
{summary}

## Findings
{findings}
"""


def write_fake_reviewer(root: Path, verdict: str = "changes_requested", findings: str | None = None) -> Path:
    if findings is None:
        findings = (
            "### F1\n"
            "severity: high\n"
            "status: unresolved\n"
            "file: src/example.py\n"
            "line: 1\n"
            "blocking: true\n\n"
            "Fake reviewer found a blocking issue.\n"
        )
    script = root / "fake_reviewer.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "prompt = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "assert '# Review Request' in prompt\n"
        "print('# ReviewResult')\n"
        "print('\\n## Verdict')\n"
        f"print({verdict!r})\n"
        "print('\\n## Summary')\n"
        "print('Fake reviewer completed the dispatched review.')\n"
        "print('\\n## Findings')\n"
        f"print({findings!r})\n",
        encoding="utf-8",
    )
    return script


def write_fake_claude_cli(bin_dir: Path, root: Path) -> Path:
    script = root / "fake_claude.py"
    script.write_text(
        "from pathlib import Path\n"
        "import re\n"
        "import sys\n"
        "if '--help' in sys.argv:\n"
        "    print('Claude Code --print -p')\n"
        "    raise SystemExit(0)\n"
        "body = ' '.join(sys.argv[1:])\n"
        "match = re.search(r'prompt at (.+?_prompt\\.md)', body)\n"
        "assert match, body\n"
        "prompt = Path(match.group(1)).read_text(encoding='utf-8')\n"
        "assert '# Review Request' in prompt\n"
        "print('# ReviewResult')\n"
        "print('\\n## Verdict')\n"
        "print('approved')\n"
        "print('\\n## Summary')\n"
        "print('Fake claude completed the dispatched review.')\n"
        "print('\\n## Findings')\n"
        "print('なし')\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        shim = bin_dir / "claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = bin_dir / "claude"
        shim.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n', encoding="utf-8")
        shim.chmod(0o755)
    return shim


def write_fake_codex_cli(bin_dir: Path, root: Path) -> Path:
    script = root / "fake_codex.py"
    script.write_text(
        "from pathlib import Path\n"
        "import re\n"
        "import sys\n"
        "body = ' '.join(sys.argv[1:])\n"
        "assert 'exec' in sys.argv, sys.argv\n"
        "match = re.search(r'prompt at (.+?_prompt\\.md)', body)\n"
        "assert match, body\n"
        "prompt = Path(match.group(1)).read_text(encoding='utf-8')\n"
        "assert '# Review Request' in prompt\n"
        "print('# ReviewResult')\n"
        "print('\\n## Verdict')\n"
        "print('approved')\n"
        "print('\\n## Summary')\n"
        "print('Fake codex completed the dispatched review.')\n"
        "print('\\n## Findings')\n"
        "print('なし')\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        shim = bin_dir / "codex.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = bin_dir / "codex"
        shim.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name="__main__")\n', encoding="utf-8")
        shim.chmod(0o755)
    return shim


def write_reviewer_config(root: Path, reviewers: list[str], auto_start: bool = True) -> Path:
    path = root / "reviewers.toml"
    blocks = []
    for reviewer in reviewers:
        blocks.append(
            f"[reviewers.{reviewer}]\n"
            'kind = "agent"\n'
            f"command = {json.dumps(sys.executable)}\n"
            'args = ["fake_reviewer.py", "{prompt_file}"]\n'
            'working_directory = "{repo_root}"\n'
            f"auto_start = {str(auto_start).lower()}\n"
            "timeout_seconds = 10\n"
            "dispatch_capable = true\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


class McpStdioClient:
    def __init__(self, db: Path) -> None:
        env = os.environ.copy()
        src_path = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src_path
        self.process = subprocess.Popen(
            [sys.executable, "-c", "from nilo.cli import main; main()", "--db", str(db), "mcp", "serve"],
            cwd=str(db.parent),
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._next_id = 1

    def initialize(self, client_name: str = "test-client") -> None:
        response = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": client_name}})
        self.assert_success(response)
        self.notify("notifications/initialized")

    def call_tool(self, name: str, arguments: dict) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        self.assert_success(response)
        result = response["result"]
        if result.get("isError"):
            raise AssertionError(result)
        return json.loads(result["content"][0]["text"])

    def request(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        return self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method}, expect_response=False)

    def _send(self, payload: dict, expect_response: bool = True) -> dict:
        if self.process.stdin is None or self.process.stdout is None:
            raise AssertionError("MCP stdio process pipes are not available")
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        if not expect_response:
            return {}
        line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr is not None else ""
            raise AssertionError(f"MCP stdio process exited without a response. stderr={stderr}")
        return json.loads(line)

    def assert_success(self, response: dict) -> None:
        if "error" in response:
            raise AssertionError(response["error"])

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()


if __name__ == "__main__":
    unittest.main()
