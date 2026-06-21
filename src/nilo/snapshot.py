from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .gitmeta import git_output, head_commit, porcelain_path


SNAPSHOT_KEYS = ("git_head", "git_diff_hash", "working_tree_dirty")


def current_git_snapshot(cwd: Path) -> dict[str, Any]:
    code, inside, _ = git_output(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or inside.strip().lower() != "true":
        return {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "git_status_porcelain": "",
            "observed_paths": [],
            "git_available": False,
        }

    status_code, status, _ = git_output(["-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=all"], cwd)
    if status_code != 0:
        status = ""
    paths = sorted({path for line in status.splitlines() if (path := porcelain_path(line).replace("\\", "/"))})
    return {
        "git_head": head_commit(cwd),
        "git_diff_hash": _diff_hash(cwd, status, paths),
        "working_tree_dirty": bool(status.strip()),
        "git_status_porcelain": status,
        "observed_paths": paths,
        "git_available": True,
    }


def snapshot_columns(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "git_head": snapshot.get("git_head"),
        "git_diff_hash": snapshot.get("git_diff_hash") or "",
        "working_tree_dirty": bool(snapshot.get("working_tree_dirty")),
        "git_status_porcelain": snapshot.get("git_status_porcelain") or "",
        "observed_paths": snapshot.get("observed_paths") or [],
    }


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: snapshot.get(key) for key in SNAPSHOT_KEYS}


def record_snapshot(record: dict[str, Any], field: str = "") -> dict[str, Any]:
    if field:
        value = record.get(field)
        if isinstance(value, dict):
            return compact_snapshot(value)
        return {}
    return compact_snapshot(record)


def snapshots_match(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not left or not right:
        return False
    return all((left.get(key) or "") == (right.get(key) or "") for key in SNAPSHOT_KEYS)


def evidence_status(verification_run: dict[str, Any] | None, current_snapshot: dict[str, Any]) -> str:
    if not verification_run:
        return "missing"
    if verification_run.get("timed_out") or verification_run.get("exit_code") not in (0, "0"):
        return "failed"
    if snapshots_match(record_snapshot(verification_run), compact_snapshot(current_snapshot)):
        return "current"
    return "stale"


def review_result_status(review_result: dict[str, Any], current_snapshot: dict[str, Any]) -> str:
    if snapshots_match(record_snapshot(review_result, "based_on_snapshot"), compact_snapshot(current_snapshot)):
        return "current"
    return "stale"


def _diff_hash(cwd: Path, status: str, paths: list[str]) -> str:
    hasher = hashlib.sha256()
    hasher.update(status.encode("utf-8", errors="replace"))
    for args in (["diff", "--no-ext-diff"], ["diff", "--cached", "--no-ext-diff"]):
        code, out, err = git_output(args, cwd)
        hasher.update(f"\n$ git {' '.join(args)}\n".encode())
        hasher.update((out if code == 0 else err).encode("utf-8", errors="replace"))
    for path in paths:
        full_path = cwd / path
        if not full_path.is_file():
            continue
        try:
            hasher.update(f"\n$ file {path}\n".encode())
            hasher.update(full_path.read_bytes())
        except OSError as exc:
            hasher.update(f"\n$ file {path} unavailable: {exc}\n".encode())
    return hasher.hexdigest()
