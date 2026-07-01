# Backup and Upgrade

Nilo stores state in `.nilo/nilo.db`. Do not place the live DB or its `.db-wal` / `.db-shm` files directly in a cloud-sync folder.

## DB Backup

Humans do not need to memorize backup commands. Ask the AI agent when needed:

```text
Back up the Nilo DB.
```

```text
Create a Nilo DB backup suitable for external handoff.
```

```text
Clean up old backups within the safe range.
```

Backups are created under `.nilo/backups/` as a `.db` file plus adjacent `.meta.json`. Metadata includes `integrity_check` and sha256.

External handoff can be configured in `.nilo/config.toml` as an argv-style command. Nilo does not run it through a shell.

```toml
[backup]
post_command = ["rclone", "copy", "{backup_path}", "remote:nilo-backups"]
```

## Upgrade

If Nilo was installed from a git checkout, humans can usually ask an AI agent to update it.

```text
Update Nilo. Check the state before and after the update.
```

```text
Before actually updating, show me what would run.
```

The update flow checks local repository state, performs a fast-forward update, reinstalls Nilo, and runs migrations. If `.nilo/nilo.db` exists, Nilo creates a `reason=before-upgrade` backup under `.nilo/backups/` before migration.

If local changes are present, the update stops first. Commit, stash, or discard the changes before rerunning it.

For backup and restore design boundaries, see [design.md](design.md).
