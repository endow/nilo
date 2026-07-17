"""Machine-readable classification of Nilo's SQLite tables.

This catalog is intentionally descriptive: it does not participate in runtime
state decisions.  Keep it in sync with ``store.SCHEMA`` and additive
migrations; ``tests/test_schema_inventory.py`` enforces table coverage.
"""

from __future__ import annotations

from typing import Final


PRIMARY_FACT = "Primary Fact"
DERIVED_PROJECTION = "Derived Projection"
OPERATIONAL_TRANSPORT = "Operational Transport"
HUMAN_ANNOTATION = "Human Annotation"
LEGACY_COMPATIBILITY = "Legacy Compatibility"
CONFIGURATION_REGISTRY = "Configuration / Registry"


SCHEMA_CATALOG: Final[dict[str, dict[str, str]]] = {
    "projects": {"concept": "Project", "classification": CONFIGURATION_REGISTRY, "recommendation": "Keep", "source_of_truth": "project configuration", "derived_from": "-", "retention": "project lifetime", "deletion": "explicit project removal only", "migration_risk": "high", "usage": "project identity, language, model and execution defaults"},
    "tasks": {"concept": "Task", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "authorized unit of work", "derived_from": "Todo or explicit human intent", "retention": "indefinite audit history", "deletion": "never during normal operation", "migration_risk": "high", "usage": "work identity and immutable intake fields; status is compatibility state"},
    "instructions": {"concept": "Instruction", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "issued instruction snapshot", "derived_from": "Task plus applicable rules", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "medium", "usage": "agent handoff and audit"},
    "agent_reports": {"concept": "AgentReport", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "agent self-report", "derived_from": "agent output", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "medium", "usage": "claimed work result; never proof of correctness"},
    "evidence_checks": {"concept": "ReportValidation", "classification": LEGACY_COMPATIBILITY, "recommendation": "Freeze", "source_of_truth": "legacy report validation result", "derived_from": "AgentReport format checks", "retention": "read existing rows indefinitely", "deletion": "Remove Later after compatibility audit", "migration_risk": "high", "usage": "schema remains and VerificationRun has optional link; no current production writer"},
    "verification_runs": {"concept": "VerificationRun", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "observed command and git snapshot", "derived_from": "local or reported command execution", "retention": "indefinite audit history; large output policy TBD", "deletion": "never during normal operation", "migration_risk": "high", "usage": "completion evidence and EvidenceStatus input"},
    "failure_logs": {"concept": "FailureLog", "classification": HUMAN_ANNOTATION, "recommendation": "Encapsulate", "source_of_truth": "recorded failure annotation", "derived_from": "observed failures and human decisions", "retention": "indefinite until an explicit policy exists", "deletion": "explicit maintenance only", "migration_risk": "medium", "usage": "reference ledger; not an automatic completion rule"},
    "model_profiles": {"concept": "ModelProfile", "classification": CONFIGURATION_REGISTRY, "recommendation": "Keep", "source_of_truth": "model capability registry", "derived_from": "operator configuration", "retention": "while referenced", "deletion": "only when unreferenced", "migration_risk": "low", "usage": "model selection metadata"},
    "model_usage_logs": {"concept": "ModelUsageLog", "classification": OPERATIONAL_TRANSPORT, "recommendation": "Keep", "source_of_truth": "model selection audit log", "derived_from": "model invocation", "retention": "define bounded audit retention before volume grows", "deletion": "prunable by future explicit policy", "migration_risk": "low", "usage": "audit only; not a completion gate"},
    "outcome_reviews": {"concept": "LegacyOutcomeDecision", "classification": LEGACY_COMPATIBILITY, "recommendation": "Encapsulate", "source_of_truth": "legacy outcome/cancellation decision", "derived_from": "AgentReport and evidence_check", "retention": "read existing rows indefinitely", "deletion": "Remove Later after cancellation projection migration", "migration_risk": "high", "usage": "active compatibility write/read for cancellation; TaskCompletion is the current positive completion fact"},
    "quality_reviews": {"concept": "QualityAnnotation", "classification": HUMAN_ANNOTATION, "recommendation": "Encapsulate", "source_of_truth": "scored review annotation", "derived_from": "reviewer scores", "retention": "retain with Task while quality CLI is supported", "deletion": "Remove Later only after CLI compatibility period", "migration_risk": "medium", "usage": "active quality CLI data; distinct from snapshot-bound ReviewResult and not a completion gate"},
    "review_requests": {"concept": "ReviewRequest", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "snapshot-bound request for review", "derived_from": "Task review transition", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "high", "usage": "review lifecycle"},
    "review_attempts": {"concept": "ReviewAttempt", "classification": OPERATIONAL_TRANSPORT, "recommendation": "Encapsulate", "source_of_truth": "delivery attempt state", "derived_from": "ReviewRequest dispatch", "retention": "bounded transport retention recommended after request closure", "deletion": "prunable only after audit policy", "migration_risk": "medium", "usage": "lease, retry, worker and adapter diagnostics"},
    "review_reviewers": {"concept": "ReviewerRegistryEntry", "classification": CONFIGURATION_REGISTRY, "recommendation": "Encapsulate", "source_of_truth": "runtime reviewer registry", "derived_from": "reviewer registration and heartbeat", "retention": "while reviewer is configured", "deletion": "explicit deregistration", "migration_risk": "medium", "usage": "capability, concurrency and heartbeat lookup"},
    "review_results": {"concept": "ReviewResult", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "reviewer verdict for a snapshot", "derived_from": "ReviewRequest response", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "high", "usage": "review evidence; stale when snapshot differs"},
    "review_dispatches": {"concept": "ReviewDispatchLog", "classification": OPERATIONAL_TRANSPORT, "recommendation": "Encapsulate", "source_of_truth": "legacy adapter process log", "derived_from": "review dispatcher execution", "retention": "bounded transport retention recommended", "deletion": "Remove Later after adapter compatibility period", "migration_risk": "medium", "usage": "active legacy-adapter diagnostics; review_attempts is the lifecycle transport model"},
    "review_findings": {"concept": "ReviewFinding", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "concrete reviewer finding", "derived_from": "ReviewResult", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "high", "usage": "blocking and non-blocking review findings"},
    "review_finding_updates": {"concept": "ReviewFindingUpdate", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "finding status transition", "derived_from": "human or agent resolution decision", "retention": "with finding", "deletion": "never during normal operation", "migration_risk": "high", "usage": "append-only resolution audit"},
    "quality_score_schemas": {"concept": "QualityScoreSchema", "classification": CONFIGURATION_REGISTRY, "recommendation": "Encapsulate", "source_of_truth": "project score configuration", "derived_from": "operator configuration", "retention": "while quality scoring is supported", "deletion": "Remove Later only with quality_reviews compatibility removal", "migration_risk": "low", "usage": "active scoring CLI configuration; not a completion gate"},
    "understanding_checks": {"concept": "UnderstandingDecision", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "recorded understanding decision", "derived_from": "Task precondition review", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "medium", "usage": "instruction readiness transition"},
    "task_completions": {"concept": "TaskCompletion", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "actor acceptance of a snapshot", "derived_from": "human/AI decision citing evidence", "retention": "indefinite audit history", "deletion": "invalidate, do not delete", "migration_risk": "high", "usage": "completion acceptance and invalidation history"},
    "recipe_task_provenance": {"concept": "RecipeTaskProvenance", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "recipe snapshot used to create Task", "derived_from": "resolved recipe", "retention": "with Task", "deletion": "never during normal operation", "migration_risk": "medium", "usage": "reproducible recipe-derived intake"},
    "recipe_runs": {"concept": "RecipeRun", "classification": OPERATIONAL_TRANSPORT, "recommendation": "Encapsulate", "source_of_truth": "active recipe execution cursor", "derived_from": "recipe workflow operations", "retention": "retain completed runs for audit; pruning policy TBD", "deletion": "never while active", "migration_risk": "high", "usage": "step cursor and pending public operations"},
    "roadmap_commitments": {"concept": "RoadmapCommitment", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "accepted plan commitment", "derived_from": "accepted RoadmapRevision", "retention": "indefinite audit history", "deletion": "close, do not delete", "migration_risk": "high", "usage": "multi-task scope and acceptance"},
    "roadmap_revisions": {"concept": "RoadmapRevision", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "proposed/decided roadmap text", "derived_from": "roadmap discussion or import", "retention": "indefinite audit history", "deletion": "reject/supersede, do not delete", "migration_risk": "medium", "usage": "plan proposal and human decision"},
    "todos": {"concept": "Todo", "classification": PRIMARY_FACT, "recommendation": "Encapsulate", "source_of_truth": "intake candidate or deferred work", "derived_from": "human request or discovered item", "retention": "until converted, superseded or explicitly removed", "deletion": "prefer status transition", "migration_risk": "medium", "usage": "non-authorizing intake queue"},
    "overdrive_runs": {"concept": "OverdriveRun", "classification": OPERATIONAL_TRANSPORT, "recommendation": "Encapsulate", "source_of_truth": "autonomous execution cursor", "derived_from": "Overdrive command", "retention": "retain completed runs for audit; pruning policy TBD", "deletion": "never while active", "migration_risk": "medium", "usage": "scope, cursor and failure budget"},
    "overdrive_events": {"concept": "OverdriveEvent", "classification": OPERATIONAL_TRANSPORT, "recommendation": "Keep", "source_of_truth": "Overdrive execution log", "derived_from": "OverdriveRun activity", "retention": "with OverdriveRun", "deletion": "prunable only with its run", "migration_risk": "low", "usage": "resume and operator diagnostics"},
    "transition_events": {"concept": "TransitionEvent", "classification": PRIMARY_FACT, "recommendation": "Keep", "source_of_truth": "state-change audit event", "derived_from": "domain transition", "retention": "indefinite audit history", "deletion": "append-only; never during normal operation", "migration_risk": "high", "usage": "audit and optimistic concurrency context"},
}


