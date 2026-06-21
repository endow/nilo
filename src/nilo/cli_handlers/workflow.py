from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..cli import (
    AGENT_TARGET_FILES,
    CLAUDE_CODE_REVIEWER_PROTOCOL_HEADING,
    build_agent_instruction_block,
    remove_markdown_section,
    requires_understanding_gate,
    understanding_approved,
    upsert_nilo_managed_block,
)
from ..agent_report_import import import_agent_report
from ..cli_support import make_id, read_text_or_exit
from ..gitmeta import head_commit
from ..guard import evaluate_evidence
from ..instruction import build_instruction, build_understanding_prompt
from ..project_model import default_project_row
from ..snapshot import compact_snapshot, current_git_snapshot
from ..store import Store
from ..task_logic import outcome_status
from ..timeutil import now_iso
from ..verification import run_local_verification


def cmd_agent_install(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        targets = list(AGENT_TARGET_FILES) if args.target == "all" else [args.target]
        install_agent_blocks(project, targets)
    finally:
        store.close()


def install_agent_blocks(project: dict, targets: list[str]) -> None:
    for target in targets:
        block = build_agent_instruction_block(project, target)
        path = Path.cwd() / AGENT_TARGET_FILES[target]
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if target == "claude-code":
            current = remove_markdown_section(current, CLAUDE_CODE_REVIEWER_PROTOCOL_HEADING)
        path.write_text(upsert_nilo_managed_block(current, block), encoding="utf-8")
        print(f"updated: {path.name}")


def ensure_nilo_gitignore_entry() -> None:
    path = Path.cwd() / ".gitignore"
    entry = ".nilo/"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = current.splitlines()
    if any(line.strip().rstrip("/") == ".nilo" for line in lines):
        return

    prefix = current
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    path.write_text(f"{prefix}{entry}\n", encoding="utf-8")
    print("updated: .gitignore")


def cmd_init(args: argparse.Namespace) -> None:
    project_id = Path.cwd().name
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        if project:
            print(f"project exists: {project_id}")
        else:
            project = default_project_row(project_id, now_iso())
            store.insert("projects", project)
            print(f"created project: {project_id}")
        install_agent_blocks(project, list(AGENT_TARGET_FILES))
        ensure_nilo_gitignore_entry()
    finally:
        store.close()


def cmd_instruct(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        project = store.get("projects", task["project_id"])
        if not project:
            raise SystemExit(f"project not found: {task['project_id']}")
        if requires_understanding_gate(task) and not understanding_approved(store, task["id"]):
            raise SystemExit("understanding check approval required before instruction generation")

        store.update("tasks", task["id"], {"base_commit": head_commit(Path.cwd())})
        task = store.get("tasks", task["id"])

        body, report_format = build_instruction(project, task)
        created_at = now_iso()
        instruction = {
            "id": make_id("instruction"),
            "task_id": task["id"],
            "applied_rule_ids": [],
            "degradation_mode": task["degradation_mode"],
            "body_md": body,
            "report_format_md": report_format,
            "created_at": created_at,
        }
        store.insert("instructions", instruction)
        print(body)
    finally:
        store.close()


def record_failure_and_rule(store: Store, project_id: str, task_id: str, report_id: str, category: str, message: str, severity: str) -> None:
    failure = {
        "id": make_id("failure"),
        "project_id": project_id,
        "task_id": task_id,
        "report_id": report_id,
        "category": category,
        "message": message,
        "severity": severity,
        "created_at": now_iso(),
    }
    store.insert("failure_logs", failure)


def cmd_report_import(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.file:
            markdown = read_text_or_exit(Path(args.file))
        else:
            markdown = sys.stdin.read()
        if not markdown.strip():
            raise SystemExit("report body is empty")

        result = import_agent_report(store, task, markdown, args.agent, Path.cwd(), evaluate_evidence)
        check = result["evidence_status"]

        print(f"report_form_status: {check['status']}")
        if check["issues"]:
            print("issues:")
            for issue in check["issues"]:
                print(f"- {issue}")
    finally:
        store.close()


def cmd_understanding_prepare(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        body = build_understanding_prompt(task)
        row = {
            "id": make_id("understanding"),
            "task_id": args.task,
            "status": "understanding_required",
            "body_md": body,
            "created_at": now_iso(),
        }
        store.insert("understanding_checks", row)
        print(body)
    finally:
        store.close()


def cmd_understanding_import(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.file:
            body = read_text_or_exit(Path(args.file))
        else:
            body = sys.stdin.read()
        if not body.strip():
            raise SystemExit("understanding body is empty")
        row = {
            "id": make_id("understanding"),
            "task_id": args.task,
            "status": "understanding_reported",
            "body_md": body,
            "created_at": now_iso(),
        }
        store.insert("understanding_checks", row)
        print(f"status: understanding_reported")
        print(f"understanding_check: {row['id']}")
    finally:
        store.close()


def cmd_understanding_approve(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        latest = store.latest_for_task("understanding_checks", args.task)
        if not latest or latest["status"] != "understanding_reported":
            raise SystemExit("understanding report import required before approval")
        body = latest["body_md"]
        row = {
            "id": make_id("understanding"),
            "task_id": args.task,
            "status": "approved_to_implement",
            "body_md": body,
            "created_at": now_iso(),
        }
        store.insert("understanding_checks", row)
        print("status: approved_to_implement")
        print(f"understanding_check: {row['id']}")
    finally:
        store.close()


def cmd_outcome_record(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        latest_report = store.latest_for_task("agent_reports", args.task)
        latest_verification = store.latest_for_task("verification_runs", args.task)
        latest_review = store.latest_for_task("review_results", args.task)
        concerns = args.concern or []
        decision = args.decision
        snapshot = compact_snapshot(current_git_snapshot(Path.cwd()))
        accepted = decision in ("accepted", "accepted_with_concerns")
        row = {
            "id": make_id("completion" if accepted else "outcome"),
            "task_id": args.task,
            "actor": "human",
            "completed_by": "human",
            "completed_snapshot": snapshot,
            "completion_note": args.reason,
            "accepted_verification_run_ids": [latest_verification["id"]] if accepted and latest_verification else [],
            "accepted_review_result_ids": [latest_review["id"]] if accepted and latest_review else [],
            "human_decision_note": "\n".join([args.reason, *concerns]).strip(),
            "completed_with_reservations": decision == "accepted_with_concerns",
            "reason": args.reason,
            "completed_at": now_iso(),
            "created_at": now_iso(),
        }
        if accepted:
            store.insert("task_completions", row)
        if decision in ("rejected", "rework_required"):
            severity = "high" if decision == "rejected" else "medium"
            record_failure_and_rule(
                store,
                task["project_id"],
                task["id"],
                latest_report["id"] if latest_report else "",
                f"human_{decision}",
                args.reason,
                severity,
            )
        print(f"status: {outcome_status(decision)}")
        if accepted:
            print(f"task_completion: {row['id']}")
    finally:
        store.close()


def cmd_verification_run(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        result = run_local_verification(args.command, Path.cwd(), args.timeout)
        row = {
            "id": make_id("verification"),
            "task_id": args.task,
            "evidence_check_id": None,
            **result,
        }
        store.insert("verification_runs", row)
        for issue in result["metadata"]["secret_issues"]:
            record_failure_and_rule(
                store,
                task["project_id"],
                task["id"],
                "",
                "secret_detected",
                issue,
                "high",
            )
        print(f"verification_run: {row['id']}")
        print(f"exit_code: {row['exit_code']}")
        print(f"timed_out: {bool(row['timed_out'])}")
    finally:
        store.close()
