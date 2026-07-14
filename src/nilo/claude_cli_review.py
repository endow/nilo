from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .review import build_review_context, build_review_result_template, extract_review_result_body, looks_like_review_result, parse_review_result
from .review_dispatcher import (
    DispatchError,
    create_quick_review_request,
    fail_quick_review_request,
    find_executable,
    import_dispatch_review_result,
    next_action_for_result,
    resolve_command_parts,
    resolve_task_project,
)
from .review_lifecycle import set_review_request_status
from .secret import mask_secrets
from .store import Store


CLAUDE_REVIEWER = "claude-code"
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 600.0
VALID_CLAUDE_VERDICTS = {"approved", "commented", "changes_requested", "rejected"}


@dataclass(frozen=True)
class ClaudeCommand:
    command: list[str]
    executable: str
    preview: str


def strict_review_result(markdown: str) -> bool:
    text = markdown.strip()
    return bool(re.match(r"(?i)^#\s*ReviewResult\s*$", text.splitlines()[0] if text else "")) and looks_like_review_result(text)


def build_claude_review_prompt(context: str, template: str, *, with_mcp: bool) -> str:
    optional_mcp = ""
    if with_mcp:
        optional_mcp = """
## Optional MCP tools

Nilo MCP tools may be available for reference if needed.
Use them only to inspect context.
Return the final ReviewResult to stdout.
Do not rely on import_review_result as the primary submission path.
Do not modify files.
"""
    return f"""# Nilo Claude CLI Review

You are reviewing a Nilo task.
Do not modify files.
Return only a Nilo ReviewResult markdown document.

## Required output

# ReviewResult

## Verdict
approved | commented | changes_requested | rejected

## Summary
...

## Findings
...

## Rules

- Review only the provided task context and current diff context.
- Do not claim tests passed unless evidence is included.
- Do not call Nilo import tools as the primary submission path.
- If MCP tools are available, use them only for context inspection.
- Final answer must be the ReviewResult markdown on stdout.
{optional_mcp}
## Review context

{context}

## Review template

{template}
"""


def resolve_claude_command(*, permission_mode: str, with_mcp: bool, mcp_config: Path) -> ClaudeCommand:
    def parts_for(command: list[str]) -> list[str]:
        parts = [*command, "-p"]
        if with_mcp:
            parts.extend(["--mcp-config", str(mcp_config)])
        if permission_mode:
            parts.extend(["--permission-mode", permission_mode])
        return parts

    if find_executable("claude"):
        resolved = resolve_command_parts(parts_for(["claude"]))
        return ClaudeCommand(command=resolved.command, executable=resolved.executable, preview=resolved.preview)
    if find_executable("rtk"):
        resolved = resolve_command_parts(parts_for(["rtk", "proxy", "claude"]))
        return ClaudeCommand(command=resolved.command, executable=resolved.executable, preview=resolved.preview)
    raise DispatchError(
        "command_resolution",
        "command not found: claude",
        {"type": "install_or_configure_claude_cli", "command": "claude"},
        stderr="command not found: claude",
    )


def claude_command_preview(*, permission_mode: str, with_mcp: bool, mcp_config: Path) -> str:
    parts = ["claude", "-p"]
    if with_mcp:
        parts.extend(["--mcp-config", str(mcp_config)])
    if permission_mode:
        parts.extend(["--permission-mode", permission_mode])
    return " ".join(shlex.quote(part) for part in parts)


def review_failed_payload(
    *,
    task_id: str,
    project_id: str,
    mcp_config: Path,
    with_mcp: bool,
    reason: str,
    failure_stage: str,
    command: str = "",
    review_request_id: str = "",
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    output_file: Path | None = None,
    next_action: str = "run nilo review claude-doctor",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "review_failed",
        "failure_stage": failure_stage,
        "reason": reason,
        "claude_exit_code": exit_code,
        "stderr": mask_secrets(stderr),
        "stdout": mask_secrets(stdout),
        "command": command,
        "review_request_id": review_request_id,
        "task_id": task_id,
        "project_id": project_id,
        "mcp": "enabled" if with_mcp else "disabled",
        "mcp_config": str(mcp_config),
        "next_action": next_action,
    }
    if output_file:
        payload["output_file"] = str(output_file)
    return payload


