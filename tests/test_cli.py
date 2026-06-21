from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from nilo.cli import git_changed_files, handson_language, main
from nilo.cli_handlers.quality import parse_git_status_porcelain_z
from nilo.roadmap_render import render_human_roadmap_markdown
from nilo.review_dispatcher import find_executable
from nilo.store import Store
from nilo.timeutil import now_iso


REPORT = """# 完了報告

## 1. 実施内容
CLIフローを確認した。

## 2. 変更ファイル一覧
- src/nilo/cli.py

## 3. 実行した検証
### テストコマンド
python -m unittest
### テスト結果
3 passed
### 型チェック
未実行。型チェック設定がないため。
### lint
未実行。lint設定がないため。

## 4. 未実行の検証（理由を記載）
型チェックとlintは設定がないため未実行。

## 5. 既知の問題 / 仕様から外れた判断
なし。

## 6. 人間に確認してほしい点
追加確認は不要。
"""


def register_test_reviewer(db: Path, reviewer: str) -> None:
    store = Store(db)
    try:
        now = now_iso()
        store.insert(
            "review_reviewers",
            {
                "id": f"reviewer_{reviewer.replace('-', '_')}",
                "reviewer": reviewer,
                "status": "available",
                "capabilities": ["review"],
                "max_concurrent": 1,
                "metadata": {"test": True},
                "last_heartbeat_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
    finally:
        store.close()


class CliTests(unittest.TestCase):
    def test_handson_language_detects_japanese_locale(self) -> None:
        with patch("nilo.project_logic.locale.getlocale", return_value=("Japanese_Japan", "utf8")):
            self.assertEqual(handson_language(), "ja")
        with patch("nilo.project_logic.locale.getlocale", return_value=("en_US", "UTF-8")):
            self.assertEqual(handson_language(), "en")

    def test_daily_facade_start_status_next_check_done_and_reject(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "start", "日常タスク", "--acceptance", "facade が動く"])
                task_id = next(line.split(": ", 1)[1] for line in start_output.getvalue().splitlines() if line.startswith("task: "))

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "status", "--verbose"])
                self.assertIn(f"project: {project_id} (Nilo)", status_output.getvalue())
                self.assertIn(f"- {task_id} [planned] implementation 日常タスク", status_output.getvalue())

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next"])
                self.assertIn(f"task: {task_id}", next_output.getvalue())
                self.assertIn(f"run nilo instruct --task {task_id}", next_output.getvalue())

                check_output = io.StringIO()
                with redirect_stdout(check_output):
                    main(["--db", str(db), "check", "python --version"])
                self.assertIn("verification_run:", check_output.getvalue())
                self.assertIn("exit_code: 0", check_output.getvalue())

                done_output = io.StringIO()
                with redirect_stdout(done_output):
                    main(["--db", str(db), "done", "--reason", "facade smoke accepted"])
                self.assertIn("status: completed_by_user", done_output.getvalue())

                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "start", "差し戻し対象"])
                reject_output = io.StringIO()
                with redirect_stdout(reject_output):
                    main(["--db", str(db), "reject", "再作業が必要"])
                self.assertIn("status: rejected_by_user", reject_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_facade_start_requires_commitment_when_multiple_are_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    for index in range(2):
                        store.insert(
                            "roadmap_commitments",
                            {
                                "id": f"commitment_{index}",
                                "project_id": project_id,
                                "title": f"Commitment {index}",
                                "intent": "test",
                                "success_criteria": [f"criterion {index}"],
                                "non_goals": [],
                                "autonomy_scope": [],
                                "review_gates": [],
                                "evidence_policy": [],
                                "status": "accepted",
                                "accepted_by": "human",
                                "accepted_at": "2026-06-19T00:00:00+09:00",
                                "created_at": "2026-06-19T00:00:00+09:00",
                            },
                        )
                finally:
                    store.close()

                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "start", "曖昧なタスク"])
                self.assertIn("multiple accepted commitments", str(raised.exception))

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "start", "明示タスク", "--commitment", "commitment_1"])
                self.assertIn("task: ", start_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_overdrive_mode_records_run_cursor_and_gate_events(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Overdrive Commitment",
                            "intent": "test overdrive execution",
                            "success_criteria": ["overdrive can select cursor"],
                            "non_goals": [],
                            "autonomy_scope": ["AI may continue through approval gates"],
                            "review_gates": ["Final human review is required"],
                            "evidence_policy": ["Record detailed Overdrive evidence"],
                            "status": "accepted",
                            "accepted_by": "human",
                            "accepted_at": "2026-06-20T00:00:00+09:00",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                finally:
                    store.close()

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "start",
                            "Overdrive task",
                            "--project",
                            project_id,
                            "--commitment",
                            "commitment_test",
                            "--mode",
                            "overdrive",
                        ]
                    )
                task_id = next(line.split(": ", 1)[1] for line in start_output.getvalue().splitlines() if line.startswith("task: "))

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", task_id])
                self.assertIn("mode: overdrive", status_output.getvalue())

                run_output = io.StringIO()
                with redirect_stdout(run_output):
                    main(["--db", str(db), "run", "--project", project_id, "--overdrive"])
                body = run_output.getvalue()
                self.assertIn("mode: overdrive", body)
                self.assertIn(f"cursor_task_id: {task_id}", body)
                self.assertIn("approval_gates: bypassed", body)
                self.assertIn("safety_gates: retained", body)
                self.assertIn("final_human_review_checkpoint: required", body)

                store = Store(db)
                try:
                    runs = store.list_where("overdrive_runs", "project_id=?", (project_id,))
                    self.assertEqual(len(runs), 1)
                    self.assertEqual(runs[0]["cursor_task_id"], task_id)
                    self.assertEqual(runs[0]["summary_json"]["human_review_points"][0], "Review the final Overdrive report before closing the roadmap commitment.")
                    events = store.list_where("overdrive_events", "run_id=?", (runs[0]["id"],))
                    event_types = {event["event_type"] for event in events}
                    self.assertIn("approval_gate_bypassed", event_types)
                    self.assertIn("safety_gate_retained", event_types)
                    self.assertIn("cursor_selected", event_types)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_overdrive_summary_json_does_not_decode_other_summary_columns(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            try:
                store.insert(
                    "quality_reviews",
                    {
                        "id": "quality_test",
                        "task_id": "task_test",
                        "reviewer": "human",
                        "scores": {},
                        "summary": "42",
                        "issues": [],
                        "created_at": "2026-06-20T00:00:00+09:00",
                    },
                )
                store.insert(
                    "review_results",
                    {
                        "id": "review_result_test",
                        "task_id": "task_test",
                        "review_request_id": "review_request_test",
                        "reviewer": "claude-code",
                        "verdict": "approved",
                        "summary": "true",
                        "body_md": "# Review\n",
                        "created_at": "2026-06-20T00:00:00+09:00",
                    },
                )

                quality = store.get("quality_reviews", "quality_test")
                result = store.get("review_results", "review_result_test")
                self.assertEqual(quality["summary"], "42")
                self.assertIsInstance(quality["summary"], str)
                self.assertEqual(result["summary"], "true")
                self.assertIsInstance(result["summary"], str)
            finally:
                store.close()

    def test_report_facade_keeps_import_subcommand_compatibility(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "start", "報告タスク"])
                task_id = next(line.split(": ", 1)[1] for line in start_output.getvalue().splitlines() if line.startswith("task: "))

                report_body = REPORT.replace("- src/nilo/cli.py", "- none")
                facade_output = io.StringIO()
                with patch("sys.stdin", io.StringIO(report_body)), redirect_stdout(facade_output):
                    main(["--db", str(db), "report"])
                self.assertIn("status:", facade_output.getvalue())

                import_output = io.StringIO()
                with patch("sys.stdin", io.StringIO(report_body)), redirect_stdout(import_output):
                    main(["--db", str(db), "report", "import", "--task", task_id])
                self.assertIn("status:", import_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_todo_add_list_show_and_triage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["todo can become ready"],
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
                            "project_id": project_id,
                            "title": "Pending Commitment",
                            "intent": "test",
                            "success_criteria": ["pending must not start todo work"],
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

                add_output = io.StringIO()
                with redirect_stdout(add_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "add",
                            "--project",
                            project_id,
                            "--kind",
                            "follow_up",
                            "--description",
                            "後続で扱う",
                            "--acceptance-hint",
                            "list/show で見える",
                            "README の導線を見直す",
                        ]
                    )
                todo_id = next(line.split(": ", 1)[1] for line in add_output.getvalue().splitlines() if line.startswith("todo: "))
                self.assertIn("status: open", add_output.getvalue())

                list_output = io.StringIO()
                with redirect_stdout(list_output):
                    main(["--db", str(db), "todo", "list", "--project", project_id])
                self.assertIn(todo_id, list_output.getvalue())
                self.assertIn("follow_up", list_output.getvalue())

                show_output = io.StringIO()
                with redirect_stdout(show_output):
                    main(["--db", str(db), "todo", "show", "--item", todo_id])
                self.assertIn("status: open", show_output.getvalue())
                self.assertIn("acceptance_hint:", show_output.getvalue())

                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "todo", "triage", "--item", todo_id, "--status", "ready", "--reason", "範囲内"])
                self.assertIn("ready todo requires --commitment", str(raised.exception))

                with self.assertRaises(SystemExit) as pending:
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "triage",
                            "--item",
                            todo_id,
                            "--status",
                            "ready",
                            "--commitment",
                            "commitment_pending",
                            "--reason",
                            "未受理 commitment は不可",
                        ]
                    )
                self.assertIn("accepted roadmap commitment not found: commitment_pending", str(pending.exception))

                with self.assertRaises(SystemExit) as terminal:
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "triage",
                            "--item",
                            todo_id,
                            "--status",
                            "converted_to_task",
                            "--reason",
                            "terminal status は start 経由のみ",
                        ]
                    )
                self.assertIn("todo status is not triage-settable: converted_to_task", str(terminal.exception))

                triage_output = io.StringIO()
                with redirect_stdout(triage_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "triage",
                            "--item",
                            todo_id,
                            "--status",
                            "ready",
                            "--commitment",
                            "commitment_test",
                            "--reason",
                            "accepted commitment の範囲内",
                        ]
                    )
                self.assertIn("status: ready", triage_output.getvalue())
                self.assertIn("roadmap_commitment_id: commitment_test", triage_output.getvalue())

                ready_output = io.StringIO()
                with redirect_stdout(ready_output):
                    main(["--db", str(db), "todo", "list", "--project", project_id, "--status", "ready"])
                self.assertIn(todo_id, ready_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_todo_start_converts_ready_item_to_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["ready todo can start"],
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
                            "project_id": project_id,
                            "title": "Pending Commitment",
                            "intent": "test",
                            "success_criteria": ["pending must not start todo work"],
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
                    store.insert(
                        "todos",
                        {
                            "id": "todo_ready",
                            "project_id": project_id,
                            "title": "Ready work",
                            "kind": "follow_up",
                            "status": "ready",
                            "description": "Build the ready work.",
                            "acceptance_hint": "Task contains acceptance.",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "commitment_test",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "2026-06-20T00:00:00+09:00",
                            "triage_reason": "accepted commitment の範囲内",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_pending_commitment",
                            "project_id": project_id,
                            "title": "Pending commitment work",
                            "kind": "follow_up",
                            "status": "ready",
                            "description": "This must not start.",
                            "acceptance_hint": "",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "commitment_pending",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "2026-06-20T00:00:00+09:00",
                            "triage_reason": "pending commitment",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_open",
                            "project_id": project_id,
                            "title": "Open work",
                            "kind": "follow_up",
                            "status": "open",
                            "description": "",
                            "acceptance_hint": "",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "",
                            "triage_reason": "",
                        },
                    )
                finally:
                    store.close()

                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "todo", "start", "--item", "todo_open"])
                self.assertIn("todo is not startable: open", str(raised.exception))

                with self.assertRaises(SystemExit) as pending_start:
                    main(["--db", str(db), "todo", "start", "--item", "todo_pending_commitment"])
                self.assertIn("accepted roadmap commitment not found: commitment_pending", str(pending_start.exception))

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "todo", "start", "--item", "todo_ready", "--type", "documentation", "--risk", "low"])
                body = start_output.getvalue()
                task_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("task: "))
                self.assertIn("status: converted_to_task", body)
                self.assertIn(f"instruct: nilo instruct --task {task_id}", body)

                store = Store(db)
                try:
                    todo = store.get("todos", "todo_ready")
                    task = store.get("tasks", task_id)
                finally:
                    store.close()
                self.assertEqual(todo["status"], "converted_to_task")
                self.assertEqual(todo["converted_task_id"], task_id)
                self.assertEqual(task["title"], "Ready work")
                self.assertEqual(task["description"], "Build the ready work.")
                self.assertEqual(task["acceptance_criteria"], ["Task contains acceptance."])
                self.assertEqual(task["task_type"], "documentation")
                self.assertEqual(task["risk_level"], "low")
                self.assertEqual(task["roadmap_commitment_id"], "commitment_test")
            finally:
                os.chdir(previous_cwd)

    def test_todo_promote_converts_requires_roadmap_item_to_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "todos",
                        {
                            "id": "todo_requires_roadmap",
                            "project_id": project_id,
                            "title": "Large follow-up",
                            "kind": "roadmap_candidate",
                            "status": "requires_roadmap",
                            "description": "This needs policy and ordering.",
                            "acceptance_hint": "Roadmap proposal captures the follow-up.",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "2026-06-20T00:00:00+09:00",
                            "triage_reason": "複数 task と成功条件定義が必要",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_ready",
                            "project_id": project_id,
                            "title": "Ready work",
                            "kind": "follow_up",
                            "status": "ready",
                            "description": "",
                            "acceptance_hint": "",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "commitment_test",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "",
                            "triage_reason": "",
                        },
                    )
                finally:
                    store.close()

                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "todo", "promote", "--item", "todo_ready", "--to", "roadmap-proposal", "--reason", "needs roadmap"])
                self.assertIn("todo is not promotable: ready", str(raised.exception))

                promote_output = io.StringIO()
                with redirect_stdout(promote_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "promote",
                            "--item",
                            "todo_requires_roadmap",
                            "--to",
                            "roadmap-proposal",
                            "--reason",
                            "needs accepted roadmap scope",
                        ]
                    )
                body = promote_output.getvalue()
                revision_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("roadmap_revision: "))
                commitment_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("proposed_commitment: "))
                self.assertIn("status: superseded", body)

                store = Store(db)
                try:
                    todo = store.get("todos", "todo_requires_roadmap")
                    revision = store.get("roadmap_revisions", revision_id)
                    commitment = store.get("roadmap_commitments", commitment_id)
                finally:
                    store.close()
                self.assertEqual(todo["status"], "superseded")
                self.assertEqual(todo["roadmap_revision_id"], revision_id)
                self.assertEqual(revision["status"], "pending")
                self.assertEqual(revision["source_path"], "todo:todo_requires_roadmap")
                self.assertIn("# Large follow-up", revision["body_md"])
                self.assertEqual(commitment["status"], "pending")
                self.assertEqual(commitment["title"], "Large follow-up")
                self.assertEqual(commitment["success_criteria"], ["Roadmap proposal captures the follow-up."])
            finally:
                os.chdir(previous_cwd)

    def test_status_and_next_include_todo_summary_when_no_task_is_active(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["todo next works"],
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
                    for todo_id, status in (
                        ("todo_ready", "ready"),
                        ("todo_requires_roadmap", "requires_roadmap"),
                        ("todo_open", "open"),
                        ("todo_deferred", "deferred"),
                    ):
                        store.insert(
                            "todos",
                            {
                                "id": todo_id,
                                "project_id": project_id,
                                "title": todo_id,
                                "kind": "follow_up",
                                "status": status,
                                "description": "",
                                "acceptance_hint": "",
                                "priority": "normal",
                                "source_type": "user_message",
                                "source_task_id": "",
                                "roadmap_commitment_id": "commitment_test" if status == "ready" else "",
                                "roadmap_revision_id": "",
                                "converted_task_id": "",
                                "created_at": "2026-06-20T00:00:00+09:00",
                                "triaged_at": "2026-06-20T00:00:00+09:00",
                                "triage_reason": "test",
                            },
                        )
                finally:
                    store.close()

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "status", "--project", project_id, "--verbose"])
                status_body = status_output.getvalue()
                self.assertIn("todo:", status_body)
                self.assertIn("- ready: 1", status_body)
                self.assertIn("- requires_roadmap: 1", status_body)

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next", "--project", project_id])
                self.assertIn("nilo todo start --item todo_ready", next_output.getvalue())

                store = Store(db)
                try:
                    store.update("todos", "todo_ready", {"status": "converted_to_task"})
                finally:
                    store.close()
                promote_next_output = io.StringIO()
                with redirect_stdout(promote_next_output):
                    main(["--db", str(db), "next", "--project", project_id])
                self.assertIn(
                    "nilo todo promote --item todo_requires_roadmap --to roadmap-proposal",
                    promote_next_output.getvalue(),
                )

                store = Store(db)
                try:
                    store.update("todos", "todo_requires_roadmap", {"status": "superseded"})
                finally:
                    store.close()
                roadmap_next_output = io.StringIO()
                with redirect_stdout(roadmap_next_output):
                    main(["--db", str(db), "next", "--project", project_id])
                self.assertIn(
                    "no active task; ask the user for the next concrete task within the current roadmap",
                    roadmap_next_output.getvalue(),
                )
                self.assertNotIn("todo_open", roadmap_next_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_agent_install_updates_codex_managed_block(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            agents = root / "AGENTS.md"
            agents.write_text("# Existing\n\nKeep this.", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = agents.read_text(encoding="utf-8")
            self.assertIn("# Existing", body)
            self.assertIn("Keep this.", body)
            self.assertIn("<!-- BEGIN NILO MANAGED BLOCK -->", body)
            self.assertIn("表の入口（daily surface）", body)
            self.assertIn("nilo init", body)
            self.assertIn("nilo start", body)
            self.assertIn("nilo status", body)
            self.assertIn("nilo next", body)
            self.assertIn("nilo check", body)
            self.assertIn("nilo report", body)
            self.assertIn("nilo done", body)
            self.assertIn("nilo reject", body)
            self.assertIn("裏側の機能", body)
            self.assertIn("nilo status --project project_test", body)
            self.assertIn("nilo next --project project_test", body)
            self.assertIn("Nilo MCP が利用可能な場合は、作業開始前に", body)
            self.assertIn("現在地と次の許可 action を確認する", body)
            self.assertIn('get_agent_work_context(project_id="project_test")', body)
            self.assertIn('get_next_step(project_id="project_test")', body)
            self.assertIn("Nilo MCP が設定済みでも callable tool として見えない", body)
            self.assertIn("tool discovery / `tool_search`", body)
            self.assertIn("lazy loading を試す", body)
            self.assertIn("CLI fallback として実行", body)
            self.assertIn("current session にロードされていない", body)
            self.assertIn("先頭の next action だけに従う", body)
            self.assertIn("明確なユーザー依頼があり active task が無い場合", body)
            self.assertIn("ユーザーに Nilo 操作を依頼せず", body)
            self.assertIn("nilo start --project project_test", body)
            self.assertIn("task 作成は裏側の作業", body)
            self.assertIn("nilo instruct --task <task_id>", body)
            self.assertIn(".nilo/reports/<task_id>.md", body)
            self.assertIn('nilo check --task <task_id> "<command>"', body)
            self.assertIn("nilo report --task <task_id> --file .nilo/reports/<task_id>.md", body)
            self.assertIn("nilo report import", body)
            self.assertIn("submit_agent_report", body)
            self.assertIn("record_test_result", body)
            self.assertIn("request_task_review", body)
            self.assertIn("dispatch_review", body)
            self.assertIn("高レベル API", body)
            self.assertIn("review request 作成だけ", body)
            self.assertIn("AI エージェント間の作業依頼・レビュー依頼は Nilo MCP 経由だけ", body)
            self.assertIn("相手エージェントのローカル CLI やプロセス起動コマンドは直接実行しない", body)
            self.assertIn("AI 間依頼に必要な MCP tool が callable tool として見えない場合", body)
            self.assertIn("代替 CLI に逃げず", body)
            self.assertIn("next_actions", body)
            self.assertIn("先頭の next action だけを実行", body)
            self.assertIn("迷ったらコマンドを推測せず", body)
            self.assertIn("再度 status を報告して停止する", body)
            self.assertIn("human gate", body)
            self.assertIn("対応タスクなしに勝手に実装へ進まない", body)
            self.assertIn("明確なユーザー依頼がある場合は先に task を作成してから進める", body)
            self.assertIn("検証していない成果を検証済みまたは完了として報告しない", body)
            self.assertIn("ユーザーの明示指示なしに `nilo task complete` や `nilo roadmap close` を実行しない", body)
            self.assertIn("ユーザーの明示許可なしに `--commit` を使わない", body)
            self.assertIn("ユーザーの明示許可なしに `--force`", body)
            self.assertNotIn("## 全コマンド一覧", body)
            self.assertNotIn("nilo project export-handson --project project_test --file HANDOFF.md", body)
            self.assertNotIn("nilo task create --project project_test", body)
            self.assertNotIn("nilo roadmap import --project project_test --file docs/roadmap_proposal.md", body)
            self.assertNotIn("nilo roadmap assess", body)
            self.assertNotIn("nilo quality autoscore import", body)
            self.assertNotIn("nilo rules derive import", body)
            self.assertNotIn("--file reports/<task_id>.md", body)

    def test_agent_install_block_is_minimal_protocol_not_command_reference(self) -> None:
        verbose_reference_only_commands = [
            "nilo project export-handson",
            "nilo roadmap assess",
            "nilo outcome accept-with-concerns",
            "nilo quality autoscore import",
            "nilo rules derive import",
        ]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("## Nilo 必須プロトコル", body)
            self.assertIn("daily surface", body)
            self.assertIn("表の入口", body)
            self.assertIn("roadmap / review / quality / rules / MCP", body)
            self.assertIn("裏側の機能", body)
            self.assertIn("Nilo MCP が利用可能な場合は、作業開始前に", body)
            self.assertIn("現在地と次の許可 action を確認する", body)
            self.assertIn('get_agent_work_context(project_id="project_test")', body)
            self.assertIn("tool discovery / `tool_search`", body)
            self.assertIn("nilo status --project project_test", body)
            self.assertIn("nilo next --project project_test", body)
            self.assertIn("CLI fallback として実行", body)
            self.assertIn("先頭の next action だけに従う", body)
            self.assertIn("nilo start --project project_test", body)
            self.assertIn("task 作成は裏側の作業", body)
            self.assertIn("nilo instruct --task <task_id>", body)
            self.assertIn(".nilo/reports/<task_id>.md", body)
            self.assertIn("nilo check --task <task_id>", body)
            self.assertIn("nilo report --task <task_id>", body)
            self.assertIn("nilo report import", body)
            self.assertIn("next_actions", body)
            self.assertIn("nilo task complete", body)
            self.assertIn("nilo roadmap close", body)
            present = [command for command in verbose_reference_only_commands if command in body]
            self.assertEqual([], present)

    def test_agent_install_all_updates_codex_and_claude_code(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "all"])
            finally:
                os.chdir(previous_cwd)

            agents = (root / "AGENTS.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("nilo status --project project_test", agents)
            self.assertIn("nilo status --project project_test", claude)
            self.assertIn("nilo next --project project_test", agents)
            self.assertIn("nilo next --project project_test", claude)
            self.assertIn('get_agent_work_context(project_id="project_test")', agents)
            self.assertIn('get_agent_work_context(project_id="project_test")', claude)
            self.assertIn("tool discovery / `tool_search`", agents)
            self.assertIn("tool discovery / `tool_search`", claude)
            self.assertIn("submit_agent_report", claude)
            self.assertIn("record_test_result", claude)
            self.assertIn("request_task_review", claude)
            self.assertIn("dispatch_review", claude)
            self.assertIn("高レベル API", claude)
            self.assertIn("AI エージェント間の作業依頼・レビュー依頼は Nilo MCP 経由だけ", agents)
            self.assertIn("AI エージェント間の作業依頼・レビュー依頼は Nilo MCP 経由だけ", claude)
            self.assertIn("相手エージェントのローカル CLI やプロセス起動コマンドは直接実行しない", agents)
            self.assertIn("代替 CLI に逃げず", claude)
            self.assertIn("nilo instruct --task <task_id>", agents)
            self.assertIn("nilo instruct --task <task_id>", claude)
            self.assertIn("human gate", agents)
            self.assertIn("human gate", claude)

    def test_agent_install_claude_code_includes_reviewer_protocol(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            body = (root / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("## Nilo MCP Reviewer Protocol", body)
            self.assertIn("Before calling `claim_next_review`, call `register_reviewer`", body)
            self.assertLess(body.index("1. `register_reviewer`"), body.index("2. `claim_next_review`"))
            self.assertLess(body.index("2. `claim_next_review`"), body.index("4. `import_review_result`"))
            self.assertIn("reviewer-start` for Claude Code reviews. It is heartbeat-only", body)
            self.assertIn("Do not use `reviewer-worker --result-file` as a substitute", body)
            self.assertIn('"worker_path": "claude-code-mcp-session"', body)
            self.assertIn('"dispatch_capable": true', body)
            self.assertIn('"source": "real Claude Code session"', body)
            self.assertIn("A connected Nilo MCP server does not by itself make the reviewer available", body)

    def test_agent_install_codex_includes_reviewer_protocol(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("## Nilo MCP Reviewer Protocol", body)
            self.assertIn("When acting as the `codex` reviewer through Nilo MCP", body)
            self.assertIn('"reviewer": "codex"', body)
            self.assertIn('"worker_path": "codex-mcp-session"', body)
            self.assertIn('"dispatch_capable": true', body)
            self.assertIn('"source": "real Codex session"', body)
            self.assertIn("Do not use `reviewer-worker --result-file` as a substitute for Codex review", body)
            self.assertIn("A connected Nilo MCP server does not by itself make the reviewer available", body)

    def test_agent_install_claude_code_reviewer_protocol_does_not_duplicate_existing_section(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            claude = root / "CLAUDE.md"
            claude.write_text(
                "# Existing\n\n"
                "## Nilo MCP Reviewer Protocol\n\n"
                "old reviewer protocol\n\n"
                "## Keep\n\n"
                "Keep this section.\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "claude-code"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            body = claude.read_text(encoding="utf-8")
            self.assertEqual(body.count("## Nilo MCP Reviewer Protocol"), 1)
            self.assertNotIn("old reviewer protocol", body)
            self.assertIn("## Keep", body)
            self.assertIn("Keep this section.", body)
            self.assertIn('"worker_path": "claude-code-mcp-session"', body)

    def test_agent_install_replaces_existing_nilo_block_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            agents = root / "AGENTS.md"
            agents.write_text(
                "before\n\n"
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold block\n<!-- END NILO MANAGED BLOCK -->\n\n"
                "after\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = agents.read_text(encoding="utf-8")
            self.assertIn("before", body)
            self.assertIn("after", body)
            self.assertNotIn("old block", body)
            self.assertEqual(body.count("<!-- BEGIN NILO MANAGED BLOCK -->"), 1)

    def test_init_creates_project_from_current_folder_and_installs_agents(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            project = store.get("projects", "sample_project")
            store.close()
            self.assertIsNotNone(project)
            self.assertEqual(project["name"], "sample_project")
            self.assertIn("created project: sample_project", output.getvalue())
            self.assertIn("updated: AGENTS.md", output.getvalue())
            self.assertIn("updated: CLAUDE.md", output.getvalue())
            self.assertIn("updated: .gitignore", output.getvalue())
            agents = (root / "AGENTS.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("nilo status --project sample_project", agents)
            self.assertIn("nilo status --project sample_project", claude)
            self.assertIn("nilo next --project sample_project", agents)
            self.assertIn("Nilo MCP が利用可能な場合は、作業開始前に", agents)
            self.assertIn("現在地と次の許可 action を確認する", agents)
            self.assertIn("表の入口", agents)
            self.assertIn(".nilo/", gitignore)

    def test_init_is_repeatable_for_existing_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "init"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            projects = store.list_where("projects", "id=?", ("sample_project",))
            store.close()
            agents = (root / "AGENTS.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertEqual(len(projects), 1)
            self.assertIn("project exists: sample_project", output.getvalue())
            self.assertEqual(agents.count("<!-- BEGIN NILO MANAGED BLOCK -->"), 1)
            self.assertEqual(claude.count("<!-- BEGIN NILO MANAGED BLOCK -->"), 1)
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8").splitlines().count(".nilo/"), 1)

    def test_init_keeps_existing_nilo_gitignore_entry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            (root / ".gitignore").write_text("build/\n.nilo\n", encoding="utf-8")
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            gitignore = (root / ".gitignore").read_text(encoding="utf-8")
            self.assertNotIn("updated: .gitignore", output.getvalue())
            self.assertEqual(gitignore.splitlines().count(".nilo"), 1)
            self.assertEqual(gitignore.splitlines().count(".nilo/"), 0)

    def test_report_import_creates_evidence_check_and_rules(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- docs/roadmap_proposal.md"), encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            checks = store.list_where("evidence_checks", "task_id=?", ("task_test",))
            rules = store.list_where("derived_rules", "project_id=?", ("project_test",))
            store.close()

            self.assertEqual(checks[0]["status"], "needs_human_review")
            self.assertTrue(rules)

    def test_report_import_keeps_evidence_submitted_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- docs/roadmap_proposal.md"), encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            self.assertIn("status: evidence_submitted", output.getvalue())

    def test_task_list_shows_project_tasks_with_projected_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "project", "create", "Other", "--id", "project_other"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_other",
                        "--id",
                        "task_other",
                        "--title",
                        "別プロジェクトのタスク",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "list", "--project", "project_test"])

            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("task_test\tevidence_submitted\timplementation\tmedium\tCLIフローを確認する\t", lines[0])
            self.assertNotIn("task_other", output.getvalue())

    def test_project_status_shows_active_tasks_next_actions_and_verifications(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            db = Path(directory) / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

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
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                        "--type",
                        "implementation",
                        "--risk",
                        "medium",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_unverified",
                        "--title",
                        "未検証タスク",
                        "--type",
                        "design",
                        "--risk",
                        "low",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_unverified"])
                with patch(
                    "nilo.verification.working_tree_state",
                    return_value={"working_tree_dirty": False, "working_tree_files": [], "working_tree_available": True},
                ):
                    main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            body = output.getvalue()
            self.assertIn("project_id: project_test", body)
            self.assertIn("roadmap_position:", body)
            self.assertIn("work_state: acceptance review 待ち", body)
            self.assertIn("current_phase: implementation", body)
            self.assertIn("active_tasks:", body)
            self.assertIn("task_verified [verification_passed] implementation medium 検証済みタスク", body)
            self.assertIn("task_unverified [instruction_generated] design low 未検証タスク", body)
            self.assertIn("latest_verification_run: verification_", body)
            self.assertIn("next_actions:", body)
            self.assertIn("review the diff, reported changed files, verification output, and unresolved caveats", body)
            self.assertIn("nilo task complete --task task_verified --reason \"...\" --actor ai", body)
            self.assertIn("add --commit only when you want Nilo to commit the accepted changes", body)
            self.assertIn("unexecuted_verifications:", body)
            self.assertIn("task_unverified: verification run not recorded", body)

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_project_status_allows_clean_verification_task_completion_without_human_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            script = root / "verify.py"
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- 変更ファイルなし"), encoding="utf-8")
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch(
                "nilo.cli_handlers.workflow.evaluate_evidence",
                return_value=("evidence_submitted", [], {"ok": True}),
            ), patch(
                "nilo.verification.working_tree_state",
                return_value={"working_tree_dirty": False, "working_tree_files": [], "working_tree_available": True},
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
                        "task_verify",
                        "--title",
                        "検証タスク",
                        "--type",
                        "verification",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_verify", "--command", f'"{sys.executable}" "{script}"'])
                main(["--db", str(db), "report", "import", "--task", "task_verify", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            body = output.getvalue()
            self.assertIn("task_verify [evidence_submitted] verification medium 検証タスク", body)
            self.assertIn(
                'run nilo task complete --task task_verify --reason "verification evidence accepted" --actor ai',
                body,
            )
            self.assertNotIn("if accepted, run nilo task complete --task task_verify", body)

    def test_project_status_guides_roadmap_setup_when_no_work_is_active(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            handoff = root / "generated_handoff.md"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                    main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            status_body = status_output.getvalue()
            self.assertIn("roadmap_position: roadmap not configured; no open design residue detected", status_body)
            self.assertIn("next_actions:", status_body)
            self.assertIn("no active task; ask the user for the next concrete task or design direction", status_body)
            self.assertNotIn("nilo roadmap discuss --project project_test", status_body)
            self.assertNotIn("nilo roadmap import --project project_test", status_body)
            self.assertNotIn("nilo roadmap accept", status_body)
            self.assertNotIn("nilo roadmap export", status_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_discussion.md", status_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", status_body)
            self.assertNotIn("write a fresh RoadmapProposal to docs/roadmap_proposal.md", status_body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(summary_output.getvalue())
            self.assertIn("next_actions", summary)
            self.assertEqual(summary["next_actions"], ["no active task; ask the user for the next concrete task or design direction"])
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["action_id"], "wait_for_user_direction")
            self.assertNotIn("nilo roadmap", summary["roadmap_agent_next_actions"][0]["command_hint"])

            with redirect_stdout(io.StringIO()), patch("nilo.project_logic.handson_language", return_value="ja"):
                main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(handoff)])

            handoff_body = handoff.read_text(encoding="utf-8")
            self.assertIn("## 次のステップ", handoff_body)
            self.assertIn("no active task; ask the user for the next concrete task or design direction", handoff_body)
            self.assertNotIn("nilo roadmap discuss --project project_test", handoff_body)
            self.assertNotIn("nilo roadmap import --project project_test", handoff_body)
            self.assertNotIn("nilo roadmap accept", handoff_body)
            self.assertNotIn("nilo roadmap export", handoff_body)
            self.assertNotIn("edit the roadmap proposal, import it, then accept with nilo roadmap accept", handoff_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", handoff_body)
            self.assertNotIn("docs/roadmap_proposal.md", handoff_body)

    def test_project_summary_shows_counts_history_and_minimal_placeholders(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

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
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                        "--type",
                        "implementation",
                        "--risk",
                        "medium",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_unverified",
                        "--title",
                        "未検証タスク",
                        "--type",
                        "design",
                        "--risk",
                        "low",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_unverified"])
                main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test"])

            body = output.getvalue()
            self.assertIn("roadmap_position:", body)
            self.assertIn("work_state: acceptance review 待ち", body)
            self.assertIn("current_phase: implementation", body)
            self.assertIn("task_status_counts:", body)
            self.assertIn("- instruction_generated: 1", body)
            self.assertIn("- verification_passed: 1", body)
            self.assertIn("recent_history:", body)
            self.assertIn("task_unverified instruction", body)
            self.assertIn("active_tasks:", body)
            self.assertIn("task_verified [verification_passed] implementation medium 検証済みタスク", body)
            self.assertIn("unexecuted_verifications:", body)
            self.assertIn("task_unverified: verification run not recorded", body)
            self.assertIn("commit_mapping:", body)
            self.assertIn("task_verified [insufficient_git_metadata]", body)
            self.assertIn("task_unverified [unmapped]", body)
            self.assertIn("design_residue:", body)
            self.assertNotIn("docs/design.md 18.9 [resolved] implementation:", body)
            self.assertNotIn("project status の最小実装を追加する", body)
            self.assertNotIn("docs/design.md 18.5 [resolved] review:", body)
            self.assertNotIn("quality quick interactive review is implemented", body)
            self.assertNotIn("review import advanced natural language parsing is implemented", body)

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_project_summary_json_outputs_structured_summary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

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
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(output.getvalue())
            self.assertEqual(summary["project_id"], "project_test")
            self.assertIn("roadmap_position", summary)
            self.assertIn("work_state", summary)
            self.assertEqual(summary["work_state"], "acceptance review 待ち")
            self.assertIn("current_phase", summary)
            self.assertIn("task_status_counts", summary)
            self.assertIn("recent_history", summary)
            self.assertIn("event_id", summary["recent_history"][0])
            self.assertIn("active_tasks", summary)
            self.assertIn("unexecuted_verifications", summary)
            self.assertIn("commit_mapping", summary)
            self.assertIn("design_residue", summary)
            self.assertEqual(summary["task_status_counts"]["verification_passed"], 1)
            self.assertEqual(summary["active_tasks"][0]["id"], "task_verified")
            self.assertEqual(summary["commit_mapping"][0]["task_id"], "task_verified")
            self.assertIn(summary["commit_mapping"][0]["status"], {"insufficient_git_metadata", "mapped_candidate", "same_head"})
            self.assertIn("base_commit", summary["commit_mapping"][0])
            self.assertIn("latest_verification_head", summary["commit_mapping"][0])
            self.assertIn("commits", summary["commit_mapping"][0])
            self.assertEqual(summary["design_residue"], [])

    def test_project_summary_design_residue_reads_design_document(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(output.getvalue())
            self.assertEqual(summary["design_residue"], [])

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_roadmap_import_accept_status_and_project_summary_position(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Intent
project status からロードマップ現在地を読めるようにする。

## Success Criteria
- roadmap_position が accepted commitment を表示する
- work_state と current_phase を分離する

## Non Goals
- ガントチャートは扱わない

## Autonomy Scope
- task 分解
- テスト追加

## Review Gates
- success criteria の変更

## Evidence Policy
- VerificationRun を記録する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
            self.assertEqual(revision["status"], "pending")
            self.assertEqual(revision["source_path"], str(proposal))
            self.assertEqual(commitment["status"], "pending")
            self.assertEqual(commitment["title"], "Phase 2.5 Roadmap Projection")
            self.assertEqual(
                commitment["success_criteria"],
                ["roadmap_position が accepted commitment を表示する", "work_state と current_phase を分離する"],
            )
            store.close()

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            self.assertIn("accepted_commitments:", status_output.getvalue())
            self.assertIn("- none", status_output.getvalue())
            self.assertIn("pending_revisions:", status_output.getvalue())
            self.assertIn(revision_id, status_output.getvalue())
            self.assertIn(f"source_path: {proposal}", status_output.getvalue())

            project_status_output = io.StringIO()
            with redirect_stdout(project_status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            project_status_body = project_status_output.getvalue()
            self.assertIn(f"roadmap update pending ({revision_id}); ask the user whether to adopt the direction", project_status_body)
            self.assertNotIn(f"nilo roadmap accept --revision {revision_id}", project_status_body)
            self.assertNotIn(str(proposal), project_status_body)

            accept_output = io.StringIO()
            with redirect_stdout(accept_output):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "ロードマップ現在地を先に扱うため", "--actor", "ai"])
            self.assertIn("accepted_by: ai", accept_output.getvalue())

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            self.assertIn("accepted_commitments:", status_output.getvalue())
            self.assertIn("Phase 2.5 Roadmap Projection", status_output.getvalue())
            self.assertIn("pending_revisions:\n- none", status_output.getvalue())

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_position"], "accepted commitment: Phase 2.5 Roadmap Projection")
            self.assertEqual(summary["roadmap_commitments"][0]["title"], "Phase 2.5 Roadmap Projection")
            self.assertEqual(summary["roadmap_commitments"][0]["accepted_by"], "ai")
            self.assertEqual(summary["pending_roadmap_revisions"], [])

            roadmap_file = root / "ROADMAP.md"
            export_output = io.StringIO()
            with redirect_stdout(export_output), patch("nilo.project_logic.locale.getlocale", return_value=("en_US", "UTF-8")):
                main(["--db", str(db), "roadmap", "export", "--project", "project_test", "--file", str(roadmap_file)])
            self.assertIn(f"written: {roadmap_file}", export_output.getvalue())
            roadmap_body = roadmap_file.read_text(encoding="utf-8")
            self.assertNotIn(b"\r\n", roadmap_file.read_bytes())
            self.assertIn("# Roadmap", roadmap_body)
            self.assertIn("- Project: Nilo", roadmap_body)
            self.assertIn("### Phase 2.5 Roadmap Projection", roadmap_body)
            self.assertIn("- roadmap_position が accepted commitment を表示する", roadmap_body)
            self.assertNotIn("project_id:", roadmap_body)
            self.assertNotIn("commitment_", roadmap_body)
            self.assertNotIn("roadmap_rev_", roadmap_body)

            japanese_roadmap_file = root / "roadmap_ja.md"
            japanese_export_output = io.StringIO()
            with redirect_stdout(japanese_export_output), patch("nilo.project_logic.locale.getlocale", return_value=("Japanese_Japan", "utf8")):
                main(["--db", str(db), "roadmap", "export", "--project", "project_test", "--file", str(japanese_roadmap_file)])
            self.assertIn(f"written: {japanese_roadmap_file}", japanese_export_output.getvalue())
            japanese_body = japanese_roadmap_file.read_text(encoding="utf-8")
            self.assertIn("# ロードマップ", japanese_body)
            self.assertIn("- 今の方向: 採用済みのロードマップ項目: Phase 2.5 Roadmap Projection", japanese_body)
            self.assertIn("## 現在のロードマップ項目", japanese_body)
            self.assertIn("#### 成功条件", japanese_body)
            self.assertIn("## 次に確認すること", japanese_body)
            self.assertNotIn("# Roadmap", japanese_body)
            self.assertNotIn("project_id:", japanese_body)
            self.assertNotIn("RoadmapCommitment", japanese_body)
            self.assertNotIn("commitment_", japanese_body)
            self.assertNotIn("roadmap_rev_", japanese_body)

            handoff = root / "generated_handoff.md"
            with redirect_stdout(io.StringIO()), patch("nilo.project_logic.handson_language", return_value="ja"):
                main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(handoff)])
            handoff_body = handoff.read_text(encoding="utf-8")
            self.assertIn("承認済み RoadmapCommitment: Phase 2.5 Roadmap Projection", handoff_body)
            self.assertNotIn("accepted commitment: Phase 2.5 Roadmap Projection", handoff_body)

            with redirect_stdout(io.StringIO()), patch("nilo.project_logic.handson_language", return_value="en"):
                main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(handoff)])
            english_handoff = handoff.read_text(encoding="utf-8")
            self.assertIn("## Roadmap Position", english_handoff)
            self.assertIn("accepted commitment: Phase 2.5 Roadmap Projection", english_handoff)
            self.assertNotIn("承認済み RoadmapCommitment: Phase 2.5 Roadmap Projection", english_handoff)

    def test_human_roadmap_markdown_masks_internal_ids_without_free_text_rewrites(self) -> None:
        summary = {
            "project_id": "project_test",
            "project_name": "Nilo",
            "roadmap_position": "active task focus: Refactor task_scheduler commitment_123",
            "work_state": "acceptance review 待ち",
            "current_phase": "implementation",
            "roadmap_commitments": [],
            "pending_roadmap_revisions": [
                {"id": "roadmap_rev_123", "status": "pending", "proposed_commitment_id": "commitment_123"}
            ],
            "active_tasks": [
                {
                    "id": "task_123",
                    "status": "evidence_submitted",
                    "task_type": "implementation",
                    "title": "Refactor task_scheduler commitment_123",
                }
            ],
            "next_actions": [
                "review pending roadmap revision roadmap_rev_123 for commitment_123",
                "task_123: review dirty-tree verification metadata before accepting this task",
            ],
        }

        body = render_human_roadmap_markdown(summary, "ja")

        self.assertIn("- 今の方向: 進行中の作業: 実装", body)
        self.assertIn("- 実装の作業 (作業報告済み)", body)
        self.assertIn("未コミット差分を含む検証記録を確認してから作業を完了する", body)
        self.assertNotIn("task_123", body)
        self.assertNotIn("commitment_123", body)
        self.assertNotIn("roadmap_rev_123", body)
        self.assertNotIn("Refactor 作業_scheduler", body)

    def test_roadmap_adopt_imports_accepts_and_exports_in_one_step(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap_adopt.md"
            roadmap_file = root / "ROADMAP.md"
            proposal.write_text(
                """# One Step Roadmap Adoption

## Intent
Reduce roadmap transaction friction.

## Success Criteria
- adopt creates an accepted commitment
- adopt exports the human roadmap

## Review Gates
- close remains separate
- commit remains separate
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "--db",
                        str(db),
                        "roadmap",
                        "adopt",
                        "--project",
                        "project_test",
                        "--file",
                        str(proposal),
                        "--reason",
                        "human chose this direction",
                        "--roadmap-file",
                        str(roadmap_file),
                    ]
                )

            body = output.getvalue()
            revision_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("accepted_revision: "))
            commitment_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("accepted_commitment: "))
            self.assertIn("accepted_by: human", body)
            self.assertIn(f"written: {roadmap_file}", body)
            self.assertTrue(roadmap_file.exists())
            self.assertIn("One Step Roadmap Adoption", roadmap_file.read_text(encoding="utf-8"))

            store = Store(db)
            try:
                revision = store.get("roadmap_revisions", revision_id)
                commitment = store.get("roadmap_commitments", commitment_id)
                pending = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            finally:
                store.close()

            self.assertEqual(revision["status"], "accepted")
            self.assertEqual(revision["reason"], "human chose this direction")
            self.assertEqual(revision["source_path"], str(proposal))
            self.assertEqual(commitment["status"], "accepted")
            self.assertEqual(commitment["accepted_by"], "human")
            self.assertEqual(pending, [])

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "status", "--project", "project_test", "--verbose"])
            self.assertIn("roadmap: accepted commitment: One Step Roadmap Adoption", status_output.getvalue())

    def test_roadmap_adopt_rejects_discussion_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            discussion = root / "roadmap_discussion.md"
            roadmap_file = root / "ROADMAP.md"
            discussion.write_text(
                """# Roadmap Discussion Context

## Project

- project_id: project_test
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "roadmap",
                            "adopt",
                            "--project",
                            "project_test",
                            "--file",
                            str(discussion),
                            "--reason",
                            "human chose this direction",
                            "--roadmap-file",
                            str(roadmap_file),
                        ]
                    )

            store = Store(db)
            try:
                revisions = store.list_where("roadmap_revisions", "project_id=?", ("project_test",))
                commitments = store.list_where("roadmap_commitments", "project_id=?", ("project_test",))
            finally:
                store.close()

            self.assertIn("roadmap adopt rejected discussion context", str(raised.exception))
            self.assertEqual(revisions, [])
            self.assertEqual(commitments, [])
            self.assertFalse(roadmap_file.exists())

    def test_roadmap_import_rejects_discussion_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            discussion = root / "roadmap_discussion.md"
            discussion.write_text(
                """# Roadmap Discussion Context

## Project

- project_id: project_test
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(discussion)])

            self.assertIn("roadmap import rejected discussion context", str(raised.exception))

    def test_roadmap_import_reads_proposal_from_stdin_without_source_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = """# Optional Artifact Roadmap

## Intent
標準入力から RoadmapProposal を取り込めるようにする。

## Success Criteria
- stdin import が pending revision を作成する
- source_path は空になる

## Non Goals
- ROADMAP.md を廃止しない

## Autonomy Scope
- CLI import の検証

## Review Gates
- file workflow を壊さない

## Evidence Policy
- unit test を追加する
"""

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            import_output = io.StringIO()
            with redirect_stdout(import_output), patch("sys.stdin", io.StringIO(proposal)):
                main(["--db", str(db), "roadmap", "import", "--project", "project_test"])

            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
            store.close()

            self.assertEqual(revision["status"], "pending")
            self.assertEqual(revision["source_path"], "")
            self.assertEqual(revision["body_md"], proposal)
            self.assertEqual(commitment["title"], "Optional Artifact Roadmap")
            self.assertEqual(
                commitment["success_criteria"],
                ["stdin import が pending revision を作成する", "source_path は空になる"],
            )

    def test_roadmap_import_rejects_h2_only_proposal_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """## Summary

This malformed proposal has no top-level title.

## Success Criteria
- should not be imported
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            store = Store(db)
            revisions = store.list_where("roadmap_revisions", "project_id=?", ("project_test",))
            commitments = store.list_where("roadmap_commitments", "project_id=?", ("project_test",))
            store.close()
            self.assertIn("missing top-level # title", str(raised.exception))
            self.assertEqual(revisions, [])
            self.assertEqual(commitments, [])

    def test_roadmap_import_rejects_duplicate_accepted_commitment_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            first = root / "first.md"
            second = root / "second.md"
            first.write_text(
                """# Duplicate Roadmap

## Intent
最初の提案。

## Success Criteria
- 同じ成功条件
""",
                encoding="utf-8",
            )
            second.write_text(
                """# Follow-up Title

## Intent
同じ範囲の重複提案。

## Success Criteria
- 同じ成功条件
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(first)])
                first_revision = next(line.split(": ", 1)[1] for line in first_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                main(["--db", str(db), "roadmap", "accept", "--revision", first_revision, "--reason", "最初の承認"])

            with self.assertRaises(SystemExit) as raised:
                second_output = io.StringIO()
                with redirect_stdout(second_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(second)])

            store = Store(db)
            pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            store.close()
            self.assertEqual([], pending_revisions)
            self.assertIn("duplicate roadmap commitment detected before import", str(raised.exception))
            self.assertIn("[accepted]", str(raised.exception))

    def test_roadmap_import_rejects_duplicate_closed_commitment_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            first = root / "first.md"
            second = root / "second.md"
            first.write_text(
                """# Closed Roadmap

## Intent
完了済みの提案。

## Success Criteria
- 完了済みの成功条件
""",
                encoding="utf-8",
            )
            second.write_text(
                """# Another Title

## Intent
古い提案の再 import。

## Success Criteria
- 完了済みの成功条件
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(first)])
                first_revision = next(line.split(": ", 1)[1] for line in first_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                main(["--db", str(db), "roadmap", "accept", "--revision", first_revision, "--reason", "最初の承認"])

            store = Store(db)
            accepted = store.list_where("roadmap_commitments", "project_id=? AND status='accepted'", ("project_test",))[0]
            store.update("roadmap_commitments", accepted["id"], {"status": "closed", "closed_by": "ai", "closed_at": "2099-01-01T00:00:00+09:00", "closure_reason": "test"})
            store.close()

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(second)])

            store = Store(db)
            pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            store.close()
            self.assertEqual([], pending_revisions)
            self.assertIn("duplicate roadmap commitment detected before import", str(raised.exception))
            self.assertIn("[closed]", str(raised.exception))

    def test_roadmap_import_rejects_duplicate_rejected_commitment_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            first = root / "first.md"
            second = root / "second.md"
            first.write_text(
                """# Rejected Roadmap

## Intent
却下した提案。

## Success Criteria
- 却下済みの成功条件
""",
                encoding="utf-8",
            )
            second.write_text(
                """# Fresh Looking Title

## Intent
却下済み proposal の再 import。

## Success Criteria
- 却下済みの成功条件
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(first)])
                first_revision = next(line.split(": ", 1)[1] for line in first_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                main(["--db", str(db), "roadmap", "reject", "--revision", first_revision, "--reason", "却下する", "--actor", "ai"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(second)])

            store = Store(db)
            pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            store.close()
            self.assertEqual([], pending_revisions)
            self.assertIn("duplicate roadmap commitment detected before import", str(raised.exception))
            self.assertIn("[rejected]", str(raised.exception))

    def test_roadmap_discuss_warns_when_default_proposal_file_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            draft_dir = root / ".nilo" / "roadmap" / "project_test"
            draft_dir.mkdir(parents=True)
            (draft_dir / "roadmap_proposal.md").write_text("# Draft Proposal\n", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", "project_test", "--file", ".nilo/roadmap/project_test/roadmap_discussion.md"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("written: .nilo", output.getvalue())
            self.assertIn("warning: .nilo", output.getvalue())
            self.assertIn("roadmap_proposal.md already exists", output.getvalue())
            self.assertIn("fresh internal RoadmapProposal draft", output.getvalue())

    def test_roadmap_reject_removes_pending_revision_from_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Roadmap Revision Proposal: Rejectable Proposal

## Summary
誤 import した proposal。

## Proposed Changes
- reject される

## Rationale
不要な提案を取り下げるため。

## Success Criteria
- pending から消える

## Non Goals
- 削除しない

## Autonomy Scope
- CLI 更新

## Review Gates
- pending 表示に残らない

## Evidence Policy
- テストで確認する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))

            reject_output = io.StringIO()
            with redirect_stdout(reject_output):
                main(["--db", str(db), "roadmap", "reject", "--revision", revision_id, "--reason", "誤 import のため", "--actor", "ai"])
            self.assertIn(f"rejected_revision: {revision_id}", reject_output.getvalue())
            self.assertIn("rejected_by: ai", reject_output.getvalue())

            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
            store.close()
            self.assertEqual(revision["status"], "rejected")
            self.assertEqual(revision["reason"], "誤 import のため")
            self.assertEqual(revision["decided_by"], "ai")
            self.assertTrue(revision["accepted_at"])
            self.assertEqual(commitment["status"], "rejected")
            self.assertEqual(commitment["accepted_by"], "ai")
            self.assertTrue(commitment["accepted_at"])

            roadmap_status = io.StringIO()
            with redirect_stdout(roadmap_status):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            self.assertIn("pending_revisions:\n- none", roadmap_status.getvalue())
            self.assertNotIn(revision_id, roadmap_status.getvalue())

            project_status = io.StringIO()
            with redirect_stdout(project_status):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            self.assertNotIn(f"review pending roadmap revision {revision_id}", project_status.getvalue())

    def test_roadmap_reject_rejects_non_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Roadmap Revision Proposal: Accepted Proposal

## Summary
承認済みにする proposal。

## Proposed Changes
- accept される

## Rationale
non-pending reject を確認するため。

## Success Criteria
- reject できない

## Non Goals
- 取り消さない

## Autonomy Scope
- CLI 更新

## Review Gates
- accepted は reject できない

## Evidence Policy
- テストで確認する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "承認する", "--actor", "ai"])

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    main(["--db", str(db), "roadmap", "reject", "--revision", revision_id, "--reason", "取り下げる", "--actor", "ai"])

    def test_store_migrates_roadmap_revision_source_path_column(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE roadmap_revisions (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  proposed_commitment_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  body_md TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  accepted_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO roadmap_revisions
                (id, project_id, proposed_commitment_id, status, body_md, reason, accepted_at, created_at)
                VALUES ('roadmap_rev_old', 'project_test', 'commitment_old', 'pending', '# Old', '', '', '2026-01-01T00:00:00+00:00')
                """
            )
            conn.commit()
            conn.close()

            store = Store(db)
            revision = store.get("roadmap_revisions", "roadmap_rev_old")
            store.close()

            self.assertEqual(revision["source_path"], "")
            self.assertEqual(revision["decided_by"], "")

    def test_store_migrates_verification_run_source_column(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE projects (
                      id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      tech_stack TEXT NOT NULL,
                      rules TEXT NOT NULL,
                      default_completion_criteria TEXT NOT NULL,
                      available_models TEXT NOT NULL,
                      fallback_models TEXT NOT NULL,
                      requires_local_execution INTEGER NOT NULL,
                      created_at TEXT NOT NULL
                    );
                    CREATE TABLE verification_runs (
                      id TEXT PRIMARY KEY,
                      task_id TEXT NOT NULL,
                      evidence_check_id TEXT,
                      command TEXT NOT NULL,
                      cwd TEXT NOT NULL,
                      stdout TEXT NOT NULL,
                      stderr TEXT NOT NULL,
                      exit_code INTEGER,
                      timed_out INTEGER NOT NULL,
                      timeout_seconds REAL NOT NULL,
                      git_head TEXT,
                      metadata TEXT NOT NULL,
                      started_at TEXT NOT NULL,
                      finished_at TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    );
                    INSERT INTO verification_runs
                    (id, task_id, evidence_check_id, command, cwd, stdout, stderr, exit_code, timed_out, timeout_seconds, git_head, metadata, started_at, finished_at, created_at)
                    VALUES ('verification_old', 'task_old', NULL, 'python -m unittest', '.', 'ok', '', 0, 0, 300.0, 'abc', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:01+00:00')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            store = Store(db)
            try:
                run = store.get("verification_runs", "verification_old")
            finally:
                store.close()

            self.assertEqual(run["source"], "nilo_executed")

    def test_roadmap_discuss_outputs_and_writes_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            output_file = root / "roadmap_context.md"

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
                        "task_test",
                        "--title",
                        "ロードマップ表示を改善する",
                    ]
                )
                stdout_output = io.StringIO()
                with redirect_stdout(stdout_output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", "project_test"])
                file_output = io.StringIO()
                with redirect_stdout(file_output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", "project_test", "--file", str(output_file)])

            body = stdout_output.getvalue()
            self.assertIn("# Roadmap Discussion Context", body)
            self.assertIn("roadmap_position:", body)
            self.assertIn("work_state:", body)
            self.assertIn("## Accepted Commitments", body)
            self.assertIn("## Pending Revisions", body)
            self.assertIn("## Active Tasks", body)
            self.assertIn("task_test [planned] implementation medium ロードマップ表示を改善する", body)
            self.assertIn("## Unexecuted Verifications", body)
            self.assertIn("task_test: verification run not recorded", body)
            self.assertIn("## Design Residue", body)
            self.assertIn("## Requested Output", body)
            self.assertEqual(output_file.read_text(encoding="utf-8"), body)
            self.assertIn(f"written: {output_file}", file_output.getvalue())

    def test_roadmap_task_plan_requires_accepted_commitment_and_writes_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            output_file = root / "task_plan.md"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Intent
project status からロードマップ現在地を読めるようにする。

## Success Criteria
- roadmap_position が accepted commitment を表示する
- work_state と current_phase を分離する

## Review Gates
- success criteria の変更

## Evidence Policy
- VerificationRun を記録する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment_id = revision["proposed_commitment_id"]
            store.close()

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "task-plan", "--commitment", commitment_id])

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "タスク候補へ分解するため"])

            stdout_output = io.StringIO()
            with redirect_stdout(stdout_output):
                main(["--db", str(db), "roadmap", "task-plan", "--commitment", commitment_id])
            file_output = io.StringIO()
            with redirect_stdout(file_output):
                main(["--db", str(db), "roadmap", "task-plan", "--commitment", commitment_id, "--file", str(output_file)])

            body = stdout_output.getvalue()
            self.assertIn("# Roadmap Task Plan", body)
            self.assertIn("## Commitment", body)
            self.assertIn("## Review Boundaries", body)
            self.assertIn("success criteria の変更", body)
            self.assertIn("## Task Candidates", body)
            self.assertIn("### 1. Implement Phase 2.5 Roadmap Projection", body)
            self.assertIn("- type: implementation", body)
            self.assertIn("- risk: medium", body)
            self.assertIn("- description: project status からロードマップ現在地を読めるようにする。", body)
            self.assertIn("roadmap_position が accepted commitment を表示する", body)
            self.assertIn("### 2. Verify Phase 2.5 Roadmap Projection", body)
            self.assertIn("- type: verification", body)
            self.assertIn("nilo task create --project \"project_test\" --title \"Implement Phase 2.5 Roadmap Projection\"", body)
            self.assertIn(f"--commitment {commitment_id}", body)
            self.assertEqual(output_file.read_text(encoding="utf-8"), body)
            self.assertIn(f"written: {output_file}", file_output.getvalue())

    def test_roadmap_assess_summarizes_commitment_tasks_and_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            report = root / "report.md"
            script = root / "verify.py"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Intent
ロードマップ評価を確認する。

## Success Criteria
- accepted commitment の達成状況を確認できる
- 検証証跡を表示できる

## Evidence Policy
- VerificationRun を記録する
""",
                encoding="utf-8",
            )
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- docs/roadmap_proposal.md"), encoding="utf-8")
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "評価するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_assess",
                        "--title",
                        "Implement Phase 2.5 Roadmap Projection",
                        "--commitment",
                        commitment_id,
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_assess"])
                main(["--db", str(db), "report", "import", "--task", "task_assess", "--file", str(report)])
                main(["--db", str(db), "verification", "run", "--task", "task_assess", "--command", f'"{sys.executable}" "{script}"'])
                main(["--db", str(db), "task", "complete", "--task", "task_assess", "--reason", "human accepted evidence"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("# Roadmap Assessment", body)
            self.assertIn(f"### {commitment_id} Phase 2.5 Roadmap Projection", body)
            self.assertIn("- status: evidence_present", body)
            self.assertIn("- closure_ready: true", body)
            self.assertIn("- [evidence_present] accepted commitment の達成状況を確認できる", body)
            self.assertIn("related_tasks: task_assess", body)
            self.assertIn("latest_verification:", body)
            self.assertIn("(passed)", body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_assessments"][0]["status"], "evidence_present")
            self.assertTrue(summary["roadmap_assessments"][0]["closure_ready"])
            self.assertEqual(summary["roadmap_assessments"][0]["related_tasks"][0]["task_id"], "task_assess")
            self.assertEqual(summary["roadmap_agent_state"]["commitment_id"], commitment_id)
            self.assertEqual(summary["roadmap_agent_state"]["work_status"], "complete")
            self.assertEqual(summary["roadmap_agent_state"]["evidence_status"], "complete")
            self.assertEqual(summary["roadmap_agent_state"]["verification_status"], "complete")
            self.assertEqual(summary["roadmap_agent_state"]["closure_status"], "awaiting_closure")
            self.assertNotIn("close_roadmap_commitment", summary["roadmap_agent_state"]["ai_allowed_actions"])
            self.assertNotIn("draft_next_roadmap_proposal", summary["roadmap_agent_state"]["ai_allowed_actions"])
            self.assertIn("wait_for_user_direction", summary["roadmap_agent_state"]["ai_allowed_actions"])
            self.assertEqual(summary["roadmap_agent_state"]["ai_blocked_actions"], [])
            self.assertEqual(summary["roadmap_agent_state"]["recommended_next_action"], "wait_for_user_direction")
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["action_id"], "summarize_current_commitment")
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["actor"], "ai")
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["status"], "allowed")
            self.assertNotIn("nilo roadmap", summary["roadmap_agent_next_actions"][0]["command_hint"])
            self.assertIn("wait_for_user_direction", [item["action_id"] for item in summary["roadmap_agent_next_actions"]])
            self.assertEqual(summary["next_actions"], ["no active task; current roadmap scope is satisfied, ask the user for the next direction"])
            self.assertNotIn("close commitment", summary["next_actions"][0])
            self.assertNotIn("--actor ai", summary["next_actions"][0])
            self.assertNotIn(commitment_id, summary["next_actions"][0])
            self.assertNotIn("nilo roadmap discuss --project project_test", summary["next_actions"][0])
            self.assertNotIn("nilo roadmap import --project project_test", summary["next_actions"][0])
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", summary["next_actions"][0])
            self.assertNotIn(str(proposal), summary["next_actions"][0])
            self.assertNotIn("--file roadmap_proposal.md", summary["next_actions"][0])
            self.assertNotIn("run nilo roadmap assess", summary["next_actions"][0])

            text_summary_output = io.StringIO()
            with redirect_stdout(text_summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test"])
            text_summary_body = text_summary_output.getvalue()
            self.assertIn("closure_ready: true", text_summary_body)
            self.assertNotIn("roadmap_agent_state:", text_summary_body)
            self.assertNotIn("roadmap_agent_next_actions:", text_summary_body)
            self.assertNotIn("action_id: close_roadmap_commitment", text_summary_body)
            self.assertNotIn("--actor ai", text_summary_body)
            self.assertIn("no active task; current roadmap scope is satisfied, ask the user for the next direction", text_summary_body)

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            status_body = status_output.getvalue()
            self.assertNotIn("roadmap_agent_state:", status_body)
            self.assertNotIn("roadmap_agent_next_actions:", status_body)
            self.assertNotIn("action_id: close_roadmap_commitment", status_body)
            self.assertNotIn("nilo roadmap discuss --project project_test", status_body)
            self.assertNotIn("nilo roadmap import --project project_test", status_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", status_body)
            self.assertNotIn(str(proposal), status_body)
            self.assertNotIn("close commitment", status_body)
            self.assertNotIn("--actor ai", status_body)
            self.assertNotIn(commitment_id, status_body)
            self.assertIn("no active task; current roadmap scope is satisfied, ask the user for the next direction", status_body)
            self.assertNotIn("run nilo roadmap assess --project project_test for final human review", status_body)

            roadmap_status_output = io.StringIO()
            with redirect_stdout(roadmap_status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            roadmap_status_body = roadmap_status_output.getvalue()
            self.assertIn("roadmap_agent_state:", roadmap_status_body)
            self.assertIn("roadmap_agent_next_actions:", roadmap_status_body)
            self.assertIn("closure_status: awaiting_closure", roadmap_status_body)
            self.assertNotIn("action_id: close_roadmap_commitment", roadmap_status_body)
            self.assertNotIn("--actor ai", roadmap_status_body)
            self.assertIn("action_id: wait_for_user_direction", roadmap_status_body)

    def test_project_status_guides_task_plan_for_accepted_commitment_without_tasks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Success Criteria
- task 候補を作れる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "次へ進めるため"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            body = output.getvalue()
            self.assertIn("roadmap_position: accepted commitment: Phase 2.5 Roadmap Projection", body)
            self.assertNotIn("roadmap_agent_state:", body)
            self.assertNotIn("roadmap_agent_next_actions:", body)
            self.assertNotIn("action_id: create_tasks_from_commitment", body)
            self.assertNotIn(f"nilo roadmap task-plan --commitment {commitment_id}", body)
            self.assertNotIn(f"create tasks from accepted commitment {commitment_id}", body)
            self.assertIn("no active task; ask the user for the next concrete task within the current roadmap", body)

    def test_roadmap_close_marks_closure_ready_commitment_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            report = root / "report.md"
            script = root / "verify.py"
            proposal.write_text(
                """# Phase 3.4 Human Roadmap Closure Command

## Success Criteria
- close 済み commitment を表示できる
""",
                encoding="utf-8",
            )
            report.write_text(
                """# 完了報告

## 1. 実施内容
close 済み commitment を表示できるようにした。

## 2. 変更ファイル一覧
変更ファイルなし
""",
                encoding="utf-8",
            )
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "closure を確認するため"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_close", "--title", "Implement closure", "--commitment", commitment_id])
                main(["--db", str(db), "instruct", "--task", "task_close"])
                main(["--db", str(db), "report", "import", "--task", "task_close", "--file", str(report)])
                main(["--db", str(db), "verification", "run", "--task", "task_close", "--command", f'"{sys.executable}" "{script}"'])
                main(["--db", str(db), "task", "complete", "--task", "task_close", "--reason", "human accepted evidence"])

            close_output = io.StringIO()
            with redirect_stdout(close_output):
                main(["--db", str(db), "roadmap", "close", "--commitment", commitment_id, "--reason", "ai closed roadmap commitment", "--actor", "ai"])
            self.assertIn(f"closed_commitment: {commitment_id}", close_output.getvalue())
            self.assertIn("closed_by: ai", close_output.getvalue())

            roadmap_output = io.StringIO()
            with redirect_stdout(roadmap_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            roadmap_body = roadmap_output.getvalue()
            self.assertIn("accepted_commitments:", roadmap_body)
            self.assertIn("- none", roadmap_body)
            self.assertIn("closed_commitments:", roadmap_body)
            self.assertIn(f"- {commitment_id} Phase 3.4 Human Roadmap Closure Command", roadmap_body)
            self.assertIn("closure_reason: ai closed roadmap commitment", roadmap_body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_commitments"], [])
            self.assertEqual(summary["closed_roadmap_commitments"][0]["id"], commitment_id)
            self.assertEqual(summary["closed_roadmap_commitments"][0]["closed_by"], "ai")
            self.assertEqual(summary["closed_roadmap_commitments"][0]["closure_reason"], "ai closed roadmap commitment")
            self.assertTrue(summary["closed_roadmap_commitments"][0]["closed_at"])
            self.assertIsNone(summary["roadmap_agent_state"])

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            status_body = status_output.getvalue()
            self.assertNotIn("roadmap_agent_state:", status_body)
            self.assertIn("- none", status_body)
            self.assertNotIn("accepted commitment: Phase 3.4 Human Roadmap Closure Command", status_body)

    def test_roadmap_close_rejects_non_closure_ready_commitment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3.4 Human Roadmap Closure Command

## Success Criteria
- close は closure-ready だけ許可する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "closure rejection を確認するため"])

            with self.assertRaises(SystemExit) as raised:
                main(["--db", str(db), "roadmap", "close", "--commitment", commitment_id, "--reason", "too early"])
            self.assertIn("not closure-ready", str(raised.exception))

    def test_roadmap_assess_marks_related_test_detected_for_simple_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- diff-aware assessment を表示できる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_cli',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: evidence_present", body)
            self.assertIn("- closure_ready: true", body)
            self.assertIn("diff_verification: related_test_detected", body)
            self.assertIn("matched_tests: src/nilo/cli.py -> tests/test_cli.py", body)

    def test_roadmap_assess_marks_missing_related_test_as_human_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- missing test を human review に倒せる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_guard',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: needs_human_review", body)
            self.assertIn("- closure_ready: false", body)
            self.assertIn("diff_verification: needs_human_review", body)
            self.assertIn("missing_tests: src/nilo/cli.py -> tests/test_cli.py", body)

    def test_roadmap_summary_explains_diff_review_as_human_wait(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 4 Human-readable Status

## Success Criteria
- human summary を表示できる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "human summary を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 4 Human-readable Status",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_guard',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "summary", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("# 現在の状態", body)
            self.assertIn("次に判断すること", body)
            self.assertIn("テスト失敗ではありません", body)
            self.assertIn("人間確認待ち", body)
            self.assertIn("変更ファイルとテストコマンド", body)
            self.assertIn("task_diff", body)

    def test_roadmap_assess_treats_unittest_discover_start_dir_as_broad_suite(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- broad unittest discover を related test として扱える
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest discover -s tests',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: evidence_present", body)
            self.assertIn("- closure_ready: true", body)
            self.assertIn("diff_verification: related_test_detected", body)
            self.assertIn("matched_tests: src/nilo/cli.py -> tests/test_cli.py", body)

    def test_roadmap_assess_marks_unknown_test_mapping_as_human_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- unknown mapping を human review に倒せる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/__init__.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_cli',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: needs_human_review", body)
            self.assertIn("- closure_ready: false", body)
            self.assertIn("diff_verification: needs_human_review", body)
            self.assertIn("diff_reason: no simple source/test mapping for changed source files", body)
            self.assertIn("unknown_files: src/nilo/__init__.py", body)

    def test_project_summary_recent_history_includes_multiple_events_for_same_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "履歴を確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(output.getvalue())
            task_events = [item for item in summary["recent_history"] if item["task_id"] == "task_test"]
            event_names = {item["event"] for item in task_events}
            self.assertGreaterEqual(len(task_events), 3)
            self.assertIn("task_created", event_names)
            self.assertIn("instruction", event_names)
            self.assertIn("verification_run", event_names)
            self.assertTrue(all("event_id" in item for item in task_events))

    def test_project_summary_commit_mapping_uses_base_and_verification_head(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            commits = [{"hash": "abc123", "subject": "Implement mapping"}]
            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="base123"), patch(
                "nilo.verification.head_commit", return_value="head456"
            ), patch("nilo.project_logic.git_commit_log", return_value=commits):
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
                        "task_test",
                        "--title",
                        "コミット対応を確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                text_output = io.StringIO()
                with redirect_stdout(text_output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test"])
                json_output = io.StringIO()
                with redirect_stdout(json_output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            self.assertIn("task_test [mapped_candidate] base_commit=base123 latest_verification_head=head456", text_output.getvalue())
            self.assertIn("abc123 Implement mapping", text_output.getvalue())
            summary = json.loads(json_output.getvalue())
            mapping = summary["commit_mapping"][0]
            self.assertEqual(mapping["task_id"], "task_test")
            self.assertEqual(mapping["base_commit"], "base123")
            self.assertEqual(mapping["latest_verification_head"], "head456")
            self.assertEqual(mapping["commits"], commits)
            self.assertEqual(mapping["status"], "mapped_candidate")

    def test_project_summary_commit_mapping_marks_multiple_commits_ambiguous(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            commits = [
                {"hash": "abc123", "subject": "First commit"},
                {"hash": "def456", "subject": "Second commit"},
            ]

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="base123"), patch(
                "nilo.verification.head_commit", return_value="head456"
            ), patch("nilo.project_logic.git_commit_log", return_value=commits):
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
                        "task_test",
                        "--title",
                        "曖昧なコミット対応を確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            mapping = json.loads(output.getvalue())["commit_mapping"][0]
            self.assertEqual(mapping["status"], "ambiguous")
            self.assertEqual(mapping["commits"], commits)

    def test_project_summary_commit_mapping_marks_shared_range_ambiguous(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            commits = [{"hash": "abc123", "subject": "Shared commit"}]

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="base123"), patch(
                "nilo.verification.head_commit", return_value="head456"
            ), patch("nilo.project_logic.git_commit_log", return_value=commits):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                for task_id in ("task_one", "task_two"):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "create",
                            "--project",
                            "project_test",
                            "--id",
                            task_id,
                            "--title",
                            f"{task_id} の対応を確認する",
                        ]
                    )
                    main(["--db", str(db), "instruct", "--task", task_id])
                    main(["--db", str(db), "verification", "run", "--task", task_id, "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            mappings = json.loads(output.getvalue())["commit_mapping"]
            self.assertEqual({mapping["status"] for mapping in mappings}, {"ambiguous"})
            self.assertTrue(all(mapping["commits"] == commits for mapping in mappings))

    def test_project_export_handson_writes_compatible_markdown(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            output_file = root / "generated_handoff.md"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

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
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_unverified",
                        "--title",
                        "未検証タスク",
                        "--type",
                        "design",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_unverified"])
                main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                with patch("nilo.project_logic.handson_language", return_value="ja"):
                    main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(output_file)])

            body = output_file.read_text(encoding="utf-8")
            self.assertIn("# 作業進捗", body)
            self.assertIn("## ロードマップ現在地", body)
            self.assertIn("## 現在の作業状態", body)
            self.assertIn("acceptance review 待ち", body)
            self.assertIn("## 現在のフェーズ", body)
            self.assertIn("## 進行中タスク", body)
            self.assertIn("task_verified [verification_passed]", body)
            self.assertIn("task_unverified [instruction_generated]", body)
            self.assertIn("## 直近履歴", body)
            self.assertIn("## 未実行検証", body)
            self.assertIn("task_unverified: verification run not recorded", body)
            self.assertIn("## 次のステップ", body)
            self.assertIn("task_verified: 差分、変更ファイル一覧、検証結果、未解決事項を確認する", body)
            self.assertIn("task_verified: 承認する場合は task complete で完了を記録し、コミットも任せる場合だけ --commit を付ける", body)

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_task_create_records_description_and_acceptance(self) -> None:
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
                        "task_test",
                        "--title",
                        "詳細仕様を指示書に反映する",
                        "--description",
                        "タスク説明を保存する。",
                        "--description",
                        "複数行の説明を扱う。",
                        "--acceptance",
                        "指示書に説明が表示される",
                        "--acceptance",
                        "指示書に受け入れ条件が表示される",
                    ]
                )
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])
                instruct_output = io.StringIO()
                with redirect_stdout(instruct_output):
                    main(["--db", str(db), "instruct", "--task", "task_test"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["description"], "タスク説明を保存する。\n複数行の説明を扱う。")
            self.assertEqual(task["acceptance_criteria"], ["指示書に説明が表示される", "指示書に受け入れ条件が表示される"])
            self.assertIn("description:", status_output.getvalue())
            self.assertIn("acceptance_criteria:", status_output.getvalue())
            self.assertIn("## タスク説明\nタスク説明を保存する。", instruct_output.getvalue())
            self.assertIn("- 指示書に説明が表示される", instruct_output.getvalue())
            self.assertIn(".nilo/reports/task_test.md", instruct_output.getvalue())
            self.assertIn("nilo report import --task task_test --file .nilo/reports/task_test.md", instruct_output.getvalue())

    def test_task_update_updates_description_and_status_output(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "更新対象"])
            update_output = io.StringIO()
            with redirect_stdout(update_output):
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "update",
                        "--task",
                        "task_test",
                        "--description",
                        "更新後の説明",
                        "--description",
                        "追加行",
                    ]
                )
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["description"], "更新後の説明\n追加行")
            self.assertIn("updated: description", update_output.getvalue())
            self.assertIn("更新後の説明\n追加行", status_output.getvalue())

    def test_task_update_appends_acceptance_without_replacing_existing(self) -> None:
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
                        "task_test",
                        "--title",
                        "追記対象",
                        "--acceptance",
                        "既存条件",
                    ]
                )
                main(["--db", str(db), "task", "update", "--task", "task_test", "--append-acceptance", "追加条件"])
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["acceptance_criteria"], ["既存条件", "追加条件"])
            self.assertIn("- 既存条件", status_output.getvalue())
            self.assertIn("- 追加条件", status_output.getvalue())

    def test_task_update_replaces_acceptance_explicitly(self) -> None:
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
                        "task_test",
                        "--title",
                        "置換対象",
                        "--acceptance",
                        "古い条件",
                    ]
                )
                main(["--db", str(db), "task", "update", "--task", "task_test", "--acceptance", "新しい条件"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["acceptance_criteria"], ["新しい条件"])

    def test_task_update_rejects_missing_task_and_ambiguous_acceptance_mode(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "曖昧指定"])
            with self.assertRaises(SystemExit) as missing:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "update", "--task", "task_missing", "--description", "missing"])
            with self.assertRaises(SystemExit) as ambiguous:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "update",
                            "--task",
                            "task_test",
                            "--acceptance",
                            "置換",
                            "--append-acceptance",
                            "追記",
                        ]
                    )

            self.assertIn("task not found: task_missing", str(missing.exception))
            self.assertIn("use either --acceptance", str(ambiguous.exception))

    def test_latest_status_event_uses_deterministic_tie_break(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            created_at = "2026-06-16T00:00:00+00:00"
            store = Store(db)
            store.insert(
                "tasks",
                {
                    "id": "task_test",
                    "project_id": "project_test",
                    "title": "同時刻イベントを確認する",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "implementation",
                    "risk_level": "medium",
                    "requires_understanding_check": False,
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "base_commit": None,
                    "created_at": created_at,
                },
            )
            store.insert(
                "agent_reports",
                {
                    "id": "report_test",
                    "task_id": "task_test",
                    "agent": "test",
                    "claimed_status": "reported",
                    "changed_files": [],
                    "body_md": "body",
                    "created_at": created_at,
                },
            )
            store.insert(
                "evidence_checks",
                {
                    "id": "evidence_test",
                    "task_id": "task_test",
                    "report_id": "report_test",
                    "status": "evidence_submitted",
                    "issues": [],
                    "metadata": {},
                    "created_at": created_at,
                },
            )
            latest = store.latest_task_status_event("task_test")
            store.close()
            self.assertEqual(latest["source"], "evidence_check")
            self.assertEqual(latest["status"], "evidence_submitted")

    def test_latest_evidence_check_can_supersede_old_outcome(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "outcome", "reject", "--task", "task_test", "--reason", "差し戻し"])
                with patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                    main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            self.assertIn("status: evidence_submitted", output.getvalue())

    def test_rules_disable_marks_rule_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("3 passed", "TODO"), encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            rule = store.list_where("derived_rules", "project_id=?", ("project_test",))[0]
            store.close()

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "rules", "disable", "--rule", rule["id"]])

            store = Store(db)
            disabled = store.get("derived_rules", rule["id"])
            store.close()
            self.assertTrue(disabled["manually_disabled"])

    def test_rules_derive_prepare_outputs_agent_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("3 passed", "TODO"), encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "rules", "derive", "prepare", "--project", "project_test"])

            self.assertIn("# DerivedRule 生成指示", output.getvalue())
            self.assertIn("## FailureLog", output.getvalue())
            self.assertIn("task_id: task_test", output.getvalue())
            self.assertIn("## Rule", output.getvalue())

    def test_rules_derive_import_records_agent_rule(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            rules = root / "rules.md"
            report.write_text(REPORT.replace("3 passed", "TODO"), encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            failure = store.list_where("failure_logs", "project_id=?", ("project_test",))[0]
            store.close()
            rules.write_text(
                "# DerivedRules\n\n"
                "## Rule\n"
                f"source_failures: {failure['id']}\n"
                "rule: 完了報告には検証ログの実行結果を具体的に記載する\n"
                "tags: #evidence, #testing\n"
                "severity: high\n"
                "confidence: 0.8\n",
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "rules", "derive", "import", "--project", "project_test", "--file", str(rules)])

            store = Store(db)
            imported = [
                rule
                for rule in store.list_where("derived_rules", "project_id=?", ("project_test",))
                if rule["source"] == "agent_import"
            ][0]
            store.close()
            self.assertIn("imported_rules: 1", output.getvalue())
            self.assertEqual(imported["source_failure_ids"], [failure["id"]])
            self.assertEqual(imported["tags"], ["#evidence", "#testing"])
            self.assertEqual(imported["severity"], "high")
            self.assertEqual(imported["confidence"], 0.8)
            self.assertTrue(imported["auto_activated"])

    def test_rules_derive_import_rejects_unknown_source_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            rules = root / "rules.md"
            rules.write_text(
                "# DerivedRules\n\n"
                "## Rule\n"
                "source_failures: failure_missing\n"
                "rule: 完了報告には検証ログの実行結果を具体的に記載する\n"
                "tags: #evidence\n"
                "severity: medium\n"
                "confidence: 0.6\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "rules", "derive", "import", "--project", "project_test", "--file", str(rules)])
            self.assertIn("unknown source failures: failure_missing", str(raised.exception))

    def test_successful_reports_move_applied_rule_to_cooling_down(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            bad_report = root / "bad_report.md"
            report.write_text(REPORT, encoding="utf-8")
            bad_report.write_text(REPORT.replace("3 passed", "TODO"), encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "テストログ必須のCLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(bad_report)])
                main(["--db", str(db), "instruct", "--task", "task_test"])
                with patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                    for _ in range(5):
                        main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            rules = store.list_where("derived_rules", "project_id=?", ("project_test",))
            store.close()
            testing_rule = next(rule for rule in rules if "#testing" in rule["tags"])
            self.assertEqual(testing_rule["success_count"], 5)
            self.assertEqual(testing_rule["state"], "cooling_down")

    def test_task_create_records_type_and_risk(self) -> None:
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
                        "task_test",
                        "--title",
                        "仕様を調査する",
                        "--type",
                        "research",
                        "--risk",
                        "high",
                        "--requires-understanding-check",
                    ]
                )

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["task_type"], "research")
            self.assertEqual(task["risk_level"], "high")
            self.assertEqual(task["requires_understanding_check"], 1)

    def test_base_commit_is_recorded_at_instruct_time(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="create_head"):
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            store = Store(db)
            created_task = store.get("tasks", "task_test")
            store.close()
            self.assertIsNone(created_task["base_commit"])

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="instruct_head"):
                main(["--db", str(db), "instruct", "--task", "task_test"])

            store = Store(db)
            instructed_task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(instructed_task["base_commit"], "instruct_head")

    def test_outcome_accept_records_human_decision(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "outcome",
                        "accept-with-concerns",
                        "--task",
                        "task_test",
                        "--reason",
                        "証跡は揃っているが文言は後で見直す",
                        "--concern",
                        "エラーメッセージの統一感が弱い",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            outcome = store.latest_for_task("outcome_reviews", "task_test")
            store.close()
            self.assertEqual(outcome["decision"], "accepted_with_concerns")
            self.assertEqual(outcome["concerns"], ["エラーメッセージの統一感が弱い"])
            self.assertIn("status: accepted_with_concerns", output.getvalue())

    def test_outcome_reject_connects_to_failure_rule(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "outcome",
                        "reject",
                        "--task",
                        "task_test",
                        "--reason",
                        "仕様意図と異なる実装になっている",
                    ]
                )

            store = Store(db)
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            rules = store.list_where("derived_rules", "project_id=?", ("project_test",))
            store.close()
            self.assertEqual(failures[0]["category"], "human_rejected")
            self.assertTrue(rules)

    def test_quality_quick_records_human_summary(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "quick",
                        "--task",
                        "task_test",
                        "--summary",
                        "仕様には合っているが既存設計との統一感は弱い",
                        "--issue",
                        "エラー文言定義と統一されていない",
                    ]
                )

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["reviewer"], "human")
            self.assertEqual(review["scores"], {})
            self.assertEqual(review["issues"], ["エラー文言定義と統一されていない"])

    def test_quality_quick_records_scores(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                quality_output = io.StringIO()
                with redirect_stdout(quality_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--score",
                            "scope_control=5",
                        ]
                    )
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["scores"], {"requirement_fit": 4, "scope_control": 5})
            self.assertIn("quality_scores:", quality_output.getvalue())
            self.assertIn("- requirement_fit: 4", quality_output.getvalue())
            self.assertIn("latest_quality_review:", status_output.getvalue())
            self.assertIn("- scope_control: 5", status_output.getvalue())

    def test_quality_quick_interactive_records_review(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                with patch(
                    "builtins.input",
                    side_effect=["対話レビューで記録した", "懸念がひとつある", "", "requirement_fit=4", ""],
                ):
                    main(["--db", str(db), "quality", "quick", "--task", "task_test", "--interactive"])

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["summary"], "対話レビューで記録した")
            self.assertEqual(review["issues"], ["懸念がひとつある"])
            self.assertEqual(review["scores"], {"requirement_fit": 4})

    def test_quality_quick_rejects_empty_score_key(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "=4",
                        ]
                    )
            self.assertIn("score key must not be empty", str(raised.exception))

    def test_quality_quick_strict_scores_rejects_missing_required_score(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--required-score",
                            "requirement_fit",
                            "--required-score",
                            "scope_control",
                            "--strict-scores",
                        ]
                    )
            self.assertIn("missing required quality scores: scope_control", str(raised.exception))

    def test_quality_quick_required_scores_are_non_strict_by_default(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "quick",
                        "--task",
                        "task_test",
                        "--summary",
                        "品質は概ね良い",
                        "--score",
                        "requirement_fit=4",
                        "--required-score",
                        "scope_control",
                    ]
                )

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["scores"], {"requirement_fit": 4})

    def test_quality_schema_set_lists_required_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                        "--required-score",
                        "scope_control",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "quality", "schema", "list", "--project", "project_test"])

            store = Store(db)
            schema = store.get("quality_score_schemas", "project_test")
            store.close()
            self.assertEqual(schema["required_scores"], ["requirement_fit", "scope_control"])
            self.assertIn("- requirement_fit", output.getvalue())
            self.assertIn("- scope_control", output.getvalue())

    def test_quality_schema_set_replaces_existing_required_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "scope_control",
                    ]
                )

            store = Store(db)
            schemas = store.list_where("quality_score_schemas", "project_id=?", ("project_test",))
            store.close()
            self.assertEqual(len(schemas), 1)
            self.assertEqual(schemas[0]["required_scores"], ["scope_control"])

    def test_quality_quick_strict_scores_uses_project_schema(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                        "--required-score",
                        "scope_control",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--strict-scores",
                        ]
                    )
            self.assertIn("missing required quality scores: scope_control", str(raised.exception))

    def test_quality_quick_strict_scores_combines_schema_and_command_required_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--required-score",
                            "scope_control",
                            "--strict-scores",
                        ]
                    )
            self.assertIn("missing required quality scores: scope_control", str(raised.exception))

    def test_quality_autoscore_prepare_outputs_agent_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "採点プロンプトを生成する",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "quality", "autoscore", "prepare", "--task", "task_test"])

            self.assertIn("# Quality Autoscore 指示", output.getvalue())
            self.assertIn("採点プロンプトを生成する", output.getvalue())
            self.assertIn("- requirement_fit", output.getvalue())

    def test_quality_autoscore_import_validates_schema_and_records_scores(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            autoscore = root / "autoscore.md"
            autoscore.write_text(
                "# QualityReview\n\n"
                "## Summary\n採点結果は良好。\n\n"
                "## Scores\nrequirement_fit: 4\nscope_control: 5\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                        "--required-score",
                        "scope_control",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "採点結果を取り込む",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "autoscore",
                        "import",
                        "--task",
                        "task_test",
                        "--file",
                        str(autoscore),
                        "--strict-scores",
                    ]
                )

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["reviewer"], "ai_autoscore")
            self.assertEqual(review["scores"], {"requirement_fit": 4, "scope_control": 5})

    def test_quality_autoscore_import_rejects_unknown_score_when_schema_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            autoscore = root / "autoscore.md"
            autoscore.write_text("# QualityReview\n\n## Summary\n採点。\n\n## Scores\nunknown_axis: 4\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "採点結果を取り込む",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "autoscore",
                            "import",
                            "--task",
                            "task_test",
                            "--file",
                            str(autoscore),
                        ]
                    )
            self.assertIn("unknown quality scores: unknown_axis", str(raised.exception))

    def test_success_add_records_pattern(self) -> None:
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
                        "task_test",
                        "--title",
                        "仕様を調査する",
                        "--type",
                        "research",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "success",
                        "add",
                        "--project",
                        "project_test",
                        "--task",
                        "task_test",
                        "--pattern",
                        "影響範囲が不明な修正では先にresearchタスクを作る",
                        "--tag",
                        "#research_first",
                        "--type",
                        "implementation",
                    ]
                )

            store = Store(db)
            patterns = store.list_where("success_patterns", "project_id=?", ("project_test",))
            store.close()
            self.assertEqual(patterns[0]["source_task_ids"], ["task_test"])
            self.assertEqual(patterns[0]["tags"], ["#research_first"])
            self.assertEqual(patterns[0]["applicable_task_types"], ["implementation"])
            self.assertEqual(patterns[0]["state"], "active")

    def test_success_pattern_is_injected_into_instruction(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "success",
                        "add",
                        "--project",
                        "project_test",
                        "--pattern",
                        "影響範囲が不明な修正では先にresearchタスクを作る",
                        "--type",
                        "implementation",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを実装する",
                        "--type",
                        "implementation",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "instruct", "--task", "task_test"])

            self.assertIn("## 参考にする成功パターン", output.getvalue())
            self.assertIn("影響範囲が不明な修正では先にresearchタスクを作る", output.getvalue())

    def test_task_split_creates_subtasks(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを改善する",
                        "--risk",
                        "high",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "task", "split", "--task", "task_test"])

            store = Store(db)
            subtasks = store.list_where("tasks", "parent_task_id=?", ("task_test",))
            store.close()
            task_types = {task["task_type"] for task in subtasks}
            self.assertEqual(len(subtasks), 4)
            self.assertEqual(task_types, {"research", "design", "implementation", "verification"})
            implementation = next(task for task in subtasks if task["task_type"] == "implementation")
            self.assertEqual(implementation["requires_understanding_check"], 1)
            self.assertIn("Generated subtasks:", output.getvalue())

    def test_task_complete_records_final_human_completion(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "complete",
                        "--task",
                        "task_test",
                        "--reason",
                        "人間が成果物を確認して完了と判断した",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertEqual(completion["actor"], "human")
            self.assertEqual(completion["reason"], "人間が成果物を確認して完了と判断した")
            self.assertIn("status: completed_by_user", output.getvalue())

    def test_task_complete_rejects_ai_completion_without_evidence(self) -> None:
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
                        "task_test",
                        "--title",
                        "AI 完了を確認する",
                    ]
                )

            with self.assertRaises(SystemExit):
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "AI が検証済み成果物を受け入れた",
                            "--actor",
                            "ai",
                        ]
                    )

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertIsNone(completion)

    def test_task_complete_can_record_ai_completion_after_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
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
                        "task_test",
                        "--title",
                        "AI 完了を確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "AI が検証済み成果物を受け入れた",
                            "--actor",
                            "ai",
                        ]
                    )
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertEqual(completion["actor"], "ai")
            self.assertEqual(completion["reason"], "AI が検証済み成果物を受け入れた")
            self.assertIn("status: completed_by_ai", output.getvalue())
            self.assertIn("completed_by: ai", output.getvalue())
            self.assertIn("status: completed_by_ai", status_output.getvalue())

    def test_task_complete_can_record_ai_completion_after_successful_verification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "AI 完了を検証で許可する",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "AI が成功した検証を確認した",
                            "--actor",
                            "ai",
                        ]
                    )

            self.assertIn("status: completed_by_ai", output.getvalue())

    def test_task_complete_does_not_auto_regenerate_handoff_for_default_db(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["project", "create", "Nilo", "--id", "project_test"])
                    main(
                        [
                            "task",
                            "create",
                            "--project",
                            "project_test",
                            "--id",
                            "task_test",
                            "--title",
                            "handoff を更新する",
                        ]
                    )
                    main(
                        [
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "人間が成果物を確認して完了と判断した",
                        ]
                    )
            finally:
                os.chdir(previous_cwd)

            handoff = root / "HANDOFF.md"
            self.assertFalse(handoff.exists())
            self.assertNotIn("handoff: regenerated", output.getvalue())

    def test_task_complete_can_commit_accepted_changes(self) -> None:
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
                        "task_test",
                        "--title",
                        "承認後コミットを確認する",
                    ]
                )
                output = io.StringIO()
                with patch("nilo.cli.git_changed_files", return_value=["src/nilo/cli.py"]), patch(
                    "nilo.cli.commit_changed_files", return_value=(0, "abc123 Commit message", "")
                ) as commit_mock, redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "人間が成果物を確認して完了と判断した",
                            "--commit",
                            "--commit-message",
                            "Complete approval guidance",
                        ]
                    )

            commit_mock.assert_called_once()
            self.assertIn("commit: created", output.getvalue())
            self.assertIn("abc123 Commit message", output.getvalue())

    def test_git_changed_files_preserves_porcelain_status_columns(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=" M src/nilo/cli.py\nA  tests/test_cli.py\nR  old.py -> new.py\n",
            stderr="",
        )
        with patch("nilo.cli.subprocess.run", return_value=completed):
            self.assertEqual(git_changed_files(Path.cwd()), ["new.py", "src/nilo/cli.py", "tests/test_cli.py"])

    def test_review_prepare_outputs_review_only_prompt(self) -> None:
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
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                        "--type",
                        "review",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "prepare", "--task", "task_test"])

            self.assertIn("# レビュー指示", output.getvalue())
            self.assertIn("コード変更は禁止", output.getvalue())

    def test_review_import_records_quality_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# QualityReview\n\n## Summary\n設計妥当性に懸念がある。", encoding="utf-8")
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
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )
                main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])

            store = Store(db)
            quality = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(quality["reviewer"], "ai_review")
            self.assertIn("設計妥当性に懸念", quality["summary"])

    def test_review_import_parses_issues_and_scores(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# QualityReview\n\n"
                "## Summary\n仕様には合っている。\n\n"
                "## Issues\n- 既存設計との統一感が弱い\n\n"
                "## Scores\nrequirement_fit: 4\nscope_control=5\n",
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
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )
                main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])

            store = Store(db)
            quality = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(quality["summary"], "仕様には合っている。")
            self.assertEqual(quality["issues"], ["既存設計との統一感が弱い"])
            self.assertEqual(quality["scores"], {"requirement_fit": 4, "scope_control": 5})

    def test_review_import_parses_labeled_natural_language(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "Summary: 仕様には合っている。\n"
                "Issue: 既存設計との統一感が弱い\n"
                "Scores: requirement_fit=4 scope_control=5\n",
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
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )
                main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])

            store = Store(db)
            quality = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(quality["summary"], "仕様には合っている。")
            self.assertEqual(quality["issues"], ["既存設計との統一感が弱い"])
            self.assertEqual(quality["scores"], {"requirement_fit": 4, "scope_control": 5})

    def test_review_import_rejects_invalid_score(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# QualityReview\n\n## Summary\n評価。\n\n## Scores\nrequirement_fit: 6\n", encoding="utf-8")
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
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])
            self.assertIn("score must be 1-5: requirement_fit=6", str(raised.exception))

    def test_review_request_prepare_import_and_status_workflow(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            review = root / "review.md"
            report.write_text(REPORT, encoding="utf-8")
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n境界値の確認が不足している。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "file: src/nilo/cli.py\n"
                "line: 12\n"
                "blocking: true\n\n"
                "境界値テストが不足している。\n\n"
                "### F2\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: false\n\n"
                "明示的に nonblocking とした高 severity finding。\n\n"
                "### F3\n"
                "severity: typo\n"
                "status: unresolved\n\n"
                "不正 severity は medium に正規化する。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
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
                        "task_test",
                        "--title",
                        "レビュー依頼を管理する",
                        "--acceptance",
                        "review prepare に acceptance criteria が含まれる",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                register_test_reviewer(db, "codex")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "request",
                            "--task",
                            "task_test",
                            "--from",
                            "claude-code",
                            "--to",
                            "codex",
                            "--reason",
                            "別AIレビュー",
                        ]
                    )

            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            self.assertIn(
                f"MCP reviewer worker codex should claim review {request_id} with nilo mcp reviewer-claim or claim_next_review",
                status_output.getvalue(),
            )

            prepare_output = io.StringIO()
            with redirect_stdout(prepare_output):
                main(["--db", str(db), "review", "prepare", "--task", "task_test", "--review", request_id])
            prepare_body = prepare_output.getvalue()
            self.assertIn("# Review Request", prepare_body)
            self.assertIn("review prepare に acceptance criteria が含まれる", prepare_body)
            self.assertIn("## Implementation Report", prepare_body)
            self.assertIn("## Verification History", prepare_body)
            self.assertIn("python -m unittest discover tests", prepare_body)

            import_output = io.StringIO()
            with redirect_stdout(import_output):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])
            self.assertIn("verdict: changes_requested", import_output.getvalue())

            review_status = io.StringIO()
            with redirect_stdout(review_status):
                main(["--db", str(db), "review", "status", "--task", "task_test"])
            self.assertIn(f"{request_id} [completed] claude-code -> codex", review_status.getvalue())
            self.assertIn("[unresolved] high blocking src/nilo/cli.py:12: F1", review_status.getvalue())

            store = Store(db)
            result = store.latest_for_task("review_results", "task_test")
            findings = store.list_where("review_findings", "task_id=?", ("task_test",))
            store.close()
            self.assertEqual(result["verdict"], "changes_requested")
            self.assertEqual(result["reviewer"], "codex")
            by_title = {finding["title"]: finding for finding in findings}
            self.assertEqual(by_title["F1"]["status"], "unresolved")
            self.assertTrue(by_title["F1"]["blocking"])
            self.assertFalse(by_title["F2"]["blocking"])
            self.assertEqual(by_title["F3"]["severity"], "medium")

    def test_mcp_reviewer_start_registers_heartbeats_and_claims_pending_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "reviewer worker"])
                    register_test_reviewer(db, "claude-code")
                    request_output = io.StringIO()
                    with redirect_stdout(request_output):
                        main(
                            [
                                "--db",
                                str(db),
                                "review",
                                "request",
                                "--task",
                                "task_test",
                                "--from",
                                "codex",
                                "--to",
                                "claude-code",
                                "--reason",
                                "MCP worker startup",
                            ]
                        )
                request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "mcp",
                            "reviewer-start",
                            "--reviewer",
                            "claude-code",
                            "--project",
                            "project_test",
                        ]
                    )
                store = Store(db)
                try:
                    reviewer = store.list_where("review_reviewers", "reviewer=?", ("claude-code",))[0]
                    request = store.get("review_requests", request_id)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(reviewer["status"], "available")
        self.assertEqual(request["status"], "requested")
        self.assertIn("revived_review_requests:\n- none", output.getvalue())
        self.assertIn("- none", output.getvalue())
        self.assertIn("next_action: reviewer worker must call claim_next_review via MCP", output.getvalue())
        self.assertNotIn(f"- review_request: {request_id}", output.getvalue())

    def test_mcp_ping_runs_stdio_handshake_and_writes_transcript(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            log_file = root / "mcp_ping.json"
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "mcp", "ping", "--log-file", str(log_file)])

            body = json.loads(output.getvalue())
            saved = json.loads(log_file.read_text(encoding="utf-8"))

        self.assertTrue(body["ok"])
        self.assertEqual(body["server"]["name"], "nilo")
        self.assertGreater(body["tool_count"], 0)
        self.assertEqual(saved["server"]["name"], "nilo")
        self.assertEqual(saved["transcript"][0]["request"]["method"], "initialize")
        self.assertEqual(saved["transcript"][0]["request"]["params"]["clientInfo"]["name"], "hello-client")
        self.assertEqual(saved["transcript"][0]["response"]["result"]["serverInfo"]["name"], "nilo")
        self.assertEqual(saved["transcript"][2]["request"]["method"], "tools/list")
        self.assertIn("get_agent_work_context", saved["tool_names"])

    def test_mcp_reviewer_start_heartbeats_alias_without_claiming(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "alias reviewer worker"])
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude", "--reason", "alias startup"])

                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "mcp",
                            "reviewer-start",
                            "--reviewer",
                            "claude",
                            "--project",
                            "project_test",
                        ]
                    )
                store = Store(db)
                try:
                    request = store.latest_for_task("review_requests", "task_test")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["status"], "reviewer_unavailable")
        self.assertIn("reviewer: claude-code", output.getvalue())
        self.assertIn("revived_review_requests:\n- none", output.getvalue())
        self.assertIn("next_action: reviewer worker must call claim_next_review via MCP", output.getvalue())
        self.assertNotIn(f"- review_request: {request['id']}", output.getvalue())

    def test_mcp_reviewer_claim_registers_worker_claims_and_writes_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claim reviewer worker"])
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude", "--reason", "claim startup"])

                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "mcp",
                            "reviewer-claim",
                            "--reviewer",
                            "claude",
                            "--project",
                            "project_test",
                            "--write-default",
                        ]
                    )
                store = Store(db)
                try:
                    request = store.latest_for_task("review_requests", "task_test")
                    reviewer = store.list_where("review_reviewers", "reviewer=?", ("claude-code",))[0]
                finally:
                    store.close()
                prompt_path = root / ".nilo" / "reviews" / f"{request['id']}_prompt.md"
                template_path = root / ".nilo" / "reviews" / f"{request['id']}.md"
                prompt_exists = prompt_path.exists()
                template_exists = template_path.exists()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["status"], "claimed")
        self.assertEqual(reviewer["metadata"], {"worker_path": "nilo mcp reviewer-claim"})
        self.assertTrue(prompt_exists)
        self.assertTrue(template_exists)
        self.assertIn("reviewer: claude-code", output.getvalue())
        self.assertIn(f"- review_request: {request['id']}", output.getvalue())
        self.assertIn("next_action: review prompt_md and import_review_result through MCP", output.getvalue())

    def test_review_request_transaction_completes_through_mcp_reviewer_worker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            result_file = root / "review_result.md"
            result_file.write_text(review_body(verdict="approved", summary="Nilo transaction completed through MCP worker.", findings=""), encoding="utf-8")
            previous_cwd = Path.cwd()
            worker: subprocess.Popen[str] | None = None
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "transaction worker"])

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
                wait_for_dispatch_capable_reviewer(db, "claude-code")

                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "request",
                            "--task",
                            "task_test",
                            "--from",
                            "codex",
                            "--to",
                            "claude-code",
                            "--reason",
                            "transaction e2e",
                        ]
                    )
                stdout, stderr = worker.communicate(timeout=10)

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "review", "status", "--task", "task_test"])
                store = Store(db)
                try:
                    request = store.latest_for_task("review_requests", "task_test")
                    result = store.latest_for_task("review_results", "task_test")
                finally:
                    store.close()
            finally:
                if worker is not None and worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
                os.chdir(previous_cwd)

        self.assertEqual(worker.returncode, 0, stderr)
        self.assertIn("review_request:", request_output.getvalue())
        self.assertIn("review_request:", stdout)
        self.assertIn("verdict: approved", stdout)
        self.assertEqual(request["status"], "completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertIn("[completed] codex -> claude-code: transaction e2e", status_output.getvalue())

    def test_project_next_marks_expired_claimed_review_stale(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "expired review claim"])
            store = Store(db)
            try:
                store.insert(
                    "review_requests",
                    {
                        "id": "review_expired_claim",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "claimed",
                        "reason": "expired claim",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
            finally:
                store.close()

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            store = Store(db)
            try:
                request = store.get("review_requests", "review_expired_claim")
            finally:
                store.close()

        self.assertEqual(request["status"], "stale")
        self.assertIn("claude-code reviewer is missing", next_output.getvalue())
        self.assertIn("review review_expired_claim", next_output.getvalue())
        self.assertNotIn("wait for MCP reviewer", next_output.getvalue())

    def test_project_next_explains_stale_review_with_heartbeat_only_reviewer(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "heartbeat only review"])
                main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])
            store = Store(db)
            try:
                store.insert(
                    "review_requests",
                    {
                        "id": "review_heartbeat_only",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "stale",
                        "reason": "heartbeat only reviewer",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
            finally:
                store.close()

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])

        body = next_output.getvalue()
        self.assertIn("review_heartbeat_only", body)
        self.assertIn("claude-code reviewer is heartbeat_only", body)
        self.assertIn("reviewer-start only records heartbeat", body)
        self.assertIn("start a real MCP reviewer worker", body)

    def test_project_next_explains_stale_review_with_stale_reviewer_heartbeat(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale reviewer heartbeat"])
            store = Store(db)
            try:
                store.insert(
                    "review_reviewers",
                    {
                        "id": "reviewer_stale_heartbeat",
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
                        "id": "review_stale_heartbeat",
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

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])

        body = next_output.getvalue()
        self.assertIn("review_stale_heartbeat", body)
        self.assertIn("claude-code reviewer heartbeat is stale", body)
        self.assertIn("start or refresh a real MCP reviewer worker", body)

    def test_review_prepare_file_and_template_generate_handoff_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            prompt = root / ".nilo" / "reviews" / "prompt.md"
            template = root / ".nilo" / "reviews" / "review.md"
            report.write_text(REPORT, encoding="utf-8")
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
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
                        "task_test",
                        "--title",
                        "handoff file を作る",
                        "--acceptance",
                        "prompt file に acceptance criteria が含まれる",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "handoff"])

            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            self.assertIn(f"handoff_prompt: nilo review prepare --task task_test --review {request_id} --write-default", request_output.getvalue())
            self.assertIn(f"review_template: nilo review template --review {request_id} --write-default", request_output.getvalue())

            prepare_output = io.StringIO()
            with redirect_stdout(prepare_output):
                main(["--db", str(db), "review", "prepare", "--task", "task_test", "--review", request_id, "--file", str(prompt)])
            self.assertIn(f"review_context: {prompt}", prepare_output.getvalue())
            prompt_body = prompt.read_text(encoding="utf-8")
            self.assertIn("# Review Request", prompt_body)
            self.assertIn("prompt file に acceptance criteria が含まれる", prompt_body)
            self.assertIn("python -m unittest discover tests", prompt_body)

            template_output = io.StringIO()
            with redirect_stdout(template_output):
                main(["--db", str(db), "review", "template", "--review", request_id, "--file", str(template)])
            self.assertIn(f"review_template: {template}", template_output.getvalue())
            template_body = template.read_text(encoding="utf-8")
            self.assertIn("# ReviewResult", template_body)
            self.assertIn(f"review_id: {request_id}", template_body)
            self.assertIn(f"nilo review import --task task_test --review {request_id}", template_body)

    def test_review_handoff_write_default_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "default handoff"])
                    register_test_reviewer(db, "claude-code")
                    request_output = io.StringIO()
                    with redirect_stdout(request_output):
                        main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "handoff"])
                request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

                prepare_output = io.StringIO()
                with redirect_stdout(prepare_output):
                    main(["--db", str(db), "review", "prepare", "--task", "task_test", "--review", request_id, "--write-default"])
                template_output = io.StringIO()
                with redirect_stdout(template_output):
                    main(["--db", str(db), "review", "template", "--review", request_id, "--write-default"])
            finally:
                os.chdir(previous_cwd)

            prompt = root / ".nilo" / "reviews" / f"{request_id}_prompt.md"
            template = root / ".nilo" / "reviews" / f"{request_id}.md"
            default_prompt = Path(".nilo") / "reviews" / f"{request_id}_prompt.md"
            default_template = Path(".nilo") / "reviews" / f"{request_id}.md"
            self.assertTrue(prompt.exists())
            self.assertTrue(template.exists())
            self.assertIn(f"review_context: {default_prompt}", prepare_output.getvalue())
            self.assertIn(f"review_template: {default_template}", template_output.getvalue())
            self.assertIn("# Review Request", prompt.read_text(encoding="utf-8"))
            self.assertIn("# ReviewResult", template.read_text(encoding="utf-8"))

    def test_review_handoff_rejects_file_and_write_default_together(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "prepare",
                            "--task",
                            "task_test",
                            "--review",
                            "review_test",
                            "--file",
                            "prompt.md",
                            "--write-default",
                        ]
                    )

    def test_review_commented_status_has_project_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# ReviewResult\n\n## Verdict\ncommented\n\n## Summary\n参考コメントのみ。", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "コメントレビューを扱う"])
                register_test_reviewer(db, "human")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "参考レビュー"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            body = output.getvalue()
            self.assertIn("work_state: acceptance review 待ち", body)
            self.assertIn("task_test [review_commented]", body)
            self.assertIn("review imported findings and decide whether to address them", body)

    def test_review_delegate_creates_request_for_active_task_and_prints_claude_instruction(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "delegate review"])
                    register_test_reviewer(db, "claude-code")
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "delegate",
                            "--project",
                            "project_test",
                            "--to",
                            "claude-code",
                            "--reason",
                            "ユーザー依頼によるレビュー",
                        ]
                    )
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["requester"], "codex")
        self.assertEqual(request["reason"], "ユーザー依頼によるレビュー")
        body = output.getvalue()
        self.assertIn(f"review_request: {request['id']}", body)
        self.assertIn(f"Nilo MCP を使って task task_test の review {request['id']} をレビューして。", body)
        self.assertIn('get_agent_work_context(project_id="project_test")', body)
        self.assertIn("get_review_prompt", body)
        self.assertIn("import_review_result", body)
        self.assertIn("submit_agent_report", body)
        self.assertIn("record_test_result", body)

    def test_review_request_resolves_registered_reviewer_alias(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "alias review"])
                register_test_reviewer(db, "claude-code")
                main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude", "--reason", "alias"])

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["status"], "requested")

    def test_review_request_marks_unregistered_reviewer_unavailable(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "missing reviewer"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "missing"])

            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["status"], "reviewer_unavailable")
            self.assertIn("status: reviewer_unavailable", output.getvalue())
            self.assertIn("start a real MCP reviewer worker for claude-code", output.getvalue())
            self.assertIn("reviewer-start only records heartbeat", output.getvalue())
            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            self.assertIn("claude-code reviewer is missing", next_output.getvalue())
            self.assertIn("start a real MCP reviewer worker", next_output.getvalue())
            self.assertIn("claim_next_review", next_output.getvalue())

    def test_review_request_marks_stale_reviewer_unavailable(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale reviewer"])
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
                        "metadata": {"test": True},
                        "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
            finally:
                store.close()

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "stale"])
            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "reviewer_unavailable")

    def test_review_prepare_reviewer_readiness_outputs_json(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "prepare", "--project", "project_test", "--reviewer", "claude-code"])
            heartbeat_only = json.loads(output.getvalue())

            store = Store(db)
            try:
                reviewer_row = store.list_where("review_reviewers", "reviewer=?", ("claude-code",))[0]
                store.update(
                    "review_reviewers",
                    reviewer_row["id"],
                    {
                        "metadata": {
                            "worker_path": "claude-code-mcp-session",
                            "dispatch_capable": True,
                            "source": "real Claude Code session",
                        },
                    },
                )
            finally:
                store.close()
            ready_output = io.StringIO()
            with redirect_stdout(ready_output):
                main(["--db", str(db), "review", "prepare", "--project", "project_test", "--reviewer", "claude-code"])
            ready = json.loads(ready_output.getvalue())

        self.assertFalse(heartbeat_only["ready"])
        self.assertEqual(heartbeat_only["reason"], "heartbeat_only")
        self.assertIn("Open the Claude Code session", heartbeat_only["next_action"])
        self.assertEqual(heartbeat_only["register_reviewer_json"]["metadata"]["worker_path"], "claude-code-mcp-session")
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["availability"], "available")

    def test_review_withdraw_marks_requested_review_and_removes_pending_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "withdraw review"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "review me"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

            withdraw_output = io.StringIO()
            with redirect_stdout(withdraw_output):
                main(["--db", str(db), "review", "withdraw", "--review", request_id, "--reason", "superseded", "--actor", "codex"])
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "review", "status", "--task", "task_test"])
            project_status = io.StringIO()
            with redirect_stdout(project_status):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])

            store = Store(db)
            try:
                request = store.get("review_requests", request_id)
            finally:
                store.close()

        self.assertEqual(request["status"], "withdrawn")
        self.assertEqual(request["withdrawn_reason"], "superseded")
        self.assertEqual(request["withdrawn_actor"], "codex")
        self.assertIn("status: withdrawn", withdraw_output.getvalue())
        self.assertIn(f"- {request_id} [withdrawn] codex -> claude-code: review me", status_output.getvalue())
        self.assertIn("withdrawn_reason: superseded", status_output.getvalue())
        self.assertNotIn("claim review", project_status.getvalue())
        self.assertNotIn("claim review", next_output.getvalue())

    def test_review_withdraw_rejects_completed_review_without_changing_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# ReviewResult\n\n## Verdict\napproved\n\n## Summary\nOK。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "terminal review"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "review me"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "withdraw", "--review", request_id, "--reason", "too late", "--actor", "codex"])
            store = Store(db)
            try:
                request = store.get("review_requests", request_id)
                results = store.list_where("review_results", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertIn("terminal and cannot be withdrawn", str(raised.exception))
        self.assertEqual(request["status"], "completed")
        self.assertEqual(len(results), 1)

    def test_review_wait_returns_completed_review_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# ReviewResult\n\n## Verdict\napproved\n\n## Summary\nOK。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "wait review"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "review me"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            wait_output = io.StringIO()
            with redirect_stdout(wait_output):
                main(["--db", str(db), "review", "wait", "--review", request_id, "--timeout", "0"])

        self.assertIn(f"review_request: {request_id}", wait_output.getvalue())
        self.assertIn("status: completed", wait_output.getvalue())
        self.assertIn("verdict: approved", wait_output.getvalue())

    def test_review_request_wait_timeout_withdraws_request_and_clears_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "timeout review"])
                register_test_reviewer(db, "claude-code")
            request_output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(request_output):
                main(
                    [
                        "--db",
                        str(db),
                        "review",
                        "request",
                        "--task",
                        "task_test",
                        "--from",
                        "codex",
                        "--to",
                        "claude-code",
                        "--reason",
                        "review me",
                        "--wait",
                        "--wait-timeout",
                        "0",
                        "--poll-interval",
                        "0",
                    ]
                )
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            store = Store(db)
            try:
                request = store.get("review_requests", request_id)
            finally:
                store.close()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(request["status"], "withdrawn")
        self.assertEqual(request["withdrawn_actor"], "codex")
        self.assertIn("wait_result: timed_out", request_output.getvalue())
        self.assertIn("review wait timed out after 0 seconds", request["withdrawn_reason"])
        self.assertNotIn("claim review", next_output.getvalue())

    def test_review_wait_withdraws_when_reviewer_heartbeat_is_stale(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale wait"])
            store = Store(db)
            try:
                store.insert(
                    "review_reviewers",
                    {
                        "id": "reviewer_stale_wait",
                        "reviewer": "claude-code",
                        "status": "available",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"test": True},
                        "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
                store.insert(
                    "review_requests",
                    {
                        "id": "review_stale_wait",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "requested",
                        "reason": "old pending request",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
            finally:
                store.close()

            wait_output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(wait_output):
                main(["--db", str(db), "review", "wait", "--review", "review_stale_wait", "--timeout", "600", "--poll-interval", "0", "--actor", "codex"])
            store = Store(db)
            try:
                request = store.get("review_requests", "review_stale_wait")
            finally:
                store.close()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(request["status"], "withdrawn")
        self.assertEqual(request["withdrawn_reason"], "reviewer unavailable while waiting: claude-code")
        self.assertIn("wait_result: reviewer_unavailable", wait_output.getvalue())

    def test_review_human_launch_claude_runs_claude_code_with_instruction_on_stdin(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claude review"])
                    register_test_reviewer(db, "claude-code")
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="レビュー完了\n",
                    stderr="",
                )
                output = io.StringIO()
                with patch("subprocess.run", return_value=completed) as run_mock, redirect_stdout(output):
                    main(["--db", str(db), "review", "human-launch-claude", "--project", "project_test", "--task", "task_test"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["requester"], "codex")
        call = run_mock.call_args
        self.assertEqual(
            call.args[0],
            [
                "rtk",
                "proxy",
                "claude",
                "-p",
                "--mcp-config",
                ".mcp.json",
                "--permission-mode",
                "bypassPermissions",
            ],
        )
        self.assertIn(f"Nilo MCP を使って task task_test の review {request['id']} をレビューして。", call.kwargs["input"])
        self.assertEqual(call.kwargs["timeout"], 600.0)
        body = output.getvalue()
        self.assertNotIn("claude_command:", body)
        self.assertNotIn("rtk proxy claude", body)
        self.assertNotIn("claude -p", body)
        self.assertIn("claude_exit_code: 0", body)
        self.assertIn("review_status: requested", body)

    def test_natural_language_cluade_code_review_dispatches_without_claude_cli(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_test", "--title", "natural review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "Cluade", "Codeにレビューしてもらって"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["requester"], "codex")
        self.assertEqual(request["reason"], "Cluade Codeにレビューしてもらって")
        self.assertEqual(request["status"], "completed")
        self.assertEqual(result["verdict"], "approved")
        body = output.getvalue()
        self.assertIn('"status": "review_completed"', body)
        self.assertIn('"next_action"', body)

    def test_review_quick_imports_parseable_review_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"], timeout_seconds=600)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "quick review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatches = store.list_where("review_dispatches")
            finally:
                store.close()

        body = output.getvalue()
        self.assertIn("# ReviewResult", body)
        self.assertIn("quick_status: review_imported", body)
        self.assertIn("quick_imported: true", body)
        self.assertEqual(request["status"], "completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(dispatches, [])

    def test_review_quick_shows_raw_output_when_unparseable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "print('plain raw review')\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "raw quick review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
            finally:
                store.close()

        body = output.getvalue()
        self.assertIn("plain raw review", body)
        self.assertIn("quick_status: raw_review", body)
        self.assertIn("quick_imported: false", body)
        self.assertEqual(request["status"], "failed")
        self.assertIsNone(result)

    def test_review_quick_marks_request_failed_on_unexpected_exception(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "exception quick review"])
                output = io.StringIO()
                with patch("nilo.review_dispatcher.build_prompt_file", side_effect=OSError("cannot write prompt")):
                    with self.assertRaises(SystemExit), redirect_stdout(output):
                        main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
            finally:
                store.close()

        body = output.getvalue()
        self.assertIn("quick_status: review_failed", body)
        self.assertIn("cannot write prompt", body)
        self.assertEqual(request["status"], "failed")
        self.assertIn("cannot write prompt", request["withdrawn_reason"])
        self.assertIsNone(result)

    def test_review_quick_no_import_does_not_create_review_request(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "no import quick review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code", "--no-import"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertIn("quick_status: raw_review", output.getvalue())
        self.assertEqual(requests, [])

    def test_natural_language_light_review_routes_to_quick(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_test", "--title", "light natural review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "Claudeに軽くレビューして"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                result = store.latest_for_task("review_results", "task_test")
                dispatches = store.list_where("review_dispatches")
            finally:
                store.close()

        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(dispatches, [])
        self.assertIn("quick_status: review_imported", output.getvalue())

    def test_natural_language_formal_review_routes_to_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_test", "--title", "formal natural review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "Claudeに正式レビューして"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                dispatches = store.list_where("review_dispatches")
            finally:
                store.close()

        self.assertEqual(len(dispatches), 1)
        self.assertIn('"status": "review_completed"', output.getvalue())

    def test_review_dispatch_missing_config_returns_structured_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "missing config"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code", "--no-auto-configure"])
            finally:
                os.chdir(previous_cwd)

        body = output.getvalue()
        self.assertIn('"status": "needs_reviewer_config"', body)
        self.assertIn('"type": "create_reviewer_config"', body)
        self.assertIn("nilo review init --reviewer claude-code", body)

    def test_review_dispatch_command_not_found_records_command_resolution_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_dispatch_reviewer_config(root, ["claude-code"], command="definitely_missing_reviewer_binary")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "missing command"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(dispatch["status"], "review_failed")
        self.assertEqual(dispatch["failure_stage"], "command_resolution")
        self.assertIn('"failure_stage": "command_resolution"', output.getvalue())

    def test_review_dispatch_timeout_fails_and_marks_request_failed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "import time\ntime.sleep(2)\n")
                write_dispatch_reviewer_config(root, ["claude-code"], timeout_seconds=0.1)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "timeout"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(request["status"], "failed")
        self.assertEqual(dispatch["failure_stage"], "reviewer_timeout")
        self.assertIn('"failure_stage": "reviewer_timeout"', output.getvalue())
        self.assertIn('"type": "reviewer_timeout"', output.getvalue())

    def test_review_dispatch_malformed_output_does_not_complete(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "print('not a review result')\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "malformed"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(request["status"], "failed")
        self.assertIsNone(result)
        self.assertEqual(dispatch["failure_stage"], "review_output_received")
        self.assertIn("reviewer output malformed", output.getvalue())

    def test_review_dispatch_unrecognized_json_output_does_not_complete(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "print('{\"error\":\"rate limited\"}')\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "malformed json"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(request["status"], "failed")
        self.assertIsNone(result)
        self.assertEqual(dispatch["failure_stage"], "review_output_received")
        self.assertIn("reviewer output malformed", output.getvalue())

    def test_review_dispatch_masks_secret_in_failure_outputs(self) -> None:
        secret = "sk-" + "a" * 24
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, f"import sys\nprint({secret!r})\nprint({secret!r}, file=sys.stderr)\nsys.exit(2)\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "secret"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, dispatch["stdout"])
        self.assertNotIn(secret, dispatch["stderr"])
        self.assertIn("[MASKED:openai_api_key]", output.getvalue())

    def test_review_dispatch_supersedes_stale_requests(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale cleanup"])
                store = Store(db)
                try:
                    store.insert(
                        "review_requests",
                        {
                            "id": "review_stale",
                            "task_id": "task_test",
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "status": "stale",
                            "reason": "old",
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                        },
                    )
                finally:
                    store.close()
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                stale = store.get("review_requests", "review_stale")
                latest_event = store.latest_task_status_event("task_test")
            finally:
                store.close()

        self.assertEqual(stale["status"], "superseded")
        self.assertNotEqual(latest_event["event_id"], "review_stale")

    def test_windows_command_resolution_finds_cmd_suffix(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "claude.cmd"
            command.write_text("@echo off\n", encoding="utf-8")
            env = {"PATH": str(root)}
            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("claude", env)

        self.assertEqual(Path(resolved).name.lower(), "claude.cmd")

    def test_review_human_launch_claude_dry_run_skips_subprocess(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claude dry run"])
                output = io.StringIO()
                with patch("subprocess.run") as run_mock, redirect_stdout(output):
                    main(["--db", str(db), "review", "human-launch-claude", "--project", "project_test", "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

        claude_calls = [call for call in run_mock.call_args_list if call.args and call.args[0][:3] == ["rtk", "proxy", "claude"]]
        self.assertEqual(claude_calls, [])
        self.assertEqual(requests, [])
        body = output.getvalue()
        self.assertIn("claude_status: skipped (dry-run)", body)
        self.assertNotIn("claude_command:", body)
        self.assertNotIn("rtk proxy claude", body)
        self.assertNotIn("claude -p", body)

    def test_review_human_launch_claude_verbose_prints_human_runner_command(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claude dry run"])
                output = io.StringIO()
                with patch("subprocess.run") as run_mock, redirect_stdout(output):
                    main(["--db", str(db), "review", "human-launch-claude", "--project", "project_test", "--dry-run", "--verbose"])
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(run_mock.call_args_list, [])
        body = output.getvalue()
        self.assertIn("human_runner_command:", body)
        self.assertIn("rtk proxy claude", body)
        self.assertNotIn("claude_command:", body)

    def test_review_delegate_creates_review_task_for_dirty_tree_without_active_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (root / "dirty.txt").write_text("review me\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    register_test_reviewer(db, "claude-code")
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
                task = tasks[0]
                request = store.latest_for_task("review_requests", task["id"])
            finally:
                store.close()

        self.assertEqual(task["task_type"], "review")
        self.assertIn("Dirty files:", task["description"])
        self.assertIn("dirty.txt", task["description"])
        self.assertIn("Dirty file: dirty.txt", task["acceptance_criteria"])
        self.assertEqual(request["reviewer"], "claude-code")
        body = output.getvalue()
        self.assertIn("review_target: current dirty tree", body)
        self.assertIn("レビュー対象は現在の未コミット差分です。", body)
        self.assertIn("コード変更はしないで。", body)

    def test_review_delegate_creates_fresh_dirty_tree_task_on_repeated_delegation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (root / "first.txt").write_text("first\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    register_test_reviewer(db, "claude-code")
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
                (root / "second.txt").write_text("second\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
                reviews_by_task = {task["id"]: store.list_where("review_requests", "task_id=?", (task["id"],)) for task in tasks}
            finally:
                store.close()

        self.assertEqual(len(tasks), 2)
        self.assertNotEqual(tasks[0]["id"], tasks[1]["id"])
        self.assertTrue(all(task["task_type"] == "review" for task in tasks))
        self.assertTrue(all(reviews_by_task[task["id"]] for task in tasks))

    def test_review_delegate_refuses_clean_tree_without_active_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    register_test_reviewer(db, "claude-code")
                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

        self.assertIn("active task not found and dirty tree could not be inspected", str(raised.exception))

    def test_review_delegate_rejects_unavailable_reviewer_without_creating_request_or_dirty_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (root / "dirty.txt").write_text("review me\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
                requests = store.list_where("review_requests")
            finally:
                store.close()

        self.assertEqual(tasks, [])
        self.assertEqual(requests, [])
        self.assertIn("reviewer is not registered or available: claude-code", str(raised.exception))

    def test_dirty_tree_status_parser_handles_spaces_unicode_and_renames(self) -> None:
        raw = " M spaced name.txt\0?? 日本語.txt\0R  old name.txt\0new name.txt\0"

        self.assertEqual(
            parse_git_status_porcelain_z(raw),
            ["new name.txt", "spaced name.txt", "日本語.txt"],
        )

    def test_review_delegate_prefers_review_ready_task_over_old_rework_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_old", "--title", "old rework"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_ready", "--title", "ready review"])
                    register_test_reviewer(db, "claude-code")
                store = Store(db)
                try:
                    created_at = "2099-01-01T00:00:00+09:00"
                    store.insert(
                        "review_results",
                        {
                            "id": "review_result_old",
                            "task_id": "task_old",
                            "review_request_id": "review_old",
                            "reviewer": "claude-code",
                            "verdict": "changes_requested",
                            "summary": "needs rework",
                            "body_md": "# ReviewResult",
                            "created_at": created_at,
                        },
                    )
                    store.insert(
                        "verification_runs",
                        {
                            "id": "verification_ready",
                            "task_id": "task_ready",
                            "evidence_check_id": None,
                            "source": "nilo_executed",
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                            "timeout_seconds": 300.0,
                            "git_head": "abc123",
                            "metadata": {},
                            "started_at": "2099-01-01T00:00:01+09:00",
                            "finished_at": "2099-01-01T00:00:02+09:00",
                            "created_at": "2099-01-01T00:00:02+09:00",
                        },
                    )
                finally:
                    store.close()

                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_ready")
                old_request = store.latest_for_task("review_requests", "task_old")
            finally:
                store.close()

        self.assertIsNotNone(request)
        self.assertIsNone(old_request)
        self.assertIn(f"task task_ready の review {request['id']}", output.getvalue())

    def test_unresolved_blocking_review_finding_blocks_ai_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n修正が必要。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "完了前に直す必要がある。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "完了をブロックする"])
                register_test_reviewer(db, "human")
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "確認"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "検証済み", "--actor", "ai"])
            self.assertIn("AI completion blocked by unresolved review findings", str(raised.exception))

    def test_project_status_default_is_human_readable_for_review_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n修正が必要。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "file: src/nilo/project_logic.py\n"
                "line: 1\n"
                "blocking: true\n\n"
                "完了前に直す必要がある。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest tests.test_cli",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

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
                        "task_test",
                        "--title",
                        "Nilo human-readable status surface 改善",
                    ]
                )
                register_test_reviewer(db, "human")
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest tests.test_cli"])
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "確認"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            store = Store(db)
            finding = store.latest_for_task("review_findings", "task_test")
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "status", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("はい、1件残っています。", body)
            self.assertIn("レビュー指摘が1件残っています", body)
            self.assertIn("直近の検証は成功しています", body)
            self.assertNotIn("review_changes_requested", body)
            self.assertNotIn("address unresolved review finding", body)
            self.assertNotIn("exit_code=0", body)
            self.assertNotIn("unresolved blocking review finding", body)
            self.assertNotIn("task_test", body)
            self.assertNotIn(finding["id"], body)

            verbose_output = io.StringIO()
            with redirect_stdout(verbose_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            verbose_body = verbose_output.getvalue()
            self.assertIn("task_test", verbose_body)
            self.assertIn(finding["id"], verbose_body)

    def test_review_finding_update_records_history_and_unblocks_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n修正が必要。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "完了前に直す必要がある。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "finding 更新で完了可能にする"])
                register_test_reviewer(db, "human")
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "確認"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            store = Store(db)
            finding = store.latest_for_task("review_findings", "task_test")
            store.close()

            update_output = io.StringIO()
            with redirect_stdout(update_output):
                main(
                    [
                        "--db",
                        str(db),
                        "review",
                        "finding",
                        "update",
                        "--finding",
                        finding["id"],
                        "--status",
                        "addressed",
                        "--reason",
                        "テストを追加して修正済み",
                        "--actor",
                        "codex",
                    ]
                )
            self.assertIn("previous_status: unresolved", update_output.getvalue())
            self.assertIn("status: addressed", update_output.getvalue())

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "review", "status", "--task", "task_test"])
            status_body = status_output.getvalue()
            self.assertIn("review_summary:", status_body)
            self.assertIn("- total_findings: 1", status_body)
            self.assertIn("- unresolved_blocking: 0", status_body)
            self.assertIn("- addressed: 1", status_body)
            self.assertIn("[addressed] high blocking", status_body)
            self.assertIn("update_history:", status_body)
            self.assertIn("codex: unresolved -> addressed; テストを追加して修正済み", status_body)

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "finding 対応済み", "--actor", "ai"])

            store = Store(db)
            updated = store.get("review_findings", finding["id"])
            updates = store.list_where("review_finding_updates", "finding_id=?", (finding["id"],))
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertEqual(updated["status"], "addressed")
            self.assertEqual(updates[0]["previous_status"], "unresolved")
            self.assertEqual(updates[0]["new_status"], "addressed")
            self.assertEqual(updates[0]["reason"], "テストを追加して修正済み")
            self.assertEqual(updates[0]["actor"], "codex")
            self.assertIsNotNone(completion)

    def test_review_status_shows_summary_and_empty_update_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n要対応。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "blocking finding。\n\n"
                "### F2\n"
                "severity: low\n"
                "status: addressed\n"
                "blocking: false\n\n"
                "対応済み finding。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "review status summary"])
                register_test_reviewer(db, "human")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "summary"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "status", "--task", "task_test"])

            body = output.getvalue()
            self.assertIn("review_summary:", body)
            self.assertIn("- total_findings: 2", body)
            self.assertIn("- unresolved_blocking: 1", body)
            self.assertIn("- addressed: 1", body)
            self.assertIn("- unresolved: 1", body)
            self.assertIn("update_history:", body)
            self.assertIn("  - none", body)

    def test_review_status_json_includes_summary_findings_and_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n要対応。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "blocking finding。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "review status json"])
                register_test_reviewer(db, "human")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "json"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])
            store = Store(db)
            finding = store.latest_for_task("review_findings", "task_test")
            store.close()
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "--db",
                        str(db),
                        "review",
                        "finding",
                        "update",
                        "--finding",
                        finding["id"],
                        "--status",
                        "addressed",
                        "--reason",
                        "JSON確認用に更新",
                        "--actor",
                        "codex",
                    ]
                )

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "status", "--task", "task_test", "--format", "json"])

            data = json.loads(output.getvalue())
            self.assertEqual(data["task_id"], "task_test")
            self.assertEqual(data["total_findings"], 1)
            self.assertEqual(data["unresolved_blocking"], 0)
            self.assertEqual(data["finding_status_counts"]["addressed"], 1)
            self.assertEqual(data["review_requests"][0]["id"], request_id)
            self.assertEqual(data["review_findings"][0]["id"], finding["id"])
            self.assertEqual(data["review_findings"][0]["status"], "addressed")
            self.assertEqual(data["review_findings"][0]["update_history"][0]["reason"], "JSON確認用に更新")

    def test_review_finding_update_rejects_invalid_status(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "finding",
                            "update",
                            "--finding",
                            "finding_missing",
                            "--status",
                            "closed",
                            "--reason",
                            "invalid",
                        ]
                    )

    def test_verification_run_records_command_output_and_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("import sys\nprint('ok stdout')\nprint('ok stderr', file=sys.stderr)\n", encoding="utf-8")
            command = f'"{sys.executable}" "{script}"'

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
                        "task_test",
                        "--title",
                        "検証実行を記録する",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", command])
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertEqual(run["command"], command)
            self.assertIn("ok stdout", run["stdout"])
            self.assertIn("ok stderr", run["stderr"])
            self.assertEqual(run["exit_code"], 0)
            self.assertEqual(run["source"], "nilo_executed")
            self.assertFalse(run["timed_out"])
            self.assertTrue(run["started_at"])
            self.assertTrue(run["finished_at"])
            self.assertIn("working_tree_dirty", run["metadata"])
            self.assertIn("working_tree_files", run["metadata"])
            self.assertIsInstance(run["metadata"]["working_tree_dirty"], bool)
            self.assertIsInstance(run["metadata"]["working_tree_files"], list)
            self.assertIn("status: verification_passed", status_output.getvalue())
            self.assertIn("latest_verification_run:", status_output.getvalue())
            self.assertIn("verification_source: nilo_executed", status_output.getvalue())

    def test_status_surfaces_dirty_verification_working_tree(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            result = {
                "command": "python -m unittest",
                "cwd": str(Path.cwd()),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": True,
                    "working_tree_files": ["src/nilo/cli.py", "tests/test_cli.py"],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

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
                        "task_test",
                        "--title",
                        "dirty tree を表示する",
                    ]
                )
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest"])
            task_output = io.StringIO()
            with redirect_stdout(task_output):
                main(["--db", str(db), "task", "status", "--task", "task_test"])
            project_output = io.StringIO()
            with redirect_stdout(project_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test"])
            json_output = io.StringIO()
            with redirect_stdout(json_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(json_output.getvalue())

            self.assertIn("verification_working_tree: dirty (2 files)", task_output.getvalue())
            self.assertIn("- src/nilo/cli.py", task_output.getvalue())
            self.assertIn("verification_working_tree: dirty (2 files)", project_output.getvalue())
            self.assertIn("verification_working_tree: dirty (2 files)", summary_output.getvalue())
            self.assertIn("review dirty-tree verification metadata before accepting this task", project_output.getvalue())
            self.assertIn("add --commit only when you want Nilo to commit the accepted changes", project_output.getvalue())
            self.assertTrue(summary["active_tasks"][0]["verification_working_tree_dirty"])
            self.assertEqual(summary["active_tasks"][0]["verification_working_tree_files"], ["src/nilo/cli.py", "tests/test_cli.py"])

    def test_verification_run_connects_latest_evidence_check(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            script = root / "verify.py"
            report.write_text(REPORT, encoding="utf-8")
            script.write_text("print('verified')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
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
                        "task_test",
                        "--title",
                        "検証実行を証跡チェックに接続する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                main(
                    [
                        "--db",
                        str(db),
                        "verification",
                        "run",
                        "--task",
                        "task_test",
                        "--command",
                        f'"{sys.executable}" "{script}"',
                    ]
                )

            store = Store(db)
            check = store.latest_for_task("evidence_checks", "task_test")
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertEqual(run["evidence_check_id"], check["id"])

    def test_verification_run_masks_secrets_before_save(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('sk-thislookssecret1234567890')\n", encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "検証ログの秘密値をマスクする",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            store.close()
            self.assertNotIn("sk-thislookssecret1234567890", run["stdout"])
            self.assertIn("[MASKED:openai_api_key]", run["stdout"])
            self.assertEqual(run["metadata"]["secret_issue_count"], 1)
            self.assertTrue(any(failure["category"] == "secret_detected" for failure in failures))

    def test_verification_run_records_timeout(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "sleep.py"
            script.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "検証実行をタイムアウトする",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "verification",
                        "run",
                        "--task",
                        "task_test",
                        "--command",
                        f'"{sys.executable}" "{script}"',
                        "--timeout",
                        "0.1",
                    ]
                )

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertTrue(run["timed_out"])
            self.assertIsNone(run["exit_code"])
            self.assertIn("timed out", run["stderr"])

    def test_secret_guard_flags_report_for_human_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("3 passed", "3 passed\nsk-thislookssecret1234567890"), encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            check = store.latest_for_task("evidence_checks", "task_test")
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            stored_report = store.latest_for_task("agent_reports", "task_test")
            store.close()
            self.assertEqual(check["status"], "needs_human_review")
            self.assertTrue(any(issue.startswith("secret detected") for issue in check["issues"]))
            self.assertTrue(any(failure["category"] == "secret_detected" for failure in failures))
            self.assertNotIn("sk-thislookssecret1234567890", stored_report["body_md"])
            self.assertIn("[MASKED:openai_api_key]", stored_report["body_md"])

    def test_success_disable_archives_pattern(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "success",
                        "add",
                        "--project",
                        "project_test",
                        "--pattern",
                        "レビュー前に変更範囲を確認する",
                    ]
                )
            store = Store(db)
            pattern = store.list_where("success_patterns", "project_id=?", ("project_test",))[0]
            store.close()

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "success", "disable", "--pattern", pattern["id"]])

            store = Store(db)
            disabled = store.get("success_patterns", pattern["id"])
            store.close()
            self.assertEqual(disabled["state"], "archived")

    def test_success_pattern_usage_updates_on_instruct(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "success",
                        "add",
                        "--project",
                        "project_test",
                        "--pattern",
                        "実装前に調査タスクを作る",
                        "--type",
                        "implementation",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを実装する",
                    ]
                )
            store = Store(db)
            before = store.list_where("success_patterns", "project_id=?", ("project_test",))[0]
            store.close()
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "instruct", "--task", "task_test"])
            store = Store(db)
            after = store.get("success_patterns", before["id"])
            store.close()
            self.assertEqual(after["success_count"], before["success_count"] + 1)
            self.assertGreaterEqual(after["last_used_at"], before["last_used_at"])

    def test_understanding_gate_blocks_high_risk_instruction_until_approved(self) -> None:
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
                        "task_test",
                        "--title",
                        "危険な実装を行う",
                        "--risk",
                        "high",
                    ]
                )

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "instruct", "--task", "task_test"])

            understanding = Path(directory) / "understanding.md"
            understanding.write_text("# 実装前確認\n\n## 1. タスク目的の理解\n理解した。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "understanding", "prepare", "--task", "task_test"])
                main(["--db", str(db), "understanding", "import", "--task", "task_test", "--file", str(understanding)])
                main(["--db", str(db), "understanding", "approve", "--task", "task_test"])
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "instruct", "--task", "task_test"])

            self.assertIn("status: approved_to_implement", status_output.getvalue())
            self.assertIn("# 作業指示", output.getvalue())

    def test_understanding_approve_requires_imported_report(self) -> None:
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
                        "task_test",
                        "--title",
                        "危険な実装を行う",
                        "--requires-understanding-check",
                    ]
                )
                main(["--db", str(db), "understanding", "prepare", "--task", "task_test"])

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "understanding", "approve", "--task", "task_test"])

    def test_understanding_import_records_reported_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            understanding = root / "understanding.md"
            understanding.write_text("# 実装前確認\n\n## 1. タスク目的の理解\n目的を理解した。", encoding="utf-8")

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
                        "task_test",
                        "--title",
                        "危険な実装を行う",
                        "--requires-understanding-check",
                    ]
                )
                main(["--db", str(db), "understanding", "import", "--task", "task_test", "--file", str(understanding)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            latest = store.latest_for_task("understanding_checks", "task_test")
            store.close()
            self.assertEqual(latest["status"], "understanding_reported")
            self.assertIn("status: understanding_reported", output.getvalue())


def wait_for_dispatch_capable_reviewer(db: Path, reviewer: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        store = Store(db)
        try:
            rows = store.list_where("review_reviewers", "reviewer=? AND status='available'", (reviewer,))
        finally:
            store.close()
        if any(row["metadata"].get("worker_path") == "nilo mcp reviewer-worker" for row in rows):
            return
        time.sleep(0.05)
    raise AssertionError(f"reviewer worker did not become available: {reviewer}")


def review_body(verdict: str = "changes_requested", summary: str = "Review found an issue.", findings: str | None = None) -> str:
    if findings is None:
        findings = """### F1
severity: high
status: unresolved
file: src/nilo/mcp_server.py
line: 12
blocking: true

Review finding.
"""
    return f"""# ReviewResult

## Verdict
{verdict}

## Summary
{summary}

## Findings
{findings}
"""


def write_fake_dispatch_reviewer(root: Path, verdict: str = "changes_requested", findings: str | None = None) -> Path:
    if findings is None:
        findings = "なし"
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
        "print('Natural language dispatch completed.')\n"
        "print('\\n## Findings')\n"
        f"print({findings!r})\n",
        encoding="utf-8",
    )
    return script


def write_fake_dispatch_reviewer_script(root: Path, body: str) -> Path:
    script = root / "fake_reviewer.py"
    script.write_text(body, encoding="utf-8")
    return script


def write_dispatch_reviewer_config(
    root: Path,
    reviewers: list[str],
    *,
    command: str | None = None,
    timeout_seconds: float = 10,
) -> Path:
    config_dir = root / ".nilo"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "reviewers.toml"
    blocks = []
    for reviewer in reviewers:
        blocks.append(
            f"[reviewers.{reviewer}]\n"
            'kind = "agent"\n'
            f"command = {json.dumps(command or sys.executable)}\n"
            'args = ["fake_reviewer.py", "{prompt_file}"]\n'
            'working_directory = "{repo_root}"\n'
            "auto_start = true\n"
            f"timeout_seconds = {timeout_seconds}\n"
            "dispatch_capable = true\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
