from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import Store


REVIEWER_HEARTBEAT_TTL_SECONDS = 300
REVIEWER_BACKEND_KINDS = {"claude_code", "codex", "local_llm", "openai_compatible", "human", "other"}
REVIEWER_CAPABILITIES = {"review_diff", "review_docs", "summarize", "propose_tests", "implement", "verify"}
LEGACY_CAPABILITY_ALIASES = {"review": "review_diff"}

REVIEWER_ALIASES = {
    "claude": "claude-code",
    "claudecode": "claude-code",
    "claudeai": "claude-code",
}

CLAUDE_CODE_REGISTER_REVIEWER_JSON = {
    "reviewer": "claude-code",
    "capabilities": ["review"],
    "max_concurrent": 1,
    "metadata": {
        "worker_path": "claude-code-mcp-session",
        "dispatch_capable": True,
        "source": "real Claude Code session",
    },
}

CODEX_REGISTER_REVIEWER_JSON = {
    "reviewer": "codex",
    "capabilities": ["review"],
    "max_concurrent": 1,
    "metadata": {
        "worker_path": "codex-mcp-session",
        "dispatch_capable": True,
        "source": "real Codex session",
    },
}

CLAUDE_CODE_STALE_NEXT_ACTION = (
    "claude-code reviewer is stale. "
    "Open the Claude Code session connected to the Nilo MCP server and call register_reviewer "
    "to refresh heartbeat before claiming reviews."
)

CLAUDE_CODE_HEARTBEAT_ONLY_NEXT_ACTION = (
    "claude-code reviewer is heartbeat_only. "
    "Open the Claude Code session connected to the Nilo MCP server and call register_reviewer "
    "with dispatch_capable=true before claiming reviews."
)


class ReviewerResolutionError(ValueError):
    def __init__(self, message: str, next_action: str) -> None:
        super().__init__(message)
        self.next_action = next_action


@dataclass(frozen=True)
class ReviewerResolution:
    requested: str
    reviewer: str
    registration: dict | None
    matched_by: str


def normalize_backend_kind(value: str | None, reviewer: str = "") -> str:
    value = (value or "").strip().lower().replace("-", "_")
    if value in REVIEWER_BACKEND_KINDS:
        return value
    if reviewer == "claude-code":
        return "claude_code"
    if reviewer == "codex":
        return "codex"
    if reviewer == "human":
        return "human"
    return "other"


