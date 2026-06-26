# Release 0.2.0 Preparation

## Summary

Prepare the 0.2.0 release handoff for Nilo.

## Proposed Changes

- Update project-owned version sources to 0.2.0.
- Check release-facing documentation for stale 0.1.10 references and update only the release scope.
- Add or update bilingual release notes with Japanese first and English second.
- Prepare suggested tag and GitHub CLI handoff commands without publishing anything.
- Record local verification evidence for tests, recipe validation, and final Nilo status.

## Rationale

The release recipe selected 0.2.0 because recent changes include CLI behavior, recipe behavior, roadmap or task flow, AI-facing workflow, and documentation changes. Those are user-facing enough to justify a minor release rather than a patch release.

## Success Criteria

- Project version sources report 0.2.0.
- Release notes for 0.2.0 include notable changes, verification, upgrade or install notes, and GitHub release handoff.
- Release notes are written with Japanese content before English content.
- Builtin recipes and release documentation remain valid.
- Verification evidence records the test suite or a documented reason it was not run.
- GitHub publication steps are prepared but not executed.

## Non Goals

- Do not implement unrelated product changes.
- Do not commit, tag, push, force, or create a GitHub release.
- Do not close the roadmap commitment.
- Do not resolve unrelated existing failure ledger entries.

## Autonomy Scope

- The agent may edit version files and release documentation required for 0.2.0.
- The agent may run local verification commands and record Nilo checks.
- The agent may prepare handoff commands for a human to run.

## Review Gates

- Human reviews the final changed files and release notes.
- Human decides whether to accept the release task evidence.
- Human decides whether to commit, tag, push, and publish the GitHub release.

## Evidence Policy

- List changed files in the final task report.
- Record version check output.
- Record verification command output with `nilo check`.
- Include the release notes draft text or release notes file path in the task report.
- Include GitHub release handoff notes in the task report.

## Suggested Tasks

- Implement release 0.2.0 version and release note updates.
- Verify release 0.2.0 readiness.
