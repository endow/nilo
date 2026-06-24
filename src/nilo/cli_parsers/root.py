from __future__ import annotations

import argparse
from collections.abc import Callable
from types import ModuleType

from .facade import register_facade
from .knowledge import register_rules, register_success
from .mcp import register_mcp
from .overdrive import register_run
from .project import register_project
from .quality import register_quality
from .recipe import register_recipe
from .roadmap import register_roadmap
from .task import register_task
from .todo import register_todo
from .workflow import register_agent, register_instruct, register_outcome, register_report, register_review, register_understanding, register_verification


def build_parser(add_common: Callable[[argparse.ArgumentParser], None], handlers: ModuleType) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nilo")
    parser.add_argument("--version", action="version", version=f"nilo {handlers.nilo_version()}")
    add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.set_defaults(func=handlers.cmd_init)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--fix-local-instructions", action="store_true")
    doctor.set_defaults(func=handlers.cmd_doctor)
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    migrate.set_defaults(func=handlers.cmd_migrate)
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--dry-run", action="store_true")
    upgrade.set_defaults(func=handlers.cmd_upgrade)

    register_run(sub, handlers)
    register_facade(sub, handlers)
    register_project(sub, handlers)
    register_recipe(sub, handlers)
    register_roadmap(sub, handlers)
    register_agent(sub, handlers)
    register_task(sub, handlers)
    register_todo(sub, handlers)
    register_instruct(sub, handlers)
    register_report(sub, handlers)
    register_understanding(sub, handlers)
    register_outcome(sub, handlers)
    register_quality(sub, handlers)
    register_review(sub, handlers)
    register_verification(sub, handlers)
    register_success(sub, handlers)
    register_rules(sub, handlers)
    register_mcp(sub, handlers)
    return parser
