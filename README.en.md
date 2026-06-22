# Nilo

> Japanese version: [README.md](README.md)
>
> The Japanese README is the primary document. This English README is a supplementary introduction.

## What is Nilo?

Nilo is a workflow discipline tool for AI-assisted development.

It does not treat an AI agent's done report as sufficient proof that work is complete. Instead, it asks for evidence, reviewable state, and human confirmation.

Nilo is not a security boundary. It is an evidence, audit, and workflow discipline layer for solo developers and small teams using coding agents.

## Why Nilo exists

AI coding agents are useful, but they can report completion before the work is actually verified.

Nilo exists to make that gap visible.

It helps record:

- what was requested
- what was changed
- what evidence was provided
- what still needs human review
- what failures should not be repeated

## Core idea

Evidence before trust.

Nilo treats completion as a state that must be supported by evidence, not as a statement made by an AI agent.

## Who is it for?

Nilo is designed for people who use AI coding agents in real development work and want a lightweight way to keep the process honest.

It is especially focused on:

- solo developers
- small teams
- AI-assisted coding workflows
- review-before-completion discipline

## What Nilo is not

Nilo is not:

- a security sandbox
- a full project management system
- a replacement for tests or code review
- a general-purpose agent framework

## How Nilo fits into a workflow

In a typical workflow, a human asks an AI agent to do a task. The agent uses Nilo to record the task state, evidence, checks, reports, and review results. The human then decides whether to accept the work, send it back, or ask for more verification.

The CLI and MCP integrations are implementation details for agents and advanced users. The main point is the discipline: completion should be reviewable, not just asserted.

## Current status

Nilo is under active development. APIs, database schema, and CLI output may change.

The Japanese documentation is the primary source of truth. The English documentation may lag behind the Japanese version.

## License

Apache License 2.0.

See [LICENSE](LICENSE).
