# Overdrive

Overdrive continuously advances AI-agent work along accepted roadmap items.

Humans do not need to remember detailed execution options. Ask the AI agent in natural language:

```text
Proceed in overdrive mode along the accepted roadmap.
```

```text
Only work on this roadmap item, and stop if failures keep repeating.
```

Overdrive does not remove final human judgment. Nilo may bypass approval gates where appropriate, but it stops at safety gates.

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
