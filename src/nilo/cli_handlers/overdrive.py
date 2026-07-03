from __future__ import annotations

import argparse

from ..overdrive import start_overdrive_run
from ..store import Store


def print_overdrive_run(run: dict) -> None:
    print(f"overdrive_run: {run['id']}")
    print(f"mode: {run['mode']}")
    print(f"scope: {run.get('scope', 'task')}")
    print(f"status: {run['status']}")
    print(f"roadmap_commitment_id: {run['roadmap_commitment_id'] or 'none'}")
    print(f"cursor_task_id: {run['cursor_task_id'] or 'none'}")
    print(f"max_failures: {run['max_failures']}")
    print("approval_gates: bypassed")
    print("safety_gates: retained")
    if run.get("scope") in {"task", "commitment"}:
        next_task_id = (run.get("summary_json") or {}).get("next_after_scope_task_id", "")
        print("scope_boundary: stop before unrelated next task")
        if next_task_id:
            print(f"next_task_after_scope: {next_task_id}")
        print("next_scope_requirement: use --scope queue or explicit approval before continuing unrelated tasks")
    print("final_human_review_checkpoint: required")


def cmd_run(args: argparse.Namespace) -> None:
    run_overdrive_command(args, "run")


def cmd_roadmap_execute(args: argparse.Namespace) -> None:
    run_overdrive_command(args, "roadmap execute")


def run_overdrive_command(args: argparse.Namespace, command_name: str) -> None:
    if not args.overdrive:
        raise SystemExit(f"{command_name} currently requires --overdrive")
    store = Store(args.db)
    try:
        run = start_overdrive_run(store, args.project, args.commitment, args.max_failures, args.scope)
        print_overdrive_run(run)
    finally:
        store.close()
