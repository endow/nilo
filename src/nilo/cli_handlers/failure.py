from __future__ import annotations

import argparse
import json

from ..failure import list_failure_logs, summarize_failure_logs, update_failure_status
from ..store import Store


def print_failure_row(failure: dict) -> None:
    print(f"- {failure['id']}")
    print(f"  severity: {failure['severity']}")
    print(f"  category: {failure['category']}")
    print(f"  status: {failure['status']}")
    print(f"  task: {failure['task_id']}")
    print(f"  message: {failure['message']}")
    print(f"  created_at: {failure['created_at']}")


def cmd_failure_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        failures = list_failure_logs(
            store,
            project_id=args.project,
            task_id=args.task,
            category=args.category,
            severity=args.severity,
            status=args.status,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps({"failures": failures}, ensure_ascii=False, indent=2))
            return
        print("failure_logs:")
        if failures:
            for failure in failures:
                print_failure_row(failure)
        else:
            print("- none")
    finally:
        store.close()


def cmd_failure_summary(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        summary = summarize_failure_logs(store, project_id=args.project, task_id=args.task, limit=args.limit)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        print("failure_summary:")
        print(f"- total: {summary['total']}")
        print(f"- open: {summary['open']}")
        print(f"- resolved: {summary['resolved']}")
        print(f"- ignored: {summary['ignored']}")
        print()
        print("by_severity:")
        for key, count in sorted(summary["by_severity"].items()):
            print(f"- {key}: {count}")
        if not summary["by_severity"]:
            print("- none")
        print()
        print("by_category:")
        for key, count in sorted(summary["by_category"].items()):
            print(f"- {key}: {count}")
        if not summary["by_category"]:
            print("- none")
        print()
        print("by_status:")
        for key, count in sorted(summary["by_status"].items()):
            print(f"- {key}: {count}")
        if not summary["by_status"]:
            print("- none")
        print()
        print("recent_high_failures:")
        recent_high = summary["recent_high_failures"]
        if recent_high:
            for failure in recent_high:
                print(f"- {failure['id']} {failure['task_id']} {failure['category']}")
        else:
            print("- none")
    finally:
        store.close()


def cmd_failure_show(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        failure = store.get("failure_logs", args.failure_id)
        if not failure:
            raise SystemExit(f"failure not found: {args.failure_id}")
        if args.json:
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            return
        for key in (
            "id",
            "project_id",
            "task_id",
            "report_id",
            "category",
            "severity",
            "status",
            "message",
            "source",
            "actor",
            "related_id",
            "snapshot",
            "resolution_note",
            "created_at",
            "resolved_at",
            "resolved_by",
        ):
            print(f"{key}: {failure.get(key, '')}")
    finally:
        store.close()


def print_status_update(prefix: str, failure: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"failure": failure}, ensure_ascii=False, indent=2))
        return
    print(f"{prefix}: {failure['id']}")
    print(f"status: {failure['status']}")


def cmd_failure_resolve(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        try:
            failure = update_failure_status(store, args.failure_id, "resolved", note=args.note, by=args.by)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print_status_update("failure_resolved", failure, args.json)
    finally:
        store.close()


def cmd_failure_ignore(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        try:
            failure = update_failure_status(store, args.failure_id, "ignored", note=args.note, by=args.by)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print_status_update("failure_ignored", failure, args.json)
    finally:
        store.close()
