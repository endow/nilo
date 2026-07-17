from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nilo.store import CORE_STATE_TABLES, Store
from nilo.timeutil import now_iso


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "nilo"

ALLOWED_CORE_WRITERS = {
    "agent_reports": {"src/nilo/agent_report_import.py"},
    "failure_logs": {"src/nilo/failure.py", "src/nilo/transitions.py"},
    "instructions": {"src/nilo/work_service.py", "src/nilo/workflow_services.py"},
    "overdrive_events": {"src/nilo/overdrive.py"},
    "overdrive_runs": {"src/nilo/overdrive.py"},
    "review_findings": {"src/nilo/transitions.py"},
    "review_finding_updates": {"src/nilo/transitions.py"},
    "review_attempts": {"src/nilo/review_lifecycle.py"},
    "review_requests": {"src/nilo/review_lifecycle.py", "src/nilo/transitions.py"},
    "review_results": {"src/nilo/transitions.py"},
    "roadmap_commitments": {"src/nilo/transitions.py", "src/nilo/cli_handlers/roadmap.py"},
    "roadmap_revisions": {"src/nilo/transitions.py", "src/nilo/cli_handlers/roadmap.py"},
    "task_completions": {"src/nilo/transitions.py", "src/nilo/cli_handlers/task.py", "src/nilo/workflow_context.py"},
    "tasks": {
        "src/nilo/transitions.py",
        "src/nilo/cli_handlers/task.py",
        "src/nilo/cli_handlers/quality.py",
        "src/nilo/mcp_server.py",
        "src/nilo/project_logic.py",
        "src/nilo/workflow_services.py",
        "src/nilo/cli_handlers/release.py",
        "src/nilo/work_service.py",
    },
    "todos": {"src/nilo/transitions.py", "src/nilo/cli_handlers/todo.py", "src/nilo/mcp_server.py"},
    "transition_events": {"src/nilo/transitions.py", "src/nilo/workflow_context.py"},
    "understanding_checks": {"src/nilo/transitions.py", "src/nilo/workflow_services.py"},
    "verification_runs": {"src/nilo/transitions.py"},
}


def production_store_writes() -> list[tuple[str, str, str, int]]:
    writes: list[tuple[str, str, str, int]] = []
    for path in SRC_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in {"insert", "update"}:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "store":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            table = node.args[0].value
            if table in CORE_STATE_TABLES:
                writes.append((relative, table, func.attr, node.lineno))
    return writes


class WritePathTests(unittest.TestCase):
    def test_core_state_direct_writes_match_allowlist(self) -> None:
        unexpected = []
        for relative, table, operation, lineno in production_store_writes():
            if relative not in ALLOWED_CORE_WRITERS.get(table, set()):
                unexpected.append(f"{relative}:{lineno} {operation}({table})")
        self.assertEqual(unexpected, [])

    def test_documented_core_tables_match_store_guard_tables(self) -> None:
        doc = (REPO_ROOT / "docs" / "internal" / "write-paths.md").read_text(encoding="utf-8")
        missing = sorted(table for table in CORE_STATE_TABLES if f"`{table}`" not in doc)
        self.assertEqual(missing, [])

    def test_store_warns_for_core_write_outside_transaction_without_blocking(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                store.insert(
                    "projects",
                    {
                        "id": "project_test",
                        "name": "Test",
                        "tech_stack": [],
                        "rules": [],
                        "default_completion_criteria": [],
                        "available_models": [],
                        "fallback_models": [],
                        "requires_local_execution": False,
                        "created_at": now_iso(),
                    },
                )
                store.insert(
                    "tasks",
                    {
                        "id": "task_test",
                        "project_id": "project_test",
                        "title": "Task",
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
                    },
                )
                self.assertEqual(store.direct_write_warnings[-1], {"table": "tasks", "operation": "insert"})
                self.assertIsNotNone(store.get("tasks", "task_test"))
            finally:
                store.close()

    def test_store_does_not_warn_for_core_write_inside_transaction(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                with store.transaction():
                    store.insert(
                        "projects",
                        {
                            "id": "project_test",
                            "name": "Test",
                            "tech_stack": [],
                            "rules": [],
                            "default_completion_criteria": [],
                            "available_models": [],
                            "fallback_models": [],
                            "requires_local_execution": False,
                            "created_at": now_iso(),
                        },
                    )
                self.assertEqual(store.direct_write_warnings, [])
            finally:
                store.close()

    def test_store_rejects_invalid_write_identifiers(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                with self.assertRaisesRegex(ValueError, "invalid SQL table identifier"):
                    store.insert("projects; DROP TABLE tasks", {"id": "project_test"})
                with self.assertRaisesRegex(ValueError, "unknown SQL table"):
                    store.insert("not_a_table", {"id": "project_test"})
                with self.assertRaisesRegex(ValueError, "invalid SQL column identifier"):
                    store.insert("projects", {"id": "project_test", "name); DROP TABLE tasks; --": "bad"})
                with self.assertRaisesRegex(ValueError, "unknown SQL column"):
                    store.update("projects", "project_test", {"not_a_column": "bad"})
            finally:
                store.close()

    def test_store_rejects_invalid_read_tables(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                with self.assertRaisesRegex(ValueError, "invalid SQL table identifier"):
                    store.get("projects; DROP TABLE tasks", "project_test")
                with self.assertRaisesRegex(ValueError, "unknown SQL table"):
                    store.latest_for_task("not_a_table", "task_test")
                with self.assertRaisesRegex(ValueError, "unknown SQL table"):
                    store.list_where("not_a_table")
            finally:
                store.close()

    def test_store_caches_validated_table_columns(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                first = store._table_columns("projects")
                second = store._table_columns("projects")
                self.assertIs(first, second)
                self.assertIn("id", first)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
