# AI Context and `status --ai`

This document supplements how AI agents should read Nilo state.

## Entry Point

At the start of work, an AI agent checks the target repository with:

```bash
nilo work "<request>" --intent change --project <project_id>
nilo status --ai --project <project_id>
nilo next --project <project_id>
```

For normal changes, use `nilo work --intent change`; use `--intent inspect` for read-only requests. `status --ai` and `next` are fallback commands when `work` stops or more background context is needed.

If there is an active task and you use `nilo next`, follow only the first action. If an active recipe is running, recipe `next` is the only relevant next action.

## Default `status --ai` Output

`nilo status --ai` prints a short work card by default: project, active task, next action, blocker summary, latest verification, latest review, required commands, and detail commands. It does not expand full evidence, roadmap text, or review findings on every turn.

Fetch details on demand:

```bash
nilo status --ai --verbose
nilo task status --task <task_id> --ai
nilo evidence show --task <task_id> --ai
nilo review status --task <task_id> --format json
nilo roadmap status --project <project_id> --ai
nilo failure list --project <project_id>
```

Evidence is retained; completion, audit, and evidence-show surfaces keep the stricter checks.

## Context Size

Set `NILO_AI_CONTEXT_MAX_CHARS` to tune the compact AI-context character budget. The value is read when the Nilo process starts.

## Completion

Do not treat stale, missing, or failed evidence as completion support. Unresolved review findings also remain items to resolve or explicitly judge before completion.

Final completion, commits, force operations, and roadmap close decisions require human judgment. AI agents gather the evidence and report verification and review state.
