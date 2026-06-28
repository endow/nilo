from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from run_shards import load_failed_shards, parse_jobs, run_shards, selected_shards
    from test_shards import UNIT_MODULES, TestShard, auto_jobs, get_shard, shard_names, shards_for_changed_files
except ModuleNotFoundError:
    from tests.run_shards import load_failed_shards, parse_jobs, run_shards, selected_shards
    from tests.test_shards import UNIT_MODULES, TestShard, auto_jobs, get_shard, shard_names, shards_for_changed_files


class ShardDefinitionTests(unittest.TestCase):
    def test_shard_list_includes_cli_and_unit_shards(self) -> None:
        names = shard_names()
        self.assertIn("cli:recipe", names)
        self.assertIn("unit:backup", names)
        self.assertIn("unit:other", names)
        self.assertIn("tests.test_shards", UNIT_MODULES["unit:other"])

    def test_cli_recipe_resolves_to_cli_group_runner(self) -> None:
        shard = get_shard("cli:recipe")
        self.assertEqual(shard.command[1:], ("tests/run_cli_group.py", "recipe"))

    def test_jobs_auto_caps_by_cpu_shards_and_eight(self) -> None:
        self.assertEqual(auto_jobs(20, 64), 8)
        self.assertEqual(auto_jobs(3, 64), 3)
        self.assertEqual(auto_jobs(5, None), 2)
        self.assertEqual(parse_jobs("auto", 2), 2)

    def test_changed_file_mapping_selects_expected_shards(self) -> None:
        self.assertEqual(shards_for_changed_files(["src/nilo/backup.py"]), ["unit:backup"])
        self.assertEqual(shards_for_changed_files(["src/nilo/review_dispatcher.py"]), ["cli:review", "unit:review_dispatcher"])
        self.assertEqual(shards_for_changed_files(["src/nilo/verification.py"]), ["cli:verification", "unit:verification"])
        self.assertEqual(shards_for_changed_files(["unknown.file"]), ["cli:compat", "cli:task", "unit:other"])


class ShardRunnerTests(unittest.TestCase):
    def test_failed_shard_is_recorded_in_summary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shard = TestShard("fake:fail", (sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"))
            summary = run_shards([shard], jobs=1, timeout=10, output_root=root / ".nilo" / "test-runs", cwd=root, run_id="run_fail")

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["failed_shards"], ["fake:fail"])
            self.assertEqual(summary["shards"][0]["exit_code"], 7)
            summary_path = root / ".nilo" / "test-runs" / "run_fail" / "summary.json"
            self.assertTrue(summary_path.exists())
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["failed_shards"], ["fake:fail"])

    def test_timeout_shard_is_recorded_in_summary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shard = TestShard("fake:timeout", (sys.executable, "-c", "import time; time.sleep(5)"))
            summary = run_shards([shard], jobs=1, timeout=0.1, output_root=root / ".nilo" / "test-runs", cwd=root, run_id="run_timeout")

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["failed_shards"], ["fake:timeout"])
            self.assertEqual(summary["shards"][0]["status"], "timeout")
            stderr_log = Path(summary["shards"][0]["stderr_log"])
            self.assertIn("timed out", stderr_log.read_text(encoding="utf-8"))

    def test_failed_from_loads_only_failed_shards(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            summary_dir = root / ".nilo" / "test-runs" / "run_1"
            summary_dir.mkdir(parents=True)
            (summary_dir / "summary.json").write_text(json.dumps({"failed_shards": ["cli:recipe"]}), encoding="utf-8")

            self.assertEqual(load_failed_shards("run_1", root), ["cli:recipe"])
            self.assertEqual(load_failed_shards(str(summary_dir / "summary.json"), root), ["cli:recipe"])

    def test_selectors_are_mutually_exclusive(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            args = type(
                "Args",
                (),
                {"all": True, "failed_from": None, "changed": False, "shard": ["cli:recipe"], "shards": None},
            )()

            with self.assertRaises(SystemExit) as context:
                selected_shards(args, root)

            self.assertIn("choose only one shard selector", str(context.exception))


if __name__ == "__main__":
    unittest.main()
