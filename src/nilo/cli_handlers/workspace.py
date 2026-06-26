from __future__ import annotations

import argparse
from pathlib import Path

from ..workspace_resolver import (
    WorkspaceResolutionError,
    add_workspace,
    list_workspace_entries,
    remove_workspace,
    show_workspace,
    workspace_db_path,
)


def _exit_on_workspace_error(exc: WorkspaceResolutionError) -> None:
    if exc.registered_workspaces:
        print(str(exc))
        print("registered:")
        for name in exc.registered_workspaces:
            print(f"- {name}")
        raise SystemExit(1)
    raise SystemExit(str(exc))


def cmd_workspace_add(args: argparse.Namespace) -> None:
    try:
        entry = add_workspace(args.name, str(args.root), force=args.force)
    except WorkspaceResolutionError as exc:
        _exit_on_workspace_error(exc)
    root = Path(entry["root"])
    print(f"workspace: {args.name}")
    print(f"root: {root}")
    print(f"db: {workspace_db_path(root)}")
    if not workspace_db_path(root).exists():
        print(f"warning: db not found: {workspace_db_path(root)}")


def cmd_workspace_list(args: argparse.Namespace) -> None:
    entries = list_workspace_entries()
    print("workspaces:")
    if not entries:
        print("- none")
        return
    for entry in entries:
        print(f"- {entry['name']}")
        print(f"  root: {entry['root']}")
        print(f"  db: {entry['db']}")


def cmd_workspace_show(args: argparse.Namespace) -> None:
    try:
        entry = show_workspace(args.name)
    except WorkspaceResolutionError as exc:
        _exit_on_workspace_error(exc)
    print(f"workspace: {entry['name']}")
    print(f"root: {entry['root']}")
    print(f"db: {entry['db']}")


def cmd_workspace_remove(args: argparse.Namespace) -> None:
    try:
        remove_workspace(args.name)
    except WorkspaceResolutionError as exc:
        _exit_on_workspace_error(exc)
    print(f"removed workspace: {args.name}")
