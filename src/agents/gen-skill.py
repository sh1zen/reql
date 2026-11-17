"""Generate REQL skills and rules with ASD-STE100-style prose and unchanged code literals."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSource:
    name: str
    title: str
    description: str
    summary: str
    workflow_steps: tuple[str, ...]
    rule_points: tuple[str, ...]
    deterministic_requirement: str


@dataclass(frozen=True)
class SkillResource:
    path: str
    content: str


PROJECT_SKILL_SOURCE = SkillSource(
    name="reql-agent",
    title="REQL Coding Workflow",
    description=(
        "Use REQL for repository context, shared work, and graph maintenance in {platform_name}. "
        "Use it for code changes, repository questions, and work across sessions."
    ),
    summary=(
        "The graph stores facts. Shared records store intent."
    ),
    workflow_steps=(
        "Reuse a supplied working set and check current source. For missing context, run `{command_name} project status` once. For a missing graph, run `project compile --if-stopped`. Read `references/bootstrap.md` for setup failures.",
        "Retrieve only missing context with `{command_name} locate \"<path>\"` or `query_context --query \"<literal terms>\" --code`. Inspect missing source with `inspect --node-id ID --limit 1` and reuse complete verified excerpts. Use text output unless you need structured fields. For requirements, use `--docs`. Refine once or search exactly. Stop when the owner, effects, and tests are clear.",
        "Read shared work before edits in a new or resumed session. Reuse complete engineering context from the handoff or `query_context`. Otherwise, use `{command_name} project context --file PATH`. Repeat `--file` for other affected files. Read marked omissions with `agent show ID --json`. Check recorded decisions and failures against current evidence before action.",
        "For managed parallel work, use one stdin batch for linked records. Workers reuse its context and return results. Independent agents use `agent init --name NAME --task ID --revision N`. It returns task context and the current revision. Recover with `project context --task ID` or `agent show ID --json` for marked omissions. Use `{command_name} project overview` for unknown goals, direction, workstreams, and blockers.",
        "Save each outcome once and use write receipts for ids and revisions. Read again for missing evidence, concurrent changes, or stale revisions. Update with `--revision`. Check dependencies and conflicts. Replace with `--supersedes ID --why REASON`. Keep the host's identity and activity.",
        "Save decisions, failures, blockers, and results when they occur. Record failures before changing direction or ending the session. Use `{command_name} agent record failure \"<approach>\" --why \"<evidence>\" --file PATH`. Include affected source and tests with repeated `--file`. Use `agent reject \"<approach>\" \"<reason>\"` when file scope is unknown. Read `references/agent-workspace.md` for session setup and record fields.",
        "Workers run focused checks. The coordinator runs integration checks and refreshes once after parallel work. Use one full-suite run when it covers worker checks. Repeat focused checks only for failures or uncovered behavior. Refresh with `{command_name} project compile --if-stopped`. Skip refresh for read-only work.",
        "Close only sessions started or continued here. Use `agent finish SUMMARY --task ID --revision N` for assigned work. It completes the task, clears its next action, and returns the checkpoint id. Otherwise, use `agent finish SUMMARY`. Pass that id instead of writing another handoff."
    ),
    rule_points=(
        (
            "Use `{command_name}`. If it is unavailable, try `{command_path}`, then `{fallback_command}`. "
            "Reuse a supplied working set. For missing context, run `project status` from the project root. "
            "Status reports compilation results, not current file changes."
        ),
        (
            "For missing context, use `{command_name} locate \"<path>\"` or `{command_name} query_context --query \"<short literal terms>\"`. "
            "Inspect missing source with `inspect --node-id ID --limit 1` and reuse complete verified bodies. "
            "Check the owners, line ranges, callers, and tests in the current source."
        ),
        (
            "For insufficient context, try one narrower query or a specific source search. For unclear requirements, use `--docs`. "
            "Follow the current source and repository instructions."
        ),
        (
            "Keep source reads within the task. Stop when the owner, effects, and tests are clear. Reuse sufficient results. "
            "Read a reference only when necessary. Use JSON only for structured fields."
        ),
        (
            "Read shared work before edits in a new or resumed session. "
            "Reuse complete engineering context from the handoff or `query_context`. Otherwise, use "
            "`{command_name} project context --file PATH`. Repeat `--file` for other affected files. "
            "Read marked omissions with `agent show ID --json`. "
            "Check recorded decisions and failures against current evidence before action."
        ),
        (
            "For managed parallel work, one coordinator owns shared records and workers return evidence. "
            "Independent agents use `agent init --task ID --revision N` to claim work and read its context. "
            "Use `project context --task ID` for read-only recovery. "
            "For unknown project direction, read `project overview` from the project directory. Keep the host's identity and activity."
        ),
        (
            "Pipe linked work records to `agent batch --name NAME --file - --finish SUMMARY`. "
            "Independent agents use `agent finish SUMMARY --task ID --revision N` to complete work and close its session. "
            "Update existing records with `--revision` and check dependencies and conflicts before changes. "
            "Reuse write receipts for ids and revisions. Read again for missing evidence, concurrent changes, or stale revisions."
        ),
        (
            "Save decisions, failures, blockers, and results when they occur. Record failures before changing direction or ending the session. "
            "Use `{command_name} agent record failure \"<approach>\" --why \"<evidence>\" --file PATH`. "
            "Repeat `--file` for affected source and tests. Link an existing task with `--parent TASK_ID`. "
            "Use `agent reject \"<approach>\" \"<reason>\"` when no file scope is known. "
            "Start a session with `agent init --name NAME` before writing if none is active. "
            "Update existing records by id and revision. Keep failures active until evidence resolves or supersedes them."
        ),
        (
            "Run checks for changed behavior and reuse passing checks for unchanged code. "
            "The coordinator runs one full-suite check after workers finish when it covers their checks. "
            "Repeat focused checks for failures or uncovered behavior. "
            "After checks, refresh once with `{command_name} project compile --if-stopped`. "
            "Omit this update for read-only work. "
            "Start watch mode only if the user asks for continuous monitoring."
        ),
        (
            "Use `{command_name} --help` to find other features. Use `<command> --help` for flags. "
            "REQL also provides project explanation, pipeline views, history, reports, exports, configuration, storage tools, and MCP. "
            "Use `reql-context-compact` only on explicit request and only when the host can export and replace a transcript."
        ),
    ),
    deterministic_requirement="Keep REQL local and deterministic. The graph stores source facts. Shared work records store plans and task status. Private notes are temporary.",
)

def skill_markdowns(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[tuple[str, str], ...]:
    return (
        (
            PROJECT_SKILL_SOURCE.name,
            skill_markdown(
                PROJECT_SKILL_SOURCE,
                platform_name,
                project=project,
                command_name=command_name,
                command_path=command_path,
                fallback_command=fallback_command,
            ),
        ),
        (
            "reql-context-compact",
            context_compact_skill(platform_name),
        ),
    )


def skill_resources(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[tuple[str, str, str], ...]:
    project_resources = _project_skill_resources(
        platform_name=platform_name,
        scope=_scope(project),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    agent_workspace = _agent_workspace_resource(command_name=command_name)
    return (
        *(tuple((PROJECT_SKILL_SOURCE.name, item.path, item.content) for item in project_resources)),
        (PROJECT_SKILL_SOURCE.name, agent_workspace.path, agent_workspace.content),
        (
            "reql-context-compact",
            "agents/openai.yaml",
            "policy:\n  allow_implicit_invocation: false\n",
        ),
    )


def context_compact_skill(platform_name: str) -> str:
    """Generate the explicit-only, cross-agent transcript compaction skill."""
    manual_policy = "disable-model-invocation: true\n" if platform_name != "codex" else ""
    return f"""---
