from __future__ import annotations

import argparse
from pathlib import Path

from ..cli_support import make_id
from ..failure import deterministic_id
from ..snapshot import compact_snapshot, current_git_snapshot, evidence_status, review_result_status
from ..store import Store
from ..task_logic import completion_status, projected_task_status, require_ai_completion_evidence, split_task_specs
from ..timeutil import now_iso


def cmd_task_create(args: argparse.Namespace) -> str:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        created_at = now_iso()
        row = {
            "id": args.id or deterministic_id("task", [args.project, args.title, created_at]),
            "project_id": args.project,
            "title": args.title,
            "description": "\n".join(args.description or []),
            "acceptance_criteria": args.acceptance or [],
            "parent_task_id": args.parent_task,
            "split_index": args.split_index,
            "task_type": args.task_type,
            "risk_level": args.risk,
            "requires_understanding_check": args.requires_understanding_check,
            "roadmap_commitment_id": args.commitment or "",
            "roadmap_item_id": args.roadmap_item or "",
            "status": "planned",
            "assigned_model_profile": args.model,
            "degradation_mode": args.degradation,
            "mode": args.mode,
            "base_commit": None,
            "created_at": created_at,
        }
        store.insert("tasks", row)
        if args.model:
            store.insert(
                "model_usage_logs",
                {
                    "id": make_id("model_usage"),
                    "task_id": row["id"],
                    "model_profile_id": args.model,
                    "purpose": "task_assignment",
                    "degradation_mode": args.degradation,
                    "created_at": created_at,
                },
            )
        print(row["id"])
        return row["id"]
    finally:
        store.close()


def cmd_task_start(args: argparse.Namespace) -> None:
    from .facade import cmd_facade_start

    cmd_facade_start(args)


