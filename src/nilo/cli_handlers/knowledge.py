from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..cli_support import make_id, read_text_or_exit
from ..failure import derived_rule_from_agent, parse_agent_derived_rules
from ..instruction import build_rules_derive_prompt
from ..store import Store
from ..timeutil import now_iso


def cmd_success_add(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        source_task_ids = args.task or []
        for task_id in source_task_ids:
            task = store.get("tasks", task_id)
            if not task:
                raise SystemExit(f"task not found: {task_id}")
            if task["project_id"] != args.project:
                raise SystemExit(f"task does not belong to project {args.project}: {task_id}")
        created_at = now_iso()
        row = {
            "id": make_id("success"),
            "project_id": args.project,
            "source_task_ids": source_task_ids,
            "pattern_text": args.pattern,
            "tags": args.tag or [],
            "applicable_task_types": args.type or [],
            "confidence": args.confidence,
            "success_count": max(len(source_task_ids), 1),
            "last_used_at": created_at,
            "state": "active",
            "created_at": created_at,
        }
        store.insert("success_patterns", row)
        print(f"success_pattern: {row['id']}")
    finally:
        store.close()


def cmd_success_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        patterns = store.list_where("success_patterns", "project_id=?", (args.project,))
        for pattern in patterns:
            tags = ",".join(pattern["tags"])
            task_types = ",".join(pattern["applicable_task_types"])
            print(f"{pattern['id']} [{pattern['state']}] {tags} ({task_types}): {pattern['pattern_text']}")
    finally:
        store.close()


def cmd_success_disable(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        pattern = store.get("success_patterns", args.pattern)
        if not pattern:
            raise SystemExit(f"success pattern not found: {args.pattern}")
        store.update("success_patterns", args.pattern, {"state": "archived"})
        print(f"disabled: {args.pattern}")
    finally:
        store.close()


def cmd_rules_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        rules = store.list_where("derived_rules", "project_id=?", (args.project,))
        for rule in rules:
            disabled = " disabled" if rule["manually_disabled"] else ""
            print(f"{rule['id']} [{rule['state']}{disabled}] {','.join(rule['tags'])}: {rule['rule_text']}")
    finally:
        store.close()


def cmd_rules_disable(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        rule = store.get("derived_rules", args.rule)
        if not rule:
            raise SystemExit(f"rule not found: {args.rule}")
        store.update("derived_rules", args.rule, {"manually_disabled": True})
        print(f"disabled: {args.rule}")
    finally:
        store.close()


def cmd_rules_derive_prepare(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        failures = store.list_where("failure_logs", "project_id=?", (args.project,))
        if args.limit:
            failures = failures[: args.limit]
        print(build_rules_derive_prompt(project, failures))
    finally:
        store.close()


def cmd_rules_derive_import(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        if args.file:
            body = read_text_or_exit(Path(args.file))
        else:
            body = sys.stdin.read()
        if not body.strip():
            raise SystemExit("derived rules body is empty")
        try:
            candidates = parse_agent_derived_rules(body)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        failures = store.list_where("failure_logs", "project_id=?", (args.project,))
        known_failure_ids = {failure["id"] for failure in failures}
        created: list[str] = []
        updated: list[str] = []
        for candidate in candidates:
            unknown = sorted(set(candidate["source_failure_ids"]) - known_failure_ids)
            if unknown:
                raise SystemExit(f"unknown source failures: {', '.join(unknown)}")
            rule = derived_rule_from_agent(args.project, candidate)
            existing = store.get("derived_rules", rule["id"])
            if existing:
                source_ids = sorted(set(existing["source_failure_ids"] + rule["source_failure_ids"]))
                store.update(
                    "derived_rules",
                    existing["id"],
                    {
                        "source_failure_ids": source_ids,
                        "source": "agent_import",
                        "rule_text": rule["rule_text"],
                        "tags": rule["tags"],
                        "severity": rule["severity"],
                        "confidence": rule["confidence"],
                        "recurrence_count": len(source_ids),
                        "last_seen_at": rule["last_seen_at"],
                        "state": "active",
                    },
                )
                updated.append(existing["id"])
            else:
                store.insert("derived_rules", rule)
                created.append(rule["id"])
        for rule_id in created:
            print(f"created: {rule_id}")
        for rule_id in updated:
            print(f"updated: {rule_id}")
        print(f"imported_rules: {len(created) + len(updated)}")
    finally:
        store.close()
