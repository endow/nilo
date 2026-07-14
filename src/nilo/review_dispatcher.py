from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cli_support import make_id
from .review import build_review_context, looks_like_review_result, parse_review_result
from .review_coordinator import ErrorClass, ReviewBackendError, ReviewContext, ReviewExecutionOutput, coordinate_review
from .review_errors import classify_provider_error
from .review_lifecycle import insert_review_request, set_review_request_status, update_review_request
from .reviewer_registry import canonical_reviewer_name, normalize_backend_kind, normalize_capabilities, reviewer_is_registered_available
from .secret import detect_secret_issues, mask_secrets
from .snapshot import compact_snapshot, current_git_snapshot
from .store import Store
from .timeutil import now_iso
from .transitions import TransitionError, import_review_result as transition_import_review_result


DISPATCH_TERMINAL_STATUSES = {"review_completed", "review_failed", "needs_reviewer_worker", "needs_reviewer_config"}
VALID_DISPATCH_VERDICTS = {"approved", "commented", "changes_requested", "rejected"}
WINDOWS_EXECUTABLE_EXTENSIONS = (".cmd", ".exe", ".bat", ".ps1")
DEFAULT_CLAUDE_REVIEW_PROMPT = (
    "You are acting as the claude-code reviewer through Nilo MCP. Read the review prompt at {prompt_file}. "
    "Review the current uncommitted changes only. Return exactly a Nilo markdown review result with sections: "
    "# ReviewResult, ## Verdict, ## Summary, ## Findings. Use one of these verdicts: approved, commented, "
    "changes_requested, rejected. Do not modify files."
)
DEFAULT_CODEX_REVIEW_PROMPT = (
    "You are acting as the codex reviewer through Nilo MCP. Read the review prompt at {prompt_file}. "
    "Review the current uncommitted changes only. Return exactly a Nilo markdown review result with sections: "
    "# ReviewResult, ## Verdict, ## Summary, ## Findings. Use one of these verdicts: approved, commented, "
    "changes_requested, rejected. Do not modify files."
)
LEGACY_DEFAULT_PROMPT_MARKERS = ("Voile MCP", "Voile markdown review result")
DEFAULT_QUICK_REVIEW_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ReviewerConfig:
    name: str
    kind: str
    command: str
    args: list[str]
    working_directory: str
    auto_start: bool
    timeout_seconds: float
    startup_timeout_seconds: float
    heartbeat_interval_seconds: float
    result_format: str
    dispatch_capable: bool
    capabilities: list[str]
    env: dict[str, str]
    persist_prompt_file: bool
    endpoint: str = ""
    model: str = ""
    api_key_env: str = ""
    confidence_threshold: float = 0.75
    local_cli_fallback: bool = False
    legacy_cli_fallback_config: bool = False


@dataclass(frozen=True)
class ResolvedCommand:
    command: list[str]
    executable: str
    preview: str


class DispatchError(Exception):
    def __init__(
        self,
        stage: str,
        reason: str,
        next_action: dict[str, Any],
        *,
        status: str = "review_failed",
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.next_action = next_action
        self.status = status
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def find_executable(command: str, env: dict[str, str] | None = None) -> str | None:
    def path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    if not command.strip():
        return None
    candidate = Path(command)
    if sys.platform == "win32":
        candidate_suffix = candidate.suffix.casefold()
        if candidate.parent != Path(".") or candidate.is_absolute():
            if candidate_suffix in WINDOWS_EXECUTABLE_EXTENSIONS:
                return str(candidate) if path_exists(candidate) else None
            if candidate_suffix:
                return None
            for suffix in WINDOWS_EXECUTABLE_EXTENSIONS:
                suffixed = candidate.with_name(candidate.name + suffix)
                if path_exists(suffixed):
                    return str(suffixed)
            return None

        path = (env or os.environ).get("PATH")
        search_dirs = (path or "").split(os.pathsep)
        lower_command = command.casefold()
        if lower_command.endswith(WINDOWS_EXECUTABLE_EXTENSIONS):
            for directory in search_dirs:
                if not directory:
                    continue
                direct = Path(directory) / command
                if path_exists(direct):
                    return str(direct)
            return None
        if Path(command).suffix:
            return None
        for directory in search_dirs:
            if not directory:
                continue
            for suffix in WINDOWS_EXECUTABLE_EXTENSIONS:
                executable = Path(directory) / f"{command}{suffix}"
                if path_exists(executable):
                    return str(executable)
        return None

    if candidate.parent != Path(".") or candidate.is_absolute():
        if path_exists(candidate):
            return str(candidate)
        return None
    path = (env or os.environ).get("PATH")
    search_dirs = (path or "").split(os.pathsep)
    suffixes = WINDOWS_EXECUTABLE_EXTENSIONS[:2]
    lower_command = command.casefold()
    for directory in search_dirs:
        if not directory:
            continue
        direct = Path(directory) / command
        if path_exists(direct):
            return str(direct)
        for suffix in suffixes:
            if lower_command.endswith(suffix):
                continue
            executable = Path(directory) / f"{command}{suffix}"
            if path_exists(executable):
                return str(executable)
    return None


def resolve_command_parts(command: list[str], env: dict[str, str] | None = None) -> ResolvedCommand:
    resolved = find_executable(command[0], env)
    if not resolved:
        message = f"command not found: {command[0]}"
        raise DispatchError(
            "command_resolution",
            message,
            {"type": "fix_reviewer_command", "command": command[0]},
            stderr=message,
        )
    resolved_suffix = Path(resolved).suffix.casefold()
    if sys.platform == "win32" and resolved_suffix in {".cmd", ".bat"}:
        shell = windows_command_shell(env)
        if not shell:
            message = f"cmd.exe executable not found for reviewer command shim: {resolved}"
            raise DispatchError(
                "command_resolution",
                message,
                {"type": "fix_reviewer_command", "command": command[0]},
                stderr=message,
            )
        normalized = [shell, "/d", "/c", "call", resolved, *command[1:]]
    if sys.platform == "win32" and resolved.lower().endswith(".ps1"):
        powershell = find_executable("powershell.exe", env) or find_executable("pwsh.exe", env)
        if not powershell:
            message = f"PowerShell executable not found for reviewer script: {resolved}"
            raise DispatchError(
                "command_resolution",
                message,
                {"type": "fix_reviewer_command", "command": command[0]},
                stderr=message,
            )
        normalized = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved, *command[1:]]
    elif not (sys.platform == "win32" and resolved_suffix in {".cmd", ".bat"}):
        normalized = [resolved, *command[1:]]
    return ResolvedCommand(
        command=normalized,
        executable=resolved,
        preview=" ".join(shlex.quote(part) for part in normalized),
    )


def windows_command_shell(env: dict[str, str] | None = None) -> str | None:
    environ = env or os.environ
    comspec = environ.get("ComSpec") or environ.get("COMSPEC")
    if comspec and Path(comspec).exists():
        return str(Path(comspec))
    return find_executable("cmd.exe", env)


