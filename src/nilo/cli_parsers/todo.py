from __future__ import annotations

import argparse
from types import ModuleType

from ..cli_handlers.todo import TODO_KINDS, TODO_PRIORITIES, TODO_STATUSES
from ._common import TASK_TYPES


def register_todo(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    todo = sub.add_parser("todo")
    todo_sub = todo.add_subparsers(dest="todo_command", required=True)

    todo_add = todo_sub.add_parser("add")
    todo_add.add_argument("--project", required=True)
    todo_add.add_argument("--kind", choices=TODO_KINDS, default="user_request")
    todo_add.add_argument("--description", action="append", default=[])
    todo_add.add_argument("--acceptance-hint", default="")
    todo_add.add_argument("--priority", choices=TODO_PRIORITIES, default="normal")
    todo_add.add_argument("--source-type", default="user_message")
    todo_add.add_argument("--source-task", default="")
    todo_add.add_argument("--id")
    todo_add.add_argument("title")
    todo_add.set_defaults(func=handlers.cmd_todo_add)

    todo_list = todo_sub.add_parser("list")
    todo_list.add_argument("--project", required=True)
    todo_list.add_argument("--status", choices=TODO_STATUSES)
    todo_list.set_defaults(func=handlers.cmd_todo_list)

    todo_show = todo_sub.add_parser("show")
    todo_show.add_argument("--item", required=True)
    todo_show.set_defaults(func=handlers.cmd_todo_show)

    todo_triage = todo_sub.add_parser("triage")
    todo_triage.add_argument("--item", required=True)
    todo_triage.add_argument("--status", choices=TODO_STATUSES, required=True)
    todo_triage.add_argument("--reason", required=True)
    todo_triage.add_argument("--actor", choices=["human", "ai"], required=True)
    todo_triage.add_argument("--human-confirm", action="store_true")
    todo_triage.add_argument("--decision-source", default="")
    todo_triage.add_argument("--commitment", default="")
    todo_triage.add_argument("--roadmap-revision", default="")
    todo_triage.set_defaults(func=handlers.cmd_todo_triage)

    todo_start = todo_sub.add_parser("start")
    todo_start.add_argument("--item", required=True)
    todo_start.add_argument("--title", default="")
    todo_start.add_argument("--type", dest="task_type", choices=TASK_TYPES, default="implementation")
    todo_start.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    todo_start.add_argument("--actor", required=True)
    todo_start.set_defaults(func=handlers.cmd_todo_start)

    todo_promote = todo_sub.add_parser("promote")
    todo_promote.add_argument("--item", required=True)
    todo_promote.add_argument("--to", choices=["roadmap-proposal"], required=True)
    todo_promote.add_argument("--reason", required=True)
    todo_promote.add_argument("--title", default="")
    todo_promote.add_argument("--actor", required=True)
    todo_promote.set_defaults(func=handlers.cmd_todo_promote)
