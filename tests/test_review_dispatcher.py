from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.mcp_server import call_tool
from nilo.review_dispatcher import DispatchError, ResolvedCommand, ReviewerConfig
from nilo.review_dispatcher import DEFAULT_CLAUDE_REVIEW_PROMPT, DEFAULT_CODEX_REVIEW_PROMPT
from nilo.review_dispatcher import dispatch_review, find_executable, load_reviewer_config, resolve_command_parts, run_reviewer_process, safe_default_config
from nilo.review_dispatcher import doctor_reviewer_config
from nilo.reviewer_registry import reviewer_is_registered_available
from nilo.store import Store
from nilo.timeutil import now_iso


def create_project_and_task(store: Store, *, task_id: str = "task_test") -> None:
    created_at = now_iso()
    if not store.get("projects", "project_test"):
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
                "created_at": created_at,
            },
        )
    store.insert(
        "tasks",
        {
            "id": task_id,
            "project_id": "project_test",
            "title": "Dispatch task",
            "description": "",
            "acceptance_criteria": [],
            "task_type": "implementation",
            "risk_level": "medium",
            "requires_understanding_check": 0,
            "roadmap_commitment_id": "",
            "roadmap_item_id": "",
            "status": "instruction_generated",
            "assigned_model_profile": "default",
            "degradation_mode": "normal",
            "mode": "normal",
            "base_commit": None,
            "created_at": created_at,
        },
    )


def write_config(root: Path, *, args: list[str], timeout_seconds: float = 10, persist_prompt_file: bool = True) -> Path:
    config = root / "reviewers.toml"
    config.write_text(
        "[reviewers.claude-code]\n"
        'kind = "agent"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = {json.dumps(args)}\n"
        'working_directory = "{repo_root}"\n'
        "auto_start = true\n"
        f"timeout_seconds = {timeout_seconds}\n"
        "dispatch_capable = true\n"
        f"persist_prompt_file = {str(persist_prompt_file).lower()}\n",
        encoding="utf-8",
    )
    return config


def write_openai_config(root: Path, endpoint: str, *, api_key_env: str = "", capabilities: list[str] | None = None) -> Path:
    api_key_line = f"api_key_env = {json.dumps(api_key_env)}\n" if api_key_env else ""
    capabilities_line = f"capabilities = {json.dumps(capabilities)}\n" if capabilities else ""
    config = root / "reviewers.toml"
    config.write_text(
        "[reviewers.local-reviewer]\n"
        'kind = "openai_compatible"\n'
        f"endpoint = {json.dumps(endpoint)}\n"
        'model = "test-model"\n'
        f"{api_key_line}"
        f"{capabilities_line}"
        "auto_start = true\n"
        "timeout_seconds = 5\n"
        "dispatch_capable = true\n"
        "confidence_threshold = 0.75\n",
        encoding="utf-8",
    )
    return config


def write_reviewer_script(root: Path, body: str, *, name: str = "reviewer.py") -> Path:
    script = root / name
    script.write_text(body, encoding="utf-8")
    return script


def review_result(*, summary: str = "OK.", finding_description: str = "") -> str:
    findings = "No findings."
    if finding_description:
        findings = (
            "- title: Secret finding\n"
            "  severity: high\n"
            "  file: src/secret.py\n"
            "  line: 12\n"
            "  blocking: true\n"
            f"  description: {finding_description}\n"
        )
    return f"""# ReviewResult

## Verdict
approved

## Summary
{summary}

## Findings
{findings}
"""


