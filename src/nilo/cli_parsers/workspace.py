from __future__ import annotations

import argparse
from pathlib import Path
from types import ModuleType


def register_workspace(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    workspace = sub.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)

    add = workspace_sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--root", type=Path, required=True)
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=handlers.cmd_workspace_add)

    list_cmd = workspace_sub.add_parser("list")
    list_cmd.set_defaults(func=handlers.cmd_workspace_list)

    show = workspace_sub.add_parser("show")
    show.add_argument("name")
    show.set_defaults(func=handlers.cmd_workspace_show)

    remove = workspace_sub.add_parser("remove")
    remove.add_argument("name")
    remove.set_defaults(func=handlers.cmd_workspace_remove)
