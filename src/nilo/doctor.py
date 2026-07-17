from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import mcp_server
from .agent_installation import inspect_agent_runtime_files
from .ai_context import (
    AI_CONTEXT_TEXT_MAX_CHARS,
    project_ai_context,
    render_ai_context_text,
)
from .cli import build_agent_instruction_block
from .completion_projection import project_completion_projections
from .display_labels import field_label
from .failure import summarize_failure_logs
from .snapshot import current_git_snapshot, evidence_status
from .state_audit import audit_project, audit_workflow, doctor_state
from .store import Store
from .task_logic import unresolved_review_findings


@dataclass(frozen=True)
class AgentRuntimeDiagnostics:
    checks: dict[str, bool]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class AiContextDiagnostics:
    instruction_chars: int
    default_tools: tuple[dict, ...]
    review_handoff_tools: tuple[dict, ...]
    long_descriptions: tuple[dict, ...]
    status_ai_chars: int
    status_ai_max_chars: int
    open_failure_count: int
    high_open_failure_count: int
    failure_summary_chars: int
    stale_evidence_count: int
    unresolved_review_count: int


def inspect_agent_runtime(cwd: Path | None = None) -> AgentRuntimeDiagnostics:
    result = inspect_agent_runtime_files(cwd)
    return AgentRuntimeDiagnostics(
        checks=dict(result["checks"]),
        warnings=tuple(result["warnings"]),
    )


def diagnose_ai_context(
    store: Store, project_id: str, cwd: Path | None = None
) -> AiContextDiagnostics:
    root = cwd or Path.cwd()
    project = store.get("projects", project_id)
    if not project:
        raise LookupError(f"project not found: {project_id}")
    runtime_body = build_agent_instruction_block(project, "all")
    status_body = render_ai_context_text(
        project_ai_context(store, project_id),
        max_chars=AI_CONTEXT_TEXT_MAX_CHARS,
    )
    failure_summary = summarize_failure_logs(store, project_id=project_id, limit=100000)
    summary_lines = [
        f"{field_label('failure_summary')}:",
        f"- {field_label('open_failures')}: {failure_summary['open_failure_count']}",
        f"- {field_label('high_open_failures')}: {failure_summary['high_open_failure_count']}",
    ]
    latest_failure = failure_summary["latest_open_failure"]
    if latest_failure:
        summary_lines.append(
            f"- {field_label('latest_open_failure')}: "
            f"{latest_failure['task_id']} {latest_failure['category']}"
        )
    summary_lines.append(
        f"詳細は `nilo failure list --project {project_id}` を確認してください。"
    )
    snapshot = current_git_snapshot(root)
    stale_count = 0
    unresolved_count = 0
    for task in store.list_where("tasks", "project_id=?", (project_id,)):
        verification_run = store.latest_for_task("verification_runs", task["id"])
        if evidence_status(verification_run, snapshot) == "stale":
            stale_count += 1
        unresolved_count += len(unresolved_review_findings(store, task["id"]))
    default_tools = tuple(mcp_server.default_tools())
    review_tools = tuple(mcp_server.review_handoff_tools())
    long_descriptions = tuple(
        {"name": tool["name"], "length": len(tool.get("description", ""))}
        for tool in mcp_server.TOOLS
        if len(tool.get("description", "")) > 160
    )
    return AiContextDiagnostics(
        instruction_chars=len(runtime_body),
        default_tools=default_tools,
        review_handoff_tools=review_tools,
        long_descriptions=long_descriptions,
        status_ai_chars=len(status_body),
        status_ai_max_chars=AI_CONTEXT_TEXT_MAX_CHARS,
        open_failure_count=failure_summary["open_failure_count"],
        high_open_failure_count=failure_summary["high_open_failure_count"],
        failure_summary_chars=len("\n".join(summary_lines)),
        stale_evidence_count=stale_count,
        unresolved_review_count=unresolved_count,
    )


