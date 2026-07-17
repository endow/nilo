from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import Store
from .task_analytics import is_open_finding, project_task_analytics, projected_status_without_snapshot
from .task_logic import is_task_completed_status


TEXT_PREVIEW_CHARS = 600
OPEN_TODO_STATUSES = {"open", "triaged", "ready", "ad_hoc_approved", "requires_roadmap", "blocked"}


def overview(db_path: Path | None, project_id: str) -> dict[str, Any]:
    store = _open_store(db_path)
    try:
        project = _project_or_exit(store, project_id)
        tasks = _project_tasks(store, project_id)
        task_ids = {task["id"] for task in tasks}
        status_by_task = _batch_statuses_for_tasks(store, tasks, task_ids)
        open_tasks = [task for task in tasks if not is_task_completed_status(status_by_task[task["id"]])]
        completed_task_ids = {task_id for task_id, status in status_by_task.items() if is_task_completed_status(status)}
        todo_rows = store.list_where("todos", "project_id=?", (project_id,))
        open_todos = [row for row in todo_rows if row.get("status") in OPEN_TODO_STATUSES]
        open_failures = store.list_where("failure_logs", "project_id=? AND status='open'", (project_id,))
        open_failure_task_ids = {failure["task_id"] for failure in open_failures}
        open_blocking = [
            finding
            for finding in _rows_for_task_ids(store, "review_findings", task_ids)
            if is_open_finding(finding) and bool(finding.get("blocking"))
        ]
        open_blocking_task_ids = {finding["task_id"] for finding in open_blocking}
        analytics_data = project_task_analytics(store, project_id)
        from .project_boundary import resolve_project_boundary
        from .work_projection import next_action_text, project_work_projection

        boundary = resolve_project_boundary(db_path=store.path)
        projection_root = store.path.parent.parent if store.path.parent.name == ".nilo" else boundary.project_root
        projection = project_work_projection(store, project_id, cwd=projection_root)
        return {
            "project": _project_payload(project, store.path),
            "summary": {
                "open_tasks": len(open_tasks),
                "completed_tasks": len(completed_task_ids),
                "open_todos": len(open_todos),
                "open_failure_logs": len(open_failure_task_ids),
                "open_blocking_findings": len(open_blocking_task_ids),
            },
            "active_task": _compact_task_row(store, open_tasks[0]) if len(open_tasks) == 1 else None,
            "next_action": next_action_text(projection),
            "work_projection": projection.to_dict(),
            "latest_verification": _latest_for_project(store, "verification_runs", task_ids),
            "latest_review": _latest_for_project(store, "review_results", task_ids),
            "analytics_summary": analytics_data.get("summary", {}),
        }
    finally:
        store.close()


def analytics(db_path: Path | None, project_id: str) -> dict[str, Any]:
    store = _open_store(db_path)
    try:
        _project_or_exit(store, project_id)
        return project_task_analytics(store, project_id)
    finally:
        store.close()


