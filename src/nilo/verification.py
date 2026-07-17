from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Callable

from .cli_support import make_id
from .failure import record_failure_log
from .project_boundary import (
    ProjectBoundaryError,
    record_nilo_issue_for_task,
    require_write_fence,
    resolve_project_boundary,
)
from .secret import detect_secret_issues, mask_secrets
from .snapshot import (
    UNCOMPUTED_DIFF_HASH,
    compact_snapshot,
    current_git_snapshot,
    git_changed_content_hash,
    git_patch_hash,
    snapshot_columns,
)
from .store import Store
from .timeutil import now_iso
from .transitions import record_verification_run


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


SHELL_CONTROL_TOKENS = {"&&", "||", "|", ";", "&", "<", ">", ">>", "2>", "2>>"}
ENV_ASSIGNMENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*=.*"


def _split_command(command: str) -> tuple[list[str] | str, bool, str]:
    try:
        args = shlex.split(command, posix=True)
    except ValueError as exc:
        return command, True, f"parse_error: {exc}"
    if not args:
        return command, True, "empty command"
    if any(_is_env_assignment(token) for token in args[:1]):
        return command, True, "environment assignment"
    if any(token in SHELL_CONTROL_TOKENS for token in args):
        return command, True, "shell control token"
    return args, False, "argv"


def _is_env_assignment(token: str) -> bool:
    import re

    return bool(re.match(ENV_ASSIGNMENT_PATTERN, token))


def _shard_summary_metadata(stdout: str, cwd: Path) -> dict:
    summary_path = ""
    failed_shards: list[str] = []
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("summary_json:"):
            summary_path = stripped.split(":", 1)[1].strip()
        if stripped == "failed_shards:":
            for item in lines[index + 1 :]:
                item = item.strip()
                if not item.startswith("- "):
                    break
                failed_shards.append(item[2:].strip())
        elif stripped.startswith("failed_shards:") and stripped != "failed_shards:":
            value = stripped.split(":", 1)[1].strip()
            if value and value != "[]":
                failed_shards.append(value)

    if summary_path:
        path = Path(summary_path)
        if not path.is_absolute():
            path = cwd / path
        if path.exists():
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                summary = {}
            if isinstance(summary, dict):
                failed = summary.get("failed_shards")
                if isinstance(failed, list):
                    failed_shards = [str(item) for item in failed]
                summary_path = str(summary.get("summary_path") or summary_path)

    result = {}
    if summary_path:
        result["summary_path"] = summary_path
        result["failed_summary_path"] = summary_path
    if failed_shards:
        result["failed_shards"] = failed_shards
    return result


SNAPSHOT_MODES = {"fast", "full", "none", "audit"}


def verification_snapshot(cwd: Path, snapshot_mode: str) -> dict:
    if snapshot_mode not in SNAPSHOT_MODES:
        raise ValueError(f"unknown verification snapshot mode: {snapshot_mode}")
    if snapshot_mode == "none":
        return {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "git_status_porcelain": "",
            "observed_paths": [],
            "git_available": False,
            "snapshot_mode": "none",
            "git_diff_hash_computed": False,
        }
    return current_git_snapshot(
        cwd, mode="full" if snapshot_mode == "audit" else snapshot_mode
    )


def run_local_verification(
    command: str, cwd: Path, timeout_seconds: float, *, snapshot_mode: str = "fast"
) -> dict:
    started_at = now_iso()
    timed_out = False
    exit_code: int | None
    stdout = ""
    stderr = ""

    command_args, use_shell, execution_reason = _split_command(command)

    try:
        completed = subprocess.run(
            command_args,
            cwd=cwd,
            shell=use_shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
    except TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        if stderr:
            stderr = f"{stderr.rstrip()}\n[nilo] command timed out after {timeout_seconds:g} seconds\n"
        else:
            stderr = f"[nilo] command timed out after {timeout_seconds:g} seconds\n"

    finished_at = now_iso()
    raw_log = f"{stdout}\n{stderr}"
    secret_issues = detect_secret_issues(raw_log)
    snapshot = verification_snapshot(cwd, snapshot_mode)
    snapshot_mode_recorded = (
        snapshot_mode
        if snapshot_mode == "audit"
        else snapshot.get("snapshot_mode", snapshot_mode)
    )
    git_diff_hash_computed = bool(
        snapshot.get(
            "git_diff_hash_computed",
            snapshot.get("git_diff_hash") not in {"", UNCOMPUTED_DIFF_HASH},
        )
    )
    shard_metadata = _shard_summary_metadata(stdout, cwd)
    return {
        "source": "nilo_executed",
        "command": command,
        "cwd": str(cwd),
        "stdout": mask_secrets(stdout),
        "stderr": mask_secrets(stderr),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        **snapshot_columns(snapshot),
        "metadata": {
            "secret_issue_count": len(secret_issues),
            "secret_issues": secret_issues,
            "runner": "local",
            "execution_mode": "shell" if use_shell else "argv",
            "execution_reason": execution_reason,
            "sandbox": "none",
            "working_tree_available": snapshot.get("git_available", False),
            "working_tree_dirty": snapshot.get("working_tree_dirty", False),
            "working_tree_files": snapshot.get("observed_paths", []),
            "working_tree_patch_hash": git_patch_hash(cwd)
            if snapshot.get("working_tree_dirty")
            else "",
            "working_tree_content_hash": git_changed_content_hash(cwd)
            if snapshot.get("working_tree_dirty")
            else "",
            "snapshot_mode": snapshot_mode_recorded,
            "requested_snapshot_mode": snapshot_mode,
            "git_diff_hash_computed": git_diff_hash_computed,
            "snapshot_policy": snapshot.get("snapshot_policy", {}),
            "snapshot_excluded_paths": snapshot.get("snapshot_excluded_paths", []),
            "snapshot_hashed_paths": snapshot.get("snapshot_hashed_paths", []),
            "snapshot_large_paths": snapshot.get("snapshot_large_paths", []),
            "snapshot_binary_paths": snapshot.get("snapshot_binary_paths", []),
            **shard_metadata,
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "created_at": finished_at,
    }


VerificationRunner = Callable[..., dict]


def execute_and_record_verification(
    store: Store,
    task: dict,
    *,
    command: str,
    timeout_seconds: float,
    verification_mode: str,
    snapshot_mode: str = "fast",
    cwd: Path | None = None,
    db_path: Path | None = None,
    runner: VerificationRunner = run_local_verification,
) -> dict:
    root = cwd or Path.cwd()
    result = runner(command, root, timeout_seconds, snapshot_mode=snapshot_mode)
    result.setdefault("metadata", {})["verification_mode"] = verification_mode
    boundary = resolve_project_boundary(db_path=db_path)
    try:
        require_write_fence(boundary)
    except ProjectBoundaryError as exc:
        record_nilo_issue_for_task(
            store,
            task["project_id"],
            task["id"],
            command,
            exc,
            boundary,
        )
        raise
    row = {
        "id": make_id("verification"),
        "task_id": task["id"],
        "evidence_check_id": None,
        **result,
    }
    record_verification_run(store, task["id"], row=row, actor="nilo")
    for issue in result["metadata"]["secret_issues"]:
        record_failure_log(
            store,
            task["project_id"],
            task["id"],
            "",
            "secret_detected",
            issue,
            "high",
            source="verification_run",
            actor="nilo",
            related_id=row["id"],
            snapshot=compact_snapshot(current_git_snapshot(root)),
            operation="secret_scan",
            error_code="credential_pattern",
            context={"check": "secret_scan"},
            preventability="likely",
            status="open",
        )
    return row
