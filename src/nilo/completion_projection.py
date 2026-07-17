from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .snapshot import commit_aware_evidence_status, current_git_snapshot, review_result_status
from .store import Store
from .task_logic import active_task_completion, completion_structural_issues, unresolved_review_findings
from .work_projection import EvidenceState, ReviewState


BEHAVIOR_CHANGING_TASK_TYPES = {"implementation", "refactor", "test_addition"}


class CompletionStage(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    REPORTED = "reported"
    VERIFICATION_REQUIRED = "verification_required"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_STALE = "verification_stale"
    VERIFIED = "verified"
    REVIEW_REQUIRED = "review_required"
    REVIEW_IN_PROGRESS = "review_in_progress"
    FINDINGS_OPEN = "findings_open"
    REVIEWED = "reviewed"
    NEEDS_HUMAN_ACCEPTANCE = "needs_human_acceptance"
    ACCEPTED = "accepted"
    ACCEPTED_WITH_RESERVATIONS = "accepted_with_reservations"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    LEGACY_PENDING = "legacy_pending"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class CompletionProjection:
    task_id: str
    stage: CompletionStage
    is_current_work: bool
    is_terminal: bool
    requires_human_action: bool
    evidence_state: EvidenceState
    review_state: ReviewState
    accepted_snapshot: dict | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "stage": self.stage.value,
            "is_current_work": self.is_current_work,
            "is_terminal": self.is_terminal,
            "requires_human_action": self.requires_human_action,
            "evidence_state": self.evidence_state.value,
            "review_state": self.review_state.value,
            "accepted_snapshot": self.accepted_snapshot,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _CompletionFacts:
    completion: dict | None = None
    findings: tuple[dict, ...] = ()
    verification: dict | None = None
    report: dict | None = None
    request: dict | None = None
    result: dict | None = None
    instruction: dict | None = None
    events: tuple[dict, ...] = ()
    requires_human: bool | None = None
    structural_issues: tuple[str, ...] = ()


def project_task_completion(
    store: Store,
    task: dict,
    *,
    current_snapshot: dict | None = None,
    current_commitment_ids: set[str] | None = None,
    explicit_current_task_ids: set[str] | None = None,
    facts: _CompletionFacts | None = None,
) -> CompletionProjection:
    """Derive completion state from primary facts without persisting a new state."""
    snapshot = current_snapshot or current_git_snapshot(Path.cwd(), mode="fast")
    task_id = task["id"]
    batched = facts is not None
    facts = facts or _CompletionFacts(
        completion=active_task_completion(store, task_id),
        findings=tuple(unresolved_review_findings(store, task_id)),
        verification=store.latest_for_task("verification_runs", task_id),
        report=store.latest_for_task("agent_reports", task_id),
        request=store.latest_for_task("review_requests", task_id),
        result=store.latest_for_task("review_results", task_id),
        instruction=store.latest_for_task("instructions", task_id),
        events=tuple(store.list_where("transition_events", "entity_type='task' AND entity_id=?", (task_id,))),
    )
    completion = facts.completion
    findings = list(facts.findings)
    verification = facts.verification
    report = facts.report
    request = facts.request
    result = facts.result
    instruction = facts.instruction
    events = list(facts.events)
    superseded = next(
        (
            event
            for event in events
            if event.get("transition") in {"supersede_task", "task_superseded"}
            or event.get("new_state") == "superseded"
        ),
        None,
    )
    cancelled = next(
        (
            event
            for event in events
            if event.get("transition") == "cancel_task"
            or event.get("new_state") == "cancelled"
        ),
        None,
    )
    evidence_raw = commit_aware_evidence_status(verification, snapshot, completion, strict=False)
    evidence = {
        "current": EvidenceState.CURRENT,
        "missing": EvidenceState.MISSING,
        "failed": EvidenceState.FAILED,
        "stale": EvidenceState.STALE,
    }.get(evidence_raw, EvidenceState.UNKNOWN)
    commitment_ids = current_commitment_ids or set()
    explicit_ids = explicit_current_task_ids if explicit_current_task_ids is not None else ({task_id} if not batched else set())
    in_current_commitment = bool(task.get("roadmap_commitment_id") in commitment_ids)
    review_open = bool(request and request.get("status") in {"requested", "claimed", "in_progress", "running"})
    current_reasons = tuple(
        reason
        for condition, reason in (
            (in_current_commitment, "current_roadmap_commitment"),
            (task_id in explicit_ids, "explicit_active_task"),
            (task.get("status") == "planned", "planned_task"),
            (review_open, "review_in_progress"),
            (bool(findings), "unresolved_review_findings"),
            (evidence is EvidenceState.CURRENT and not completion, "current_verification"),
            (bool(instruction) and task_id in explicit_ids, "instruction_selected"),
        )
        if condition
    )
    is_current = bool(current_reasons)
    warnings: list[str] = []

    def build(stage: CompletionStage, *, current: bool = is_current, reasons: tuple[str, ...] = current_reasons) -> CompletionProjection:
        review = ReviewState.NOT_REQUESTED
        if findings:
            review = ReviewState.FINDINGS_OPEN
        elif review_open:
            review = ReviewState.IN_PROGRESS
        elif result:
            review = ReviewState.APPROVED if result.get("verdict") == "approved" else ReviewState.CHANGES_REQUESTED
        return CompletionProjection(
            task_id,
            stage,
            current,
            stage in {
                CompletionStage.ACCEPTED,
                CompletionStage.ACCEPTED_WITH_RESERVATIONS,
                CompletionStage.CANCELLED,
                CompletionStage.SUPERSEDED,
            },
            stage is CompletionStage.NEEDS_HUMAN_ACCEPTANCE,
            evidence,
            review,
            completion.get("completed_snapshot") if completion else None,
            reasons,
            tuple(warnings),
        )

    structural_issues = list(facts.structural_issues) if batched else completion_structural_issues(store, task)
    if completion and (findings or structural_issues):
        warnings.extend(structural_issues)
        if findings:
            warnings.append("accepted_task_has_open_findings")
        return build(CompletionStage.INCONSISTENT, current=is_current, reasons=("primary_facts_conflict",))
    if completion:
        reserved = completion.get("outcome") == "accepted_with_reservations" or bool(completion.get("completed_with_reservations"))
        return build(CompletionStage.ACCEPTED_WITH_RESERVATIONS if reserved else CompletionStage.ACCEPTED, current=False, reasons=("task_completion_recorded",))
    if cancelled or task.get("status") == "cancelled":
        reasons = (f"transition:{cancelled['id']}",) if cancelled else ("task_status:cancelled",)
        return build(CompletionStage.CANCELLED, current=False, reasons=reasons)
    if superseded:
        return build(CompletionStage.SUPERSEDED, current=False, reasons=(f"transition:{superseded['id']}",))
    if findings:
        return build(CompletionStage.FINDINGS_OPEN, current=True)
    if review_open:
        return build(CompletionStage.REVIEW_IN_PROGRESS, current=True)
    if (report or result) and evidence is EvidenceState.FAILED:
        return build(CompletionStage.VERIFICATION_FAILED)
    if (report or result) and evidence is EvidenceState.STALE:
        return build(CompletionStage.VERIFICATION_STALE)
    if report and not verification and not result:
        return build(CompletionStage.REPORTED if is_current else CompletionStage.LEGACY_PENDING)
    if (report or result) and evidence in {EvidenceState.MISSING, EvidenceState.UNKNOWN}:
        return build(CompletionStage.VERIFICATION_REQUIRED if is_current else CompletionStage.LEGACY_PENDING)
    if request and not result and evidence is EvidenceState.CURRENT:
        return build(CompletionStage.REVIEW_REQUIRED if is_current else CompletionStage.LEGACY_PENDING)
    if result:
        freshness = review_result_status(result, snapshot)
        if result.get("verdict") == "approved" and freshness == "current":
            from .transitions import requires_human_completion

            requires_human = (
                facts.requires_human
                if facts.requires_human is not None
                else task.get("task_type") in BEHAVIOR_CHANGING_TASK_TYPES or requires_human_completion(store, task)
            )
            stage = CompletionStage.NEEDS_HUMAN_ACCEPTANCE if requires_human else CompletionStage.REVIEWED
            return build(stage if is_current else CompletionStage.LEGACY_PENDING)
        return build(CompletionStage.REVIEW_REQUIRED if is_current else CompletionStage.LEGACY_PENDING)
    if verification and evidence is EvidenceState.CURRENT:
        from .transitions import requires_human_completion

        requires_human = (
            facts.requires_human
            if facts.requires_human is not None
            else task.get("task_type") in BEHAVIOR_CHANGING_TASK_TYPES or requires_human_completion(store, task)
        )
        stage = CompletionStage.NEEDS_HUMAN_ACCEPTANCE if requires_human else CompletionStage.VERIFIED
        return build(stage if is_current else CompletionStage.LEGACY_PENDING)
    if report:
        return build(CompletionStage.REPORTED if is_current else CompletionStage.LEGACY_PENDING)
    if task.get("status") == "planned" and is_current:
        return build(CompletionStage.NOT_STARTED, current=True)
    if is_current:
        return build(CompletionStage.IN_PROGRESS)
    return build(CompletionStage.LEGACY_PENDING, current=False, reasons=("no_current_work_evidence",))


def project_completion_projections(
    store: Store,
    project_id: str,
    tasks: list[dict],
    *,
    current_snapshot: dict | None = None,
    current_commitment_ids: set[str] | None = None,
    explicit_current_task_ids: set[str] | None = None,
    statuses: dict[str, str] | None = None,
) -> dict[str, CompletionProjection]:
    from .project_logic import accepted_roadmap_commitments, related_tasks_for_commitment

    commitments = accepted_roadmap_commitments(store, project_id)
    commitment_ids = current_commitment_ids if current_commitment_ids is not None else ({commitments[0]["id"]} if commitments else set())
    snapshot = current_snapshot or current_git_snapshot(Path.cwd(), mode="fast")
    facts_by_task = _project_facts(store, tasks)
    activity_tasks = [
        (row.get("created_at", row.get("updated_at", "")), task_id)
        for task_id, facts in facts_by_task.items()
        for row in (facts.instruction, facts.report, facts.request, facts.result, facts.verification)
        if row
    ]
    explicit_ids = set(explicit_current_task_ids or ())
    explicit_ids.update(_recent_activity_task_ids(activity_tasks))
    if commitments and current_commitment_ids is None:
        explicit_ids.update(task["id"] for task in related_tasks_for_commitment(tasks, commitments[0]))
    if not commitments:
        explicit_ids.update(
            task["id"]
            for task in tasks
            if (statuses or {}).get(task["id"], task.get("status")) == "planned"
        )
    return {
        task["id"]: project_task_completion(
            store,
            {**task, "status": statuses.get(task["id"], task.get("status"))} if statuses else task,
            current_snapshot=snapshot,
            current_commitment_ids=commitment_ids,
            explicit_current_task_ids=explicit_ids,
            facts=facts_by_task[task["id"]],
        )
        for task in tasks
    }


def _project_facts(store: Store, tasks: list[dict]) -> dict[str, _CompletionFacts]:
    task_ids = [task["id"] for task in tasks]
    grouped: dict[str, dict[str, object]] = {task_id: {} for task_id in task_ids}
    table_keys = {
        "task_completions": "completion",
        "verification_runs": "verification",
        "agent_reports": "report",
        "review_requests": "request",
        "review_results": "result",
        "instructions": "instruction",
    }
    for table, key in table_keys.items():
        for row in _rows_for_tasks(store, table, task_ids):
            if table == "task_completions" and row.get("invalidated_at"):
                continue
            grouped[row["task_id"]].setdefault(key, row)
    for row in _rows_for_tasks(store, "review_findings", task_ids):
        if row.get("status") == "unresolved":
            grouped[row["task_id"]].setdefault("findings", []).append(row)
    for row in _rows_for_tasks(
        store,
        "transition_events",
        task_ids,
        task_column="entity_id",
        extra="entity_type='task'",
    ):
        grouped[row["entity_id"]].setdefault("events", []).append(row)
    release_task_ids = {
        row["task_id"]
        for row in _rows_for_tasks(store, "recipe_runs", task_ids)
        if row.get("recipe_name") == "release"
    }
    from .transitions import HIGH_RISK_COMPLETION_TERMS

    tasks_by_id = {task["id"]: task for task in tasks}
    open_high_failure_task_ids = {
        row["task_id"]
        for row in _rows_for_tasks(store, "failure_logs", task_ids)
        if row.get("status") == "open" and row.get("severity") == "high"
    }
    return {
        task_id: _CompletionFacts(
            completion=data.get("completion"),
            findings=tuple(data.get("findings", [])),
            verification=data.get("verification"),
            report=data.get("report"),
            request=data.get("request"),
            result=data.get("result"),
            instruction=data.get("instruction"),
            events=tuple(data.get("events", [])),
            requires_human=(
                tasks_by_id[task_id].get("task_type") in BEHAVIOR_CHANGING_TASK_TYPES
                or tasks_by_id[task_id].get("risk_level") == "high"
                or task_id in release_task_ids
                or any(
                    term in f"{tasks_by_id[task_id].get('title', '')} {tasks_by_id[task_id].get('description', '')}".lower()
                    for term in HIGH_RISK_COMPLETION_TERMS
                )
            ),
            structural_issues=tuple(
                issue
                for condition, issue in (
                    (
                        bool(data.get("completion"))
                        and (data["completion"].get("actor") or data["completion"].get("completed_by")) == "human"
                        and not str(data["completion"].get("human_decision_note") or "").strip(),
                        "human_decision_note_missing",
                    ),
                    (
                        bool(data.get("completion"))
                        and (data["completion"].get("actor") or data["completion"].get("completed_by")) == "ai"
                        and bool(data.get("findings")),
                        "ai_unresolved_review_findings",
                    ),
                    (
                        bool(data.get("completion"))
                        and tasks_by_id[task_id].get("task_type") == "implementation"
                        and not data["completion"].get("accepted_verification_run_ids"),
                        "missing_accepted_verification",
                    ),
                    (task_id in open_high_failure_task_ids, "open_high_failure"),
                )
                if condition
            ),
        )
        for task_id, data in grouped.items()
    }


def _recent_activity_task_ids(activity_tasks: list[tuple[str, str]]) -> set[str]:
    parsed: list[tuple[datetime, str]] = []
    for created_at, task_id in activity_tasks:
        if not created_at:
            continue
        try:
            parsed.append((datetime.fromisoformat(created_at), task_id))
        except ValueError:
            continue
    if not parsed:
        return set()
    cutoff = max(created_at for created_at, _ in parsed) - timedelta(hours=24)
    return {task_id for created_at, task_id in parsed if created_at >= cutoff}


def _rows_for_tasks(
    store: Store,
    table: str,
    task_ids: list[str],
    *,
    task_column: str = "task_id",
    extra: str = "",
) -> list[dict]:
    rows: list[dict] = []
    for start in range(0, len(task_ids), 500):
        chunk = task_ids[start : start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        clause = f"{extra} AND " if extra else ""
        rows.extend(store.list_where(table, f"{clause}{task_column} IN ({placeholders})", tuple(chunk)))
    return rows