def tasks(
    db_path: Path | None,
    project_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    status: str = "",
    task_type: str = "",
    risk_level: str = "",
    open_findings: bool = False,
    open_failures: bool = False,
    reservations: bool = False,
    roadmap: str = "",
) -> dict[str, Any]:
    store = _open_store(db_path)
    try:
        _project_or_exit(store, project_id)
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        all_tasks = _project_tasks(store, project_id)
        all_task_ids = {task["id"] for task in all_tasks}
        status_by_task = _batch_statuses_for_tasks(store, all_tasks, all_task_ids)
        from .project_logic import accepted_roadmap_commitments, ordered_roadmap_commitments

        commitments = ordered_roadmap_commitments(
            store,
            accepted_roadmap_commitments(store, project_id),
            all_tasks,
            status_by_task,
        )
        current_commitment_ids = {commitments[0]["id"]} if commitments else set()
        completion_filter = status if status in {"current", "accepted", "cancelled", "superseded", "legacy_pending", "inconsistent"} else ""
        normal_status = "" if completion_filter else status
        all_tasks = _filter_tasks(
            store,
            all_tasks,
            status_by_task,
            status=normal_status,
            task_type=task_type,
            risk_level=risk_level,
            open_findings=open_findings,
            open_failures=open_failures,
            reservations=reservations,
            roadmap=roadmap,
        )
        from .completion_projection import project_completion_projections

        completion_projections = project_completion_projections(
            store,
            project_id,
            all_tasks,
            current_commitment_ids=current_commitment_ids,
            statuses=status_by_task,
        )
        if completion_filter:
            def matches_completion_filter(task: dict[str, Any]) -> bool:
                projection = completion_projections[task["id"]]
                if completion_filter == "current":
                    return projection.is_current_work
                if completion_filter == "accepted":
                    return projection.stage.value in {"accepted", "accepted_with_reservations"}
                return projection.stage.value == completion_filter
            all_tasks = [task for task in all_tasks if matches_completion_filter(task)]
        total = len(all_tasks)
        start = (page - 1) * page_size
        page_tasks = all_tasks[start : start + page_size]
        return {
            "tasks": [
                {**row, "completion_projection": completion_projections[row["id"]].to_dict()}
                for row in _task_list_rows(store, page_tasks, status_by_task=status_by_task)
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if total else 1,
            },
        }
    finally:
        store.close()


def todos(db_path: Path | None, project_id: str) -> dict[str, Any]:
    store = _open_store(db_path)
    try:
        _project_or_exit(store, project_id)
        rows = store.list_where("todos", "project_id=?", (project_id,))
        return {
            "todos": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "kind": row.get("kind", ""),
                    "status": row.get("status", ""),
                    "priority": row.get("priority", ""),
                    "description": row.get("description", ""),
                    "acceptance_hint": row.get("acceptance_hint", ""),
                    "source_type": row.get("source_type", ""),
                    "source_task_id": row.get("source_task_id", ""),
                    "roadmap_commitment_id": row.get("roadmap_commitment_id", ""),
                    "roadmap_revision_id": row.get("roadmap_revision_id", ""),
                    "converted_task_id": row.get("converted_task_id", ""),
                    "triaged_at": row.get("triaged_at", ""),
                    "triage_reason": row.get("triage_reason", ""),
                    "created_at": row.get("created_at", ""),
                }
                for row in rows
            ],
            "summary": {
                "total": len(rows),
                "open": sum(1 for row in rows if row.get("status") in OPEN_TODO_STATUSES),
                "ready": sum(1 for row in rows if row.get("status") in {"ready", "ad_hoc_approved"}),
                "blocked": sum(1 for row in rows if row.get("status") == "blocked"),
                "converted": sum(1 for row in rows if row.get("status") == "converted_to_task"),
            },
        }
    finally:
        store.close()


def task_detail(db_path: Path | None, project_id: str, task_id: str) -> dict[str, Any]:
    store = _open_store(db_path)
    try:
        _project_or_exit(store, project_id)
        task = store.get("tasks", task_id)
        if not task or task.get("project_id") != project_id:
            raise KeyError(f"task not found: {task_id}")
        completions = store.list_where("task_completions", "task_id=? AND COALESCE(invalidated_at, '')=''", (task_id,))
        verification_runs = store.list_where("verification_runs", "task_id=?", (task_id,))
        review_results = store.list_where("review_results", "task_id=?", (task_id,))
        return {
            "task": {
                "id": task["id"],
                "project_id": task["project_id"],
                "title": task["title"],
                "description": task.get("description", ""),
                "acceptance_criteria": task.get("acceptance_criteria") or [],
                "status": projected_status_without_snapshot(store, task),
                "task_type": task.get("task_type", ""),
                "risk_level": task.get("risk_level", ""),
                "mode": task.get("mode", "normal"),
                "roadmap_commitment_id": task.get("roadmap_commitment_id", ""),
                "roadmap_item_id": task.get("roadmap_item_id", ""),
                "created_at": task.get("created_at", ""),
            },
            "completion": {"completions": [_completion_payload(row) for row in completions]},
            "accepted_verification_runs": [
                _verification_payload(row) for row in _accepted_rows(verification_runs, completions, "accepted_verification_run_ids")
            ],
            "accepted_review_results": [
                _review_result_payload(row) for row in _accepted_rows(review_results, completions, "accepted_review_result_ids")
            ],
            "verification_history": [_verification_payload(row, include_output=True) for row in verification_runs],
            "review_results": [_review_result_payload(row, include_body=True) for row in review_results],
            "review_findings": store.list_where("review_findings", "task_id=?", (task_id,)),
            "failure_logs": store.list_where("failure_logs", "task_id=?", (task_id,)),
            "transition_events": _task_transition_events(store, task_id),
            "analytics": _safe_task_analytics(store, task_id),
        }
    finally:
        store.close()


