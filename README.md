# REQL

REQL is a local repository context and working-memory layer for coding agents
and developer tools. It compiles source files and supported documents into a
property graph, then returns bounded context about code, tests, dependencies,
findings and provenance.

Installed assistant instructions make REQL part of the agent's normal workflow.
The project graph holds repository facts; Agent Workspace holds engineering
intent, decisions and handoffs. Current source and tests remain authoritative.

REQL runs locally without mandatory model calls, accounts, hosted services or
an external graph database. Bounded retrieval and saved work can reduce repeated
discovery, but instructions and tool results also consume tokens; total savings
depend on the host and workflow.

## Quick Start

REQL requires Python 3.10 or newer:

```bash
python -m pip install reql
reql install codex
```

For development from a checkout, use `python -m pip install -e .`. Replace
`codex` with another supported agent platform, or run interactive installation
to detect one. Run project commands from the target repository root:

```bash
reql project status
reql project compile
reql query_context --query "payment service" --code
```

Compile when the graph is missing or needs refresh. If a watcher already owns
refresh, leave it responsible; `reql project compile --if-stopped` checks that
ownership before opening the graph. Registration alone does not establish a
successful compilation.

Use `reql locate "src/service.py"` for a known path. Follow the inspection
commands returned by context retrieval for missing source, and reuse complete
verified excerpts. Test targets are suggestions; use the repository's test runner.
See [retrieval and source limits](docs/CLI.md#retrieval-commands).

For assigned work, recover with `reql project context --task WORK_ID`.
Use `project overview` when project direction is unknown. Multi-step or resumed
work can use Agent Workspace to record decisions and verified outcomes; see
[Engineering coordination](docs/COORDINATION.md).

The installer also adds the explicit-only `reql-context-compact` skill for hosts
that can export and replace a JSON transcript. It removes only large, exact
duplicate tool results. It cannot replace the live context window in Codex or
another host without that capability. See [assistant installs](docs/CLI.md#assistant-installs).

From a checkout, `python cli.py ...` exposes the same CLI without an editable
installation. Dependencies must still be installed. Start the optional MCP server
with `reql-mcp --read-only`; see [MCP setup](docs/MCP.md).

## Features

- Incremental code and document compilation with hashes, source spans and provenance.
- Bounded lexical and graph retrieval through a shared Python, CLI and MCP contract.
- Static-analysis findings, structural code links, communities and hub analysis.
- Revision-checked shared work, private session scratch and recoverable checkpoints.
- Local block storage with transactions, locking, compaction and retention.
- Markdown reports, JSON and HTML graph export, and project pipeline views.

Supported languages, formats and parser requirements are listed in
[Code analysis](docs/CODE_ANALYSIS.md) and [Artifact ingestion](docs/ARTIFACT_INGESTION.md).

## Runtime Model

```text
project files -> scan and compile -> canonical graph -> bounded source context
agent outcomes -> shared work records -> task recovery and project overview
```

`project compile`, `update_project` and watch mode share the incremental
compiler. File hashes, compiler options and successfully compiled content
govern cache reuse. An incomplete scan cannot establish deletions or publish a
new revision. See [Incremental compilation](docs/INCREMENTAL_COMPILATION.md) for
stage ordering, failures and recovery.

Storage defaults to `./.reql/memory.reql`. Projects configure behavior through
`reql.conf`; see [Configuration](docs/CONFIGURATION.md) for precedence and defaults.
Storage accepts the current validated format. `storage clear` builds a fresh
graph and discards history after a successful replacement; see
[Storage maintenance](docs/STORAGE.md#inspection-compaction-and-clean-rebuilds).

## Interactive Launcher

```bash
python launcher.py
python launcher.py --storage .reql/memory.reql
```

The launcher guides storage selection, compilation, queries, analysis, reports,
exports and MCP configuration. Use the CLI for scripting and the complete command
surface. See [CLI entry points](docs/CLI.md#entry-points).

## Documentation

| Guide | Contents |
| --- | --- |
| [Architecture](docs/ARCHITECTURE.md) | Components, ownership and dependency boundaries |
| [CLI](docs/CLI.md) | Commands, arguments, installation and source-context output |
| [Configuration](docs/CONFIGURATION.md) | Defaults, `reql.conf`, overrides and scan rules |
| [Engineering coordination](docs/COORDINATION.md) | Work records, sessions, batches and handoffs |
| [Incremental compilation](docs/INCREMENTAL_COMPILATION.md) | Stages, cache, deletion and recovery |
| [Storage](docs/STORAGE.md) | Format, transactions, locking and maintenance |
| [Artifact ingestion](docs/ARTIFACT_INGESTION.md) | Document parsers, fragments and relations |
| [Code analysis](docs/CODE_ANALYSIS.md) | Languages, Tree-sitter and static findings |
| [REQL language](docs/REQL.md) | Query syntax and examples |
| [Schema](docs/SCHEMA.md) | Node, edge and provenance fields |
| [Graph analysis](docs/GRAPH_ANALYSIS.md) | Communities, hubs and bridge signals |
| [Repository explanation](docs/REPOSITORY_EXPLANATION.md) | Derived capabilities and semantic workflows |
| [Reporting](docs/REPORTING.md) | Reports and HTML/Mermaid export |
| [MCP server](docs/MCP.md) | Transports, tools and client configuration |
| [Extending](docs/EXTENDING.md) | Adapter and extractor contracts |

## Architecture

The public API delegates to application services, deterministic engines and the
`GraphStore` storage port. Repository facts, shared engineering work and private
scratch have separate owners. See the [architecture guide](docs/ARCHITECTURE.md).

## Public API

The canonical import is `from reql import MemoryGraph`. The same package exposes
`BlockGraphStore` for direct storage access and typed query-context models:

```python
from reql import MemoryGraph, QueryContextRequest, QueryMode, RetrievalBudget

graph = MemoryGraph.open(".reql/memory.reql")
try:
    graph.compile_project(".")
    request = QueryContextRequest(
        text="payment service",
        mode=QueryMode.INFORMATIVE,
        scopes=frozenset({"code"}),
        budget=RetrievalBudget(top_k=20, max_depth=3, max_items=20),
    )
    result = graph.query_context_result(request)
    print(result.to_dict())
finally:
    graph.close()
```

The [MemoryGraph facade](src/api/memory_graph.py) exposes compilation, retrieval,
inspection, REQL queries, reporting and analysis. Python compilation overrides
use `CompilationOptions` from `memory.artifacts`; see
[typed configuration](docs/CONFIGURATION.md#loader). Context requests return
schema-version-2 envelopes with source revision, freshness, confidence and a
nested payload; see [retrieval contracts](docs/CLI.md#retrieval-commands).

## License

MIT. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, benchmarks and pull
request expectations. Generated stores, caches and benchmark outputs stay local.
