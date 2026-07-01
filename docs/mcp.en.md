# MCP Integration

Nilo can be used through the CLI or through MCP (Model Context Protocol).

Being able to call an MCP tool does not prove it is looking at the correct repository. Check the MCP response identity, and do not use it if repository, project, git root, or DB path does not match the current working repository.

## Identity Guard

Nilo MCP reports the repository, project, git root, and DB path it is reading.

`expected_project` is usually a repository identity guard based on the repository directory name, not an arbitrary project id inside the Nilo DB. On mismatch, the tool returns `ok: false` and `error: "repository_mismatch"` without a normal status payload.

Fallback is to use the CLI from the target repository directory.

```bash
nilo mcp doctor
nilo status --ai
nilo next
```

## Multi-workspace

By default, Nilo MCP uses the `.nilo/nilo.db` of the repository where the MCP server was started.

When multiple repositories are open, pass `project_root` or `workspace` to the MCP tool call.

```json
{
  "project_root": "/path/to/Chiffon"
}
```

To register a workspace:

```bash
nilo workspace add Chiffon --root /path/to/Chiffon
nilo workspace list
```

After registration, pass the workspace name to MCP tools.

```json
{
  "workspace": "Chiffon"
}
```

Confirm that `repository_name` and `db_path` in the MCP response match the target repository.
