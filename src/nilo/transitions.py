from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from .agent_report_import import import_agent_report as import_agent_report_body
from .cli_support import make_id
from .review import VALID_FINDING_STATUSES, parse_review_result
from .secret import mask_secrets
from .snapshot import compact_snapshot, current_git_snapshot, evidence_status, snapshots_match
from .store import Store
from .task_logic import completion_status, projected_task_status, unresolved_review_findings
from .timeutil import now_iso


@dataclass
class TransitionResult:
    transition: str
    actor: str
    allowed: bool
    created_ids: dict[str, str] = field(default_factory=dict)
    updated_ids: dict[str, str] = field(default_factory=dict)
    previous_status: str | None = None
    new_status: str | None = None
    audit_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TransitionError(Exception):
    code: str
    message: str
    remediation: str

    def __init__(self, code: str, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation


def atomic_transition(func):
    @wraps(func)
    def wrapper(store: Store, *args, **kwargs):
        with store.transaction():
            return func(store, *args, **kwargs)

    return wrapper


HUMAN_DECISION_SOURCES = {"human_interactive", "human_explicit"}
TRUSTED_VERIFICATION_SOURCES = {"nilo_executed"}
AI_ALLOWED_TODO_STATUSES = {"triaged", "ready", "ad_hoc_approved", "requires_roadmap", "blocked"}
CLOSED_TODO_STATUSES = {"rejected", "deferred", "superseded", "converted_to_task"}
HIGH_RISK_COMPLETION_TERMS = (
    "db schema",
    "database schema",
    "schema migration",
    "状態遷移",
    "state transition",
    "破壊的",
    "destructive",
    "本番公開",
    "production publish",
)


def requires_human_completion(store: Store, task: dict) -> bool:
    if task.get("risk_level") == "high":
        return True
    if store.list_where("recipe_runs", "task_id=? AND recipe_name='release'", (task["id"],)):
        return True
    text = f"{task.get('title', '')} {task.get('description', '')}".lower()
    return any(term in text for term in HIGH_RISK_COMPLETION_TERMS)


def _require_actor(actor: str) -> None:
    if not actor:
        raise TransitionError("actor_required", "transition requires an explicit actor", "pass --actor or --by explicitly")


def _require_human_decision(actor: str, human_confirm: bool, decision_note: str, decision_source: str) -> None:
    _require_actor(actor)
    if actor != "human":
        raise TransitionError("human_only", "this transition records a human decision and cannot be performed by AI")
    if not human_confirm:
        raise TransitionError("human_confirm_required", "human decision requires human_confirm=True", "pass --human-confirm")
    if not decision_note.strip():
        raise TransitionError("decision_note_required", "human decision requires a decision note", "pass --decision-note or --reason")
    if decision_source not in HUMAN_DECISION_SOURCES:
        raise TransitionError(
            "decision_source_required",
            "human decision requires decision_source=human_interactive or human_explicit",
            "use CLI --human-confirm with an explicit note",
        )


def _require_expected(value: str, expected: str | None, code: str, message: str) -> None:
    if expected is not None and value != expected:
        raise TransitionError(code, message)


def _require_task_event(store: Store, task_id: str, expected_event_id: str | None) -> None:
    if expected_event_id is None:
        return
    latest = store.latest_task_status_event(task_id)
    current_event_id = latest["event_id"] if latest else ""
    if expected_event_id != current_event_id:
        raise TransitionError("stale_task_context", f"stale task state: expected_event_id={expected_event_id}, current_event_id={current_event_id}")


def _event(
    store: Store,
    transition: str,
    entity_type: str,
    entity_id: str,
    *,
    actor: str,
    decision_source: str = "",
    human_confirmed: bool = False,
    reason: str = "",
    previous_state: str = "",
    new_state: str = "",
    related_ids: dict[str, str] | list[str] | None = None,
    snapshot: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> str:
    event_id = make_id("transition")
    store.insert(
        "transition_events",
        {
            "id": event_id,
            "transition": transition,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "decision_source": decision_source,
            "human_confirmed": human_confirmed,
            "reason": reason,
            "previous_state": previous_state,
            "new_state": new_state,
            "related_ids": related_ids or [],
            "snapshot": snapshot or {},
            "warnings": warnings or [],
            "created_at": now_iso(),
        },
    )
    return event_id


def _result(
    transition: str,
    actor: str,
    *,
    created_ids: dict[str, str] | None = None,
    updated_ids: dict[str, str] | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    audit_notes: list[str] | None = None,
    warnings: list[str] | None = None,
) -> TransitionResult:
    return TransitionResult(
        transition=transition,
        actor=actor,
        allowed=True,
        created_ids=created_ids or {},
        updated_ids=updated_ids or {},
        previous_status=previous_status,
        new_status=new_status,
        audit_notes=audit_notes or [],
        warnings=warnings or [],
    )


def _latest_verified_completion_evidence(store: Store, task_id: str, cwd: Path) -> tuple[dict, dict[str, Any]]:
    verification = store.latest_for_task("verification_runs", task_id)
    snapshot = current_git_snapshot(cwd)
    if not verification:
        raise TransitionError("verification_missing", "AI completion requires a current verification run", "run `nilo check` first")
    status = evidence_status(verification, snapshot)
    if status not in {"current", "recorded"}:
        from .workflow_context import release_commit_aware_evidence_status, release_commit_verified_verification

        status = release_commit_aware_evidence_status(store, task_id, verification, snapshot)
        verification = release_commit_verified_verification(store, task_id, verification, snapshot) or verification
    if status not in {"current", "recorded"}:
        raise TransitionError("verification_not_current", f"AI completion requires current evidence, got {status}")
    if verification.get("timed_out"):
        raise TransitionError("verification_timed_out", "AI completion cannot use a timed out verification run")
    if verification.get("exit_code") != 0:
        raise TransitionError("verification_failed", "AI completion requires exit_code=0 verification")
    source = verification.get("source", "nilo_executed")
    metadata = verification.get("metadata") or {}
    if source not in TRUSTED_VERIFICATION_SOURCES and not metadata.get("trusted_runner"):
        raise TransitionError(
            "untrusted_verification_source",
            f"AI completion cannot rely on {source} verification without trusted runner metadata",
        )
    return verification, snapshot


def _require_no_open_high_failure(store: Store, task: dict) -> None:
    high = store.list_where(
        "failure_logs",
        "task_id=? AND status='open' AND severity='high'",
        (task["id"],),
    )
    if high:
        ids = ", ".join(item["id"] for item in high[:5])
        raise TransitionError("open_high_failure", f"completion blocked by open high failure(s): {ids}")


def _require_completion_evidence(store: Store, task: dict, verification: dict | None, snapshot: dict[str, Any]) -> None:
    if task.get("task_type") != "implementation":
        return
    if not verification:
        raise TransitionError("verification_missing", "completion requires a verification run for implementation tasks")
    status = evidence_status(verification, snapshot)
    if status not in {"current", "recorded"}:
        from .workflow_context import release_commit_aware_evidence_status

        status = release_commit_aware_evidence_status(store, task["id"], verification, snapshot)
    if status not in {"current", "recorded"}:
        raise TransitionError("verification_not_current", f"completion requires current verification evidence, got {status}")
    if verification.get("timed_out"):
        raise TransitionError("verification_timed_out", "completion cannot use a timed out verification run")
    if verification.get("exit_code") != 0:
        raise TransitionError("verification_failed", "completion requires exit_code=0 verification")


def complete_task(
    store: Store,
    task_id: str,
    *,
    actor: str,
    reason: str,
    human_confirm: bool = False,
    decision_source: str = "",
    decision_note: str = "",
    cwd: Path | None = None,
    completed_with_reservations: bool = False,
    expected_task_event_id: str | None = None,
) -> TransitionResult:
    _require_actor(actor)
    cwd = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        raise TransitionError("task_not_found", f"task not found: {task_id}")
    _require_task_event(store, task_id, expected_task_event_id)
    previous_status = projected_task_status(store, task)
    latest_review = store.latest_for_task("review_results", task_id)
    _require_no_open_high_failure(store, task)
    commit_transition: dict[str, Any] = {}
    if actor == "human":
        _require_human_decision(actor, human_confirm, decision_note, decision_source)
        latest_verification = store.latest_for_task("verification_runs", task_id)
        snapshot = current_git_snapshot(cwd)
        if unresolved_review_findings(store, task_id):
            raise TransitionError("unresolved_review_findings", "completion blocked by unresolved review findings")
        _require_completion_evidence(store, task, latest_verification, snapshot)
        from .workflow_context import release_commit_transition_metadata, release_commit_verified_verification

        if evidence_status(latest_verification, snapshot) not in {"current", "recorded"}:
            release_verified = release_commit_verified_verification(store, task_id, latest_verification, snapshot)
            if release_verified:
                latest_verification = release_verified
                commit_transition = release_commit_transition_metadata(store, task_id)
    elif actor == "ai":
        if human_confirm or decision_source in HUMAN_DECISION_SOURCES:
            raise TransitionError("ai_human_decision_forbidden", "AI cannot create a human decision")
        if requires_human_completion(store, task):
            raise TransitionError("human_completion_required", "high-risk task completion requires an explicit human decision")
        unresolved = unresolved_review_findings(store, task_id)
        if unresolved:
            ids = ", ".join(item["id"] for item in unresolved[:5])
            raise TransitionError("unresolved_review_findings", f"AI completion blocked by unresolved review findings: {ids}")
        latest_verification, snapshot = _latest_verified_completion_evidence(store, task_id, cwd)
        from .workflow_context import release_commit_transition_metadata, release_commit_verified_verification

        release_verified = release_commit_verified_verification(store, task_id, latest_verification, snapshot)
        if release_verified and release_verified.get("id") == latest_verification.get("id") and evidence_status(latest_verification, snapshot) not in {"current", "recorded"}:
            commit_transition = release_commit_transition_metadata(store, task_id)
    else:
        raise TransitionError("invalid_actor", "actor must be human or ai")
    created_at = now_iso()
    completed_snapshot = compact_snapshot(snapshot)
    if commit_transition:
        completed_snapshot["commit_transition"] = commit_transition
    row = {
        "id": make_id("completion"),
        "task_id": task_id,
        "actor": actor,
        "completed_by": actor,
        "completed_snapshot": completed_snapshot,
        "completion_note": reason,
        "accepted_verification_run_ids": [latest_verification["id"]] if latest_verification else [],
        "accepted_review_result_ids": [latest_review["id"]] if latest_review else [],
        "human_decision_note": decision_note or reason if actor == "human" else "",
        "completed_with_reservations": completed_with_reservations,
        "decision_source": decision_source,
        "human_confirmed": human_confirm,
        "completed_at": created_at,
        "reason": reason,
        "created_at": created_at,
    }
    store.insert("task_completions", row)
    _event(
        store,
        "complete_task",
        "task",
        task_id,
        actor=actor,
        decision_source=decision_source,
        human_confirmed=human_confirm,
        reason=reason,
        previous_state=previous_status,
        new_state=completion_status(actor),
        related_ids={"completion": row["id"]},
        snapshot=compact_snapshot(snapshot),
        warnings=[],
    )
    from .state_audit import audit_task

    audit = audit_task(store, task_id, cwd=cwd, current_snapshot=snapshot)
    blocking = [item for item in audit if item["severity"] == "error" and item["code"].startswith("completion_")]
    if blocking:
        store.update(
            "task_completions",
            row["id"],
            {
                "invalidated_at": now_iso(),
                "invalidated_by": "transition_audit",
                "invalidation_reason": "; ".join(item["code"] for item in blocking),
            },
        )
        _event(
            store,
            "invalidate_task_completion",
            "task_completion",
            row["id"],
            actor="transition_audit",
            reason="; ".join(item["code"] for item in blocking),
            previous_state="active",
            new_state="invalidated",
            related_ids={"task": task_id},
        )
        raise TransitionError("completion_audit_failed", "completion failed post-write audit", ", ".join(item["code"] for item in blocking))
    return _result(
        "complete_task",
        actor,
        created_ids={"task_completion": row["id"]},
        previous_status=previous_status,
        new_status=completion_status(actor),
        audit_notes=[item["code"] for item in audit],
        warnings=[],
    )


complete_task = atomic_transition(complete_task)


def invalidate_task_completion(store: Store, completion_id: str, *, actor: str, reason: str) -> TransitionResult:
    _require_actor(actor)
    completion = store.get("task_completions", completion_id)
    if not completion:
        raise TransitionError("completion_not_found", f"task completion not found: {completion_id}")
    if completion.get("invalidated_at"):
        raise TransitionError("completion_already_invalidated", f"task completion already invalidated: {completion_id}")
    store.update(
        "task_completions",
        completion_id,
        {"invalidated_at": now_iso(), "invalidated_by": actor, "invalidation_reason": reason},
    )
    _event(
        store,
        "invalidate_task_completion",
        "task_completion",
        completion_id,
        actor=actor,
        reason=reason,
        previous_state="active",
        new_state="invalidated",
        related_ids={"task": completion["task_id"]},
    )
    return _result("invalidate_task_completion", actor, updated_ids={"task_completion": completion_id}, previous_status="active", new_status="invalidated")


invalidate_task_completion = atomic_transition(invalidate_task_completion)


def record_outcome_decision(
    store: Store,
    task_id: str,
    *,
    decision: str,
    actor: str,
    reason: str,
    concerns: list[str] | None = None,
    human_confirm: bool = False,
    decision_source: str = "human_interactive",
    decision_note: str = "",
    cwd: Path | None = None,
    expected_task_event_id: str | None = None,
) -> TransitionResult:
    if decision in {"accepted", "accepted_with_concerns"}:
        return complete_task(
            store,
            task_id,
            actor=actor,
            reason=reason,
            human_confirm=human_confirm,
            decision_source=decision_source,
            decision_note="\n".join([decision_note or reason, *(concerns or [])]).strip(),
            cwd=cwd,
            completed_with_reservations=decision == "accepted_with_concerns",
            expected_task_event_id=expected_task_event_id,
        )
    _require_actor(actor)
    task = store.get("tasks", task_id)
    if not task:
        raise TransitionError("task_not_found", f"task not found: {task_id}")
    previous = projected_task_status(store, task)
    from .failure import record_failure_log

    failure = record_failure_log(
        store,
        task["project_id"],
        task_id,
        "",
        f"{actor}_{decision}",
        reason,
        "high" if decision == "rejected" else "medium",
        source="outcome_record",
        actor=actor,
        snapshot=compact_snapshot(current_git_snapshot(cwd or Path.cwd())),
    )
    if decision in {"rejected", "deferred"}:
        from .workflow_context import recipe_run_for_task

        recipe_run = recipe_run_for_task(store, task_id)
        if recipe_run and recipe_run.get("status") in {"active", "paused_for_fix", "waiting_public_approval"}:
            metadata = {
                **(recipe_run.get("metadata") or {}),
                "cancelled_by_outcome": decision,
                "cancellation_reason": reason,
            }
            store.update(
                "recipe_runs",
                recipe_run["id"],
                {
                    "status": "cancelled",
                    "current_step": "cancelled",
                    "pending_steps": [],
                    "pending_public_operations": [],
                    "metadata": metadata,
                    "updated_at": now_iso(),
                },
            )
    _event(store, "record_outcome_decision", "task", task_id, actor=actor, reason=reason, previous_state=previous, new_state=decision, related_ids={"failure": failure["id"]})
    return _result("record_outcome_decision", actor, created_ids={"failure": failure["id"]}, previous_status=previous, new_status=decision)


record_outcome_decision = atomic_transition(record_outcome_decision)


def cancel_task(store: Store, task_id: str, *, actor: str, reason: str, human_confirm: bool = False, decision_note: str = "") -> TransitionResult:
    _require_actor(actor)
    if actor == "human":
        _require_human_decision(actor, human_confirm, decision_note, "human_explicit")
    elif human_confirm or decision_note:
        raise TransitionError("ai_human_decision_forbidden", "AI cannot attach a human cancellation decision")
    if not reason.strip():
        raise TransitionError("reason_required", "task cancellation requires a reason")
    task = store.get("tasks", task_id)
    if not task:
        raise TransitionError("task_not_found", f"task not found: {task_id}")
    previous = projected_task_status(store, task)
    from .task_logic import is_task_closed_status
    from .workflow_context import recipe_run_for_task

    if is_task_closed_status(previous):
        raise TransitionError("task_already_closed", f"task is already closed: {task_id} ({previous})")
    recipe_run = recipe_run_for_task(store, task_id)
    if recipe_run and recipe_run.get("status") in {"active", "paused_for_fix", "waiting_public_approval"}:
        raise TransitionError("active_recipe_cannot_cancel", "active recipe task cannot be cancelled; finish or explicitly reject the recipe")
    cancellation_id = make_id("outcome")
    store.insert("outcome_reviews", {"id": cancellation_id, "task_id": task_id, "agent_report_id": "", "evidence_check_id": "", "decision": "cancelled", "reason": reason, "concerns": [], "rework_required": False, "created_at": now_iso()})
    _event(store, "cancel_task", "task", task_id, actor=actor, decision_source="human_explicit" if actor == "human" else "ai", human_confirmed=human_confirm, reason=reason, previous_state=previous, new_state="cancelled", related_ids={"outcome_review": cancellation_id})
    return _result("cancel_task", actor, created_ids={"outcome_review": cancellation_id}, previous_status=previous, new_status="cancelled")


cancel_task = atomic_transition(cancel_task)


def accept_roadmap_revision(
    store: Store,
    revision_id: str,
    *,
    actor: str,
    reason: str,
    decision_note: str = "",
    human_confirm: bool = False,
    decision_source: str = "human_interactive",
    expected_revision_status: str | None = None,
) -> TransitionResult:
    _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    revision = store.get("roadmap_revisions", revision_id)
    if not revision:
        raise TransitionError("roadmap_revision_not_found", f"roadmap revision not found: {revision_id}")
    if revision["status"] != "pending":
        raise TransitionError("roadmap_revision_not_pending", f"roadmap revision is not pending: {revision_id}")
    _require_expected(revision["status"], expected_revision_status, "stale_roadmap_revision", f"stale roadmap revision state: expected={expected_revision_status}, current={revision['status']}")
    commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
    if not commitment:
        raise TransitionError("roadmap_commitment_not_found", f"roadmap commitment not found: {revision['proposed_commitment_id']}")
    accepted_at = now_iso()
    store.update(
        "roadmap_revisions",
        revision_id,
        {"status": "accepted", "reason": reason, "decided_by": actor, "decision_source": decision_source, "decision_note": decision_note or reason, "human_confirmed": human_confirm, "accepted_at": accepted_at},
    )
    store.update(
        "roadmap_commitments",
        commitment["id"],
        {"status": "accepted", "accepted_by": actor, "accepted_at": accepted_at, "decision_source": decision_source, "decision_note": decision_note or reason, "human_confirmed": human_confirm},
    )
    _event(store, "accept_roadmap_revision", "roadmap_revision", revision_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=revision["status"], new_state="accepted", related_ids={"commitment": commitment["id"]})
    return _result("accept_roadmap_revision", actor, updated_ids={"roadmap_revision": revision_id, "roadmap_commitment": commitment["id"]}, previous_status=revision["status"], new_status="accepted")


accept_roadmap_revision = atomic_transition(accept_roadmap_revision)


def adopt_roadmap_proposal(
    store: Store,
    *,
    project_id: str,
    proposal: dict,
    body_md: str,
    source_path: str,
    actor: str,
    reason: str,
    decision_note: str = "",
    human_confirm: bool = False,
    decision_source: str = "human_interactive",
) -> TransitionResult:
    _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    created_at = now_iso()
    commitment_id = make_id("commitment")
    revision_id = make_id("roadmap_rev")
    store.insert(
        "roadmap_commitments",
        {
            "id": commitment_id,
            "project_id": project_id,
            "title": proposal["title"],
            "intent": proposal["intent"],
            "success_criteria": proposal["success_criteria"],
            "non_goals": proposal["non_goals"],
            "autonomy_scope": proposal["autonomy_scope"],
            "review_gates": proposal["review_gates"],
            "evidence_policy": proposal["evidence_policy"],
            "status": "accepted",
            "accepted_by": actor,
            "accepted_at": created_at,
            "decision_source": decision_source,
            "decision_note": decision_note or reason,
            "human_confirmed": human_confirm,
            "created_at": created_at,
        },
    )
    store.insert(
        "roadmap_revisions",
        {
            "id": revision_id,
            "project_id": project_id,
            "proposed_commitment_id": commitment_id,
            "status": "accepted",
            "body_md": body_md,
            "source_path": source_path,
            "reason": reason,
            "decided_by": actor,
            "decision_source": decision_source,
            "decision_note": decision_note or reason,
            "human_confirmed": human_confirm,
            "accepted_at": created_at,
            "created_at": created_at,
        },
    )
    _event(store, "adopt_roadmap_proposal", "roadmap_commitment", commitment_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state="missing", new_state="accepted", related_ids={"roadmap_revision": revision_id})
    return _result("adopt_roadmap_proposal", actor, created_ids={"roadmap_commitment": commitment_id, "roadmap_revision": revision_id}, previous_status="missing", new_status="accepted")


adopt_roadmap_proposal = atomic_transition(adopt_roadmap_proposal)


def reject_roadmap_revision(store: Store, revision_id: str, *, actor: str, reason: str, decision_note: str = "", human_confirm: bool = False, decision_source: str = "human_interactive", expected_revision_status: str | None = None) -> TransitionResult:
    _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    revision = store.get("roadmap_revisions", revision_id)
    if not revision:
        raise TransitionError("roadmap_revision_not_found", f"roadmap revision not found: {revision_id}")
    commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
    if revision["status"] != "pending":
        raise TransitionError("roadmap_revision_not_pending", f"roadmap revision is not pending: {revision_id}")
    _require_expected(revision["status"], expected_revision_status, "stale_roadmap_revision", f"stale roadmap revision state: expected={expected_revision_status}, current={revision['status']}")
    rejected_at = now_iso()
    store.update("roadmap_revisions", revision_id, {"status": "rejected", "reason": reason, "decided_by": actor, "decision_source": decision_source, "decision_note": decision_note or reason, "human_confirmed": human_confirm, "accepted_at": rejected_at})
    if commitment:
        store.update("roadmap_commitments", commitment["id"], {"status": "rejected", "accepted_by": actor, "accepted_at": rejected_at, "decision_source": decision_source, "decision_note": decision_note or reason, "human_confirmed": human_confirm})
    _event(store, "reject_roadmap_revision", "roadmap_revision", revision_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=revision["status"], new_state="rejected", related_ids={"commitment": commitment["id"] if commitment else ""})
    return _result("reject_roadmap_revision", actor, updated_ids={"roadmap_revision": revision_id}, previous_status=revision["status"], new_status="rejected")


reject_roadmap_revision = atomic_transition(reject_roadmap_revision)


def close_roadmap_commitment(
    store: Store,
    commitment_id: str,
    *,
    actor: str,
    reason: str,
    decision_note: str = "",
    closure_ready: bool,
    force: bool = False,
    human_confirm: bool = False,
    decision_source: str = "",
    expected_commitment_status: str | None = None,
) -> TransitionResult:
    _require_actor(actor)
    commitment = store.get("roadmap_commitments", commitment_id)
    if not commitment:
        raise TransitionError("roadmap_commitment_not_found", f"roadmap commitment not found: {commitment_id}")
    if commitment["status"] != "accepted":
        raise TransitionError("roadmap_commitment_not_accepted", f"roadmap commitment is not accepted: {commitment_id}")
    _require_expected(commitment["status"], expected_commitment_status, "stale_roadmap_commitment", f"stale roadmap commitment state: expected={expected_commitment_status}, current={commitment['status']}")
    if force:
        _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    elif not closure_ready:
        raise TransitionError("roadmap_commitment_not_ready", "roadmap commitment is not closure-ready")
    closed_at = now_iso()
    store.update(
        "roadmap_commitments",
        commitment_id,
        {"status": "closed", "closed_by": actor, "closed_at": closed_at, "closure_reason": reason, "decision_source": decision_source, "decision_note": decision_note or reason, "human_confirmed": human_confirm},
    )
    _event(store, "close_roadmap_commitment", "roadmap_commitment", commitment_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state="accepted", new_state="closed")
    return _result("close_roadmap_commitment", actor, updated_ids={"roadmap_commitment": commitment_id}, previous_status="accepted", new_status="closed")


close_roadmap_commitment = atomic_transition(close_roadmap_commitment)


def resolve_failure(
    store: Store,
    failure_id: str,
    *,
    actor: str,
    reason: str,
    human_confirm: bool = False,
    decision_source: str = "",
    decision_note: str = "",
    expected_status: str | None = None,
) -> TransitionResult:
    _require_actor(actor)
    failure = store.get("failure_logs", failure_id)
    if not failure:
        raise TransitionError("failure_not_found", f"failure not found: {failure_id}")
    _require_expected(failure["status"], expected_status, "stale_failure", f"stale failure state: expected={expected_status}, current={failure['status']}")
    if actor == "human":
        _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    elif actor == "ai":
        verification = store.latest_for_task("verification_runs", failure["task_id"])
        review = store.latest_for_task("review_results", failure["task_id"])
        if not verification and not review:
            raise TransitionError("failure_resolution_evidence_required", "AI failure resolution requires verification or review evidence")
    else:
        raise TransitionError("invalid_actor", "actor must be human or ai")
    store.update(
        "failure_logs",
        failure_id,
        {
            "status": "resolved",
            "resolved_at": now_iso(),
            "resolved_by": actor,
            "resolution_note": reason,
            "decision_note": decision_note,
            "resolution_source": decision_source,
            "human_confirmed": human_confirm,
        },
    )
    _event(store, "resolve_failure", "failure", failure_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=failure["status"], new_state="resolved")
    return _result("resolve_failure", actor, updated_ids={"failure": failure_id}, previous_status=failure["status"], new_status="resolved")


resolve_failure = atomic_transition(resolve_failure)


def ignore_failure(
    store: Store,
    failure_id: str,
    *,
    actor: str,
    reason: str,
    human_confirm: bool = False,
    decision_source: str = "human_interactive",
    decision_note: str = "",
    expected_status: str | None = None,
) -> TransitionResult:
    _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    failure = store.get("failure_logs", failure_id)
    if not failure:
        raise TransitionError("failure_not_found", f"failure not found: {failure_id}")
    _require_expected(failure["status"], expected_status, "stale_failure", f"stale failure state: expected={expected_status}, current={failure['status']}")
    store.update(
        "failure_logs",
        failure_id,
        {
            "status": "ignored",
            "resolved_at": now_iso(),
            "resolved_by": actor,
            "resolution_note": reason,
            "decision_note": decision_note,
            "resolution_source": decision_source,
            "human_confirmed": human_confirm,
        },
    )
    _event(store, "ignore_failure", "failure", failure_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=failure["status"], new_state="ignored")
    return _result("ignore_failure", actor, updated_ids={"failure": failure_id}, previous_status=failure["status"], new_status="ignored")


ignore_failure = atomic_transition(ignore_failure)


def approve_understanding(
    store: Store,
    task_id: str,
    *,
    actor: str,
    reason: str,
    human_confirm: bool = False,
    decision_source: str = "human_interactive",
    decision_note: str = "",
) -> TransitionResult:
    _require_human_decision(actor, human_confirm, decision_note or reason, decision_source)
    latest = store.latest_for_task("understanding_checks", task_id)
    if not latest or latest["status"] != "understanding_reported":
        raise TransitionError("understanding_report_required", "understanding report import required before approval")
    row = {"id": make_id("understanding"), "task_id": task_id, "status": "approved_to_implement", "body_md": latest["body_md"], "actor": actor, "reason": reason, "decision_source": decision_source, "human_confirmed": human_confirm, "created_at": now_iso()}
    store.insert("understanding_checks", row)
    _event(store, "approve_understanding", "task", task_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=latest["status"], new_state="approved_to_implement", related_ids={"understanding": row["id"]})
    return _result("approve_understanding", actor, created_ids={"understanding_check": row["id"]}, previous_status=latest["status"], new_status="approved_to_implement")


approve_understanding = atomic_transition(approve_understanding)


def update_review_finding(
    store: Store,
    finding_id: str,
    *,
    status: str,
    reason: str,
    actor: str,
    human_confirm: bool = False,
    decision_source: str = "",
    expected_status: str | None = None,
) -> TransitionResult:
    _require_actor(actor)
    if status not in VALID_FINDING_STATUSES:
        raise TransitionError("invalid_finding_status", f"invalid finding status: {status}")
    finding = store.get("review_findings", finding_id)
    if not finding:
        raise TransitionError("review_finding_not_found", f"review finding not found: {finding_id}")
    _require_expected(finding["status"], expected_status, "stale_review_finding", f"stale review finding state: expected={expected_status}, current={finding['status']}")
    if status == "accepted-risk":
        _require_human_decision(actor, human_confirm, reason, decision_source)
    warnings: list[str] = []
    if actor == "ai" and status == "addressed":
        if not store.latest_for_task("verification_runs", finding["task_id"]) and not store.latest_for_task("agent_reports", finding["task_id"]):
            warnings.append("addressed_without_recent_report_or_verification")
    updated_at = now_iso()
    update = {"id": make_id("finding_update"), "finding_id": finding_id, "task_id": finding["task_id"], "previous_status": finding["status"], "new_status": status, "reason": reason, "actor": actor, "decision_source": decision_source, "human_confirmed": human_confirm, "created_at": updated_at}
    store.insert("review_finding_updates", update)
    store.update("review_findings", finding_id, {"status": status, "updated_at": updated_at})
    _event(store, "update_review_finding", "review_finding", finding_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=finding["status"], new_state=status, related_ids={"finding_update": update["id"]}, warnings=warnings)
    return _result("update_review_finding", actor, created_ids={"review_finding_update": update["id"]}, updated_ids={"review_finding": finding_id}, previous_status=finding["status"], new_status=status, warnings=warnings)


update_review_finding = atomic_transition(update_review_finding)


def triage_todo(
    store: Store,
    todo_id: str,
    *,
    status: str,
    reason: str,
    actor: str,
    human_confirm: bool = False,
    decision_source: str = "",
    commitment_id: str = "",
    roadmap_revision_id: str = "",
    expected_status: str | None = None,
) -> TransitionResult:
    _require_actor(actor)
    todo = store.get("todos", todo_id)
    if not todo:
        raise TransitionError("todo_not_found", f"todo not found: {todo_id}")
    _require_expected(todo["status"], expected_status, "stale_todo", f"stale todo state: expected={expected_status}, current={todo['status']}")
    if status in {"rejected", "deferred"} and not (human_confirm and actor == "human") and not (commitment_id or roadmap_revision_id):
        raise TransitionError("todo_close_decision_required", "closing a todo requires human confirmation or a linked successor")
    if status in {"rejected", "deferred"} and actor == "human":
        _require_human_decision(actor, human_confirm, reason, decision_source)
    if actor == "ai" and status not in AI_ALLOWED_TODO_STATUSES and status not in {"superseded", "converted_to_task"}:
        raise TransitionError("todo_transition_not_ai_allowed", f"AI cannot set todo status {status}")
    values = {"status": status, "triaged_at": now_iso(), "triage_reason": reason, "actor": actor, "decision_source": decision_source}
    if commitment_id:
        values["roadmap_commitment_id"] = commitment_id
    if roadmap_revision_id:
        values["roadmap_revision_id"] = roadmap_revision_id
    store.update("todos", todo_id, values)
    _event(store, "triage_todo", "todo", todo_id, actor=actor, decision_source=decision_source, human_confirmed=human_confirm, reason=reason, previous_state=todo["status"], new_state=status, related_ids={"commitment": commitment_id, "roadmap_revision": roadmap_revision_id})
    return _result("triage_todo", actor, updated_ids={"todo": todo_id}, previous_status=todo["status"], new_status=status)


triage_todo = atomic_transition(triage_todo)


def create_task_from_todo(store: Store, todo_id: str, *, task: dict, actor: str, reason: str = "", expected_todo_status: str | None = None) -> TransitionResult:
    _require_actor(actor)
    todo = store.get("todos", todo_id)
    if not todo:
        raise TransitionError("todo_not_found", f"todo not found: {todo_id}")
    _require_expected(todo["status"], expected_todo_status, "stale_todo", f"stale todo state: expected={expected_todo_status}, current={todo['status']}")
    store.insert("tasks", task)
    store.update("todos", todo_id, {"status": "converted_to_task", "converted_task_id": task["id"], "triaged_at": now_iso(), "triage_reason": reason or f"converted to task {task['id']}", "actor": actor, "decision_source": "successor_link", "superseded_by_type": "task", "superseded_by_id": task["id"]})
    _event(store, "create_task_from_todo", "todo", todo_id, actor=actor, reason=reason, previous_state=todo["status"], new_state="converted_to_task", related_ids={"task": task["id"]})
    return _result("create_task_from_todo", actor, created_ids={"task": task["id"]}, updated_ids={"todo": todo_id}, previous_status=todo["status"], new_status="converted_to_task")


create_task_from_todo = atomic_transition(create_task_from_todo)


def promote_todo_to_roadmap_proposal(store: Store, todo_id: str, *, commitment: dict, revision: dict, actor: str, reason: str, expected_todo_status: str | None = None) -> TransitionResult:
    _require_actor(actor)
    todo = store.get("todos", todo_id)
    if not todo:
        raise TransitionError("todo_not_found", f"todo not found: {todo_id}")
    _require_expected(todo["status"], expected_todo_status, "stale_todo", f"stale todo state: expected={expected_todo_status}, current={todo['status']}")
    store.insert("roadmap_commitments", commitment)
    store.insert("roadmap_revisions", revision)
    store.update("todos", todo_id, {"status": "superseded", "roadmap_revision_id": revision["id"], "triaged_at": now_iso(), "triage_reason": reason, "actor": actor, "decision_source": "successor_link", "superseded_by_type": "roadmap_revision", "superseded_by_id": revision["id"]})
    _event(store, "promote_todo_to_roadmap_proposal", "todo", todo_id, actor=actor, reason=reason, previous_state=todo["status"], new_state="superseded", related_ids={"roadmap_revision": revision["id"], "roadmap_commitment": commitment["id"]})
    return _result("promote_todo_to_roadmap_proposal", actor, created_ids={"roadmap_commitment": commitment["id"], "roadmap_revision": revision["id"]}, updated_ids={"todo": todo_id}, previous_status=todo["status"], new_status="superseded")


promote_todo_to_roadmap_proposal = atomic_transition(promote_todo_to_roadmap_proposal)


def import_review_result(
    store: Store,
    task_id: str,
    review_id: str,
    *,
    body_md: str,
    reviewer: str,
    last_seen_event_id: str,
    cwd: Path | None = None,
) -> TransitionResult:
    _require_actor(reviewer)
    task = store.get("tasks", task_id)
    if not task:
        raise TransitionError("task_not_found", f"task not found: {task_id}")
    request = store.get("review_requests", review_id)
    if not request or request["task_id"] != task_id:
        raise TransitionError("review_request_not_found", f"review request not found for task: {review_id}")
    if request["status"] not in {"claimed", "in_progress"}:
        raise TransitionError("review_request_not_claimed", f"review request must be claimed or in_progress before import: {review_id} [{request['status']}]")
    if reviewer != request["reviewer"]:
        raise TransitionError("reviewer_mismatch", f"reviewer mismatch for review {review_id}: expected {request['reviewer']}, got {reviewer}")
    latest = store.latest_task_status_event(task_id)
    current_event_id = latest["event_id"] if latest else ""
    if last_seen_event_id != current_event_id:
        raise TransitionError("stale_review_context", f"stale task state: last_seen_event_id={last_seen_event_id}, current_event_id={current_event_id}")
    request_snapshot = request.get("based_on_snapshot") or {}
    if not request_snapshot:
        raise TransitionError("review_request_snapshot_missing", "review request has no based_on_snapshot")
    if not snapshots_match(request_snapshot, compact_snapshot(current_git_snapshot(cwd or Path.cwd()))):
        raise TransitionError("stale_review_snapshot", "review request based_on_snapshot is stale")
    verdict, summary, findings = parse_review_result(body_md)
    created_at = now_iso()
    result = {"id": make_id("review_result"), "task_id": task_id, "review_request_id": review_id, "reviewer": reviewer, "verdict": verdict, "summary": mask_secrets(summary), "based_on_event_id": request.get("based_on_event_id", ""), "based_on_snapshot": request.get("based_on_snapshot", {}), "body_md": mask_secrets(body_md), "created_at": created_at}
    store.insert("review_results", result)
    created_findings = []
    for finding in findings:
        row = {"id": make_id("finding"), "task_id": task_id, "review_request_id": review_id, "review_result_id": result["id"], "title": mask_secrets(finding["title"]), "severity": finding["severity"], "status": finding["status"], "file_path": mask_secrets(finding["file_path"]), "line": mask_secrets(finding["line"]), "blocking": finding["blocking"], "description": mask_secrets(finding["description"]), "created_at": created_at, "updated_at": created_at}
        store.insert("review_findings", row)
        created_findings.append(row["id"])
    store.update("review_requests", review_id, {"status": "completed", "updated_at": created_at})
    _event(store, "import_review_result", "review_request", review_id, actor=reviewer, reason="review result import", previous_state=request["status"], new_state="completed", related_ids={"review_result": result["id"], "findings": ",".join(created_findings)})
    return _result("import_review_result", reviewer, created_ids={"review_result": result["id"]}, updated_ids={"review_request": review_id}, previous_status=request["status"], new_status="completed")


import_review_result = atomic_transition(import_review_result)


def record_verification_run(store: Store, task_id: str, *, row: dict, actor: str = "ai") -> TransitionResult:
    _require_actor(actor)
    task = store.get("tasks", task_id)
    if not task:
        raise TransitionError("task_not_found", f"task not found: {task_id}")
    if not task.get("base_commit") and row.get("git_head"):
        store.update("tasks", task_id, {"base_commit": row["git_head"]})
    store.insert("verification_runs", row)
    _event(store, "record_verification_run", "task", task_id, actor=actor, reason=row.get("command", ""), new_state="verification_recorded", related_ids={"verification": row["id"]})
    return _result("record_verification_run", actor, created_ids={"verification_run": row["id"]}, new_status="verification_recorded")


record_verification_run = atomic_transition(record_verification_run)


def import_agent_report(store: Store, task: dict, markdown: str, agent: str, cwd: Path, evaluate_evidence) -> TransitionResult:
    _require_actor(agent)
    result = import_agent_report_body(store, task, markdown, agent, cwd, evaluate_evidence)
    report = store.latest_for_task("agent_reports", task["id"])
    evidence = result["evidence_status"]
    report_status = "agent_reported" if evidence["status"] == "present" else "needs_human_review"
    _event(store, "import_agent_report", "task", task["id"], actor=agent, reason="agent report import", new_state=report_status, related_ids={"agent_report": report["id"] if report else ""})
    return _result(
        "import_agent_report",
        agent,
        created_ids={"agent_report": report["id"] if report else ""},
        new_status=report_status,
        audit_notes=[evidence["status"]],
        warnings=evidence.get("issues", []),
    )


import_agent_report = atomic_transition(import_agent_report)
