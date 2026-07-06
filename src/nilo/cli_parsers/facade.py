from __future__ import annotations

import argparse
from types import ModuleType

from ._common import TASK_TYPES


def add_project_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", help="Project id. Defaults to the current directory name.")


def add_task_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", help="Task id. Recommended for verification; omitted only when one unfinished project task is a safe target.")


def register_facade(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    work = sub.add_parser("work", help="Default daily workflow entrypoint.")
    work.add_argument("request", nargs="?", default="")
    add_project_option(work)
    add_task_option(work)
    work.add_argument("--recipe")
    work.add_argument("--no-recipe", action="store_true")
    work.add_argument("--check")
    work.add_argument("--mode", choices=["quick", "targeted", "full"], default="targeted")
    work.add_argument("--snapshot", choices=["fast", "full", "none", "audit"], default="fast")
    work.add_argument("--timeout", type=float, default=300.0)
    work.add_argument("--no-done", action="store_true")
    work.add_argument("--audit", action="store_true")
    work.add_argument("--dry-run", action="store_true")
    work.add_argument("--json", action="store_true")
    work.set_defaults(func=handlers.cmd_facade_work)

    status = sub.add_parser(
        "status",
        help="Lightweight current-position check. Use --verbose for details, --audit for strict evidence checks, or --ai for agent context.",
    )
    add_project_option(status)
    status.add_argument("--verbose", action="store_true", help="Show detailed project status with heavier summaries.")
    status.add_argument("--audit", action="store_true", help="Run strict evidence-oriented status checks.")
    status.add_argument("--ai", action="store_true", help="Show AI-oriented project context.")
    status.add_argument("--json", action="store_true", help="Show AI-oriented project context as JSON.")
    status.add_argument("--debug-timing", action="store_true", help="Print fast status timing buckets.")
    status.set_defaults(func=handlers.cmd_facade_status)

    next_step = sub.add_parser("next")
    add_project_option(next_step)
    add_task_option(next_step)
    next_step.add_argument("--verbose", action="store_true", help="Show background context in addition to the first action.")
    next_step.add_argument("--ai", action="store_true", help="Show machine-readable next action context.")
    next_step.add_argument("--do", action="store_true", help="Preview the safe next local step, or stop with the reason it cannot run yet.")
    next_step.set_defaults(func=handlers.cmd_facade_next)

    queue = sub.add_parser("queue")
    add_project_option(queue)
    queue.add_argument("--json", action="store_true")
    queue.add_argument("--verbose", action="store_true")
    queue.add_argument("--audit", action="store_true")
    queue.set_defaults(func=handlers.cmd_facade_queue)

    start = sub.add_parser("start")
    start.add_argument("title")
    add_project_option(start)
    start.add_argument("--description", action="append", default=[])
    start.add_argument("--acceptance", action="append", default=[])
    start.add_argument("--commitment", default="")
    start.add_argument("--mode", choices=["normal", "overdrive"], default="normal")
    start.add_argument("--type", dest="task_type", choices=TASK_TYPES, default="implementation")
    start.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    start.add_argument("--self-development", action="store_true")
    start.set_defaults(func=handlers.cmd_facade_start)

    check = sub.add_parser("check", help="Run and record verification. Prefer --task to avoid ambiguous evidence targets.")
    check.add_argument("command")
    add_project_option(check)
    add_task_option(check)
    check.add_argument("--mode", choices=["quick", "targeted", "full"], default="targeted")
    check.add_argument("--snapshot", choices=["fast", "full", "none", "audit"], default="fast")
    check.add_argument("--timeout", type=float, default=300.0)
    check.set_defaults(func=handlers.cmd_facade_check)

    done = sub.add_parser("done")
    add_project_option(done)
    add_task_option(done)
    done.add_argument("--reason", default="daily workflow accepted")
    done.add_argument("--actor", choices=["ai", "human"], required=True)
    done.add_argument("--human-confirm", action="store_true", help="Required when recording a human completion decision.")
    done.add_argument("--decision-note", default="", help="Required with --actor human.")
    done.add_argument(
        "--human-acceptance",
        default="",
        help="Human-written acceptance phrase; implies --human-confirm and is used as --decision-note when omitted.",
    )
    done.add_argument("--commit", action="store_true")
    done.add_argument("--commit-message")
    done.set_defaults(func=handlers.cmd_facade_done)

    reject = sub.add_parser("reject")
    reject.add_argument("reason")
    add_project_option(reject)
    add_task_option(reject)
    reject.set_defaults(func=handlers.cmd_facade_reject)
