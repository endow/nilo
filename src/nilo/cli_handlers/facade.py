from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from ..ai_context import AI_CONTEXT_TEXT_MAX_CHARS, project_ai_context, render_ai_context_text
from ..display_labels import field_label, status_label
from ..failure import deterministic_id
from ..human_status import human_next_action_text
from ..project_logic import refresh_review_dispatch_state
from ..project_boundary import (
    ProjectBoundaryError,
    assert_self_development_allowed,
    boundary_warning_lines,
    resolve_project_boundary,
)
from ..store import Store
from ..timeutil import now_iso
from .task import cmd_task_complete, cmd_task_create
from .workflow import cmd_outcome_record, cmd_report_import, cmd_verification_run

QUEUE_TODO_STATUSES = {"open", "ready", "triaged", "blocked", "requires_roadmap", "deferred"}


def default_project_id(args: argparse.Namespace) -> str:
    return args.project or Path.cwd().name


def active_tasks_for_project(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    from .. import cli as c

    tasks, statuses = c.project_tasks_and_statuses(store, project_id)
    return [task for task in tasks if not c.is_task_completed_status(statuses[task["id"]])], statuses


def first_active_task_for_project(store: Store, project_id: str) -> dict | None:
    from .. import cli as c

    for task in store.list_where("tasks", "project_id=?", (project_id,)):
        status = c.projected_task_status(store, task)
        if not c.is_task_completed_status(status):
            return task
    return None


def work_queue_data(store: Store, project_id: str) -> dict:
    from .. import cli as c

    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks = []
    for task in store.list_where("tasks", "project_id=?", (project_id,)):
        status = c.projected_task_status(store, task)
        if c.is_task_completed_status(status):
            continue
        tasks.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": status,
                "task_type": task["task_type"],
                "risk_level": task["risk_level"],
            }
        )
    todos = []
    for todo in store.list_where("todos", "project_id=?", (project_id,)):
        if todo["status"] not in QUEUE_TODO_STATUSES:
            continue
        todos.append(
            {
                "id": todo["id"],
                "title": todo["title"],
                "status": todo["status"],
                "priority": todo["priority"],
                "kind": todo["kind"],
            }
        )
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "counts": {
            "tasks": len(tasks),
            "todos": len(todos),
            "total": len(tasks) + len(todos),
        },
        "tasks": tasks,
        "todos": todos,
    }


def no_active_task_recovery_message(project_id: str) -> str:
    return (
        f"active task not found for project: {project_id}. "
        "Before implementation, create or select a Nilo task. "
        f'For a concrete implementation request, run `nilo start "<short title>" --project {project_id}`, '
        "then rerun `nilo check ...` or pass `--task <task_id>`."
    )


def multiple_active_tasks_recovery_message(project_id: str, active_tasks: list[dict]) -> str:
    sorted_tasks = sorted(active_tasks, key=lambda task: task["id"])
    ids = ", ".join(task["id"] for task in sorted_tasks[:5])
    suffix = "" if len(active_tasks) <= 5 else f", ... ({len(active_tasks)} total)"
    return (
        f"multiple active tasks for project: {project_id}. "
        "nilo check refuses to guess because verification evidence must be attached to exactly one task. "
        f"Pass `--task <task_id>` to record this verification on the intended task: {ids}{suffix}. "
        "If this command is not evidence for any active task, do not attach it to an unrelated task; "
        "run it outside `nilo check` or create/select the correct task first."
    )


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
        raise SystemExit(no_active_task_recovery_message(project_id))
    if len(active_tasks) > 1:
        raise SystemExit(multiple_active_tasks_recovery_message(project_id, active_tasks))
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
    print(f"{field_label('task')}: {task['id']}")
    print(f"{field_label('title')}: {task['title']}")
    print(f"{field_label('status')}: {status_label(status)}")
    print(f"{field_label('next_action')}:")
    pending_review = p.latest_pending_review_request(store, task_id)
    if pending_review:
        print(f"- {human_next_action_text(p.next_action_for_review_request(store, pending_review))}")
        return
    for action in p.task_next_actions(task, status, verification_run, unexecuted):
        print(f"- {human_next_action_text(action)}")


