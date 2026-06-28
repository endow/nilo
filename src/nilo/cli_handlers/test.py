from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _test_modules():
    tests_dir = Path.cwd() / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    from test_shards import all_shards, changed_files, resolve_shards, shards_for_changed_files

    return all_shards, changed_files, resolve_shards, shards_for_changed_files


def _runner_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "tests/run_shards.py"]
    if getattr(args, "full", False):
        command.append("--all")
    if getattr(args, "changed", False):
        command.append("--changed")
    for shard in getattr(args, "shard", []) or []:
        command.extend(["--shard", shard])
    if getattr(args, "shards", None):
        command.extend(["--shards", args.shards])
    if not any(option in command for option in ("--all", "--changed", "--shard", "--shards", "--failed-from")):
        command.append("--changed")
    command.extend(["--jobs", str(getattr(args, "jobs", "auto"))])
    if hasattr(args, "timeout"):
        command.extend(["--timeout", f"{args.timeout:g}"])
    return command


def _planned_shard_names(args: argparse.Namespace) -> list[str]:
    all_shards, changed_files, resolve_shards, shards_for_changed_files = _test_modules()
    names: list[str] = []
    for shard in getattr(args, "shard", []) or []:
        names.append(shard)
    if getattr(args, "shards", None):
        names.extend([item.strip() for item in args.shards.split(",") if item.strip()])
    if getattr(args, "full", False):
        names.extend(shard.name for shard in all_shards())
    if getattr(args, "changed", False) or not names:
        names.extend(shards_for_changed_files(changed_files(Path.cwd())))
    return [shard.name for shard in resolve_shards(sorted(set(names)))]


def _print_plan(command: list[str], shard_names: list[str]) -> None:
    print("Verification plan:")
    for name in shard_names:
        print(name)
    print()
    print("Run:")
    print(" ".join(command))


def cmd_test_plan(args: argparse.Namespace) -> None:
    command = _runner_command(args)
    _print_plan(command, _planned_shard_names(args))


def cmd_test_run(args: argparse.Namespace) -> None:
    command = _runner_command(args)
    completed = subprocess.run(command, cwd=Path.cwd(), check=False)
    raise SystemExit(completed.returncode)


def cmd_test_rerun_failed(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "tests/run_shards.py",
        "--failed-from",
        args.run_id,
        "--jobs",
        str(args.jobs),
        "--timeout",
        f"{args.timeout:g}",
    ]
    completed = subprocess.run(command, cwd=Path.cwd(), check=False)
    raise SystemExit(completed.returncode)
