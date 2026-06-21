from __future__ import annotations

import argparse
from types import ModuleType

from ._common import TASK_TYPES


def register_success(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    success = sub.add_parser("success")
    success_sub = success.add_subparsers(dest="success_command", required=True)
    success_add = success_sub.add_parser("add")
    success_add.add_argument("--project", required=True)
    success_add.add_argument("--task", action="append", default=[])
    success_add.add_argument("--pattern", required=True)
    success_add.add_argument("--tag", action="append", default=[])
    success_add.add_argument("--type", action="append", choices=TASK_TYPES, default=[])
    success_add.add_argument("--confidence", type=float, default=0.55)
    success_add.set_defaults(func=handlers.cmd_success_add)
    success_list = success_sub.add_parser("list")
    success_list.add_argument("--project", required=True)
    success_list.set_defaults(func=handlers.cmd_success_list)
    success_disable = success_sub.add_parser("disable")
    success_disable.add_argument("--pattern", required=True)
    success_disable.set_defaults(func=handlers.cmd_success_disable)


def register_rules(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    rules = sub.add_parser("rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_list = rules_sub.add_parser("list")
    rules_list.add_argument("--project", required=True)
    rules_list.set_defaults(func=handlers.cmd_rules_list)
    rules_disable = rules_sub.add_parser("disable")
    rules_disable.add_argument("--rule", required=True)
    rules_disable.set_defaults(func=handlers.cmd_rules_disable)
    rules_derive = rules_sub.add_parser("derive")
    rules_derive_sub = rules_derive.add_subparsers(dest="rules_derive_command", required=True)
    rules_derive_prepare = rules_derive_sub.add_parser("prepare")
    rules_derive_prepare.add_argument("--project", required=True)
    rules_derive_prepare.add_argument("--limit", type=int)
    rules_derive_prepare.set_defaults(func=handlers.cmd_rules_derive_prepare)
    rules_derive_import = rules_derive_sub.add_parser("import")
    rules_derive_import.add_argument("--project", required=True)
    rules_derive_import.add_argument("--file")
    rules_derive_import.set_defaults(func=handlers.cmd_rules_derive_import)
