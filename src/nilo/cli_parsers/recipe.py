from __future__ import annotations

import argparse
from types import ModuleType

from ._common import TASK_TYPES


def register_recipe(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    recipe = sub.add_parser("recipe")
    recipe_sub = recipe.add_subparsers(dest="recipe_command", required=True)

    recipe_list = recipe_sub.add_parser("list")
    recipe_list.add_argument("--project", default="")
    recipe_list.add_argument("--all", action="store_true")
    recipe_list.add_argument("--format", choices=["text", "json"], default="text")
    recipe_list.set_defaults(func=handlers.cmd_recipe_list)

    recipe_show = recipe_sub.add_parser("show")
    recipe_show.add_argument("name")
    recipe_show.add_argument("--project", default="")
    recipe_show.add_argument("--source", choices=["project", "user", "builtin"], default="")
    recipe_show.add_argument("--format", choices=["text", "json"], default="text")
    recipe_show.set_defaults(func=handlers.cmd_recipe_show)

    recipe_doctor = recipe_sub.add_parser("doctor")
    recipe_doctor.add_argument("--project", default="")
    recipe_doctor.add_argument("--format", choices=["text", "json"], default="text")
    recipe_doctor.set_defaults(func=handlers.cmd_recipe_doctor)

    recipe_run = recipe_sub.add_parser("run")
    recipe_run.add_argument("name")
    recipe_run.add_argument("--project", default="")
    recipe_run.add_argument("--var", action="append", default=[])
    recipe_run.add_argument("--title", default="")
    recipe_run.add_argument("--commitment", default="")
    recipe_run.add_argument("--type", dest="task_type", choices=TASK_TYPES, default="implementation")
    recipe_run.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    recipe_run.add_argument("--dry-run", action="store_true")
    recipe_run.set_defaults(func=handlers.cmd_recipe_run)
