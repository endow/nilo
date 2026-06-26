from __future__ import annotations

import os
from pathlib import Path

from .gitmeta import git_output


def mcp_identity(cwd: Path, db_path: Path | None = None) -> dict:
    resolved_cwd = cwd.resolve()
    git_root, git_head = _git_root_and_head(resolved_cwd)
    repository_root = Path(git_root) if git_root else resolved_cwd
    actual_db_path = db_path if db_path is not None else _default_db_path_for_cwd(resolved_cwd)
    working_tree_dirty = _working_tree_dirty(resolved_cwd, bool(git_root))
    project_id = repository_root.name
    return {
        "cwd": str(resolved_cwd),
        "git_root": git_root,
        "db_path": str(actual_db_path.resolve()),
        "project_id": project_id,
        "project_name": project_id,
        "repository_name": repository_root.name,
        "git_head": git_head,
        "working_tree_dirty": working_tree_dirty,
    }


def identity_matches_expected(identity: dict, expected_project: str = "", expected_git_root: str = "") -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if expected_project:
        project_id = str(identity.get("project_id", ""))
        repository_name = str(identity.get("repository_name", ""))
        if expected_project not in {project_id, repository_name}:
            actual = repository_name or project_id
            reasons.append(f"expected project {expected_project} but MCP is serving {actual}")
    if expected_git_root:
        expected = _normalize_path(expected_git_root)
        actual = _normalize_path(str(identity.get("git_root", "")))
        if expected != actual:
            reasons.append(f"expected git root {expected} but MCP git root is {actual}")
    return not reasons, reasons


def repository_mismatch_response(identity: dict, expected_project: str = "", expected_git_root: str = "") -> dict:
    fallback_commands = ["nilo status --ai", "nilo next"]
    if expected_git_root:
        fallback_commands.insert(0, f"cd {expected_git_root}")
    return {
        "ok": False,
        "error": "repository_mismatch",
        "message": "Nilo MCP is serving a different repository",
        "expected": {
            "project": expected_project,
            "git_root": expected_git_root,
        },
        "actual": {
            "project_id": identity.get("project_id", ""),
            "repository_name": identity.get("repository_name", ""),
            "git_root": identity.get("git_root", ""),
            "db_path": identity.get("db_path", ""),
        },
        "fallback": "CLI fallback",
        "fallback_commands": fallback_commands,
    }


def _git_root_and_head(cwd: Path) -> tuple[str, str]:
    code, out, _ = git_output(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not out.strip():
        return "", ""
    git_root = str(Path(out.strip()).resolve())
    head_code, head_out, _ = git_output(["rev-parse", "HEAD"], cwd)
    git_head = head_out.strip() if head_code == 0 else ""
    return git_root, git_head


def _working_tree_dirty(cwd: Path, in_git_repo: bool) -> bool:
    if not in_git_repo:
        return False
    code, out, _ = git_output(["status", "--porcelain=v1", "--untracked-files=all"], cwd)
    return code == 0 and bool(out.strip())


def _normalize_path(value: str) -> str:
    if not value:
        return ""
    return str(Path(value).expanduser().resolve())


def _default_db_path_for_cwd(cwd: Path) -> Path:
    env = os.environ.get("NILO_DB")
    if env:
        return Path(env)
    return cwd / ".nilo" / "nilo.db"