def cmd_facade_status(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    store = Store(args.db)
    try:
        if getattr(args, "ai", False) or getattr(args, "json", False):
            try:
                data = project_ai_context(store, project_id)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            data["project_boundary"] = boundary.to_dict()
            if getattr(args, "json", False):
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                if boundary.should_print_text():
                    print("\n".join(boundary.text_lines()))
                    for warning in boundary_warning_lines(boundary):
                        print(warning)
                    print()
                print(render_ai_context_text({key: value for key, value in data.items() if key != "project_boundary"}, max_chars=AI_CONTEXT_TEXT_MAX_CHARS))
            return
        if boundary.should_print_text():
            for line in boundary.text_lines():
                print(line)
            for warning in boundary_warning_lines(boundary):
                print(warning)
        summary = summary_for_project(store, project_id)
        if not getattr(args, "verbose", False):
            project = store.get("projects", project_id)
            if not project:
                raise SystemExit(f"project not found: {project_id}")
            active_tasks, statuses = active_tasks_for_project(store, project_id)
            from .. import cli as c

            c.print_human_project_status(store, project, active_tasks, statuses)
            return
        print(f"{field_label('project')}: {summary['project_id']} ({summary['project_name']})")
        print(f"ロードマップ: {summary['roadmap_position']}")
        print(f"作業状態: {summary['work_state']}")
        print("作業中:")
        if summary["active_tasks"]:
            for task in summary["active_tasks"]:
                print(f"- {task['id']} [{status_label(task['status'])}] {field_label('task_type')}: {task['task_type']} {task['title']}")
        else:
            print("- なし")
        print("TODO:")
        if summary["todo_status_counts"]:
            for status, count in summary["todo_status_counts"].items():
                print(f"- {status_label(status)}: {count}")
        else:
            print("- なし")
        print(f"{field_label('next_action')}:")
        actions = summary["next_actions"] or []
        if actions:
            for action in actions[:3]:
                print(f"- {human_next_action_text(action)}")
        else:
            print("- なし")
    finally:
        store.close()


def cmd_facade_next(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    store = Store(args.db)
    try:
        if boundary.should_print_text():
            for line in boundary.text_lines():
                print(line)
            for warning in boundary_warning_lines(boundary):
                print(warning)
            print()
        if getattr(args, "task", None):
            print_facade_next_for_task(store, args.task)
            return
        project = store.get("projects", project_id)
        if not project:
            raise SystemExit(f"project not found: {project_id}")
        refresh_review_dispatch_state(store, project_id)
        active_task = first_active_task_for_project(store, project_id)
        if active_task:
            print_facade_next_for_task(store, active_task["id"])
            return
        summary = summary_for_project(store, project_id)
        print(f"{field_label('project')}: {summary['project_id']} ({summary['project_name']})")
        print(f"{field_label('next_action')}:")
        actions = summary["next_actions"] or []
        if actions:
            print(f"- {human_next_action_text(actions[0])}")
        else:
            print("- なし")
    finally:
        store.close()


def cmd_facade_queue(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    store = Store(args.db)
    try:
        data = work_queue_data(store, project_id)
        if getattr(args, "json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print(f"{field_label('project')}: {data['project_id']} ({data['project_name']})")
        print(f"queue: total={data['counts']['total']} tasks={data['counts']['tasks']} todos={data['counts']['todos']}")
        print(f"{field_label('tasks')}:")
        if data["tasks"]:
            for task in data["tasks"]:
                print(
                    f"- {task['id']} [{status_label(task['status'])}] "
                    f"{task['task_type']} {task['risk_level']} {task['title']}"
                )
        else:
            print("- なし")
        print("TODO:")
        if data["todos"]:
            for todo in data["todos"]:
                print(f"- {todo['id']} [{status_label(todo['status'])}] {todo['priority']} {todo['title']}")
        else:
            print("- なし")
    finally:
        store.close()


def cmd_facade_start(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    if getattr(args, "self_development", False):
        try:
            assert_self_development_allowed(boundary)
        except ProjectBoundaryError as exc:
            raise SystemExit(str(exc)) from exc
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
    cmd_verification_run(argparse.Namespace(db=args.db, task=task_id, command=args.command, mode=args.mode, timeout=args.timeout))


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
