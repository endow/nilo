from __future__ import annotations

import argparse

from ..store import Store


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


def cmd_rules_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        rules = store.list_where("derived_rules", "project_id=?", (args.project,))
        for rule in rules:
            disabled = " disabled" if rule["manually_disabled"] else ""
            print(f"{rule['id']} [{rule['state']}{disabled}] {','.join(rule['tags'])}: {rule['rule_text']}")
    finally:
        store.close()


