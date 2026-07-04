from __future__ import annotations


def verification_summary(verification_run: dict | None) -> str:
    if not verification_run:
        return "none"
    result = "timed_out" if verification_run["timed_out"] else f"exit_code={verification_run['exit_code']}"
    source = verification_run.get("source", "nilo_executed")
    mode = verification_run.get("metadata", {}).get("verification_mode", "targeted")
    return f"{verification_run['id']} ({result}, source={source}, mode={mode})"


def human_verification_summary(verification_run: dict | None) -> str:
    if not verification_run:
        return "直近の検証結果はまだ記録されていません。"
    if verification_run["timed_out"]:
        return "直近の検証はタイムアウトしています。"
    if verification_run["exit_code"] == 0:
        return "直近の検証は成功しています。"
    return "直近の検証は失敗しています。"


def verification_working_tree_state(verification_run: dict | None) -> dict:
    metadata = verification_run["metadata"] if verification_run else {}
    dirty = bool(metadata.get("working_tree_dirty", verification_run.get("working_tree_dirty", False) if verification_run else False))
    files = metadata.get("working_tree_files") or (verification_run.get("observed_paths", []) if verification_run else [])
    return {
        "available": bool(metadata.get("working_tree_available", verification_run is not None)),
        "dirty": dirty,
        "files": files,
    }


def verification_working_tree_summary(verification_run: dict | None) -> str:
    if not verification_run:
        return "none"
    state = verification_working_tree_state(verification_run)
    if not state["available"]:
        return "unavailable"
    if not state["dirty"]:
        return "clean"
    count = len(state["files"])
    return f"dirty ({count} file{'s' if count != 1 else ''})"


def verification_snapshot_policy_summary(verification_run: dict | None) -> dict:
    metadata = verification_run["metadata"] if verification_run else {}
    excluded_paths = metadata.get("snapshot_excluded_paths", [])
    hashed_paths = metadata.get("snapshot_hashed_paths", [])
    reasons: dict[str, int] = {}
    for item in excluded_paths:
        reason = item.get("reason", "unknown") if isinstance(item, dict) else "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "observed_paths": len(metadata.get("working_tree_files", [])),
        "hashed_paths": len(hashed_paths),
        "skipped_paths": len(excluded_paths),
        "skipped_reasons": reasons,
    }


def verification_snapshot_policy_lines(verification_run: dict | None) -> list[str]:
    summary = verification_snapshot_policy_summary(verification_run)
    if not summary["skipped_paths"]:
        return []
    reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(summary["skipped_reasons"].items())) or "none"
    return [
        "snapshot:",
        f"  observed paths: {summary['observed_paths']}",
        f"  hashed paths: {summary['hashed_paths']}",
        f"  skipped paths: {summary['skipped_paths']}",
        f"  skipped reasons: {reasons}",
    ]
