from __future__ import annotations

from .cli_support import make_id
from .store import Store
from .task_logic import is_task_completed_status
from .timeutil import now_iso


MODES = ("normal", "overdrive")
OVERDRIVE_SCOPES = ("task", "commitment", "project", "queue")
APPROVAL_GATES = (
    "instruction_generation",
    "task_progression",
    "evidence_acceptance",
    "roadmap_assessment",
)
SAFETY_GATES = (
    "destructive_operation",
    "secret_or_credential_access",
    "billing_or_external_publication",
    "delete_operation",
    "max_failure_exceeded",
    "out_of_scope_design_change",
    "ambiguous_specification",
    "unexpected_dirty_working_tree",
)


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    return mode


def validate_scope(scope: str) -> str:
    if scope not in OVERDRIVE_SCOPES:
        raise ValueError(f"unknown overdrive scope: {scope}")
    return scope


def overdrive_gate_decision(mode: str, gate: str) -> dict:
    validate_mode(mode)
    if gate in SAFETY_GATES:
        return {"gate": gate, "gate_type": "safety", "decision": "stop", "bypassed": False}
    if gate in APPROVAL_GATES and mode == "overdrive":
        return {"gate": gate, "gate_type": "approval", "decision": "bypass", "bypassed": True}
    if gate in APPROVAL_GATES:
        return {"gate": gate, "gate_type": "approval", "decision": "require_approval", "bypassed": False}
    return {"gate": gate, "gate_type": "unknown", "decision": "stop", "bypassed": False}


def task_cursor_for_commitment(store: Store, project_id: str, commitment_id: str) -> dict | None:
    from . import cli as c

    tasks, statuses = c.project_tasks_and_statuses(store, project_id)
    candidates = [
        task
        for task in tasks
        if task.get("roadmap_commitment_id") == commitment_id and not is_task_completed_status(statuses[task["id"]])
    ]
    if not candidates:
        return None
    priority = {"planned": 0, "instruction_generated": 1, "agent_reported": 2, "verification_failed": 3}
    return sorted(candidates, key=lambda task: (priority.get(statuses[task["id"]], 50), task["created_at"], task["id"]))[0]


def task_cursor_for_project(store: Store, project_id: str) -> dict | None:
    active = prioritized_active_tasks_for_project(store, project_id)
    return active[0] if active else None


def prioritized_active_tasks_for_project(store: Store, project_id: str) -> list[dict]:
    from . import project_logic as p

    tasks, statuses = p.fast_project_tasks_and_recorded_statuses(store, project_id)
    active, _ = p.roadmap_prioritized_project_active_tasks(store, project_id, tasks, statuses)
    return active


def active_tasks_after_scope(active_tasks: list[dict], cursor: dict | None, scope: str) -> list[dict]:
    if not cursor or scope in {"project", "queue"}:
        return []
    try:
        cursor_index = next(index for index, task in enumerate(active_tasks) if task["id"] == cursor["id"])
    except StopIteration:
        return []
    active = active_tasks[cursor_index + 1 :]
    if scope == "commitment":
        commitment_id = cursor.get("roadmap_commitment_id", "")
        active = [task for task in active if task.get("roadmap_commitment_id") != commitment_id]
    return active


def accepted_commitment_for_project(store: Store, project_id: str) -> dict:
    commitments = store.list_where("roadmap_commitments", "project_id=? AND status='accepted'", (project_id,))
    if not commitments:
        raise SystemExit(f"accepted roadmap commitment not found for project: {project_id}")
    if len(commitments) > 1:
        ids = ", ".join(commitment["id"] for commitment in commitments[:5])
        raise SystemExit(f"multiple accepted commitments; pass --commitment explicitly: {ids}")
    return commitments[0]


