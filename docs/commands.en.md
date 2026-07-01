# Commands and Stored Files

This document supplements the README with basic Nilo commands, stored files, and display-language behavior.

## Status

`nilo status` is the lightweight current-position check. The default view avoids expensive diff-hash, roadmap, commit, and history summaries, and its git dirty indicator covers tracked-file changes only.

```bash
nilo status
nilo status --verbose
nilo status --audit
nilo status --ai
```

Use `--verbose` for detailed status, `--audit` for stricter evidence checks, and `--ai` for agent-oriented context.

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
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```
