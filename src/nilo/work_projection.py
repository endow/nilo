from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from .snapshot import commit_aware_evidence_status, current_git_snapshot, review_result_status, snapshot_has_diff_hash
from .store import Store
from .task_logic import active_task_completion, is_task_closed_status, unresolved_review_findings


class WorkProjectionError(RuntimeError):
    """Raised when the projection API contract is violated."""


class WorkPhase(StrEnum):
    IDLE = "idle"
    INTAKE = "intake"
    PLANNING = "planning"
    READY = "ready"
    WORKING = "working"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    AWAITING_HUMAN = "awaiting_human"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class NextActionCode(StrEnum):
    NONE = "none"
    TRIAGE_TODO = "triage_todo"
    REVIEW_ROADMAP = "review_roadmap"
    APPROVE_ROADMAP = "approve_roadmap"
    CREATE_TASK = "create_task"
    START_TASK = "start_task"
    CONFIRM_UNDERSTANDING = "confirm_understanding"
    CONTINUE_WORK = "continue_work"
    IMPORT_AGENT_REPORT = "import_agent_report"
    RUN_VERIFICATION = "run_verification"
    RERUN_VERIFICATION = "rerun_verification"
    REQUEST_REVIEW = "request_review"
    WAIT_FOR_REVIEW = "wait_for_review"
    RESOLVE_REVIEW_FINDINGS = "resolve_review_findings"
    ACCEPT_COMPLETION = "accept_completion"
    REASSESS_STATE = "reassess_state"
    RESOLVE_BLOCKER = "resolve_blocker"


class EvidenceState(StrEnum):
    NOT_REQUIRED = "not_required"
    MISSING = "missing"
    FAILED = "failed"
    STALE = "stale"
    CURRENT = "current"
    UNKNOWN = "unknown"


class ReviewState(StrEnum):
    NOT_REQUIRED = "not_required"
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    FINDINGS_OPEN = "findings_open"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class CompletionState(StrEnum):
    NOT_READY = "not_ready"
    REPORTED = "reported"
    VERIFIED = "verified"
    REVIEWED = "reviewed"
    NEEDS_HUMAN_ACCEPTANCE = "needs_human_acceptance"
    ACCEPTED = "accepted"
    ACCEPTED_WITH_RESERVATIONS = "accepted_with_reservations"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NextAction:
    code: NextActionCode
    task_id: str | None = None
    roadmap_id: str | None = None
    todo_id: str | None = None
    command_hint: tuple[str, ...] = ()


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str


@dataclass(frozen=True)
class WorkProjection:
    project_id: str
    scope: Literal["project", "task", "roadmap", "todo"]
    active_task_id: str | None
    phase: WorkPhase
    next_action: NextAction
    blocker: Blocker | None
    evidence_state: EvidenceState
    review_state: ReviewState
    completion_state: CompletionState
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "scope": self.scope,
            "active_task_id": self.active_task_id,
            "phase": self.phase.value,
            "next_action": {
                "code": self.next_action.code.value,
                "task_id": self.next_action.task_id,
                "roadmap_id": self.next_action.roadmap_id,
                "todo_id": self.next_action.todo_id,
                "command_hint": list(self.next_action.command_hint),
            },
            "blocker": ({"code": self.blocker.code, "message": self.blocker.message} if self.blocker else None),
            "evidence_state": self.evidence_state.value,
            "review_state": self.review_state.value,
            "completion_state": self.completion_state.value,
            "human_next_action": next_action_text(self),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "diagnostics": dict(self.diagnostics),
        }


def _action(code: NextActionCode, task_id: str | None = None, *, hint: str = "") -> NextAction:
    return NextAction(code, task_id=task_id, command_hint=(hint,) if hint else ())


