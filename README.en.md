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

For review integration, the important point is that a real reviewer or agent session exists and that its result is recorded in Nilo. A connection that only appears available, or a fixed imported file, is not treated as a real review.

Reviewers are described by capability and availability, not only by names such as Codex or Claude Code. Local LLMs and OpenAI-compatible endpoints can be registered as thin local reviewers, but their low confidence and limitations are saved as limitations. They are not direct evidence for task completion. Completion still depends on tests, command output, diff inspection, and any needed human or trusted reviewer approval.

## Stored Files

Nilo stores project state in `.nilo/nilo.db`.

This repository does not commit local runtime files such as:

- `.nilo/`: state database, verification logs, temporary reports
- `HANDOFF.md`: optional human handoff file
- `.mcp.json`: local MCP settings
- Python caches, virtual environments, coverage output, and build artifacts

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
python -m unittest discover tests
```

For design details, see [docs/design.md](docs/design.md).

## License

Apache License 2.0.

See [LICENSE](LICENSE).
