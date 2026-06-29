from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import os
from pathlib import Path
import sys
import time
from typing import Any

from .gitmeta import git_output, head_commit, porcelain_path


SNAPSHOT_KEYS = ("git_head", "git_diff_hash", "working_tree_dirty")
SNAPSHOT_MODE_FULL = "full"
SNAPSHOT_MODE_FAST = "fast"
UNCOMPUTED_DIFF_HASH = "__not_computed__"
SNAPSHOT_WARNING_SECONDS = 5.0
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 1_000_000
DEFAULT_SNAPSHOT_IGNORE_PATTERNS = [
    ".git/**",
    ".nilo/reviews/**",
    ".nilo/backups/**",
    ".nilo/*.db",
    ".nilo/*.db-*",
    "node_modules/**",
    "dist/**",
    "build/**",
    ".next/**",
    ".venv/**",
    "venv/**",
    "__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    "coverage/**",
    ".coverage",
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.tmp",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.7z",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.mp4",
    "*.mov",
    "*.pdf",
]


@dataclass
class SnapshotHashResult:
    digest: str
    hashed_paths: list[str]
    excluded_paths: list[dict[str, Any]]
    large_paths: list[str]
    binary_paths: list[str]


def current_git_snapshot(cwd: Path, mode: str = SNAPSHOT_MODE_FULL) -> dict[str, Any]:
    if mode not in {SNAPSHOT_MODE_FULL, SNAPSHOT_MODE_FAST}:
        raise ValueError(f"unknown git snapshot mode: {mode}")
    code, inside, _ = git_output(["rev-parse", "--is-inside-work-tree"], cwd)
    if code != 0 or inside.strip().lower() != "true":
        return {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "git_status_porcelain": "",
            "observed_paths": [],
            "git_available": False,
            "snapshot_mode": mode,
            "git_diff_hash_computed": False,
        }

    untracked = "all" if mode == SNAPSHOT_MODE_FULL else "no"
    status_code, status, _ = git_output(["-c", "core.quotepath=false", "status", "--porcelain=v1", f"--untracked-files={untracked}"], cwd)
    if status_code != 0:
        status = ""
    paths = sorted({path for line in status.splitlines() if (path := porcelain_path(line).replace("\\", "/"))})
    if mode == SNAPSHOT_MODE_FAST:
        return {
            "git_head": head_commit(cwd),
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": bool(status.strip()),
            "git_status_porcelain": status,
            "observed_paths": paths,
            "git_available": True,
            "snapshot_mode": SNAPSHOT_MODE_FAST,
            "git_diff_hash_computed": False,
        }
    started = time.monotonic()
    hash_result = _diff_hash(cwd, status, paths)
    elapsed = time.monotonic() - started
    warnings = []
    if elapsed > SNAPSHOT_WARNING_SECONDS:
        warning = f"snapshot warning: git diff hash took {elapsed:.1f}s; consider .niloignore or use fast status"
        warnings.append(warning)
        print(warning, file=sys.stderr)
    return {
        "git_head": head_commit(cwd),
        "git_diff_hash": hash_result.digest,
        "working_tree_dirty": bool(status.strip()),
        "git_status_porcelain": status,
        "observed_paths": paths,
        "git_available": True,
        "snapshot_mode": SNAPSHOT_MODE_FULL,
        "git_diff_hash_computed": True,
        "snapshot_timing": {"diff_hash_seconds": elapsed},
        "snapshot_warnings": warnings,
        "snapshot_excluded_paths": hash_result.excluded_paths,
        "snapshot_hashed_paths": hash_result.hashed_paths,
        "snapshot_large_paths": hash_result.large_paths,
        "snapshot_binary_paths": hash_result.binary_paths,
        "snapshot_policy": {
            "max_file_bytes": max_snapshot_file_bytes(),
            "ignore_file": ".niloignore",
            "default_ignore_patterns": True,
        },
    }


def current_git_snapshot_fast(cwd: Path) -> dict[str, Any]:
    """Return HEAD and dirty state without computing a diff hash."""
    return current_git_snapshot(cwd, mode=SNAPSHOT_MODE_FAST)


def current_git_snapshot_full(cwd: Path) -> dict[str, Any]:
    """Return the full audit snapshot, including the diff hash."""
    return current_git_snapshot(cwd, mode=SNAPSHOT_MODE_FULL)


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


def snapshot_has_diff_hash(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    if snapshot.get("git_available") is False:
        return True
    diff_hash = snapshot.get("git_diff_hash") or ""
    return bool(snapshot.get("git_diff_hash_computed", True)) and bool(diff_hash) and diff_hash != UNCOMPUTED_DIFF_HASH


def snapshot_mode(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return str(record.get("snapshot_mode") or metadata.get("snapshot_mode") or "")


def snapshot_is_explicit_fast(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    computed = record.get("git_diff_hash_computed", metadata.get("git_diff_hash_computed", True))
    return snapshot_mode(record) == SNAPSHOT_MODE_FAST and not bool(computed) and (record.get("git_diff_hash") or "") == UNCOMPUTED_DIFF_HASH


def execution_impact_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith(("src/", "tests/")):
        return True
    return normalized in {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "requirements.txt",
        "requirements-dev.txt",
        "uv.lock",
        "poetry.lock",
    }


def fast_snapshot_paths_still_match(verification_run: dict[str, Any], current_snapshot: dict[str, Any]) -> bool:
    if (verification_run.get("git_head") or "") != (current_snapshot.get("git_head") or ""):
        return False
    verified_paths = {
        path.replace("\\", "/")
        for path in (verification_run.get("observed_paths") or [])
        if isinstance(path, str) and execution_impact_path(path)
    }
    current_paths = {
        path.replace("\\", "/")
        for path in (current_snapshot.get("observed_paths") or [])
        if isinstance(path, str) and execution_impact_path(path)
    }
    return current_paths.issubset(verified_paths)


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


def evidence_status(verification_run: dict[str, Any] | None, current_snapshot: dict[str, Any], *, strict: bool = True) -> str:
    if not verification_run:
        return "missing"
    if verification_run.get("timed_out") or verification_run.get("exit_code") not in (0, "0"):
        return "failed"
    if snapshot_mode(verification_run) == "none":
        return "stale"
    if not snapshot_has_diff_hash(verification_run):
        if snapshot_is_explicit_fast(verification_run):
            if not fast_snapshot_paths_still_match(verification_run, current_snapshot):
                return "stale"
            return "recorded" if strict else "present"
        if current_snapshot.get("git_available") is False and snapshots_match(record_snapshot(verification_run), compact_snapshot(current_snapshot)):
            return "current"
        return "stale"
    if not snapshot_has_diff_hash(current_snapshot):
        return "recorded" if strict else "present"
    if snapshots_match(record_snapshot(verification_run), compact_snapshot(current_snapshot)):
        return "current"
    return "stale"


def completion_commit_metadata(completion: dict[str, Any] | None) -> dict[str, Any]:
    if not completion:
        return {}
    snapshot = completion.get("completed_snapshot") or {}
    metadata = snapshot.get("commit_transition") if isinstance(snapshot, dict) else {}
    return metadata if isinstance(metadata, dict) else {}


def committed_evidence_matches(
    verification_run: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    completion: dict[str, Any] | None,
) -> bool:
    metadata = completion_commit_metadata(completion)
    if not verification_run or not metadata.get("committed_from_verified_dirty_tree"):
        return False
    if not metadata.get("commit_sha"):
        return False
    if current_snapshot.get("working_tree_dirty"):
        return False
    verified_snapshot = metadata.get("verified_snapshot") or {}
    pre_commit_snapshot = metadata.get("pre_commit_snapshot") or {}
    post_commit_snapshot = metadata.get("post_commit_snapshot") or {}
    if not snapshots_match(record_snapshot(verification_run), compact_snapshot(verified_snapshot)):
        return False
    if not snapshots_match(verified_snapshot, pre_commit_snapshot):
        return False
    if not snapshots_match(post_commit_snapshot, current_snapshot):
        return False
    if metadata.get("verified_diff_hash") != verified_snapshot.get("git_diff_hash"):
        return False
    if metadata.get("verified_diff_hash") != pre_commit_snapshot.get("git_diff_hash"):
        return False
    return True


def commit_aware_evidence_status(
    verification_run: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    completion: dict[str, Any] | None,
    *,
    strict: bool = True,
) -> str:
    status = evidence_status(verification_run, current_snapshot, strict=strict)
    if status == "stale" and committed_evidence_matches(verification_run, current_snapshot, completion):
        return "current"
    return status


def review_result_status(review_result: dict[str, Any], current_snapshot: dict[str, Any]) -> str:
    if snapshots_match(record_snapshot(review_result, "based_on_snapshot"), compact_snapshot(current_snapshot)):
        return "current"
    return "stale"


def snapshot_ignore_patterns(cwd: Path) -> list[str]:
    patterns = list(DEFAULT_SNAPSHOT_IGNORE_PATTERNS)
    ignore_file = cwd / ".niloignore"
    if not ignore_file.is_file():
        return patterns
    try:
        for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
            pattern = line.strip()
            if not pattern or pattern.startswith("#"):
                continue
            # Negated patterns are intentionally unsupported for this lightweight policy.
            if pattern.startswith("!"):
                continue
            patterns.append(pattern.replace("\\", "/"))
    except OSError:
        return patterns
    return patterns


def max_snapshot_file_bytes() -> int:
    raw_value = os.environ.get("NILO_SNAPSHOT_MAX_FILE_BYTES", "")
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_SNAPSHOT_MAX_FILE_BYTES
    return value if value >= 0 else DEFAULT_SNAPSHOT_MAX_FILE_BYTES


def should_skip_file_content(path: str, full_path: Path, patterns: list[str], max_file_bytes: int) -> tuple[bool, str]:
    normalized = path.replace("\\", "/")
    if _matches_snapshot_pattern(normalized, patterns):
        return True, "ignored"
    try:
        stat = full_path.stat()
    except OSError:
        return True, "unavailable"
    if stat.st_size > max_file_bytes:
        return True, "large_file"
    if is_binary_file(full_path):
        return True, "binary"
    return False, ""


def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            chunk = handle.read(8192)
    except OSError:
        return False
    if b"\0" in chunk:
        return True
    if not chunk:
        return False
    decoded = chunk.decode("utf-8", errors="replace")
    replacement_count = decoded.count("\ufffd")
    return replacement_count / max(len(decoded), 1) > 0.1


def _matches_snapshot_pattern(path: str, patterns: list[str]) -> bool:
    basename = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        normalized = pattern.strip().replace("\\", "/")
        if not normalized:
            continue
        if normalized.endswith("/"):
            normalized = f"{normalized}**"
        if fnmatch.fnmatch(path, normalized) or ("/" not in normalized and fnmatch.fnmatch(basename, normalized)):
            return True
    return False


def _diff_hash(cwd: Path, status: str, paths: list[str]) -> SnapshotHashResult:
    hasher = hashlib.sha256()
    hashed_paths: list[str] = []
    excluded_paths: list[dict[str, Any]] = []
    large_paths: list[str] = []
    binary_paths: list[str] = []
    patterns = snapshot_ignore_patterns(cwd)
    max_file_bytes = max_snapshot_file_bytes()
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
            skip, reason = should_skip_file_content(path, full_path, patterns, max_file_bytes)
            if skip:
                if reason == "unavailable":
                    excluded_paths.append({"path": path, "reason": reason})
                    _hash_unavailable_file_meta(hasher, path, reason)
                    continue
                stat = full_path.stat()
                entry: dict[str, Any] = {"path": path, "reason": reason, "size": stat.st_size}
                excluded_paths.append(entry)
                if reason == "large_file":
                    large_paths.append(path)
                elif reason == "binary":
                    binary_paths.append(path)
                _hash_file_meta(hasher, path, stat.st_size, stat.st_mtime_ns, reason)
                continue
            hasher.update(f"\n$ file {path}\n".encode())
            hasher.update(full_path.read_bytes())
            hashed_paths.append(path)
        except OSError as exc:
            hasher.update(f"\n$ file {path} unavailable: {exc}\n".encode())
    return SnapshotHashResult(
        digest=hasher.hexdigest(),
        hashed_paths=hashed_paths,
        excluded_paths=excluded_paths,
        large_paths=large_paths,
        binary_paths=binary_paths,
    )


def _hash_file_meta(hasher: Any, path: str, size: int, mtime_ns: int, reason: str) -> None:
    hasher.update(f"\n$ file-meta {path}\n".encode())
    hasher.update(f"size={size}\n".encode())
    hasher.update(f"mtime_ns={mtime_ns}\n".encode())
    hasher.update(f"reason={reason}\n".encode())


def _hash_unavailable_file_meta(hasher: Any, path: str, reason: str) -> None:
    hasher.update(f"\n$ file-meta {path}\n".encode())
    hasher.update(f"reason={reason}\n".encode())
