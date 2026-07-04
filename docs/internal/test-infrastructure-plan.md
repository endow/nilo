# Test Infrastructure Plan

## Current Shape

- `tests/test_cli.py`: 11,796 lines. It is the main CLI behavior suite and mixes facade, project, roadmap, review, release, recipe, todo, workflow, and historical compatibility coverage.
- `tests/run_cli_group.py`: focused runner for named groups inside `tests.test_cli`. It gives quick feedback without importing or running the entire monolithic CLI suite.
- `tests/test_shards.py`: shard manifest. It maps CLI groups and unit/integration modules to named shard commands.
- `tests/run_shards.py`: local parallel shard runner. It executes named shards, writes stdout/stderr logs, records durations, emits rerun commands, and captures a git snapshot.
- `tests/run_test_durations.py`: duration helper for measuring test command timing.
- `tests/test_shard_runner.py`: unit tests for shard runner behavior.

## Split Priority

1. Extract roadmap tests from `tests/test_cli.py` into `tests/test_cli_roadmap.py`.
   Roadmap behavior has clear command boundaries and many long fixtures.
2. Extract review and quality command tests into `tests/test_cli_review.py` and `tests/test_cli_quality.py`.
   These already map to existing shard groups and have relatively independent fixtures.
3. Extract release and recipe workflow tests into `tests/test_cli_release.py` and `tests/test_cli_recipe.py`.
   These are higher risk because they chain state transitions and release metadata.
4. Keep facade smoke tests in `tests/test_cli.py` until the command surface has stable lightweight coverage elsewhere.

## Runner Inventory

- Keep `run_cli_group.py` while `tests/test_cli.py` remains monolithic. It is the cheapest way to run focused command groups.
- Keep `test_shards.py` as the source of truth for named local/CI shard coverage.
- Keep `run_shards.py` until pytest-xdist has equivalent rerun commands, per-shard logs, timeout handling, and git snapshot metadata.
- Keep `run_test_durations.py` until shard durations are recorded by the replacement runner or CI artifacts.
- Keep `test_shard_runner.py` as long as the custom runner remains.

## pytest-xdist Comparison

pytest-xdist can replace parallel execution once these conditions are met:

- Runtime is equal or better than `python tests/run_shards.py --all`.
- Failed-test output includes a simple rerun command comparable to each shard's `rerun_command`.
- CI artifacts preserve stdout/stderr or pytest logs per logical group.
- Timeout behavior is explicit and does not leave hung subprocess-backed tests.
- Changed-file targeted selection has an equivalent or a small wrapper.

Until those are true, delete no runner files. Prefer adding a compatibility wrapper that invokes pytest while preserving the current `run_shards.py` command shape.

## Coverage Guard

Before removing or replacing any runner, compare the old and new target sets:

```bash
PYTHONPATH=src python tests/run_shards.py --list
PYTHONPATH=src python -m unittest tests.test_shards tests.test_shard_runner
```

The replacement must cover all shard names from `tests/test_shards.py`, including CLI groups, integration shards, and unit shards.