def timeline(db_path: Path | None, project_id: str, *, limit: int = 200) -> dict[str, Any]:
    store = _open_store(db_path)
    try:
        _project_or_exit(store, project_id)
        tasks_by_id = {task["id"]: task for task in _project_tasks(store, project_id)}
        task_ids = set(tasks_by_id)
        events: list[dict[str, Any]] = []
        events.extend(_task_timeline_events(store, tasks_by_id))
        events.extend(_table_events(store, "verification_runs", task_ids, "verification_recorded", "Verification recorded"))
        events.extend(_table_events(store, "review_requests", task_ids, "review_requested", "Review requested"))
        events.extend(_table_events(store, "review_results", task_ids, "review_imported", "Review imported"))
        events.extend(_table_events(store, "review_finding_updates", task_ids, "finding_updated", "Finding updated"))
        events.extend(_table_events(store, "task_completions", task_ids, "completion_recorded", "Completion recorded"))
        events.extend(_table_events(store, "failure_logs", task_ids, "failure_logged", "Failure logged"))
        overdrive_events = [
            _event_payload(row, "overdrive_event", "overdrive", row.get("task_id") or project_id, row.get("event_type", "Overdrive"), row.get("message", ""))
            for row in store.list_where("overdrive_events", "project_id=?", (project_id,))
        ]
        events.extend(overdrive_events)
        events = sorted(events, key=lambda row: row.get("created_at", ""), reverse=True)
        return {"events": events[:limit]}
    finally:
        store.close()


def _open_store(db_path: Path | None) -> Store:
    return Store(db_path, read_only=True)


def _project_or_exit(store: Store, project_id: str) -> dict[str, Any]:
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    return project


def _project_payload(project: dict[str, Any], db_path: Path) -> dict[str, Any]:
    return {"id": project["id"], "name": project["name"], "db_path": str(db_path)}


def _project_tasks(store: Store, project_id: str) -> list[dict[str, Any]]:
    return store.list_where("tasks", "project_id=?", (project_id,))


def _task_list_rows(store: Store, tasks: list[dict[str, Any]], *, status_by_task: dict[str, str] | None = None) -> list[dict[str, Any]]:
    task_ids = {task["id"] for task in tasks}
    if not task_ids:
        return []
    completions_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    verifications_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    reviews_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    findings_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    failures_by_task: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in task_ids}
    if status_by_task is None:
        status_by_task = _batch_statuses_for_tasks(store, tasks, task_ids)
    for row in _rows_for_task_ids(store, "task_completions", task_ids):
        if not row.get("invalidated_at"):
            completions_by_task[row["task_id"]].append(row)
    for row in _rows_for_task_ids(store, "verification_runs", task_ids):
        verifications_by_task[row["task_id"]].append(row)
    for row in _rows_for_task_ids(store, "review_results", task_ids):
        reviews_by_task[row["task_id"]].append(row)
    for row in _rows_for_task_ids(store, "review_findings", task_ids):
        findings_by_task[row["task_id"]].append(row)
    for row in _rows_for_task_ids(store, "failure_logs", task_ids):
        if row.get("status", "open") == "open":
            failures_by_task[row["task_id"]].append(row)
    return [
        _task_list_row_from_groups(
            store,
            task,
            status_by_task[task["id"]],
            completions_by_task[task["id"]],
            verifications_by_task[task["id"]],
            reviews_by_task[task["id"]],
            findings_by_task[task["id"]],
            failures_by_task[task["id"]],
        )
        for task in tasks
    ]


