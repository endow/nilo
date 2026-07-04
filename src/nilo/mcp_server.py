from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .agent_report_import import import_agent_report
from .ai_context import project_ai_context
from .cli_support import make_id
from .human_status import human_next_action_text, human_task_status
from .mcp_identity import identity_matches_expected, mcp_identity, repository_mismatch_response
from .project_boundary import (
    ProjectBoundaryError,
    boundary_warning_lines,
    record_nilo_issue_for_task,
    require_write_fence,
    resolve_project_boundary,
)
from .project_status import ProjectNotFoundError, build_project_status, project_status_view
from .project_language import (
    human_readable_language_issues,
    human_readable_language_policy,
    project_primary_language,
    render_roadmap_proposal_from_todo,
    roadmap_proposal_texts,
)
from .review import VALID_FINDING_STATUSES, build_review_context, build_review_result_template, parse_review_result
from .review_dispatcher import DispatchError, dispatch_review
from .review_lifecycle import insert_review_request, set_review_request_status, update_review_request
from .reviewer_registry import (
    ReviewerResolutionError,
    canonical_reviewer_name,
    normalize_capabilities,
    reviewer_prepare_status,
    reviewer_availability,
    reviewer_evidence_profile,
    reviewer_heartbeat_age_seconds,
    reviewer_identity,
    reviewer_unavailable_next_action,
    resolve_reviewer,
    resolve_known_review_request_target,
    resolve_review_request_target,
    reviewer_is_claude_code_e2e_capable,
    reviewer_is_dispatch_capable,
    reviewer_is_fresh,
    reviewer_is_registered_available,
)
from .secret import detect_secret_issues, mask_secrets
from .snapshot import compact_snapshot, current_git_snapshot, snapshot_columns
from .store import Store
from .task_logic import projected_task_status
from .timeutil import iso_age_seconds, now_iso
from .transitions import (
    TransitionError,
    create_task_from_todo as transition_create_task_from_todo,
    import_review_result as transition_import_review_result,
    promote_todo_to_roadmap_proposal as transition_promote_todo_to_roadmap_proposal,
    record_verification_run as transition_record_verification_run,
    triage_todo as transition_triage_todo,
    update_review_finding as transition_update_review_finding,
)
from .workspace_resolver import WorkspaceResolutionError, list_workspace_entries, resolve_workspace_context


PROTOCOL_VERSION = "2024-11-05"

MCP_WORKSPACE_ENV_VARS = (
    "NILO_WORKSPACE_ROOT",
    "NILO_PROJECT_ROOT",
    "CODEX_WORKSPACE_ROOT",
    "CODEX_CWD",
    "WORKSPACE_ROOT",
    "REPO_ROOT",
    "PROJECT_ROOT",
    "INIT_CWD",
    "PWD",
)


def text_tool_result(data: Any, is_error: bool = False) -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(data, ensure_ascii=False, indent=2),
            }
        ],
        "isError": is_error,
    }


