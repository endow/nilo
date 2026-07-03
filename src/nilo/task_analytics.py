from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from .store import Store
from .task_logic import is_task_completed_status
from .timeutil import now_iso


OPEN_FINDING_STATUSES = {"unresolved"}
RESOLVED_FINDING_STATUSES = {"addressed", "accepted-risk"}


def project_task_analytics(store: Store, project_id: str, *, since: str = "") -> dict[str, Any]:
    if not store.get("projects", project_id):
        raise SystemExit(f"project not found: {project_id}")
    since_at = parse_since(since)
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    tasks = [task for task in tasks if in_window(task.get("created_at", ""), since_at)]
    task_ids = {task["id"] for task in tasks}
    completions = active_completions_for_tasks(store, task_ids, since_at)
    completion_by_task = {completion["task_id"]: completion for completion in completions}
    statuses = {
        task["id"]: "completed_by_user"
        if task["id"] in completion_by_task
        else task.get("status", "")
        for task in tasks
    }
    completed_task_ids = {task_id for task_id, status in statuses.items() if is_task_completed_status(status)}

    verification_runs = rows_for_tasks(store, "verification_runs", task_ids, since_at)
    review_requests = rows_for_tasks(store, "review_requests", task_ids, since_at)
    review_results = rows_for_tasks(store, "review_results", task_ids, since_at)
    review_findings = rows_for_tasks(store, "review_findings", task_ids, since_at)
    review_updates = rows_for_tasks(store, "review_finding_updates", task_ids, since_at)
    failure_logs = rows_for_tasks(store, "failure_logs", task_ids, since_at)

    tasks_with_verification = {run["task_id"] for run in verification_runs}
    tasks_with_review = {result["task_id"] for result in review_results}
    open_failures = [failure for failure in failure_logs if failure.get("status", "open") == "open"]
    open_blocking_findings = [
        finding for finding in review_findings if is_open_finding(finding) and bool(finding.get("blocking"))
    ]

    summary = {
        "task_count": len(tasks),
        "completed_count": len(completed_task_ids),
        "open_count": len(tasks) - len(completed_task_ids),
        "completed_with_reservations_count": len(
            {completion["task_id"] for completion in completions if bool(completion.get("completed_with_reservations"))}
        ),
        "human_confirmed_completion_count": len(
            {completion["task_id"] for completion in completions if bool(completion.get("human_confirmed"))}
        ),
        "completed_with_verification_count": len(completed_task_ids & tasks_with_verification),
        "completed_with_review_count": len(completed_task_ids & tasks_with_review),
        "open_failure_task_count": len({failure["task_id"] for failure in open_failures}),
        "open_blocking_review_finding_task_count": len({finding["task_id"] for finding in open_blocking_findings}),
        "overdrive_task_count": sum(1 for task in tasks if task.get("mode") == "overdrive"),
    }

    return {
        "project_id": project_id,
        "scope": {"since": since or "", "since_at": since_at.isoformat() if since_at else ""},
        "summary": summary,
        "verification": verification_summary(verification_runs, completed_task_ids),
        "review": review_summary(review_requests, review_results, review_findings, review_updates),
        "failure": failure_summary(failure_logs),
        "task_design": task_design_summary(tasks, failure_logs, review_findings),
    }


