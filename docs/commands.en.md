# Commands and Stored Files

This document supplements the README with basic Nilo commands, stored files, and display-language behavior.

## Normal Work

Use `nilo work` as the normal work entrypoint. The caller declares side effects with `--intent inspect|change`; Nilo does not infer intent from specific words in the request. With no intent and no explicit work option, it safely defaults to `inspect` and creates no task.

```bash
nilo work "shorten the README" --intent change
nilo work "explain the current state" --intent inspect
nilo work --recipe bugfix "fix the review result import crash" --intent change
nilo check --task <task_id> "python -m unittest tests.test_cli"
```

`--intent inspect` cannot be combined with `--task`, `--recipe`, `--no-recipe`, or `--check`. `nilo work --check` is a shortcut that also records AI completion when verification succeeds. Use `nilo check --task <task_id> "..."` when you only want to record verification before the completion decision.

After upgrading an existing environment to this intent contract, run `nilo agent install --project <project_id> --target all` to regenerate the Codex and Claude operating rules. Regeneration is required because an old bare `nilo work "<request>"` instruction would make change requests read-only.

`status`, `next`, `start`, `check`, and `done` remain helper, advanced, or fallback commands.

## Status

`nilo status` is the lightweight current-position check. The default view avoids expensive diff-hash, roadmap, commit, and history summaries, and its git dirty indicator covers tracked-file changes only.

```bash
nilo status
nilo status --verbose
nilo status --audit
nilo status --ai
nilo next --do
```

Use `--verbose` for detailed status, `--audit` for stricter evidence checks, and `--ai` for agent-oriented context.
`nilo next --do` previews only a safe daily next-step candidate. In the initial implementation it does not execute the step; it prints the stop reason and next command.

## Human View

```bash
nilo view
```

`nilo view` opens a read-only local browser view. By default it binds only to `127.0.0.1:8765` and does not write to the DB.

Use `--no-open` if the browser should not open automatically, `--port` to choose another port, and `--format json` to print only summary JSON.

## Stored Files

Nilo stores project state in `.nilo/nilo.db` at the project root.

In this repository, generated local files are not committed:

- `.nilo/`: state DB, verification logs, temporary report files
- `HANDOFF.md`: optional human handoff file
- `.mcp.json`: local MCP configuration
- Python caches, virtual environments, coverage output, and build artifacts

Commit source code, tests, READMEs, design docs, AI-agent instructions, and other files meant to be shared by the project.

## Display Language

Internal state values, DB records, and JSON output use stable English identifiers.

Normal command output is primarily Japanese. `--ai` output is also primarily Japanese for human review, with internal values shown in parentheses where useful.

`--json` output is for integrations and is not localized.

## Help

The CLI help is the source of truth for exact command options.

```bash
nilo --help
nilo work --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```