def json_schema(properties: dict[str, dict], required: list[str]) -> dict:
    # Reserved across all MCP tools because workspace routing is handled before
    # tool-specific argument dispatch.
    properties = {
        **properties,
        "project_root": {
            "type": "string",
            "description": "Optional repository root. When provided, Nilo uses <project_root>/.nilo/nilo.db instead of the MCP server cwd database.",
        },
        "workspace": {
            "type": "string",
            "description": "Optional registered workspace name. When provided, Nilo resolves the workspace root and uses <root>/.nilo/nilo.db.",
        },
        "expected_project": {
            "type": "string",
            "description": "Optional repository guard. This is the expected repository/project identity, usually the repository directory name. If set, reject the call unless MCP identity project_id or repository_name matches.",
        },
        "expected_git_root": {
            "type": "string",
            "description": "Optional repository guard. If set, reject the call unless the MCP identity git root matches.",
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


HEADROOM_TOOL_METADATA = [
    {
        "tool": "nilo_import_review_result",
        "compressible": False,
        "reason": "primary evidence / write payload",
    },
    {
        "tool": "nilo_get_test_log",
        "compressible": True,
        "reason": "large diagnostic output; raw artifact is stored separately",
    },
]


def headroom_tool_metadata(tool_name: str) -> dict | None:
    for metadata in HEADROOM_TOOL_METADATA:
        if metadata["tool"] == tool_name:
            return dict(metadata)
    return None


def _candidate_workspace_roots() -> list[Path]:
    roots: list[Path] = []
    for name in MCP_WORKSPACE_ENV_VARS:
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    cwd = Path.cwd()
    roots.extend([cwd, *cwd.parents])
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        marker = str(root.expanduser())
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(root)
    return deduped


def _mcp_context_filters(tool_name: str, arguments: dict) -> dict[str, str]:
    filters: dict[str, str] = {}
    project_id = arguments.get("project_id")
    if isinstance(project_id, str) and project_id:
        filters["project_id"] = project_id
    task_id = arguments.get("task_id")
    if isinstance(task_id, str) and task_id:
        filters["task_id"] = task_id
    if tool_name in {"get_review_prompt", "get_review_template", "import_review_result"}:
        review_id = arguments.get("review_id")
        if isinstance(review_id, str) and review_id:
            filters["review_id"] = review_id
    return filters


def _db_contains_context(candidate: Path, filters: dict[str, str]) -> bool:
    if not filters:
        return True
    uri = f"file:{candidate.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            if "project_id" in filters:
                row = conn.execute("SELECT 1 FROM projects WHERE id=? LIMIT 1", (filters["project_id"],)).fetchone()
                if row is None:
                    return False
            if "task_id" in filters:
                args: list[str] = [filters["task_id"]]
                where = "id=?"
                if "project_id" in filters:
                    where += " AND project_id=?"
                    args.append(filters["project_id"])
                row = conn.execute(f"SELECT 1 FROM tasks WHERE {where} LIMIT 1", tuple(args)).fetchone()
                if row is None:
                    return False
            if "review_id" in filters:
                row = conn.execute("SELECT 1 FROM review_requests WHERE id=? LIMIT 1", (filters["review_id"],)).fetchone()
                if row is None:
                    return False
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def resolve_mcp_db_path(db_path: Path | None, tool_name: str, arguments: dict) -> Path:
    env_db = os.environ.get("NILO_DB")
    if env_db:
        return Path(env_db)
    filters = _mcp_context_filters(tool_name, arguments)
    checked: list[str] = []
    existing: list[str] = []
    for root in _candidate_workspace_roots():
        candidate = root.expanduser() / ".nilo" / "nilo.db"
        checked.append(str(candidate))
        if not candidate.exists():
            continue
        existing.append(str(candidate))
        if _db_contains_context(candidate, filters):
            return candidate
    if existing and filters:
        filter_text = ", ".join(f"{key}={value}" for key, value in filters.items())
        raise McpToolError(
            "Nilo MCP found project database candidates, but none matched the requested context. "
            f"context={filter_text}; cwd={Path.cwd()}; candidates={existing}. "
            "Start the MCP server with --db <repo>/.nilo/nilo.db, set NILO_DB, "
            "or set NILO_WORKSPACE_ROOT to the target repository."
        )
    raise McpToolError(
        "Nilo MCP could not resolve a project database. "
        f"cwd={Path.cwd()}; checked={checked}. "
        "Start the MCP server with --db <repo>/.nilo/nilo.db, set NILO_DB, "
        "or set NILO_WORKSPACE_ROOT to the target repository."
    )


def resolve_mcp_workspace_context(db_path: Path | None, tool_name: str, arguments: dict) -> dict[str, str]:
    project_root = arguments.get("project_root")
    if project_root is not None and not isinstance(project_root, str):
        raise McpToolError("argument must be a string: project_root")
    workspace = arguments.get("workspace")
    if workspace is not None and not isinstance(workspace, str):
        raise McpToolError("argument must be a string: workspace")
    if project_root or workspace or db_path is not None:
        return resolve_workspace_context(
            project_root=project_root or None,
            workspace=workspace or None,
            db_path=db_path,
            default_cwd=Path.cwd(),
        )
    resolved_db = resolve_mcp_db_path(db_path, tool_name, arguments)
    context = resolve_workspace_context(db_path=resolved_db, default_cwd=Path.cwd())
    context["source"] = "nilo_db" if os.environ.get("NILO_DB") else "server_cwd"
    context["db_path"] = str(resolved_db.resolve())
    return context


def project_not_found_error(store: Store, project_id: str) -> McpToolError:
    return McpToolError(
        f"project not found: {project_id}; "
        f"Nilo MCP is using db={store.path} cwd={Path.cwd()}. "
        "If this is not the target repository database, restart MCP with "
        "--db <repo>/.nilo/nilo.db or set NILO_DB/NILO_WORKSPACE_ROOT."
    )


TOOLS = [
    {
        "name": "get_status",
        "description": "Return compact AI status for a project.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "record_verification",
        "description": "Record verification; pass context_token or last_seen_event_id.",
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "context_token": {
                    "type": "string",
                    "description": "Task write_context_token from get_status or get_task_status.",
                },
                "last_seen_event_id": {
                    "type": "string",
                    "description": "Latest task event id from get_status or get_task_status.",
                },
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": ["integer", "null"]},
                "timed_out": {"type": "boolean"},
                "timeout_seconds": {"type": "number"},
                "mode": {"type": "string", "enum": ["quick", "targeted", "full"], "description": "Verification scope."},
                "git_head": {"type": ["string", "null"]},
                "git_diff_hash": {"type": "string"},
                "working_tree_dirty": {"type": "boolean"},
                "git_status_porcelain": {"type": "string"},
                "observed_paths": {"type": "array", "items": {"type": "string"}},
                "metadata": {"type": "object"},
                "started_at": {"type": "string"},
                "finished_at": {"type": "string"},
            },
            ["task_id", "command", "cwd", "stdout", "stderr", "exit_code", "timed_out"],
        ),
    },
    {
        "name": "get_agent_work_context",
        "description": "Return the agent-oriented project work context, safe next step, human gates, and task write context tokens.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "get_next_step",
        "description": "Return the single recommended agent step and whether it requires explicit human intent.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "mcp_doctor",
        "description": "Diagnose the MCP tool surface and project readability without changing state.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "mcp_ping",
        "description": "Return MCP server liveness and repository identity without reading project state.",
        "inputSchema": json_schema({}, []),
    },
    {
        "name": "prepare_reviewer",
        "description": "Check reviewer readiness and return the next action needed before review dispatch.",
        "inputSchema": json_schema(
            {"project_id": {"type": "string"}, "reviewer": {"type": "string"}},
            ["project_id", "reviewer"],
        ),
    },
    {
        "name": "submit_agent_report",
        "description": "Wrapper for importing an agent report and returning refreshed task context.",
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "body_md": {"type": "string"},
                "agent": {"type": "string"},
                "context_token": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
            },
            ["task_id", "body_md", "agent"],
        ),
    },
    {
        "name": "record_test_result",
        "description": "Record an external verification log and return refreshed task context.",
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "context_token": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": ["integer", "null"]},
                "timed_out": {"type": "boolean"},
                "timeout_seconds": {"type": "number"},
                "mode": {"type": "string", "enum": ["quick", "targeted", "full"], "description": "Verification scope."},
                "git_head": {"type": ["string", "null"]},
                "git_diff_hash": {"type": "string"},
                "working_tree_dirty": {"type": "boolean"},
                "git_status_porcelain": {"type": "string"},
                "observed_paths": {"type": "array", "items": {"type": "string"}},
                "metadata": {"type": "object"},
                "started_at": {"type": "string"},
                "finished_at": {"type": "string"},
            },
            ["task_id", "command", "cwd", "stdout", "stderr", "exit_code", "timed_out"],
        ),
    },
    {
        "name": "request_task_review",
        "description": "Low-level: only create request; use dispatch_review for AI review.",
        "metadata": {
            "api_level": "low_level",
            "recommended_for": "manual review handoff only",
            "prefer_for_ai_review": "dispatch_review",
        },
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "requester": {"type": "string"},
                "reviewer": {"type": "string"},
                "reason": {"type": "string"},
                "context_token": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
            },
            ["task_id", "requester", "reviewer", "reason"],
        ),
    },
    {
        "name": "dispatch_review",
        "description": "High-level MCP AI review; CLI fallback requires opt-in.",
        "metadata": {
            "api_level": "high_level",
            "recommended_for": "normal AI-to-AI review",
            "workflow": "mcp request, start, claim, run, import, confirm",
            "cli_fallback": "disabled unless allow_cli_fallback=true",
        },
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "project_id": {"type": "string"},
                "actor": {"type": "string"},
                "reviewer": {"type": "string"},
                "reason": {"type": "string"},
                "auto_start": {"type": "boolean"},
                "auto_configure": {"type": "boolean"},
                "allow_cli_fallback": {"type": "boolean"},
                "config_path": {"type": "string"},
            },
            ["task_id", "actor", "reviewer"],
        ),
    },
    {
        "name": "register_reviewer",
        "description": "Register or heartbeat an MCP reviewer worker.",
        "inputSchema": json_schema(
            {
                "reviewer": {"type": "string"},
                "capabilities": {"type": "array", "items": {"type": "string"}},
                "max_concurrent": {"type": "integer"},
                "metadata": {"type": "object"},
            },
            ["reviewer"],
        ),
    },
    {
        "name": "claim_next_review",
        "description": "Claim the next pending review for a registered MCP reviewer.",
        "inputSchema": json_schema(
            {
                "reviewer": {"type": "string"},
                "project_id": {"type": "string"},
            },
            ["reviewer"],
        ),
    },
    {
        "name": "mark_stale_review_requests",
        "description": "Mark expired claimed review requests stale.",
        "inputSchema": json_schema(
            {
                "reviewer": {"type": "string"},
                "stale_after_seconds": {"type": "integer"},
            },
            [],
        ),
    },
    {
        "name": "get_project_status",
        "description": "Return the current Nilo project status, next actions, active tasks, and roadmap agent state.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "get_project_summary",
        "description": "Return the full read-only Nilo project summary.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "get_roadmap_status",
        "description": "Return accepted commitments, pending revisions, roadmap agent state, and assessments.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "discuss_roadmap",
        "description": "Return roadmap discussion context markdown without changing project state.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "get_task_status",
        "description": "Return read-only task status and latest task events.",
        "inputSchema": json_schema({"task_id": {"type": "string"}}, ["task_id"]),
    },
    {
        "name": "get_instruction",
        "description": "Return the latest existing instruction for a task without generating a new instruction.",
        "inputSchema": json_schema({"task_id": {"type": "string"}}, ["task_id"]),
    },
    {
        "name": "get_review_status",
        "description": "Return review requests, results, and findings for a task.",
        "inputSchema": json_schema({"task_id": {"type": "string"}}, ["task_id"]),
    },
    {
        "name": "request_review",
        "description": "Create a review request for a task.",
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "from_actor": {"type": "string"},
                "to_actor": {"type": "string"},
                "reason": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
                "context_token": {"type": "string"},
                "allow_unavailable": {"type": "boolean"},
            },
            ["task_id", "from_actor", "to_actor", "reason"],
        ),
    },
    {
        "name": "get_review_prompt",
        "description": "Return the review context markdown for an existing review request.",
        "inputSchema": json_schema(
            {"task_id": {"type": "string"}, "review_id": {"type": "string"}},
            ["task_id", "review_id"],
        ),
    },
    {
        "name": "get_review_template",
        "description": "Return the ReviewResult template markdown for an existing review request.",
        "inputSchema": json_schema({"review_id": {"type": "string"}}, ["review_id"]),
    },
    {
        "name": "import_review_result",
        "description": "Import a ReviewResult markdown body for an existing review request.",
        "metadata": headroom_tool_metadata("nilo_import_review_result"),
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "review_id": {"type": "string"},
                "body_md": {"type": "string"},
                "reviewer": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
                "context_token": {"type": "string"},
            },
            ["task_id", "review_id", "body_md", "reviewer"],
        ),
    },
    {
        "name": "update_review_finding",
        "description": "Update a review finding status and record update history.",
        "inputSchema": json_schema(
            {
                "finding_id": {"type": "string"},
                "status": {"type": "string"},
                "reason": {"type": "string"},
                "actor": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
                "context_token": {"type": "string"},
                "human_confirm": {"type": "boolean"},
                "decision_source": {"type": "string"},
            },
            ["finding_id", "status", "reason", "actor"],
        ),
    },
    {
        "name": "assess_roadmap",
        "description": "Return roadmap commitment assessment for a project.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "list_recent_history",
        "description": "Return recent projected project history.",
        "inputSchema": json_schema({"project_id": {"type": "string"}}, ["project_id"]),
    },
    {
        "name": "create_task",
        "description": (
            "Create an executable Nilo Task. Use this when the user explicitly asks to taskize work, create a Task, "
            'start a concrete implementation/design/research/documentation task, or says phrases like "タスク化して", '
            '"Taskにして", "作業タスクを作って". If the user intent is clear, infer reasonable type, risk, description, '
            "and acceptance criteria instead of creating a Todo. commitment_id is optional reference metadata, not a "
            "permission requirement. Human-readable title, description, and acceptance fields must use the project's "
            "primary language; keep commands, paths, identifiers, status, enum, and JSON field names unchanged."
        ),
        "inputSchema": json_schema(
            {
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "type": {"type": "string"},
                "risk": {"type": "string"},
                "commitment_id": {"type": "string"},
                "description": {"type": "string"},
                "acceptance": {"type": "array", "items": {"type": "string"}},
                "roadmap_item_id": {"type": "string"},
            },
            ["project_id", "title", "type", "risk", "description", "acceptance"],
        ),
    },
    {
        "name": "import_agent_report",
        "description": "Import an agent completion report through Nilo's existing evidence guard.",
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "body_md": {"type": "string"},
                "agent": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
                "context_token": {"type": "string"},
            },
            ["task_id", "body_md", "agent"],
        ),
    },
    {
        "name": "record_verification_run",
        "description": "Record an external verification log as agent_reported evidence.",
        "inputSchema": json_schema(
            {
                "task_id": {"type": "string"},
                "last_seen_event_id": {"type": "string"},
                "context_token": {"type": "string"},
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": ["integer", "null"]},
                "timed_out": {"type": "boolean"},
                "timeout_seconds": {"type": "number"},
                "mode": {"type": "string", "enum": ["quick", "targeted", "full"], "description": "Verification scope."},
                "git_head": {"type": ["string", "null"]},
                "metadata": {"type": "object"},
                "started_at": {"type": "string"},
                "finished_at": {"type": "string"},
            },
            ["task_id", "command", "cwd", "stdout", "stderr", "exit_code", "timed_out"],
        ),
    },
    {
        "name": "create_todo",
        "description": (
            "Create a Todo intake item for ideas, deferred requests, discovered issues, follow-ups, or ambiguous work. "
            "A Todo is only intake and does not grant execution permission. Do NOT use this when the user explicitly "
            'asks to taskize work, create a Task, start a concrete task, or says phrases like "タスク化して", '
            '"Taskにして", "作業タスクを作って". Human-readable title, description, and acceptance_hint fields must use '
            "the project's primary language while technical tokens stay unchanged."
        ),
        "inputSchema": json_schema(
            {
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "kind": {"type": "string"},
                "description": {"type": "string"},
                "source_task_id": {"type": "string"},
                "acceptance_hint": {"type": "string"},
                "priority": {"type": "string"},
                "source_type": {"type": "string"},
            },
            ["project_id", "title", "kind"],
        ),
    },
    {
        "name": "list_todos",
        "description": "List Todo intake items for a project, optionally filtered by status.",
        "inputSchema": json_schema(
            {"project_id": {"type": "string"}, "status": {"type": "string"}},
            ["project_id"],
        ),
    },
    {
        "name": "triage_todo",
        "description": "Triage a Todo item into an execution or roadmap state.",
        "inputSchema": json_schema(
            {
                "todo_id": {"type": "string"},
                "status": {"type": "string"},
                "reason": {"type": "string"},
                "commitment_id": {"type": "string"},
                "actor": {"type": "string"},
                "human_confirm": {"type": "boolean"},
                "decision_source": {"type": "string"},
                "context_token": {"type": "string"},
            },
            ["todo_id", "status", "reason"],
        ),
    },
    {
        "name": "promote_todo_to_roadmap_proposal",
        "description": "Promote a requires_roadmap Todo to a pending RoadmapProposal.",
        "inputSchema": json_schema(
            {
                "todo_id": {"type": "string"},
                "reason": {"type": "string"},
                "title": {"type": "string"},
                "actor": {"type": "string"},
                "context_token": {"type": "string"},
            },
            ["todo_id", "reason"],
        ),
    },
    {
        "name": "create_task_from_todo",
        "description": (
            "Create a Task from an existing ready or ad_hoc_approved Todo. Use create_task for new concrete work that "
            "the user directly asks to taskize; use create_task_from_todo only when converting an already-created Todo. "
            "The resulting human-readable task fields must preserve the project's primary language policy."
        ),
        "inputSchema": json_schema(
            {
                "todo_id": {"type": "string"},
                "type": {"type": "string"},
                "risk": {"type": "string"},
                "title": {"type": "string"},
                "actor": {"type": "string"},
                "context_token": {"type": "string"},
            },
            ["todo_id", "type", "risk"],
        ),
    },
]


