from __future__ import annotations

import subprocess
import hashlib
from pathlib import Path

REPORT_STAGING_PREFIX = ".nilo/reports/"
EMPTY_TREE_COMMIT = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git_output(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout.rstrip("\n"), proc.stderr.rstrip("\n")


def head_commit(cwd: Path) -> str | None:
    code, out, _ = git_output(["rev-parse", "HEAD"], cwd)
    return out if code == 0 else None


def task_base_commit(cwd: Path) -> str | None:
    """Return an explicit comparison base, including for an unborn branch."""
    code, inside, _ = git_output(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or inside.strip().lower() != "true":
        return None
    return head_commit(cwd) or EMPTY_TREE_COMMIT


def task_base_snapshot(cwd: Path) -> dict:
    code, out, _ = git_output(["ls-files", "--others", "--exclude-standard"], cwd)
    files = sorted(line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()) if code == 0 else []
    return {
        "unborn": head_commit(cwd) is None,
        "untracked_files": files,
        "untracked_content_hashes": {
            path: hashlib.sha256((cwd / path).read_bytes()).hexdigest()
            for path in files
            if (cwd / path).is_file()
        },
    }


def working_tree_state(cwd: Path) -> dict:
    code, inside, _ = git_output(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or inside.strip().lower() != "true":
        return {
            "working_tree_dirty": False,
            "working_tree_files": [],
            "working_tree_available": False,
        }

    code, out, _ = git_output(["status", "--porcelain=v1", "--untracked-files=all"], cwd)
    if code != 0:
        return {
            "working_tree_dirty": False,
            "working_tree_files": [],
            "working_tree_available": False,
        }
    files: list[str] = []
    for line in out.splitlines():
        value = porcelain_path(line)
        if value:
            files.append(value.replace("\\", "/"))
    unique_files = sorted(set(files))
    return {
        "working_tree_dirty": bool(unique_files),
        "working_tree_files": unique_files,
        "working_tree_available": True,
    }


def porcelain_path(line: str) -> str:
    if not line:
        return ""
    value = line[3:].strip() if len(line) > 2 and line[2] == " " else line[2:].strip()
    if " -> " in value:
        value = value.split(" -> ", 1)[1].strip()
    return value


def changed_files_since(base_commit: str | None, cwd: Path, initial_untracked: dict[str, str] | None = None) -> tuple[set[str], list[str]]:
    files: set[str] = set()
    warnings: list[str] = []

    code, inside, _ = git_output(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or inside.strip().lower() != "true":
        return files, ["not a git repository; changed_files metadata cannot be compared"]

    commands = [
        ["diff", "--name-only"],
        ["diff", "--name-only", "--staged"],
        ["ls-files", "--others", "--exclude-standard"],
    ]
    if base_commit:
        commands.insert(2, ["diff", "--name-only", f"{base_commit}..HEAD"])
    else:
        warnings.append("base_commit is missing; committed task changes cannot be compared")

    for args in commands:
        code, out, err = git_output(args, cwd)
        if code != 0:
            warnings.append(err or f"git {' '.join(args)} failed")
            continue
        files.update(normalized for line in out.splitlines() if (normalized := line.strip().replace("\\", "/")) and not is_report_staging_file(normalized))

    # Files that already existed untracked when the task began are not task
    # changes merely because an initial commit later included them.
    unchanged_initial = {
        path
        for path, expected_hash in (initial_untracked or {}).items()
        if (cwd / path).is_file() and hashlib.sha256((cwd / path).read_bytes()).hexdigest() == expected_hash
    }
    files.difference_update(unchanged_initial)
    return files, warnings


def is_report_staging_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(REPORT_STAGING_PREFIX) and normalized.endswith(".md")
