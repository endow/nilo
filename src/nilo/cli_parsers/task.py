from __future__ import annotations

import argparse
from types import ModuleType

from ._common import TASK_TYPES


def register_task(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    task_create = task_sub.add_parser("create")
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--description", action="append", default=[])
    task_create.add_argument("--acceptance", action="append", default=[])
    task_create.add_argument("--id")
    task_create.add_argument("--parent-task", default=None, help=argparse.SUPPRESS)
    task_create.add_argument("--split-index", type=int, default=None, help=argparse.SUPPRESS)
    task_create.add_argument("--commitment", default="")
    task_create.add_argument("--roadmap-item", default="")
    task_create.add_argument("--model", default="")
    task_create.add_argument("--degradation", choices=["normal", "degraded"], default="normal")
    task_create.add_argument("--mode", choices=["normal", "overdrive"], default="normal")
    task_create.add_argument("--type", dest="task_type", choices=TASK_TYPES, default="implementation")
    task_create.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    task_create.add_argument("--requires-understanding-check", action="store_true")
    task_create.set_defaults(func=handlers.cmd_task_create)

    task_start = task_sub.add_parser("start")
    task_start.add_argument("title")
    task_start.add_argument("--project", required=True)
    task_start.add_argument("--description", action="append", default=[])
    task_start.add_argument("--acceptance", action="append", default=[])
    task_start.add_argument("--commitment", default="")
    task_start.add_argument("--mode", choices=["normal", "overdrive"], default="normal")
    task_start.add_argument("--type", dest="task_type", choices=TASK_TYPES, default="implementation")
    task_start.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    task_start.set_defaults(func=handlers.cmd_task_start)

    task_status = task_sub.add_parser("status")
    task_status.add_argument("--task", required=True)
    task_status.add_argument("--ai", action="store_true")
    task_status.add_argument("--format", choices=["text", "json"], default="text")
    task_status.set_defaults(func=handlers.cmd_task_status)

    task_show = task_sub.add_parser("show")
    task_show.add_argument("--task", required=True)
    task_show.add_argument("--ai", action="store_true", default=True)
    task_show.add_argument("--format", choices=["text", "json"], default="text")
    task_show.set_defaults(func=handlers.cmd_task_status)

    task_update = task_sub.add_parser("update")
    task_update.add_argument("--task", required=True)
    task_update.add_argument("--description", action="append")
    task_update.add_argument("--acceptance", action="append")
    task_update.add_argument("--append-acceptance", action="append")
    task_update.set_defaults(func=handlers.cmd_task_update)

    task_list = task_sub.add_parser("list")
    task_list.add_argument("--project", required=True)
    task_list.set_defaults(func=handlers.cmd_task_list)

    task_analytics = task_sub.add_parser("analytics")
    analytics_target = task_analytics.add_mutually_exclusive_group(required=True)
    analytics_target.add_argument("--project")
    analytics_target.add_argument("--task")
    task_analytics.add_argument("--since", default="")
    task_analytics.add_argument("--format", choices=["text", "json"], default="text")
    task_analytics.set_defaults(func=handlers.cmd_task_analytics)

    task_split = task_sub.add_parser("split")
    task_split.add_argument("--task", required=True)
    task_split.add_argument("--child", action="append", default=[])
    task_split.set_defaults(func=handlers.cmd_task_split)

    task_complete = task_sub.add_parser("complete")
    task_complete.add_argument("--task", required=True)
    task_complete.add_argument("--reason", required=True)
    task_complete.add_argument("--actor", choices=["human", "ai"], required=True)
    task_complete.add_argument("--human-confirm", action="store_true", help="Required when recording a human completion decision.")
    task_complete.add_argument("--decision-note", default="", help="Required with --actor human.")
    task_complete.add_argument(
        "--human-acceptance",
        default="",
        help="Human-written acceptance phrase; implies --human-confirm and is used as --decision-note when omitted.",
    )
    task_complete.add_argument("--commit", action="store_true", help="Commit current accepted task changes after recording completion")
    task_complete.add_argument("--commit-message")
    task_complete.set_defaults(func=handlers.cmd_task_complete)

    completion = task_sub.add_parser("completion")
    completion_sub = completion.add_subparsers(dest="completion_command", required=True)
    invalidate = completion_sub.add_parser("invalidate")
    invalidate.add_argument("--completion", required=True)
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--actor", choices=["human"], required=True)
    invalidate.add_argument("--human-confirm", action="store_true", help="Required to reopen a completed task.")
    invalidate.set_defaults(func=handlers.cmd_task_completion_invalidate)
