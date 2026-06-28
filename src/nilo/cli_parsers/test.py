from __future__ import annotations

import argparse
from types import ModuleType


def register_test(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    test = sub.add_parser("test")
    test_sub = test.add_subparsers(dest="test_command", required=True)

    plan = test_sub.add_parser("plan")
    plan_selector = plan.add_mutually_exclusive_group()
    plan_selector.add_argument("--changed", action="store_true")
    plan_selector.add_argument("--full", action="store_true")
    plan_selector.add_argument("--shard", action="append", default=[])
    plan_selector.add_argument("--shards")
    plan.add_argument("--jobs", default="auto")
    plan.set_defaults(func=handlers.cmd_test_plan)

    run = test_sub.add_parser("run")
    run_selector = run.add_mutually_exclusive_group()
    run_selector.add_argument("--changed", action="store_true")
    run_selector.add_argument("--full", action="store_true")
    run_selector.add_argument("--shard", action="append", default=[])
    run_selector.add_argument("--shards")
    run.add_argument("--jobs", default="auto")
    run.add_argument("--timeout", type=float, default=300.0)
    run.set_defaults(func=handlers.cmd_test_run)

    rerun_failed = test_sub.add_parser("rerun-failed")
    rerun_failed.add_argument("run_id")
    rerun_failed.add_argument("--jobs", default="auto")
    rerun_failed.add_argument("--timeout", type=float, default=300.0)
    rerun_failed.set_defaults(func=handlers.cmd_test_rerun_failed)
