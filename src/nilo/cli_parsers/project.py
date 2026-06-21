from __future__ import annotations

import argparse
from types import ModuleType


def register_project(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)

    project_create = project_sub.add_parser("create")
    project_create.add_argument("name")
    project_create.add_argument("--id")
    project_create.add_argument("--tech-stack", action="append", default=[])
    project_create.add_argument("--rule", action="append", default=[])
    project_create.add_argument("--criterion", action="append", default=[])
    project_create.add_argument("--model", action="append", default=[])
    project_create.add_argument("--fallback-model", action="append", default=[])
    project_create.add_argument("--requires-local-execution", action="store_true")
    project_create.set_defaults(func=handlers.cmd_project_create)

    project_status = project_sub.add_parser("status")
    project_status.add_argument("--project", required=True)
    project_status.add_argument("--verbose", action="store_true")
    project_status.set_defaults(func=handlers.cmd_project_status)

    project_summary = project_sub.add_parser("summary")
    project_summary.add_argument("--project", required=True)
    project_summary.add_argument("--format", choices=["text", "json"], default="text")
    project_summary.set_defaults(func=handlers.cmd_project_summary)

    project_export_handson = project_sub.add_parser("export-handson")
    project_export_handson.add_argument("--project", required=True)
    project_export_handson.add_argument("--file", required=True)
    project_export_handson.set_defaults(func=handlers.cmd_project_export_handson)
