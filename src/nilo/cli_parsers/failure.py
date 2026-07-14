from __future__ import annotations

import argparse
from types import ModuleType


def add_failure_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--severity", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")


def register_failure(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    failure = sub.add_parser("failure")
    failure_sub = failure.add_subparsers(dest="failure_command", required=True)

    failure_list = failure_sub.add_parser("list")
    add_failure_filters(failure_list)
    failure_list.set_defaults(func=handlers.cmd_failure_list)

    failure_summary = failure_sub.add_parser("summary")
    failure_summary.add_argument("--project", default="")
    failure_summary.add_argument("--task", default="")
    failure_summary.add_argument("--limit", type=int, default=50)
    failure_summary.add_argument("--json", action="store_true")
    failure_summary.set_defaults(func=handlers.cmd_failure_summary)

    failure_shadow = failure_sub.add_parser("shadow-report")
    failure_shadow.add_argument("--project", default="")
    failure_shadow.add_argument("--since", default="")
    failure_shadow.add_argument("--until", default="")
    failure_shadow.add_argument("--json", action="store_true")
    failure_shadow.set_defaults(func=handlers.cmd_failure_shadow_report)

    failure_show = failure_sub.add_parser("show")
    failure_show.add_argument("failure_id")
    failure_show.add_argument("--json", action="store_true")
    failure_show.set_defaults(func=handlers.cmd_failure_show)

    failure_resolve = failure_sub.add_parser("resolve")
    failure_resolve.add_argument("failure_id")
    failure_resolve.add_argument("--note", required=True)
    failure_resolve.add_argument("--by", choices=["human", "ai"], required=True)
    failure_resolve.add_argument("--human-confirm", action="store_true")
    failure_resolve.add_argument("--decision-note", default="")
    failure_resolve.add_argument("--json", action="store_true")
    failure_resolve.set_defaults(func=handlers.cmd_failure_resolve)

    failure_ignore = failure_sub.add_parser("ignore")
    failure_ignore.add_argument("failure_id")
    failure_ignore.add_argument("--note", required=True)
    failure_ignore.add_argument("--by", choices=["human", "ai"], required=True)
    failure_ignore.add_argument("--human-confirm", action="store_true")
    failure_ignore.add_argument("--decision-note", default="")
    failure_ignore.add_argument("--json", action="store_true")
    failure_ignore.set_defaults(func=handlers.cmd_failure_ignore)
