from __future__ import annotations

import argparse
import json

from ..display_labels import category_label, field_label, severity_label, status_label
from ..failure import fingerprint_shadow_report, list_failure_logs, summarize_failure_logs
from ..store import Store
from ..transitions import TransitionError, ignore_failure, resolve_failure


def print_failure_row(failure: dict) -> None:
    print(f"- {failure['id']}")
    print(f"  {field_label('severity')}: {severity_label(failure['severity'])}")
    print(f"  {field_label('category')}: {category_label(failure['category'])}")
    print(f"  {field_label('status')}: {status_label(failure['status'])}")
    print(f"  {field_label('task')}: {failure['task_id']}")
    print(f"  {field_label('message')}: {failure['message']}")
    print(f"  {field_label('created_at')}: {failure['created_at']}")


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
        print(f"{field_label('failure_logs')}:")
        if failures:
            for failure in failures:
                print_failure_row(failure)
        else:
            print("- なし")
    finally:
        store.close()


def cmd_failure_summary(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        summary = summarize_failure_logs(store, project_id=args.project, task_id=args.task, limit=args.limit)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        print(f"{field_label('failure_summary')}:")
        print(f"- {field_label('total')}: {summary['total']}")
        print(f"- {field_label('open')}: {summary['open']}")
        print(f"- {field_label('resolved')}: {summary['resolved']}")
        print(f"- {field_label('ignored')}: {summary['ignored']}")
        print()
        print(f"{field_label('by_severity')}:")
        for key, count in sorted(summary["by_severity"].items()):
            print(f"- {severity_label(key)}: {count}")
        if not summary["by_severity"]:
            print("- なし")
        print()
        print(f"{field_label('by_category')}:")
        for key, count in sorted(summary["by_category"].items()):
            print(f"- {category_label(key)}: {count}")
        if not summary["by_category"]:
            print("- なし")
        print()
        print(f"{field_label('by_status')}:")
        for key, count in sorted(summary["by_status"].items()):
            print(f"- {status_label(key)}: {count}")
        if not summary["by_status"]:
            print("- なし")
        print()
        print(f"{field_label('recent_high_failures')}:")
        recent_high = summary["recent_high_failures"]
        if recent_high:
            for failure in recent_high:
                print(f"- {failure['id']} {failure['task_id']} {category_label(failure['category'])}")
        else:
            print("- なし")
    finally:
        store.close()


def cmd_failure_shadow_report(args: argparse.Namespace) -> None:
    store = Store(args.db, read_only=True)
    try:
        report = fingerprint_shadow_report(store, project_id=args.project, since=args.since, until=args.until)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        print("fingerprint shadow report:")
        print(f"- project_id: {report['project_id'] or 'all'}")
        print(f"- period: {report['since'] or 'unbounded'} .. {report['until'] or 'unbounded'} (until exclusive)")
        print(f"- total_failures: {report['total_failures']}")
        print(f"- classified_count: {report['classified_count']}")
        print(f"- empty_fingerprint_count: {report['empty_fingerprint_count']}")
        print(f"- unspecified_count: {report['unspecified_count']}")
        print(f"- classification_rate: {report['classification_rate']:.3f}")
        print("groups:")
        if not report["groups"]:
            print("- なし")
        for group in report["groups"]:
            print(
                f"- {group['fingerprint']}: occurrences={group['occurrence_count']} "
                f"tasks={group['distinct_task_count']} collision={str(group['possible_collision']).lower()}"
            )
            print(f"  severity: {json.dumps(group['severity'], ensure_ascii=False, sort_keys=True)}")
            print(f"  representative_failure_ids: {', '.join(group['failure_ids'])}")
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
            "decision_note",
            "created_at",
            "resolved_at",
            "resolved_by",
        ):
            value = failure.get(key, "")
            if key == "category":
                value = category_label(value)
            elif key == "severity":
                value = severity_label(value)
            elif key == "status":
                value = status_label(value)
            print(f"{field_label(key)}: {value}")
    finally:
        store.close()


def print_status_update(prefix: str, failure: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"failure": failure}, ensure_ascii=False, indent=2))
        return
    print(f"{prefix}: {failure['id']}")
    print(f"{field_label('status')}: {status_label(failure['status'])}")


def cmd_failure_resolve(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        try:
            resolve_failure(
                store,
                args.failure_id,
                actor=args.by,
                reason=args.note,
                human_confirm=args.human_confirm,
                decision_source="human_interactive" if args.by == "human" else "",
                decision_note=args.decision_note,
            )
            failure = store.get("failure_logs", args.failure_id)
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        print_status_update("failure_resolved", failure, args.json)
    finally:
        store.close()


def cmd_failure_ignore(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        try:
            ignore_failure(
                store,
                args.failure_id,
                actor=args.by,
                reason=args.note,
                human_confirm=args.human_confirm,
                decision_source="human_interactive",
                decision_note=args.decision_note,
            )
            failure = store.get("failure_logs", args.failure_id)
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        print_status_update("failure_ignored", failure, args.json)
    finally:
        store.close()
