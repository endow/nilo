from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from subprocess import TimeoutExpired

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_shards import TestShard, all_shards, auto_jobs, changed_files, resolve_shards, shard_names, shards_for_changed_files


@dataclass(frozen=True)
class ShardResult:
    name: str
    command: str
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout_log: str
    stderr_log: str
    rerun_command: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_shard_filename(name: str, suffix: str) -> str:
    return name.replace(":", "__").replace("/", "_").replace("\\", "_") + suffix


def command_text(command: tuple[str, ...]) -> str:
    return " ".join(repr(part) if any(char.isspace() for char in part) else part for part in command)


def git_snapshot(cwd: Path) -> dict:
    snapshot: dict[str, object] = {}
    for key, args in {
        "git_head": ["git", "rev-parse", "HEAD"],
        "git_diff_hash": ["git", "diff", "--binary"],
        "git_status_porcelain": ["git", "status", "--porcelain"],
    }.items():
        completed = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            snapshot["git_available"] = False
            snapshot[key] = ""
            continue
        snapshot["git_available"] = True
        if key == "git_diff_hash":
            import hashlib

            snapshot[key] = hashlib.sha256(completed.stdout.encode("utf-8", errors="replace")).hexdigest()
        else:
            snapshot[key] = completed.stdout.strip()
    snapshot["working_tree_dirty"] = bool(snapshot.get("git_status_porcelain"))
    return snapshot


def run_one_shard(shard: TestShard, output_dir: Path, timeout: float, cwd: Path) -> ShardResult:
    stdout_path = output_dir / safe_shard_filename(shard.name, ".stdout.log")
    stderr_path = output_dir / safe_shard_filename(shard.name, ".stderr.log")
    started = time.monotonic()
    status = "failed"
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    env = os.environ.copy()
    src_path = cwd / "src"
    if src_path.exists():
        current_pythonpath = env.get("PYTHONPATH", "")
        entries = [str(src_path), *([current_pythonpath] if current_pythonpath else [])]
        env["PYTHONPATH"] = os.pathsep.join(entries)
    try:
        completed = subprocess.run(
            list(shard.command),
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
        status = "passed" if completed.returncode == 0 else "failed"
    except TimeoutExpired as exc:
        status = "timeout"
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        timeout_line = f"[nilo] shard timed out after {timeout:g} seconds\n"
        stderr = f"{stderr.rstrip()}\n{timeout_line}" if stderr else timeout_line
    duration = time.monotonic() - started
    stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
    return ShardResult(
        name=shard.name,
        command=command_text(shard.command),
        status=status,
        exit_code=exit_code,
        duration_seconds=round(duration, 3),
        stdout_log=stdout_path.as_posix(),
        stderr_log=stderr_path.as_posix(),
        rerun_command=f"{sys.executable} tests/run_shards.py --shard {shard.name}",
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def load_failed_shards(path_or_run_id: str, cwd: Path) -> list[str]:
    path = Path(path_or_run_id)
    if not path.is_absolute():
        candidates = [
            cwd / path,
            cwd / ".nilo" / "test-runs" / path_or_run_id / "summary.json",
            cwd / ".nilo" / "test-runs" / path_or_run_id,
        ]
        path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if path.is_dir():
        path = path / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    names = data.get("failed_shards", [])
    return [str(name) for name in names]


def selected_shards(args: argparse.Namespace, cwd: Path) -> list[TestShard]:
    selectors = [
        bool(args.all),
        bool(args.failed_from),
        bool(args.changed),
        bool(args.shard),
        bool(args.shards),
    ]
    if sum(selectors) > 1:
        raise SystemExit("choose only one shard selector: --all, --changed, --shard/--shards, or --failed-from")
    if args.all:
        return all_shards()
    if args.failed_from:
        return resolve_shards(load_failed_shards(args.failed_from, cwd))
    names: list[str] = []
    for shard in args.shard or []:
        names.append(shard)
    for value in args.shards or []:
        names.extend([item.strip() for item in value.split(",") if item.strip()])
    if args.changed:
        names.extend(shards_for_changed_files(changed_files(cwd)))
    if not names:
        raise SystemExit("choose --all, --changed, --shard, --shards, or --failed-from")
    return resolve_shards(sorted(set(names)))


def parse_jobs(value: str, shard_count: int) -> int:
    if value == "auto":
        return auto_jobs(shard_count, os.cpu_count())
    try:
        jobs = int(value)
    except ValueError as exc:
        raise SystemExit("--jobs must be 'auto' or a positive integer") from exc
    if jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    return min(jobs, max(shard_count, 1))


def run_shards(shards: list[TestShard], *, jobs: int, timeout: float, output_root: Path, cwd: Path, run_id: str | None = None) -> dict:
    started_at = utc_now()
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results: list[ShardResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [executor.submit(run_one_shard, shard, output_dir, timeout, cwd) for shard in shards]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.name)
    failed = [result.name for result in results if result.status != "passed"]
    finished_at = utc_now()
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started, 3),
        "jobs": jobs,
        "timeout_seconds": timeout,
        "status": "failed" if failed else "passed",
        "command": " ".join(sys.argv),
        "cwd": cwd.as_posix(),
        "summary_path": (output_dir / "summary.json").as_posix(),
        "git_snapshot": git_snapshot(cwd),
        "shards": [asdict(result) for result in results],
        "failed_shards": failed,
        "rerun_failed_command": f"{sys.executable} tests/run_shards.py --failed-from {(output_dir / 'summary.json').as_posix()} --jobs auto",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def print_summary(summary: dict) -> None:
    print(f"test_run: {summary['run_id']}")
    print(f"status: {summary['status']}")
    print(f"summary_json: {summary['summary_path']}")
    print(f"duration_seconds: {summary['duration_seconds']}")
    if summary["failed_shards"]:
        print("failed_shards:")
        for name in summary["failed_shards"]:
            shard = next(item for item in summary["shards"] if item["name"] == name)
            print(f"- {name} status={shard['status']} exit_code={shard['exit_code']}")
            print(f"  rerun: {shard['rerun_command']}")
        print(f"rerun_failed: {summary['rerun_failed_command']}")
    else:
        print("failed_shards: []")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Nilo test shards in isolated subprocesses.")
    parser.add_argument("--list", action="store_true", help="List available shards.")
    parser.add_argument("--all", action="store_true", help="Run all shards.")
    parser.add_argument("--changed", action="store_true", help="Run shards selected from git changed files.")
    parser.add_argument("--shard", action="append", help="Run one shard. May be repeated.")
    parser.add_argument("--shards", action="append", help="Comma-separated shard names.")
    parser.add_argument("--failed-from", help="Run failed shards from a summary path, run directory, or run id.")
    parser.add_argument("--jobs", default="1", help="'auto' or a positive integer.")
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-shard timeout in seconds.")
    parser.add_argument("--output-root", default=".nilo/test-runs")
    args = parser.parse_args(argv)

    if args.list:
        for name in shard_names():
            print(name)
        return 0

    cwd = Path.cwd()
    shards = selected_shards(args, cwd)
    jobs = parse_jobs(args.jobs, len(shards))
    summary = run_shards(shards, jobs=jobs, timeout=args.timeout, output_root=Path(args.output_root), cwd=cwd)
    print_summary(summary)
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
