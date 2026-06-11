# REQL MCP Server

The optional REQL MCP server lets Codex, Claude Desktop, and other MCP clients
query REQL as a bounded context substrate. Clients ask for compact
context, ranked nodes, REQL rows, project status, or hubs instead of
placing the whole graph in a prompt.

The core memory system does not depend on MCP. Tool behavior is implemented in
`mcp.tools` as pure Python handlers, and `mcp.server` provides dependency-free
stdio and HTTP JSON-RPC transports built with the Python standard library.

## Start the Server

From a source checkout:

```bash
PYTHONPATH=src python -m mcp.server
```

On PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m mcp.server
```

After installation, the `reql-mcp` console script is also available:

```bash
reql-mcp
```

To expose only read-only tools:

```bash
reql-mcp --read-only
```

To start MCP tools with a project config and process-level overrides:

```bash
reql-mcp --config ./conf.yaml --set project.id=agent-a --read-only
```

To share the MCP server with clients on the same machine over HTTP, bind a
local address and provide an API key:

```bash
REQL_MCP_API_KEY="change-this-key" reql-mcp --transport http --host 127.0.0.1 --port 8765 --read-only
```

To share it with other machines on a trusted LAN, bind `0.0.0.0`:

```bash
REQL_MCP_API_KEY="change-this-key" reql-mcp --transport http --host 0.0.0.0 --port 8765 --read-only
```

HTTP clients send JSON-RPC requests to `/mcp` and must include:

```text
Authorization: Bearer change-this-key
```

The health endpoint is available at `/health`.

## Tools

Read-only tools are intended to be callable without server-side approvals. They
return bounded JSON payloads and never return the full graph.

- `query_graph`: retrieves a structured agent context with seed nodes,
  expanded graph nodes, edges, textual sources, filtered generic nodes, and a
  compact rendered context block.
- `query_context`: retrieves compact structured working-set context with owner
  targets, targeted reads, snippets, impact, tests, and structured next-step
  commands for JSON clients.
- `query_explore`: retrieves dependency-oriented slices for coding agents:
  owners, callers, public surface, serialization paths, docs mentions, and code
  working-set records.
- `query_memories`: retrieves a compact ranked list of relevant memory/source
  texts for clients that do not need graph debugging details.
- `inspect_node`: resolves a node id from `query_memories`, `query_graph`,
  `retrieve`, or REQL rows to the node payload, normalized source/location
  hints, adjacent edges, and immediate neighbors.
- `reql_query`: executes REQL and returns `columns`, bounded `rows`, and
  metadata. Mutating analysis statements such as `COMMUNITIES`, `HUBS`, and
  similar write-backed graph analysis statements are rejected by this read-only
  tool.
- `reql_project_status`: returns registered project/cache status for a path.

Write/update tools modify the block-backed graph. Configure your client to ask
for approval before using them.

- `reql_compile_project`: scans and incrementally compiles a project path.
- `reql_watch_project`: performs bounded watchdog monitoring and incrementally
  compiles dirty project artifacts.
- `reql_hubs`: analyzes graph hubs and persists hub scores before returning
  scores and reasons.

Default limits are intentionally conservative: `top_k` is capped at 50,
`max_depth` at 5, structured context nodes at 200, structured context edges at
400, context items at 50, memory text snippets at 2000 characters, and REQL
output rows at 200.

Every MCP tool accepts optional `config_path` and `config_overrides` arguments.
`config_path` selects a `conf.yaml`; `config_overrides` is an object using
nested sections or dotted keys. The server process also honors `REQL_CONFIG`
and `REQL_CONFIG_OVERRIDES`. Tools that receive a project `path` search for
`conf.yaml` from that path upward when no explicit config is supplied, then
fall back to the canonical config at the REQL code root.

## Codex `config.toml`

Example after installing the package:

```toml
[mcp_servers.reql]
command = "reql-mcp"
args = []
```

Read-only only:

```toml
[mcp_servers.reql]
command = "reql-mcp"
args = ["--read-only"]
```

Tool calls should pass the storage path explicitly:

```json
{
  "storage_path": "/absolute/path/to/project/.reql/memory.reql",
  "query": "project compile behavior",
  "config_path": "/absolute/path/to/Reql/conf.yaml"
}
```

## HTTP JSON-RPC

The HTTP transport accepts the same JSON-RPC MCP messages as stdio. Example
request:

```bash
curl -s http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer change-this-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

