from __future__ import annotations

import argparse
from types import ModuleType

from ._common import TASK_TYPES


def add_project_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", help="Project id. Defaults to the current directory name.")


def add_task_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", help="Task id. Defaults to the single active task in the project.")


def register_facade(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    status = sub.add_parser("status")
    add_project_option(status)
    status.add_argument("--verbose", action="store_true")
    status.add_argument("--ai", action="store_true")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=handlers.cmd_facade_status)

    next_step = sub.add_parser("next")
    add_project_option(next_step)
    add_task_option(next_step)
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

    check = sub.add_parser("check")
    check.add_argument("command")
    add_project_option(check)
    add_task_option(check)
    check.add_argument("--mode", choices=["quick", "targeted", "full"], default="targeted")
    check.add_argument("--timeout", type=float, default=300.0)
    check.set_defaults(func=handlers.cmd_facade_check)

    done = sub.add_parser("done")
    add_project_option(done)
    add_task_option(done)
    done.add_argument("--reason", default="daily workflow accepted")
    done.add_argument("--actor", choices=["ai", "human"], required=True)
    done.add_argument("--human-confirm", action="store_true", help="Required when recording a human completion decision.")
    done.add_argument("--decision-note", default="", help="Required with --actor human.")
    done.add_argument("--commit", action="store_true")
    done.add_argument("--commit-message")
    done.set_defaults(func=handlers.cmd_facade_done)

    reject = sub.add_parser("reject")
    reject.add_argument("reason")
    add_project_option(reject)
    add_task_option(reject)
    reject.set_defaults(func=handlers.cmd_facade_reject)
