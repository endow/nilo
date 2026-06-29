from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
current_pythonpath = os.environ.get("PYTHONPATH", "")
if str(SRC_ROOT) not in current_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), *([current_pythonpath] if current_pythonpath else [])])
from test_cli import CliTests


GROUPS = {
    # Keep these groups as CLI-boundary shards only. Logic-heavy cases belong in
    # tests/test_<feature>.py, and git/subprocess cases belong in integration
    # modules so changed-test selection does not over-select tests.test_cli.
    "smoke": [
        "test_help_ai_*",
        "test_recipe_json_lists_builtin_without_project_files",
        "test_recipe_show_prints_effective_recipe_details",
        "test_daily_facade_*",
        "test_task_create_records_type_and_risk",
        "test_review_prepare_outputs_review_only_prompt",
    ],
    "compat": [
        "test_report_facade_keeps_import_subcommand_compatibility",
        "test_rules_*",
        "test_success_*command_is_removed*",
        "test_mcp_ping_*",
        "test_plain_task_*",
        "test_windows_*",
    ],
    "compat-core": [
        "test_mcp_ping_*",
        "test_plain_task_*",
        "test_windows_*",
    ],
    "recipe": ["test_recipe_*", "test_focused_implementation_recipe_*"],
    "help": ["test_help_ai_*"],
    "status": [
        "test_daily_facade_*",
        "test_facade_*",
        "test_ai_*",
        "test_status_ai_*",
        "test_overdrive_*",
        "test_handson_*",
        "test_queue_*",
    ],
    "todo": ["test_todo_*", "test_status_and_next_include_todo_*"],
    "agent": ["test_agent_*", "test_init_*", "test_doctor_*", "test_migrate_*"],
    "roadmap-import": [
        "test_roadmap_import_*",
        "test_roadmap_adopt_*",
        "test_roadmap_output_commands_*",
    ],
    "roadmap-discuss": [
        "test_roadmap_discuss_*",
        "test_roadmap_task_plan_*",
        "test_pending_roadmap_*",
    ],
    "roadmap-assess": [
        "test_roadmap_assess_*",
        "test_roadmap_summary_*",
    ],
    "roadmap-lifecycle": [
        "test_roadmap_close_*",
        "test_roadmap_reject_*",
        "test_roadmap_agent_state_*",
        "test_human_roadmap_*",
        "test_multiple_accepted_roadmap_commitments_*",
    ],
    "project": ["test_project_*"],
    "report": ["test_report_*", "test_rules_*", "test_successful_reports_*"],
    "store": ["test_store_*"],
    "task": ["test_task_*", "test_latest_*", "test_base_commit_*", "test_outcome_*"],
    "quality": ["test_quality_*"],
    "review-core": [
        "test_review_import_*",
        "test_review_prepare_outputs_review_only_prompt",
        "test_review_status_*",
        "test_review_finding_update_rejects_invalid_status",
        "test_review_commented_status_has_project_next_action",
        "test_review_withdraw_*",
        "test_review_wait_*",
        "test_review_delegate_*",
        "test_review_handoff_*",
    ],
    "review-dispatch": [
        "test_review_dispatch_*",
        "test_review_quick_*",
        "test_review_human_launch_*",
        "test_natural_language_*",
    ],
    "review-mcp": [
        "test_mcp_reviewer_*",
        "test_review_request_transaction_completes_through_mcp_reviewer_worker",
    ],
    "review-workflow": [
        "test_review_request_marks_stale_reviewer_unavailable",
        "test_review_request_marks_unregistered_reviewer_unavailable",
        "test_review_request_prepare_import_and_status_workflow",
        "test_review_request_resolves_registered_reviewer_alias",
        "test_review_request_wait_timeout_withdraws_request_and_clears_next_action",
        "test_review_prepare_file_and_template_generate_handoff_files",
        "test_review_prepare_reviewer_readiness_outputs_json",
        "test_review_finding_update_records_history_and_unblocks_completion",
    ],
    "verification": ["test_verification_*", "test_status_surfaces_dirty_verification_*"],
    "guard": ["test_secret_*", "test_success_*", "test_understanding_*"],
    "workflow": [
        "test_next_*",
        "test_unresolved_*",
    ],
}


def all_test_names() -> list[str]:
    return unittest.TestLoader().getTestCaseNames(CliTests)


def selected_test_names(group: str) -> list[str]:
    names = all_test_names()
    if group == "ungrouped":
        grouped = set()
        for name in GROUPS:
            grouped.update(selected_test_names(name))
        return [name for name in names if name not in grouped]
    patterns = GROUPS[group]
    return [name for name in names if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)]


def build_suite(group: str) -> unittest.TestSuite:
    names = selected_test_names(group)
    if not names:
        raise SystemExit(f"no tests matched group: {group}")
    suite = unittest.TestSuite()
    for name in names:
        suite.addTest(CliTests(name))
    return suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a targeted focused group from tests.test_cli.")
    parser.add_argument("group", choices=sorted([*GROUPS, "ungrouped"]))
    parser.add_argument("--list", action="store_true", help="List selected test method names without running them.")
    args = parser.parse_args(argv)

    names = selected_test_names(args.group)
    if args.list:
        for name in names:
            print(name)
        return 0

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(build_suite(args.group))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
