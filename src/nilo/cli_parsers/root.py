from __future__ import annotations

import argparse
from collections.abc import Callable
from types import ModuleType

from .backup import register_backup
from .facade import register_facade
from .failure import register_failure
from .mcp import register_mcp
from .overdrive import register_run
from .project import register_project
from .quality import register_quality
from .recipe import register_recipe
from .roadmap import register_roadmap
from .task import register_task
from .test import register_test
from .todo import register_todo
from .workspace import register_workspace
from .workflow import register_agent, register_instruct, register_outcome, register_report, register_review, register_understanding, register_verification


def build_parser(add_common: Callable[[argparse.ArgumentParser], None], handlers: ModuleType) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nilo")
    parser.add_argument("--version", action="version", version=f"nilo {handlers.nilo_version()}")
    add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--repair-project-binding", action="store_true")
    init.set_defaults(func=handlers.cmd_init)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--fix-local-instructions", action="store_true")
    doctor.set_defaults(func=handlers.cmd_doctor)
    doctor_sub = doctor.add_subparsers(dest="doctor_command")
    doctor_ai_context = doctor_sub.add_parser("ai-context")
    doctor_ai_context.add_argument("--project", help="Project id. Defaults to the current directory name.")
    doctor_ai_context.set_defaults(func=handlers.cmd_doctor_ai_context)
    doctor_completions = doctor_sub.add_parser("completions")
    doctor_completions.add_argument("--project", help="Project id. Defaults to the current directory name.")
    doctor_completions.add_argument("--json", action="store_true")
    doctor_completions.set_defaults(func=handlers.cmd_doctor_completions)
    doctor_state = doctor_sub.add_parser("state")
    doctor_state.add_argument("--project", help="Project id. Defaults to the current directory name.")
    doctor_state.add_argument("--json", action="store_true")
    doctor_state.set_defaults(func=handlers.cmd_doctor_state)
    for doctor_name in ("workflow", "recipe", "release"):
        doctor_workflow = doctor_sub.add_parser(doctor_name)
        doctor_workflow.add_argument("--project", help="Project id. Defaults to the current directory name.")
        doctor_workflow.add_argument("--json", action="store_true")
        doctor_workflow.set_defaults(func=handlers.cmd_doctor_workflow)
    doctor_transitions = doctor_sub.add_parser("transitions")
    doctor_transitions.add_argument("--project", help="Project id. Defaults to the current directory name.")
    doctor_transitions.add_argument("--limit", type=int, default=50)
    doctor_transitions.add_argument("--json", action="store_true")
    doctor_transitions.set_defaults(func=handlers.cmd_doctor_transitions)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    migrate.set_defaults(func=handlers.cmd_migrate)
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--dry-run", action="store_true")
    upgrade.set_defaults(func=handlers.cmd_upgrade)
    update_check = sub.add_parser("update-check")
    update_check.set_defaults(func=handlers.cmd_update_check)
    help_cmd = sub.add_parser("help")
    help_sub = help_cmd.add_subparsers(dest="help_topic", required=True)
    help_ai = help_sub.add_parser("ai")
    help_ai.set_defaults(func=handlers.cmd_help_ai)
    register_backup(sub, handlers)

    register_run(sub, handlers)
    register_facade(sub, handlers)
    register_failure(sub, handlers)
    register_project(sub, handlers)
    register_recipe(sub, handlers)
    register_roadmap(sub, handlers)
    register_agent(sub, handlers)
    register_task(sub, handlers)
    register_test(sub, handlers)
    register_todo(sub, handlers)
    register_instruct(sub, handlers)
    register_report(sub, handlers)
    register_understanding(sub, handlers)
    register_outcome(sub, handlers)
    register_quality(sub, handlers)
    register_review(sub, handlers)
    register_verification(sub, handlers)
    register_mcp(sub, handlers)
    register_workspace(sub, handlers)
    return parser
