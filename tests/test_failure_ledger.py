from __future__ import annotations

import io
import json
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
python -m pytest tests/test_failure_ledger.py
### テスト結果
passed
### 型チェック
未実行。型チェック環境が未定義のため。
### lint
未実行。lint環境が未定義のため。

## 4. 未実行の検証（理由を記載）
型チェックとlintはプロジェクト設定がないため未実行。

## 5. 既知の問題 / 仕様から外れた判断
なし。

## 6. 人間に確認してほしい点
追加確認は不要。
"""


class FailureLedgerTests(unittest.TestCase):
    def table_names(self, db: Path) -> set[str]:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {str(row[0]) for row in rows}
        finally:
            conn.close()

    def create_project_task(self, db: Path, task_id: str = "task_test", project_id: str = "project_test") -> None:
        with redirect_stdout(io.StringIO()):
            main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
            main(["--db", str(db), "task", "create", "--project", project_id, "--id", task_id, "--title", "Test task"])

    def insert_failure(
        self,
        db: Path,
        failure_id: str,
        *,
        project_id: str = "project_test",
        task_id: str = "task_test",
        category: str = "metadata_mismatch",
        severity: str = "high",
        status: str = "open",
        message: str | None = None,
        created_at: str | None = None,
    ) -> None:
        store = Store(db)
        try:
            store.insert(
                "failure_logs",
                {
                    "id": failure_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "report_id": "report_test",
                    "category": category,
                    "message": message or f"{category} message",
                    "severity": severity,
                    "source": "manual",
                    "actor": "human",
                    "related_id": "report_test",
                    "snapshot": {},
                    "status": status,
                    "resolved_at": "",
                    "resolved_by": "",
                    "resolution_note": "",
                    "created_at": created_at or now_iso(),
                },
            )
        finally:
            store.close()

    def test_legacy_learning_tables_are_not_recreated(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            store.close()
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_report_import_records_open_failure_logs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")
            self.create_project_task(db)

            with redirect_stdout(io.StringIO()), patch(
                "nilo.agent_report_import.evaluate_evidence",
                return_value=("failed", ["changed_files mismatch"], {"ok": False}),
            ), patch(
                "nilo.cli_handlers.workflow.evaluate_evidence",
                return_value=("failed", ["changed_files mismatch"], {"ok": False}),
            ):
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            reports = store.list_where("agent_reports", "task_id=?", ("task_test",))
            store.close()
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["status"], "open")
            self.assertEqual(failures[0]["source"], "report_import")
            self.assertEqual(failures[0]["actor"], "nilo")
            self.assertEqual(failures[0]["related_id"], reports[0]["id"])
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_outcome_rejected_records_human_failure_log(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "outcome", "reject", "--task", "task_test", "--reason", "not acceptable"])

            store = Store(db)
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            store.close()
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["category"], "human_rejected")
            self.assertEqual(failures[0]["severity"], "high")
            self.assertEqual(failures[0]["source"], "outcome_record")
            self.assertEqual(failures[0]["actor"], "human")
            self.assertEqual(failures[0]["status"], "open")

    def test_failure_list_filters(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.create_project_task(db, task_id="task_other", project_id="project_other")
            self.insert_failure(db, "failure_a", category="metadata_mismatch", severity="high")
            self.insert_failure(db, "failure_b", task_id="task_other", project_id="project_other", category="evidence_missing", severity="medium")
            self.insert_failure(db, "failure_c", category="metadata_mismatch", severity="low", status="ignored")

            output = io.StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "--db",
                        str(db),
                        "failure",
                        "list",
                        "--project",
                        "project_test",
                        "--task",
                        "task_test",
                        "--category",
                        "metadata_mismatch",
                        "--severity",
                        "high",
                        "--status",
                        "open",
                        "--limit",
                        "1",
                        "--json",
                    ]
                )
            failures = json.loads(output.getvalue())["failures"]
            self.assertEqual([failure["id"] for failure in failures], ["failure_a"])

    def test_failure_resolve_and_ignore_update_status(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_resolve")
            self.insert_failure(db, "failure_ignore")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "failure", "resolve", "failure_resolve", "--note", "fixed"])
                main(["--db", str(db), "failure", "ignore", "failure_ignore", "--note", "external"])

            store = Store(db)
            resolved = store.get("failure_logs", "failure_resolve")
            ignored = store.get("failure_logs", "failure_ignore")
            store.close()
            assert resolved is not None
            assert ignored is not None
            self.assertEqual(resolved["status"], "resolved")
            self.assertTrue(resolved["resolved_at"])
            self.assertEqual(resolved["resolved_by"], "human")
            self.assertEqual(resolved["resolution_note"], "fixed")
            self.assertEqual(ignored["status"], "ignored")
            self.assertTrue(ignored["resolved_at"])
            self.assertEqual(ignored["resolved_by"], "human")
            self.assertEqual(ignored["resolution_note"], "external")

    def test_failure_summary_counts_all_rows_but_recent_high_is_open(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_resolved_high", severity="high", status="resolved", created_at="2026-06-26T12:00:00+09:00")
            self.insert_failure(db, "failure_open_medium", severity="medium", category="evidence_missing", created_at="2026-06-26T11:00:00+09:00")
            self.insert_failure(db, "failure_open_high", severity="high", category="secret_detected", created_at="2026-06-26T10:00:00+09:00")

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "failure", "summary", "--project", "project_test", "--limit", "1", "--json"])
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary["open"], 2)
            self.assertEqual(summary["resolved"], 1)
            self.assertEqual(summary["by_severity"]["high"], 2)
            self.assertEqual([failure["id"] for failure in summary["recent_high_failures"]], [])

    def test_failure_show_outputs_extended_fields(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_show", message="detailed failure")

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "failure", "show", "failure_show"])
            body = output.getvalue()
            self.assertIn("source: manual", body)
            self.assertIn("actor: human", body)
            self.assertIn("related_id: report_test", body)
            self.assertIn("snapshot: {}", body)
            self.assertIn("resolution_note:", body)

    def test_doctor_ai_context_includes_failure_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_doctor")

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "doctor", "ai-context", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- open_failure_count: 1", body)
            self.assertIn("- high_open_failure_count: 1", body)
            self.assertIn("- failure_summary_chars:", body)

    def test_task_show_ai_includes_task_failures_only(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_other", "--title", "Other task"])
            self.insert_failure(db, "failure_target", task_id="task_test", category="human_rework_required", severity="medium")
            self.insert_failure(db, "failure_other", task_id="task_other", category="secret_detected", severity="high")

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "task", "show", "--task", "task_test", "--ai"])
            body = output.getvalue()
            self.assertIn("failure_logs:", body)
            self.assertIn("human_rework_required", body)
            self.assertIn("Failure logs are observations, not mandatory rules.", body)
            self.assertNotIn("secret_detected", body)

    def test_status_ai_includes_compact_failure_summary(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_one")
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "status", "--project", "project_test", "--ai"])
            body = output.getvalue()
            self.assertIn("failure_summary:", body)
            self.assertIn("- open_failures: 1", body)
            self.assertIn("Use `nilo failure list --project project_test` for details.", body)
            self.assertNotIn("metadata_mismatch message metadata_mismatch message", body)


if __name__ == "__main__":
    unittest.main()
