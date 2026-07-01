from __future__ import annotations

import argparse
from types import ModuleType


def register_view(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    view = sub.add_parser("view")
    view.add_argument("--project", help="Project id. Defaults to the current directory name.")
    view.add_argument("--host", default="127.0.0.1")
    view.add_argument("--port", type=int, default=8765)
    view.add_argument("--no-open", action="store_true")
    view.add_argument("--format", choices=["server", "json"], default="server")
    view.set_defaults(func=handlers.cmd_view)
