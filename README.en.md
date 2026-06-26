# Nilo

> Japanese version: [README.md](README.md)
>
> The Japanese README is the primary document. This English README is a supplementary introduction.

Nilo is a local development tool for not taking an AI agent's "done" at face value.

Nilo records the **current state, completion criteria, verification evidence, and review results** for work delegated to Codex, Claude Code, ChatGPT, local LLMs, and other AI agents inside the project's `.nilo/` directory.

It keeps workflow state out of a vendor's chat history, so humans can inspect the same evidence and make an acceptance decision even when AI agents are swapped.

```text
Human asks an AI agent to do work
    ↓
AI uses Nilo to record task state, checks, reports, and reviews
    ↓
Human decides to accept, send back, or ask for more verification
```

When you delegate development work to Codex, Claude Code, or another coding agent, the conversation can look productive while important questions stay unclear:

- what the agent is working on now
- what conditions define completion
- whether the work was actually verified
- whether the verification log is only self-reported or actually recorded
- whether review findings are still unresolved
- who finally accepted the work as complete
- whether the workflow state is locked inside a specific vendor's chat history or session

Nilo is not a tool that expects humans to memorize and run commands all day. Most operations are meant to be run by AI agents behind the scenes. Humans should look at what is happening, what remains unresolved, and whether the work is acceptable.

Nilo is not a task management app. It is a local workbench for keeping the evidence of AI-assisted development available for later inspection.

Nilo is currently an experimental tool for stabilizing AI-assisted development workflows. APIs, database schema, and CLI output may change. It is not a replacement for production safety, sandboxing, authentication, CI, or final human judgment. Nilo is not a security boundary; it records evidence, audit history, and workflow discipline.

## Why Nilo Exists

In AI-assisted coding, the conversation alone often does not preserve the evidence needed to understand where the work stands or why it should be considered complete.

If completion criteria, verification logs, review results, and final acceptance are ambiguous, it becomes hard to check later whether the task was really done.

If workflow state is locked inside a specific AI vendor's chat history or session, context is lost when you move the work to another agent.

The more you switch between Codex, Claude Code, ChatGPT, and local LLMs, the more important it becomes to decide where the authoritative workflow record lives.

Nilo records that ambiguity in `.nilo/nilo.db`, so you do not have to rely only on the AI agent's own completion report. It keeps workflow state in the project, where it can be inspected later.

## Core Idea

> Evidence Before Trust
> Look at real changes and verification evidence before trusting an AI agent's report.

Here, evidence does not mean an absolute authenticity guarantee. In Nilo, evidence means verification results, reviewer results, git snapshots, and human completion decisions recorded through the normal path separately from an AI agent's own report.

Another premise is that the authoritative workflow record should live in the project, not on the AI vendor's side.

AI agents can be swapped. Completion criteria, verification evidence, review results, and acceptance decisions should remain in the project.

When an AI agent says "done", Nilo treats that as a candidate state, not final completion. Completion happens only after the criteria, changed files, verification results, and review results are inspected and accepted.

Nilo records things such as:

- the current task
- instructions given to the AI agent
- completion criteria
- verification commands and results
- the git snapshot associated with checks and reviews
- AI reports
- human or AI review results
- failures that should not be repeated
- policy notes for later reference

Verification and review results are tied to a specific code state. If the code changes after a check, Nilo can distinguish older evidence as `stale` instead of treating it as evidence for the current tree.

### Failure logs

Nilo records missing evidence, metadata mismatches, and human rejections in `failure_logs`.

Failure logs are observations, not automatic rules.  
Nilo does not turn them into generated instructions or hidden requirements.

They are a ledger for humans and future agents to understand where previous work failed.

## The Boundary Nilo Enforces

Nilo is not a security boundary that prevents malicious users or direct writes to the database.

Nilo is designed for cooperative AI agents such as Codex, Claude Code, ChatGPT, and local LLMs working through Nilo's normal CLI / MCP paths.

