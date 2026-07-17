from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from nilo.agent_installation import (
    inspect_agent_runtime_files,
    install_agent_runtime_files,
    remove_nilo_managed_block,
)
from nilo.cli import NILO_BLOCK_BEGIN, NILO_BLOCK_END, NILO_GENERATED_MARKER
from nilo.project_model import default_project_row


class AgentInstallationServiceTests(unittest.TestCase):
    def test_install_and_inspect_runtime_files_without_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = default_project_row("sample", "2026-01-01T00:00:00+00:00")

            result = install_agent_runtime_files(
                project,
                ["claude-code", "codex"],
                cwd=root,
            )

            self.assertEqual(
                set(result.updated_paths),
                {
                    ".nilo/agent-instructions.md",
                    "CLAUDE.local.md",
                    "AGENTS.override.md",
                },
            )
            self.assertIn("not a git work tree", result.warnings[0])
            diagnostics = inspect_agent_runtime_files(root)
            self.assertTrue(diagnostics["checks"][".nilo/agent-instructions.md"])
            self.assertTrue(diagnostics["checks"]["CLAUDE.local.md"])
            self.assertTrue(diagnostics["checks"]["AGENTS.override.md"])

    def test_unmanaged_local_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unmanaged = root / "CLAUDE.local.md"
            unmanaged.write_text("human content\n", encoding="utf-8")
            project = default_project_row("sample", "2026-01-01T00:00:00+00:00")

            result = install_agent_runtime_files(project, ["claude-code"], cwd=root)

            self.assertEqual(unmanaged.read_text(encoding="utf-8"), "human content\n")
            self.assertIn(
                "not overwriting unmanaged local file", "\n".join(result.warnings)
            )

    def test_remove_legacy_block_preserves_human_content(self) -> None:
        source = f"before\n\n{NILO_BLOCK_BEGIN}\ngenerated\n{NILO_BLOCK_END}\n\nafter\n"
        self.assertEqual(remove_nilo_managed_block(source), "before\n\nafter\n")
        with self.assertRaisesRegex(ValueError, "malformed"):
            remove_nilo_managed_block(f"{NILO_BLOCK_BEGIN}\nmissing end")


class WorkflowArchitectureTests(unittest.TestCase):
    def test_workflow_handler_has_no_subprocess_or_direct_file_writes(self) -> None:
        path = Path("src/nilo/cli_handlers/workflow.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertNotIn("subprocess", imported_modules)
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        self.assertNotIn("write_text", calls)
        self.assertNotIn("write_bytes", calls)

    def test_domain_style_modules_do_not_import_cli_handlers(self) -> None:
        paths = [
            Path("src/nilo/agent_installation.py"),
            Path("src/nilo/doctor.py"),
            Path("src/nilo/verification.py"),
            Path("src/nilo/workflow_services.py"),
        ]
        for path in paths:
            with self.subTest(path=path):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = [
                    node.module or ""
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                ]
                self.assertFalse(any("cli_handlers" in item for item in imports))

    def test_agent_files_keep_generated_marker(self) -> None:
        self.assertTrue(NILO_GENERATED_MARKER.startswith("<!--"))


if __name__ == "__main__":
    unittest.main()
