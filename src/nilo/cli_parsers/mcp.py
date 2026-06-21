from __future__ import annotations

import argparse
from pathlib import Path
from types import ModuleType


def register_mcp(sub: argparse._SubParsersAction, handlers: ModuleType) -> None:
    mcp = sub.add_parser("mcp")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)

    serve = mcp_sub.add_parser("serve")
    serve.set_defaults(func=handlers.cmd_mcp_serve)

    ping = mcp_sub.add_parser("ping")
    ping.add_argument("--log-file", type=Path, default=None)
    ping.set_defaults(func=handlers.cmd_mcp_ping)

    doctor = mcp_sub.add_parser("doctor")
    doctor.add_argument("--project", required=True)
    doctor.set_defaults(func=handlers.cmd_mcp_doctor)

    reviewer_start = mcp_sub.add_parser("reviewer-start")
    reviewer_start.add_argument("--reviewer", required=True)
    reviewer_start.add_argument("--project", default="")
    # Always include "review"; extra capabilities describe the same reviewer worker.
    reviewer_start.add_argument("--capability", dest="capabilities", action="append", default=["review"])
    reviewer_start.add_argument("--max-concurrent", type=int, default=1)
    reviewer_start.set_defaults(func=handlers.cmd_mcp_reviewer_start)

    reviewer_claim = mcp_sub.add_parser("reviewer-claim")
    reviewer_claim.add_argument("--reviewer", required=True)
    reviewer_claim.add_argument("--project", default="")
    reviewer_claim.add_argument("--capability", dest="capabilities", action="append", default=["review"])
    reviewer_claim.add_argument("--max-concurrent", type=int, default=1)
    reviewer_claim.add_argument("--write-default", action="store_true")
    reviewer_claim.set_defaults(func=handlers.cmd_mcp_reviewer_claim)

    reviewer_worker = mcp_sub.add_parser("reviewer-worker")
    reviewer_worker.add_argument("--reviewer", required=True)
    reviewer_worker.add_argument("--project", default="")
    reviewer_worker.add_argument("--result-file", type=argparse.FileType("r", encoding="utf-8"), required=True)
    reviewer_worker.add_argument("--capability", dest="capabilities", action="append", default=["review"])
    reviewer_worker.add_argument("--max-concurrent", type=int, default=1)
    reviewer_worker.add_argument("--wait-seconds", type=float, default=0.0)
    reviewer_worker.add_argument("--poll-interval", type=float, default=1.0)
    reviewer_worker.set_defaults(func=handlers.cmd_mcp_reviewer_worker)
