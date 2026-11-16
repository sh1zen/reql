# REQL MCP Server

REQL exposes bounded repository context and shared engineering work through
stdio or HTTP JSON-RPC. `mcp.tools` owns handlers and schemas; `mcp.server` owns
the standard-library transports. The core system does not require MCP.

Successful tool content is compact JSON text. Formatting preserves field values,
source whitespace and Unicode; clients decode it with a standard JSON parser.
Shared retrieval behavior is documented in [CLI retrieval](CLI.md#retrieval-commands),
and work semantics in [Engineering coordination](COORDINATION.md).

## Start the Server

After installing REQL and its dependencies:

```bash
reql-mcp --read-only
reql-mcp --config ./reql.conf --set scan.max_file_size_mb=2 --read-only
```

From a source checkout:

```bash
PYTHONPATH=src python -m mcp.server --read-only
```

On PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m mcp.server --read-only
```

HTTP requires a Bearer key. This example binds only the local machine:

```bash
REQL_MCP_API_KEY="change-this-key" reql-mcp --transport http --host 127.0.0.1 --port 8765 --read-only
```

`--api-key-env NAME` selects another environment variable; `--api-key` supplies
the key directly. Requests go to `/mcp`; `/health` is the health endpoint.
Binding `0.0.0.0` exposes all interfaces; see [Security notes](#security-notes).

## Tools

| Read tool | Result |
| --- | --- |
| `query_context` | Working set, owners, tests, impact and bounded source/read commands |
| `query_explore` | Selected dependency views such as owners, callers and serialization paths |
| `query_graph` | Seeds, expanded nodes/edges, source records and graph diagnostics |
| `query_memories` | Ranked memory/source texts |
| `inspect_node` | Node, location, source excerpt and bounded adjacent relations |
| `reql_query` | REQL columns, bounded rows and diagnostics; rejects mutating analysis statements |
| `reql_project_status` | Registered project and last-compilation status |
| `reql_work_context` | Focused shared work without opening the canonical project graph |
| `reql_work_overview` | Project direction, workstreams, execution and next steps |

Write tools are available unless the server starts with `--read-only`:

| Write tool | Effect |
| --- | --- |
| `reql_compile_project` | Incrementally compiles a project |
| `reql_watch_project` | Performs bounded filesystem monitoring and compilation |
| `reql_hubs` | Calculates and persists hub scores |
| `reql_work_record` | Writes a revision-checked shared record with explicit provenance |

Read-only mode hides and rejects write tools. The client controls authorization;
the server does not provide an interactive approval step.

Every tool accepts `storage_path` and optional `config_path`/`config_overrides`.
Configuration uses the same owner and precedence as the CLI; see
[Configuration](CONFIGURATION.md#overrides). Project tools discover `reql.conf`
from their project path upward when no explicit config is supplied.
The server also honors `REQL_CONFIG`, `REQL_CONFIG_OVERRIDES`, `--config` and
repeated `--set` overrides.

Limits depend on the tool: `top_k` is capped at 50, `max_depth` at 5, graph nodes
at 200, graph edges at 400, context items at 50, memory text at 2,000 characters
per snippet and REQL rows at 200. Query-context defaults are `20/3/20` for
`top_k`/`max_depth`/`max_items`. Inspect the tool schema for required arguments
and command-specific limits.

## Codex `config.toml`

```toml
[mcp_servers.reql]
command = "reql-mcp"
args = ["--read-only"]
```

Run the server from an allowed workspace root and pass storage paths explicitly.
Client-specific configuration controls the server's working directory and permissions.

## HTTP JSON-RPC

HTTP uses the same JSON-RPC messages as stdio:

```bash
curl -s http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer change-this-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

## Claude Desktop

```json
{
  "mcpServers": {
    "reql": {
      "command": "reql-mcp",
      "args": ["--read-only"]
    }
  }
}
```

## Workflow

Compile or refresh through `reql_compile_project` when write tools are enabled
and the operation is authorized by the client:

```json
{
  "tool": "reql_compile_project",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "path": ".",
    "cache_enabled": true
  }
}
```

Then retrieve the working set:

```json
{
  "tool": "query_context",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "query": "incremental compilation deleted files",
    "scopes": ["code"]
  }
}
```

`query_context` returns the shared `ContextResult` envelope. Use `include_source:
true` with explicit code/test scopes for verified bodies, or follow returned
`inspect_node` ids for missing source. Source limits, statuses and freshness are
the [same across Python, CLI and MCP](CLI.md#retrieval-commands).

Request only missing `query_explore` views, such as `owners`, `callers`,
`public_surface`, `serialization_paths`, `docs_mentions`, `structural_duplicates`
or `code`. Use `query_graph` for graph diagnostics and `query_memories` for short
ranked texts. Drill into selected graph records with REQL:

```json
{
  "tool": "reql_query",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "statement": "FRAGMENTS WHERE relative_path CONTAINS 'compiler' LIMIT 20",
    "limit": 20
  }
}
```

After source changes and checks, refresh once. For bounded monitoring,
`reql_watch_project` accepts `max_iterations`; for continuous monitoring, use
the CLI `project compile --watch`. Leave an active watcher responsible for refresh
instead of sending repeated compile calls. See
[watch operation](CLI.md#incremental-compilation).

Shared work tools use the same record owner as the CLI. `reql_work_record`
requires `kind`, `content`, `agent_id` and `session_id` in addition to storage;
updates carry `record_id` and `expected_revision`. MCP-only clients write
explicit checkpoints at session boundaries. See
[work records](COORDINATION.md#work-records).

## Security Notes

`storage_path`, project `path` and `config_path` must resolve inside an allowed
MCP root. The default root is the server's current working directory.
`REQL_MCP_ALLOWED_ROOTS` accepts an `os.pathsep`-separated list of absolute roots
for servers intentionally exposing more than one workspace or storage directory.
The server provides no shell execution or arbitrary filesystem-operation tool.

HTTP requires a Bearer key. Use trusted networks and TLS or a trusted reverse
proxy before exposing it outside a private network. Prefer `--read-only` for
shared contexts.

MCP bounds user-controlled strings, strips control characters and neutralizes
chat-template delimiters before returning results. Use specific queries and
limits; intentional full graph export belongs to the [CLI](CLI.md#inspection-and-export).