name: reql-context-compact
description: Use only when the user asks for /reql-context-compact. Remove large repeated tool results from an exported JSON transcript.
{manual_policy}---

# REQL context compact

Run only when the user asks for `/reql-context-compact`. Do not start this skill during ordinary REQL use. Do not start it automatically before host compaction.

First, check that the host can export a JSON transcript and accept a new message list. Use Python from the active REQL environment. Send the transcript to `python -m agents.context_compact` through stdin. Check stdout before the host uses the new list. The command uses no network or model. It writes no files. Add `--stats` to report character savings on stderr.

The input is a JSON message array or an object with a `messages` array. Each tool result must have `role: "tool"`, a nonempty `name`, and string `content`.

The filter keeps every message and all metadata. It keeps instructions, user messages, assistant messages, marked error results, and the latest four messages. It keeps the first complete copy of each tool result.

The filter changes only old tool content with at least 512 characters. An earlier result from the same tool must have identical bytes. The new content points to the retained message and includes its SHA-256 digest.

If the host cannot export or replace the transcript, report this limit. Do not claim that the live context changed. Do not use a model-based `/compact` command instead.

For an exported file, use `python -m agents.context_compact < transcript.json > compacted.json` to make a separate copy. The host must choose to use this copy for later messages. Do not overwrite the source transcript. Do not change private host session files.
"""


def skill_markdown(
    source: SkillSource,
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> str:
    workflow = _numbered(source.workflow_steps, command_name=command_name)
    reference_routing = _reference_routing()
    return f"""---
name: {source.name}
description: {source.description.format(platform_name=platform_name)}
---

# REQL Coding Workflow

{source.summary}

{workflow}

## References

{reference_routing}

Read only the necessary reference. Reuse it during the session. Use `{command_name} --help` to find commands. Use `<command> --help` for flags.
"""


def _reference_routing() -> str:
    return "\n".join(
        [
            "- For setup, configuration, or assistant installation, read `references/bootstrap.md`.",
            "- For files, source, tests, or memory search, read `references/query.md`.",
            "- For REQL statements, dependency views, or cleanup findings, read `references/query-advanced.md`.",
            "- For refresh, watch mode, history, cache, or storage, read `references/update-watch.md`.",
            "- For architecture, pipeline views, reports, exports, or MCP, read `references/reports-exports.md`.",
            "- For documents, read `references/document-semantics.md`.",
            "- For individual record formats or session recovery, read `references/agent-workspace.md`. Use `agent dashboard` only to recover a session.",
            "- For batch fields or recovery, read `references/agent-batch.md`.",
        ]
    )


def _project_skill_resources(
    *,
    platform_name: str,
    scope: str,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[SkillResource, ...]:
    usage = _command_usage(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    openai_yaml = """interface:
  display_name: "REQL Coding Workflow"
  short_description: "Find source context and save shared work."
  default_prompt: "Use $reql-agent to find source context. Check the source and tests. Save important work results. Update the graph after changes."
