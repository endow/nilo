from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.cli import main
from nilo.failure import (
    SEED_FAILURE_PATTERNS,
    match_failure_patterns,
    recurrence_evidence_issues,
)
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


class FailurePatternTests(unittest.TestCase):
    def test_claude_review_instruction_matches_cli_fallback_pattern(self) -> None:
        task = {"title": "Claudeでレビューして", "description": "", "acceptance_criteria": [], "task_type": "review"}

        matches = match_failure_patterns(SEED_FAILURE_PATTERNS, task)

        self.assertIn("failure_mcp_review_cli_fallback", [pattern["id"] for pattern, _ in matches])

    def test_matched_pattern_injects_cli_fallback_ban_and_required_evidence(self) -> None:
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
            self.assertIn("failure_mcp_review_cli_fallback", body)
            self.assertIn("Do not invoke claude, claude -p, or any Claude CLI fallback", body)
            self.assertIn("Created review_request id, or a clear callable-tool-unavailable result.", body)

            store = Store(db)
            instruction = store.latest_for_task("instructions", "task_review")
            store.close()
            self.assertEqual(instruction["applied_failure_pattern_ids"], ["failure_mcp_review_cli_fallback"])

    def test_mcp_review_pattern_requires_review_request_or_unavailable_evidence(self) -> None:
        pattern = next(pattern for pattern in SEED_FAILURE_PATTERNS if pattern["id"] == "failure_mcp_review_cli_fallback")

        issues = recurrence_evidence_issues(REPORT, [pattern])

        self.assertIn(
            "recurrence prevention missing evidence (failure_mcp_review_cli_fallback): review_request id or callable-tool-unavailable result",
            issues,
        )

    def test_blocking_pattern_missing_required_evidence_is_not_completion_ready(self) -> None:
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
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "report", "import", "--task", "task_review", "--file", str(report)])

            self.assertIn("status: evidence_missing", output.getvalue())
            with self.assertRaises(SystemExit):
                main(["--db", str(db), "task", "complete", "--task", "task_review", "--actor", "ai", "--reason", "done"])

    def test_blocking_pattern_missing_required_evidence_blocks_completion_even_with_passing_verification(self) -> None:
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

            with self.assertRaisesRegex(SystemExit, "recurrence prevention rule"):
                main(["--db", str(db), "task", "complete", "--task", "task_review", "--actor", "ai", "--reason", "done"])

    def test_connectivity_pattern_result_requires_result_near_connectivity_check(self) -> None:
        pattern = next(pattern for pattern in SEED_FAILURE_PATTERNS if pattern["id"] == "failure_integration_before_connectivity_check")
        report = (
            "# 完了報告\n\n"
            "疎通確認コマンド: nilo mcp doctor\n\n"
            + ("詳細説明。" * 90)
            + "\n\n### テスト結果\npassed\n"
        )

        issues = recurrence_evidence_issues(report, [pattern])

        self.assertIn(
            "recurrence prevention missing evidence (failure_integration_before_connectivity_check): result of the connectivity check",
            issues,
        )

    def test_connectivity_pattern_accepts_result_near_connectivity_check(self) -> None:
        pattern = next(pattern for pattern in SEED_FAILURE_PATTERNS if pattern["id"] == "failure_integration_before_connectivity_check")
        report = "# 完了報告\n\n疎通確認コマンド: nilo mcp doctor\n疎通確認結果: exit_code=0\n"

        issues = recurrence_evidence_issues(report, [pattern])

        self.assertNotIn(
            "recurrence prevention missing evidence (failure_integration_before_connectivity_check): result of the connectivity check",
            issues,
        )

    def test_mcp_external_tool_task_matches_connectivity_pattern(self) -> None:
        task = {
            "title": "MCP reviewer 連携を実装する",
            "description": "",
            "acceptance_criteria": [],
            "task_type": "implementation",
        }

        matches = match_failure_patterns(SEED_FAILURE_PATTERNS, task)

        self.assertIn("failure_integration_before_connectivity_check", [pattern["id"] for pattern, _ in matches])

    def test_normal_task_does_not_inject_recurrence_prevention(self) -> None:
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
                        "task_docs",
                        "--title",
                        "READMEの誤字を修正する",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "instruct", "--task", "task_docs"])

            self.assertNotIn("Recurrence prevention", output.getvalue())


if __name__ == "__main__":
    unittest.main()
