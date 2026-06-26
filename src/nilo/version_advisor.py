from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .recipe import SEMVER_PATTERN, bump_patch, existing_release_tag, read_project_version, strip_v_prefix


PRERELEASE_TAG_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+-")
DB_SCHEMA_TERMS = ("CREATE TABLE", "ALTER TABLE", "MIGRATION", "MIGRATION_COLUMN_DEFINITIONS")
AI_FACING_TERMS = (
    "help ai",
    "status --ai",
    "doctor ai-context",
    "agent-instructions",
    "runtime instruction",
    "build_agent_instruction_block",
)
BREAKING_TERMS = ("breaking", "remove command", "drop table", "rename option", "incompatible")
PATCH_MESSAGE_TERMS = ("fix", "bug", "typo", "docs", "test", "refactor", "cleanup")


def advise_version_bump(cwd: Path) -> dict[str, Any]:
    warnings: list[str] = []
    current_version = read_project_version(cwd)
    latest_tag, prerelease_tags = read_latest_semver_tag_with_warnings(cwd)
    if prerelease_tags:
        warnings.append("pre-release semver tags are ignored: " + ", ".join(prerelease_tags))

    if not current_version:
        return {"resolved": False, "reason": "current version not found", "warnings": ["pyproject version was not found", *warnings]}
    if not SEMVER_PATTERN.match(current_version):
        return {
            "resolved": False,
            "reason": f"current version is not SemVer: {current_version}",
            "current_version": current_version,
            "latest_tag": latest_tag or "",
            "latest_semver_tag": latest_tag or "",
            "warnings": warnings,
        }

    patch_candidate = bump_patch(current_version)
    minor_candidate = bump_minor(current_version)
    major_candidate = bump_major(current_version)
    latest_tag_version = strip_v_prefix(latest_tag) if latest_tag else ""

    changed_files, changed_files_warning = read_changed_files(cwd, latest_tag)
    if changed_files_warning:
        warnings.append(changed_files_warning)
    commit_messages, commit_warning = read_commit_messages(cwd, latest_tag)
    if commit_warning:
        warnings.append(commit_warning)
    diff_text, diff_warning = read_diff_text(cwd, latest_tag)
    if diff_warning:
        warnings.append(diff_warning)
    if working_tree_dirty(cwd):
        warnings.append("working tree has uncommitted changes")

    existing_patch_tag = existing_release_tag(cwd, patch_candidate)
    existing_minor_tag = existing_release_tag(cwd, minor_candidate)
    if existing_patch_tag:
        warnings.append(f"tag already exists: {existing_patch_tag}")
    if existing_minor_tag:
        warnings.append(f"tag already exists: {existing_minor_tag}")

    reasons = recommendation_reasons(changed_files, commit_messages, diff_text)
    breaking_detected = contains_any("\n".join(commit_messages) + "\n" + diff_text, BREAKING_TERMS)
    major, minor, _patch = semver_tuple(current_version)
    recommended_bump_type = "minor" if reasons else "patch"
    if breaking_detected:
        warnings.append("compatibility-impacting change detected")
        recommended_bump_type = "major" if major >= 1 else "minor"
        if not reasons and major == 0:
            reasons.append("Compatibility-impacting change detected")

    recommended_version = {
        "patch": patch_candidate,
        "minor": minor_candidate,
        "major": major_candidate,
    }[recommended_bump_type]
    if not reasons and recommended_bump_type == "patch":
        reasons.append("Changed files and commit messages suggest a small patch-level change")

    mismatch = bool(latest_tag and latest_tag_version != current_version)
    if mismatch:
        warnings.append("current version and latest tag do not match")
    existing_recommended_tag = existing_release_tag(cwd, recommended_version)
    recommended_tag_exists = bool(existing_recommended_tag)
    # Dirty release trees are common while preparing a release, but uncommitted
    # changes make automatic target_version adoption too easy to misread.
    low_confidence = bool(
        not latest_tag
        or changed_files_warning
        or any(item == "working tree has uncommitted changes" for item in warnings)
        or mismatch
        or prerelease_tags
    )
    confidence = confidence_level(low_confidence, recommended_bump_type, reasons, commit_messages, recommended_tag_exists)
    requires_explicit_confirmation = (
        recommended_bump_type != "patch"
        or confidence == "low"
        or mismatch
        or recommended_tag_exists
    )

    reason = ""
    if mismatch:
        reason = f"current version {current_version} does not match latest tag {latest_tag}"
    elif recommended_tag_exists:
        reason = f"tag already exists: {existing_recommended_tag}"
    elif requires_explicit_confirmation:
        reason = f"{recommended_bump_type} bump requires explicit target_version"
    else:
        reason = f"version advisor recommends {recommended_bump_type}"

    return {
        "resolved": not requires_explicit_confirmation,
        "reason": reason,
        "current_version": current_version,
        "latest_tag": latest_tag or "",
        "latest_semver_tag": latest_tag or "",
        "latest_tag_version": latest_tag_version,
        "patch_candidate": patch_candidate,
        "minor_candidate": minor_candidate,
        "recommended_version": recommended_version,
        "recommended_bump_type": recommended_bump_type,
        "confidence": confidence,
        "requires_explicit_confirmation": requires_explicit_confirmation,
        "reasons": reasons,
        "warnings": warnings,
        "changed_files": changed_files,
    }


