from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot import current_git_snapshot, evidence_status
from .store import Store
from .task_logic import is_task_completed_status, projected_task_status, unresolved_review_findings


def active_tasks(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    from . import project_logic as p

    tasks, statuses = p.project_tasks_and_statuses(store, project_id)
    return [task for task in tasks if not is_task_completed_status(statuses[task["id"]])], statuses


def task_ai_context(store: Store, task_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    from . import project_logic as p

    cwd = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    snapshot = current_git_snapshot(cwd)
    verification_run = store.latest_for_task("verification_runs", task_id)
    latest_report = store.latest_for_task("agent_reports", task_id)
    evidence = evidence_status(verification_run, snapshot)
    if evidence == "missing" and latest_report:
        evidence = "present"
    unresolved = unresolved_review_findings(store, task_id)
    status = projected_task_status(store, task)
    latest_event = store.latest_task_status_event(task_id)
    latest_event_id = latest_event["event_id"] if latest_event else ""
    unexecuted = p.unexecuted_verifications_for_task(status, verification_run)
    next_actions = p.next_actions_for_task(status, verification_run, unexecuted, task["id"], task["task_type"])
    blocking_reasons: list[str] = []
    if evidence != "current":
        blocking_reasons.append(f"evidence_{evidence}")
    if unresolved:
        blocking_reasons.append(f"unresolved_review_findings:{len(unresolved)}")
    completion_allowed = not blocking_reasons
    return {
        "task": {
            "id": task["id"],
            "title": task["title"],
            "state": status,
            "task_type": task["task_type"],
            "risk_level": task["risk_level"],
        },
        "git": {
            "git_head": snapshot.get("git_head"),
            "git_diff_hash": snapshot.get("git_diff_hash") or "",
            "dirty": bool(snapshot.get("working_tree_dirty")),
        },
        "evidence": {
            "status": evidence,
            "verification_run_id": verification_run["id"] if verification_run else "",
            "report_id": latest_report["id"] if latest_report else "",
            "verification_exit_code": verification_run["exit_code"] if verification_run else None,
            "verification_timed_out": bool(verification_run["timed_out"]) if verification_run else False,
        },
        "review": {
            "unresolved_count": len(unresolved),
            "unresolved_blocking_count": len([item for item in unresolved if item["blocking"]]),
        },
        "completion": {
            "allowed": completion_allowed,
            "blocked": not completion_allowed,
            "blocking_reasons": blocking_reasons,
        },
        "write_context_token": f"task:{task_id}:{latest_event_id}" if latest_event_id else "",
        "latest_task_status_event_id": latest_event_id,
        "next_required_actions": next_actions[:3],
    }


def project_ai_context(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    from . import project_logic as p

    cwd = cwd or Path.cwd()
    project = store.get("projects", project_id)
    if not project:
        raise ValueError(f"project not found: {project_id}")
    tasks, statuses = p.project_tasks_and_statuses(store, project_id)
    active = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
    current = task_ai_context(store, active[0]["id"], cwd=cwd) if active else None
    design_residue = p.project_design_residue()
    commitments = p.accepted_roadmap_commitments(store, project_id)
    pending_revisions = p.pending_roadmap_revisions(store, project_id)
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "current_task": current,
        "next_required_actions": (
            current["next_required_actions"]
            if current
            else p.project_level_next_actions(store, tasks, statuses, design_residue, commitments, pending_revisions, project_id)[:3]
        ),
    }


def render_ai_context_text(data: dict[str, Any]) -> str:
    lines = [f"project: {data['project_id']} ({data['project_name']})"]
    current = data.get("current_task")
    if not current:
        lines.append("current_task: none")
    else:
        task = current["task"]
        git = current["git"]
        evidence = current["evidence"]
        review = current["review"]
        completion = current["completion"]
        lines.extend(
            [
                f"current_task: {task['id']} [{task['state']}] {task['title']}",
                f"git: head={git['git_head'] or 'none'} diff_hash={git['git_diff_hash'] or 'none'} dirty={git['dirty']}",
                f"evidence: {evidence['status']}",
                f"unresolved_review_count: {review['unresolved_count']}",
                f"completion: {'allowed' if completion['allowed'] else 'blocked'}",
            ]
        )
        if completion["blocking_reasons"]:
            lines.append("blocking_reasons:")
            lines.extend(f"- {reason}" for reason in completion["blocking_reasons"])
    lines.append("next_required_actions:")
    actions = data.get("next_required_actions") or []
    lines.extend(f"- {action}" for action in actions) if actions else lines.append("- none")
    return "\n".join(lines)


def evidence_ai_context(store: Store, task_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    return task_ai_context(store, task_id, cwd=cwd)["evidence"]


def review_ai_context(store: Store, task_id: str) -> dict[str, Any]:
    findings = unresolved_review_findings(store, task_id)
    return {
        "task_id": task_id,
        "unresolved_count": len(findings),
        "unresolved_blocking_count": len([item for item in findings if item["blocking"]]),
        "unresolved_findings": [
            {
                "id": item["id"],
                "severity": item["severity"],
                "blocking": bool(item["blocking"]),
                "title": item["title"],
                "file_path": item["file_path"],
                "line": item["line"],
            }
            for item in findings[:10]
        ],
    }
