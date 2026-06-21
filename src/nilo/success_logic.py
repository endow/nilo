from __future__ import annotations

from .failure import rule_success_update
from .store import Store
from .timeutil import now_iso


def record_rule_successes(store: Store, task_id: str) -> None:
    instruction = store.latest_for_task("instructions", task_id)
    if not instruction:
        return
    for rule_id in instruction["applied_rule_ids"]:
        rule = store.get("derived_rules", rule_id)
        if not rule or rule["manually_disabled"] or rule["state"] not in ("new", "active"):
            continue
        store.update("derived_rules", rule_id, rule_success_update(rule))


def select_success_patterns(store: Store, project_id: str, task: dict) -> list[dict]:
    patterns = store.list_where("success_patterns", "project_id=? AND state='active'", (project_id,))
    applicable: list[dict] = []
    task_type = task["task_type"]
    for pattern in patterns:
        task_types = pattern["applicable_task_types"]
        if task_types and task_type not in task_types:
            continue
        applicable.append(pattern)
    applicable.sort(key=lambda pattern: (float(pattern["confidence"]), int(pattern["success_count"])), reverse=True)
    limit = 1 if task["degradation_mode"] == "degraded" else 3
    return applicable[:limit]


def record_success_pattern_usage(store: Store, patterns: list[dict]) -> None:
    used_at = now_iso()
    for pattern in patterns:
        store.update(
            "success_patterns",
            pattern["id"],
            {
                "success_count": int(pattern["success_count"]) + 1,
                "last_used_at": used_at,
            },
        )
