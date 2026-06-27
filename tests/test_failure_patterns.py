from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.cli import main
from nilo.store import Store
from nilo.timeutil import now_iso

LEGACY_LEARNING_TABLES = {
    "derived_rules",
    "active_instruction_rules",
    "failure_patterns",
    "task_failure_pattern_matches",
    "success_patterns",
}


REPORT = """# 完了報告

## 1. 実施内容
対象の実装を行った。

## 2. 変更ファイル一覧
- src/nilo/failure.py

## 3. 実行した検証
### テストコマンド
python -m pytest tests/test_failure_patterns.py
### テスト結果
passed
### 型チェック
未実行。型チェック環境が未定義のため。
### lint
未実行。lint環境が未定義のため。

## 4. 未実行の検証（理由を記載）
型チェックとlintはプロジェクト設定がないため未実行。

## 5. 既知の問題 / 仕様から外れた判断
なし。仕様から外れた判断はない。

## 6. 人間に確認してほしい点
追加確認は不要。
"""


class FailurePatternPurgeTests(unittest.TestCase):
    def table_names(self, db: Path) -> set[str]:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {str(row[0]) for row in rows}
        finally:
            conn.close()

    def test_claude_review_instruction_does_not_inject_recurrence_prevention(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
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
                        "--id",
                        "task_review",
                        "--title",
                        "Claudeでレビューして",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "instruct", "--task", "task_review"])

            body = output.getvalue()
            self.assertNotIn("Recurrence prevention", body)
            self.assertNotIn("failure_mcp_review_cli_fallback", body)
            self.assertNotIn("## 今回必須の行動規則", body)
            self.assertNotIn("## 参考にする成功パターン", body)

            store = Store(db)
            instruction = store.latest_for_task("instructions", "task_review")
            store.close()
            self.assertEqual(instruction["applied_rule_ids"], [])
            self.assertNotIn("applied_failure_pattern_ids", instruction)
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_report_import_keeps_failure_logs_without_pattern_rows(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("passed", "TODO"), encoding="utf-8")

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
                        "--id",
                        "task_review",
                        "--title",
                        "Claudeでレビューして",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_review"])
                main(["--db", str(db), "report", "import", "--task", "task_review", "--file", str(report)])

            store = Store(db)
            failures = store.list_where("failure_logs", "task_id=?", ("task_review",))
            store.close()
            self.assertTrue(failures)
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_learning_schema_tables_are_not_created_for_new_store(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            try:
                self.assertEqual(store.list_where("failure_logs"), [])
            finally:
                store.close()
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_store_initializes_when_legacy_learning_tables_remain(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE derived_rules (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
                conn.execute("CREATE TABLE active_instruction_rules (id TEXT PRIMARY KEY, task_id TEXT NOT NULL)")
                conn.execute("CREATE TABLE failure_patterns (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
                conn.execute("CREATE TABLE task_failure_pattern_matches (id TEXT PRIMARY KEY, task_id TEXT NOT NULL)")
                conn.execute("CREATE TABLE success_patterns (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
                conn.commit()
            finally:
                conn.close()

            store = Store(db)
            try:
                self.assertEqual(store.list_where("failure_logs"), [])
            finally:
                store.close()

    def test_legacy_instruction_failure_pattern_ids_decode_as_json(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            try:
                store.insert(
                    "instructions",
                    {
                        "id": "instruction_legacy",
                        "task_id": "task_review",
                        "applied_rule_ids": [],
                        "degradation_mode": "normal",
                        "body_md": "body",
                        "report_format_md": "format",
                        "created_at": now_iso(),
                    },
                )
            finally:
                store.close()

            conn = sqlite3.connect(db)
            try:
                conn.execute("ALTER TABLE instructions ADD COLUMN applied_failure_pattern_ids TEXT NOT NULL DEFAULT '[]'")
                conn.execute(
                    "UPDATE instructions SET applied_failure_pattern_ids=? WHERE id=?",
                    ('["pattern_legacy"]', "instruction_legacy"),
                )
                conn.commit()
            finally:
                conn.close()

            store = Store(db)
            try:
                instruction = store.get("instructions", "instruction_legacy")
            finally:
                store.close()

            self.assertEqual(instruction["applied_failure_pattern_ids"], ["pattern_legacy"])

    def test_completion_is_blocked_by_missing_current_verification_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch(
                "nilo.cli_handlers.workflow.evaluate_evidence",
                return_value=("evidence_submitted", [], {"ok": True}),
            ):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_review",
                        "--title",
                        "Claudeでレビューして",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_review"])
                main(["--db", str(db), "report", "import", "--task", "task_review", "--file", str(report)])

            with self.assertRaisesRegex(SystemExit, "current verification run"):
                main(["--db", str(db), "task", "complete", "--task", "task_review", "--actor", "ai", "--reason", "done"])

    def test_current_verification_allows_ai_completion_without_recurrence_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch(
                "nilo.cli_handlers.workflow.evaluate_evidence",
                return_value=("evidence_submitted", [], {"ok": True}),
            ):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_review",
                        "--title",
                        "Claudeでレビューして",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_review"])
                main(["--db", str(db), "report", "import", "--task", "task_review", "--file", str(report)])

            store = Store(db)
            timestamp = now_iso()
            store.insert(
                "verification_runs",
                {
                    "id": "verification_passed",
                    "task_id": "task_review",
                    "evidence_check_id": "",
                    "source": "nilo_executed",
                    "command": "python -m unittest",
                    "cwd": str(root),
                    "stdout": "OK",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 120.0,
                    "git_head": "",
                    "metadata": {},
                    "started_at": timestamp,
                    "finished_at": timestamp,
                    "created_at": timestamp,
                },
            )
            store.close()

            snapshot = {"git_head": "", "git_diff_hash": "", "working_tree_dirty": False}
            with patch("nilo.task_logic.current_git_snapshot", return_value=snapshot), patch(
                "nilo.transitions.current_git_snapshot",
                return_value=snapshot,
            ):
                main(["--db", str(db), "task", "complete", "--task", "task_review", "--actor", "ai", "--reason", "done"])

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_review")
            store.close()
            self.assertIsNotNone(completion)


if __name__ == "__main__":
    unittest.main()