You can pass the key directly with `--api-key`, or set the environment variable
named by `--api-key-env`:

```bash
reql-mcp --transport http --host 127.0.0.1 --port 8765 --api-key "change-this-key"
reql-mcp --transport http --host 127.0.0.1 --port 8765 --api-key-env MY_REQL_MCP_KEY
```

## Claude Desktop

Example after installation:

```json
{
  "mcpServers": {
    "reql": {
      "command": "reql-mcp",
      "args": []
    }
  }
}
```

## Workflow

1. Compile the project after approval:

```json
{
  "tool": "reql_compile_project",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "path": ".",
      "cache_enabled": true,
    "config_overrides": {"scan.max_file_size_mb": 2}
  }
}
```

Use bounded watchdog monitoring when the client wants to refresh memory after
file changes without starting an unbounded MCP tool call. For continuous
monitoring, run the CLI `reql project compile . --watch` from the workspace as
monitor mode and do not call `reql_compile_project` repeatedly while that
monitor is responsible for updates.

```json
{
  "tool": "reql_watch_project",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "path": ".",
      "max_iterations": 1,
    "config_path": "conf.yaml"
  }
}
```

2. Ask for bounded context before a task:

```json
{
  "tool": "query_context",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "query": "how does incremental compilation handle deleted files?",
      "top_k": 12,
    "max_depth": 3,
    "max_items": 12,
    "config_path": "conf.yaml"
  }
}
```

Use `query_memories` instead when the agent only needs a compact list of
relevant memory/source texts:

```json
{
  "tool": "query_memories",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "query": "how does incremental compilation handle deleted files?",
      "top_k": 12,
    "max_depth": 2,
    "limit": 8,
    "config_path": "conf.yaml"
  }
}
```

Use `query_graph` instead when the agent needs seed nodes, edges, source
records, filtered-node diagnostics, and a rendered graph context:

When retrieval returns relevant code nodes, `query_context` is informative by
default. Pass `"scopes": ["code"]`, or booleans such as `"code": true`,
`"docs": true`, or `"test": true`, to limit results to code,
documentation/imported documents, or tests. Pass `"mode": "cleanup"` to return
only cleanup candidates matching the query for dead-code or unused-symbol work.

Use `query_explore` when an agent needs dependency slices before reading source
files or editing. Pass `views` to reduce output to one or more of `owners`,
`callers`, `public_surface`, `serialization_paths`, `docs_mentions`, and `code`;
the `code` view includes usage guidance, snippets, and targeted reads:

```json
{
  "tool": "query_explore",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "query": "remove stale storage field",
    "views": ["owners", "code"],
    "config_path": "conf.yaml"
  }
}
```

```json
{
  "tool": "query_graph",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "query": "how does incremental compilation handle deleted files?",
      "top_k": 12,
    "max_depth": 2,
    "config_path": "conf.yaml"
  }
}
```

3. Drill into graph records with REQL:

```json
{
  "tool": "reql_query",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "statement": "FRAGMENTS WHERE relative_path CONTAINS 'compiler' LIMIT 20",
      "limit": 20,
    "config_overrides": {"project": {"id": "default"}}
  }
}
```

4. Modify code in the repository using normal editor or agent tooling.

5. Refresh the project graph after approved source changes:

```json
{
  "tool": "reql_compile_project",
  "arguments": {
    "storage_path": ".reql/memory.reql",
    "path": "."
  }
}
```

## Security Notes

The MCP server does not run shell commands and does not expose destructive file
operations. It only opens local filesystem paths. `storage_path`, project
`path`, and `config_path` are resolved before use and must stay under an allowed
MCP root. By default the allowed root is the process current working directory;
set `REQL_MCP_ALLOWED_ROOTS` to an `os.pathsep`-separated list of absolute roots
when a server intentionally needs to expose another workspace or storage
directory. Project tools scan or compile the supplied project path through the
same dependency-light project compiler used by the CLI.

HTTP transport requires a Bearer API key. Binding `0.0.0.0` exposes the server
on all network interfaces; use it only on trusted networks, prefer
`--read-only` for shared contexts, and put the server behind TLS or a trusted
reverse proxy before exposing it outside a private network.

Read tools return compact, JSON-serializable payloads. User-controlled strings
are bounded, control characters are stripped, and chat-template delimiters are
neutralized before MCP returns them to agent clients. Use `reql_query` with a
specific REQL statement and `limit` when drilling into records. For graph export,
use the CLI intentionally rather than an MCP tool.