def _filter_tasks(
    store: Store,
    tasks: list[dict[str, Any]],
    status_by_task: dict[str, str],
    *,
    status: str = "",
    task_type: str = "",
    risk_level: str = "",
    open_findings: bool = False,
    open_failures: bool = False,
    reservations: bool = False,
    roadmap: str = "",
) -> list[dict[str, Any]]:
    task_ids = {task["id"] for task in tasks}
    open_finding_task_ids = {
        row["task_id"]
        for row in _rows_for_task_ids(store, "review_findings", task_ids)
        if is_open_finding(row) and bool(row.get("blocking"))
    } if open_findings else set()
    open_failure_task_ids = {
        row["task_id"]
        for row in _rows_for_task_ids(store, "failure_logs", task_ids)
        if row.get("status", "open") == "open"
    } if open_failures else set()
    reservation_task_ids = {
        row["task_id"]
        for row in _rows_for_task_ids(store, "task_completions", task_ids)
        if not row.get("invalidated_at") and bool(row.get("completed_with_reservations"))
    } if reservations else set()
    filtered = []
    for task in tasks:
        task_id = task["id"]
        projected_status = status_by_task.get(task_id, task.get("status", ""))
        if status == "open" and is_task_completed_status(projected_status):
            continue
        if status == "completed" and not is_task_completed_status(projected_status):
            continue
        if status and status not in {"open", "completed"} and projected_status != status:
            continue
        if task_type and task.get("task_type", "") != task_type:
            continue
        if risk_level and task.get("risk_level", "") != risk_level:
            continue
        if open_findings and task_id not in open_finding_task_ids:
            continue
        if open_failures and task_id not in open_failure_task_ids:
            continue
        if reservations and task_id not in reservation_task_ids:
            continue
        if roadmap == "roadmap" and not task.get("roadmap_commitment_id"):
            continue
        if roadmap == "standalone" and task.get("roadmap_commitment_id"):
            continue
        filtered.append(task)
    return filtered