class ReviewDispatcherTests(unittest.TestCase):
    def test_mcp_registers_and_lists_local_reviewer_abstraction(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            try:
                create_project_and_task(store)
            finally:
                store.close()

            registered = call_tool(
                "register_reviewer",
                {
                    "reviewer": "local-reviewer",
                    "capabilities": ["review_diff", "summarize", "propose_tests"],
                    "metadata": {
                        "display_name": "Local Review Model",
                        "backend_kind": "openai_compatible",
                        "dispatch_capable": True,
                        "context_limits": {"max_input_tokens": 4096},
                        "tool_access_limitations": ["no shell access"],
                        "evidence_requirements": ["tests", "diff inspection"],
                    },
                },
                db,
            )
            doctor = call_tool("mcp_doctor", {"project_id": "project_test"}, db)

        reviewer = registered["reviewer"]
        local_rows = [row for row in doctor["reviewers"] if row["reviewer"] == "local-reviewer"]
        self.assertEqual(reviewer["capabilities"], ["review_diff", "summarize", "propose_tests"])
        self.assertEqual(reviewer["metadata"]["backend_kind"], "openai_compatible")
        self.assertEqual(local_rows[0]["display_name"], "Local Review Model")
        self.assertEqual(local_rows[0]["backend_kind"], "openai_compatible")
        self.assertEqual(local_rows[0]["availability"], "available")
        self.assertTrue(local_rows[0]["dispatch_capable"])

    def test_empty_existing_reviewer_capabilities_remain_review_capable(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            try:
                create_project_and_task(store)
                store.insert(
                    "review_reviewers",
                    {
                        "id": "reviewer_legacy_empty",
                        "reviewer": "legacy-reviewer",
                        "status": "available",
                        "capabilities": [],
                        "max_concurrent": 1,
                        "metadata": {"dispatch_capable": True},
                        "last_heartbeat_at": now_iso(),
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
                available = reviewer_is_registered_available(store, "legacy-reviewer")
            finally:
                store.close()

        self.assertTrue(available)

    def test_doctor_reviewer_config_reports_configured_capabilities_and_backend_kind(self) -> None:
        with LocalOpenAICompatibleServer({"summary": "unused"}) as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            config = write_openai_config(root, endpoint, capabilities=["review", "summarize", "propose_tests"])
            from nilo.review_dispatcher import doctor_reviewer_config

            result = doctor_reviewer_config(config, ["local-reviewer"])

        reviewer = result["reviewers"][0]
        self.assertEqual(reviewer["backend_kind"], "openai_compatible")
        self.assertEqual(reviewer["capabilities"], ["review_diff", "summarize", "propose_tests"])

    def test_doctor_reviewer_config_reports_resolved_executable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            shim = bin_dir / "claude.cmd"
            shim.write_text("@echo off\r\n", encoding="utf-8")
            config = root / "reviewers.toml"
            config.write_text(
                "[reviewers.claude-code]\n"
                'kind = "agent"\n'
                'command = "claude"\n'
                'args = ["{prompt_file}"]\n',
                encoding="utf-8",
            )
            from nilo.review_dispatcher import doctor_reviewer_config

            with (
                patch("nilo.review_dispatcher.sys.platform", "win32"),
                patch.dict("os.environ", {"PATH": str(bin_dir)}),
            ):
                result = doctor_reviewer_config(config, ["claude-code"])

        reviewer = result["reviewers"][0]
        self.assertEqual(reviewer["command"], "claude")
        self.assertEqual(Path(reviewer["resolved_executable"]).name.casefold(), "claude.cmd")
        self.assertEqual(reviewer["executable"], reviewer["resolved_executable"])
        self.assertTrue(reviewer["command_found"])

    def test_dispatch_openai_compatible_local_reviewer_preserves_limitations(self) -> None:
        response = {
            "summary": "Local review found no blocking issue, but confidence is limited.",
            "findings": [],
            "confidence": 0.4,
            "limitations": ["small local model", "did not execute tests"],
            "suggested_next_actions": ["run unit tests"],
        }
        with LocalOpenAICompatibleServer(response) as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = write_openai_config(root, endpoint)
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="local-reviewer", task_id="task_test", config_path=config, repo_root=root)
                review = store.latest_for_task("review_results", "task_test")
                reviewer_row = store.list_where("review_reviewers", "reviewer=?", ("local-reviewer",))[0]
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["verdict"], "commented")
        self.assertEqual(reviewer_row["metadata"]["backend_kind"], "openai_compatible")
        self.assertIn("confidence: 0.4", review["body_md"])
        self.assertIn("small local model", review["body_md"])
        self.assertIn("low confidence local review", review["body_md"])
        self.assertIn("run unit tests", review["body_md"])

    def test_local_reviewer_non_numeric_confidence_is_low_confidence_limitation(self) -> None:
        response = {
            "summary": "Local review used a schema-ish confidence value.",
            "findings": [],
            "confidence": "number from 0 to 1",
            "limitations": [],
            "suggested_next_actions": [],
        }
        with LocalOpenAICompatibleServer(response) as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = write_openai_config(root, endpoint)
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="local-reviewer", task_id="task_test", config_path=config, repo_root=root)
                request = store.latest_for_task("review_requests", "task_test")
                review = store.latest_for_task("review_results", "task_test")
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(request["status"], "completed")
        self.assertIn("confidence: 0", review["body_md"])
        self.assertIn("local reviewer returned non-numeric confidence", review["body_md"])
        self.assertIn("low confidence local review", review["body_md"])

    def test_local_reviewer_uses_api_key_env_authorization_header(self) -> None:
        response = {
            "summary": "Local review used bearer auth.",
            "findings": [],
            "confidence": 0.8,
            "limitations": [],
            "suggested_next_actions": [],
        }
        with LocalOpenAICompatibleServer(response, expected_authorization="Bearer test-token") as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = write_openai_config(root, endpoint, api_key_env="NILO_TEST_LOCAL_REVIEWER_KEY")
            store = Store(db)
            try:
                create_project_and_task(store)
                with patch.dict("os.environ", {"NILO_TEST_LOCAL_REVIEWER_KEY": "test-token"}):
                    result = dispatch_review(store, actor="codex", reviewer="local-reviewer", task_id="task_test", config_path=config, repo_root=root)
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")

    def test_local_reviewer_requires_configured_api_key_env_before_http_request(self) -> None:
        response = {
            "summary": "Should not be requested.",
            "findings": [],
            "confidence": 0.8,
            "limitations": [],
            "suggested_next_actions": [],
        }
        server = LocalOpenAICompatibleServer(response)
        with server as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = write_openai_config(root, endpoint, api_key_env="NILO_TEST_MISSING_LOCAL_REVIEWER_KEY")
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="local-reviewer", task_id="task_test", config_path=config, repo_root=root)
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(result["status"], "needs_reviewer_config")
        self.assertEqual(result["failure_stage"], "reviewer_config")
        self.assertEqual(request["status"], "failed")
        self.assertEqual(server.requests, [])

    def test_local_reviewer_masks_prompt_before_http_request(self) -> None:
        secret = "sk-" + "b" * 48
        response = {
            "summary": "Local review received masked prompt.",
            "findings": [],
            "confidence": 0.8,
            "limitations": [],
            "suggested_next_actions": [],
        }
        server = LocalOpenAICompatibleServer(response)
        with server as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = write_openai_config(root, endpoint)
            store = Store(db)
            try:
                create_project_and_task(store)
                store.update("tasks", "task_test", {"description": f"Do not send raw secret {secret}"})
                result = dispatch_review(store, actor="codex", reviewer="local-reviewer", task_id="task_test", config_path=config, repo_root=root)
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")
        sent = json.dumps(server.requests, ensure_ascii=False)
        self.assertNotIn(secret, sent)
        self.assertIn("[MASKED:openai_api_key]", sent)

    def test_local_reviewer_blocking_finding_round_trips_to_changes_requested(self) -> None:
        response = {
            "summary": "Local review found a blocking issue.",
            "findings": [
                {
                    "title": "Unsafe local backend config",
                    "severity": "high",
                    "status": "unresolved",
                    "file_path": "src/nilo/review_dispatcher.py",
                    "line": "488",
                    "blocking": True,
                    "description": "Endpoint trust must be explicit.",
                }
            ],
            "confidence": 0.9,
            "limitations": [],
            "suggested_next_actions": ["document endpoint trust"],
        }
        with LocalOpenAICompatibleServer(response) as endpoint, TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = write_openai_config(root, endpoint)
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="local-reviewer", task_id="task_test", config_path=config, repo_root=root)
                finding = store.latest_for_task("review_findings", "task_test")
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(result["verdict"], "changes_requested")
        self.assertEqual(finding["title"], "F1: Unsafe local backend config")
        self.assertEqual(finding["severity"], "high")
        self.assertTrue(finding["blocking"])
        self.assertEqual(finding["file_path"], "src/nilo/review_dispatcher.py")
        self.assertEqual(finding["line"], "488")

    def test_claude_code_safe_default_config_marks_cli_fallback(self) -> None:
        with TemporaryDirectory() as directory, patch("nilo.review_dispatcher.find_executable", return_value=sys.executable):
            config_path = Path(directory) / "reviewers.toml"
            config = safe_default_config(config_path, "claude-code")
            body = config_path.read_text(encoding="utf-8")

        self.assertIsNotNone(config)
        self.assertEqual(config.command, "claude")
        self.assertEqual(config.args[:4], ["-p", "--permission-mode", "dontAsk", "--output-format"])
        self.assertIn("claude-code reviewer", config.args[-1])
        self.assertTrue(config.auto_start)
        self.assertTrue(config.dispatch_capable)
        self.assertTrue(config.local_cli_fallback)
        self.assertIn("Local CLI reviewer process fallback", body)
        self.assertIn("local_cli_fallback = true", body)

    def test_codex_safe_default_config_marks_cli_fallback(self) -> None:
        with TemporaryDirectory() as directory, patch("nilo.review_dispatcher.find_executable", return_value=sys.executable):
            config_path = Path(directory) / "reviewers.toml"
            config = safe_default_config(config_path, "codex")
            body = config_path.read_text(encoding="utf-8")

        self.assertIsNotNone(config)
        self.assertEqual(config.command, "codex")
        self.assertEqual(config.args[:2], ["exec", "--skip-git-repo-check"])
        self.assertIn("codex reviewer", config.args[-1])
        self.assertTrue(config.auto_start)
        self.assertTrue(config.dispatch_capable)
        self.assertTrue(config.local_cli_fallback)
        self.assertIn("Local CLI reviewer process fallback", body)
        self.assertIn("local_cli_fallback = true", body)

    def test_explicit_reviewer_config_defaults_to_cli_fallback_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "reviewers.toml"
            config_path.write_text(
                "[reviewers.claude-code]\n"
                'command = "fake-reviewer"\n'
                'args = ["{prompt_file}"]\n'
                'auto_start = true\n',
                encoding="utf-8",
            )

            config = load_reviewer_config(config_path, "claude-code")

        self.assertTrue(config.local_cli_fallback)

    def test_legacy_claude_cli_fallback_config_prompt_is_normalized(self) -> None:
        legacy_prompt = (
            "You are acting as the claude-code reviewer through Voile MCP. Read the review prompt at {prompt_file}. "
            "Return exactly a Voile markdown review result."
        )
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "reviewers.toml"
            config_path.write_text(
                "[reviewers.claude-code]\n"
                'kind = "agent"\n'
                'command = "claude"\n'
                f"args = {json.dumps(['-p', legacy_prompt])}\n"
                "auto_start = true\n"
                "dispatch_capable = true\n",
                encoding="utf-8",
            )

            config = load_reviewer_config(config_path, "claude-code")
            doctor = doctor_reviewer_config(config_path, ["claude-code"])

        self.assertEqual(config.args[-1], DEFAULT_CLAUDE_REVIEW_PROMPT)
        self.assertNotIn("Voile", config.args[-1])
        self.assertTrue(config.local_cli_fallback)
        self.assertTrue(config.legacy_cli_fallback_config)
        self.assertTrue(doctor["reviewers"][0]["legacy_cli_fallback_config"])
        self.assertEqual(doctor["reviewers"][0]["next_action"]["type"], "migrate_legacy_reviewer_config")

    def test_legacy_codex_cli_fallback_config_prompt_is_normalized(self) -> None:
        legacy_prompt = (
            "You are acting as the codex reviewer through Voile MCP. Read the review prompt at {prompt_file}. "
            "Return exactly a Voile markdown review result."
        )
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "reviewers.toml"
            config_path.write_text(
                "[reviewers.codex]\n"
                'kind = "agent"\n'
                'command = "codex"\n'
                f"args = {json.dumps(['exec', legacy_prompt])}\n"
                "auto_start = true\n"
                "dispatch_capable = true\n",
                encoding="utf-8",
            )

            config = load_reviewer_config(config_path, "codex")

        self.assertEqual(config.args[-1], DEFAULT_CODEX_REVIEW_PROMPT)
        self.assertNotIn("Voile", config.args[-1])
        self.assertTrue(config.legacy_cli_fallback_config)

    def test_windows_cmd_command_resolution(self) -> None:
        with TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            (bin_dir / "claude.cmd").write_text("@echo off\r\n", encoding="utf-8")

            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("claude", {"PATH": str(bin_dir)})

        self.assertEqual(Path(resolved or "").name.casefold(), "claude.cmd")

    def test_find_executable_prefers_cmd_on_windows(self) -> None:
        with TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            (bin_dir / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "claude.cmd").write_text("@echo off\r\n", encoding="utf-8")

            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("claude", {"PATH": str(bin_dir)})

        self.assertEqual(Path(resolved or "").name.casefold(), "claude.cmd")

    def test_find_executable_does_not_return_extensionless_on_windows(self) -> None:
        with TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            (bin_dir / "claude").write_text("#!/bin/sh\n", encoding="utf-8")

            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("claude", {"PATH": str(bin_dir)})

        self.assertIsNone(resolved)

    def test_find_executable_prefers_codex_cmd_on_windows(self) -> None:
        with TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            (bin_dir / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")

            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("codex", {"PATH": str(bin_dir)})

        self.assertEqual(Path(resolved or "").name.casefold(), "codex.cmd")

    def test_resolve_command_wraps_windows_cmd_shim_with_cmd_exe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            cmd_exe = root / "cmd.exe"
            cmd_exe.write_text("", encoding="utf-8")
            shim = bin_dir / "claude.cmd"
            shim.write_text("@echo off\r\n", encoding="utf-8")

            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = resolve_command_parts(
                    ["claude", "-p", "prompt with spaces"],
                    {"PATH": str(bin_dir), "ComSpec": str(cmd_exe)},
                )

        self.assertEqual(resolved.executable, str(shim))
        self.assertEqual(resolved.command[:5], [str(cmd_exe), "/d", "/c", "call", str(shim)])
        self.assertIn("prompt with spaces", resolved.command)

    @unittest.skipUnless(sys.platform == "win32", "Windows .cmd argument forwarding test")
    def test_windows_cmd_shim_forwards_quoted_prompt_argument(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            capture = root / "capture.py"
            captured = root / "captured.json"
            prompt = 'Read the prompt at C:\\repo\\.nilo\\reviews\\review_prompt.md and keep "quotes" intact.'
            capture.write_text(
                "import json, sys\n"
                f"open({str(captured)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            shim = bin_dir / "claude.cmd"
            shim.write_text(f"@echo off\r\n\"{sys.executable}\" \"{capture}\" %*\r\n", encoding="utf-8")
            resolved = resolve_command_parts(
                ["claude", "-p", "--output-format", "text", prompt],
                {"PATH": str(bin_dir), "ComSpec": os.environ.get("ComSpec", "")},
            )

            process = subprocess.run(resolved.command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            captured_args = json.loads(captured.read_text(encoding="utf-8"))

        self.assertEqual(captured_args, ["-p", "--output-format", "text", prompt])

    def test_run_reviewer_process_converts_oserror_to_dispatch_error(self) -> None:
        config = ReviewerConfig(
            name="claude-code",
            kind="agent",
            command="claude",
            args=[],
            working_directory="{repo_root}",
            auto_start=True,
            timeout_seconds=10,
            startup_timeout_seconds=30,
            heartbeat_interval_seconds=30,
            result_format="markdown_review",
            dispatch_capable=True,
            capabilities=["review_diff"],
            env={},
            persist_prompt_file=True,
        )
        resolved = ResolvedCommand(
            command=["C:\\bin\\claude", "-p"],
            executable="C:\\bin\\claude",
            preview="C:\\bin\\claude -p",
        )

        with patch(
            "nilo.review_dispatcher.subprocess.run",
            side_effect=OSError("[WinError 193] %1 is not a valid Win32 application"),
        ):
            with self.assertRaises(DispatchError) as raised:
                run_reviewer_process(config, Path.cwd(), {}, resolved)

        self.assertEqual(raised.exception.stage, "reviewer_process_start")
        self.assertEqual(raised.exception.next_action["type"], "fix_reviewer_command")
        self.assertEqual(raised.exception.next_action["reviewer"], "claude-code")
        self.assertIn("WinError 193", raised.exception.stderr)

    def test_command_not_found_records_command_resolution_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            config = root / "reviewers.toml"
            config.write_text(
                "[reviewers.claude-code]\n"
                'command = "definitely-missing-reviewer-binary"\n'
                'args = ["{prompt_file}"]\n'
                'auto_start = true\n',
                encoding="utf-8",
            )
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="claude-code", task_id="task_test", config_path=config, repo_root=root)
                dispatch = store.list_where("review_dispatches", "task_id=?", ("task_test",))[0]
            finally:
                store.close()

        self.assertEqual(result["status"], "review_failed")
        self.assertEqual(result["failure_stage"], "command_resolution")
        self.assertEqual(dispatch["failure_stage"], "command_resolution")
        self.assertEqual(store_requests(db), [])

    def test_timeout_records_reviewer_timeout_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            write_reviewer_script(root, "import time\ntime.sleep(10)\n")
            config = write_config(root, args=["reviewer.py", "{prompt_file}"], timeout_seconds=0.02)
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="claude-code", task_id="task_test", config_path=config, repo_root=root)
                request = store.latest_for_task("review_requests", "task_test")
                dispatch = store.list_where("review_dispatches", "task_id=?", ("task_test",))[0]
            finally:
                store.close()

        self.assertEqual(result["status"], "review_failed")
        self.assertEqual(result["failure_stage"], "reviewer_timeout")
        self.assertEqual(request["status"], "failed")
        self.assertEqual(dispatch["failure_stage"], "reviewer_timeout")

    def test_malformed_output_records_review_output_received_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            write_reviewer_script(root, "print('not a review result')\n")
            config = write_config(root, args=["reviewer.py", "{prompt_file}"])
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="claude-code", task_id="task_test", config_path=config, repo_root=root)
                request = store.latest_for_task("review_requests", "task_test")
                dispatch = store.list_where("review_dispatches", "task_id=?", ("task_test",))[0]
                review_results = store.list_where("review_results", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(result["status"], "review_failed")
        self.assertEqual(result["failure_stage"], "review_output_received")
        self.assertEqual(request["status"], "failed")
        self.assertEqual(dispatch["failure_stage"], "review_output_received")
        self.assertEqual(dispatch["status"], "review_failed")
        self.assertEqual(review_results, [])

    def test_masks_secrets_in_stdout_stderr_result_and_finding(self) -> None:
        secret = "sk-" + "a" * 48
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            body = review_result(summary=f"summary {secret}", finding_description=f"finding {secret}")
            write_reviewer_script(root, f"import sys\nprint({body!r})\nprint({secret!r}, file=sys.stderr)\n")
            config = write_config(root, args=["reviewer.py", "{prompt_file}"])
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="claude-code", task_id="task_test", config_path=config, repo_root=root)
                dispatch = store.list_where("review_dispatches", "task_id=?", ("task_test",))[0]
                review = store.latest_for_task("review_results", "task_test")
                finding = store.latest_for_task("review_findings", "task_test")
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")
        self.assertNotIn(secret, dispatch["stdout"])
        self.assertNotIn(secret, dispatch["stderr"])
        self.assertNotIn(secret, result["summary"])
        self.assertNotIn(secret, review["body_md"])
        self.assertNotIn(secret, finding["description"])
        self.assertIn("[MASKED:openai_api_key]", dispatch["stdout"])
        self.assertIn("[MASKED:openai_api_key]", dispatch["stderr"])
        self.assertIn("[MASKED:openai_api_key]", finding["description"])

    def test_persist_prompt_file_false_deletes_prompt_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            write_reviewer_script(root, f"print({review_result()!r})\n")
            config = write_config(root, args=["reviewer.py", "{prompt_file}"], persist_prompt_file=False)
            store = Store(db)
            try:
                create_project_and_task(store)
                result = dispatch_review(store, actor="codex", reviewer="claude-code", task_id="task_test", config_path=config, repo_root=root)
            finally:
                store.close()

            prompt_files = list((root / ".nilo" / "reviews").glob("*_prompt.md"))
            metadata_files = list((root / ".nilo" / "reviews").glob("*_prompt.metadata.json"))

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(prompt_files, [])
        self.assertEqual(len(metadata_files), 1)

    def test_stale_and_previous_active_reviews_are_superseded(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            write_reviewer_script(root, f"print({review_result()!r})\n")
            config = write_config(root, args=["reviewer.py", "{prompt_file}"])
            store = Store(db)
            try:
                create_project_and_task(store)
                for review_id, status in [("review_stale", "stale"), ("review_active", "in_progress")]:
                    store.insert(
                        "review_requests",
                        {
                            "id": review_id,
                            "task_id": "task_test",
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "status": status,
                            "reason": "old",
                            "created_at": "2000-01-01T00:00:00+00:00",
                            "updated_at": "2000-01-01T00:00:00+00:00",
                        },
                    )

                result = dispatch_review(store, actor="codex", reviewer="claude-code", task_id="task_test", config_path=config, repo_root=root)
                stale = store.get("review_requests", "review_stale")
                active = store.get("review_requests", "review_active")
            finally:
                store.close()

        self.assertEqual(result["status"], "review_completed")
        self.assertEqual(stale["status"], "superseded")
        self.assertEqual(active["status"], "superseded")

    def test_dispatch_review_success_only_sets_review_completed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            write_reviewer_script(root, f"print({review_result()!r})\n", name="success.py")
            success_config = write_config(root, args=["success.py", "{prompt_file}"])
            store = Store(db)
            try:
                create_project_and_task(store, task_id="task_success")
                success = dispatch_review(
                    store,
                    actor="codex",
                    reviewer="claude-code",
                    task_id="task_success",
                    config_path=success_config,
                    repo_root=root,
                )
                write_reviewer_script(root, "print('broken')\n", name="failure.py")
                failure_config = write_config(root, args=["failure.py", "{prompt_file}"])
                create_project_and_task(store, task_id="task_failure")
                failure = dispatch_review(
                    store,
                    actor="codex",
                    reviewer="claude-code",
                    task_id="task_failure",
                    config_path=failure_config,
                    repo_root=root,
                )
            finally:
                store.close()

        self.assertEqual(success["status"], "review_completed")
        self.assertEqual(failure["status"], "review_failed")
        self.assertNotEqual(failure["status"], "review_completed")


def store_requests(db: Path) -> list[dict]:
    store = Store(db)
    try:
        return store.list_where("review_requests")
    finally:
        store.close()


class LocalOpenAICompatibleServer:
    def __init__(self, review_response: dict, *, expected_authorization: str = "") -> None:
        self.review_response = review_response
        self.expected_authorization = expected_authorization
        self.requests: list[dict] = []
        self.httpd: HTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> str:
        review_response = self.review_response
        expected_authorization = self.expected_authorization
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                request = json.loads(body.decode("utf-8"))
                requests.append(request)
                if self.path != "/v1/chat/completions":
                    self.send_response(404)
                    self.end_headers()
                    return
                if "expected_response_schema" not in request["messages"][1]["content"]:
                    self.send_response(400)
                    self.end_headers()
                    return
                if expected_authorization and self.headers.get("Authorization") != expected_authorization:
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(review_response),
                            }
                        }
                    ]
                }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
