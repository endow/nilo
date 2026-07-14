from __future__ import annotations

from pathlib import Path
import hashlib
import re
from typing import Any

from .cli_support import make_id
from .snapshot import compact_snapshot, current_git_snapshot
from .store import Store
from .timeutil import now_iso


def deterministic_id(prefix: str, parts: list[str]) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


FAILURE_STATUSES = {"open", "resolved", "ignored"}
FAILURE_FINGERPRINT_VERSION = "v1"
FAILURE_PREVENTABILITY = {"unknown", "likely", "external"}


def normalize_fingerprint_part(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().casefold()).strip("_")
    return normalized or "unspecified"


def failure_fingerprint(*, source: str, operation: str, category: str, error_code: str) -> str:
    """Build a stable fingerprint from bounded structured classifications only."""
    parts = (source, operation, category, error_code)
    return ":".join([FAILURE_FINGERPRINT_VERSION, *(normalize_fingerprint_part(part) for part in parts)])


def failure_snapshot(cwd: Path | None = None) -> dict[str, Any]:
    try:
        return compact_snapshot(current_git_snapshot(cwd or Path.cwd()))
    except Exception:
        return {}


def record_failure_log(
    store: Store,
    project_id: str,
    task_id: str,
    report_id: str,
    category: str,
    message: str,
    severity: str,
    *,
    source: str = "",
    actor: str = "",
    related_id: str = "",
    snapshot: dict[str, Any] | None = None,
    operation: str = "",
    error_code: str = "",
    context: dict[str, Any] | None = None,
    preventability: str = "unknown",
    status: str = "open",
) -> dict[str, Any]:
    if preventability not in FAILURE_PREVENTABILITY:
        raise ValueError(f"invalid failure preventability: {preventability}")
    failure = {
        "id": make_id("failure"),
        "project_id": project_id,
        "task_id": task_id,
        "report_id": report_id,
        "category": category,
        "message": message,
        "severity": severity,
        "source": source,
        "actor": actor,
        "related_id": related_id or report_id,
        "snapshot": snapshot if snapshot is not None else failure_snapshot(),
        "fingerprint": failure_fingerprint(source=source, operation=operation, category=category, error_code=error_code),
        "operation": operation,
        "error_code": error_code,
        "context": context or {},
        "preventability": preventability,
        "status": status,
        "resolved_at": "",
        "resolved_by": "",
        "resolution_note": "",
        "created_at": now_iso(),
    }
    store.insert("failure_logs", failure)
    return failure


def failure_filters(
    *,
    project_id: str = "",
    task_id: str = "",
    category: str = "",
    severity: str = "",
    status: str = "",
) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    args: list[Any] = []
    if project_id:
        clauses.append("project_id=?")
        args.append(project_id)
    if task_id:
        clauses.append("task_id=?")
        args.append(task_id)
    if category:
        clauses.append("category=?")
        args.append(category)
    if severity:
        clauses.append("severity=?")
        args.append(severity)
    if status:
        clauses.append("status=?")
        args.append(status)
    return (" AND ".join(clauses) if clauses else "1=1", tuple(args))


def list_failure_logs(
    store: Store,
    *,
    project_id: str = "",
    task_id: str = "",
    category: str = "",
    severity: str = "",
    status: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    where, args = failure_filters(project_id=project_id, task_id=task_id, category=category, severity=severity, status=status)
    rows = store.list_where("failure_logs", where, args)
    return rows[:limit] if limit > 0 else rows


def summarize_failure_logs(
    store: Store,
    *,
    project_id: str = "",
    task_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    failures = list_failure_logs(store, project_id=project_id, task_id=task_id, limit=0)
    recent_scope = failures[:limit] if limit > 0 else failures
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for failure in failures:
        by_severity[failure["severity"]] = by_severity.get(failure["severity"], 0) + 1
        by_category[failure["category"]] = by_category.get(failure["category"], 0) + 1
        by_status[failure["status"]] = by_status.get(failure["status"], 0) + 1
    open_failures = [failure for failure in failures if failure["status"] == "open"]
    high_open = [failure for failure in open_failures if failure["severity"] == "high"]
    recent_high = [failure for failure in recent_scope if failure["status"] == "open" and failure["severity"] == "high"][:5]
    return {
        "total": len(failures),
        "open": by_status.get("open", 0),
        "resolved": by_status.get("resolved", 0),
        "ignored": by_status.get("ignored", 0),
        "by_severity": by_severity,
        "by_category": by_category,
        "by_status": by_status,
        "recent_high_failures": recent_high,
        "open_failure_count": len(open_failures),
        "high_open_failure_count": len(high_open),
        "latest_open_failure": open_failures[0] if open_failures else None,
    }


def compact_failure_message(message: str, limit: int = 200) -> str:
    first_line = (message or "").splitlines()[0] if message else ""
    if len(first_line) <= limit:
        return first_line
    return first_line[: limit - 3] + "..."


def fingerprint_shadow_report(
    store: Store,
    *,
    project_id: str = "",
    since: str = "",
    until: str = "",
) -> dict[str, Any]:
    clauses: list[str] = []
    args: list[Any] = []
    if project_id:
        clauses.append("project_id=?")
        args.append(project_id)
    if since:
        clauses.append("created_at>=?")
        args.append(since)
    if until:
        clauses.append("created_at<?")
        args.append(until)
    failures = store.list_where("failure_logs", " AND ".join(clauses) or "1=1", tuple(args))

    groups: dict[str, dict[str, Any]] = {}
    empty_count = 0
    unspecified_count = 0
    for failure in failures:
        fingerprint = failure.get("fingerprint") or ""
        if not fingerprint:
            empty_count += 1
            continue
        if "unspecified" in fingerprint.split(":"):
            unspecified_count += 1
        group = groups.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "occurrence_count": 0,
                "task_ids": set(),
                "severity": {},
                "failure_ids": [],
                "classifications": set(),
            },
        )
        group["occurrence_count"] += 1
        group["task_ids"].add(failure["task_id"])
        severity = failure["severity"]
        group["severity"][severity] = group["severity"].get(severity, 0) + 1
        if len(group["failure_ids"]) < 3:
            group["failure_ids"].append(failure["id"])
        group["classifications"].add(
            (failure.get("source") or "", failure.get("operation") or "", failure["category"], failure.get("error_code") or "")
        )

    rendered_groups = []
    for group in groups.values():
        classifications = sorted(group.pop("classifications"))
        task_ids = group.pop("task_ids")
        rendered_groups.append(
            {
                **group,
                "distinct_task_count": len(task_ids),
                "severity": dict(sorted(group["severity"].items())),
                "possible_collision": len(classifications) > 1,
                "classifications": [
                    {"source": item[0], "operation": item[1], "category": item[2], "error_code": item[3]}
                    for item in classifications
                ],
            }
        )
    rendered_groups.sort(key=lambda item: (-item["occurrence_count"], item["fingerprint"]))
    classified_count = len(failures) - empty_count - unspecified_count
    return {
        "project_id": project_id,
        "since": since,
        "until": until,
        "total_failures": len(failures),
        "classified_count": classified_count,
        "empty_fingerprint_count": empty_count,
        "unspecified_count": unspecified_count,
        "classification_rate": classified_count / len(failures) if failures else 0.0,
        "groups": rendered_groups,
    }