def normalize_capability(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    return LEGACY_CAPABILITY_ALIASES.get(normalized, normalized)


def normalize_capabilities(values: list[str]) -> list[str]:
    normalized = []
    for value in values:
        capability = normalize_capability(value)
        if capability in REVIEWER_CAPABILITIES and capability not in normalized:
            normalized.append(capability)
    return normalized


def reviewer_identity(row: dict | None, requested: str = "") -> dict:
    reviewer = (row or {}).get("reviewer") or requested
    metadata = (row or {}).get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    stored_capabilities = (row or {}).get("capabilities") or metadata.get("capabilities") or []
    if not isinstance(stored_capabilities, list):
        stored_capabilities = []
    capabilities = normalize_capabilities([str(item) for item in stored_capabilities])
    if not capabilities and row is not None:
        capabilities = ["review_diff"]
    return {
        "reviewer_id": reviewer,
        "display_name": str(metadata.get("display_name") or reviewer),
        "backend_kind": normalize_backend_kind(str(metadata.get("backend_kind") or ""), reviewer),
        "capabilities": capabilities,
        "context_limits": metadata.get("context_limits") or {},
        "tool_access_limitations": metadata.get("tool_access_limitations") or [],
        "evidence_requirements": metadata.get("evidence_requirements")
        or [
            "command output",
            "tests",
            "diff inspection",
            "explicit human or trusted reviewer approval when required",
        ],
    }


def reviewer_supports_capability(row: dict, capability: str) -> bool:
    wanted = normalize_capability(capability)
    capabilities = reviewer_identity(row)["capabilities"]
    return wanted in capabilities


def reviewer_heartbeat_age_seconds(row: dict) -> float:
    try:
        parsed = datetime.fromisoformat(row["last_heartbeat_at"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()


def reviewer_is_fresh(row: dict, heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS) -> bool:
    return row.get("status") == "available" and reviewer_heartbeat_age_seconds(row) <= heartbeat_ttl_seconds


def reviewer_is_dispatch_capable(row: dict) -> bool:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    if metadata.get("startup_path") == "nilo mcp reviewer-start":
        return False
    if "dispatch_capable" in metadata:
        return metadata.get("dispatch_capable") is True
    return True


def reviewer_evidence_profile(row: dict) -> str:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if not reviewer_is_dispatch_capable(row):
        return "heartbeat_only"
    if (
        row.get("reviewer") == "claude-code"
        and metadata.get("worker_path") == "claude-code-mcp-session"
        and metadata.get("dispatch_capable") is True
    ):
        return "claude_code_mcp_session"
    if (
        row.get("reviewer") == "codex"
        and metadata.get("worker_path") == "codex-mcp-session"
        and metadata.get("dispatch_capable") is True
    ):
        return "codex_mcp_session"
    if metadata.get("worker_path") == "nilo mcp reviewer-worker":
        return "synthetic_result_file_worker"
    if metadata.get("worker_path") == "nilo mcp reviewer-claim":
        return "manual_claim_worker"
    return "generic_mcp_worker"


def reviewer_is_claude_code_e2e_capable(row: dict) -> bool:
    return reviewer_evidence_profile(row) == "claude_code_mcp_session"


def reviewer_is_registered_available(
    store: Store,
    reviewer: str,
    heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS,
) -> bool:
    return any(
        reviewer_is_fresh(row, heartbeat_ttl_seconds) and reviewer_is_dispatch_capable(row) and reviewer_supports_capability(row, "review_diff")
        for row in store.list_where("review_reviewers", "reviewer=? AND status='available'", (reviewer,))
    )


def latest_reviewer_row(store: Store, reviewer: str) -> dict | None:
    rows = store.list_where("review_reviewers", "reviewer=?", (reviewer,))
    if not rows:
        return None
    return max(rows, key=lambda row: row["last_heartbeat_at"])


def reviewer_availability(row: dict | None, heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS) -> str:
    if row is None:
        return "missing"
    if row.get("status") == "pending_approval":
        return "pending_approval"
    if not reviewer_is_fresh(row, heartbeat_ttl_seconds):
        return "stale"
    if not reviewer_is_dispatch_capable(row):
        return "heartbeat_only"
    return "available"


def claude_code_prompt() -> str:
    return (
        "Open the Claude Code session connected to the Nilo MCP server and run:\n"
        "1. register_reviewer with the provided JSON to refresh heartbeat\n"
        "2. claim_next_review\n"
        "3. Generate ReviewResult\n"
        "4. import_review_result"
    )


def codex_prompt() -> str:
    return (
        "Open the Codex session connected to the Nilo MCP server and run:\n"
        "1. register_reviewer with the provided JSON to refresh heartbeat\n"
        "2. claim_next_review\n"
        "3. Generate ReviewResult\n"
        "4. import_review_result"
    )


def reviewer_prepare_status(
    store: Store,
    reviewer: str,
    heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS,
) -> dict:
    canonical = canonical_reviewer_name(reviewer)
    row = latest_reviewer_row(store, canonical)
    availability = reviewer_availability(row, heartbeat_ttl_seconds)
    dispatch_capable = reviewer_is_dispatch_capable(row) if row else False
    ready = availability == "available" and dispatch_capable
    reason = availability
    next_action = ""
    register_json = None
    prompt = ""
    if canonical == "claude-code":
        register_json = CLAUDE_CODE_REGISTER_REVIEWER_JSON
        prompt = claude_code_prompt()
        if ready:
            next_action = "claude-code reviewer is ready; call claim_next_review before reviewing."
        elif availability == "stale":
            next_action = CLAUDE_CODE_STALE_NEXT_ACTION
        elif availability == "heartbeat_only":
            next_action = CLAUDE_CODE_HEARTBEAT_ONLY_NEXT_ACTION
        elif availability == "pending_approval":
            next_action = "claude-code reviewer is pending_approval; approve the Claude Code MCP session before claiming reviews."
        elif availability == "missing":
            reason = "mcp_server_not_connected"
            next_action = (
                "claude-code reviewer is not registered. "
                "Open the Claude Code session connected to the Nilo MCP server and call register_reviewer "
                "before claiming reviews."
            )
        else:
            next_action = (
                "claude-code reviewer is unavailable. "
                "Open the Claude Code session connected to the Nilo MCP server and call register_reviewer."
            )
    elif canonical == "codex":
        register_json = CODEX_REGISTER_REVIEWER_JSON
        prompt = codex_prompt()
        if ready:
            next_action = "codex reviewer is ready; call claim_next_review before reviewing."
        elif availability == "stale":
            next_action = "codex reviewer is stale. Open the Codex session connected to the Nilo MCP server and call register_reviewer to refresh heartbeat before claiming reviews."
        elif availability == "heartbeat_only":
            next_action = "codex reviewer is heartbeat_only. Open the Codex session connected to the Nilo MCP server and call register_reviewer with dispatch_capable=true before claiming reviews."
        elif availability == "pending_approval":
            next_action = "codex reviewer is pending_approval; approve the Codex MCP session before claiming reviews."
        elif availability == "missing":
            reason = "mcp_server_not_connected"
            next_action = (
                "codex reviewer is not registered. "
                "Open the Codex session connected to the Nilo MCP server and call register_reviewer "
                "before claiming reviews."
            )
        else:
            next_action = (
                "codex reviewer is unavailable. "
                "Open the Codex session connected to the Nilo MCP server and call register_reviewer."
            )
    else:
        if ready:
            next_action = f"{canonical} reviewer is ready; call claim_next_review before reviewing."
        elif availability == "pending_approval":
            next_action = f"{canonical} reviewer is pending_approval; approve the MCP session before claiming reviews."
        elif availability == "heartbeat_only":
            next_action = f"{canonical} reviewer is heartbeat_only; start a real MCP reviewer worker before claiming reviews."
        elif availability == "stale":
            next_action = f"{canonical} reviewer is stale; refresh reviewer heartbeat before claiming reviews."
        else:
            next_action = f"{canonical} reviewer is unavailable; register a real MCP reviewer worker before claiming reviews."
    return {
        **reviewer_identity(row, canonical),
        "reviewer": canonical,
        "requested_reviewer": reviewer,
        "ready": ready,
        "reason": reason,
        "availability": availability,
        "dispatch_capable": dispatch_capable,
        "evidence_profile": reviewer_evidence_profile(row) if row else "missing",
        "heartbeat_age_seconds": reviewer_heartbeat_age_seconds(row) if row else None,
        "last_heartbeat_at": row["last_heartbeat_at"] if row else None,
        "metadata": row["metadata"] if row else None,
        "next_action": next_action,
        "claude_code_prompt": prompt,
        "register_reviewer_json": register_json,
    }


def reviewer_unavailable_next_action(store: Store, requested: str) -> str:
    status = reviewer_prepare_status(store, requested)
    if status["reviewer"] == "claude-code":
        suffix = f" register_reviewer_json: {json.dumps(CLAUDE_CODE_REGISTER_REVIEWER_JSON, ensure_ascii=False)}"
        return status["next_action"] + suffix
    return status["next_action"]


def active_reviewer_rows(store: Store, heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS) -> list[dict]:
    rows = store.list_where("review_reviewers", "status='available'")
    freshest_by_reviewer: dict[str, dict] = {}
    for row in rows:
        reviewer = row["reviewer"]
        current = freshest_by_reviewer.get(reviewer)
        if current is None or row["last_heartbeat_at"] > current["last_heartbeat_at"]:
            freshest_by_reviewer[reviewer] = row
    return [
        row
        for row in freshest_by_reviewer.values()
        if reviewer_is_fresh(row, heartbeat_ttl_seconds)
        and reviewer_is_dispatch_capable(row)
        and reviewer_supports_capability(row, "review_diff")
    ]


def normalize_reviewer_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def canonical_reviewer_name(requested: str) -> str:
    normalized = normalize_reviewer_name(requested.strip())
    return REVIEWER_ALIASES.get(normalized, requested.strip())


def resolve_reviewer(
    store: Store,
    requested: str,
    heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS,
) -> ReviewerResolution:
    requested = requested.strip()
    if not requested:
        raise ReviewerResolutionError("reviewer is empty", "start a real MCP reviewer worker, then retry with --to <reviewer>")
    active = active_reviewer_rows(store, heartbeat_ttl_seconds)
    if not active:
        raise ReviewerResolutionError(
            f"reviewer is not registered or available: {requested}",
            f"start a real MCP reviewer worker for {requested}; reviewer-start only records heartbeat",
        )

    by_exact = {row["reviewer"]: row for row in active}
    if requested in by_exact:
        return ReviewerResolution(requested=requested, reviewer=requested, registration=by_exact[requested], matched_by="exact")

    normalized = normalize_reviewer_name(requested)
    aliased = REVIEWER_ALIASES.get(normalized)
    if aliased and aliased in by_exact:
        return ReviewerResolution(requested=requested, reviewer=aliased, registration=by_exact[aliased], matched_by="alias")

    matches = [
        row
        for row in active
        if normalized
        and (
            normalize_reviewer_name(row["reviewer"]).startswith(normalized)
            or normalized in normalize_reviewer_name(row["reviewer"])
            or normalize_reviewer_name(row["reviewer"]) in normalized
        )
    ]
    unique = {row["reviewer"]: row for row in matches}
    if len(unique) == 1:
        row = next(iter(unique.values()))
        return ReviewerResolution(requested=requested, reviewer=row["reviewer"], registration=row, matched_by="natural")
    if len(unique) > 1:
        choices = ", ".join(sorted(unique))
        raise ReviewerResolutionError(
            f"ambiguous reviewer: {requested}; candidates: {choices}",
            f"retry with one exact reviewer id: {choices}",
        )

    available = ", ".join(sorted(row["reviewer"] for row in active))
    raise ReviewerResolutionError(
        f"reviewer is not registered or available: {requested}",
        f"retry with an available reviewer id ({available}) or start a real MCP reviewer worker for {requested}",
    )


def resolve_review_request_target(
    store: Store,
    requested: str,
    heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS,
) -> ReviewerResolution:
    try:
        return resolve_reviewer(store, requested, heartbeat_ttl_seconds)
    except ReviewerResolutionError:
        reviewer = canonical_reviewer_name(requested)
        if not reviewer:
            raise ReviewerResolutionError("reviewer is empty", "start a real MCP reviewer worker, then retry with --to <reviewer>") from None
        return ReviewerResolution(requested=requested.strip(), reviewer=reviewer, registration=None, matched_by="unavailable")


def resolve_known_review_request_target(
    store: Store,
    requested: str,
    heartbeat_ttl_seconds: int = REVIEWER_HEARTBEAT_TTL_SECONDS,
) -> ReviewerResolution:
    requested = requested.strip()
    if not requested:
        raise ReviewerResolutionError("reviewer is empty", "start a real MCP reviewer worker, then retry with --to <reviewer>")
    try:
        return resolve_reviewer(store, requested, heartbeat_ttl_seconds)
    except ReviewerResolutionError:
        canonical = canonical_reviewer_name(requested)
        row = latest_reviewer_row(store, canonical)
        if row:
            return ReviewerResolution(requested=requested, reviewer=canonical, registration=row, matched_by="known_unavailable")
        if canonical == "claude-code":
            return ReviewerResolution(requested=requested, reviewer=canonical, registration=None, matched_by="supported_unavailable")
        raise ReviewerResolutionError(
            f"reviewer is not registered or supported: {requested}",
            f"retry with a known reviewer id or register a real MCP reviewer worker for {requested}",
        ) from None
