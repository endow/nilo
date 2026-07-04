from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


RunCommand = Callable[[list[str], Path], CommandResult]


def package_location() -> Path:
    return Path(__file__).resolve().parent


def run_command(command: list[str], cwd: Path, *, oserror_returncode: int | None = None) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        if oserror_returncode is None:
            raise
        return CommandResult(oserror_returncode, "", str(exc))
    return CommandResult(completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n"))