def task_work_projection(
    store: Store,
    task_id: str,
    *,
    cwd: Path | None = None,
    current_snapshot: dict[str, Any] | None = None,
) -> WorkProjection:
    from . import project_logic as p

    task = store.get("tasks", task_id)
    if not task:
        raise WorkProjectionError(f"task not found: {task_id}")
    snapshot = current_snapshot or current_git_snapshot(cwd or Path.cwd(), mode="fast")
    status = p.projected_task_status(store, task, current_snapshot=snapshot)
    completion = active_task_completion(store, task_id)
    findings = unresolved_review_findings(store, task_id)
    verification = store.latest_for_task("verification_runs", task_id)
    report = store.latest_for_task("agent_reports", task_id)
    request = store.latest_for_task("review_requests", task_id)
    result = store.latest_for_task("review_results", task_id)
    evidence_raw = commit_aware_evidence_status(verification, snapshot, completion, strict=False)
    evidence = {
        "current": EvidenceState.CURRENT,
        "present": EvidenceState.UNKNOWN,
        "recorded": EvidenceState.UNKNOWN,
        "missing": EvidenceState.MISSING,
        "stale": EvidenceState.STALE,
        "failed": EvidenceState.FAILED,
    }.get(evidence_raw, EvidenceState.UNKNOWN)
    unexecuted = p.unexecuted_verifications_for_task(status, verification)
    # Human-facing compatibility text is diagnostic only.  Projection must
    # remain usable for old/minimal task rows that predate ``task_type``.
    legacy_actions = p.task_next_actions(
        {**task, "task_type": task.get("task_type", "normal")},
        status,
        verification,
        unexecuted,
    )
    legacy_action = legacy_actions[0] if legacy_actions else ""
    if request and request.get("status") in {"requested", "claimed", "in_progress", "running", "stale", "failed", "reviewer_unavailable"}:
        legacy_action = p.next_action_for_review_request(store, request)
    diagnostics = MappingProxyType(
        {"task_status": status, "evidence_raw": evidence_raw, "legacy_next_action": legacy_action}
    )

    if is_task_closed_status(status):
        if findings:
            return _blocked(task["project_id"], task_id, "closed_task_has_findings", "完了済み Task に未解決 finding があります")
        completion_state = CompletionState.ACCEPTED_WITH_RESERVATIONS if completion and completion.get("outcome") == "accepted_with_reservations" else CompletionState.ACCEPTED
        closed_review_state = ReviewState.NOT_REQUIRED
        if result:
            closed_review_state = ReviewState.APPROVED if result.get("verdict") == "approved" else ReviewState.CHANGES_REQUESTED
        return WorkProjection(task["project_id"], "task", task_id, WorkPhase.COMPLETED, _action(NextActionCode.NONE, task_id), None, evidence, closed_review_state, completion_state, diagnostics=diagnostics)
    if findings:
        return WorkProjection(task["project_id"], "task", task_id, WorkPhase.REVIEWING, _action(NextActionCode.RESOLVE_REVIEW_FINDINGS, task_id, hint=f"nilo review status --task {task_id} --format json"), None, evidence, ReviewState.FINDINGS_OPEN, CompletionState.NOT_READY, (f"unresolved_review_findings:{len(findings)}",), diagnostics=diagnostics)
    if request and (not result or result.get("review_request_id") != request["id"]):
        request_status = request.get("status")
        if request_status in {"failed", "reviewer_unavailable", "stale"}:
            return WorkProjection(
                task["project_id"],
                "task",
                task_id,
                WorkPhase.BLOCKED,
                _action(
                    NextActionCode.RESOLVE_BLOCKER,
                    task_id,
                    hint=f"nilo review status --task {task_id} --format json",
                ),
                Blocker("review_unavailable", "reviewer が利用できません"),
                evidence,
                ReviewState.UNAVAILABLE,
                CompletionState.NOT_READY,
                diagnostics=diagnostics,
            )
        if request_status in {"requested", "claimed", "in_progress", "running"}:
            return WorkProjection(task["project_id"], "task", task_id, WorkPhase.REVIEWING, _action(NextActionCode.WAIT_FOR_REVIEW, task_id), None, evidence, ReviewState.IN_PROGRESS, CompletionState.NOT_READY, diagnostics=diagnostics)
    if (report or result) and evidence in {EvidenceState.MISSING, EvidenceState.FAILED, EvidenceState.STALE, EvidenceState.UNKNOWN}:
        code = NextActionCode.RUN_VERIFICATION if evidence is EvidenceState.MISSING else NextActionCode.RERUN_VERIFICATION
        return WorkProjection(task["project_id"], "task", task_id, WorkPhase.VERIFYING, _action(code, task_id, hint=f'nilo check --task {task_id} "<verification command>"'), None, evidence, ReviewState.NOT_REQUESTED, CompletionState.REPORTED, diagnostics=diagnostics)
    if result:
        freshness = (
            review_result_status(result, snapshot)
            if snapshot_has_diff_hash(snapshot) or snapshot.get("git_available") is False
            else "unknown"
        )
        if freshness == "stale":
            return WorkProjection(task["project_id"], "task", task_id, WorkPhase.REVIEWING, _action(NextActionCode.REQUEST_REVIEW, task_id), None, evidence, ReviewState.NOT_REQUESTED, CompletionState.VERIFIED, ("review_result_stale",), diagnostics=diagnostics)
        if result.get("verdict") != "approved":
            return WorkProjection(
                task["project_id"],
                "task",
                task_id,
                WorkPhase.WORKING,
                _action(NextActionCode.CONTINUE_WORK, task_id),
                None,
                evidence,
                ReviewState.CHANGES_REQUESTED,
                CompletionState.NOT_READY,
                (f"review_verdict:{result.get('verdict', 'unknown')}",),
                diagnostics=diagnostics,
            )
        if freshness == "unknown":
            return WorkProjection(
                task["project_id"],
                "task",
                task_id,
                WorkPhase.BLOCKED,
                _action(
                    NextActionCode.REASSESS_STATE,
                    task_id,
                    hint=f"nilo status --ai --verbose --project {task['project_id']}",
                ),
                Blocker("review_freshness_unknown", "fast snapshot では承認済み review の鮮度を確定できません"),
                evidence,
                ReviewState.UNKNOWN,
                CompletionState.NOT_READY,
                ("review_freshness_unknown",),
                diagnostics=diagnostics,
            )
        return WorkProjection(task["project_id"], "task", task_id, WorkPhase.AWAITING_HUMAN, _action(NextActionCode.ACCEPT_COMPLETION, task_id), None, evidence, ReviewState.APPROVED, CompletionState.NEEDS_HUMAN_ACCEPTANCE, diagnostics=diagnostics)
    if ((report or verification) and evidence is EvidenceState.CURRENT) or status == "verification_passed":
        return WorkProjection(
            task["project_id"],
            "task",
            task_id,
            WorkPhase.AWAITING_HUMAN,
            _action(NextActionCode.ACCEPT_COMPLETION, task_id),
            None,
            evidence,
            ReviewState.NOT_REQUIRED,
            CompletionState.NEEDS_HUMAN_ACCEPTANCE,
            diagnostics=diagnostics,
        )
    if status == "planned":
        return WorkProjection(task["project_id"], "task", task_id, WorkPhase.READY, _action(NextActionCode.START_TASK, task_id, hint=f"nilo instruct --task {task_id}"), None, evidence, ReviewState.NOT_REQUESTED, CompletionState.NOT_READY, diagnostics=diagnostics)
    return WorkProjection(task["project_id"], "task", task_id, WorkPhase.WORKING, _action(NextActionCode.CONTINUE_WORK, task_id), None, evidence, ReviewState.NOT_REQUESTED, CompletionState.NOT_READY, diagnostics=diagnostics)