def task_analytics(store: Store, task_id: str) -> dict[str, Any]:
    task = store.get("tasks", task_id)
    if not task:
        raise SystemExit(f"task not found: {task_id}")
    verification_runs = store.list_where("verification_runs", "task_id=?", (task_id,))
    review_requests = store.list_where("review_requests", "task_id=?", (task_id,))
    review_results = store.list_where("review_results", "task_id=?", (task_id,))
    review_findings = store.list_where("review_findings", "task_id=?", (task_id,))
    review_updates = store.list_where("review_finding_updates", "task_id=?", (task_id,))
    failure_logs = store.list_where("failure_logs", "task_id=?", (task_id,))
    completions = store.list_where("task_completions", "task_id=? AND COALESCE(invalidated_at, '')=''", (task_id,))
    transitions = store.list_where("transition_events", "entity_type='task' AND entity_id=?", (task_id,))
    return {
        "task": {
            "id": task["id"],
            "project_id": task["project_id"],
            "title": task["title"],
            "status": projected_status_without_snapshot(store, task),
            "task_type": task.get("task_type", ""),
            "risk_level": task.get("risk_level", ""),
            "mode": task.get("mode", "normal"),
            "roadmap_commitment_id": task.get("roadmap_commitment_id", ""),
            "requires_understanding_check": bool(task.get("requires_understanding_check")),
        },
        "summary": {
            "verification_run_count": len(verification_runs),
            "review_request_count": len(review_requests),
            "review_result_count": len(review_results),
            "review_finding_count": len(review_findings),
            "open_review_finding_count": sum(1 for finding in review_findings if is_open_finding(finding)),
            "failure_log_count": len(failure_logs),
            "open_failure_log_count": sum(1 for failure in failure_logs if failure.get("status", "open") == "open"),
            "completion_count": len(completions),
            "transition_count": len(transitions),
        },
        "verification": verification_summary(verification_runs, {task_id} if completions else set()),
        "review": review_summary(review_requests, review_results, review_findings, review_updates),
        "failure": failure_summary(failure_logs),
        "completion": {
            "completions": [
                {
                    "id": completion["id"],
                    "actor": completion.get("actor", completion.get("completed_by", "")),
                    "human_confirmed": bool(completion.get("human_confirmed")),
                    "completed_with_reservations": bool(completion.get("completed_with_reservations")),
                    "accepted_verification_run_ids": completion.get("accepted_verification_run_ids") or [],
                    "accepted_review_result_ids": completion.get("accepted_review_result_ids") or [],
                    "created_at": completion.get("created_at", ""),
                }
                for completion in completions
            ],
        },
        "transitions": [
            {
                "id": transition["id"],
                "transition": transition.get("transition", ""),
                "previous_state": transition.get("previous_state", ""),
                "new_state": transition.get("new_state", ""),
                "actor": transition.get("actor", ""),
                "created_at": transition.get("created_at", ""),
            }
            for transition in transitions
        ],
    }


def verification_summary(runs: list[dict[str, Any]], completed_task_ids: set[str]) -> dict[str, Any]:
    command_stats: dict[str, Counter] = defaultdict(Counter)
    command_durations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    command_timeouts: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        command = run.get("command", "")
        command_stats[command]["run_count"] += 1
        duration = verification_duration_seconds(run)
        if duration is not None:
            command_durations[command].append(
                {
                    "seconds": duration,
                    "created_at": run.get("created_at", ""),
                    "started_at": run.get("started_at", ""),
                }
            )
        timeout_seconds = run.get("timeout_seconds")
        if isinstance(timeout_seconds, (int, float)):
            command_timeouts[command].append(float(timeout_seconds))
        if bool(run.get("timed_out")):
            command_stats[command]["timed_out_count"] += 1
        if run.get("exit_code") not in (0, None):
            command_stats[command]["failed_count"] += 1
    completed_without_verification = sorted(completed_task_ids - {run["task_id"] for run in runs})
    dirty_task_ids = sorted({run["task_id"] for run in runs if bool(run.get("working_tree_dirty"))})
    return {
        "run_count": len(runs),
        "passed_count": sum(1 for run in runs if not bool(run.get("timed_out")) and run.get("exit_code") == 0),
        "failed_count": sum(1 for run in runs if not bool(run.get("timed_out")) and run.get("exit_code") not in (0, None)),
        "timed_out_count": sum(1 for run in runs if bool(run.get("timed_out"))),
        "commands": [
            {
                "command": command,
                "run_count": counts["run_count"],
                "failed_count": counts["failed_count"],
                "timed_out_count": counts["timed_out_count"],
            }
            for command, counts in sorted(command_stats.items(), key=lambda item: (-item[1]["run_count"], item[0]))
        ],
        "timeout_commands": top_commands(command_stats, "timed_out_count"),
        "failed_commands": top_commands(command_stats, "failed_count"),
        "duration_commands": command_duration_summaries(command_durations, command_timeouts),
        "completed_without_verification_task_ids": completed_without_verification,
        "dirty_snapshot_task_ids": dirty_task_ids,
    }


