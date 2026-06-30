from __future__ import annotations

import argparse
from types import ModuleType


def register_release(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    release = sub.add_parser("release")
    release_sub = release.add_subparsers(dest="release_command", required=True)

    prepare = release_sub.add_parser("prepare")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--target-version", default="")
    prepare.add_argument("--timeout", type=float, default=600.0)
    prepare.set_defaults(func=handlers.cmd_release_prepare)

    run = release_sub.add_parser("run")
    run.add_argument("--project", required=True)
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-version", default="")
    target.add_argument("--auto-patch", action="store_true")
    run.add_argument("--timeout", type=float, default=600.0)
    run.set_defaults(func=handlers.cmd_release_run)

    resume = release_sub.add_parser("resume")
    resume.add_argument("--project", required=True)
    resume.add_argument("--timeout", type=float, default=600.0)
    resume.set_defaults(func=handlers.cmd_release_resume)

    publish = release_sub.add_parser("publish")
    publish.add_argument("--project", required=True)
    publish.add_argument("--approval", required=True, help="Explicit approval text, e.g. v0.5.1 を tag/push/release して")
    publish.add_argument("--release-url", default="")
    publish.set_defaults(func=handlers.cmd_release_publish)
