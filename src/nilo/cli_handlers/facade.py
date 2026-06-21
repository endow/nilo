from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from pathlib import Path

from ..failure import deterministic_id
from ..store import Store
from ..timeutil import now_iso
from .task import cmd_task_complete, cmd_task_create
from .workflow import cmd_outcome_record, cmd_report_import, cmd_verification_run


def default_project_id(args: argparse.Namespace) -> str:
    return args.project or Path.cwd().name


def active_tasks_for_project(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    from .. import cli as c

    tasks, statuses = c.project_tasks_and_statuses(store, project_id)
    return [task for task in tasks if not c.is_task_completed_status(statuses[task["id"]])], statuses


def resolve_task_id(args: argparse.Namespace, store: Store) -> str:
    if getattr(args, "task", None):
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        return args.task

    project_id = default_project_id(args)
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    active_tasks, _ = active_tasks_for_project(store, project_id)
    if not active_tasks:
        raise SystemExit(f"active task not found for project: {project_id}")
    if len(active_tasks) > 1:
        ids = ", ".join(task["id"] for task in active_tasks[:5])
        raise SystemExit(f"multiple active tasks; pass --task explicitly: {ids}")
    return active_tasks[0]["id"]


def summary_for_project(store: Store, project_id: str) -> dict:
    from .. import cli as c

    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks, statuses = c.project_tasks_and_statuses(store, project_id)
    return c.project_summary_data(store, project, tasks, statuses)


def print_facade_next_for_task(store: Store, task_id: str) -> None:
    from .. import cli as c
    from .. import project_logic as p

    task = store.get("tasks", task_id)
    if not task:
        raise SystemExit(f"task not found: {task_id}")
    status = c.projected_task_status(store, task)
    verification_run = store.latest_for_task("verification_runs", task_id)
    unexecuted = c.unexecuted_verifications_for_task(status, verification_run)
    print(f"task: {task['id']}")
    print(f"title: {task['title']}")
    print(f"status: {status}")
    print("next:")
    pending_review = p.latest_pending_review_request(store, task_id)
    if pending_review:
        print(f"- {p.next_action_for_review_request(store, pending_review)}")
        return
    for action in c.next_actions_for_task(status, verification_run, unexecuted, task["id"], task["task_type"]):
        print(f"- {action}")


def cmd_facade_status(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    store = Store(args.db)
    try:
        summary = summary_for_project(store, project_id)
        if not getattr(args, "verbose", False):
            project = store.get("projects", project_id)
            if not project:
                raise SystemExit(f"project not found: {project_id}")
            active_tasks, statuses = active_tasks_for_project(store, project_id)
            from .. import cli as c

            c.print_human_project_status(store, project, active_tasks, statuses)
            return
        print(f"project: {summary['project_id']} ({summary['project_name']})")
        print(f"roadmap: {summary['roadmap_position']}")
        print(f"work: {summary['work_state']}")
        print("active:")
        if summary["active_tasks"]:
            for task in summary["active_tasks"]:
                print(f"- {task['id']} [{task['status']}] {task['task_type']} {task['title']}")
        else:
            print("- none")
        print("todo:")
        if summary["todo_status_counts"]:
            for status, count in summary["todo_status_counts"].items():
                print(f"- {status}: {count}")
        else:
            print("- none")
        print("next:")
        actions = summary["next_actions"] or []
        if actions:
            for action in actions[:3]:
                print(f"- {action}")
        else:
            print("- none")
    finally:
        store.close()


def cmd_facade_next(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    store = Store(args.db)
    try:
        if getattr(args, "task", None):
            print_facade_next_for_task(store, args.task)
            return
        summary = summary_for_project(store, project_id)
        if summary["active_tasks"]:
            task_id = summary["active_tasks"][0]["id"]
            print_facade_next_for_task(store, task_id)
            return
        print(f"project: {summary['project_id']} ({summary['project_name']})")
        print("next:")
        actions = summary["next_actions"] or []
        if actions:
            print(f"- {actions[0]}")
        else:
            print("- none")
    finally:
        store.close()


def cmd_facade_start(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        if not project:
            raise SystemExit(f"project not found: {project_id}")
        commitment_id = args.commitment
        task_id = deterministic_id("task", [project_id, args.title, now_iso()])
    finally:
        store.close()

    create_args = argparse.Namespace(
        db=args.db,
        project=project_id,
        title=args.title,
        description=args.description,
        acceptance=args.acceptance,
        id=task_id,
        parent_task=None,
        split_index=None,
        commitment=commitment_id,
        roadmap_item="",
        model="",
        degradation="normal",
        mode=args.mode,
        task_type=args.task_type,
        risk=args.risk,
        requires_understanding_check=False,
    )
    with redirect_stdout(io.StringIO()):
        cmd_task_create(create_args)
    print(f"task: {task_id}")
    print(f"next: nilo next --task {task_id}")
    print(f"instruct: nilo instruct --task {task_id}")


def cmd_facade_check(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_verification_run(argparse.Namespace(db=args.db, task=task_id, command=args.command, timeout=args.timeout))


def cmd_facade_report(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_report_import(argparse.Namespace(db=args.db, task=task_id, file=args.file, agent=args.agent))


def cmd_facade_done(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_task_complete(
        argparse.Namespace(
            db=args.db,
            task=task_id,
            reason=args.reason,
            actor=args.actor,
            commit=args.commit,
            commit_message=args.commit_message,
        )
    )


def cmd_facade_reject(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_outcome_record(
        argparse.Namespace(
            db=args.db,
            task=task_id,
            reason=args.reason,
            concern=[],
            decision="rejected",
        )
    )
