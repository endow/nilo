# Overdrive

Overdrive reduces confirmation waits while advancing the current task. It can also continue along accepted roadmap items, but by default it stays within the current requested work.

`nilo next --do` advances only one safe daily step. Overdrive is the stronger execution mode for continuing the current task or accepted roadmap work.

Humans do not need to remember detailed execution options. Ask the AI agent in natural language:

```text
Proceed in overdrive mode along the accepted roadmap.
```

```text
Only work on this roadmap item, and stop if failures keep repeating.
```

Overdrive does not remove final human judgment. Nilo may bypass approval gates where appropriate, but it stops at safety gates.

## Scope

`nilo run --overdrive` and `nilo roadmap execute --overdrive` accept `--scope`.

- `task`: Default. Continue through implementation, verification, report, and completion for the current task. Do not automatically move to an unrelated next task.
- `commitment`: Continue through tasks under the current roadmap commitment.
- `project`: Target the current project.
- `queue`: Target the full queue. Use this only when explicitly allowing automatic progress into another task.

When a human says "overdrive everything", Nilo treats it as `task` by default. Before moving to another task, the AI stops and reports a boundary such as:

```text
The requested verification task is complete up to this point.
Another task task_xxx remains.
Continuing overdrive requires --scope queue or explicit approval.
```

## Safety Gates

- Destructive operation (`destructive_operation`)
- Secret or credential access (`secret_or_credential_access`)
- Billing or external publication (`billing_or_external_publication`)
- Delete operation (`delete_operation`)
- Maximum failure count exceeded (`max_failure_exceeded`)
- Out-of-scope design change (`out_of_scope_design_change`)
- Ambiguous specification (`ambiguous_specification`)
- Unexpected dirty working tree (`unexpected_dirty_working_tree`)

When stopped, the AI reports the reason and the human decision needed next.