def default_reviewer_command(reviewer: str) -> str:
    if reviewer == "claude-code":
        return "claude"
    if reviewer == "codex":
        return "codex"
    return reviewer


def safe_default_args(reviewer: str) -> list[str]:
    if reviewer == "claude-code":
        return ["-p", "--permission-mode", "dontAsk", "--output-format", "text", DEFAULT_CLAUDE_REVIEW_PROMPT]
    if reviewer == "codex":
        return ["exec", "--skip-git-repo-check", DEFAULT_CODEX_REVIEW_PROMPT]
    return ["{prompt_file}"]


def default_review_prompt(reviewer: str) -> str | None:
    if reviewer == "claude-code":
        return DEFAULT_CLAUDE_REVIEW_PROMPT
    if reviewer == "codex":
        return DEFAULT_CODEX_REVIEW_PROMPT
    return None


def normalize_legacy_default_args(reviewer: str, args: list[str]) -> tuple[list[str], bool]:
    replacement = default_review_prompt(reviewer)
    if replacement is None:
        return args, False
    normalized: list[str] = []
    changed = False
    for arg in args:
        if "{prompt_file}" in arg and any(marker in arg for marker in LEGACY_DEFAULT_PROMPT_MARKERS):
            normalized.append(replacement)
            changed = True
        else:
            normalized.append(arg)
    return normalized, changed


def safe_default_config(path: Path, reviewer: str) -> ReviewerConfig | None:
    if reviewer not in {"claude-code", "codex"}:
        return None
    command_name = default_reviewer_command(reviewer)
    command = find_executable(command_name)
    if not command:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# Local CLI reviewer process fallback. Prefer Nilo MCP dispatch_review for normal AI-to-AI review handoff.\n"
        f"[reviewers.{reviewer}]\n"
        'kind = "agent"\n'
        f"command = {json.dumps(command_name)}\n"
        f"args = {json.dumps(safe_default_args(reviewer))}\n"
        'working_directory = "{repo_root}"\n'
        "auto_start = true\n"
        "timeout_seconds = 600\n"
        "dispatch_capable = true\n"
        "local_cli_fallback = true\n"
        "persist_prompt_file = true\n"
    )
    if path.exists() and path.read_text(encoding="utf-8").strip():
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n" + body)
    else:
        path.write_text(body, encoding="utf-8")
    return load_reviewer_config(path, reviewer, auto_configure=False)


def reviewer_config_next_action(path: Path, reviewer: str) -> dict[str, Any]:
    command_name = default_reviewer_command(reviewer)
    auto_configure_available = find_executable(command_name) is not None
    return {
        "type": "create_reviewer_config",
        "reviewer": reviewer,
        "path": str(path),
        "auto_configure_available": auto_configure_available,
        "command": f"nilo review init --reviewer {reviewer}",
    }