WRITE_FENCE_TOOL_NAMES = {
    "record_verification",
    "submit_agent_report",
    "record_test_result",
    "request_task_review",
    "dispatch_review",
    "register_reviewer",
    "claim_next_review",
    "mark_stale_review_requests",
    "request_review",
    "import_review_result",
    "update_review_finding",
    "create_task",
    "import_agent_report",
    "record_verification_run",
    "create_todo",
    "triage_todo",
    "promote_todo_to_roadmap_proposal",
    "create_task_from_todo",
}


DEFAULT_TOOL_NAMES = {
    "get_status",
    "record_verification",
    "request_review",
    "import_review_result",
    "get_task_status",
}

REVIEW_HANDOFF_ADDITIONAL_TOOL_NAMES = {
    "register_reviewer",
    "claim_next_review",
    "request_task_review",
    "dispatch_review",
    "get_review_prompt",
    "get_review_template",
    "get_review_status",
    "mark_stale_review_requests",
}


def default_tools() -> list[dict]:
    names = DEFAULT_TOOL_NAMES.union(REVIEW_HANDOFF_ADDITIONAL_TOOL_NAMES)
    return [tool for tool in TOOLS if tool["name"] in names]


def review_handoff_tools() -> list[dict]:
    return [tool for tool in TOOLS if tool["name"] in REVIEW_HANDOFF_ADDITIONAL_TOOL_NAMES]


def tools_for_list_params(params: dict | None) -> list[dict]:
    params = params or {}
    if params.get("context") in {"advanced", "all"}:
        return TOOLS
    return default_tools()


HUMAN_GATED_TOOL_NAMES = {
    "complete_task",
    "close_roadmap_commitment",
    "commit_changes",
    "force_close_or_override",
    "destructive_db_migration",
}


BEHAVIOR_CHANGING_TASK_TYPES = {"implementation", "refactor", "test_addition"}
TODO_KINDS = {"user_request", "discovered_issue", "follow_up", "cleanup", "question", "roadmap_candidate"}
TODO_STATUSES = {
    "open",
    "triaged",
    "ready",
    "ad_hoc_approved",
    "requires_roadmap",
    "blocked",
    "converted_to_task",
    "deferred",
    "rejected",
    "superseded",
}
TODO_PRIORITIES = {"low", "normal", "high"}
TRIAGE_TODO_STATUSES = {"triaged", "ready", "ad_hoc_approved", "requires_roadmap", "blocked", "deferred", "rejected"}
STARTABLE_TODO_STATUSES = {"ready", "ad_hoc_approved"}
PROMOTABLE_TODO_STATUSES = {"requires_roadmap"}
TASK_TYPES = {"implementation", "refactor", "test_addition", "verification", "research", "review", "documentation", "design"}
TASK_RISKS = {"low", "medium", "high"}


class McpToolError(ValueError):
    pass


