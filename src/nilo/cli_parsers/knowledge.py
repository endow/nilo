from __future__ import annotations

import argparse
from types import ModuleType


def register_success(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    success = sub.add_parser("success")
    success_sub = success.add_subparsers(dest="success_command", required=True)
    success_list = success_sub.add_parser("list")
    success_list.add_argument("--project", required=True)
    success_list.set_defaults(func=handlers.cmd_success_list)


def register_rules(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    rules = sub.add_parser("rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_list = rules_sub.add_parser("list")
    rules_list.add_argument("--project", required=True)
    rules_list.set_defaults(func=handlers.cmd_rules_list)