def bump_minor(version: str) -> str:
    major, minor, _patch = semver_tuple(version)
    return f"{major}.{minor + 1}.0"


def bump_major(version: str) -> str:
    major, _minor, _patch = semver_tuple(version)
    return f"{major + 1}.0.0"


def read_latest_semver_tag_with_warnings(cwd: Path) -> tuple[str, list[str]]:
    completed = run_git(cwd, ["tag", "--list"])
    if completed.returncode != 0:
        return "", []
    tags: list[tuple[tuple[int, int, int], str]] = []
    prerelease_tags: list[str] = []
    for raw in completed.stdout.splitlines():
        tag = raw.strip()
        match = SEMVER_PATTERN.match(tag)
        if match:
            tags.append(((int(match.group(1)), int(match.group(2)), int(match.group(3))), tag))
        elif PRERELEASE_TAG_PATTERN.match(tag):
            prerelease_tags.append(tag)
    latest = sorted(tags, key=lambda item: item[0])[-1][1] if tags else ""
    return latest, prerelease_tags


def read_changed_files(cwd: Path, latest_tag: str) -> tuple[list[str], str]:
    args = ["diff", "--name-only", f"{latest_tag}..HEAD"] if latest_tag else ["show", "--name-only", "--format=", "HEAD"]
    completed = run_git(cwd, args)
    if completed.returncode != 0:
        return [], "changed files could not be read"
    return [line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()], ""


def read_commit_messages(cwd: Path, latest_tag: str) -> tuple[list[str], str]:
    args = ["log", "--format=%s", f"{latest_tag}..HEAD"] if latest_tag else ["log", "-1", "--format=%s"]
    completed = run_git(cwd, args)
    if completed.returncode != 0:
        return [], "commit messages could not be read"
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()], ""


def read_diff_text(cwd: Path, latest_tag: str) -> tuple[str, str]:
    args = ["diff", f"{latest_tag}..HEAD"] if latest_tag else ["show", "--format=", "HEAD"]
    completed = run_git(cwd, args)
    if completed.returncode != 0:
        return "", "diff could not be read"
    return completed.stdout, ""


def working_tree_dirty(cwd: Path) -> bool:
    completed = run_git(cwd, ["status", "--porcelain"])
    return completed.returncode == 0 and bool(completed.stdout.strip())


def recommendation_reasons(changed_files: list[str], commit_messages: list[str], diff_text: str) -> list[str]:
    reasons: list[str] = []
    src_files = [path for path in changed_files if path.startswith("src/")]
    non_test_files = [path for path in changed_files if not is_test_path(path)]
    combined_text = "\n".join([*changed_files, *commit_messages, diff_text])

    if any(path == "src/nilo/cli.py" or path.startswith("src/nilo/cli_parsers/") or path.startswith("src/nilo/cli_handlers/") for path in non_test_files):
        reasons.append("CLI command or behavior changed")
    if "src/nilo/store.py" in non_test_files and contains_any(diff_text, DB_SCHEMA_TERMS, case_sensitive=True):
        reasons.append("DB schema or migration changed")
    if any(path.startswith("recipes/") or path in {"src/nilo/cli_handlers/recipe.py", "src/nilo/cli_parsers/recipe.py"} for path in non_test_files):
        reasons.append("Recipe behavior changed")
    if any(
        path.startswith("src/nilo/roadmap")
        or path in {"src/nilo/cli_handlers/roadmap.py", "src/nilo/cli_parsers/roadmap.py", "src/nilo/task_logic.py", "src/nilo/project_logic.py"}
        for path in non_test_files
    ):
        reasons.append("Roadmap or task flow changed")
    if contains_any(combined_text, AI_FACING_TERMS):
        reasons.append("AI-facing workflow or runtime instruction changed")
    if any(
        path
        in {
            "src/nilo/agent_report_import.py",
            "src/nilo/verification.py",
            "src/nilo/review_dispatcher.py",
            "src/nilo/cli_handlers/quality.py",
            "src/nilo/cli_handlers/failure.py",
            "src/nilo/cli_parsers/failure.py",
        }
        for path in non_test_files
    ):
        reasons.append("Evidence, review, or failure workflow changed")
    if any(path == "README.md" or path == "README.en.md" or path.startswith("docs/") for path in changed_files) and src_files:
        reasons.append("Documentation describes user-facing behavior changes")

    return dedupe(reasons)


def confidence_level(low_confidence: bool, recommended_bump_type: str, reasons: list[str], commit_messages: list[str], recommended_tag_exists: bool) -> str:
    if low_confidence or recommended_tag_exists:
        return "low"
    patch_messages = contains_any("\n".join(commit_messages), PATCH_MESSAGE_TERMS)
    if recommended_bump_type == "patch" and (patch_messages or not reasons):
        return "high"
    if recommended_bump_type == "minor" and len(reasons) == 1 and not patch_messages:
        return "high"
    return "medium"


def contains_any(text: str, terms: tuple[str, ...], *, case_sensitive: bool = False) -> bool:
    haystack = text if case_sensitive else text.lower()
    needles = terms if case_sensitive else tuple(term.lower() for term in terms)
    return any(term in haystack for term in needles)


def is_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/test_" in path or path.endswith("_test.py")


def semver_tuple(version: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.match(version)
    if not match:
        return 0, 0, 0
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
