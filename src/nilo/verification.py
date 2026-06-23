from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from subprocess import TimeoutExpired

from .secret import detect_secret_issues, mask_secrets
from .snapshot import current_git_snapshot, snapshot_columns
from .timeutil import now_iso


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


SHELL_CONTROL_TOKENS = {"&&", "||", "|", ";", "&", "<", ">", ">>", "2>", "2>>"}


def _split_command(command: str) -> tuple[list[str] | str, bool, str]:
    try:
        args = shlex.split(command, posix=True)
    except ValueError as exc:
        return command, True, f"parse_error: {exc}"
    if not args:
        return command, True, "empty command"
    if any(token in SHELL_CONTROL_TOKENS for token in args):
        return command, True, "shell control token"
    return args, False, "argv"


def run_local_verification(command: str, cwd: Path, timeout_seconds: float) -> dict:
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
    snapshot = current_git_snapshot(cwd)
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
            "snapshot_policy": snapshot.get("snapshot_policy", {}),
            "snapshot_excluded_paths": snapshot.get("snapshot_excluded_paths", []),
            "snapshot_hashed_paths": snapshot.get("snapshot_hashed_paths", []),
            "snapshot_large_paths": snapshot.get("snapshot_large_paths", []),
            "snapshot_binary_paths": snapshot.get("snapshot_binary_paths", []),
        },
        "started_at": started_at,
        "finished_at": finished_at,
        "created_at": finished_at,
    }
