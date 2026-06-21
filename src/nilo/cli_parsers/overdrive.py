from __future__ import annotations

import argparse
from types import ModuleType


def register_run(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    run = sub.add_parser("run")
    run.add_argument("--project", required=True)
    run.add_argument("--overdrive", action="store_true")
    run.add_argument("--commitment", default="")
    run.add_argument("--max-failures", type=int, default=3)
    run.set_defaults(func=handlers.cmd_run)