"""
    bootstrap = f"""# REQL reference: graph setup and project status

Read this reference for graph setup, command failures, or compilation problems.

## Command paths

{usage}

## Existing graph

Check the project status:

```bash
{command_name} project status
```

Run each command from the project root. The default store is `.reql/memory.reql` in that directory. The launcher path does not select the project. Do not create a second graph in a source subdirectory.

Successful status means that the project has a registration record. Check `Last compilation`, or `last_compilation` with `--json`:

- `completed` means that compilation succeeded.
- `failed` includes compilation errors.
- `none` means that no compilation result exists.

Status does not scan current files. Do not compile only because the user asks a repository question. Use the graph to find related paths and line ranges. Then check the current source.

## First compilation

If status reports `Project not found` or no compilation result, run this command from the project root:

```bash
{command_name} project compile
```

Use the existing task authorization for initial compilation. Check for exit code zero and `Status: completed`. A failed compilation can leave incomplete context. A successful status command does not prove that compilation succeeded.

If a parser dependency is missing, repair the declared REQL dependencies in the launcher Python environment. Then try compilation again. Do not repeat the same failed compilation without a change. If compilation remains unavailable or fails again, report the problem. Continue with specific source checks. Do not report incomplete results as complete evidence.

## Source selection

Use REQL results to select files, symbols, owners, line ranges, relations, and tests. If information is missing, try one narrower query. For a missing relation, use `query_explore`, `query_graph`, `query_memories`, or `inspect`.

Use text output first. Use `--json` only when structured fields answer a specific question. Read the selected source and the related callers, contracts, and tests. Do not repeatedly compile a usable graph. Do not replace a specific query with a copy of the complete repository.

For relations outside the graph, search for exact names or paths. Dynamic registrations can need this check. Stop when the owner, effects, change locations, and tests are clear.

## Exclusions

Set additional compilation exclusions under `scan.exclude` in the project `reql.conf` file. Use `./` for rules relative to the configuration directory. Omit `./` to match at any depth. Only the final path segment permits the wildcard form `*suffix`. Do not use patterns for the complete workspace, such as `*`, `**`, or `**/*`. Do not exclude source or framework roots that the task needs.

## Configuration

Project commands search for `reql.conf` from the target path through its parent directories. Internal defaults apply first. Project settings, environment overrides, and explicit command overrides follow in that order. Lists merge by default. `scan.ignore_defaults: true` replaces the default scan lists. Check the effective configuration before changes:

```bash
{command_name} config show
{command_name} config init
{command_name} config set KEY VALUE
```

`config init` creates a template if absent. `config set` validates and saves one local option. `REQL_CONFIG` selects a configuration file. `REQL_CONFIG_OVERRIDES` supplies JSON overrides. Keep compilation independent of model providers.

## Assistant installation

Use `install <platform>` to update generated skills, rules, and supported hooks. Use `--user` for a user profile. The default scope is the current project. Use `--dry-run --json` to inspect planned files. `--no-hooks` omits hooks. `uninstall <platform>` removes REQL-owned files and sections. Preserve other instructions. Do not change personal profiles unless requested.

Installation: {platform_name} ({scope}).
"""
    query = f"""# REQL reference: specific queries

REQL matches terms and graph links. It does not interpret requirements. Use short literal identifiers, paths, or error text.

For empty or insufficient results, refine once or search specific source files. For requirements, use `--docs`.

Unknown freshness on an empty result does not require compilation. Current freshness describes saved graph or watcher state, unless source checks find changed files.

Select the command for the missing information:

- For a known path, use `{command_name} locate "path/to/file"`.
- For owners, callers, implementation links, and tests, use `query_context --query "<terms>" --code`.
- For missing current source, use the returned `inspect --node-id ID --limit 1` command. Reuse complete verified bodies.
- For callers and owners, use `query_explore --query "<terms>" --view owners --view callers --view code`.
- For graph edges, use `query_graph --query "<terms>" --max-depth 2`.
- For ranked text, use `query_memories --query "<terms>" --limit 8`.
- For custom columns, more views, or unused code, read `query-advanced.md`.

Use `--json` for structured fields: `owner_candidates`, `read_plan`, `contracts`, `impact`, and `test_targets`.

Use dotted field names for cross-layer data traces. The `data_trace` rows show observed field uses and transformations. Check current source to confirm static links. Source confidence and graph revision do not validate attached engineering claims.

Add `--source` to `--code` or `--test` when you need source bodies. This reads current files and checks their hashes against the committed revision. It returns at most three files, six regions, 120 lines, and 6,000 source characters. Overlapping regions are merged. Files larger than 4 MiB are excluded.

Reuse complete verified excerpts. Read missing or truncated ranges directly. For stale or unavailable rows, check the current file directly. The graph does not refresh during this query.

Keep the selected files with the task. Stop queries when the owner, effects, and tests are clear.

`query_context` includes bounded shared work. Use `project context --file PATH` for missing engineering history. Use `project context --task ID` for assigned work. Read marked omissions with `agent show ID --json`.