def load_reviewer_config(path: Path, reviewer: str, *, auto_configure: bool = False) -> ReviewerConfig:
    if not path.exists():
        if auto_configure:
            config = safe_default_config(path, reviewer)
            if config:
                return config
        raise DispatchError(
            "reviewer_config",
            f"reviewer config not found: {path}",
            reviewer_config_next_action(path, reviewer),
            status="needs_reviewer_config",
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    reviewers = data.get("reviewers")
    if not isinstance(reviewers, dict):
        raise DispatchError(
            "reviewer_config",
            "reviewer config must contain a [reviewers] table",
            {"type": "fix_reviewer_config", "path": str(path)},
            status="needs_reviewer_config",
        )
    raw = reviewers.get(reviewer)
    if not isinstance(raw, dict):
        if auto_configure:
            config = safe_default_config(path, reviewer)
            if config:
                return config
        raise DispatchError(
            "reviewer_config",
            f"reviewer is not configured: {reviewer}",
            reviewer_config_next_action(path, reviewer),
            status="needs_reviewer_config",
        )
    kind = str(raw.get("kind") or "agent")
    command = str(raw.get("command") or "").strip()
    endpoint = str(raw.get("endpoint") or raw.get("base_url") or "").strip()
    model = str(raw.get("model") or "").strip()
    if kind in {"openai_compatible", "local_llm"}:
        if not endpoint:
            raise DispatchError(
                "reviewer_config",
                f"local reviewer endpoint is empty: {reviewer}",
                {"type": "fix_reviewer_config", "reviewer": reviewer},
                status="needs_reviewer_config",
            )
        if not model:
            raise DispatchError(
                "reviewer_config",
                f"local reviewer model is empty: {reviewer}",
                {"type": "fix_reviewer_config", "reviewer": reviewer},
                status="needs_reviewer_config",
            )
    elif not command:
        raise DispatchError(
            "reviewer_config",
            f"reviewer command is empty: {reviewer}",
            {"type": "fix_reviewer_command", "reviewer": reviewer},
            status="needs_reviewer_config",
        )
    args = raw.get("args") or []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise DispatchError(
            "reviewer_config",
            f"reviewer args must be a string array: {reviewer}",
            {"type": "fix_reviewer_config", "reviewer": reviewer},
            status="needs_reviewer_config",
        )
    args, legacy_cli_fallback_config = normalize_legacy_default_args(reviewer, args)
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise DispatchError(
            "reviewer_config",
            f"reviewer env must be an object: {reviewer}",
            {"type": "fix_reviewer_config", "reviewer": reviewer},
            status="needs_reviewer_config",
        )
    raw_capabilities = raw.get("capabilities") or ["review_diff"]
    if not isinstance(raw_capabilities, list) or any(not isinstance(item, str) for item in raw_capabilities):
        raise DispatchError(
            "reviewer_config",
            f"reviewer capabilities must be a string array: {reviewer}",
            {"type": "fix_reviewer_config", "reviewer": reviewer},
            status="needs_reviewer_config",
        )
    capabilities = normalize_capabilities(raw_capabilities) or ["review_diff"]
    return ReviewerConfig(
        name=reviewer,
        kind=kind,
        command=command,
        args=args,
        working_directory=str(raw.get("working_directory") or "{repo_root}"),
        auto_start=bool(raw.get("auto_start", False)),
        timeout_seconds=float(raw.get("timeout_seconds", 600)),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds", 30)),
        heartbeat_interval_seconds=float(raw.get("heartbeat_interval_seconds", 30)),
        result_format=str(raw.get("result_format") or "markdown_review"),
        dispatch_capable=bool(raw.get("dispatch_capable", True)),
        capabilities=capabilities,
        env={str(key): str(value) for key, value in env.items()},
        persist_prompt_file=bool(raw.get("persist_prompt_file", True)),
        endpoint=endpoint,
        model=model,
        api_key_env=str(raw.get("api_key_env") or ""),
        confidence_threshold=float(raw.get("confidence_threshold", 0.75)),
        local_cli_fallback=bool(raw.get("local_cli_fallback", True)),
        legacy_cli_fallback_config=legacy_cli_fallback_config,
    )


def init_reviewer_config(path: Path, reviewers: list[str], *, overwrite: bool = False) -> dict[str, Any]:
    created: list[str] = []
    skipped: list[str] = []
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    if overwrite:
        path.unlink(missing_ok=True)
        existing = ""
    for reviewer in reviewers:
        canonical = canonical_reviewer_name(reviewer)
        if f"[reviewers.{canonical}]" in existing:
            skipped.append(canonical)
            continue
        config = safe_default_config(path, canonical)
        if config:
            created.append(canonical)
            existing = path.read_text(encoding="utf-8")
        else:
            skipped.append(canonical)
    return {"path": str(path), "created": created, "skipped": skipped}


def doctor_reviewer_config(path: Path, reviewers: list[str] | None = None) -> dict[str, Any]:
    if reviewers is None:
        reviewers = configured_reviewer_names(path) or ["claude-code", "codex"]
    checks = []
    for reviewer in reviewers:
        canonical = canonical_reviewer_name(reviewer)
        command_name = default_reviewer_command(canonical)
        executable = find_executable(command_name)
        configured = False
        config_error = ""
        kind = ""
        endpoint = ""
        model = ""
        capabilities = ["review_diff"]
        legacy_cli_fallback_config = False
        if path.exists():
            try:
                config = load_reviewer_config(path, canonical, auto_configure=False)
                configured = True
                kind = config.kind
                endpoint = config.endpoint
                model = config.model
                capabilities = config.capabilities
                legacy_cli_fallback_config = config.legacy_cli_fallback_config
                if config.kind in {"openai_compatible", "local_llm"}:
                    command_name = config.kind
                    executable = endpoint
                else:
                    command_name = config.command
                    env = os.environ.copy()
                    env.update(config.env)
                    executable = find_executable(config.command, env)
            except DispatchError as exc:
                config_error = exc.reason
        else:
            config_error = f"reviewer config not found: {path}"
        if legacy_cli_fallback_config:
            next_action = {"type": "migrate_legacy_reviewer_config", "path": str(path), "reviewer": canonical}
        elif configured:
            next_action = {"type": "none"}
        else:
            next_action = reviewer_config_next_action(path, canonical)
        checks.append(
            {
                "reviewer": canonical,
                "command": command_name,
                "executable": executable or "",
                "resolved_executable": executable or "",
                "command_found": executable is not None,
                "configured": configured,
                "kind": kind or "agent",
                "backend_kind": normalize_backend_kind(kind, canonical),
                "capabilities": capabilities,
                "endpoint": endpoint,
                "model": model,
                "legacy_cli_fallback_config": legacy_cli_fallback_config,
                "config_error": config_error,
                "next_action": next_action,
            }
        )
    return {"path": str(path), "exists": path.exists(), "reviewers": checks}


def configured_reviewer_names(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return []
    reviewers = data.get("reviewers")
    if not isinstance(reviewers, dict):
        return []
    return sorted(str(name) for name in reviewers)


def masked_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return mask_secrets(value)


def render_template(value: str, variables: dict[str, str]) -> str:
    rendered = value
    for key, replacement in variables.items():
        rendered = rendered.replace("{" + key + "}", replacement)
    return rendered


def command_preview(config: ReviewerConfig, variables: dict[str, str] | None = None) -> str:
    variables = variables or {}
    if config.kind in {"openai_compatible", "local_llm"}:
        return f"{config.kind} {config.endpoint} model={config.model}"
    parts = [render_template(config.command, variables), *[render_template(arg, variables) for arg in config.args]]
    return " ".join(shlex.quote(part) for part in parts)


def render_command_parts(config: ReviewerConfig, variables: dict[str, str]) -> list[str]:
    if config.kind in {"openai_compatible", "local_llm"}:
        return []
    return [render_template(config.command, variables), *[render_template(arg, variables) for arg in config.args]]


def insert_dispatch(store: Store, row: dict[str, Any]) -> None:
    if "stdout" in row:
        row["stdout"] = masked_output(row["stdout"])
    if "stderr" in row:
        row["stderr"] = masked_output(row["stderr"])
    store.insert("review_dispatches", row)


def update_dispatch(store: Store, dispatch_id: str, **values: Any) -> None:
    if "stdout" in values:
        values["stdout"] = masked_output(values["stdout"])
    if "stderr" in values:
        values["stderr"] = masked_output(values["stderr"])
    values["updated_at"] = now_iso()
    store.update("review_dispatches", dispatch_id, values)


def resolve_task_project(store: Store, task_id: str, project_id: str | None) -> tuple[dict, str]:
    task = store.get("tasks", task_id)
    if not task:
        raise DispatchError("resolve_context", f"task not found: {task_id}", {"type": "fix_task_id", "task_id": task_id})
    resolved_project = project_id or task["project_id"]
    if task["project_id"] != resolved_project:
        raise DispatchError(
            "resolve_context",
            f"task does not belong to project {resolved_project}: {task_id}",
            {"type": "fix_project_or_task", "task_id": task_id, "project_id": resolved_project},
        )
    if not store.get("projects", resolved_project):
        raise DispatchError(
            "resolve_context",
            f"project not found: {resolved_project}",
            {"type": "fix_project_id", "project_id": resolved_project},
        )
    return task, resolved_project


def create_review_request(
    store: Store,
    task_id: str,
    actor: str,
    reviewer: str,
    reason: str,
    *,
    cwd: Path | None = None,
) -> dict:
    created_at = now_iso()
    latest_event = store.latest_task_status_event(task_id)
    snapshot = compact_snapshot(current_git_snapshot(cwd or Path.cwd()))
    row = {
        "id": make_id("review"),
        "task_id": task_id,
        "requester": actor,
        "reviewer": reviewer,
        "status": "requested" if reviewer_is_registered_available(store, reviewer) else "reviewer_unavailable",
        "reason": reason,
        "based_on_event_id": latest_event["event_id"] if latest_event else "",
        "based_on_snapshot": snapshot,
        "created_at": created_at,
        "updated_at": created_at,
    }
    insert_review_request(store, row)
    return row


def register_dispatch_reviewer(store: Store, reviewer: str, config: ReviewerConfig, command_line: str) -> dict:
    now = now_iso()
    rows = store.list_where("review_reviewers", "reviewer=?", (reviewer,))
    existing = max(rows, key=lambda row: row["last_heartbeat_at"]) if rows else None
    metadata = {
        "worker_path": "nilo review dispatch (CLI reviewer process fallback)",
        "dispatch_capable": config.dispatch_capable,
        "dispatch_capable_meaning": "local CLI reviewer process can be started by the fallback dispatcher; this is not the MCP dispatch_review tool",
        "local_cli_fallback": config.local_cli_fallback,
        "legacy_cli_fallback_config": config.legacy_cli_fallback_config,
        "source": "review_dispatcher",
        "command": command_line,
        "result_format": config.result_format,
        "reviewer_id": reviewer,
        "display_name": reviewer,
        "backend_kind": normalize_backend_kind(config.kind, reviewer),
        "capabilities": config.capabilities,
        "context_limits": {"timeout_seconds": config.timeout_seconds},
        "tool_access_limitations": ["local reviewer output cannot complete tasks directly"] if config.kind in {"openai_compatible", "local_llm"} else [],
        "evidence_requirements": [
            "command output",
            "tests",
            "diff inspection",
            "explicit human or trusted reviewer approval when required",
        ],
    }
    row = {
        "id": existing["id"] if existing else make_id("reviewer"),
        "reviewer": reviewer,
        "status": "available",
        "capabilities": config.capabilities,
        "max_concurrent": 1,
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
        return store.get("review_reviewers", existing["id"])
    store.insert("review_reviewers", row)
    return row


def claim_review(store: Store, review_request: dict) -> dict:
    if review_request["status"] != "requested":
        raise DispatchError(
            "review_claimed",
            f"review request is not claimable: {review_request['id']} [{review_request['status']}]",
            {"type": "retry_when_reviewer_available", "review_request_id": review_request["id"]},
        )
    updated_at = now_iso()
    return update_review_request(store, review_request["id"], {"status": "claimed", "updated_at": updated_at})


def build_prompt_file(store: Store, request: dict, repo_root: Path) -> tuple[Path, str]:
    task = store.get("tasks", request["task_id"])
    report = store.latest_for_task("agent_reports", task["id"])
    verification_run = store.latest_for_task("verification_runs", task["id"])
    prompt_md = build_review_context(task, request, report, None, verification_run, repo_root)
    prompt_path = repo_root / ".nilo" / "reviews" / f"{request['id']}_prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_md, encoding="utf-8")
    write_prompt_metadata(prompt_path, prompt_md)
    return prompt_path, prompt_md


def write_prompt_metadata(prompt_path: Path, prompt_md: str) -> Path:
    secret_warnings = detect_secret_issues(prompt_md)
    metadata = {
        "prompt_file": str(prompt_path),
        "storage_scope": "temporary reviewer handoff under .nilo/reviews",
        "masking_policy": "stdout/stderr and imported review results are secret-masked before DB storage; prompt handoff files keep raw reviewer context unless persist_prompt_file is false",
        "secret_detected": bool(secret_warnings),
        "secret_warnings": secret_warnings,
        "raw_prompt_persistence": "configurable with reviewer persist_prompt_file; set false to delete the raw handoff prompt after dispatch",
    }
    metadata_path = prompt_path.with_suffix(".metadata.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata_path


def resolve_reviewer_command(config: ReviewerConfig, variables: dict[str, str], env: dict[str, str]) -> ResolvedCommand:
    return resolve_command_parts(render_command_parts(config, variables), env)


def reviewer_process_context(
    config: ReviewerConfig,
    variables: dict[str, str],
    repo_root: Path,
) -> tuple[Path, dict[str, str], ResolvedCommand]:
    cwd = Path(render_template(config.working_directory, variables))
    if not cwd.is_absolute():
        cwd = repo_root / cwd
    env = os.environ.copy()
    env.update({key: render_template(value, variables) for key, value in config.env.items()})
    if config.kind in {"openai_compatible", "local_llm"}:
        return cwd, env, ResolvedCommand(command=[], executable=config.kind, preview=command_preview(config, variables))
    resolved = resolve_reviewer_command(config, variables, env)
    return cwd, env, resolved


def openai_chat_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def expected_local_response_schema() -> dict[str, Any]:
    return {
        "summary": "string",
        "findings": [
            {
                "title": "string",
                "severity": "critical|high|medium|low|info",
                "status": "unresolved|addressed|accepted-risk",
                "file_path": "string",
                "line": "string",
                "blocking": "boolean",
                "description": "string",
            }
        ],
        "confidence": "number from 0 to 1",
        "limitations": ["string"],
        "suggested_next_actions": ["string"],
    }


def local_reviewer_messages(prompt_md: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a Nilo local reviewer. Review the provided task context and diff. "
                "Return only JSON matching the expected schema. You may report limitations explicitly. "
                "Do not claim tests passed unless the prompt includes evidence."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task_context": prompt_md,
                    "review_instructions": [
                        "Find correctness, safety, test, and documentation issues.",
                        "Local AI output cannot mark a task complete.",
                        "Low confidence should be reflected in confidence and limitations.",
                    ],
                    "expected_response_schema": expected_local_response_schema(),
                },
                ensure_ascii=False,
            ),
        },
    ]


def run_local_reviewer(config: ReviewerConfig, env: dict[str, str], prompt_md: str) -> subprocess.CompletedProcess[str]:
    headers = {"Content-Type": "application/json"}
    if config.api_key_env:
        token = env.get(config.api_key_env, "")
        if not token:
            raise DispatchError(
                "reviewer_config",
                f"local reviewer API key env is not set: {config.api_key_env}",
                {"type": "fix_reviewer_config", "reviewer": config.name, "api_key_env": config.api_key_env},
                status="needs_reviewer_config",
            )
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "model": config.model,
        "messages": local_reviewer_messages(mask_secrets(prompt_md)),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        openai_chat_url(config.endpoint),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        stderr = exc.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess([config.kind, config.endpoint], exc.code, "", stderr)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DispatchError(
            "local_reviewer_connectivity",
            f"local reviewer connectivity failed: {exc}",
            {"type": "check_local_reviewer_endpoint", "reviewer": config.name, "endpoint": config.endpoint},
            stderr=str(exc),
        ) from None
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise DispatchError(
            "review_output_received",
            f"local reviewer response malformed: {exc}",
            {"type": "fix_local_reviewer_output", "reviewer": config.name},
            stdout=body,
        ) from None
    return subprocess.CompletedProcess([config.kind, config.endpoint], 0, local_review_json_to_markdown(content, config), "")


def local_review_json_to_markdown(content: str, config: ReviewerConfig) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {"summary": content.strip(), "findings": [], "confidence": 0.0, "limitations": ["local reviewer returned non-JSON content"], "suggested_next_actions": []}
    limitations = data.get("limitations") or []
    if not isinstance(limitations, list):
        limitations = [str(limitations)]
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
        limitations.append("local reviewer returned non-numeric confidence")
    next_actions = data.get("suggested_next_actions") or []
    if not isinstance(next_actions, list):
        next_actions = [str(next_actions)]
    findings = data.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    verdict = "commented"
    if findings:
        verdict = "changes_requested" if any(bool(item.get("blocking")) for item in findings if isinstance(item, dict)) else "commented"
    if confidence < config.confidence_threshold and "low confidence local review" not in limitations:
        limitations.append("low confidence local review")
    lines = [
        "# ReviewResult",
        "",
        "## Verdict",
        verdict,
        "",
        "## Summary",
        str(data.get("summary") or "Local reviewer returned no summary.").strip(),
        "",
        "## Local Reviewer Metadata",
        f"confidence: {confidence:g}",
        f"confidence_threshold: {config.confidence_threshold:g}",
        "limitations:",
        *[f"- {item}" for item in limitations],
        "suggested_next_actions:",
        *[f"- {item}" for item in next_actions],
        "",
        "## Findings",
    ]
    if not findings:
        lines.append("No findings.")
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            continue
        lines.extend(
            [
                f"### F{index}: {finding.get('title') or f'Finding {index}'}",
                f"severity: {finding.get('severity') or 'medium'}",
                f"status: {finding.get('status') or 'unresolved'}",
                f"file: {finding.get('file_path') or finding.get('file') or ''}",
                f"line: {finding.get('line') or ''}",
                f"blocking: {str(bool(finding.get('blocking'))).lower()}",
                "",
                str(finding.get("description") or "").strip(),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def run_reviewer_process(config: ReviewerConfig, cwd: Path, env: dict[str, str], resolved: ResolvedCommand) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            resolved.command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise DispatchError(
            "command_resolution",
            f"command not found: {resolved.command[0]}",
            {"type": "fix_reviewer_command", "reviewer": config.name},
            stderr=f"{resolved.command[0]}: {exc}",
        ) from None
    except OSError as exc:
        raise DispatchError(
            "reviewer_process_start",
            f"reviewer process could not be started: {exc}",
            {"type": "fix_reviewer_command", "reviewer": config.name, "command": resolved.preview},
            stderr=str(exc),
        ) from None
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(
            "reviewer_timeout",
            f"reviewer process timed out after {config.timeout_seconds:g} seconds",
            {"type": "reviewer_timeout", "reviewer": config.name, "action": "increase_timeout_or_fix_reviewer"},
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from None


def import_dispatch_review_result(store: Store, request: dict, reviewer: str, body_md: str, *, last_seen_event_id: str, cwd: Path | None = None) -> tuple[dict, list[dict]]:
    if request["status"] not in {"claimed", "in_progress", "running"}:
        raise DispatchError(
            "review_importing",
            f"review request must be claimed or in_progress before import: {request['id']} [{request['status']}]",
            {"type": "retry_dispatch", "review_request_id": request["id"]},
        )
    if not body_md.strip():
        raise DispatchError(
            "review_output_received",
            "reviewer process produced empty output",
            {"type": "fix_reviewer_output", "reviewer": reviewer},
        )
    if not looks_like_review_result(body_md):
        raise DispatchError(
            "review_output_received",
            "reviewer output malformed",
            {"type": "fix_reviewer_output", "reviewer": reviewer},
            stdout=body_md,
        )
    try:
        verdict, _summary, _findings = parse_review_result(body_md)
    except ValueError as exc:
        raise DispatchError(
            "review_output_received",
            str(exc),
            {"type": "fix_reviewer_output", "reviewer": reviewer},
            stdout=body_md,
        ) from exc
    if verdict not in VALID_DISPATCH_VERDICTS:
        raise DispatchError(
            "review_output_received",
            "reviewer output malformed",
            {"type": "fix_reviewer_output", "reviewer": reviewer},
            stdout=body_md,
        )
    try:
        transition = transition_import_review_result(
            store,
            request["task_id"],
            request["id"],
            body_md=body_md,
            reviewer=reviewer,
            last_seen_event_id=last_seen_event_id,
            cwd=cwd,
        )
    except TransitionError as exc:
        raise DispatchError("review_importing", exc.message, {"type": "retry_dispatch", "review_request_id": request["id"]}) from exc
    result = store.get("review_results", transition.created_ids["review_result"])
    stored_findings = store.list_where("review_findings", "review_result_id=?", (result["id"],))
    return result, stored_findings


class DirectReviewerAdapter:
    transport = "direct_cli"

    def __init__(self, store: Store, config: ReviewerConfig, *, actor: str, repo_root: Path) -> None:
        self.store = store
        self.config = config
        self.actor = actor
        self.repo_root = repo_root
        self.reviewer = canonical_reviewer_name(config.name)
        self.backend_kind = normalize_backend_kind(config.kind, self.reviewer)
        self.result: dict[str, Any] | None = None
        self.findings: list[dict[str, Any]] = []
        self._prompt_path: Path | None = None
        self._last_seen_event_id = ""

    def _variables(self, context: ReviewContext, prompt_path: Path) -> dict[str, str]:
        task = self.store.get("tasks", context.task_id)
        return {
            "repo_root": str(self.repo_root),
            "prompt_file": str(prompt_path),
            "task_id": context.task_id,
            "project_id": task["project_id"],
            "review_id": context.review_request_id,
            "reviewer": self.reviewer,
            "actor": self.actor,
        }

    def readiness(self, context: ReviewContext) -> bool:
        if self.config.kind in {"openai_compatible", "local_llm"}:
            return True
        variables = self._variables(context, self.repo_root / ".nilo" / "reviews" / f"{context.review_request_id}_prompt.md")
        try:
            reviewer_process_context(self.config, variables, self.repo_root)
        except DispatchError as exc:
            raise ReviewBackendError(ErrorClass.CONFIGURATION, exc.reason, diagnostics={"stderr": exc.stderr}) from exc
        return True

    def execute(self, context: ReviewContext) -> ReviewExecutionOutput:
        request = self.store.get("review_requests", context.review_request_id)
        self._prompt_path, prompt_md = build_prompt_file(self.store, request, self.repo_root)
        latest_event = self.store.latest_task_status_event(context.task_id)
        self._last_seen_event_id = latest_event["event_id"] if latest_event else ""
        variables = self._variables(context, self._prompt_path)
        try:
            cwd, env, resolved = reviewer_process_context(self.config, variables, self.repo_root)
            if self.config.kind in {"openai_compatible", "local_llm"}:
                process = run_local_reviewer(self.config, env, prompt_md)
            else:
                process = run_reviewer_process(self.config, cwd, env, resolved)
        except DispatchError as exc:
            error_class = ErrorClass.TIMEOUT if exc.stage == "reviewer_timeout" else ErrorClass.TRANSPORT
            raise ReviewBackendError(error_class, exc.reason, diagnostics={"stdout": exc.stdout, "stderr": exc.stderr}) from exc
        finally:
            if self._prompt_path and not self.config.persist_prompt_file:
                self._prompt_path.unlink(missing_ok=True)
        classified = classify_provider_error(
            self.reviewer,
            stdout=process.stdout,
            stderr=process.stderr,
            exit_code=process.returncode,
        )
        if classified:
            raise ReviewBackendError(
                classified.error_class,
                classified.message,
                error_code=classified.error_code,
                retry_after=classified.retry_after,
                diagnostics={"stdout": process.stdout, "stderr": process.stderr},
            )
        if process.returncode != 0:
            raise ReviewBackendError(
                ErrorClass.TRANSPORT,
                f"reviewer process exited with code {process.returncode}",
                error_code=str(process.returncode),
                diagnostics={"stdout": process.stdout, "stderr": process.stderr},
            )
        return ReviewExecutionOutput(process.stdout, {"stderr": process.stderr, "exit_code": process.returncode})

    def finalize(self, store: Store, context: ReviewContext, output: ReviewExecutionOutput) -> None:
        request = store.get("review_requests", context.review_request_id)
        try:
            self.result, self.findings = import_dispatch_review_result(
                store,
                request,
                self.reviewer,
                output.body,
                last_seen_event_id=self._last_seen_event_id,
                cwd=self.repo_root,
            )
        except DispatchError as exc:
            raise ReviewBackendError(ErrorClass.INVALID_OUTPUT, exc.reason, diagnostics={"stdout": exc.stdout, "stderr": exc.stderr}) from exc

    def cancel(self, attempt_id: str) -> None:
        return None


class ReviewAdapterRegistry:
    PROVIDERS = frozenset({"claude-code", "codex", "grok"})

    def create_direct(
        self,
        store: Store,
        *,
        reviewer: str,
        actor: str,
        repo_root: Path,
        config_path: Path | None = None,
    ) -> DirectReviewerAdapter:
        reviewer = canonical_reviewer_name(reviewer)
        if reviewer not in self.PROVIDERS:
            raise DispatchError(
                "reviewer_config",
                f"unsupported direct reviewer: {reviewer}",
                {"type": "configure_supported_reviewer", "reviewer": reviewer, "supported": sorted(self.PROVIDERS)},
                status="needs_reviewer_config",
            )
        effective_path = config_path or repo_root / ".nilo" / "reviewers.toml"
        config = load_reviewer_config(effective_path, reviewer, auto_configure=config_path is None)
        return DirectReviewerAdapter(store, config, actor=actor, repo_root=repo_root)


DIRECT_REVIEW_ADAPTERS = ReviewAdapterRegistry()


def dispatch_review_direct(
    store: Store,
    *,
    actor: str,
    reviewer: str,
    task_id: str,
    reason: str = "direct agent review",
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path.cwd()
    reviewer = canonical_reviewer_name(reviewer)
    adapter = DIRECT_REVIEW_ADAPTERS.create_direct(
        store,
        reviewer=reviewer,
        actor=actor,
        repo_root=repo_root,
        config_path=config_path,
    )
    coordinated = coordinate_review(store, task_id=task_id, requester=actor, reason=reason, adapter=adapter, cwd=repo_root)
    return {
        "status": coordinated.status,
        "task_id": task_id,
        "reviewer": reviewer,
        "review_request_id": coordinated.review_request["id"],
        "review_attempt_id": coordinated.review_attempt["id"],
        "error_class": coordinated.review_attempt.get("error_class", ""),
        "retry_after": coordinated.review_attempt.get("retry_after", ""),
        "result": adapter.result,
        "findings": adapter.findings,
    }


def close_superseded_pending_reviews(store: Store, task_id: str, reviewer: str, completed_review_id: str, actor: str) -> list[str]:
    now = now_iso()
    closed: list[str] = []
    rows = store.list_where(
        "review_requests",
        "task_id=? AND reviewer=? AND status IN ('requested', 'reviewer_unavailable', 'claimed', 'in_progress', 'stale')",
        (task_id, reviewer),
    )
    for request in rows:
        if request["id"] == completed_review_id:
            continue
        update_review_request(
            store,
            request["id"],
            {
                "status": "superseded",
                "updated_at": now,
                "withdrawn_reason": f"superseded by completed dispatch review {completed_review_id}",
                "withdrawn_actor": actor,
                "withdrawn_at": now,
            },
        )
        closed.append(request["id"])
    return closed


def close_previous_active_reviews(store: Store, task_id: str, reviewer: str, keep_review_id: str, actor: str, reason: str) -> list[str]:
    now = now_iso()
    closed: list[str] = []
    rows = store.list_where(
        "review_requests",
        "task_id=? AND reviewer=? AND id<>? AND status IN ('requested', 'reviewer_unavailable', 'claimed', 'in_progress', 'stale')",
        (task_id, reviewer, keep_review_id),
    )
    for request in rows:
        update_review_request(
            store,
            request["id"],
            {
                "status": "superseded",
                "updated_at": now,
                "withdrawn_reason": reason,
                "withdrawn_actor": actor,
                "withdrawn_at": now,
            },
        )
        closed.append(request["id"])
    return closed


def next_action_for_result(verdict: str, findings: list[dict]) -> dict[str, Any]:
    blocking = [finding for finding in findings if finding["blocking"] and finding["status"] == "unresolved"]
    if blocking:
        return {"type": "address_blocking_findings", "finding_ids": [finding["id"] for finding in blocking]}
    if verdict == "approved":
        return {"type": "ready_to_complete_task"}
    if findings:
        return {"type": "review_non_blocking_findings", "finding_ids": [finding["id"] for finding in findings]}
    return {"type": "review_comments"}


def result_payload(
    *,
    status: str,
    actor: str,
    reviewer: str,
    task_id: str,
    project_id: str,
    dispatch_id: str,
    review_request_id: str = "",
    result: dict | None = None,
    findings: list[dict] | None = None,
    next_action: dict[str, Any] | None = None,
    failure_stage: str = "",
    reason: str = "",
    command: str = "",
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
) -> dict[str, Any]:
    findings = findings or []
    blocking = [finding for finding in findings if finding["blocking"] and finding["status"] == "unresolved"]
    non_blocking = [finding for finding in findings if not finding["blocking"]]
    payload: dict[str, Any] = {
        "status": status,
        "operation": "cli_reviewer_process_fallback",
        "mcp_preferred_tool": "dispatch_review",
        "fallback_note": "Use this local CLI reviewer process only when the Nilo MCP review workflow is unavailable.",
        "actor": actor,
        "reviewer": reviewer,
        "task_id": task_id,
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "review_request_id": review_request_id,
        "next_action": next_action or {},
    }
    if result:
        payload.update(
            {
                "verdict": result["verdict"],
                "blocking_findings": len(blocking),
                "non_blocking_findings": len(non_blocking),
                "summary": result["summary"],
                "review_result_id": result["id"],
            }
        )
    if status != "review_completed":
        payload.update(
            {
                "failure_stage": failure_stage,
                "reason": reason,
                "command": command,
                "stdout": masked_output(stdout),
                "stderr": masked_output(stderr),
                "exit_code": exit_code,
            }
        )
    return payload


def dispatch_review(
    store: Store,
    *,
    actor: str,
    reviewer: str,
    task_id: str,
    project_id: str | None = None,
    reason: str = "dispatched agent review",
    auto_start: bool | None = None,
    auto_configure: bool = True,
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path.cwd()
    reviewer = canonical_reviewer_name(reviewer)
    task, resolved_project = resolve_task_project(store, task_id, project_id)
    dispatch_id = make_id("review_dispatch")
    created_at = now_iso()
    effective_config_path = config_path or repo_root / ".nilo" / "reviewers.toml"
    try:
        config = load_reviewer_config(effective_config_path, reviewer, auto_configure=auto_configure and config_path is None)
    except DispatchError as exc:
        insert_dispatch(
            store,
            {
                "id": dispatch_id,
                "actor": actor,
                "reviewer": reviewer,
                "task_id": task["id"],
                "project_id": resolved_project,
                "review_request_id": "",
                "status": exc.status,
                "command": "",
                "args": [],
                "working_directory": "",
                "exit_code": exc.exit_code,
                "stdout": masked_output(exc.stdout),
                "stderr": masked_output(exc.stderr),
                "failure_stage": exc.stage,
                "failure_reason": exc.reason,
                "created_at": created_at,
                "updated_at": created_at,
            },
        )
        return result_payload(
            status=exc.status,
            actor=actor,
            reviewer=reviewer,
            task_id=task["id"],
            project_id=resolved_project,
            dispatch_id=dispatch_id,
            failure_stage=exc.stage,
            reason=exc.reason,
            stdout=masked_output(exc.stdout),
            stderr=masked_output(exc.stderr),
            exit_code=exc.exit_code,
            next_action=exc.next_action,
        )

    effective_auto_start = config.auto_start if auto_start is None else auto_start
    command_line = command_preview(config)
    insert_dispatch(
        store,
        {
            "id": dispatch_id,
            "actor": actor,
            "reviewer": reviewer,
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_request_id": "",
            "status": "dispatch_started",
            "command": config.command,
            "args": config.args,
            "working_directory": config.working_directory,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "failure_stage": "",
            "failure_reason": "",
            "created_at": created_at,
            "updated_at": created_at,
        },
    )

    try:
        initial_variables = {
            "repo_root": str(repo_root),
            "prompt_file": "",
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_id": "",
            "reviewer": reviewer,
            "actor": actor,
        }
        _initial_cwd, _initial_env, resolved_initial = reviewer_process_context(config, initial_variables, repo_root)
        update_dispatch(store, dispatch_id, status="command_resolved", command=resolved_initial.executable)
        command_line = resolved_initial.preview
        if not reviewer_is_registered_available(store, reviewer):
            if not effective_auto_start:
                update_dispatch(
                    store,
                    dispatch_id,
                    status="needs_reviewer_worker",
                    failure_stage="reviewer_available",
                    failure_reason="auto_start is disabled for this reviewer",
                )
                return result_payload(
                    status="needs_reviewer_worker",
                    actor=actor,
                    reviewer=reviewer,
                    task_id=task["id"],
                    project_id=resolved_project,
                    dispatch_id=dispatch_id,
                    failure_stage="reviewer_available",
                    reason="auto_start is disabled for this reviewer",
                    command=command_line,
                    stdout="",
                    stderr="",
                    exit_code=None,
                    next_action={"type": "start_reviewer_worker", "reviewer": reviewer, "command": command_line},
                )
            update_dispatch(store, dispatch_id, status="reviewer_starting")
            register_dispatch_reviewer(store, reviewer, config, command_line)
        update_dispatch(store, dispatch_id, status="reviewer_available")

        request = create_review_request(store, task["id"], actor, reviewer, reason, cwd=repo_root)
        update_dispatch(store, dispatch_id, status="review_requested", review_request_id=request["id"])
        close_previous_active_reviews(
            store,
            task["id"],
            reviewer,
            request["id"],
            actor,
            f"superseded by newer dispatch review {request['id']}",
        )
        request = claim_review(store, request)
        update_dispatch(store, dispatch_id, status="review_claimed")

        prompt_path, prompt_md = build_prompt_file(store, request, repo_root)
        variables = {
            "repo_root": str(repo_root),
            "prompt_file": str(prompt_path),
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_id": request["id"],
            "reviewer": reviewer,
            "actor": actor,
        }
        command_line = command_preview(config, variables)
        request = set_review_request_status(store, request["id"], "in_progress")
        import_seen_event = store.latest_task_status_event(request["task_id"])
        cwd, env, resolved = reviewer_process_context(config, variables, repo_root)
        command_line = resolved.preview
        update_dispatch(store, dispatch_id, status="review_running", command=resolved.executable, args=resolved.command[1:])
        try:
            if config.kind in {"openai_compatible", "local_llm"}:
                process = run_local_reviewer(config, env, prompt_md)
            else:
                process = run_reviewer_process(config, cwd, env, resolved)
        finally:
            if not config.persist_prompt_file:
                prompt_path.unlink(missing_ok=True)
        if process.returncode != 0:
            raise DispatchError(
                "review_running",
                f"reviewer process exited with code {process.returncode}",
                {"type": "fix_reviewer_command", "reviewer": reviewer},
                exit_code=process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
            )
        update_dispatch(store, dispatch_id, status="review_output_received", exit_code=process.returncode, stdout=process.stdout, stderr=process.stderr)
        result, findings = import_dispatch_review_result(
            store,
            request,
            reviewer,
            process.stdout,
            last_seen_event_id=import_seen_event["event_id"] if import_seen_event else "",
            cwd=repo_root,
        )
        update_dispatch(store, dispatch_id, status="review_imported")
        refreshed = store.get("review_requests", request["id"])
        if not refreshed or refreshed["status"] != "completed":
            raise DispatchError(
                "review_completed",
                f"review request did not reach completed: {request['id']}",
                {"type": "inspect_review_status", "review_request_id": request["id"]},
            )
        superseded = close_superseded_pending_reviews(store, task["id"], reviewer, request["id"], actor)
        update_dispatch(store, dispatch_id, status="review_completed")
        next_action = next_action_for_result(result["verdict"], findings)
        if superseded:
            next_action["superseded_review_request_ids"] = superseded
        return result_payload(
            status="review_completed",
            actor=actor,
            reviewer=reviewer,
            task_id=task["id"],
            project_id=resolved_project,
            dispatch_id=dispatch_id,
            review_request_id=request["id"],
            result=result,
            findings=findings,
            next_action=next_action,
        )
    except DispatchError as exc:
        update_dispatch(
            store,
            dispatch_id,
            status=exc.status,
            exit_code=exc.exit_code,
            stdout=masked_output(exc.stdout),
            stderr=masked_output(exc.stderr),
            failure_stage=exc.stage,
            failure_reason=exc.reason,
        )
        request_id = ""
        dispatch = store.get("review_dispatches", dispatch_id)
        if dispatch:
            request_id = dispatch.get("review_request_id") or ""
        if request_id:
            request = store.get("review_requests", request_id)
            if request and request["status"] not in {"completed", "withdrawn", "failed"}:
                update_review_request(
                    store,
                    request_id,
                    {
                        "status": "failed",
                        "updated_at": now_iso(),
                        "withdrawn_reason": exc.reason,
                        "withdrawn_actor": actor,
                        "withdrawn_at": now_iso(),
                    },
                )
        return result_payload(
            status=exc.status,
            actor=actor,
            reviewer=reviewer,
            task_id=task["id"],
            project_id=resolved_project,
            dispatch_id=dispatch_id,
            review_request_id=request_id,
            failure_stage=exc.stage,
            reason=exc.reason,
            command=command_line,
            stdout=masked_output(exc.stdout),
            stderr=masked_output(exc.stderr),
            exit_code=exc.exit_code,
            next_action=exc.next_action,
        )


def create_quick_review_request(
    store: Store,
    task_id: str,
    actor: str,
    reviewer: str,
    reason: str,
    *,
    cwd: Path | None = None,
) -> dict:
    created_at = now_iso()
    latest_event = store.latest_task_status_event(task_id)
    snapshot = compact_snapshot(current_git_snapshot(cwd or Path.cwd()))
    row = {
        "id": make_id("review"),
        "task_id": task_id,
        "requester": actor,
        "reviewer": reviewer,
        "status": "in_progress",
        "reason": reason,
        "based_on_event_id": latest_event["event_id"] if latest_event else "",
        "based_on_snapshot": snapshot,
        "created_at": created_at,
        "updated_at": created_at,
    }
    insert_review_request(store, row)
    return row


def fail_quick_review_request(store: Store, request: dict, actor: str, reason: str) -> None:
    update_review_request(
        store,
        request["id"],
        {
            "status": "failed",
            "updated_at": now_iso(),
            "withdrawn_reason": reason,
            "withdrawn_actor": actor,
            "withdrawn_at": now_iso(),
        },
    )


def ephemeral_quick_review_request(task_id: str, actor: str, reviewer: str, reason: str) -> dict:
    return {
        "id": "quick_review",
        "task_id": task_id,
        "requester": actor,
        "reviewer": reviewer,
        "status": "quick",
        "reason": reason,
    }


def quick_review(
    store: Store,
    *,
    actor: str,
    reviewer: str,
    task_id: str,
    project_id: str | None = None,
    reason: str = "quick agent review",
    should_import: bool = True,
    timeout_seconds: float = DEFAULT_QUICK_REVIEW_TIMEOUT_SECONDS,
    auto_configure: bool = True,
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path.cwd()
    reviewer = canonical_reviewer_name(reviewer)
    task, resolved_project = resolve_task_project(store, task_id, project_id)
    effective_config_path = config_path or repo_root / ".nilo" / "reviewers.toml"
    request = create_quick_review_request(store, task["id"], actor, reviewer, reason, cwd=repo_root) if should_import else ephemeral_quick_review_request(task["id"], actor, reviewer, reason)
    import_seen_event = store.latest_task_status_event(task["id"]) if should_import else None
    prompt_path: Path | None = None
    try:
        config = load_reviewer_config(effective_config_path, reviewer, auto_configure=auto_configure and config_path is None)
        config = replace(config, timeout_seconds=min(config.timeout_seconds, timeout_seconds))
        prompt_path, prompt_md = build_prompt_file(store, request, repo_root)
        variables = {
            "repo_root": str(repo_root),
            "prompt_file": str(prompt_path),
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_id": request["id"],
            "reviewer": reviewer,
            "actor": actor,
        }
        cwd, env, resolved = reviewer_process_context(config, variables, repo_root)
        if config.kind in {"openai_compatible", "local_llm"}:
            process = run_local_reviewer(config, env, prompt_md)
        else:
            process = run_reviewer_process(config, cwd, env, resolved)
        stdout = masked_output(process.stdout)
        stderr = masked_output(process.stderr)
        if process.returncode != 0:
            if should_import:
                fail_quick_review_request(store, request, actor, f"quick reviewer process exited with code {process.returncode}")
            return {
                "status": "review_failed",
                "actor": actor,
                "reviewer": reviewer,
                "task_id": task["id"],
                "project_id": resolved_project,
                "review_request_id": request["id"] if should_import else "",
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": process.returncode,
                "imported": False,
                "reason": f"reviewer process exited with code {process.returncode}",
            }
        if not should_import:
            return {
                "status": "raw_review",
                "actor": actor,
                "reviewer": reviewer,
                "task_id": task["id"],
                "project_id": resolved_project,
                "review_request_id": "",
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": process.returncode,
                "imported": False,
                "reason": "import disabled",
            }
        if not looks_like_review_result(process.stdout):
            fail_quick_review_request(store, request, actor, "quick review output was not importable")
            return {
                "status": "raw_review",
                "actor": actor,
                "reviewer": reviewer,
                "task_id": task["id"],
                "project_id": resolved_project,
                "review_request_id": request["id"],
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": process.returncode,
                "imported": False,
                "reason": "reviewer output was not a parseable ReviewResult",
            }
        try:
            result, findings = import_dispatch_review_result(
                store,
                request,
                reviewer,
                process.stdout,
                last_seen_event_id=import_seen_event["event_id"] if import_seen_event else "",
                cwd=repo_root,
            )
        except DispatchError as exc:
            fail_quick_review_request(store, request, actor, exc.reason)
            return {
                "status": "raw_review",
                "actor": actor,
                "reviewer": reviewer,
                "task_id": task["id"],
                "project_id": resolved_project,
                "review_request_id": request["id"],
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": process.returncode,
                "imported": False,
                "reason": exc.reason,
            }
        return {
            "status": "review_imported",
            "actor": actor,
            "reviewer": reviewer,
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_request_id": request["id"],
            "review_result_id": result["id"],
            "verdict": result["verdict"],
            "blocking_findings": len([finding for finding in findings if finding["blocking"] and finding["status"] == "unresolved"]),
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": process.returncode,
            "imported": True,
            "reason": "",
        }
    except DispatchError as exc:
        if should_import:
            fail_quick_review_request(store, request, actor, exc.reason)
        return {
            "status": exc.status,
            "actor": actor,
            "reviewer": reviewer,
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_request_id": request["id"] if should_import else "",
            "stdout": masked_output(exc.stdout),
            "stderr": masked_output(exc.stderr),
            "exit_code": exc.exit_code,
            "imported": False,
            "reason": exc.reason,
        }
    except Exception as exc:
        reason = f"quick review failed: {exc}"
        if should_import:
            fail_quick_review_request(store, request, actor, reason)
        return {
            "status": "review_failed",
            "actor": actor,
            "reviewer": reviewer,
            "task_id": task["id"],
            "project_id": resolved_project,
            "review_request_id": request["id"] if should_import else "",
            "stdout": "",
            "stderr": masked_output(str(exc)),
            "exit_code": None,
            "imported": False,
            "reason": reason,
        }
    finally:
        if prompt_path and not should_import:
            prompt_path.unlink(missing_ok=True)