def cmd_task_status(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        verification_run = store.latest_for_task("verification_runs", args.task)
        current_snapshot = current_git_snapshot(Path.cwd())
        instruction = store.latest_for_task("instructions", args.task)
        report = store.latest_for_task("agent_reports", args.task)
        quality_review = store.latest_for_task("quality_reviews", args.task)
        review_request = store.latest_for_task("review_requests", args.task)
        review_result = store.latest_for_task("review_results", args.task)
        review_findings = store.list_where("review_findings", "task_id=?", (args.task,))
        recipe_provenance = store.latest_for_task("recipe_task_provenance", args.task)
        print(f"id: {task['id']}")
        print(f"status: {projected_task_status(store, task)}")
        print(f"task_type: {task['task_type']}")
        print(f"risk_level: {task['risk_level']}")
        print(f"requires_understanding_check: {bool(task['requires_understanding_check'])}")
        print(f"mode: {task.get('mode', 'normal')}")
        if recipe_provenance:
            print(f"recipe: {recipe_provenance['recipe_name']} ({recipe_provenance['source_layer']} layer)")
            print("recipe_provenance: stored for audit")
        if task.get("description"):
            print("description:")
            print(task["description"])
        if task.get("acceptance_criteria"):
            print("acceptance_criteria:")
            for criterion in task["acceptance_criteria"]:
                print(f"- {criterion}")
        print(f"base_commit: {task['base_commit'] or 'none'}")
        if instruction:
            print(f"latest_instruction: {instruction['id']}")
        if report:
            print(f"latest_report: {report['id']} ({report['claimed_status']})")
        print(f"evidence_status: {evidence_status(verification_run, current_snapshot)}")
        if verification_run:
            result = "timed_out" if verification_run["timed_out"] else f"exit_code={verification_run['exit_code']}"
            print(f"latest_verification_run: {verification_run['id']} ({result})")
            print(f"verification_source: {verification_run.get('source', 'nilo_executed')}")
            print(f"verification_command: {verification_run['command']}")
            print(f"verification_working_tree: {c.verification_working_tree_summary(verification_run)}")
            for line in c.verification_snapshot_policy_lines(verification_run):
                print(line)
            for file in c.verification_working_tree_state(verification_run)["files"]:
                print(f"- {file}")
            if verification_run["evidence_check_id"]:
                print(f"verification_evidence_check: {verification_run['evidence_check_id']}")
        if quality_review:
            c.print_quality_review(quality_review)
        if review_request:
            print(f"latest_review_request: {review_request['id']} ({review_request['status']}) {review_request['requester']} -> {review_request['reviewer']}")
        if review_result:
            print(f"latest_review_result: {review_result['id']} ({review_result['verdict']}, {review_result_status(review_result, current_snapshot)})")
        if review_findings:
            print("review_findings:")
            for finding in review_findings:
                marker = "blocking" if finding["blocking"] else "nonblocking"
                location = f" {finding['file_path']}:{finding['line']}" if finding["file_path"] else ""
                print(f"- {finding['id']} [{finding['status']}] {finding['severity']} {marker}{location}: {finding['title']}")
        completion_warnings = c.recipe_completion_warnings(store, args.task)
        if completion_warnings:
            print("completion_warnings:")
            for warning in completion_warnings:
                print(f"- {warning['severity']}: {warning['message']}")
        understanding = store.latest_for_task("understanding_checks", args.task)
        if understanding:
            print(f"latest_understanding_check: {understanding['id']} ({understanding['status']})")
        completion = store.latest_for_task("task_completions", args.task)
        if completion:
            print(f"completed_reason: {completion['reason']}")
            print(f"completed_with_reservations: {bool(completion.get('completed_with_reservations'))}")
    finally:
        store.close()


def cmd_task_update(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        values = {}
        changes = []
        if args.description is not None:
            values["description"] = "\n".join(args.description)
            changes.append("description")
        if args.acceptance is not None and args.append_acceptance is not None:
            raise SystemExit("use either --acceptance to replace criteria or --append-acceptance to append criteria, not both")
        if args.acceptance is not None:
            values["acceptance_criteria"] = args.acceptance
            changes.append("acceptance_criteria")
        if args.append_acceptance is not None:
            values["acceptance_criteria"] = [*task.get("acceptance_criteria", []), *args.append_acceptance]
            changes.append("acceptance_criteria")
        if not values:
            raise SystemExit("nothing to update; pass --description, --acceptance, or --append-acceptance")
        store.update("tasks", args.task, values)
        updated = store.get("tasks", args.task)
        print(f"task: {updated['id']}")
        print(f"updated: {', '.join(changes)}")
        if "description" in values:
            print("description:")
            print(updated["description"])
        if "acceptance_criteria" in values:
            print("acceptance_criteria:")
            for criterion in updated["acceptance_criteria"]:
                print(f"- {criterion}")
    finally:
        store.close()


def cmd_task_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks = store.list_where("tasks", "project_id=?", (args.project,))
        for task in reversed(tasks):
            print(
                "\t".join(
                    [
                        task["id"],
                        projected_task_status(store, task),
                        task["task_type"],
                        task["risk_level"],
                        task["title"],
                        task["created_at"],
                    ]
                )
            )
    finally:
        store.close()


def cmd_task_complete(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.actor == "ai":
            require_ai_completion_evidence(store, args.task)
        now = now_iso()
        snapshot = compact_snapshot(current_git_snapshot(Path.cwd()))
        latest_verification = store.latest_for_task("verification_runs", args.task)
        latest_review = store.latest_for_task("review_results", args.task)
        row = {
            "id": make_id("completion"),
            "task_id": args.task,
            "actor": args.actor,
            "completed_by": args.actor,
            "completed_snapshot": snapshot,
            "completion_note": args.reason,
            "accepted_verification_run_ids": [latest_verification["id"]] if latest_verification else [],
            "accepted_review_result_ids": [latest_review["id"]] if latest_review else [],
            "human_decision_note": args.reason if args.actor == "human" else "",
            "completed_with_reservations": False,
            "completed_at": now,
            "reason": args.reason,
            "created_at": now,
        }
        store.insert("task_completions", row)
        print(f"status: {completion_status(args.actor)}")
        print(f"completed_by: {args.actor}")
        print(f"task_completion: {row['id']}")
        completion_warnings = c.recipe_completion_warnings(store, args.task)
        if completion_warnings:
            print("completion_warnings:")
            for warning in completion_warnings:
                print(f"- {warning['severity']}: {warning['message']}")
        changed_files = c.git_changed_files(Path.cwd())
        if changed_files and args.commit:
            message = args.commit_message or f"Complete {task['title']}"
            code, out, err = c.commit_changed_files(Path.cwd(), changed_files, message)
            if code != 0:
                raise SystemExit(err or "git commit failed")
            print("commit: created")
            if out:
                print(out)
        elif changed_files:
            print("next_actions:")
            print("- commit accepted changes")
            print(f"- suggested command: git add {' '.join(changed_files)} && git commit -m \"Complete {task['title']}\"")
    finally:
        store.close()


def cmd_task_split(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        specs = split_task_specs(task)
        print("Generated subtasks:")
        for index, (task_type, title) in enumerate(specs, start=1):
            row = {
                "id": make_id("task"),
                "project_id": task["project_id"],
                "title": title,
                "description": "",
                "acceptance_criteria": [],
                "parent_task_id": task["id"],
                "split_index": index,
                "task_type": task_type,
                "risk_level": task["risk_level"],
                "requires_understanding_check": task_type == "implementation" and task["risk_level"] == "high",
                "status": "planned",
                "assigned_model_profile": task["assigned_model_profile"],
                "degradation_mode": task["degradation_mode"],
                "mode": task.get("mode", "normal"),
                "base_commit": None,
                "created_at": now_iso(),
            }
            store.insert("tasks", row)
            code_policy = "Code changes: forbidden" if task_type in ("research", "design", "review", "verification") else "Code changes: allowed"
            print(f"{index}. {task_type}")
            print(f"   id: {row['id']}")
            print(f"   {title}")
            print(f"   {code_policy}")
    finally:
        store.close()
