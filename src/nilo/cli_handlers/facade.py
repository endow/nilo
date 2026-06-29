from __future__ import annotations

import argparse
import io
import json
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from ..ai_context import AI_CONTEXT_TEXT_MAX_CHARS, project_ai_context, render_ai_context_text
from ..display_labels import bool_label, field_label, status_label
from ..failure import deterministic_id
from ..human_status import human_next_action_text
from ..open_state import open_state_detector
from ..project_logic import fast_project_tasks_and_recorded_statuses, fast_unfinished_verification_targets
from ..project_boundary import (
    ProjectBoundaryError,
    assert_self_development_allowed,
    boundary_warning_lines,
    resolve_project_boundary,
)
from ..snapshot import commit_aware_evidence_status, current_git_snapshot_fast, current_git_snapshot_full
from ..store import Store
from ..task_logic import active_task_completion, completion_audit_issues, is_task_completed_status, projected_task_status
from ..timeutil import now_iso
from ..workflow_context import workflow_context
from .task import cmd_task_complete, cmd_task_create
from .workflow import cmd_outcome_record, cmd_report_import, cmd_verification_run

QUEUE_TODO_STATUSES = {"open", "ready", "triaged", "blocked", "requires_roadmap", "deferred"}
NEXT_TODO_STATUS_PRIORITY = {"ready": 0, "requires_roadmap": 1}


def default_project_id(args: argparse.Namespace) -> str:
    return args.project or Path.cwd().name


def active_tasks_for_project(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    tasks, statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    return [task for task in tasks if not is_task_completed_status(statuses[task["id"]])], statuses


def first_active_task_for_project(store: Store, project_id: str) -> dict | None:
    tasks, statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    for task in tasks:
        if not is_task_completed_status(statuses[task["id"]]):
            return task
    return None


def unresolved_project_state(store: Store, project_id: str, *, verbose: bool = False) -> dict:
    return open_state_detector(store, project_id, verbose=verbose)


def first_next_todo_for_project(store: Store, project_id: str) -> dict | None:
    todos = [
        item
        for item in store.list_where("todos", "project_id=?", (project_id,))
        if item["status"] in NEXT_TODO_STATUS_PRIORITY
    ]
    if not todos:
        return None
    return sorted(todos, key=lambda item: (NEXT_TODO_STATUS_PRIORITY[item["status"]], item["created_at"], item["id"]))[0]


def work_queue_data(store: Store, project_id: str, *, audit: bool = False, verbose: bool = False) -> dict:
    from .. import cli as c

    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks = []
    completion_audit_tasks = []
    snapshot = current_git_snapshot_full(Path.cwd()) if audit else None
    task_rows, recorded_statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    for task in task_rows:
        status = c.projected_task_status(store, task, current_snapshot=snapshot) if audit else recorded_statuses[task["id"]]
        if c.is_task_completed_status(status):
            if audit:
                audit_issues = completion_audit_issues(store, task, current_snapshot=snapshot)
                if not audit_issues:
                    continue
                completion = active_task_completion(store, task["id"])
                completion_audit_tasks.append(
                    {
                        "id": task["id"],
                        "title": task["title"],
                        "status": "invalid_completion",
                        "projected_status": status,
                        "task_type": task["task_type"],
                        "risk_level": task["risk_level"],
                        "completion_id": completion["id"] if completion else "",
                        "audit_issues": audit_issues,
                    }
                )
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
    if audit:
        tasks.extend(completion_audit_tasks)
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
    unresolved = unresolved_project_state(store, project_id, verbose=verbose) if (audit or verbose) else {}
    unresolved_total = sum(value for key, value in unresolved.items() if isinstance(value, int))
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "counts": {
            "tasks": len(tasks),
            "todos": len(todos),
            "total": len(tasks) + len(todos),
            "completion_audit_tasks": len(completion_audit_tasks),
            "unresolved_project_state": unresolved_total,
        },
        "tasks": tasks,
        "todos": todos,
        "completion_audit_tasks": completion_audit_tasks,
        "unresolved_project_state": unresolved,
    }


def no_active_task_recovery_message(project_id: str) -> str:
    return (
        f"active task not found for project: {project_id}. "
        "Before implementation, create or select a Nilo task. "
        f'For a concrete implementation request, run `nilo start "<short title>" --project {project_id}`, '
        "then rerun `nilo check --task <task_id> \"...\"`."
    )


