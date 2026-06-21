from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.cli import main
from nilo.store import Store
from nilo.timeutil import now_iso


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
            matches = store.list_where("task_failure_pattern_matches", "task_id=?", ("task_review",))
            patterns = store.list_where("failure_patterns")
            instruction = store.latest_for_task("instructions", "task_review")
            store.close()
            self.assertEqual(matches, [])
            self.assertEqual(patterns, [])
            self.assertEqual(instruction["applied_rule_ids"], [])
            self.assertEqual(instruction["applied_failure_pattern_ids"], [])

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
            matches = store.list_where("task_failure_pattern_matches", "task_id=?", ("task_review",))
            patterns = store.list_where("failure_patterns")
            store.close()
            self.assertTrue(failures)
            self.assertEqual(matches, [])
            self.assertEqual(patterns, [])

    def test_pattern_schema_tables_remain_available(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                self.assertEqual(store.list_where("failure_patterns"), [])
                self.assertEqual(store.list_where("task_failure_pattern_matches"), [])
            finally:
                store.close()

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

            with patch(
                "nilo.task_logic.current_git_snapshot",
                return_value={"git_head": "", "git_diff_hash": "", "working_tree_dirty": False},
            ):
                main(["--db", str(db), "task", "complete", "--task", "task_review", "--actor", "ai", "--reason", "done"])

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_review")
            store.close()
            self.assertIsNotNone(completion)


if __name__ == "__main__":
    unittest.main()