"""
    query_advanced = f"""# REQL reference: raw queries, graph relations, and unused code

Read this reference for custom rows, missing relations, or unused-code checks. For ordinary queries, read `query.md`.

For project records, use `query "PROJECTS LIMIT 20"` or `query "ARTIFACTS LIMIT 20"`.
For analysis or cache records, use `query "HUBS LIMIT 20"` or `query "CACHE STATUS"`.
`WHERE` permits LIKE, ILIKE, REGEX/MATCHES, BETWEEN, IN, IS NULL, and IS NOT NULL.

## Raw REQL Statements

Use `{command_name} query "..."` for exact ids, custom columns, source locations, graph relations, findings, or cache state. These queries return rows instead of a context summary.

For text search with specific columns, use `RETRIEVE ... RETURN ...`:

```bash
{command_name} query "RETRIEVE '<literal terms>' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"
{command_name} query "RETRIEVE '<literal terms>' LIMIT 8 RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start"
```

For a known filter, id, file, symbol, or relation, use `FIND`, `SYMBOLS`, `FRAGMENTS`, or `MATCH`:

```bash
{command_name} query "FIND nodes WHERE id IN [document_term:abc, document_term:def] RETURN id,type,label,text"
{command_name} query "SYMBOLS WHERE relative_path = 'path/to/source.py' RETURN type,name,qualified_name,start_line,end_line LIMIT 50"
{command_name} query "FRAGMENTS WHERE relative_path = 'README.md' RETURN id,text,line_start,line_end LIMIT 20"
{command_name} query "MATCH (s)-[:REFERENCES]->(t) WHERE s.relative_path = 'README.md' RETURN s.id,s.text,t.type,t.name LIMIT 20"
```

Set a `LIMIT` in each raw query. Request only the columns that the task needs. For source evidence, include location or relation fields. Examples are `relative_path`, `line_start`, `line_end`, `source_for`, `relation`, and `direction`.

Other statement forms provide focused graph operations:

- `SEARCH` returns ranked nodes. `RETRIEVE` also returns connected source text.
- `ACTIVATE` follows weighted links from known node ids.
- `PATH FROM ... TO ... DEPTH N LIMIT N` finds bounded paths.
- `EXPLAIN NODE` shows neighbors. `EXPLAIN SEARCH` shows ranking evidence.
- `STATS` and `STATS TYPE` return graph counts.

Use `INCLUDE ARCHIVED` only when historical source records are needed. Filters, types, columns, and limits depend on the statement.

First, use `query_context`, `query_explore`, `query_memories`, or `query_graph` to get ids or file paths. Do not use raw queries to scan the complete repository.

## Dependencies

For dependencies around a known target, use `query_explore`:

```bash
{command_name} query_explore --query "<terms from user request>" --view owners --view code
{command_name} query_explore --query "<terms from user request>" --owners-only
{command_name} query_explore --query "<terms from user request>" --callers-only
{command_name} query_explore --query "<terms from user request>" --serialization-paths-only
{command_name} query_explore --query "<template terms>" --structural-duplicates-only
{command_name} query_explore --query "<terms from user request>" --view owners --view callers --view public_surface
```

Select the view for the required evidence:

- Use `owners` for the implementation location.
- Use `callers` for affected consumers.
- Before symbol removal or name changes, use `public_surface` for exported symbols.
- Before model or storage field changes, use `serialization_paths`.
- Use `docs_mentions` for documentation and examples.
- Use `structural_duplicates` for repeated template markup.
- Use `code` for file selection and source line ranges.

## Unused code

For unused-code tasks, start with REQL findings. Do not create a second repository scanner. Check each possible removal in the current source. Public APIs, callbacks, entry points, reflection targets, and plugin hooks can have uses outside the graph.

Use these steps:

1. Check that compilation completed.
2. Run `{command_name} query_context --query "unused code dead code cleanup" --cleanup`.
3. List the findings:

```bash
{command_name} query "FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class','possibly_orphan_directory'] RETURN finding_type,severity,cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,directory,file_count,files,line_start,reason,evidence_scope,confidence ORDER BY cleanup_priority LIMIT 100"
```

4. For unclear rows, get source evidence:

```bash
{command_name} query "VERIFY FINDING FINDING_ID"
{command_name} query "MATCH (s)-[:HAS_FINDING]->(f:StaticAnalysisFinding) RETURN s.type,s.name,s.qualified_name,f.finding_type,f.relative_path,f.line_start,f.reason LIMIT 100"
```

`VERIFY FINDING` returns compiled snippets, known uses, checked scopes, and risks. It does not read current files.

5. Check the selected files and their callers and importers. If necessary, search for entry points, tests, public exports, callbacks, reflection, and documentation examples.
6. Separate safe removals, possible public API risks, directory checks, and incorrect findings.

The `possibly_unused_function`, `possibly_unused_method`, and `possibly_unused_class` findings do not prove that code has no external users. First, check high-priority `unused_variable` and `unused_import` findings.

The `possibly_orphan_directory` finding groups isolated code files under one directory. Its `file_count` and `files` fields identify those files. Before directory removal, check entry points, plugins, scripts, dynamic imports, and external users. Get stronger evidence before removal of public symbols, generated adapters, CLI/MCP tools, or framework lifecycle methods.

