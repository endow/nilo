# Development Notes

This document is for people developing Nilo itself. Normal users do not need it.

## CLI Help

```bash
nilo --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```

## Test Policy

Choose `quick`, `targeted`, or `full` based on the purpose of the check. Timeouts are guardrails for the chosen scope, not a way to make full-suite verification the default for every task.

```bash
nilo check --task <task_id> "python -m unittest tests.test_verification" --project nilo --mode quick --timeout 60
nilo check --task <task_id> "python tests/run_cli_group.py verification" --project nilo --mode targeted --timeout 120
nilo check --task <task_id> "python tests/run_shards.py --all --jobs auto" --project nilo --mode full --timeout 300
```

`quick` is a narrow smoke check, `targeted` covers the changed module or a focused CLI group, and `full` is for releases or broad, high-risk changes.

Verification levels are not skipped verification; they separate responsibilities by stage of work.

- changed check: `python tests/run_shards.py --changed --jobs auto`. It selects shards from changed files for fast in-progress feedback.
- smoke / compat check: short checks for CLI startup, compatibility entry points, and basic output.
- full check: `python tests/run_shards.py --all --jobs auto`. Use it before release publication or for broad changes.
- audit snapshot: a strict check that evidence and completion decisions still match the current git snapshot.

In the release workflow, `release prepare` runs a changed check when no reusable full verification exists and reports `full_check: deferred`. `release publish` always verifies that a valid full check exists before public operations; if one is missing, it runs `RELEASE_FULL_CHECK_COMMAND`. If the full check fails, Nilo does not proceed to tag, push, or create a GitHub release.

Use `nilo check` with `--task` by default. It may be omitted only when there is exactly one safe unfinished verification target.

## During Changes

```bash
python tests/run_shards.py --changed --jobs auto
nilo test plan --changed
nilo test run --changed
```

## Before Completion

Run the full equivalent test suite with shard parallelism. Results are stored in `.nilo/test-runs/<run_id>/summary.json` and shard stdout / stderr logs. On failure, Nilo shows failed shards and rerun commands.

```bash
python tests/run_shards.py --all --jobs auto
nilo test run --full
nilo test rerun-failed <run_id-or-summary-json>
```

The older serial test command remains available for compatibility.

```bash
python -m unittest discover tests
```

Focused `tests.test_cli` groups can be run with the helper.

```bash
python tests/run_cli_group.py review
python tests/run_cli_group.py verification
python tests/run_cli_group.py roadmap
```

For design details, see [design.md](design.md).
