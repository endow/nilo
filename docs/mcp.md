# MCP 連携

Nilo は通常の CLI だけでなく MCP (Model Context Protocol) 経由でも利用できます。

MCP が callable であることと、現在の repository に対して正しいことは別です。MCP response の identity を確認し、repository / project / git root / DB path が現在作業中のリポジトリと一致しない場合は使いません。

## Identity guard

Nilo MCP は、参照中の repository、project、git root、DB path を identity として表示します。

`expected_project` は Nilo DB 内の任意の project id ではなく、通常は repository directory name として扱う repository identity guard です。不一致時は `ok: false`、`error: "repository_mismatch"` を返し、通常の status payload は返しません。

Fallback は対象 repository の作業ディレクトリで CLI を使います。

```bash
nilo mcp doctor
nilo status --ai
nilo next
```

## Multi-workspace

Nilo MCP は、既定では MCP server を起動した repository の `.nilo/nilo.db` を使います。

複数 repository を同時に扱う場合は、MCP tool の呼び出しに `project_root` または `workspace` を指定できます。

```json
{
  "project_root": "/path/to/Chiffon"
}
```

Workspace を登録する場合:

```bash
nilo workspace add Chiffon --root /path/to/Chiffon
nilo workspace list
```

登録後は、MCP tool に workspace 名を渡せます。

```json
{
  "workspace": "Chiffon"
}
```

MCP response の `repository_name` / `db_path` が対象 repository と一致していることを確認してください。