def run_claude(command: ClaudeCommand, prompt: str, *, timeout_seconds: float, repo_root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command.command,
            input=prompt,
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(
            "reviewer_timeout",
            f"claude process timed out after {timeout_seconds:g} seconds",
            {"type": "increase_timeout_or_fix_claude_cli"},
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        ) from None
    except OSError as exc:
        raise DispatchError(
            "reviewer_process_start",
            f"claude process could not be started: {exc}",
            {"type": "fix_claude_command"},
            stderr=str(exc),
        ) from None


def default_prompt_path(repo_root: Path, review_id: str) -> Path:
    return repo_root / ".nilo" / "reviews" / f"{review_id}_claude_prompt.md"


def default_result_path(repo_root: Path, review_id: str) -> Path:
    return repo_root / ".nilo" / "reviews" / f"{review_id}_claude_result.md"


def write_text(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def dry_run_claude_review(
    store: Store,
    *,
    task_id: str,
    project_id: str | None,
    reason: str,
    permission_mode: str,
    with_mcp: bool,
    mcp_config: Path,
    write_prompt: bool,
    output_file: Path | None,
    no_import: bool,
    repo_root: Path,
) -> dict[str, Any]:
    task, resolved_project = resolve_task_project(store, task_id, project_id)
    prompt_path = repo_root / ".nilo" / "reviews" / "<review_id>_claude_prompt.md" if write_prompt else None
    command_resolution = "resolved"
    try:
        command_preview = resolve_claude_command(permission_mode=permission_mode, with_mcp=with_mcp, mcp_config=mcp_config).preview
    except DispatchError as exc:
        command_preview = claude_command_preview(permission_mode=permission_mode, with_mcp=with_mcp, mcp_config=mcp_config)
        command_resolution = f"unavailable: {exc.reason}"
    return {
        "status": "dry_run",
        "task_id": task["id"],
        "project_id": resolved_project,
        "reviewer": CLAUDE_REVIEWER,
        "reason": reason,
        "command": command_preview,
        "command_resolution": command_resolution,
        "mcp": "enabled" if with_mcp else "disabled",
        "mcp_config": str(mcp_config),
        "prompt_file": str(prompt_path) if prompt_path else "",
        "output_file": str(output_file) if output_file else "",
        "import": not no_import,
    }


def claude_review(
    store: Store,
    *,
    task_id: str,
    project_id: str | None = None,
    requester: str = "nilo",
    reason: str = "claude cli review",
    timeout_seconds: float = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    permission_mode: str = "bypassPermissions",
    with_mcp: bool = False,
    mcp_config: Path | None = None,
    write_prompt: bool = False,
    output_file: Path | None = None,
    no_import: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path.cwd()
    mcp_config = mcp_config or Path(".mcp.json")
    task, resolved_project = resolve_task_project(store, task_id, project_id)
    try:
        command = resolve_claude_command(permission_mode=permission_mode, with_mcp=with_mcp, mcp_config=mcp_config)
    except DispatchError as exc:
        return review_failed_payload(
            task_id=task["id"],
            project_id=resolved_project,
            mcp_config=mcp_config,
            with_mcp=with_mcp,
            reason=exc.reason,
            failure_stage=exc.stage,
            stderr=exc.stderr,
            exit_code=exc.exit_code,
            next_action="run nilo review claude-doctor",
        )
    request = create_quick_review_request(store, task["id"], requester, CLAUDE_REVIEWER, reason, cwd=repo_root)
    import_seen_event = store.latest_task_status_event(task["id"])
    report = store.latest_for_task("agent_reports", task["id"])
    verification_run = store.latest_for_task("verification_runs", task["id"])
    context = build_review_context(task, request, report, None, verification_run, repo_root)
    prompt = build_claude_review_prompt(
        context,
        build_review_result_template(request, include_import_command=False),
        with_mcp=with_mcp,
    )
    prompt_path = default_prompt_path(repo_root, request["id"]) if write_prompt else None
    if prompt_path:
        write_text(prompt_path, prompt)
    request = set_review_request_status(store, request["id"], "in_progress")
    try:
        process = run_claude(command, prompt, timeout_seconds=timeout_seconds, repo_root=repo_root)
    except DispatchError as exc:
        fail_quick_review_request(store, request, requester, exc.reason)
        return review_failed_payload(
            task_id=task["id"],
            project_id=resolved_project,
            mcp_config=mcp_config,
            with_mcp=with_mcp,
            reason=exc.reason,
            failure_stage=exc.stage,
            command=command.preview,
            review_request_id=request["id"],
            stdout=exc.stdout,
            stderr=exc.stderr,
            exit_code=exc.exit_code,
            next_action="run nilo review claude-doctor",
        )
    result_path = output_file
    if output_file:
        write_text(output_file, process.stdout)
    if process.returncode != 0:
        if not result_path and process.stdout:
            result_path = default_result_path(repo_root, request["id"])
            write_text(result_path, process.stdout)
        fail_quick_review_request(store, request, requester, f"claude process exited with code {process.returncode}")
        return review_failed_payload(
            task_id=task["id"],
            project_id=resolved_project,
            mcp_config=mcp_config,
            with_mcp=with_mcp,
            reason=f"claude process exited with code {process.returncode}",
            failure_stage="review_running",
            command=command.preview,
            review_request_id=request["id"],
            stdout=process.stdout,
            stderr=process.stderr,
            exit_code=process.returncode,
            output_file=result_path,
            next_action="run nilo review claude-doctor",
        )
    body_md = extract_review_result_body(process.stdout.strip())
    if no_import:
        if not result_path:
            result_path = default_result_path(repo_root, request["id"])
            write_text(result_path, process.stdout)
        return {
            "status": "raw_review",
            "review_request_id": request["id"],
            "task_id": task["id"],
            "project_id": resolved_project,
            "stdout": mask_secrets(process.stdout),
            "stderr": mask_secrets(process.stderr),
            "output_file": str(result_path),
            "imported": False,
            "mcp": "enabled" if with_mcp else "disabled",
            "mcp_config": str(mcp_config),
            "next_action": f"nilo review import --task {task['id']} --review {request['id']} --file {result_path}",
        }
    recoverable_output = False
    try:
        if not strict_review_result(body_md):
            raise DispatchError(
                "review_output_received",
                "claude output malformed",
                {"type": "fix_claude_review_output", "reviewer": CLAUDE_REVIEWER},
                stdout=process.stdout,
                stderr=process.stderr,
            )
        recoverable_output = True
        try:
            verdict, _summary, _findings = parse_review_result(body_md)
        except ValueError as exc:
            raise DispatchError(
                "review_output_received",
                str(exc),
                {"type": "fix_claude_review_output", "reviewer": CLAUDE_REVIEWER},
                stdout=process.stdout,
                stderr=process.stderr,
            ) from exc
        if verdict not in VALID_CLAUDE_VERDICTS:
            raise DispatchError(
                "review_output_received",
                f"claude output has invalid verdict: {verdict}",
                {"type": "fix_claude_review_output", "reviewer": CLAUDE_REVIEWER},
                stdout=process.stdout,
                stderr=process.stderr,
            )
        result, findings = import_dispatch_review_result(
            store,
            request,
            CLAUDE_REVIEWER,
            body_md,
            last_seen_event_id=import_seen_event["event_id"] if import_seen_event else "",
            cwd=repo_root,
        )
    except DispatchError as exc:
        if not result_path:
            result_path = default_result_path(repo_root, request["id"])
            write_text(result_path, process.stdout)
        if exc.stage != "review_output_received" or not recoverable_output:
            fail_quick_review_request(store, request, requester, exc.reason)
        return {
            "status": "review_import_failed",
            "failure_stage": exc.stage,
            "reason": exc.reason,
            "raw_output_file": str(result_path),
            "review_request_id": request["id"],
            "task_id": task["id"],
            "project_id": resolved_project,
            "stdout": mask_secrets(process.stdout),
            "stderr": mask_secrets(process.stderr),
            "mcp": "enabled" if with_mcp else "disabled",
            "mcp_config": str(mcp_config),
            "next_action": (
                f"edit the file and run nilo review import --task {task['id']} --review {request['id']} --file {result_path}"
                if recoverable_output
                else "retry the Claude review"
            ),
        }
    blocking = [finding for finding in findings if finding["blocking"] and finding["status"] == "unresolved"]
    nonblocking = [finding for finding in findings if not (finding["blocking"] and finding["status"] == "unresolved")]
    return {
        "status": "review_completed",
        "review_request_id": request["id"],
        "review_result_id": result["id"],
        "task_id": task["id"],
        "project_id": resolved_project,
        "verdict": result["verdict"],
        "summary": result["summary"],
        "findings": findings,
        "blocking_findings": len(blocking),
        "non_blocking_findings": len(nonblocking),
        "mcp": "enabled" if with_mcp else "disabled",
        "mcp_config": str(mcp_config),
        "prompt_file": str(prompt_path) if prompt_path else "",
        "output_file": str(result_path) if result_path else "",
        "command": command.preview,
        "next_action": next_action_for_result(result["verdict"], findings),
    }


def claude_doctor(*, mcp_config: Path, with_mcp: bool) -> dict[str, Any]:
    claude = find_executable("claude")
    rtk = find_executable("rtk")
    command_preview = ""
    error = ""
    try:
        command = resolve_claude_command(permission_mode="bypassPermissions", with_mcp=with_mcp, mcp_config=mcp_config)
        command_preview = command.preview
    except DispatchError as exc:
        error = exc.reason
    return {
        "claude_found": bool(claude),
        "claude_executable": claude or "",
        "rtk_found": bool(rtk),
        "rtk_executable": rtk or "",
        "mcp": "enabled" if with_mcp else "disabled",
        "mcp_config": str(mcp_config),
        "mcp_config_exists": mcp_config.exists(),
        "command": command_preview,
        "permission_mode_default": "bypassPermissions",
        "permission_mode_note": "Default follows the current Nilo Claude review instruction; reviewers are still instructed not to modify files. Override with --permission-mode for stricter local policy.",
        "rtk_fallback_note": "rtk fallback is available but claude itself was not found; run a real review or inspect rtk configuration if command execution fails." if rtk and not claude else "",
        "error": error,
    }