"""
    update_watch = f"""# REQL reference: graph updates and watcher problems

Read this reference for graph update problems, `stale` or `unknown` watcher status, cache checks, or deltas.

## Graph updates

After source checks, request one safe refresh:

```bash
{command_name} project compile --if-stopped
```

This command compiles when the watcher is stopped. It leaves refresh to a running watcher. Uncertain status stops the command before graph access. For troubleshooting, inspect the watcher:

```bash
{command_name} project watch-status --json
```

The cache limits compilation to changed or deleted artifacts. If the watcher is `running`, let it process the file event. Do not start another compilation. For `stale` or `unknown` status, check the reported lock metadata. Use `storage locks` for lock details. Do not use `ps`, `Get-CimInstance`, or other process listings.

Watch mode and `watch-status` use the same project `.reql/memory.reql` store.

## Watch mode

Start watch mode only if the user asks for continuous monitoring:

```bash
{command_name} project compile --watch
```

The watcher first checks the cache. Then it compiles changed or deleted artifacts. For scripts and tests, set a limit or interval:

```bash
{command_name} project compile --watch --watch-iterations 1
{command_name} project compile --watch --watch-interval 2 --watch-debounce 0.5
```

The `{command_name} project watch-status` command reports `running`, `stopped`, `stale`, or `unknown`. When available, it includes the PID and process status. It reads the lock sidecar directly. Thus, it can operate while the watcher holds the graph write lock.

Use `cache clear` only when cache removal is authorized. It is not a normal refresh step.

## Cache and deltas

Check the cache and recent compilation changes:

```bash
{command_name} cache status
{command_name} project history --limit 10
{command_name} project diff
{command_name} query "DELTAS LIMIT 10"
{command_name} query "DELTAS WHERE id = 'delta:...' LIMIT 1" --json
```

Use `{command_name} project compile` for initial compilation and later graph updates.

`project history` lists retained revisions. `project diff --revision ID` shows the saved delta for one revision. It does not show uncompiled source edits.

## Storage

Use `storage inspect` for block and index details. Use `storage locks` for current owners. `storage locks --recover-stale` removes only locks proven stale. Use it only for authorized lock recovery.

`storage compact` rewrites storage without discarding graph history. It is maintenance, not a required refresh step. `storage clear` rebuilds the current project and discards historical graph state. Use it only for an authorized rebuild. Use `--help` for each operation before maintenance.
"""
    reports_exports = f"""# REQL reference: reports, exports, and MCP

Read this reference for project reports, graph analysis, exports, or MCP setup.

## Project views

```bash
{command_name} project explain
{command_name} project pipeline --html --out reports/pipeline.html
{command_name} project pipeline --code --out reports/pipeline.mmd
{command_name} stats
```

`project explain` returns source-backed architecture and capabilities. `project overview` also includes shared engineering work. `project pipeline` writes observed execution flows as HTML or Mermaid. It requires a compiled project and does not infer runtime-only calls. `stats` reports graph counts.

## Reports

Write the project reports:

```bash
{command_name} project report --output reports/
```

