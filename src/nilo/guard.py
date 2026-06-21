from __future__ import annotations

from pathlib import Path

from .gitmeta import changed_files_since
from .report import declares_no_changed_files, section_value, parse_sections, validate_report_shape
from .secret import detect_secret_issues


def evaluate_evidence(markdown: str, reported_files: list[str], base_commit: str | None, cwd: Path) -> tuple[str, list[str], dict]:
    issues = validate_report_shape(markdown)
    secret_issues = detect_secret_issues(markdown)
    issues.extend(secret_issues)
    actual_files, warnings = changed_files_since(base_commit, cwd)
    metadata = {
        "reported_changed_files": reported_files,
        "actual_changed_files": sorted(actual_files),
        "git_warnings": warnings,
        "secret_issue_count": len(secret_issues),
    }

    no_changed_files_claimed = declares_no_changed_files(section_value(parse_sections(markdown), ["変更ファイル一覧"]))
    if not reported_files and not (no_changed_files_claimed and not actual_files):
        issues.append("missing changed_files")

    if warnings:
        issues.extend(f"git metadata warning: {warning}" for warning in warnings)

    reported_set = set(reported_files)
    if reported_set != actual_files:
        missing = sorted(actual_files - reported_set)
        extra = sorted(reported_set - actual_files)
        if missing:
            issues.append(f"changed_files missing actual files: {', '.join(missing)}")
        if extra:
            issues.append(f"changed_files contains non-local changes: {', '.join(extra)}")

    if any(issue.startswith("changed_files") or issue.startswith("git metadata") or issue.startswith("secret detected") for issue in issues):
        return "needs_human_review", issues, metadata
    if issues:
        return "evidence_missing", issues, metadata
    return "evidence_submitted", issues, metadata