def append_overdrive_event(
    store: Store,
    run: dict,
    event_type: str,
    message: str,
    metadata: dict | None = None,
    task_id: str = "",
) -> dict:
    event = {
        "id": make_id("overdrive_event"),
        "run_id": run["id"],
        "project_id": run["project_id"],
        "task_id": task_id,
        "event_type": event_type,
        "message": message,
        "metadata": metadata or {},
        "created_at": now_iso(),
    }
    store.insert("overdrive_events", event)
    return event


def start_overdrive_run(
    store: Store,
    project_id: str,
    commitment_id: str = "",
    max_failures: int = 3,
    scope: str = "task",
) -> dict:
    validate_scope(scope)
    if max_failures < 1:
        raise SystemExit("max failures must be at least 1")
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    commitment = None
    if scope == "commitment" or commitment_id:
        commitment = store.get("roadmap_commitments", commitment_id) if commitment_id else accepted_commitment_for_project(store, project_id)
        if not commitment or commitment["project_id"] != project_id or commitment["status"] != "accepted":
            raise SystemExit(f"accepted roadmap commitment not found: {commitment_id}")
        cursor = task_cursor_for_commitment(store, project_id, commitment["id"])
        active_tasks = prioritized_active_tasks_for_project(store, project_id)
    else:
        active_tasks = prioritized_active_tasks_for_project(store, project_id)
        cursor = active_tasks[0] if active_tasks else None
        if cursor and cursor.get("roadmap_commitment_id"):
            commitment = store.get("roadmap_commitments", cursor["roadmap_commitment_id"])
    next_after_scope = active_tasks_after_scope(active_tasks, cursor, scope)
    created_at = now_iso()
    run = {
        "id": make_id("overdrive"),
        "project_id": project_id,
        "roadmap_commitment_id": commitment["id"] if commitment else "",
        "mode": "overdrive",
        "scope": scope,
        "status": "ready" if cursor else "awaiting_human_review",
        "cursor_task_id": cursor["id"] if cursor else "",
        "max_failures": max_failures,
        "failure_count": 0,
        "summary": "ready" if cursor else "awaiting final human review",
        "summary_json": {
            "executed_tasks": [],
            "changed_files": [],
            "commands": [],
            "test_results": [],
            "failures": [],
            "unresolved_concerns": [],
            "scope": scope,
            "next_after_scope_task_id": next_after_scope[0]["id"] if next_after_scope else "",
            "human_review_points": [
                "Review the final Overdrive report before continuing outside the selected overdrive scope.",
            ],
        },
        "created_at": created_at,
        "updated_at": created_at,
    }
    store.insert("overdrive_runs", run)
    append_overdrive_event(
        store,
        run,
        "mode_selected",
        "Overdrive Mode selected; approval gates are bypassable within scope but safety gates remain enforced.",
        {"mode": "overdrive", "scope": scope, "max_failures": max_failures},
    )
    for gate in APPROVAL_GATES:
        append_overdrive_event(store, run, "approval_gate_bypassed", f"approval gate bypassed: {gate}", overdrive_gate_decision("overdrive", gate))
    for gate in SAFETY_GATES:
        append_overdrive_event(store, run, "safety_gate_retained", f"safety gate retained: {gate}", overdrive_gate_decision("overdrive", gate))
    if cursor:
        append_overdrive_event(store, run, "cursor_selected", f"overdrive cursor selected task: {cursor['id']}", {"title": cursor["title"], "scope": scope}, cursor["id"])
        if next_after_scope:
            append_overdrive_event(
                store,
                run,
                "scope_boundary_required",
                f"stop before unrelated next task: {next_after_scope[0]['id']}",
                {"scope": scope, "next_task_id": next_after_scope[0]["id"]},
                cursor["id"],
            )
    else:
        append_overdrive_event(
            store,
            run,
            "human_review_checkpoint_required",
            "no incomplete task remains in the selected overdrive scope; final human review checkpoint is required",
            {"commitment_id": commitment["id"] if commitment else "", "scope": scope},
        )
    return run