def _task_list_row_from_groups(
    store: Store,
    task: dict[str, Any],
    status: str,
    completion: list[dict[str, Any]],
    verifications: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_verification = verifications[0] if verifications else None
    return {
        "id": task["id"],
        "title": task["title"],
        "status": status,
        "task_type": task.get("task_type", ""),
        "risk_level": task.get("risk_level", ""),
        "mode": task.get("mode", "normal"),
        "roadmap_commitment_id": task.get("roadmap_commitment_id", ""),
        "roadmap_item_id": task.get("roadmap_item_id", ""),
        "created_at": task.get("created_at", ""),
        "completion": {
            "completed": is_task_completed_status(status),
            "completed_with_reservations": any(bool(row.get("completed_with_reservations")) for row in completion),
            "human_confirmed": any(bool(row.get("human_confirmed")) for row in completion),
        },
        "verification": {
            "run_count": len(verifications),
            "latest_status": _verification_status(latest_verification) if latest_verification else "",
        },
        "review": {
            "result_count": len(reviews),
            "open_blocking_findings": sum(1 for finding in findings if is_open_finding(finding) and bool(finding.get("blocking"))),
        },
        "failure": {"open_count": len(failures)},
    }


def _batch_statuses_for_tasks(store: Store, tasks: list[dict[str, Any]], task_ids: set[str]) -> dict[str, str]:
    events: dict[str, list[tuple[str, int, str]]] = {
        task["id"]: [(task.get("created_at", ""), 10, task.get("status", ""))]
        for task in tasks
    }
    for row in _rows_for_task_ids(store, "understanding_checks", task_ids):
        events[row["task_id"]].append((row.get("created_at", ""), 20, row.get("status", "")))
    for row in _rows_for_task_ids(store, "instructions", task_ids):
        events[row["task_id"]].append((row.get("created_at", ""), 30, "instruction_generated"))
    for row in _rows_for_task_ids(store, "agent_reports", task_ids):
        events[row["task_id"]].append((row.get("created_at", ""), 40, "agent_reported"))
    for row in _rows_for_task_ids(store, "review_requests", task_ids):
        status = _review_request_status(row)
        if status:
            events[row["task_id"]].append((row.get("updated_at", row.get("created_at", "")), 45, status))
    for row in _rows_for_task_ids(store, "verification_runs", task_ids):
        events[row["task_id"]].append((row.get("created_at", ""), 55, _verification_status_for_task(row)))
    for row in _rows_for_task_ids(store, "review_results", task_ids):
        events[row["task_id"]].append((row.get("created_at", ""), 65, _review_result_status(row)))
    for row in _rows_for_task_ids(store, "review_finding_updates", task_ids):
        events[row["task_id"]].append((row.get("created_at", ""), 66, "review_changes_requested"))
    for row in _rows_for_task_ids(store, "task_completions", task_ids):
        if not row.get("invalidated_at"):
            actor = row.get("actor", row.get("completed_by", ""))
            events[row["task_id"]].append((row.get("created_at", ""), 70, "completed_by_ai" if actor == "ai" else "completed_by_user"))
    return {
        task_id: sorted(task_events, key=lambda event: (event[0], event[1]), reverse=True)[0][2]
        for task_id, task_events in events.items()
    }


def _review_request_status(row: dict[str, Any]) -> str:
    status = row.get("status", "")
    if status == "requested":
        return "review_requested"
    if status == "reviewer_unavailable":
        return "review_reviewer_unavailable"
    if status == "claimed":
        return "review_claimed"
    if status == "in_progress":
        return "review_in_progress"
    if status == "stale":
        return "review_stale"
    return ""


def _verification_status_for_task(row: dict[str, Any]) -> str:
    if bool(row.get("timed_out")):
        return "verification_timed_out"
    if row.get("exit_code") == 0:
        return "verification_passed"
    return "verification_failed"


def _review_result_status(row: dict[str, Any]) -> str:
    verdict = row.get("verdict", "")
    if verdict == "approved":
        return "review_approved"
    if verdict == "changes_requested":
        return "review_changes_requested"
    return "review_commented"


def _compact_task_row(store: Store, task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "title": task["title"],
        "status": projected_status_without_snapshot(store, task),
        "task_type": task.get("task_type", ""),
        "risk_level": task.get("risk_level", ""),
        "mode": task.get("mode", "normal"),
        "roadmap_commitment_id": task.get("roadmap_commitment_id", ""),
        "created_at": task.get("created_at", ""),
    }


def _verification_status(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    if bool(row.get("timed_out")):
        return "timed_out"
    if row.get("exit_code") == 0:
        return "passed"
    return "failed"


def _latest_for_project(store: Store, table: str, task_ids: set[str]) -> dict[str, Any] | None:
    rows = _rows_for_task_ids(store, table, task_ids)
    if not rows:
        return None
    row = rows[0]
    if table == "verification_runs":
        return _verification_payload(row)
    if table == "review_results":
        return _review_result_payload(row)
    return row


def _rows_for_task_ids(store: Store, table: str, task_ids: set[str]) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    rows: list[dict[str, Any]] = []
    ids = sorted(task_ids)
    chunk_size = 500
    for index in range(0, len(ids), chunk_size):
        chunk = ids[index : index + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(store.list_where(table, f"task_id IN ({placeholders})", tuple(chunk)))
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def _verification_payload(row: dict[str, Any], *, include_output: bool = False) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "task_id": row["task_id"],
        "command": row.get("command", ""),
        "status": _verification_status(row),
        "exit_code": row.get("exit_code"),
        "timed_out": bool(row.get("timed_out")),
        "created_at": row.get("created_at", ""),
    }
    if include_output:
        payload["stdout"] = _text_payload(row.get("stdout", ""))
        payload["stderr"] = _text_payload(row.get("stderr", ""))
    return payload


def _review_result_payload(row: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "task_id": row["task_id"],
        "reviewer": row.get("reviewer", ""),
        "verdict": row.get("verdict", ""),
        "summary": row.get("summary", ""),
        "created_at": row.get("created_at", ""),
    }
    if include_body:
        payload["body_md"] = _text_payload(row.get("body_md", ""))
    return payload


def _text_payload(value: str) -> dict[str, Any]:
    text = value or ""
    return {"preview": text[:TEXT_PREVIEW_CHARS], "truncated": len(text) > TEXT_PREVIEW_CHARS, "length": len(text)}


def _completion_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "actor": row.get("actor", row.get("completed_by", "")),
        "reason": row.get("reason", ""),
        "human_confirmed": bool(row.get("human_confirmed")),
        "completed_with_reservations": bool(row.get("completed_with_reservations")),
        "accepted_verification_run_ids": row.get("accepted_verification_run_ids") or [],
        "accepted_review_result_ids": row.get("accepted_review_result_ids") or [],
        "created_at": row.get("created_at", ""),
    }


def _accepted_rows(rows: list[dict[str, Any]], completions: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    accepted_ids = {row_id for completion in completions for row_id in (completion.get(field) or [])}
    return [row for row in rows if row["id"] in accepted_ids]


def _task_transition_events(store: Store, task_id: str) -> list[dict[str, Any]]:
    return [
        _event_payload(row, "task_transition", "task", task_id, row.get("transition", "Task transition"), _transition_summary(row))
        for row in store.list_where("transition_events", "entity_type='task' AND entity_id=?", (task_id,))
    ]


def _task_timeline_events(store: Store, tasks_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for task in tasks_by_id.values():
        events.append(_event_payload(task, "task_created", "task", task["id"], "Task created", task.get("title", "")))
    rows = [row for row in store.list_where("transition_events", "entity_type='task'") if row.get("entity_id") in tasks_by_id]
    events.extend(_event_payload(row, "task_transition", "task", row["entity_id"], row.get("transition", ""), _transition_summary(row)) for row in rows)
    return events


def _table_events(store: Store, table: str, task_ids: set[str], event_type: str, title: str) -> list[dict[str, Any]]:
    return [
        _event_payload(row, event_type, "task", row["task_id"], title, _table_event_summary(row, event_type))
        for row in _rows_for_task_ids(store, table, task_ids)
    ]


def _event_payload(row: dict[str, Any], event_type: str, entity_type: str, entity_id: str, title: str, summary: str) -> dict[str, Any]:
    return {
        "created_at": row.get("created_at", ""),
        "type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": title,
        "summary": summary,
    }


def _transition_summary(row: dict[str, Any]) -> str:
    previous = row.get("previous_state", "")
    new = row.get("new_state", "")
    if previous or new:
        return f"{previous} -> {new}".strip()
    return row.get("reason", "")


def _table_event_summary(row: dict[str, Any], event_type: str) -> str:
    if event_type == "verification_recorded":
        return f"{_verification_status(row)}: {row.get('command', '')}"
    if event_type == "review_imported":
        return f"{row.get('verdict', '')}: {row.get('summary', '')}"
    if event_type == "failure_logged":
        return f"{row.get('severity', '')}: {row.get('message', '')}"
    if event_type == "finding_updated":
        return f"{row.get('previous_status', '')} -> {row.get('new_status', '')}"
    return row.get("reason", "") or row.get("status", "")


def _next_action(open_tasks: list[dict[str, Any]]) -> str:
    if not open_tasks:
        return "作業中のタスクはありません。"
    if len(open_tasks) == 1:
        return f"次は {open_tasks[0]['title']} の状態確認です。"
    return f"{len(open_tasks)} 件の未完了タスクがあります。"


def _safe_task_analytics(store: Store, task_id: str) -> dict[str, Any]:
    from .task_analytics import task_analytics

    return task_analytics(store, task_id)