def review_summary(
    requests: list[dict[str, Any]],
    results: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    open_findings = [finding for finding in findings if is_open_finding(finding)]
    resolved_findings = [finding for finding in findings if finding.get("status") in RESOLVED_FINDING_STATUSES]
    open_blocking = [finding for finding in open_findings if bool(finding.get("blocking"))]
    return {
        "request_count": len(requests),
        "result_count": len(results),
        "verdict_counts": dict(Counter(result.get("verdict", "") for result in results)),
        "finding_severity_counts": dict(Counter(finding.get("severity", "") for finding in findings)),
        "blocking_finding_count": sum(1 for finding in findings if bool(finding.get("blocking"))),
        "open_finding_count": len(open_findings),
        "resolved_finding_count": len(resolved_findings),
        "open_blocking_findings": [
            {
                "id": finding["id"],
                "task_id": finding["task_id"],
                "severity": finding.get("severity", ""),
                "title": finding.get("title", ""),
                "file_path": finding.get("file_path", ""),
                "line": finding.get("line", ""),
            }
            for finding in open_blocking
        ],
        "tasks_with_post_review_updates": sorted({update["task_id"] for update in updates}),
    }


def failure_summary(failures: list[dict[str, Any]]) -> dict[str, Any]:
    category_by_task: dict[str, Counter] = defaultdict(Counter)
    for failure in failures:
        category_by_task[failure["task_id"]][failure.get("category", "")] += 1
    return {
        "log_count": len(failures),
        "open_count": sum(1 for failure in failures if failure.get("status", "open") == "open"),
        "resolved_count": sum(1 for failure in failures if failure.get("status") == "resolved"),
        "category_counts": dict(Counter(failure.get("category", "") for failure in failures)),
        "severity_counts": dict(Counter(failure.get("severity", "") for failure in failures)),
        "tasks_with_repeated_categories": [
            {"task_id": task_id, "category": category, "count": count}
            for task_id, counts in sorted(category_by_task.items())
            for category, count in counts.items()
            if count > 1
        ],
        "recent_category_counts": dict(Counter(failure.get("category", "") for failure in most_recent(failures, 10))),
    }


def task_design_summary(tasks: list[dict[str, Any]], failures: list[dict[str, Any]], findings: list[dict[str, Any]]) -> dict[str, Any]:
    task_by_id = {task["id"]: task for task in tasks}
    risk_failure_counts: dict[str, int] = defaultdict(int)
    risk_review_finding_counts: dict[str, int] = defaultdict(int)
    for failure in failures:
        task = task_by_id.get(failure["task_id"])
        if task:
            risk_failure_counts[task.get("risk_level", "")] += 1
    for finding in findings:
        task = task_by_id.get(finding["task_id"])
        if task:
            risk_review_finding_counts[task.get("risk_level", "")] += 1
    return {
        "task_type_counts": dict(Counter(task.get("task_type", "") for task in tasks)),
        "risk_level_counts": dict(Counter(task.get("risk_level", "") for task in tasks)),
        "roadmap_task_count": sum(1 for task in tasks if task.get("roadmap_commitment_id")),
        "standalone_task_count": sum(1 for task in tasks if not task.get("roadmap_commitment_id")),
        "overdrive_task_count": sum(1 for task in tasks if task.get("mode") == "overdrive"),
        "requires_understanding_check_task_count": sum(1 for task in tasks if bool(task.get("requires_understanding_check"))),
        "risk_failure_counts": dict(risk_failure_counts),
        "risk_review_finding_counts": dict(risk_review_finding_counts),
    }


def projected_status_without_snapshot(store: Store, task: dict[str, Any]) -> str:
    latest = store.latest_task_status_event(task["id"])
    return latest["status"] if latest else task.get("status", "")


def active_completions_for_tasks(store: Store, task_ids: set[str], since_at: datetime | None) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    completions = store.list_where("task_completions", "COALESCE(invalidated_at, '')=''", ())
    return [
        completion
        for completion in completions
        if completion["task_id"] in task_ids and in_window(completion.get("created_at", ""), since_at)
    ]


def rows_for_tasks(store: Store, table: str, task_ids: set[str], since_at: datetime | None) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    rows = store.list_where(table)
    return [row for row in rows if row.get("task_id") in task_ids and in_window(row.get("created_at", ""), since_at)]


def parse_since(value: str) -> datetime | None:
    if not value:
        return None
    now = datetime.fromisoformat(now_iso())
    if value.endswith("d") and value[:-1].isdigit():
        return now - timedelta(days=int(value[:-1]))
    if value.endswith("h") and value[:-1].isdigit():
        return now - timedelta(hours=int(value[:-1]))
    return normalize_datetime(datetime.fromisoformat(value))


def in_window(created_at: str, since_at: datetime | None) -> bool:
    if since_at is None or not created_at:
        return True
    try:
        created = normalize_datetime(datetime.fromisoformat(created_at))
    except ValueError:
        return False
    return created >= since_at


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=datetime.fromisoformat(now_iso()).tzinfo)