def diagnose_completions(store: Store, project_id: str) -> dict:
    from .project_logic import (
        accepted_roadmap_commitments,
        fast_project_tasks_and_recorded_statuses,
        ordered_roadmap_commitments,
    )

    if not store.get("projects", project_id):
        raise LookupError(f"project not found: {project_id}")
    tasks, statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    commitments = ordered_roadmap_commitments(
        store,
        accepted_roadmap_commitments(store, project_id),
        tasks,
        statuses,
    )
    projections = project_completion_projections(
        store,
        project_id,
        tasks,
        current_commitment_ids={commitments[0]["id"]} if commitments else set(),
        statuses=statuses,
    )
    audit_findings = [
        item
        for item in audit_project(store, project_id)
        if item["code"].startswith("completion_")
    ]
    groups: dict[str, list[dict]] = {
        "current_acceptance_pending": [],
        "legacy_pending": [],
        "superseded": [],
        "superseded_candidate": [],
        "inconsistent": [],
        "insufficient_data": [],
    }
    for projection in projections.values():
        stage = projection.stage.value
        if stage == "needs_human_acceptance":
            groups["current_acceptance_pending"].append(projection.to_dict())
        elif stage in {"legacy_pending", "superseded", "inconsistent"}:
            groups[stage].append(projection.to_dict())
        elif projection.evidence_state.value == "unknown":
            groups["insufficient_data"].append(projection.to_dict())
    successor_parent_ids = {
        task.get("parent_task_id") for task in tasks if task.get("parent_task_id")
    }
    groups["superseded_candidate"] = [
        projection.to_dict()
        for task_id, projection in projections.items()
        if task_id in successor_parent_ids
        and projection.stage.value == "legacy_pending"
    ]
    return {
        "project_id": project_id,
        "counts": {key: len(value) for key, value in groups.items()},
        "audit_findings": audit_findings,
        **groups,
    }


def diagnose_state(store: Store, project_id: str, cwd: Path | None = None) -> dict:
    return doctor_state(store, project_id, cwd=cwd or Path.cwd())


def diagnose_performance(
    store: Store, project_id: str, cwd: Path | None = None
) -> dict:
    if not store.get("projects", project_id):
        raise LookupError(f"project not found: {project_id}")
    snapshot = current_git_snapshot(cwd or Path.cwd(), mode="full")
    timing = snapshot.get("snapshot_timing") or {}
    return {
        "project_id": project_id,
        "snapshot_mode": snapshot.get("snapshot_mode", "full"),
        "git_available": bool(snapshot.get("git_available", True)),
        "git_diff_hash_computed": bool(snapshot.get("git_diff_hash_computed", True)),
        "diff_hash_seconds": timing.get("diff_hash_seconds", 0.0),
        "observed_paths": len(snapshot.get("observed_paths") or []),
        "hashed_paths": len(snapshot.get("snapshot_hashed_paths") or []),
        "excluded_paths": len(snapshot.get("snapshot_excluded_paths") or []),
        "large_paths": len(snapshot.get("snapshot_large_paths") or []),
        "binary_paths": len(snapshot.get("snapshot_binary_paths") or []),
        "warnings": snapshot.get("snapshot_warnings") or [],
    }


def diagnose_workflow(store: Store, project_id: str, cwd: Path | None = None) -> dict:
    if not store.get("projects", project_id):
        raise LookupError(f"project not found: {project_id}")
    findings = audit_workflow(store, project_id, cwd=cwd or Path.cwd())
    return {"project_id": project_id, "count": len(findings), "findings": findings}


def diagnose_transitions(store: Store, project_id: str, limit: int) -> dict:
    task_ids = {
        task["id"] for task in store.list_where("tasks", "project_id=?", (project_id,))
    }
    events = [
        event
        for event in store.list_where("transition_events", "1=1")
        if event["entity_id"] in task_ids
        or (
            isinstance(event.get("related_ids"), dict)
            and any(value in task_ids for value in event["related_ids"].values())
        )
    ]
    return {
        "project_id": project_id,
        "count": len(events),
        "transition_events": events[:limit],
    }
