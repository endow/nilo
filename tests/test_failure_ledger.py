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
from nilo.failure import failure_fingerprint, fingerprint_shadow_report, record_failure_log
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

    def test_failure_fingerprint_uses_only_structured_classification(self) -> None:
        first = failure_fingerprint(
            source="Report Import",
            operation="Evidence Check",
            category="Metadata Mismatch",
            error_code="Changed Files Mismatch",
        )
        second = failure_fingerprint(
            source="report-import",
            operation="evidence/check",
            category="metadata_mismatch",
            error_code="changed_files_mismatch",
        )

        self.assertEqual(first, "v1:report_import:evidence_check:metadata_mismatch:changed_files_mismatch")
        self.assertEqual(first, second)

    def test_record_failure_fingerprint_ignores_variable_failure_values(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            store = Store(db)
            try:
                first = record_failure_log(
                    store,
                    "project_test",
                    "task_test",
                    "report_one",
                    "metadata_mismatch",
                    "changed_files mismatch at /tmp/one for task_one with token sk-secret-one",
                    "high",
                    source="report_import",
                    operation="evidence_check",
                    error_code="changed_files_mismatch",
                    related_id="task_one",
                    context={"check": "changed_files"},
                )
                second = record_failure_log(
                    store,
                    "project_test",
                    "task_test",
                    "report_two",
                    "metadata_mismatch",
                    "changed_files mismatch at /other/two for task_two with token sk-secret-two",
                    "high",
                    source="report_import",
                    operation="evidence_check",
                    error_code="changed_files_mismatch",
                    related_id="task_two",
                    context={"check": "changed_files"},
                )
            finally:
                store.close()

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotIn("task_one", first["fingerprint"])
        self.assertNotIn("tmp", first["fingerprint"])
        self.assertNotIn("secret", first["fingerprint"])

    def test_failure_schema_migrates_legacy_rows_without_guessing_fingerprint(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE failure_logs (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                  report_id TEXT, category TEXT NOT NULL, message TEXT NOT NULL,
                  severity TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',
                  related_id TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL DEFAULT '{}',
                  status TEXT NOT NULL DEFAULT 'open', resolved_at TEXT NOT NULL DEFAULT '',
                  resolved_by TEXT NOT NULL DEFAULT '', resolution_note TEXT NOT NULL DEFAULT '',
                  decision_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO failure_logs
                (id, project_id, task_id, report_id, category, message, severity, source,
                 actor, related_id, snapshot, status, resolved_at, resolved_by,
                 resolution_note, decision_note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "failure_legacy", "project_test", "task_test", "report_test",
                    "metadata_mismatch", "variable legacy message", "high", "report_import",
                    "nilo", "report_test", "{}", "open", "", "", "", "", now_iso(),
                ),
            )
            conn.commit()
            conn.close()

            store = Store(db)
            try:
                failure = store.get("failure_logs", "failure_legacy")
                columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(failure_logs)").fetchall()}
            finally:
                store.close()

        assert failure is not None
        self.assertTrue({"fingerprint", "operation", "error_code", "context", "preventability"} <= columns)
        self.assertEqual(failure["fingerprint"], "")
        self.assertEqual(failure["operation"], "")
        self.assertEqual(failure["error_code"], "")
        self.assertEqual(failure["context"], {})
        self.assertEqual(failure["preventability"], "unknown")
        self.assertEqual(failure["message"], "variable legacy message")

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
            self.assertEqual(failures[0]["operation"], "evidence_check")
            self.assertEqual(failures[0]["error_code"], "changed_files_mismatch")
            self.assertEqual(
                failures[0]["fingerprint"],
                "v1:report_import:evidence_check:metadata_mismatch:changed_files_mismatch",
            )
            self.assertEqual(failures[0]["context"], {"check": "evidence_check"})
            self.assertEqual(failures[0]["preventability"], "likely")
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
            self.assertEqual(failures[0]["operation"], "human_outcome")
            self.assertEqual(failures[0]["error_code"], "rejected")
            self.assertEqual(failures[0]["fingerprint"], "v1:outcome_record:human_outcome:human_rejected:rejected")
            self.assertEqual(failures[0]["context"], {"decision": "rejected"})

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
                main(["--db", str(db), "failure", "resolve", "failure_resolve", "--note", "fixed", "--by", "human", "--human-confirm", "--decision-note", "test human decision"])
                main(["--db", str(db), "failure", "ignore", "failure_ignore", "--note", "external", "--by", "human", "--human-confirm", "--decision-note", "test human decision"])

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
            self.assertEqual(resolved["decision_note"], "test human decision")
            self.assertEqual(ignored["status"], "ignored")
            self.assertTrue(ignored["resolved_at"])
            self.assertEqual(ignored["resolved_by"], "human")
            self.assertEqual(ignored["resolution_note"], "external")
            self.assertEqual(ignored["decision_note"], "test human decision")

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

    def test_fingerprint_shadow_report_groups_period_and_classification_quality(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_other", "--title", "Other"])
            store = Store(db)
            try:
                for task_id in ("task_test", "task_other"):
                    failure = record_failure_log(
                        store,
                        "project_test",
                        task_id,
                        "",
                        "metadata_mismatch",
                        f"variable message for {task_id}",
                        "high" if task_id == "task_test" else "medium",
                        source="report_import",
                        operation="evidence_check",
                        error_code="changed_files_mismatch",
                        context={"check": "evidence_check"},
                    )
                    store.update("failure_logs", failure["id"], {"created_at": "2026-07-10T00:00:00+09:00"})
                unspecified = record_failure_log(
                    store,
                    "project_test",
                    "task_test",
                    "",
                    "evidence_missing",
                    "unknown structured source",
                    "low",
                    source="report_import",
                )
                store.update("failure_logs", unspecified["id"], {"created_at": "2026-07-11T00:00:00+09:00"})
                self.insert_failure(db, "failure_empty", created_at="2026-07-12T00:00:00+09:00")
                self.insert_failure(db, "failure_outside", created_at="2026-06-01T00:00:00+09:00")
                report = fingerprint_shadow_report(
                    store,
                    project_id="project_test",
                    since="2026-07-01T00:00:00+09:00",
                    until="2026-08-01T00:00:00+09:00",
                )
            finally:
                store.close()

        self.assertEqual(report["total_failures"], 4)
        self.assertEqual(report["classified_count"], 2)
        self.assertEqual(report["empty_fingerprint_count"], 1)
        self.assertEqual(report["unspecified_count"], 1)
        self.assertEqual(report["classification_rate"], 0.5)
        self.assertEqual(report["groups"][0]["occurrence_count"], 2)
        self.assertEqual(report["groups"][0]["distinct_task_count"], 2)
        self.assertEqual(report["groups"][0]["severity"], {"high": 1, "medium": 1})
        self.assertEqual(len(report["groups"][0]["failure_ids"]), 2)
        self.assertFalse(report["groups"][0]["possible_collision"])

    def test_shadow_report_cli_is_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_empty")
            store = Store(db)
            try:
                before = {
                    table: store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("tasks", "todos", "failure_logs", "transition_events")
                }
            finally:
                store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "failure", "shadow-report", "--project", "project_test", "--json"])

            store = Store(db)
            try:
                after = {
                    table: store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                    for table in ("tasks", "todos", "failure_logs", "transition_events")
                }
            finally:
                store.close()

        body = json.loads(output.getvalue())
        self.assertEqual(body["total_failures"], 1)
        self.assertEqual(body["empty_fingerprint_count"], 1)
        self.assertEqual(before, after)

    def test_failure_show_outputs_extended_fields(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_show", message="detailed failure")

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "failure", "show", "failure_show"])
            body = output.getvalue()
            self.assertIn("発生元: manual", body)
            self.assertIn("記録者: human", body)
            self.assertIn("related_id: report_test", body)
            self.assertIn("snapshot: {}", body)
            self.assertIn("解決メモ:", body)

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
            self.assertIn("失敗ログ:", body)
            self.assertIn("人間による修正要求", body)
            self.assertIn("失敗ログは観測履歴であり、必須ルールではありません。", body)
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
            self.assertNotIn("failure_summary:", body)
            self.assertNotIn("detail_commands:", body)
            self.assertNotIn("nilo failure list --project project_test", body)
            self.assertNotIn("metadata_mismatch message metadata_mismatch message", body)

    def test_status_text_surfaces_use_japanese_labels_but_json_stays_machine_readable(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "status", "--project", "project_test"])
            status_body = status_output.getvalue()
            self.assertIn("状態", status_body)
            self.assertIn("次の作業", status_body)
            self.assertIn("証跡", status_body)

            ai_output = io.StringIO()
            with redirect_stdout(ai_output):
                main(["--db", str(db), "status", "--project", "project_test", "--ai"])
            ai_body = ai_output.getvalue()
            self.assertIn("active_task:", ai_body)
            self.assertIn("next_action:", ai_body)
            self.assertIn("latest_verification: status=missing", ai_body)
            self.assertNotIn("detail_commands:", ai_body)

            verbose_ai_output = io.StringIO()
            with redirect_stdout(verbose_ai_output):
                main(["--db", str(db), "status", "--project", "project_test", "--ai", "--verbose"])
            verbose_ai_body = verbose_ai_output.getvalue()
            self.assertIn("状態", verbose_ai_body)
            self.assertIn("次の作業", verbose_ai_body)
            self.assertIn("証跡", verbose_ai_body)
            self.assertIn("未提出 (missing)", verbose_ai_body)

            json_output = io.StringIO()
            with redirect_stdout(json_output):
                main(["--db", str(db), "status", "--project", "project_test", "--json"])
            data = json.loads(json_output.getvalue())
            self.assertTrue(data["compact"])
            self.assertEqual(data["active_task"]["status"], "planned")
            self.assertNotIn("状態", data)

    def test_failure_text_surfaces_use_japanese_labels_but_db_values_stay_english(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.create_project_task(db)
            self.insert_failure(db, "failure_japanese_labels", category="metadata_mismatch", severity="high", status="open")

            list_output = io.StringIO()
            with redirect_stdout(list_output):
                main(["--db", str(db), "failure", "list", "--project", "project_test"])
            list_body = list_output.getvalue()
            for label in ["失敗ログ", "重大度", "分類", "状態", "内容", "作成日時"]:
                self.assertIn(label, list_body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "failure", "summary", "--project", "project_test"])
            summary_body = summary_output.getvalue()
            for label in ["失敗ログ概要", "重大度別", "分類別"]:
                self.assertIn(label, summary_body)

            store = Store(db)
            try:
                failure = store.get("failure_logs", "failure_japanese_labels")
            finally:
                store.close()
            assert failure is not None
            self.assertEqual(failure["status"], "open")
            self.assertEqual(failure["severity"], "high")
            self.assertEqual(failure["category"], "metadata_mismatch")


if __name__ == "__main__":
    unittest.main()