def is_open_finding(finding: dict[str, Any]) -> bool:
    status = finding.get("status", "")
    return status in OPEN_FINDING_STATUSES


def top_commands(command_stats: dict[str, Counter], key: str) -> list[dict[str, Any]]:
    return [
        {"command": command, key: counts[key]}
        for command, counts in sorted(command_stats.items(), key=lambda item: (-item[1][key], item[0]))
        if counts[key] > 0
    ]


def verification_duration_seconds(run: dict[str, Any]) -> float | None:
    try:
        started = normalize_datetime(datetime.fromisoformat(run.get("started_at", "")))
        finished = normalize_datetime(datetime.fromisoformat(run.get("finished_at", "")))
    except ValueError:
        return None
    duration = (finished - started).total_seconds()
    if duration < 0:
        return None
    return duration


def command_duration_summaries(command_durations: dict[str, list[dict[str, Any]]], command_timeouts: dict[str, list[float]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for command, duration_rows in command_durations.items():
        if not duration_rows:
            continue
        durations = [row["seconds"] for row in duration_rows]
        sorted_durations = sorted(durations)
        max_duration = sorted_durations[-1]
        latest = max(duration_rows, key=duration_sort_key)
        timeout_seconds = min(command_timeouts.get(command, []), default=None)
        timeout_may_be_short = timeout_seconds is not None and max_duration >= timeout_seconds * 0.9
        summaries.append(
            {
                "command": command,
                "run_count": len(durations),
                "min_seconds": round(sorted_durations[0], 3),
                "max_seconds": round(max_duration, 3),
                "latest_seconds": round(latest["seconds"], 3),
                "timeout_seconds": timeout_seconds,
                "timeout_may_be_short": timeout_may_be_short,
            }
        )
    return sorted(summaries, key=lambda item: (-item["max_seconds"], item["command"]))


def duration_sort_key(row: dict[str, Any]) -> tuple[datetime, datetime]:
    return (parse_optional_datetime(row.get("created_at", "")), parse_optional_datetime(row.get("started_at", "")))


def parse_optional_datetime(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=datetime.fromisoformat(now_iso()).tzinfo)
    try:
        return normalize_datetime(datetime.fromisoformat(value))
    except ValueError:
        return datetime.min.replace(tzinfo=datetime.fromisoformat(now_iso()).tzinfo)


def most_recent(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)[:limit]