def require_string(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise McpToolError(f"missing required string argument: {key}")
    return value


def optional_string(arguments: dict, key: str, default: str = "") -> str:
    value = arguments.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise McpToolError(f"argument must be a string: {key}")
    return value


def require_string_list(arguments: dict, key: str) -> list[str]:
    value = arguments.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise McpToolError(f"missing required string list argument: {key}")
    return value


def optional_string_list(arguments: dict, key: str) -> list[str]:
    value = arguments.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise McpToolError(f"argument must be a string list: {key}")
    return value


def require_mcp_human_readable_language(project: dict, fields: dict[str, str | list[str]]) -> None:
    language = project_primary_language(project)
    issues = human_readable_language_issues(language, fields)
    if issues:
        details = "; ".join(issues)
        raise McpToolError(
            f"human-readable field language mismatch: {details}. "
            "Keep commands, paths, identifiers, status, enum, and JSON field names unchanged."
        )


def require_bool(arguments: dict, key: str) -> bool:
    value = arguments.get(key)
    if not isinstance(value, bool):
        raise McpToolError(f"missing required boolean argument: {key}")
    return value


def optional_number(arguments: dict, key: str, default: float) -> float:
    value = arguments.get(key, default)
    if not isinstance(value, (int, float)):
        raise McpToolError(f"argument must be a number: {key}")
    return float(value)


def optional_int_or_none(arguments: dict, key: str) -> int | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise McpToolError(f"argument must be an integer or null: {key}")
    return value


def optional_int(arguments: dict, key: str, default: int) -> int:
    value = arguments.get(key, default)
    if not isinstance(value, int):
        raise McpToolError(f"argument must be an integer: {key}")
    return value


def optional_bool(arguments: dict, key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise McpToolError(f"argument must be a boolean: {key}")
    return value


def optional_object(arguments: dict, key: str) -> dict:
    value = arguments.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpToolError(f"argument must be an object: {key}")
    return value


def latest_reviewer_registration(store: Store, reviewer: str) -> dict | None:
    rows = store.list_where("review_reviewers", "reviewer=?", (reviewer,))
    if not rows:
        return None
    return max(rows, key=lambda row: row["last_heartbeat_at"])


def reviewer_is_available(store: Store, reviewer: str) -> bool:
    return reviewer_is_registered_available(store, reviewer)


def initial_review_request_status(store: Store, reviewer: str) -> str:
    return "requested" if reviewer_is_available(store, reviewer) else "reviewer_unavailable"


def require_fresh_task_event(store: Store, task_id: str, last_seen_event_id: str) -> dict | None:
    latest_event = store.latest_task_status_event(task_id)
    current_event_id = latest_event["event_id"] if latest_event else ""
    if last_seen_event_id != current_event_id:
        raise McpToolError(
            f"stale task state: last_seen_event_id={last_seen_event_id}, current_event_id={current_event_id}"
        )
    return latest_event


def task_context_token(task_id: str, latest_event: dict | None) -> str:
    event_id = latest_event["event_id"] if latest_event else ""
    return f"task:{task_id}:{event_id}"


def event_id_from_context_token(token: str, expected_task_id: str) -> str:
    parts = token.split(":", 2)
    if len(parts) != 3 or parts[0] != "task" or not parts[1] or not parts[2]:
        raise McpToolError("invalid context_token")
    if parts[1] != expected_task_id:
        raise McpToolError(f"context_token task mismatch: token_task_id={parts[1]}, task_id={expected_task_id}")
    return parts[2]


def observed_task_event_id(arguments: dict, task_id: str) -> str:
    last_seen_event_id = optional_string(arguments, "last_seen_event_id")
    context_token = optional_string(arguments, "context_token")
    if context_token:
        token_event_id = event_id_from_context_token(context_token, task_id)
        if last_seen_event_id and last_seen_event_id != token_event_id:
            raise McpToolError("last_seen_event_id does not match context_token")
        return token_event_id
    if last_seen_event_id:
        return last_seen_event_id
    raise McpToolError("missing required argument: context_token or last_seen_event_id")


def require_fresh_task_context(store: Store, task_id: str, arguments: dict) -> dict | None:
    return require_fresh_task_event(store, task_id, observed_task_event_id(arguments, task_id))


def project_summary(store: Store, project_id: str) -> dict:
    try:
        return build_project_status(store, project_id)
    except ProjectNotFoundError:
        raise project_not_found_error(store, project_id)


def mcp_arguments_boundary(arguments: dict, store: Store) -> Any:
    context = arguments.get("__nilo_workspace_context") or {}
    root = Path(context.get("project_root") or context.get("git_root") or Path.cwd())
    return resolve_project_boundary(root, db_path=store.path)


def classify_next_step(summary: dict) -> dict:
    workflow = summary.get("workflow_context") or {}
    if workflow.get("type") == "recipe_run":
        waiting_public = workflow.get("status") == "waiting_public_approval"
        action = (summary.get("next_actions") or ["continue active recipe workflow"])[0]
        return {
            "action_id": "continue_active_recipe" if not waiting_public else "await_public_operation_confirmation",
            "task_id": workflow.get("task_id", ""),
            "task_type": "recipe",
            "task_status": workflow.get("status", ""),
            "command_hint": action,
            "human_next_action": human_next_action_text(action),
            "safe_for_ai": not waiting_public,
            "requires_explicit_human_intent": waiting_public,
            "reason": "active recipe run is the current workflow focus",
            "workflow_context": workflow,
        }
    active_tasks = summary["active_tasks"]
    if active_tasks:
        task = active_tasks[0]
        action = (summary["next_actions"] or ["review current task state"])[0]
        human_action = (summary.get("human_next_actions") or [human_next_action_text(action)])[0]
        human_status = task.get("human_status") or human_task_status(task["status"], task)
        requires_human = task["task_type"] in BEHAVIOR_CHANGING_TASK_TYPES and task["status"] in {
            "verification_passed",
            "needs_human_review",
            "review_commented",
            "review_approved",
        }
        return {
            "action_id": "continue_active_task",
            "task_id": task["id"],
            "task_type": task["task_type"],
            "task_status": task["status"],
            "command_hint": action,
            "human_next_action": human_action,
            "human_status": human_status,
            "safe_for_ai": not requires_human,
            "requires_explicit_human_intent": requires_human,
            "reason": "active task is the current work focus",
        }
    actions = summary["roadmap_agent_next_actions"]
    if actions:
        action = actions[0]
        requires_human = action["action_id"] in {"close_roadmap_commitment"}
        return {
            "action_id": action["action_id"],
            "task_id": "",
            "task_type": "",
            "task_status": "",
            "command_hint": action["command_hint"],
            "human_next_action": action.get("human_next_action", action["command_hint"]),
            "safe_for_ai": not requires_human,
            "requires_explicit_human_intent": requires_human,
            "reason": action["reason"],
        }
    action = (summary["next_actions"] or ["no action available"])[0]
    return {
        "action_id": "project_next_action",
        "task_id": "",
        "task_type": "",
        "task_status": "",
        "command_hint": action,
        "human_next_action": human_next_action_text(action),
        "safe_for_ai": True,
        "requires_explicit_human_intent": False,
        "reason": "project-level next action",
    }


def agent_work_context_from_summary(store: Store, summary: dict) -> dict:
    active_tasks = []
    for task in summary["active_tasks"]:
        latest_event = store.latest_task_status_event(task["id"])
        task_context = dict(task)
        task_context["latest_task_status_event"] = latest_event
        task_context["write_context_token"] = task_context_token(task["id"], latest_event)
        instruction = store.latest_for_task("instructions", task["id"])
        task_context["instruction_exists"] = instruction is not None
        task_context["instruction_id"] = instruction["id"] if instruction else ""
        active_tasks.append(task_context)
    next_step = classify_next_step({**summary, "active_tasks": active_tasks})
    write_context_token = active_tasks[0]["write_context_token"] if active_tasks else ""
    return {
        "project_id": summary["project_id"],
        "project_name": summary["project_name"],
        "roadmap_position": summary["roadmap_position"],
        "work_state": summary["work_state"],
        "human_work_state": summary["work_state"],
        "current_phase": summary["current_phase"],
        "roadmap_agent_state": summary["roadmap_agent_state"],
        "workflow_context": summary.get("workflow_context", {"type": "project", "status": "no_active_recipe"}),
        "roadmap_agent_next_actions": summary["roadmap_agent_next_actions"],
        "allowed_actions": (summary["roadmap_agent_state"] or {}).get("ai_allowed_actions", []),
        "blocked_actions": (summary["roadmap_agent_state"] or {}).get("ai_blocked_actions", []),
        "human_gates": sorted(HUMAN_GATED_TOOL_NAMES),
        "active_tasks": active_tasks,
        "next_actions": summary["next_actions"],
        "human_next_actions": summary["human_next_actions"],
        "next_step": next_step,
        "write_context_token": write_context_token,
        "unexecuted_verifications": summary["unexecuted_verifications"],
    }


def refreshed_task_context(store: Store, task_id: str) -> dict:
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    context = agent_work_context_from_summary(store, project_summary(store, task["project_id"]))
    matching = [item for item in context["active_tasks"] if item["id"] == task_id]
    if matching:
        task_context = matching[0]
    else:
        latest_event = store.latest_task_status_event(task_id)
        instruction = store.latest_for_task("instructions", task_id)
        task_context = {
            "id": task_id,
            "title": task["title"],
            "status": projected_task_status(store, task),
            "human_status": human_task_status(projected_task_status(store, task), task),
            "task_type": task["task_type"],
            "risk_level": task["risk_level"],
            "latest_task_status_event": latest_event,
            "write_context_token": task_context_token(task_id, latest_event),
            "instruction_exists": instruction is not None,
            "instruction_id": instruction["id"] if instruction else "",
        }
    return {"project_context": context, "task_context": task_context}


def get_project_status(store: Store, arguments: dict) -> dict:
    summary = project_summary(store, require_string(arguments, "project_id"))
    boundary = mcp_arguments_boundary(arguments, store)
    return project_status_view(summary, boundary.to_dict())


def get_status(store: Store, arguments: dict) -> dict:
    project_id = require_string(arguments, "project_id")
    if not store.get("projects", project_id):
        raise project_not_found_error(store, project_id)
    boundary = mcp_arguments_boundary(arguments, store)
    return {**project_ai_context(store, project_id), "project_boundary": boundary.to_dict()}


def get_project_summary(store: Store, arguments: dict) -> dict:
    return project_summary(store, require_string(arguments, "project_id"))


def get_agent_work_context(store: Store, arguments: dict) -> dict:
    summary = project_summary(store, require_string(arguments, "project_id"))
    boundary = mcp_arguments_boundary(arguments, store)
    return {**agent_work_context_from_summary(store, summary), "project_boundary": boundary.to_dict()}


def get_next_step(store: Store, arguments: dict) -> dict:
    summary = project_summary(store, require_string(arguments, "project_id"))
    boundary = mcp_arguments_boundary(arguments, store)
    return {
        "project_id": summary["project_id"],
        "project_boundary": boundary.to_dict(),
        "roadmap_position": summary["roadmap_position"],
        "work_state": summary["work_state"],
        "human_work_state": summary["work_state"],
        "current_phase": summary["current_phase"],
        "next_step": classify_next_step(summary),
        "workflow_context": summary.get("workflow_context", {"type": "project", "status": "no_active_recipe"}),
    }


def mcp_doctor(store: Store, arguments: dict) -> dict:
    project_id = require_string(arguments, "project_id")
    project = store.get("projects", project_id)
    if not project:
        raise project_not_found_error(store, project_id)
    tool_names = [tool["name"] for tool in TOOLS]
    default_tool_names = [tool["name"] for tool in default_tools()]
    review_handoff_tool_names = [tool["name"] for tool in review_handoff_tools()]
    review_context_tool_names = sorted(DEFAULT_TOOL_NAMES.union(REVIEW_HANDOFF_ADDITIONAL_TOOL_NAMES))
    exposed_human_gated = sorted(HUMAN_GATED_TOOL_NAMES.intersection(tool_names))
    summary = project_summary(store, project_id)
    reviewers = []
    for row in store.list_where("review_reviewers"):
        dispatch_capable = reviewer_is_dispatch_capable(row)
        availability = reviewer_availability(row)
        reviewers.append(
            {
                "reviewer": row["reviewer"],
                **reviewer_identity(row),
                "status": row["status"],
                "availability": availability,
                "dispatch_capable": dispatch_capable,
                "evidence_profile": reviewer_evidence_profile(row),
                "claude_code_e2e_capable": reviewer_is_claude_code_e2e_capable(row),
                "heartbeat_age_seconds": reviewer_heartbeat_age_seconds(row),
                "metadata": row["metadata"],
                "last_heartbeat_at": row["last_heartbeat_at"],
            }
        )
    return {
        "ok": not exposed_human_gated,
        "project_id": project_id,
        "project_readable": True,
        "tool_count": len(tool_names),
        "tool_names": tool_names,
        "default_tool_count": len(default_tool_names),
        "default_tool_names": default_tool_names,
        "review_handoff_tool_count": len(review_handoff_tool_names),
        "review_handoff_tool_names": review_handoff_tool_names,
        "review_context_tool_count": len(review_context_tool_names),
        "review_context_tool_names": review_context_tool_names,
        "exposed_human_gated_tools": exposed_human_gated,
        "expected_safe_tools_present": all(
            name in tool_names
            for name in [
                "get_status",
                "get_task_status",
                "record_verification",
                "request_review",
                "import_review_result",
            ]
        ),
        "review_handoff_tools_present": all(
            name in review_context_tool_names
            for name in [
                "register_reviewer",
                "claim_next_review",
                "request_task_review",
                "dispatch_review",
                "get_review_prompt",
                "get_review_template",
                "import_review_result",
                "get_review_status",
                "mark_stale_review_requests",
            ]
        ),
        "reviewers": reviewers,
        "claude_code_reviewer": reviewer_prepare_status(store, "claude-code"),
        "work_state": summary["work_state"],
        "roadmap_position": summary["roadmap_position"],
        "registered_workspaces": list_workspace_entries(),
        "multi_workspace_usage": [
            "pass project_root to use a specific repository",
            "pass workspace to use a registered workspace",
        ],
    }


def mcp_ping(store: Store, arguments: dict) -> dict:
    return {
        "ok": True,
        "server": {"name": "nilo", "version": __version__},
        "protocolVersion": PROTOCOL_VERSION,
    }


def prepare_reviewer(store: Store, arguments: dict) -> dict:
    project_id = require_string(arguments, "project_id")
    if not store.get("projects", project_id):
        raise project_not_found_error(store, project_id)
    return {
        "project_id": project_id,
        **reviewer_prepare_status(store, require_string(arguments, "reviewer")),
    }


def get_roadmap_status(store: Store, arguments: dict) -> dict:
    from .roadmap_render import render_pending_roadmap_plan_lines

    summary = project_summary(store, require_string(arguments, "project_id"))
    return {
        "project_id": summary["project_id"],
        "roadmap_position": summary["roadmap_position"],
        "roadmap_commitments": summary["roadmap_commitments"],
        "closed_roadmap_commitments": summary["closed_roadmap_commitments"],
        "pending_roadmap_revisions": summary["pending_roadmap_revisions"],
        "pending_roadmap_review_messages": [
            "\n".join(render_pending_roadmap_plan_lines(revision, "ja"))
            for revision in summary["pending_roadmap_revisions"]
        ],
        "human_next_actions": summary["human_next_actions"],
        "roadmap_agent_state": summary["roadmap_agent_state"],
        "roadmap_agent_next_actions": summary["roadmap_agent_next_actions"],
        "roadmap_assessments": summary["roadmap_assessments"],
    }


def discuss_roadmap(store: Store, arguments: dict) -> dict:
    from .roadmap_render import render_roadmap_discuss_markdown

    summary = project_summary(store, require_string(arguments, "project_id"))
    return {"project_id": summary["project_id"], "body_md": render_roadmap_discuss_markdown(summary)}


def latest_for_task_tables(store: Store, task_id: str) -> dict:
    tables = [
        "instructions",
        "agent_reports",
        "verification_runs",
        "understanding_checks",
        "quality_reviews",
        "review_requests",
        "review_results",
        "task_completions",
    ]
    return {table: store.latest_for_task(table, task_id) for table in tables}


def get_task_status(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    latest = latest_for_task_tables(store, task_id)
    status = projected_task_status(store, task)
    latest_event = store.latest_task_status_event(task_id)
    return {
        "task": task,
        "status": status,
        "human_status": human_task_status(status, task, latest),
        "latest_task_status_event": latest_event,
        "write_context_token": task_context_token(task_id, latest_event),
        "latest_task_status_event_id": latest_event["event_id"] if latest_event else "",
        "latest": latest,
    }


def get_instruction(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    instruction = store.latest_for_task("instructions", task_id)
    return {
        "task_id": task_id,
        "status": projected_task_status(store, task),
        "base_commit": task.get("base_commit"),
        "instruction": instruction,
        "instruction_exists": instruction is not None,
    }


def get_review_status(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    if not store.get("tasks", task_id):
        raise McpToolError(f"task not found: {task_id}")
    findings = store.list_where("review_findings", "task_id=?", (task_id,))
    enriched_findings = []
    for finding in findings:
        item = dict(finding)
        item["update_history"] = list(reversed(store.list_where("review_finding_updates", "finding_id=?", (finding["id"],))))
        enriched_findings.append(item)
    return {
        "task_id": task_id,
        "review_requests": store.list_where("review_requests", "task_id=?", (task_id,)),
        "review_results": store.list_where("review_results", "task_id=?", (task_id,)),
        "review_findings": enriched_findings,
    }


def mcp_request_review(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    from_actor = require_string(arguments, "from_actor")
    to_actor = require_string(arguments, "to_actor")
    reason = require_string(arguments, "reason")
    if not store.get("tasks", task_id):
        raise McpToolError(f"task not found: {task_id}")
    previous_event = require_fresh_task_context(store, task_id, arguments)
    allow_unavailable = optional_bool(arguments, "allow_unavailable", True)
    known_unavailable_only = optional_bool(arguments, "known_unavailable_only", False)
    try:
        if allow_unavailable and known_unavailable_only:
            resolved = resolve_known_review_request_target(store, to_actor)
        elif allow_unavailable:
            resolved = resolve_review_request_target(store, to_actor)
        else:
            resolved = resolve_reviewer(store, to_actor)
    except ReviewerResolutionError as exc:
        next_action = reviewer_unavailable_next_action(store, to_actor)
        raise McpToolError(f"{exc}; next_action: {next_action}") from None
    created_at = now_iso()
    workspace_root = Path((arguments.get("__nilo_workspace_context") or {}).get("project_root", Path.cwd()))
    snapshot = compact_snapshot(current_git_snapshot(workspace_root))
    row = {
        "id": make_id("review"),
        "task_id": task_id,
        "requester": from_actor,
        "reviewer": resolved.reviewer,
        "status": initial_review_request_status(store, resolved.reviewer),
        "reason": reason,
        "based_on_event_id": previous_event["event_id"] if previous_event else "",
        "based_on_snapshot": snapshot,
        "created_at": created_at,
        "updated_at": created_at,
    }
    insert_review_request(store, row)
    return {
        "task_id": task_id,
        "review_request": row,
        "previous_event": previous_event,
        "latest_event": store.latest_task_status_event(task_id),
    }


def register_reviewer(store: Store, arguments: dict) -> dict:
    reviewer = canonical_reviewer_name(require_string(arguments, "reviewer"))
    raw_capabilities = optional_string_list(arguments, "capabilities")
    capabilities = normalize_capabilities(raw_capabilities) or ["review_diff"]
    max_concurrent = optional_int(arguments, "max_concurrent", 1)
    metadata = optional_object(arguments, "metadata")
    now = now_iso()
    existing = latest_reviewer_registration(store, reviewer)
    row = {
        "id": existing["id"] if existing else make_id("reviewer"),
        "reviewer": reviewer,
        "status": "available",
        "capabilities": capabilities,
        "max_concurrent": max_concurrent,
        "metadata": metadata,
        "last_heartbeat_at": now,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    if existing:
        store.update(
            "review_reviewers",
            existing["id"],
            {
                "status": row["status"],
                "capabilities": row["capabilities"],
                "max_concurrent": row["max_concurrent"],
                "metadata": row["metadata"],
                "last_heartbeat_at": row["last_heartbeat_at"],
                "updated_at": row["updated_at"],
            },
        )
        row = store.get("review_reviewers", existing["id"])
    else:
        store.insert("review_reviewers", row)
    revived = []
    if reviewer_is_dispatch_capable(row):
        for request in store.list_where("review_requests", "reviewer=? AND status='reviewer_unavailable'", (reviewer,)):
            update_review_request(store, request["id"], {"status": "requested", "updated_at": now})
            revived.append(request["id"])
    return {"reviewer": row, "revived_review_requests": revived}


def claim_next_review(store: Store, arguments: dict) -> dict:
    reviewer = canonical_reviewer_name(require_string(arguments, "reviewer"))
    project_id = optional_string(arguments, "project_id")
    if not reviewer_is_available(store, reviewer):
        raise McpToolError(f"reviewer is not registered or available: {reviewer}")
    where = "reviewer=? AND status IN ('requested', 'stale')"
    args: tuple[Any, ...] = (reviewer,)
    if project_id:
        where += " AND task_id IN (SELECT id FROM tasks WHERE project_id=?)"
        args = (reviewer, project_id)
    rows = store.list_where("review_requests", where, args)
    if not rows:
        return {"reviewer": reviewer, "claimed": False, "review_request": None}
    request = rows[-1]
    now = now_iso()
    request = update_review_request(store, request["id"], {"status": "claimed", "updated_at": now})
    task = store.get("tasks", request["task_id"])
    report = store.latest_for_task("agent_reports", task["id"])
    verification_run = store.latest_for_task("verification_runs", task["id"])
    workspace_root = Path((arguments.get("__nilo_workspace_context") or {}).get("project_root", Path.cwd()))
    return {
        "reviewer": reviewer,
        "claimed": True,
        "review_request": request,
        "task_id": task["id"],
        "review_id": request["id"],
        "prompt_md": build_review_context(task, request, report, None, verification_run, workspace_root),
        "template_md": build_review_result_template(request),
        "latest_event": store.latest_task_status_event(task["id"]),
    }


def mark_stale_review_requests(store: Store, arguments: dict) -> dict:
    reviewer = optional_string(arguments, "reviewer")
    stale_after_seconds = optional_int(arguments, "stale_after_seconds", 900)
    where = "status IN ('claimed', 'in_progress')"
    args: tuple[Any, ...] = ()
    if reviewer:
        where += " AND reviewer=?"
        args = (reviewer,)
    now = now_iso()
    stale = []
    for request in store.list_where("review_requests", where, args):
        if iso_age_seconds(request["updated_at"]) < stale_after_seconds:
            continue
        set_review_request_status(store, request["id"], "stale", updated_at=now)
        stale.append(request["id"])
    return {"stale_review_requests": stale, "count": len(stale)}


def get_review_prompt(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    review_id = require_string(arguments, "review_id")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    request = store.get("review_requests", review_id)
    if not request or request["task_id"] != task_id:
        raise McpToolError(f"review request not found for task: {review_id}")
    report = store.latest_for_task("agent_reports", task_id)
    verification_run = store.latest_for_task("verification_runs", task_id)
    return {
        "task_id": task_id,
        "review_id": review_id,
        "body_md": build_review_context(task, request, report, None, verification_run, Path.cwd()),
    }


def get_review_template(store: Store, arguments: dict) -> dict:
    review_id = require_string(arguments, "review_id")
    request = store.get("review_requests", review_id)
    if not request:
        raise McpToolError(f"review request not found: {review_id}")
    return {"task_id": request["task_id"], "review_id": review_id, "body_md": build_review_result_template(request)}


def mcp_import_review_result(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    review_id = require_string(arguments, "review_id")
    body_md = require_string(arguments, "body_md")
    reviewer = require_string(arguments, "reviewer")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    request = store.get("review_requests", review_id)
    if not request or request["task_id"] != task_id:
        raise McpToolError(f"review request not found for task: {review_id}")
    if request["status"] not in {"claimed", "in_progress"}:
        raise McpToolError(f"review request must be claimed or in_progress before import: {review_id} [{request['status']}]")
    if reviewer != request["reviewer"]:
        raise McpToolError(f"reviewer mismatch for review {review_id}: expected {request['reviewer']}, got {reviewer}")
    observed_event_id = observed_task_event_id(arguments, task_id)
    previous_event = require_fresh_task_event(store, task_id, observed_event_id)
    try:
        transition = transition_import_review_result(
            store,
            task_id,
            review_id,
            body_md=body_md,
            reviewer=reviewer,
            last_seen_event_id=observed_event_id,
            cwd=Path((arguments.get("__nilo_workspace_context") or {}).get("project_root", Path.cwd())),
        )
    except TransitionError as exc:
        raise McpToolError(exc.message) from exc
    result = store.get("review_results", transition.created_ids["review_result"])
    stored_findings = store.list_where("review_findings", "review_result_id=?", (result["id"],))
    return {
        "task_id": task_id,
        "review_result": result,
        "review_findings": stored_findings,
        "previous_event": previous_event,
        "latest_event": store.latest_task_status_event(task_id),
    }


def mcp_update_review_finding(store: Store, arguments: dict) -> dict:
    finding_id = require_string(arguments, "finding_id")
    status = require_string(arguments, "status")
    reason = require_string(arguments, "reason")
    actor = require_string(arguments, "actor")
    finding = store.get("review_findings", finding_id)
    if not finding:
        raise McpToolError(f"review finding not found: {finding_id}")
    previous_event = require_fresh_task_context(store, finding["task_id"], arguments)
    try:
        transition = transition_update_review_finding(
            store,
            finding_id,
            status=status,
            reason=reason,
            actor=actor,
            human_confirm=optional_bool(arguments, "human_confirm", False),
            decision_source=optional_string(arguments, "decision_source"),
        )
    except TransitionError as exc:
        raise McpToolError(exc.message) from exc
    update = store.get("review_finding_updates", transition.created_ids["review_finding_update"])
    return {
        "task_id": finding["task_id"],
        "review_finding": store.get("review_findings", finding_id),
        "review_finding_update": update,
        "previous_event": previous_event,
        "latest_event": store.latest_task_status_event(finding["task_id"]),
    }


def assess_roadmap(store: Store, arguments: dict) -> dict:
    summary = project_summary(store, require_string(arguments, "project_id"))
    return {
        "project_id": summary["project_id"],
        "roadmap_position": summary["roadmap_position"],
        "roadmap_assessments": summary["roadmap_assessments"],
    }


def list_recent_history(store: Store, arguments: dict) -> dict:
    summary = project_summary(store, require_string(arguments, "project_id"))
    return {"project_id": summary["project_id"], "recent_history": summary["recent_history"]}


def mcp_create_task(store: Store, arguments: dict) -> dict:
    project_id = require_string(arguments, "project_id")
    title = require_string(arguments, "title")
    task_type = require_string(arguments, "type")
    risk = require_string(arguments, "risk")
    commitment_id = optional_string(arguments, "commitment_id")
    description = require_string(arguments, "description")
    acceptance = require_string_list(arguments, "acceptance")
    roadmap_item_id = optional_string(arguments, "roadmap_item_id")
    if task_type not in {"implementation", "refactor", "test_addition", "verification", "research", "review", "documentation", "design"}:
        raise McpToolError(f"unsupported task type: {task_type}")
    if risk not in {"low", "medium", "high"}:
        raise McpToolError(f"unsupported risk: {risk}")
    project = store.get("projects", project_id)
    if not project:
        raise project_not_found_error(store, project_id)
    require_mcp_human_readable_language(
        project,
        {"title": title, "description": description, "acceptance": acceptance},
    )
    language_policy = human_readable_language_policy(project)
    created_at = now_iso()
    row = {
        "id": make_id("task"),
        "project_id": project_id,
        "title": title,
        "description": description,
        "acceptance_criteria": acceptance,
        "parent_task_id": None,
        "split_index": None,
        "task_type": task_type,
        "risk_level": risk,
        "requires_understanding_check": False,
        "roadmap_commitment_id": commitment_id,
        "roadmap_item_id": roadmap_item_id,
        "status": "planned",
        "assigned_model_profile": "",
        "degradation_mode": "normal",
        "base_commit": None,
        "created_at": created_at,
    }
    store.insert("tasks", row)
    return {"task": row, "language_policy": language_policy, "latest_event": store.latest_task_status_event(row["id"])}


def mcp_import_agent_report(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    body_md = require_string(arguments, "body_md")
    agent = require_string(arguments, "agent")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    latest_event = require_fresh_task_context(store, task_id, arguments)
    result = import_agent_report(store, task, body_md, agent, Path.cwd())
    return {
        "task_id": task_id,
        "report": result["report"],
        "evidence_status": result["evidence_status"],
        "evidence_check": result["evidence_check"],
        "previous_event": latest_event,
        "latest_event": store.latest_task_status_event(task_id),
    }


def mcp_record_verification_run(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    latest_event = require_fresh_task_context(store, task_id, arguments)
    stdout = optional_string(arguments, "stdout")
    stderr = optional_string(arguments, "stderr")
    raw_log = f"{stdout}\n{stderr}"
    secret_issues = detect_secret_issues(raw_log)
    metadata = arguments.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise McpToolError("argument must be an object: metadata")
    mode = optional_string(arguments, "mode", metadata.get("verification_mode", "targeted")) or "targeted"
    if mode not in {"quick", "targeted", "full"}:
        raise McpToolError("argument must be one of quick, targeted, full: mode")
    finished_at = optional_string(arguments, "finished_at", now_iso()) or now_iso()
    if any(key in arguments for key in ("git_head", "git_diff_hash", "working_tree_dirty")):
        snapshot = {
            "git_head": optional_string(arguments, "git_head", ""),
            "git_diff_hash": optional_string(arguments, "git_diff_hash", ""),
            "working_tree_dirty": optional_bool(arguments, "working_tree_dirty", False),
            "git_status_porcelain": optional_string(arguments, "git_status_porcelain", ""),
            "observed_paths": optional_string_list(arguments, "observed_paths"),
        }
    else:
        snapshot = current_git_snapshot(Path.cwd())
    row = {
        "id": make_id("verification"),
        "task_id": task_id,
        "evidence_check_id": None,
        "source": "agent_reported",
        "command": require_string(arguments, "command"),
        "cwd": require_string(arguments, "cwd"),
        "stdout": mask_secrets(stdout),
        "stderr": mask_secrets(stderr),
        "exit_code": optional_int_or_none(arguments, "exit_code"),
        "timed_out": require_bool(arguments, "timed_out"),
        "timeout_seconds": optional_number(arguments, "timeout_seconds", 0.0),
        **snapshot_columns(snapshot),
        "metadata": {
            **metadata,
            "verification_mode": mode,
            "secret_issue_count": len(secret_issues),
            "secret_issues": secret_issues,
            "runner": metadata.get("runner", "external_agent"),
        },
        "started_at": optional_string(arguments, "started_at", finished_at) or finished_at,
        "finished_at": finished_at,
        "created_at": finished_at,
    }
    try:
        transition_record_verification_run(store, task_id, row=row, actor="ai")
    except TransitionError as exc:
        raise McpToolError(exc.message) from exc
    return {"task_id": task_id, "verification_run": row, "previous_event": latest_event, "latest_event": store.latest_task_status_event(task_id)}


def todo_context_token(todo: dict) -> str:
    return f"todo:{todo['id']}:{todo['status']}"


def require_fresh_todo_context(todo: dict, arguments: dict) -> None:
    token = optional_string(arguments, "context_token")
    if not token:
        return
    expected = todo_context_token(todo)
    if token != expected:
        raise McpToolError(f"stale todo state: context_token={token}, current_context_token={expected}")


def mcp_create_todo(store: Store, arguments: dict) -> dict:
    project_id = require_string(arguments, "project_id")
    title = require_string(arguments, "title")
    kind = require_string(arguments, "kind")
    if kind not in TODO_KINDS:
        raise McpToolError(f"unsupported todo kind: {kind}")
    priority = optional_string(arguments, "priority", "normal") or "normal"
    if priority not in TODO_PRIORITIES:
        raise McpToolError(f"unsupported todo priority: {priority}")
    project = store.get("projects", project_id)
    if not project:
        raise project_not_found_error(store, project_id)
    require_mcp_human_readable_language(
        project,
        {
            "title": title,
            "description": optional_string(arguments, "description"),
            "acceptance_hint": optional_string(arguments, "acceptance_hint"),
        },
    )
    language_policy = human_readable_language_policy(project)
    created_at = now_iso()
    row = {
        "id": make_id("todo"),
        "project_id": project_id,
        "title": title,
        "kind": kind,
        "status": "open",
        "description": optional_string(arguments, "description"),
        "acceptance_hint": optional_string(arguments, "acceptance_hint"),
        "priority": priority,
        "source_type": optional_string(arguments, "source_type", "mcp"),
        "source_task_id": optional_string(arguments, "source_task_id"),
        "roadmap_commitment_id": "",
        "roadmap_revision_id": "",
        "converted_task_id": "",
        "created_at": created_at,
        "triaged_at": "",
        "triage_reason": "",
    }
    store.insert("todos", row)
    return {"todo": row, "language_policy": language_policy, "context_token": todo_context_token(row)}


def mcp_list_todos(store: Store, arguments: dict) -> dict:
    project_id = require_string(arguments, "project_id")
    if not store.get("projects", project_id):
        raise project_not_found_error(store, project_id)
    status = optional_string(arguments, "status")
    where = "project_id=?"
    values: tuple[Any, ...] = (project_id,)
    if status:
        if status not in TODO_STATUSES:
            raise McpToolError(f"unsupported todo status: {status}")
        where += " AND status=?"
        values = (project_id, status)
    todos = list(reversed(store.list_where("todos", where, values)))
    return {
        "project_id": project_id,
        "todos": [{**todo, "context_token": todo_context_token(todo)} for todo in todos],
    }


def mcp_triage_todo(store: Store, arguments: dict) -> dict:
    todo_id = require_string(arguments, "todo_id")
    status = require_string(arguments, "status")
    reason = require_string(arguments, "reason")
    if status not in TODO_STATUSES:
        raise McpToolError(f"unsupported todo status: {status}")
    if status not in TRIAGE_TODO_STATUSES:
        allowed = ", ".join(sorted(TRIAGE_TODO_STATUSES))
        raise McpToolError(f"todo status is not triage-settable: {status} (allowed: {allowed})")
    todo = store.get("todos", todo_id)
    if not todo:
        raise McpToolError(f"todo not found: {todo_id}")
    require_fresh_todo_context(todo, arguments)
    commitment_id = optional_string(arguments, "commitment_id")
    actor = optional_string(arguments, "actor", "ai") or "ai"
    try:
        transition_triage_todo(
            store,
            todo_id,
            status=status,
            reason=reason,
            actor=actor,
            human_confirm=optional_bool(arguments, "human_confirm", False),
            decision_source=optional_string(arguments, "decision_source"),
            commitment_id=commitment_id,
        )
    except TransitionError as exc:
        raise McpToolError(exc.message) from exc
    updated = store.get("todos", todo_id)
    return {"todo": updated, "context_token": todo_context_token(updated)}


def mcp_promote_todo_to_roadmap_proposal(store: Store, arguments: dict) -> dict:
    todo_id = require_string(arguments, "todo_id")
    reason = require_string(arguments, "reason")
    todo = store.get("todos", todo_id)
    if not todo:
        raise McpToolError(f"todo not found: {todo_id}")
    require_fresh_todo_context(todo, arguments)
    if todo["status"] not in PROMOTABLE_TODO_STATUSES:
        allowed = ", ".join(sorted(PROMOTABLE_TODO_STATUSES))
        raise McpToolError(f"todo is not promotable: {todo['status']} (allowed: {allowed})")
    project = store.get("projects", todo["project_id"])
    if not project:
        raise project_not_found_error(store, todo["project_id"])
    language_policy = human_readable_language_policy(project)
    primary_language = project_primary_language(project)
    proposal_texts = roadmap_proposal_texts(primary_language)
    created_at = now_iso()
    title = optional_string(arguments, "title") or todo["title"]
    body = render_roadmap_proposal_from_todo(title, todo["description"] or todo["title"], todo["acceptance_hint"], primary_language)
    commitment_id = make_id("commitment")
    revision_id = make_id("roadmap_rev")
    commitment = {
        "id": commitment_id,
        "project_id": project["id"],
        "title": title,
        "intent": todo["description"] or todo["title"],
        "success_criteria": [todo["acceptance_hint"]] if todo["acceptance_hint"] else [],
        "non_goals": [proposal_texts["non_goal"]],
        "autonomy_scope": [proposal_texts["autonomy_scope"]],
        "review_gates": [proposal_texts["review_gate"]],
        "evidence_policy": [proposal_texts["evidence_policy"]],
        "status": "pending",
        "accepted_by": "",
        "accepted_at": "",
        "created_at": created_at,
    }
    revision = {
        "id": revision_id,
        "project_id": project["id"],
        "proposed_commitment_id": commitment_id,
        "status": "pending",
        "body_md": body,
        "source_path": f"todo:{todo_id}",
        "reason": reason,
        "accepted_at": "",
        "created_at": created_at,
    }
    try:
        transition_promote_todo_to_roadmap_proposal(
            store,
            todo_id,
            commitment=commitment,
            revision=revision,
            actor=optional_string(arguments, "actor", "ai") or "ai",
            reason=reason,
        )
    except TransitionError as exc:
        raise McpToolError(exc.message) from exc
    updated = store.get("todos", todo_id)
    return {
        "todo": updated,
        "roadmap_revision": revision,
        "proposed_commitment": commitment,
        "language_policy": language_policy,
        "context_token": todo_context_token(updated),
    }


def mcp_create_task_from_todo(store: Store, arguments: dict) -> dict:
    todo_id = require_string(arguments, "todo_id")
    task_type = require_string(arguments, "type")
    risk = require_string(arguments, "risk")
    if task_type not in TASK_TYPES:
        raise McpToolError(f"unsupported task type: {task_type}")
    if risk not in TASK_RISKS:
        raise McpToolError(f"unsupported risk: {risk}")
    todo = store.get("todos", todo_id)
    if not todo:
        raise McpToolError(f"todo not found: {todo_id}")
    require_fresh_todo_context(todo, arguments)
    if todo["status"] not in STARTABLE_TODO_STATUSES:
        allowed = ", ".join(sorted(STARTABLE_TODO_STATUSES))
        raise McpToolError(f"todo is not startable: {todo['status']} (allowed: {allowed})")
    commitment_id = todo["roadmap_commitment_id"]
    project = store.get("projects", todo["project_id"])
    if not project:
        raise project_not_found_error(store, todo["project_id"])
    language_policy = human_readable_language_policy(project)
    created_at = now_iso()
    task_id = make_id("task")
    task = {
        "id": task_id,
        "project_id": todo["project_id"],
        "title": optional_string(arguments, "title") or todo["title"],
        "description": todo["description"],
        "acceptance_criteria": [todo["acceptance_hint"]] if todo["acceptance_hint"] else [],
        "parent_task_id": None,
        "split_index": None,
        "task_type": task_type,
        "risk_level": risk,
        "requires_understanding_check": False,
        "roadmap_commitment_id": commitment_id,
        "roadmap_item_id": "",
        "status": "planned",
        "assigned_model_profile": "",
        "degradation_mode": "normal",
        "base_commit": None,
        "created_at": created_at,
    }
    try:
        transition_create_task_from_todo(
            store,
            todo_id,
            task=task,
            actor=optional_string(arguments, "actor", "ai") or "ai",
            reason=f"converted to task {task_id}",
        )
    except TransitionError as exc:
        raise McpToolError(exc.message) from exc
    updated = store.get("todos", todo_id)
    return {
        "todo": updated,
        "task": task,
        "language_policy": language_policy,
        "latest_event": store.latest_task_status_event(task_id),
        "context_token": todo_context_token(updated),
    }


def submit_agent_report(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    result = mcp_import_agent_report(store, arguments)
    return {
        "operation": "submit_agent_report",
        "result": result,
        "refreshed_context": refreshed_task_context(store, task_id),
    }


def record_test_result(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    result = mcp_record_verification_run(store, arguments)
    return {
        "operation": "record_test_result",
        "result": result,
        "refreshed_context": refreshed_task_context(store, task_id),
    }


def request_task_review(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    task = store.get("tasks", task_id)
    if not task:
        raise McpToolError(f"task not found: {task_id}")
    delegated = {
        **arguments,
        "from_actor": require_string(arguments, "requester"),
        "to_actor": require_string(arguments, "reviewer"),
        "allow_unavailable": True,
        "known_unavailable_only": True,
    }
    result = mcp_request_review(store, delegated)
    review_request = result["review_request"]
    reviewer_status = reviewer_prepare_status(store, review_request["reviewer"])
    claude_prompt = ""
    if review_request["reviewer"] == "claude-code":
        register_json = json.dumps(reviewer_status["register_reviewer_json"], ensure_ascii=False, indent=2)
        claude_prompt = (
            "Open the Claude Code session connected to the Nilo MCP server and run:\n"
            f"1. call register_reviewer with reviewer=\"claude-code\" and:\n{register_json}\n"
            f"2. call claim_next_review with reviewer=\"claude-code\" and project_id=\"{task['project_id']}\"\n"
            "3. generate a real review response\n"
            "4. call import_review_result for the claimed review"
        )
    next_action = reviewer_status["next_action"]
    if reviewer_status["reviewer"] == "claude-code" and reviewer_status["availability"] == "stale":
        next_action = (
            "claude-code reviewer is stale. Open the Claude Code session connected to the Nilo MCP server. "
            "The session must call register_reviewer to refresh heartbeat, then claim_next_review."
        )
    return {
        "operation": "request_task_review",
        "result": result,
        "reviewer_availability": reviewer_status["availability"],
        "reviewer_dispatch_capable": reviewer_status["dispatch_capable"],
        "next_action": next_action,
        "claude_code_prompt": claude_prompt,
        "refreshed_context": refreshed_task_context(store, task_id),
    }


def mcp_dispatch_review(store: Store, arguments: dict) -> dict:
    task_id = require_string(arguments, "task_id")
    actor = require_string(arguments, "actor")
    reviewer = require_string(arguments, "reviewer")
    reason = optional_string(arguments, "reason", "dispatched agent review") or "dispatched agent review"
    auto_start = arguments.get("auto_start")
    if auto_start is not None and not isinstance(auto_start, bool):
        raise McpToolError("argument must be a boolean: auto_start")
    auto_configure = arguments.get("auto_configure", False)
    if not isinstance(auto_configure, bool):
        raise McpToolError("argument must be a boolean: auto_configure")
    allow_cli_fallback = arguments.get("allow_cli_fallback", False)
    if not isinstance(allow_cli_fallback, bool):
        raise McpToolError("argument must be a boolean: allow_cli_fallback")
    config_path = optional_string(arguments, "config_path")
    if not allow_cli_fallback:
        latest_event = store.latest_task_status_event(task_id)
        requested = request_task_review(
            store,
            {
                **arguments,
                "requester": actor,
                "reviewer": reviewer,
                "reason": reason,
                "last_seen_event_id": arguments.get("last_seen_event_id") or (latest_event["event_id"] if latest_event else ""),
            },
        )
        review_request = requested["result"]["review_request"]
        return {
            "operation": "dispatch_review",
            "mode": "mcp_reviewer_workflow",
            "status": review_request["status"],
            "actor": actor,
            "reviewer": review_request["reviewer"],
            "task_id": task_id,
            "project_id": optional_string(arguments, "project_id") or "",
            "review_request_id": review_request["id"],
            "reviewer_availability": requested["reviewer_availability"],
            "reviewer_dispatch_capable": requested["reviewer_dispatch_capable"],
            "next_action": requested["next_action"],
            "claude_code_prompt": requested["claude_code_prompt"],
            "result": requested["result"],
            "refreshed_context": requested["refreshed_context"],
        }
    try:
        return dispatch_review(
            store,
            actor=actor,
            reviewer=reviewer,
            task_id=task_id,
            project_id=optional_string(arguments, "project_id"),
            reason=reason,
            auto_start=auto_start,
            auto_configure=auto_configure,
            config_path=Path(config_path) if config_path else None,
            repo_root=Path((arguments.get("__nilo_workspace_context") or {}).get("project_root", Path.cwd())),
        )
    except DispatchError as exc:
        raise McpToolError(f"review dispatch failed during {exc.stage}: {exc.reason}") from exc
    except Exception as exc:
        raise McpToolError(f"review dispatch failed unexpectedly: {type(exc).__name__}: {exc}") from exc


TOOL_HANDLERS = {
    "get_status": get_status,
    "record_verification": mcp_record_verification_run,
    "get_agent_work_context": get_agent_work_context,
    "get_next_step": get_next_step,
    "mcp_ping": mcp_ping,
    "mcp_doctor": mcp_doctor,
    "prepare_reviewer": prepare_reviewer,
    "submit_agent_report": submit_agent_report,
    "record_test_result": record_test_result,
    "request_task_review": request_task_review,
    "dispatch_review": mcp_dispatch_review,
    "register_reviewer": register_reviewer,
    "claim_next_review": claim_next_review,
    "mark_stale_review_requests": mark_stale_review_requests,
    "get_project_status": get_project_status,
    "get_project_summary": get_project_summary,
    "get_roadmap_status": get_roadmap_status,
    "discuss_roadmap": discuss_roadmap,
    "get_task_status": get_task_status,
    "get_instruction": get_instruction,
    "get_review_status": get_review_status,
    "request_review": mcp_request_review,
    "get_review_prompt": get_review_prompt,
    "get_review_template": get_review_template,
    "import_review_result": mcp_import_review_result,
    "update_review_finding": mcp_update_review_finding,
    "assess_roadmap": assess_roadmap,
    "list_recent_history": list_recent_history,
    "create_task": mcp_create_task,
    "import_agent_report": mcp_import_agent_report,
    "record_verification_run": mcp_record_verification_run,
    "create_todo": mcp_create_todo,
    "list_todos": mcp_list_todos,
    "triage_todo": mcp_triage_todo,
    "promote_todo_to_roadmap_proposal": mcp_promote_todo_to_roadmap_proposal,
    "create_task_from_todo": mcp_create_task_from_todo,
}


def maybe_record_mcp_boundary_failure(
    store: Store,
    arguments: dict,
    tool_name: str,
    error: ProjectBoundaryError,
    boundary: Any,
) -> None:
    if error.details.get("outside_write_targets"):
        return
    task_id = arguments.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return
    task = store.get("tasks", task_id)
    if not task:
        return
    record_nilo_issue_for_task(store, task["project_id"], task_id, f"mcp {tool_name}", error, boundary)


def call_tool(name: str, arguments: dict | None, db_path: Path | None = None) -> dict:
    if name not in TOOL_HANDLERS:
        raise McpToolError(f"unknown tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise McpToolError("tool arguments must be an object")
    try:
        context = resolve_mcp_workspace_context(db_path, name, arguments)
    except WorkspaceResolutionError as exc:
        if exc.error != "workspace_not_found":
            raise McpToolError(str(exc)) from exc
        return exc.response()
    store = Store(Path(context["db_path"]))
    try:
        identity_root = Path(context.get("project_root") or context.get("git_root") or Path.cwd())
        identity = mcp_identity(identity_root, store.path, source=context.get("source", ""))
        identity = {**identity, **context, "db_path": str(store.path.resolve())}
        boundary = resolve_project_boundary(identity_root, db_path=store.path)
        identity["project_boundary"] = boundary.to_dict()
        identity["project_boundary_warnings"] = boundary_warning_lines(boundary)
        expected_project = optional_string(arguments, "expected_project")
        expected_git_root = optional_string(arguments, "expected_git_root")
        matches, mismatch_reasons = identity_matches_expected(identity, expected_project, expected_git_root)
        identity_mismatch = None
        if not matches:
            if name in WRITE_FENCE_TOOL_NAMES:
                return repository_mismatch_response(identity, expected_project, expected_git_root)
            identity_mismatch = {
                "ok": False,
                "error": "repository_mismatch",
                "mode": "read_only_external_reference",
                "reasons": mismatch_reasons,
                "expected": {"project": expected_project, "git_root": expected_git_root},
                "actual": {
                    "project_id": identity.get("project_id", ""),
                    "repository_name": identity.get("repository_name", ""),
                    "git_root": identity.get("git_root", ""),
                    "db_path": identity.get("db_path", ""),
                },
                "message": "Identity mismatch detected; read-only MCP inspection is allowed, writes remain blocked.",
            }
            identity["identity_mismatch"] = identity_mismatch
        if name in WRITE_FENCE_TOOL_NAMES:
            try:
                require_write_fence(boundary)
            except ProjectBoundaryError as exc:
                maybe_record_mcp_boundary_failure(store, arguments, name, exc, boundary)
                return {
                    "ok": False,
                    "error": exc.code,
                    "message": str(exc),
                    "project_boundary": boundary.to_dict(),
                    "write_fence": exc.details,
                    "identity": identity,
                }
        handler_arguments = {**arguments, "__nilo_workspace_context": context}
        result = TOOL_HANDLERS[name](store, handler_arguments)
        if isinstance(result, dict) and "identity" not in result:
            result = {**result, "identity": identity}
        if isinstance(result, dict) and identity_mismatch is not None and "identity_mismatch" not in result:
            result = {**result, "identity_mismatch": identity_mismatch}
        return result
    finally:
        store.close()


def success_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(message: dict, db_path: Path | None = None) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return success_response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "nilo", "version": __version__},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        listed_tools = tools_for_list_params(params)
        return success_response(
            request_id,
            {
                "tools": listed_tools,
                "default_tool_count": len(default_tools()),
                "advanced_tool_count": len(TOOLS) - len(default_tools()),
                "review_handoff_tool_count": len(review_handoff_tools()),
            },
        )
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if not isinstance(name, str):
            return success_response(request_id, text_tool_result({"error": "missing tool name"}, is_error=True))
        try:
            result = call_tool(name, params.get("arguments") or {}, db_path)
        except McpToolError as exc:
            return success_response(request_id, text_tool_result({"error": str(exc)}, is_error=True))
        return success_response(request_id, text_tool_result(result))
    return error_response(request_id, -32601, f"method not found: {method}")


def serve_stdio(db_path: Path | None = None, input_stream: TextIO | None = None, output_stream: TextIO | None = None) -> None:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = error_response(None, -32700, f"parse error: {exc.msg}")
        else:
            if not isinstance(message, dict):
                response = error_response(None, -32600, "invalid request")
            else:
                response = handle_request(message, db_path)
        if response is None:
            continue
        output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        output_stream.flush()