def project_work_projection(
    store: Store,
    project_id: str,
    *,
    cwd: Path | None = None,
    current_snapshot: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    statuses: dict[str, str] | None = None,
) -> WorkProjection:
    from . import project_logic as p

    if not store.get("projects", project_id):
        raise WorkProjectionError(f"project not found: {project_id}")
    snapshot = current_snapshot or current_git_snapshot(cwd or Path.cwd(), mode="fast")
    if tasks is None or statuses is None:
        tasks, statuses = p.fast_project_tasks_and_recorded_statuses(store, project_id)
    active, commitments = p.roadmap_prioritized_project_active_tasks(store, project_id, tasks, statuses, current_snapshot=snapshot)
    if active:
        return task_work_projection(store, active[0]["id"], cwd=cwd, current_snapshot=snapshot)
    revisions = p.pending_roadmap_revisions(store, project_id)
    if revisions:
        revision = revisions[0]
        return WorkProjection(project_id, "roadmap", None, WorkPhase.PLANNING, NextAction(NextActionCode.REVIEW_ROADMAP, roadmap_id=revision["id"]), None, EvidenceState.NOT_REQUIRED, ReviewState.NOT_REQUIRED, CompletionState.NOT_READY)
    if commitments:
        commitment = p.selected_roadmap_commitment(
            store, commitments, tasks, statuses, current_snapshot=snapshot
        ) or commitments[0]
        assessment = p.roadmap_commitment_assessment(
            store, commitment, tasks, statuses, current_snapshot=snapshot
        )
        if assessment["status"] == "task_plan_required":
            return WorkProjection(project_id, "roadmap", None, WorkPhase.PLANNING, NextAction(NextActionCode.CREATE_TASK, roadmap_id=commitment["id"]), None, EvidenceState.NOT_REQUIRED, ReviewState.NOT_REQUIRED, CompletionState.NOT_READY)
        if assessment["status"] == "evidence_present":
            attention = p.roadmap_attention_summary(store, project_id, tasks=tasks, statuses=statuses)
            if attention["items"]:
                titles = "、".join(item["title"] for item in attention["items"])
                return WorkProjection(
                    project_id,
                    "roadmap",
                    None,
                    WorkPhase.BLOCKED,
                    NextAction(NextActionCode.REASSESS_STATE, command_hint=(f"nilo roadmap status --ai --project {project_id}",)),
                    Blocker("roadmap_evidence_attention", "変更ファイルとテストの対応が人間確認待ちです"),
                    EvidenceState.UNKNOWN,
                    ReviewState.NOT_REQUIRED,
                    CompletionState.NOT_READY,
                    diagnostics=MappingProxyType(
                        {"legacy_next_action": f"完了済みロードマップに証跡注意があります。{titles}"}
                    ),
                )
            return WorkProjection(project_id, "roadmap", None, WorkPhase.COMPLETED, NextAction(NextActionCode.NONE, roadmap_id=commitment["id"]), None, EvidenceState.NOT_REQUIRED, ReviewState.NOT_REQUIRED, CompletionState.ACCEPTED)
        return WorkProjection(
            project_id,
            "roadmap",
            None,
            WorkPhase.BLOCKED,
            NextAction(
                NextActionCode.REASSESS_STATE,
                roadmap_id=commitment["id"],
                command_hint=(f"nilo roadmap status --ai --project {project_id}",),
            ),
            Blocker("roadmap_evidence_incomplete", assessment.get("unresolved_reason") or assessment["status"]),
            EvidenceState.UNKNOWN,
            ReviewState.NOT_REQUIRED,
            CompletionState.NOT_READY,
            (f"roadmap_assessment:{assessment['status']}",),
        )
    residual_commitments = p.accepted_roadmap_commitments(store, project_id)
    if residual_commitments:
        commitment = p.selected_roadmap_commitment(
            store, residual_commitments, tasks, statuses, current_snapshot=snapshot
        ) or residual_commitments[0]
        assessment = p.roadmap_commitment_assessment(
            store, commitment, tasks, statuses, current_snapshot=snapshot
        )
        if assessment["status"] != "evidence_present":
            return WorkProjection(
                project_id,
                "roadmap",
                None,
                WorkPhase.BLOCKED,
                NextAction(
                    NextActionCode.REASSESS_STATE,
                    roadmap_id=commitment["id"],
                    command_hint=(f"nilo roadmap status --ai --project {project_id}",),
                ),
                Blocker("roadmap_evidence_attention", assessment.get("unresolved_reason") or assessment["status"]),
                EvidenceState.UNKNOWN,
                ReviewState.NOT_REQUIRED,
                CompletionState.NOT_READY,
                (f"roadmap_assessment:{assessment['status']}",),
                diagnostics=MappingProxyType(
                    {"legacy_next_action": "完了済みロードマップに証跡注意があります。証跡を確認してください。"}
                ),
            )
    attention = p.roadmap_attention_summary(store, project_id, tasks=tasks, statuses=statuses)
    if attention["items"]:
        titles = "、".join(item["title"] for item in attention["items"])
        return WorkProjection(
            project_id,
            "roadmap",
            None,
            WorkPhase.BLOCKED,
            NextAction(NextActionCode.REASSESS_STATE, command_hint=(f"nilo roadmap status --ai --project {project_id}",)),
            Blocker("roadmap_evidence_attention", "変更ファイルとテストの対応が人間確認待ちです"),
            EvidenceState.UNKNOWN,
            ReviewState.NOT_REQUIRED,
            CompletionState.NOT_READY,
            diagnostics=MappingProxyType(
                {"legacy_next_action": f"完了済みロードマップに証跡注意があります。{titles}"}
            ),
        )
    todos = store.list_where("todos", "project_id=?", (project_id,))
    for todo_status in ("ready", "requires_roadmap", "open", "deferred"):
        candidates = [row for row in todos if row.get("status") == todo_status]
        if candidates:
            # list_where is newest-first; preserve FIFO within each queue state.
            todo = candidates[-1]
            return WorkProjection(
                project_id,
                "todo",
                None,
                WorkPhase.INTAKE,
                NextAction(NextActionCode.TRIAGE_TODO, todo_id=todo["id"]),
                None,
                EvidenceState.NOT_REQUIRED,
                ReviewState.NOT_REQUIRED,
                CompletionState.NOT_READY,
                (f"todo_status:{todo_status}",),
            )
    return WorkProjection(project_id, "project", None, WorkPhase.IDLE, _action(NextActionCode.NONE), None, EvidenceState.NOT_REQUIRED, ReviewState.NOT_REQUIRED, CompletionState.NOT_READY)