Within that scope, Nilo prevents these states from being confused:

- a state where an AI agent only reported "done"
- a state where Nilo actually ran and recorded a verification command
- a state where an external AI agent reported verification
- a state where a reviewer left findings against a specific snapshot
- a state where a human accepted a specific snapshot as complete

These are not treated as the same thing.

Verification results carry a `source` that distinguishes whether the result was executed by Nilo or reported by an external AI agent. In common cases, a result run locally by Nilo is `nilo_executed`, while a result submitted by an external AI agent is `agent_reported`.

Verification, review, and completion decisions are also saved with the `git_head`, `git_diff_hash`, and `working_tree_dirty` they targeted. If the code changes after the work, older checks and reviews are not treated as evidence for current completion; Nilo distinguishes them as `stale` when displaying state.

Unresolved reviewer findings remain visible as items to check before completion. `actor=ai` cannot finalize task completion by its own judgment, and a human decision to accept a specific snapshot is recorded separately from the AI agent's work report.

Nilo prevents a cooperative AI agent, on the normal path, from skipping work, presenting self-reports as verified results, or moving toward completion while hiding old verification or unresolved findings.

Nilo does not prevent direct database modification, misuse of OS privileges, malicious actors, or a human forcing a completion decision.

For the detailed design boundary, see [docs/design.md](docs/design.md).

## Install

Nilo requires Python 3.12 or later and Git.

```bash
git clone https://github.com/endow/nilo.git
cd nilo
python -m pip install -e .
```

You can check this repository with:

```bash
python -m unittest discover tests
nilo status --project nilo
```

When recording verification during a Nilo task, use `nilo check "python -m unittest discover tests" --project nilo --timeout 300` to avoid short AI runner defaults.

## Update Nilo

If Nilo was installed from a git checkout, update it with:

```bash
nilo upgrade
```

This checks the local repository state, runs `git pull --ff-only`, reinstalls Nilo, and runs migrations. If `.nilo/nilo.db` exists, Nilo creates a backup under `.nilo/backups/` before migration.

If local changes are present, Nilo stops before updating. Commit, stash, or discard those changes before running `nilo upgrade` again.

To see what would run without applying changes:

```bash
nilo upgrade --dry-run
```

## Database Backups

Nilo stores its state database at `.nilo/nilo.db`. Do not place the live database or its `.db-wal` / `.db-shm` files directly in a cloud sync folder. Use verified backup artifacts instead:

```bash
nilo backup
nilo backup --reason daily
nilo backup --export ~/NiloBackups
nilo backup --encrypt --recipient age1... --export ~/NiloBackups
nilo backups
nilo backups prune --keep 30
nilo restore --decrypt ~/NiloBackups/nilo-20260624-180000.db.age
```

`nilo backup` writes a `.db` file and adjacent `.meta.json` under `.nilo/backups/`, including `integrity_check` and sha256. `nilo backups prune --keep 30` keeps the newest 30 prunable backups and deletes older ones. By default, `manual`, `before-upgrade`, `before-migration`, and `before-restore` backups are protected; use `--dry-run` to preview deletions and `--include-reason daily` to scope pruning by reason.

For external handoff, configure `.nilo/config.toml` with an argv-style `backup.post_command`. Nilo does not run it through a shell. It only substitutes `{backup_path}`, `{meta_path}`, `{reason}`, `{sha256}`, `{encrypted}`, `{exported_backup_path}`, and `{exported_meta_path}`. Unknown `{...}` tokens and literal braces are rejected. Nilo records the result in local backup metadata and mirrors it to exported metadata when an export artifact exists.

```toml
[backup]
post_command = ["rclone", "copy", "{backup_path}", "remote:nilo-backups"]
```

## Getting Started

Initialize Nilo once at the root of a project:

```bash
nilo init
```

This creates or updates local runtime files such as:

