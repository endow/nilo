from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..agent_installation import install_agent_runtime_files
from ..cli import AGENT_TARGET_FILES
from ..doctor import (
    diagnose_ai_context,
    diagnose_completions,
    diagnose_performance,
    diagnose_state,
    diagnose_transitions,
    diagnose_workflow,
    inspect_agent_runtime,
)
from ..project_model import default_project_row
from ..store import Store
from ..timeutil import now_iso


def _install_agent_blocks(
    project: dict, targets: list[str], *, warn_unmanaged: bool
) -> None:
    result = install_agent_runtime_files(
        project, targets, warn_unmanaged=warn_unmanaged
    )
    for path in result.updated_paths:
        print(f"updated: {path}")
    for warning in result.warnings:
        print(f"warning: {warning}")


def cmd_doctor(args: argparse.Namespace) -> None:
    project_id = Path.cwd().name
    store = Store(args.db)
    try:
        project = store.get("projects", project_id) or default_project_row(
            project_id, now_iso()
        )
        if args.fix_local_instructions:
            _install_agent_blocks(
                project, list(AGENT_TARGET_FILES), warn_unmanaged=False
            )
        result = inspect_agent_runtime()
        for name, ok in result.checks.items():
            print(f"{name}: {'ok' if ok else '未作成'}")
        for warning in result.warnings:
            print(f"警告: {warning}")
    finally:
        store.close()


def cmd_doctor_ai_context(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    store = Store(args.db)
    try:
        try:
            result = diagnose_ai_context(store, project_id)
        except LookupError as exc:
            raise SystemExit(str(exc)) from exc
        print("AIコンテキスト:")
        print(f"- instruction_chars: {result.instruction_chars}")
        print(f"- mcp_default_tool_count: {len(result.default_tools)}")
        print(f"- mcp_review_handoff_tool_count: {len(result.review_handoff_tools)}")
        print(
            "- mcp_review_handoff_tools: "
            + ", ".join(tool["name"] for tool in result.review_handoff_tools)
        )
        print(f"- long_tool_descriptions: {len(result.long_descriptions)}")
        for item in result.long_descriptions[:5]:
            print(f"  - {item['name']}: {item['length']}")
        print(f"- status_ai_chars: {result.status_ai_chars}")
        print(f"- status_ai_max_chars: {result.status_ai_max_chars}")
        print(
            f"- status_ai_within_budget: {result.status_ai_chars <= result.status_ai_max_chars}"
        )
        print(f"- open_failure_count: {result.open_failure_count}")
        print(f"- high_open_failure_count: {result.high_open_failure_count}")
        print(f"- failure_summary_chars: {result.failure_summary_chars}")
        print(f"- stale_evidence_count: {result.stale_evidence_count}")
        print(f"- unresolved_review_count: {result.unresolved_review_count}")
    finally:
        store.close()


def cmd_doctor_completions(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        try:
            result = diagnose_completions(store, project_id)
        except LookupError as exc:
            raise SystemExit(str(exc)) from exc
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"project: {project_id} ({project['name']})")
        print("completion_projection:")
        for key, value in result["counts"].items():
            print(f"- {key}: {value}")
        findings = result["audit_findings"]
        print(f"completion_audit: {len(findings)} issue(s)")
        for finding in findings[:20]:
            print(
                f"- {finding['entity_type']}={finding['entity_id']} [{finding['severity']}] {finding['code']}: {finding['message']}"
            )
        if len(findings) > 20:
            print(f"- ... 残り {len(findings) - 20} 件（--json で確認）")
        print("自動修復は行っていません。")
    finally:
        store.close()


def cmd_doctor_state(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    store = Store(args.db)
    try:
        result = diagnose_state(store, project_id)
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"project: {project_id}")
        print(f"state_audit: {result['count']} finding(s)")
        print("No automatic changes were made.")
        for item in result["findings"]:
            print(
                f"- [{item['severity']}] {item['code']} {item['entity_type']}={item['entity_id']}: {item['message']}"
            )
            if item.get("remediation"):
                print(f"  remediation: {item['remediation']}")
    finally:
        store.close()


def cmd_doctor_performance(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        try:
            result = diagnose_performance(store, project_id)
        except LookupError as exc:
            raise SystemExit(str(exc)) from exc
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"project: {project_id} ({project['name']})")
        print("git_snapshot:")
        print(f"- mode: {result['snapshot_mode']}")
        print(f"- diff_hash_seconds: {result['diff_hash_seconds']:.3f}")
        for key in (
            "observed_paths",
            "hashed_paths",
            "excluded_paths",
            "large_paths",
            "binary_paths",
        ):
            print(f"- {key}: {result[key]}")
        for warning in result["warnings"]:
            print(warning)
    finally:
        store.close()


def cmd_doctor_workflow(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    store = Store(args.db)
    try:
        try:
            result = diagnose_workflow(store, project_id)
        except LookupError as exc:
            raise SystemExit(str(exc)) from exc
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"project: {project_id}")
        print(f"workflow_audit: {result['count']} finding(s)")
        print("No automatic changes were made.")
        for item in result["findings"]:
            print(
                f"- [{item['severity']}] {item['code']} {item['entity_type']}={item['entity_id']}: {item['message']}"
            )
    finally:
        store.close()


def cmd_doctor_transitions(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    store = Store(args.db)
    try:
        result = diagnose_transitions(store, project_id, args.limit)
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"project: {project_id}")
        print(f"transition_events: {result['count']}")
        for event in result["transition_events"]:
            print(
                f"- {event['created_at']} {event['transition']} {event['entity_type']}={event['entity_id']} actor={event['actor']} {event['previous_state']} -> {event['new_state']}"
            )
    finally:
        store.close()