The output includes `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and `CACHE_REPORT.md`. Use these reports for project summaries, cache status, deltas, symbols, communities, and hubs.

## Analysis commands

```bash
{command_name} query "COMMUNITIES LIMIT 20"
{command_name} query "HUBS LIMIT 20"
{command_name} query "HUBS TYPE Function,Class LIMIT 10"
{command_name} query "EXPLAIN HUB 'NODE_ID'" --json
```

The `COMMUNITIES` and `HUBS` commands save analysis records. Apply the same approval rules as for other write commands.

## Exports

```bash
{command_name} export --json --out reql-graph-out
{command_name} export --html --out graph.html
{command_name} export --html --json --out reql-graph-out
```

HTML export creates `graph.html` with embedded data, search, filters, and node details. JSON export writes `graph.json`.

## MCP

Start the optional MCP server only when the client needs live tool access:

```bash
reql-mcp --read-only
reql-mcp --config reql.conf --set project.id=agent-a --read-only
```

If the package is installed but `reql-mcp` is unavailable, use `python -m mcp.server --read-only`. If necessary, use the same `--config` or `--set` options.

Use read-only mode for context queries. Apply CLI approval rules to write tools, such as compilation, watch mode, and hubs.

Use the tool schema for arguments and limits. CLI and MCP share retrieval and work semantics:

- Use `query_context`, `query_explore`, `query_graph`, and `query_memories` for source context.
- Use `inspect_node` for source and relations. Use `reql_query` for bounded REQL rows.
- Use `reql_project_status` for compilation status.
- Use `reql_work_context` for scoped work. Use `reql_work_overview` for project direction.
- Use `reql_work_record` to save decisions, failures, and results with explicit agent and session ids.
- Use `reql_compile_project`, `reql_watch_project`, and `reql_hubs` only for authorized writes.

Read-only mode hides and rejects write tools. MCP-only clients save explicit checkpoints at session boundaries. No private CLI session is required for MCP work records.
"""
    document_semantics = f"""# REQL reference: document structure

Read this reference for document compilation or changes to document parsing and links.

## Default behavior

Project compilation is deterministic. REQL parses supported code. Enable required document formats with `compile.ingest_documents` and `compile.documents` in `reql.conf`. Use `config show` to check active formats. Document ingestion creates source fragments. PDF text extraction requires the optional PDF dependency.

Compile the project:

```bash
{command_name} project compile
```

Each document fragment links to its source artifact. A local document processor creates ranked terms, raw observation events, and term co-occurrence edges. It creates code links when the text names code symbols.

## Deterministic document processor

The document layer does not require a specific language or document structure. It splits Unicode text into tokens locally. It ranks useful terms and short phrases. It records `RawEvent` observations below each term. It creates `CO_OCCURS_WITH` relations for terms in the same fragment.

The processor writes:

- It writes `Concept` nodes with `extractor: document_processor`, `rank`, `term_frequency`, `fragment_count`, and `raw_event_count`.
- It writes `RawEvent` nodes with source fragments, terms, occurrence counts, ranks, line ranges, and evidence text.
- It writes `MENTIONS`, `EVIDENCED_BY`, `DERIVED_FROM`, and `CO_OCCURS_WITH` edges with source evidence.
- It writes `REFERENCES` edges from document terms to code symbols that the same compiled fragment names.

Do not add manual document import steps. Keep compilation deterministic and local.
"""
    agent_batch = f"""# REQL reference: related work records

## Choose the work owner

For managed parallel work, one coordinator can own the shared records. Workers use its graph-backed briefs and return actual files, checks, and blockers. They need no private sessions for that work. The coordinator reviews outcomes and writes them with observed revisions. This keeps revision, dependency, conflict, and provenance checks at the existing owner.

State who owns records in each worker brief. Include the contract, source paths, relevant tests, task ids, and observed revisions. Workers retrieve only missing evidence with `project context --task ID`. Keep the host's identities unchanged. Use individual claim and finish commands when agents must continue work independently.

A sufficient brief replaces another overview or plan-file read. Check current source at the supplied paths. Use overview only when direction is unknown. Read the current task when evidence is missing or revisions have changed.

## Save a plan

Pipe one batch for a linked plan or several related updates. Start the named session and finish after successful writes in the same command. Use the host's stdin support instead of a separate file-write command.

The input is a UTF-8 JSON array of 1-40 record objects. Use a project-local file or stdin with `--file -`. Keep input files under `.reql/` to exclude them from the source graph. The file is input, not the current task status. Fields use Python API names, such as `rationale`, `record_id`, `expected_revision`, and `next_action`.

PowerShell example:

```powershell
@'
[
  {{"ref":"goal","kind":"goal","content":"Preserve the delivery contract","key":"delivery"}},
  {{"ref":"contract","kind":"decision","content":"Keep the wire schema","rationale":"Existing consumers","parent":{{"ref":"goal"}}}},
  {{"ref":"backend","kind":"task","content":"Update the producer","status":"in_progress","parent":{{"ref":"goal"}},"depends_on":[{{"ref":"contract"}}],"files":["src/producer.py","tests/test_producer.py"]}},
  {{"ref":"surface","kind":"task","content":"Update the consumer","status":"in_progress","parent":{{"ref":"goal"}},"depends_on":[{{"ref":"contract"}}],"files":["src/consumer.py","tests/test_consumer.py"]}},
  {{"ref":"integration","kind":"task","content":"Verify integration","parent":{{"ref":"goal"}},"depends_on":[{{"ref":"backend"}},{{"ref":"surface"}}]}}
]
'@ | {command_name} agent batch --name "Plan delivery" --file - --finish "Contract agreed and workers assigned"
```

In Bash, supply the same array with a quoted here-document. Use `--file .reql/plan.json` for saved plans and recovery.

The optional `ref` names an entry inside this batch. A reference object selects an earlier entry's id. Forward or duplicate references are errors. Existing string ids also work. Update existing records with `record_id` and `expected_revision`. Use `next_action: ""` to clear an action without shell quoting.

For updates, omit unchanged `kind` and `content`. The store retains them inside the revision-checked transaction. Do not retrieve full records only to copy their text into updates. Supply a `rationale` for decisions, failures, blockers, replacements, and reopened work.

Text receipts return each ref, id, kind, status, and revision. Receipts describe each applied operation. Later writes can change an earlier record. Pass task ids and revisions to workers. Include graph-backed paths and relevant tests in their handoffs. Use `--json` only for compact machine-readable receipts.

Use these receipts for the handoff. Do not query all records to confirm a successful write. Read again for missing evidence, concurrent changes, or stale revisions. Check failed or partial batches before retrying.

## Accept managed results

For managed dispatch, set worker tasks to `in_progress` in the plan. After source review and passing integration checks, accept outcomes in dependency order. Complete worker tasks before integration, then complete the goal. Keep the worker evidence in each result's `rationale`. The coordinator records acceptance under its own identity.

```powershell
@'
[
  {{"record_id":"BACKEND_ID","expected_revision":1,"status":"done","rationale":"Backend worker: producer checks passed","next_action":""}},
  {{"record_id":"SURFACE_ID","expected_revision":1,"status":"done","rationale":"Surface worker: consumer checks passed","next_action":""}},
  {{"record_id":"INTEGRATION_ID","expected_revision":1,"status":"done","rationale":"Coordinator: integration checks passed","next_action":""}},
  {{"record_id":"GOAL_ID","expected_revision":1,"status":"done","rationale":"Reviewed outcomes satisfy the goal","next_action":""}}
]
'@ | {command_name} agent batch --name "Accept delivery" --file - --finish "Verified outcomes and remaining work"
```

Use the actual ids, revisions, and observed checks. Finish returns a checkpoint receipt with its id and revision. Pass that id and needed task receipts to the next agent. Use `project context --task CHECKPOINT_ID` for recovery. It reads current linked records, not a copy of the input plan.

Do not create another receipt file or handoff document to repeat these records. Save separate evidence only when it adds information absent from the records. If the user requests a handoff file, create it. Keep full test logs in files and return the summary or failing sections.

Workers run focused tests. After integration, run the documented full suite once when it includes those tests. Repeat focused suites only for failures, changed dependencies, or behavior outside the full suite. Reuse a passing baseline only when its tested source and dependencies are unchanged. Do not omit checks required by repository instructions.

Record blocked or failed work accurately instead of accepting it. A stale revision stops the batch with partial receipts. Reconcile current work before another attempt. Do not copy results into separate observations or public notes. One coordinator refreshes the source graph after integration.

## Recover partial writes

Each record uses atomic revision and dependency checks. The whole batch is not atomic. On failure, earlier successful records remain saved. The command stops and reports their receipts and the failed entry. Do not assume that finish ran after an error. Recover current state before correction.

Use one result per outcome. Do not copy the same checks into observations, checkpoints, and public notes. The finish checkpoint already retains outcomes and remaining work. For assigned-task completion, use `agent finish SUMMARY --task ID --revision N` instead of several record commands.
"""
    return (
        SkillResource("agents/openai.yaml", openai_yaml),
        SkillResource("references/bootstrap.md", bootstrap),
        SkillResource("references/query.md", query),
        SkillResource("references/query-advanced.md", query_advanced),
        SkillResource("references/update-watch.md", update_watch),
        SkillResource("references/reports-exports.md", reports_exports),
        SkillResource("references/document-semantics.md", document_semantics),
        SkillResource("references/agent-batch.md", agent_batch),
    )


def _agent_workspace_resource(
    *,
    command_name: str,
) -> SkillResource:
    agent_workspace = f"""# REQL reference: Agent Workspace

For an assigned task, use its id and observed revision:

```bash
{command_name} agent init --name "<work>" --task TASK_ID --revision N
```

This starts the session, claims the task, and returns its current revision and scoped context. Read that context before changes. Reuse the coordinator's working set. Check current source at those paths. Retrieve only missing facts. Keep the host's agent identity and activity.

`project context --task ID` keeps complete outcome text within the shared budget. It follows replacement chains and includes current causal work. Inspect marked omissions with `agent show ID --json`. Recover the relevant contract and results once. Do not query every historical id when the supplied context is sufficient.

After the owned checks pass, complete the task and close the session:

```bash
{command_name} agent finish "<result, checks, remaining work>" --task TASK_ID --revision CURRENT_N
```

This saves the result, clears its next action, and returns the finish checkpoint id and revision. Pass that id for recovery instead of another handoff copy. Stale revisions or unresolved dependencies reject completion. Keep the session open for correction after an error. Do not repeat an update with a new revision without checking its meaning.

For a plan or several related writes, read `agent-batch.md`. Save each outcome once. A task result and its checkpoint already retain checks. Do not add equivalent observations, checkpoints, or public notes.

Use write receipts for ids and revisions. Plain `agent record` does not repeat the submitted text. Use `--json` when you need the complete saved record. Read again for missing evidence, concurrent changes, or stale revisions.

For status or evidence updates, use `agent record --id ID --revision N` with the changed flags. The store retains omitted text inside the transaction. Creation and content replacement still use the kind and content arguments. Do not copy unchanged task text into updates.

For read-only recovery, use `project context --task ID` or `--query TERMS --file PATH --workstream NAME`. The `project overview` command needs no path or agent id. Use `--history` for obsolete work. The `agent show ID --json` command includes the last eight revisions. Plain text omits full history.

Read shared work before edits in a new or resumed session. Reuse complete engineering context from a handoff or `query_context`. Otherwise, recover by file:

```bash
{command_name} project context --file SOURCE_PATH --file TEST_PATH
```

Read marked omissions with `agent show ID --json`. Check the current source and tests against recorded decisions and failures. Retry a failed approach only when new evidence addresses its reason.

Use `agent dashboard` only for session recovery. Use explicit `agent --agent ID --activity SESSION` when the host cannot select identity safely. For work without an assignment, use `agent init --name NAME` and plain `agent finish SUMMARY`. Plain finish does not complete unfinished tasks.

Kinds are goal, task, decision, constraint, failure, question, change, observation, and checkpoint. States are active/open/in_progress/blocked, done/resolved, and superseded/invalidated/abandoned. Decisions, failures, blocked or obsolete work, and reopened work need a reason.

For individual updates, use the existing record id and revision:

```bash
{command_name} agent record question "<question>" --id ID --revision N --status resolved --why "<answer and evidence>" --next=
{command_name} agent record decision "<replacement>" --supersedes OLD_ID --why "<reason>"
{command_name} agent reject "<approach>" "<failure and reason>"
```

Save decisions, failures, blockers, and results when they occur. Record failures before changing direction or ending the session. Start a session if none is active. Attach known source and test paths:

```bash
{command_name} agent record failure "<approach>" --why "<evidence>" --file SOURCE_PATH --file TEST_PATH
```

Include observed failing checks or the broken contract in the reason. Add `--parent TASK_ID` for an existing task. Use `agent reject` when file scope is unknown. Save each failure once. Update existing records by id and revision. Keep failures active until evidence resolves or supersedes them. Session completion does not resolve a failed approach.

Use `--next=` for an empty value in every shell. PowerShell can drop a separate empty quoted argument. Replacement keeps its reason and old id. Check affected tasks before more work. Use `--depends-on ID` for prerequisites and `--contradicts ID` for conflicts.

Use `--key` for stable identity, `--workstream` for scope, and `--importance 1..3` for priority. Repeat `--file` for evidence paths. Use `--summarizes ID` to link checkpoint evidence. Links must name existing records. Updates require the observed revision.

## Other session tools

Use `agent --help` for the complete command list:

- `agent status` shows the selected session. `agent list --all` includes closed agents.
- `agent search TERMS` searches work and scratch. `agent export --json` exports the selected workspace.
- `agent note TEXT` saves scratch. `--public` publishes a note. `--agent ID` sends a directed note when authorized.
- `agent task add`, `agent task done`, and `agent task list` use the shared task owner.
- `agent reset` discards private scratch. `agent terminate ID` releases another stale agent's private store. Use these only for authorized session recovery.

Checkpoints describe earlier work. Check current linked statuses before action. Use private notes only for temporary information. Do not paste source files, broad search output, credentials, or personal data.

Workers run focused checks and report outcomes. The coordinator runs integration checks and refreshes the source graph once after integration. Read source in bounded ranges. If a tool truncates output, read the missing range instead of the same complete files.

Shared records remain after finish. Finish removes the private store and sidecars when no activity remains. Active and referenced work remain. REQL keeps 200 unreferenced terminal outcomes. The `retention.agent_sessions` setting limits only public messages and the roster.

Private stores contain sessions, private notes, and directed notes. Save durable work as shared records. The `agent reset` command discards private scratch and preserves shared work.
"""
    return SkillResource("references/agent-workspace.md", agent_workspace)


def instruction_section(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
    supported_clients: str,
    section_start: str,
    section_end: str,
) -> str:
    scope = _scope(project)
    body = _bullets(
        _embedded_rule_points(),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return f"""{section_start}
## REQL

REQL uses {scope} installation paths for source context and shared work.
These coding assistants use this generated section: {supported_clients}.

{body}
{section_end}
"""


def cursor_rule(*, command_name: str, command_path: Path, fallback_command: str, section_start: str, section_end: str) -> str:
    body = _cursor_body(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    return f"""---
description: Use REQL to find source context and save shared work.
alwaysApply: true
---

{section_start}
# REQL

{body}
{section_end}
"""


def vscode_copilot_rule(*, command_name: str, command_path: Path, fallback_command: str, section_start: str, section_end: str) -> str:
    body = shared_rule_body(
        "GitHub Copilot CLI and VS Code Copilot Chat",
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=section_start,
        section_end=section_end,
    )
    return f"""---
applyTo: "**"
---

{body}
"""


def markdown_rule(
    client_name: str,
    *,
    command_name: str,
    command_path: Path,
    fallback_command: str,
    section_start: str,
    section_end: str,
) -> str:
    return shared_rule_body(
        client_name,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=section_start,
        section_end=section_end,
    )


def shared_rule_body(
    client_name: str,
    *,
    command_name: str,
    command_path: Path,
    fallback_command: str,
    section_start: str,
    section_end: str,
) -> str:
    body = _bullets(
        _embedded_rule_points(),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return f"""{section_start}
# REQL

Use REQL to select source files when {client_name} needs repository context.

{body}
{section_end}
"""


def _scope(project: bool) -> str:
    return "project-local" if project else "user-level"


def _embedded_rule_points() -> tuple[str, ...]:
    return (
        "For each task in this REQL-indexed repository, use `reql-agent` or these rules to select source files. Use `/reql` to start the skill directly.",
        *PROJECT_SKILL_SOURCE.rule_points,
        PROJECT_SKILL_SOURCE.deterministic_requirement,
    )


def _command_usage(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return (
        f"Use `{command_name}` for REQL commands. If it is not on `PATH`, use the installed launcher at "
        f"`{command_path}`. If that path is unavailable, use `{fallback_command}`.\n\n"
        "In PowerShell, use `&` before a quoted executable path. If both saved paths are obsolete, use Python from the active REQL environment. "
        "Run that Python with `-m memory.cli` or an absolute checkout `cli.py` path. "
        "Keep the working directory at the project root."
    )


def _numbered(items: tuple[str, ...], **values: object) -> str:
    return "\n".join(f"{index}. {item.format(**values)}" for index, item in enumerate(items, start=1))


def _bullets(items: tuple[str, ...], **values: object) -> str:
    return "\n".join(f"- {item.format(**values)}" for item in items)


def _cursor_body(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _bullets(
        _embedded_rule_points(),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