- `.nilo/nilo.db`: SQLite database for task state
- `.nilo/agent-instructions.md`: shared runtime instructions for AI agents
- `AGENTS.override.md` / `CLAUDE.local.md`: per-worktree local instruction files

After that, ask your AI agent as usual:

```text
Update the README. Check Nilo's state before you start.
```

The agent should check the current state, create or continue a task, read the instructions, run verification, and report back through Nilo.

### Route Large Work Through Roadmap

Small changes can proceed as normal tasks.

Large work should go through roadmap first: multi-module changes, schema/migration changes, CLI behavior changes, AI-facing output changes, or work that requires docs and tests together.

```bash
nilo roadmap discuss
nilo roadmap accept
nilo roadmap task-plan
```

Roadmap keeps large AI work from turning into one unchecked implementation step.

## Human Workflow

Humans usually do not need to know the command set. The normal interface is natural language:

```text
What's next?
```

```text
Is anything still unresolved?
```

```text
Did verification pass?
```

```text
Can I accept this as complete?
```

The AI agent reads Nilo as needed and explains the current state. The human decides whether to accept the work, send it back, or ask for additional verification.

Completion, rejection, commits, and final direction changes are human decision points. Actor names in Nilo are audit labels, not OS-level or Git-level authorization. Nilo records who accepted what, but it is not an authorization system that can fully prevent misuse.

## Overdrive Mode

Nilo includes **Overdrive Mode** for continuously advancing AI agent work along an accepted roadmap commitment.

In normal mode, humans confirm key moments such as instruction generation, task progression, evidence acceptance, and roadmap assessment. In Overdrive Mode, those approval gates can be bypassed when appropriate so Nilo can move to the next incomplete task.

```bash
nilo run --project <project> --overdrive
```

To target a specific roadmap commitment, pass `--commitment`:

```bash
nilo run --project <project> --overdrive --commitment <commitment_id>
```

The failure limit can be set with `--max-failures`:

```bash
nilo run --project <project> --overdrive --max-failures 3
```

Overdrive Mode does not remove human judgment. Nilo can bypass approval gates, but safety gates remain. For example, it stops for:

- destructive operations (`destructive_operation`)
- access to secrets or credentials (`secret_or_credential_access`)
- billing or external publication (`billing_or_external_publication`)
- delete operations (`delete_operation`)
- exceeding the failure limit (`max_failure_exceeded`)
- out-of-scope design changes (`out_of_scope_design_change`)
- ambiguous specifications (`ambiguous_specification`)
- an unexpected dirty working tree (`unexpected_dirty_working_tree`)

A final human review checkpoint is still required.

Overdrive Mode is therefore not a way to let AI agents act without bounds. It is an operating mode for preserving evidence, verification results, review results, and unresolved concerns while avoiding unnecessary stops for small human approvals. Nilo still does not treat an AI agent's self-report as completion by itself, and it still leaves humans with the evidence needed to decide whether to accept the work. Nilo is not a security boundary; it is an evidence, audit, and workflow discipline tool.

## What AI Agents Do

AI agents use Nilo behind the scenes to:

- check the current state
- create task units
- read instructions and completion criteria
- record verification results
- leave work reports
- request human or AI review
- import review results
- return to unresolved findings when needed

These operations exist to preserve the evidence behind an AI agent's report. They are not meant to be a daily manual checklist for humans.

## AI Agent Integration

Nilo is not tied to a specific AI agent. Codex, Claude Code, ChatGPT, local LLMs, and other tools can use it through the CLI or MCP (Model Context Protocol).

MCP lets an AI agent read Nilo state and write verification or review results through conversation tools.

`nilo init` writes runtime instructions to local files rather than tracked files such as `CLAUDE.md` or `AGENTS.md`:

- Claude Code: `CLAUDE.local.md`
- Codex: `AGENTS.override.md`
- Shared generated body: `.nilo/agent-instructions.md`

These files are runtime files and should not be committed. Nilo uses Git local exclude instead of tracked `.gitignore` for `.nilo/` and local override files. Run `nilo init` in each new clone or worktree so the local ignore settings are prepared.

