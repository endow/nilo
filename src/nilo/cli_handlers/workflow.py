from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..agent_report_import import validate_agent_report
from ..cli_support import read_text_or_exit
from ..gitmeta import head_commit, task_base_snapshot
from ..guard import evaluate_evidence
from ..project_boundary import (
    ProjectBoundaryError,
    record_nilo_issue_for_task,
    require_write_fence,
    resolve_project_boundary,
)
from ..store import Store
from ..task_logic import outcome_status
from ..transitions import (
    TransitionError,
    approve_understanding,
    import_agent_report,
    record_outcome_decision,
)
from ..update_check import check_for_update, is_disabled, update_message
from ..verification import execute_and_record_verification, run_local_verification
from ..workflow_services import (
    create_instruction,
    import_understanding,
    prepare_understanding,
)


def cmd_update_check(args: argparse.Namespace) -> None:
    if is_disabled():
        print("Nilo update check skipped: disabled by NILO_NO_UPDATE_CHECK.")
        return
    print(update_message(check_for_update(), language="en"))


def cmd_instruct(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        try:
            body = create_instruction(
                store,
                args.task,
                plan=args.plan,
                db_path=args.db,
                cwd=Path.cwd(),
                head_provider=head_commit,
                snapshot_provider=task_base_snapshot,
            )
        except (LookupError, PermissionError) as exc:
            raise SystemExit(str(exc)) from exc
        print(body)
    finally:
        store.close()


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

        boundary = resolve_project_boundary(db_path=args.db)
        try:
            require_write_fence(boundary)
        except ProjectBoundaryError as exc:
            record_nilo_issue_for_task(
                store, task["project_id"], task["id"], "report import", exc, boundary
            )
            raise SystemExit(str(exc)) from exc
        try:
            result = import_agent_report(
                store, task, markdown, args.agent, Path.cwd(), evaluate_evidence
            )
        except TransitionError as exc:
            raise SystemExit(
                f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}"
            ) from exc
        check = {
            "status": result.audit_notes[0] if result.audit_notes else "unknown",
            "issues": result.warnings,
        }

        print(f"report_form_status: {check['status']}")
        if check["issues"]:
            print("issues:")
            for issue in check["issues"]:
                print(f"- {issue}")
    finally:
        store.close()


def cmd_report_validate(args: argparse.Namespace) -> None:
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

        result = validate_agent_report(
            store, task, markdown, Path.cwd(), evaluate_evidence
        )
        print(f"report_form_status: {result['status']}")
        print("changed_files:")
        if result["changed_files"]:
            for path in result["changed_files"]:
                print(f"- {path}")
        else:
            print("- none")
        if result["issues"]:
            print("issues:")
            for issue in result["issues"]:
                print(f"- {issue}")
            raise SystemExit(1)
    finally:
        store.close()


def cmd_understanding_prepare(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        try:
            body = prepare_understanding(store, args.task)
        except LookupError as exc:
            raise SystemExit(str(exc)) from exc
        print(body)
    finally:
        store.close()


def cmd_understanding_import(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        if args.file:
            body = read_text_or_exit(Path(args.file))
        else:
            body = sys.stdin.read()
        try:
            row_id = import_understanding(store, args.task, body)
        except (LookupError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print("status: understanding_reported")
        print(f"understanding_check: {row_id}")
    finally:
        store.close()


def cmd_understanding_approve(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        try:
            result = approve_understanding(
                store,
                args.task,
                actor=args.actor,
                reason=args.reason,
                human_confirm=args.human_confirm,
                decision_source="human_interactive",
                decision_note=args.decision_note,
            )
        except TransitionError as exc:
            raise SystemExit(
                f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}"
            ) from exc
        print("status: approved_to_implement")
        print(f"understanding_check: {result.created_ids['understanding_check']}")
    finally:
        store.close()


def cmd_outcome_record(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        concerns = args.concern or []
        decision = args.decision
        if decision in ("accepted", "accepted_with_concerns"):
            boundary = resolve_project_boundary(db_path=args.db)
            try:
                require_write_fence(boundary)
            except ProjectBoundaryError as exc:
                record_nilo_issue_for_task(
                    store,
                    task["project_id"],
                    task["id"],
                    f"outcome {decision}",
                    exc,
                    boundary,
                )
                raise SystemExit(str(exc)) from exc
        try:
            result = record_outcome_decision(
                store,
                args.task,
                decision=decision,
                actor="human",
                reason=args.reason,
                concerns=concerns,
                human_confirm=getattr(args, "human_confirm", False),
                decision_source="human_interactive",
                decision_note=getattr(args, "decision_note", ""),
                cwd=Path.cwd(),
            )
        except TransitionError as exc:
            raise SystemExit(
                f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}"
            ) from exc
        print(f"status: {outcome_status(decision)}")
        if "task_completion" in result.created_ids:
            print(f"task_completion: {result.created_ids['task_completion']}")
    finally:
        store.close()


def cmd_verification_run(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        snapshot_mode = getattr(args, "snapshot", "fast")
        try:
            row = execute_and_record_verification(
                store,
                task,
                command=args.command,
                timeout_seconds=args.timeout,
                verification_mode=args.mode,
                snapshot_mode=snapshot_mode,
                cwd=Path.cwd(),
                db_path=args.db,
                runner=run_local_verification,
            )
        except (ProjectBoundaryError, TransitionError) as exc:
            if isinstance(exc, ProjectBoundaryError):
                raise SystemExit(str(exc)) from exc
            raise SystemExit(
                f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}"
            ) from exc
        print(f"verification_run: {row['id']}")
        print(f"mode: {args.mode}")
        print(f"snapshot: {row['metadata'].get('snapshot_mode', snapshot_mode)}")
        print(f"exit_code: {row['exit_code']}")
        print(f"timed_out: {bool(row['timed_out'])}")
    finally:
        store.close()