def _blocked(project_id: str, task_id: str | None, code: str, message: str) -> WorkProjection:
    return WorkProjection(project_id, "task" if task_id else "project", task_id, WorkPhase.BLOCKED, _action(NextActionCode.REASSESS_STATE, task_id), Blocker(code, message), EvidenceState.UNKNOWN, ReviewState.UNKNOWN, CompletionState.UNKNOWN, (code,))


def next_action_text(projection: WorkProjection) -> str:
    action = projection.next_action
    target = action.task_id or action.roadmap_id or action.todo_id or ""
    if action.code is NextActionCode.CONTINUE_WORK and projection.diagnostics.get("task_status") == "completion_needs_review":
        return f"{target}: 最新のタスク状態を確認してください。"
    if action.code is NextActionCode.ACCEPT_COMPLETION:
        return f"{target}: 差分、変更ファイル、検証結果、未解決事項を確認してください。"
    legacy_action = projection.diagnostics.get("legacy_next_action")
    if legacy_action:
        from .human_status import human_next_action_text

        prefix = f"{target}: " if target else ""
        return f"{prefix}{human_next_action_text(str(legacy_action))}"
    labels = {
        NextActionCode.NONE: "現在必要な操作はありません。",
        NextActionCode.TRIAGE_TODO: "次の Todo を triage してください。",
        NextActionCode.REVIEW_ROADMAP: "Roadmap proposal を確認してください。",
        NextActionCode.APPROVE_ROADMAP: "Roadmap proposal の人間承認を待ってください。",
        NextActionCode.CREATE_TASK: "承認済み Roadmap から次の Task を作成してください。",
        NextActionCode.START_TASK: "Task の作業指示を生成してください。",
        NextActionCode.CONFIRM_UNDERSTANDING: "作業内容の理解確認を完了してください。",
        NextActionCode.CONTINUE_WORK: "現在の Task の作業を続け、完了報告を取り込んでください。",
        NextActionCode.IMPORT_AGENT_REPORT: "AgentReport を取り込んでください。",
        NextActionCode.RUN_VERIFICATION: "現在の変更を検証してください。",
        NextActionCode.RERUN_VERIFICATION: "現在の snapshot に対して検証を再実行してください。",
        NextActionCode.REQUEST_REVIEW: "Task のレビューを依頼してください。",
        NextActionCode.WAIT_FOR_REVIEW: "進行中のレビュー結果を待ってください。",
        NextActionCode.RESOLVE_REVIEW_FINDINGS: "未解決のレビュー指摘に対応してください。",
        NextActionCode.ACCEPT_COMPLETION: "人間による完了判断を行ってください。",
        NextActionCode.REASSESS_STATE: "保存状態を再評価してください。",
        NextActionCode.RESOLVE_BLOCKER: "blocker を解消してください。",
    }
    prefix = f"{target}: " if target else ""
    return prefix + labels[action.code]