JSON_FIELD_SCHEMAS: Final[dict[str, dict[str, str]]] = {
    "projects": {"tech_stack": "list[str]", "rules": "list[str]", "default_completion_criteria": "list[str]", "available_models": "list[str]", "fallback_models": "list[str]"},
    "tasks": {"acceptance_criteria": "list[str]", "base_snapshot": "SnapshotRef object"},
    "instructions": {"applied_rule_ids": "list[str]"},
    "agent_reports": {"changed_files": "list[str]"},
    "evidence_checks": {"issues": "list[str]", "metadata": "object"},
    "verification_runs": {"observed_paths": "list[str]", "metadata": "object; mode and runner-specific details"},
    "failure_logs": {"snapshot": "SnapshotRef object", "context": "redacted diagnostic object"},
    "model_profiles": {"capabilities": "list[str]"},
    "outcome_reviews": {"concerns": "list[str]"},
    "quality_reviews": {"scores": "object[str, number]", "issues": "list[str]"},
    "review_requests": {"based_on_snapshot": "SnapshotRef object"},
    "review_attempts": {"based_on_snapshot": "SnapshotRef object", "diagnostics": "transport diagnostic object"},
    "review_reviewers": {"capabilities": "list[str]", "metadata": "registry-specific object"},
    "review_results": {"based_on_snapshot": "SnapshotRef object"},
    "review_dispatches": {"args": "list[str]"},
    "quality_score_schemas": {"required_scores": "list[str]"},
    "task_completions": {"completed_snapshot": "SnapshotRef object", "accepted_verification_run_ids": "list[str]", "accepted_review_result_ids": "list[str]"},
    "recipe_task_provenance": {"rendered_fields": "object", "recipe_snapshot": "recipe definition object"},
    "recipe_runs": {"completed_steps": "list[str]", "pending_steps": "list[str]", "pending_public_operations": "list[str]", "metadata": "recipe-run object"},
    "roadmap_commitments": {"success_criteria": "list[str]", "non_goals": "list[str]", "autonomy_scope": "object or list", "review_gates": "list or object", "evidence_policy": "object"},
    "overdrive_runs": {"summary_json": "run summary object"},
    "overdrive_events": {"metadata": "event-specific object"},
    "transition_events": {"related_ids": "list[str]", "snapshot": "SnapshotRef object", "warnings": "list[str]"},
}


OPTIONAL_LEGACY_JSON_FIELDS: Final[dict[str, dict[str, str]]] = {
    "instructions": {
        "applied_failure_pattern_ids": "list[str]; decoded when present in a legacy DB, not created by current schema",
    },
}
