# Recipes

Recipes start common work in a consistent way.

For change requests, `nilo work --intent change` selects the needed recipe from the request. Use `--intent inspect` for read-only requests. `nilo recipe run` remains for explicit runs, recipe debugging, release work, and other advanced paths.

Examples:

- Update documentation only
- Write a design note before implementation
- Make a small implementation change with verification and review expectations
- Fix a bug with reproduction, cause analysis, regression testing, related checks, and verification evidence
- Improve performance with before/after measurements, comparison, correctness verification, and side-effect checks
- Prepare release versioning, Japanese and English release notes, and GitHub release handoff

Humans can ask in natural language:

```text
Use the README update recipe for this.
```

```bash
nilo work "Use the README update recipe for this" --intent change
```

```text
Turn the work we just did into a recipe so we can repeat it next time.
```

When needed, project recipes are saved under `.nilo/recipes/`.

## Perf Recipe

`perf` is the standard recipe for measuring slow work, identifying the bottleneck, and recording before/after comparison evidence.

```bash
nilo recipe run perf --project nilo
```

Normal work can start through `work`:

```bash
nilo work "full check is too slow; improve it" --intent change --project nilo
```

`performance` and `performance-investigation` are aliases for `perf`. Its completion contract includes the target, measurement conditions, baseline measurement, bottleneck analysis, change summary, after measurement, comparison result, correctness verification, and side-effect check.

## Release Recipe Version Suggestions

If `target_version` is omitted, the release recipe suggests the next version from the current version and latest git tag.

Small fixes suggest a patch version:

```bash
0.1.9 -> 0.1.10
```

Changes to major feature areas such as CLI additions, DB schema or migrations, recipes, AI-facing output, roadmap, review, or failure logs suggest a minor version:

```bash
0.1.9 -> 0.2.0
```

When a minor version is recommended, Nilo prints the reason and the rerun command. If `target_version` is explicitly provided, Nilo does not override it.

```bash
nilo recipe run release --project nilo --var target_version=0.2.0
```

The release task created at the start owns preparation, failure fixes, re-verification, publication, and completion or cancellation. A failed verification pauses the run as `paused_for_fix`; fix it in that same task and run `nilo release resume --project <project_id>`. Nilo does not create a separate fix task or child task. To abandon the release, use `nilo release cancel --project <project_id> --reason <reason> --human-confirm`. Publication still requires explicit human approval.

Recipe YAML instructions, acceptance criteria, and completion contracts are declarative documentation. They do not turn `steps` into a generic execution engine; release-specific orchestration controls the state and public operations. Completion and cancellation close the recipe run and its task in one transaction.

For recipe design boundaries, see [design.md](design.md).
