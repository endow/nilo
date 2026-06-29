from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
current_pythonpath = os.environ.get("PYTHONPATH", "")
if str(SRC_ROOT) not in current_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), *([current_pythonpath] if current_pythonpath else [])])


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._started_at: dict[unittest.case.TestCase, float] = {}
        self.durations: list[tuple[float, str]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self._started_at[test] = time.monotonic()
        super().startTest(test)

    def stopTest(self, test: unittest.case.TestCase) -> None:
        started = self._started_at.pop(test, None)
        if started is not None:
            self.durations.append((time.monotonic() - started, test.id()))
        super().stopTest(test)


class TimingRunner(unittest.TextTestRunner):
    resultclass = TimingResult


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run unittest targets and print slowest test method durations.")
    parser.add_argument("targets", nargs="*", default=["tests.test_cli"], help="unittest module/class/method targets")
    parser.add_argument("--top", type=int, default=20, help="number of slow tests to print")
    parser.add_argument("--verbosity", type=int, default=1)
    args = parser.parse_args(argv)

    suite = unittest.defaultTestLoader.loadTestsFromNames(args.targets)
    runner = TimingRunner(verbosity=args.verbosity)
    result = runner.run(suite)

    print(f"slowest_tests_top_{args.top}:")
    for duration, test_id in sorted(result.durations, reverse=True)[: args.top]:
        print(f"{duration:.3f}s {test_id}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