Older versions of Nilo could write generated blocks into `CLAUDE.md` or `AGENTS.md`. To inspect old blocks:

```bash
nilo migrate
```

To remove old generated blocks from tracked files and update local runtime files:

```bash
nilo migrate --apply
```

For review integration, the important point is that a real reviewer or agent session exists and that its result is recorded in Nilo. A connection that only appears available, or a fixed imported file, is not used as direct evidence for completion.

Review results are not a replacement for `VerificationRun`. Reviewers are described by capability, availability, limitations, and the target snapshot, not only by names such as Codex or Claude Code. Review results that do not match the current snapshot are distinguished as `stale`. Local LLMs and OpenAI-compatible endpoints can be registered as thin local reviewers, but their low confidence and limitations are saved as limitations. They are not direct evidence for task completion. Completion still depends on tests, command output, diff inspection, and any needed human or trusted reviewer approval.

## Stored Files

Nilo stores project state in `.nilo/nilo.db`.

This repository does not commit local runtime files such as:

- `.nilo/`: state database, verification logs, temporary reports
- `HANDOFF.md`: optional human handoff file
- `.mcp.json`: local MCP settings
- Python caches, virtual environments, coverage output, and build artifacts

## Recipes

A recipe helps start common work in a consistent way.

For example, these requests often need the same kind of setup:

- update documentation only
- summarize design points before implementation
- make a small change with verification and review focus

In those cases, you can ask an AI agent like this:

```text
Use a recipe for this work.
```

```text
Create a work item from the README update recipe.
```

The agent checks the available recipes, chooses one that fits the work, and creates a Nilo work item. After that, the work proceeds like any other Nilo work: instructions, completion criteria, verification, reports, and reviews are recorded.

Nilo includes a few starter recipes, such as:

- write a design note
- update documentation
- make a small implementation change
- update a release version, bilingual release notes, and the GitHub release handoff

Recipes save you from explaining the same work setup every time. They are not a mechanism for letting AI run everything automatically. Work created from a recipe still ends with a human deciding whether to accept it after reading the verification results and report.

You can also ask an AI agent to create a project recipe for repeated work. You do not need to start by editing YAML yourself. Describe what you want to reuse in natural language:

```text
Turn the release note update work into a recipe I can reuse next time.
```

```text
This project has specific checks for README updates. Create a README update recipe for it.
```

```text
Turn the work we just did into a recipe so we can follow the same approach next time.
```

The agent asks or infers the purpose, allowed scope, non-goals, completion criteria, and verification points, then saves the result as a project recipe. You review the finished recipe description and decide whether that workflow is the one you want.

After a recipe exists, you can ask:

```text
Use the release note update recipe we created earlier.
```

The storage format is handled by the agent. When needed, recipes are saved under `.nilo/recipes/` in the project.

If you want to inspect available recipes directly, use `nilo recipe list`.

### Version Advice In The Release Recipe

When `target_version` is omitted, the release recipe can suggest a patch or minor bump from the current version, latest git tag, and changed files.

Small fixes usually suggest a patch bump.

User-facing changes such as CLI behavior, DB migrations, recipes, AI-facing output, roadmap/review/failure workflows, or documentation for new workflows may suggest a minor bump.

Explicit `target_version` values are never overwritten.

## Developer Notes

Use `--help` for CLI details:

```bash
nilo --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```

Run tests with:

```bash
nilo check "python -m unittest discover tests" --project nilo --timeout 300
```

To run a focused slice of `tests.test_cli`, use the helper runner. It is outside unittest discovery, so it does not add duplicate tests to the full suite.

```bash
python tests/run_cli_group.py review
python tests/run_cli_group.py verification
python tests/run_cli_group.py roadmap
```

For design details, see [docs/design.md](docs/design.md).

## License

Apache License 2.0.

See [LICENSE](LICENSE).