def multiple_active_tasks_recovery_message(project_id: str, active_tasks: list[dict]) -> str:
    sorted_tasks = sorted(active_tasks, key=lambda task: task["id"])
    ids = ", ".join(task["id"] for task in sorted_tasks[:5])
    suffix = "" if len(active_tasks) <= 5 else f", ... ({len(active_tasks)} total)"
    example = sorted_tasks[0]["id"] if sorted_tasks else "<task_id>"
    return (
        f"multiple active tasks for project: {project_id}. "
        "nilo check refuses to guess because verification evidence must be attached to exactly one task. "
        f"Pass `--task <task_id>` to record this verification on the intended task: {ids}{suffix}. "
        f"Example: `nilo check --task {example} \"...\"`. "
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


def explicit_check_task_warning(store: Store, task: dict) -> str:
    status = fast_project_tasks_and_recorded_statuses(store, task["project_id"])[1].get(task["id"], task["status"])
    if is_task_completed_status(status):
        return f"warning: recording verification on completed task {task['id']} ({status_label(status)}) because --task was explicit"
    return ""


def resolve_check_task_id(args: argparse.Namespace, store: Store) -> tuple[str, str]:
    if getattr(args, "task", None):
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        return args.task, explicit_check_task_warning(store, task)

    project_id = default_project_id(args)
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    candidates = fast_unfinished_verification_targets(store, project_id)
    if not candidates:
        raise SystemExit(no_active_task_recovery_message(project_id))
    if len(candidates) > 1:
        raise SystemExit(multiple_active_tasks_recovery_message(project_id, candidates))
    return candidates[0]["id"], ""


def summary_for_project(store: Store, project_id: str) -> dict:
    from .. import cli as c

    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks, statuses = c.project_tasks_and_statuses(store, project_id)
    return c.project_summary_data(store, project, tasks, statuses)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _short_command(command: str, limit: int = 80) -> str:
    command = " ".join((command or "").split())
    if len(command) <= limit:
        return command
    return command[: limit - 3] + "..."


def _fast_evidence_status(verification_run: dict | None, snapshot: dict[str, Any]) -> str:
    if not verification_run:
        return "missing"
    if verification_run.get("timed_out") or verification_run.get("exit_code") not in (0, "0"):
        return "failed"
    if not snapshot.get("git_available", True):
        return "not checked in fast status"
    return "recorded"


def _fast_todo_counts(store: Store, project_id: str) -> dict[str, int]:
    rows = store.conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM todos
        WHERE project_id=? AND status IN ('ready', 'open')
        GROUP BY status
        """,
        (project_id,),
    ).fetchall()
    counts = {"ready": 0, "open": 0}
    counts.update({row["status"]: row["count"] for row in rows})
    return counts


def _fast_active_tasks_and_statuses(store: Store, project_id: str, *, limit: int = 3) -> tuple[list[dict[str, Any]], dict[str, str]]:
    statuses: dict[str, str] = {}
    active_tasks = []
    offset = 0
    batch_size = max(limit * 4, 12)
    while len(active_tasks) < limit:
        rows = store.conn.execute(
            """
            SELECT *
            FROM tasks t
            WHERE t.project_id=?
              AND t.status NOT IN ('completed_by_ai', 'completed_by_user')
              AND NOT EXISTS (
                SELECT 1
                FROM task_completions c
                WHERE c.task_id=t.id AND COALESCE(c.invalidated_at, '')=''
              )
            ORDER BY t.created_at DESC, t.rowid DESC
            LIMIT ? OFFSET ?
            """,
            (project_id, batch_size, offset),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            task = store._decode_row(row, "tasks")
            latest = store.latest_task_status_event(task["id"])
            status = latest["status"] if latest else task["status"]
            if not is_task_completed_status(status):
                active_tasks.append(task)
                statuses[task["id"]] = status
                if len(active_tasks) >= limit:
                    break
        offset += len(rows)
    return active_tasks[:limit], statuses


def fast_status_data(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Build the lightweight human status payload.

    This deliberately avoids full snapshots, roadmap assessment, commit mapping,
    history, failure-log summaries, and projected status audits.
    """
    timings: dict[str, float] = {}
    total_started = time.perf_counter()

    started = time.perf_counter()
    project = store.get("projects", project_id)
    timings["project_lookup_ms"] = _elapsed_ms(started)
    if not project:
        raise SystemExit(f"project not found: {project_id}")

    started = time.perf_counter()
    active_tasks, statuses = _fast_active_tasks_and_statuses(store, project_id, limit=3)
    timings["fast_task_query_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    snapshot = current_git_snapshot_fast(cwd or Path.cwd())
    timings["git_fast_status_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    task_summaries = []
    for task in active_tasks:
        verification_run = store.latest_for_task("verification_runs", task["id"])
        task_summaries.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": statuses[task["id"]],
                "task_type": task["task_type"],
                "risk_level": task["risk_level"],
                "latest_verification_run": {
                    "exit_code": verification_run["exit_code"] if verification_run else None,
                    "timed_out": bool(verification_run["timed_out"]) if verification_run else False,
                    "command": _short_command(verification_run["command"]) if verification_run else "",
                },
                "evidence": _fast_evidence_status(verification_run, snapshot),
            }
        )
    timings["verification_lookup_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    task_ids = [task["id"] for task in active_tasks]
    blocking_counts: dict[str, int] = {}
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        rows = store.conn.execute(
            f"""
            SELECT task_id, COUNT(*) AS count
            FROM review_findings
            WHERE task_id IN ({placeholders}) AND status='unresolved' AND blocking=1
            GROUP BY task_id
            """,
            tuple(task_ids),
        ).fetchall()
        blocking_counts = {row["task_id"]: row["count"] for row in rows}
    for task in task_summaries:
        task["unresolved_blocking_review_findings"] = int(blocking_counts.get(task["id"], 0))
    timings["review_count_ms"] = _elapsed_ms(started)

    todo_counts = _fast_todo_counts(store, project_id)
    timings["total_ms"] = _elapsed_ms(total_started)

    return {
        "project": {"id": project["id"], "name": project["name"]},
        "active_tasks": task_summaries,
        "todo_counts": todo_counts,
        "git": {
            "head": snapshot.get("git_head"),
            "tracked_dirty": bool(snapshot.get("working_tree_dirty")),
            "git_available": bool(snapshot.get("git_available")),
        },
        "timings": timings,
    }


def print_fast_status(data: dict[str, Any], *, debug_timing: bool = False) -> None:
    project = data["project"]
    active_tasks = data["active_tasks"]
    print(f"{field_label('project')}: {project['id']} ({project['name']})")
    print(
        "git: "
        f"head={data['git']['head'] or 'none'} "
        f"tracked_dirty={bool_label(data['git']['tracked_dirty'])} "
        f"available={bool_label(data['git']['git_available'])}"
    )
    if not active_tasks:
        print(f"{field_label('status')}: {status_label('no_active_task')}")
        todos = data["todo_counts"]
        print(f"todo: ready={todos['ready']} open={todos['open']}")
        print(f"{field_label('next_action')}:")
        print("- 作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。")
    else:
        print(f"{field_label('status')}: {status_label('in_progress')}")
        print("作業中のタスク:")
        for task in active_tasks:
            verification = task["latest_verification_run"]
            verification_bits = []
            if verification["command"]:
                verification_bits.append(f"command={verification['command']}")
            if verification["exit_code"] is not None:
                verification_bits.append(f"exit_code={verification['exit_code']}")
            verification_bits.append(f"timed_out={str(verification['timed_out']).lower()}")
            print(f"- {task['id']} [{status_label(task['status'])}] {task['title']}")
            print(f"  {field_label('evidence')}: {status_label(task['evidence'])}")
            print(f"  {field_label('latest_verification_run')}: {', '.join(verification_bits)}")
            print(f"  {field_label('unresolved_blocking_count')}: {task['unresolved_blocking_review_findings']}")
        todos = data["todo_counts"]
        print(f"todo: ready={todos['ready']} open={todos['open']}")
        print(f"{field_label('next_action')}:")
        first = active_tasks[0]
        print(f"- 次は {first['id']} の状態と検証結果を確認してください。")
    if debug_timing:
        print("debug_timing:")
        for key in (
            "project_lookup_ms",
            "fast_task_query_ms",
            "git_fast_status_ms",
            "verification_lookup_ms",
            "review_count_ms",
            "total_ms",
        ):
            print(f"- {key}: {data['timings'][key]}")


def audit_status_data(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    snapshot = current_git_snapshot_full(cwd or Path.cwd())
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    audited_tasks = []
    for task in tasks:
        status = projected_task_status(store, task, current_snapshot=snapshot)
        verification_run = store.latest_for_task("verification_runs", task["id"])
        completion = active_task_completion(store, task["id"])
        evidence = commit_aware_evidence_status(verification_run, snapshot, completion)
        blocking_count = store.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM review_findings
            WHERE task_id=? AND status='unresolved' AND blocking=1
            """,
            (task["id"],),
        ).fetchone()["count"]
        audited_tasks.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": status,
                "evidence": evidence,
                "verification_exit_code": verification_run["exit_code"] if verification_run else None,
                "verification_timed_out": bool(verification_run["timed_out"]) if verification_run else False,
                "unresolved_blocking_review_findings": int(blocking_count or 0),
            }
        )
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "git": {
            "head": snapshot.get("git_head"),
            "dirty": bool(snapshot.get("working_tree_dirty")),
            "git_available": bool(snapshot.get("git_available")),
            "diff_hash_computed": bool(snapshot.get("git_diff_hash_computed")),
        },
        "tasks": audited_tasks,
    }


def print_audit_status(data: dict[str, Any]) -> None:
    project = data["project"]
    print(f"{field_label('project')}: {project['id']} ({project['name']})")
    print("モード: 厳密監査")
    print(
        "git: "
        f"head={data['git']['head'] or 'none'} "
        f"dirty={bool_label(data['git']['dirty'])} "
        f"available={bool_label(data['git']['git_available'])} "
        f"diff_hash_computed={bool_label(data['git']['diff_hash_computed'])}"
    )
    print("タスク監査:")
    if not data["tasks"]:
        print("- なし")
        return
    for task in data["tasks"]:
        bits = [f"evidence={task['evidence']}"]
        if task["verification_exit_code"] is not None:
            bits.append(f"exit_code={task['verification_exit_code']}")
        bits.append(f"timed_out={str(task['verification_timed_out']).lower()}")
        bits.append(f"blocking_reviews={task['unresolved_blocking_review_findings']}")
        print(f"- {task['id']} [{status_label(task['status'])}] {task['title']}")
        print(f"  {', '.join(bits)}")


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
    for action in p.task_next_actions(task, status, verification_run, unexecuted)[:1]:
        print(f"- {human_next_action_text(action)}")


def cmd_facade_status(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    store = Store(args.db)
    try:
        if getattr(args, "ai", False) or getattr(args, "json", False):
            try:
                snapshot_mode = "full" if getattr(args, "verbose", False) else "fast"
                data = project_ai_context(store, project_id, snapshot_mode=snapshot_mode, verbose=bool(getattr(args, "verbose", False)))
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
                max_chars = None if getattr(args, "verbose", False) else AI_CONTEXT_TEXT_MAX_CHARS
                print(render_ai_context_text({key: value for key, value in data.items() if key != "project_boundary"}, max_chars=max_chars))
            return
        if boundary.should_print_text():
            for line in boundary.text_lines():
                print(line)
            for warning in boundary_warning_lines(boundary):
                print(warning)
        if not getattr(args, "verbose", False) and not getattr(args, "audit", False):
            print_fast_status(fast_status_data(store, project_id), debug_timing=bool(getattr(args, "debug_timing", False)))
            return
        if getattr(args, "audit", False):
            print_audit_status(audit_status_data(store, project_id))
            return
        summary = summary_for_project(store, project_id)
        print(f"{field_label('project')}: {summary['project_id']} ({summary['project_name']})")
        print("モード: 詳細表示")
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
        workflow = workflow_context(store, project_id)
        if workflow.get("type") == "recipe_run":
            print(f"{field_label('project')}: {project_id} ({project['name']})")
            if getattr(args, "verbose", False):
                print("workflow_context:")
                print(f"- recipe: {workflow['recipe_name']}")
                print(f"- status: {workflow['status']}")
                print(f"- current_step: {workflow['current_step']}")
                print(f"- next_step: {workflow['next_step']}")
                if workflow.get("pending_public_operations"):
                    print("pending_public_operations:")
                    for operation in workflow["pending_public_operations"]:
                        print(f"- {operation['operation']}: {operation['target']}")
                    if workflow.get("approval_prompt"):
                        print(workflow["approval_prompt"])
                    if workflow.get("public_execution_command"):
                        print(f"execute_after_approval: {workflow['public_execution_command']}")
            print(f"{field_label('next_action')}:")
            if workflow.get("status") == "waiting_public_approval":
                print("- Release recipe is waiting for explicit public operation approval.")
                if workflow.get("public_execution_command"):
                    if getattr(args, "verbose", False):
                        print(f"- After approval, execute: {workflow['public_execution_command']}")
                    else:
                        print(f"execute_after_approval: {workflow['public_execution_command']}")
            else:
                print(f"- Continue release recipe step: {workflow['next_step']}")
            if not getattr(args, "verbose", False):
                print(f"details: nilo status --ai --verbose --project {project_id}")
            return
        active_task = first_active_task_for_project(store, project_id)
        if active_task:
            print_facade_next_for_task(store, active_task["id"])
            return
        print(f"{field_label('project')}: {project_id} ({project['name']})")
        print(f"{field_label('next_action')}:")
        todo = first_next_todo_for_project(store, project_id)
        if todo:
            if todo["status"] == "requires_roadmap":
                print(f"- この依頼は大きめなので、作業計画の確認後に Task 化します。{todo['id']}: {todo['title']}")
            else:
                print(f"- 実行できる依頼を具体的な Task にします。{todo['id']}: {todo['title']}")
        else:
            print("- 作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。")
    finally:
        store.close()


def cmd_facade_queue(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    store = Store(args.db)
    try:
        verbose = bool(getattr(args, "verbose", False))
        data = work_queue_data(store, project_id, audit=bool(getattr(args, "audit", False)), verbose=verbose)
        if getattr(args, "json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print(f"{field_label('project')}: {data['project_id']} ({data['project_name']})")
        print(f"queue: total={data['counts']['total']} tasks={data['counts']['tasks']} todos={data['counts']['todos']}")
        unresolved = data["unresolved_project_state"]
        if unresolved:
            unresolved_summary = (
                f"failures={unresolved['failures']} review_findings={unresolved['review_findings']} "
                f"evidence_issues={unresolved['evidence_issues']} roadmap_commitments={unresolved['roadmap_commitments']} "
                f"pending_roadmap_revisions={unresolved['pending_roadmap_revisions']} "
                f"invalid_completions={unresolved['invalid_completions']} "
                f"review_dispatches={unresolved['review_dispatches']} overdrive_runs={unresolved['overdrive_runs']}"
            )
            if data["counts"]["total"] == 0 and data["counts"]["unresolved_project_state"]:
                print(f"task/todo queue is empty, but unresolved project state remains: {unresolved_summary}")
            else:
                print(f"unresolved_project_state: {unresolved_summary}")
        if data["counts"]["completion_audit_tasks"] and not getattr(args, "audit", False):
            print(f"completion_audit: {data['counts']['completion_audit_tasks']} completed task(s) need audit; rerun with --audit")
        print(f"{field_label('tasks')}:")
        if data["tasks"]:
            for task in data["tasks"]:
                issue_suffix = f" issues={','.join(task.get('audit_issues', []))}" if task.get("audit_issues") else ""
                print(
                    f"- {task['id']} [{status_label(task['status'])}] "
                    f"{task['task_type']} {task['risk_level']} {task['title']}{issue_suffix}"
                )
        else:
            print("- なし")
        print("TODO:")
        if data["todos"]:
            for todo in data["todos"]:
                print(f"- {todo['id']} [{status_label(todo['status'])}] {todo['priority']} {todo['title']}")
        else:
            print("- なし")
        if verbose and unresolved.get("details"):
            print("unresolved_details:")
            for kind, items in unresolved["details"].items():
                print(f"- {kind}: {len(items)}")
                for item in items[:20]:
                    fields = " ".join(f"{key}={value}" for key, value in item.items())
                    print(f"  - {fields}")
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
        task_id, warning = resolve_check_task_id(args, store)
    finally:
        store.close()
    if warning:
        print(warning)
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
            human_confirm=args.human_confirm,
            decision_note=args.decision_note,
            human_acceptance=args.human_acceptance,
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
