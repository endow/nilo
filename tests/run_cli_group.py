from __future__ import annotations

import argparse
import fnmatch
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cli import CliTests


GROUPS = {
    "recipe": ["test_recipe_*"],
    "facade": [
        "test_daily_facade_*",
        "test_facade_*",
        "test_ai_*",
        "test_overdrive_*",
        "test_handson_*",
    ],
    "todo": ["test_todo_*", "test_status_and_next_include_todo_*"],
    "agent": ["test_agent_*", "test_init_*", "test_doctor_*", "test_migrate_*"],
    "compat": [
        "test_dirty_tree_*",
        "test_git_changed_files_*",
        "test_mcp_ping_*",
        "test_plain_task_*",
        "test_unresolved_*",
        "test_windows_*",
    ],
    "roadmap": ["test_roadmap_*", "test_human_roadmap_*"],
    "project": ["test_project_*"],
    "report": ["test_report_*", "test_rules_*", "test_successful_reports_*"],
    "store": ["test_store_*"],
    "task": ["test_task_*", "test_latest_*", "test_base_commit_*", "test_outcome_*"],
    "quality": ["test_quality_*"],
    "review": ["test_review_*", "test_mcp_reviewer_*", "test_natural_language_*"],
    "verification": ["test_verification_*", "test_status_surfaces_dirty_verification_*"],
    "guard": ["test_secret_*", "test_success_*", "test_understanding_*"],
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
