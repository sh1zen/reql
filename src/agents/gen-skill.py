"""Generate REQL agent skills, instructions, and coding-agent rules."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandExample:
    command: str
    description: str


@dataclass(frozen=True)
class SkillSource:
    name: str
    title: str
    description: str
    summary: str
    command_examples: tuple[CommandExample, ...]
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
        "Use for every {platform_name} task involving a REQL-indexed repository, including small edits and "
        "read-only investigations. Start with REQL project status and focused graph retrieval to establish "
        "repository context before inspecting source."
    ),
    summary=(
        "Use REQL to narrow repository discovery, not to replace source inspection. Retrieve enough graph evidence "
        "to identify the likely owner and impact, then verify the current code and tests before changing anything. "
        "Keep the working set small and retain operational decisions so resumed work does not repeat broad scans."
    ),
    command_examples=(
        CommandExample("project status", "check whether this project has a compiled REQL graph"),
        CommandExample("project compile", "bootstrap or refresh the graph, including once after edits"),
        CommandExample('query_context --query "<terms from user request>"', "compact informative context"),
        CommandExample('query_context --query "<terms from user request>" --code', "compact code-scoped context with files, owner symbols, line ranges, and associated tests"),
        CommandExample("agent dashboard", "read shared coordination context and the current private dashboard"),
        CommandExample("project overview", "read project context and complete rejected, done, and open work across agents"),
        CommandExample('agent reject "<approach tried>" "<reason rejected>"', "retain a failed approach for later sessions"),
        CommandExample("agent list", "list active agents coordinating in this workspace"),
    ),
    workflow_steps=(
        (
            "Follow the repository's own instructions, then run `{command_name} project status`. If the command is unavailable "
            "or the graph is missing, use `references/bootstrap.md`; do not compile a healthy graph just to begin a task."
        ),
        (
            "For multi-step, resumed, or coordinated work, run `{command_name} project overview` from the project directory before choosing an approach; "
            "it shows rejected, done, and open work across registered agents without selecting one. Then run `{command_name} agent dashboard` for the selected private view. "
            "If the workspace is uninitialized, run `{command_name} agent init --name \"<task>\"` and retry the dashboard. "
            "Skip Agent Workspace for a small, self-contained task."
        ),
        (
            "If a path is already known, use `{command_name} locate \"<path>\"`; otherwise run `{command_name} query_context --query \"<short literal terms>\"`. "
            "Add `--code` for implementation work and other scope flags only when they improve the result."
        ),
        (
            "Use returned owners, paths, spans, relationships, and tests as a bounded discovery set. Inspect the relevant source, callers, contracts, and tests; "
            "the current files are authoritative when graph evidence is incomplete or stale."
        ),
        (
            "If context is insufficient, make one narrower graph query or use a targeted exact-name/path search for the unresolved gap. "
            "Do not loop on broad queries, dump the full graph, or rescan the whole repository once the working set is known. "
            "Treat REQL confidence as a lead, not proof of correctness."
        ),
        (
            "Edit the authoritative owner, update affected consumers and tests together, and run the repository's documented checks."
        ),
        (
            "After changing tracked project files, run `{command_name} project watch-status --json`; let a running watcher refresh the graph, otherwise run one "
            "`{command_name} project compile`. Skip refresh for read-only work."
        ),
        (
            "If this pass initialized Agent Workspace, close it with `{command_name} agent finish \"<outcome and validation>\"`."
        ),
    ),
    rule_points=(
        (
            "Prefer `{command_name}`; fall back to `{command_path}`, then `{fallback_command}`. Start with `project status` and bootstrap "
            "with `project compile` only when the graph is missing or unusable."
        ),
        (
            "If a path is known, use `{command_name} locate \"<path>\"`; otherwise start with `{command_name} query_context --query \"<short literal terms>\"`. "
            "Let graph paths, owners, spans, relationships, and associated tests bound discovery, then inspect the current source and contracts needed to make the change safely."
        ),
        (
            "When graph context is insufficient, refine once or use a targeted exact-name/path search. Source is authoritative; REQL evidence "
            "narrows work but does not replace code review, tests, or repository instructions."
        ),
        (
            "Keep retrieval and source reads bounded: use short literal queries, read returned paths and spans, ask for JSON only when structured fields are needed, "
            "and stop discovery once ownership, impact, and tests are clear. Avoid repeated project compile or workspace-wide scans on a healthy graph."
        ),
        (
            "Use Agent Workspace only for multi-step, resumed, or coordinated work. If `agent dashboard` is uninitialized, run `agent init --name \"<task>\"`; "
            "read `project overview` from the project directory for cross-agent rejected, done, and open history. Record a discarded approach "
            "with `agent reject \"<approach>\" \"<reason>\"`; finish only a pass that initialized or resumed an Agent Workspace session."
        ),
        (
            "After changed project files pass their documented checks, run `{command_name} project watch-status --json`; let a running watcher refresh the graph or run one "
            "`{command_name} project compile`. Skip refresh for read-only work and do not start watch mode unless continuous monitoring was requested."
        ),
        (
            "In the final response, report changed files, behavior, and checks actually run. Keep personal configuration actions separate and "
            "never edit them unless requested."
        ),
    ),
    deterministic_requirement="Keep REQL deterministic and local; the project graph stores repository facts, while Agent Workspace stores only operational task state.",
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
    agent_workspace = _agent_workspace_resource(
        platform_name=platform_name,
        scope=_scope(project),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return (
        *(tuple((PROJECT_SKILL_SOURCE.name, item.path, item.content) for item in project_resources)),
        (PROJECT_SKILL_SOURCE.name, agent_workspace.path, agent_workspace.content),
    )


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
    reference_routing = _reference_routing(source.name)
    return f"""---
name: {source.name}
description: {source.description.format(platform_name=platform_name)}
---

# REQL Coding Workflow

{source.summary}

{workflow}

## Load only when triggered

{reference_routing}

## Rules

- Load a routed reference only when its trigger occurs. Load another only if the task later reaches that separate case; never preload all references.
- Treat graph output as discovery evidence, current source as authoritative, and repository instructions as controlling.
- Stop querying once the owner, impact, edit locations, and test targets are clear.
"""


def _reference_routing(source_name: str) -> str:
    return "\n".join(
        [
            "- Missing/stale graph, compile, exclusions, command fallback -> `references/bootstrap.md`.",
            "- Query choice, raw REQL, or insufficient context -> `references/query.md`.",
            "- Watch/compile troubleshooting, custom storage, cache, deltas, or special local configuration -> `references/update-watch.md`.",
            "- Reports, exports, hubs, communities, or MCP -> `references/reports-exports.md`.",
            "- Document ingestion or local processing -> `references/document-semantics.md`.",
            "- Durable planning, dashboard recovery, `project overview`, or `reql agent` -> `references/agent-workspace.md`.",
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
  short_description: "Narrow coding work with REQL graph evidence."
  default_prompt: "Use $reql-agent to narrow repository discovery, verify the current source and tests before editing, use Agent Workspace only for multi-step or resumed work, and refresh the graph once after changed project files."
"""
    bootstrap = f"""# REQL reference: bootstrap and project state

Load this when checking whether a workspace already has REQL graph context, when first compiling a project, or when deciding how to establish a bounded working set.

## Command resolution

{usage}

## Fast path: existing graph

Start repository discovery here:

```bash
{command_name} project status
```

If status succeeds, treat `.reql/memory.reql` as the repository context index. Do not rebuild just because the user asked a natural-language codebase question. Query the graph until it identifies the relevant paths and spans, then read exact files only when edits, debugging, or tests require them.

## First-time bootstrap

If status reports `Project not found`, run a one-shot compile from the runtime workspace root:

```bash
{command_name} project compile
```

The one-shot bootstrap is allowed without asking again because the installed workflow selected REQL project mode. If compile fails, report the error briefly and continue from the smallest source locations implied by the request.

## Graph-defined working set

Use REQL results to establish a bounded discovery set: candidate files, symbols, owners, source fragments, line ranges, relationships, and associated tests. When the first result is incomplete, make one narrower query or follow a specific graph edge with `query_explore`, `query_graph`, `query_memories`, or `inspect`.

For token-efficient work, request rendered context first and use `--json` only when its structured fields answer a specific next question. Keep source reads to the returned paths and spans plus callers, contracts, and tests needed for the change. Do not repeatedly compile a healthy graph or replace a narrow query with a whole-project file or text dump.

Once the graph identifies specific paths or spans, inspect the current source plus the callers, contracts, and tests needed to verify the change. Use targeted exact-name or path searches when checking references the graph may not model, such as dynamic registrations. Stop discovery as soon as the owner, impact, edit locations, and test targets are clear.

## Exclusions

Configure additional compile exclusions in the project's `reql.conf` under
`scan.exclude`. Use `./` only for rules anchored to the config directory; omit
it to match at any depth. The only wildcard form is `*suffix` in the final
segment. Never use workspace-wide patterns such as `*`, `**`, or `**/*`. Never
exclude source/framework roots needed for the task just to make indexing smaller.

## Configuration

Project commands search for `reql.conf` from the target path upward and join its lists with protected internal defaults. Keep the core compile path deterministic and usable without model providers.

Installed for: {platform_name} ({scope}).
"""
    query = f"""# REQL reference: querying existing graph context

Load this when the user asks a question about a compiled project, architecture, dependencies, symbols, reports, memories, or source evidence.

## Choose the narrowest query

- Use `{command_name} locate "path/to/known/file"` when the project-relative path is known or its documentation extension is omitted. It uses exact normalized path indexes without semantic ranking or graph expansion.
- Use `{command_name} query_context --query "<terms from user request>"` first for most questions and small edits. Keep the query short and literal; for exact-name cleanup, `query_context --query "graphify"` is better than a long synthetic query.
- Add `--code`, `--docs`, `--test`, or `--cleanup` only when the user request clearly needs that section. Start without `--json`; rendered context is usually enough for a coding agent to choose files and line ranges.
- Use `{command_name} query_memories --query "<terms from user request>" --limit 8` for compact source/memory text rows when `query_context` is too broad.
- Use `{command_name} query_explore --query "<terms from user request>" --view owners --view code` when a coding task needs a tighter function-level owner slice before source reads.
- Use `{command_name} query_graph --query "<terms from user request>" --max-depth 2` when you need seed nodes, edges, sources, and filtered-node diagnostics.
- Use `{command_name} query "RETRIEVE '<terms from user request>' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"` when explicit custom REQL columns or source locations are needed.
- Use `{command_name} inspect --node-id NODE_ID --json` after `query_memories`, `query_graph`, or a REQL statement prints an id and you need the node's source/location and immediate neighbors.
- Use `{command_name} query "..."` for explicit REQL statements.

Common REQL statements:

```bash
{command_name} query "PROJECTS"
{command_name} query "ARTIFACTS LIMIT 20"
{command_name} query "SYMBOLS TYPE Function WHERE name CONTAINS 'compile' LIMIT 20"
{command_name} query "FRAGMENTS WHERE relative_path CONTAINS 'docs' LIMIT 20"
{command_name} query "RETRIEVE 'office plant' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"
{command_name} query "FIND nodes WHERE text ILIKE '%office plant%' LIMIT 10"
{command_name} query "FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class','possibly_orphan_directory'] RETURN finding_type,severity,cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,directory,file_count,files,line_start,reason"
{command_name} query "MATCH (s)-[:HAS_FINDING]->(f:StaticAnalysisFinding) RETURN s.type,s.name,f.finding_type,f.relative_path,f.line_start"
{command_name} query "HUBS LIMIT 20"
{command_name} query "CACHE STATUS"
```

Useful `WHERE` operators include `LIKE`, `ILIKE`, `REGEX` or `MATCHES`,
`BETWEEN ... AND ...`, `IN [...]`, `IS NULL`, and `IS NOT NULL`.

## Raw REQL Statements

Use raw `{command_name} query "..."` statements when you need deterministic rows instead of a synthesized context block. Raw queries are for verification and narrowing: exact ids, custom columns, provenance, source locations, graph relationships, cleanup candidates, cache state, or a compact table that another tool can consume.

Use `RETRIEVE ... RETURN ...` when a natural-language query is still useful but you need explicit columns:

```bash
{command_name} query "RETRIEVE 'office plant' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"
{command_name} query "RETRIEVE 'payment workflow' LIMIT 8 RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start"
```

Use `FIND`, `SYMBOLS`, `FRAGMENTS`, and `MATCH` when you already know a filter, id, file, symbol, or relationship:

```bash
{command_name} query "FIND nodes WHERE id IN [document_term:abc, document_term:def] RETURN id,type,label,text"
{command_name} query "SYMBOLS WHERE relative_path = 'src/memory/services/retrieval.py' RETURN type,name,qualified_name,start_line,end_line LIMIT 50"
{command_name} query "FRAGMENTS WHERE relative_path = 'README.md' RETURN id,text,line_start,line_end LIMIT 20"
{command_name} query "MATCH (s)-[:REFERENCES]->(t) WHERE s.relative_path = 'README.md' RETURN s.id,s.text,t.type,t.name LIMIT 20"
```

Keep raw queries bounded: include `LIMIT`, request only the columns needed for the next decision, and include `relative_path`, `line_start`, `line_end`, `source_for`, `relation`, or `direction` when provenance matters. Prefer raw queries after `query_context`, `query_explore`, `query_memories`, or `query_graph` has surfaced ids or candidate files; do not use raw queries as a reason to scan the whole repository manually.

## Free-form Query Shape

REQL is not an LLM. It uses tokenization, lexical matching, graph links, and activation, so free-form queries work best when they include 3-8 informative terms from the user's request and nearby context. Keep the user's language instead of translating. Preserve exact identifiers, file names, commands, error messages, fields, endpoints, APIs, and symbol names when available. Avoid empty, placeholder, or context-dependent pronoun queries; rewrite them into anchored terms before querying.

## Query Types

- Informative: use no mode flag for project knowledge, structure, documents, architecture, existence checks, and "is there anything like X" questions. Prefer `{command_name} query_context --query "<terms from user request>"`, `{command_name} query_memories --query "<terms from user request>"`, or `{command_name} query_graph --query "<terms from user request>" --max-depth 2`. Code-scoped `query_context` renders at most eight paths with owner symbols, bounded line ranges, and associated tests; use `--json` when graph links or planning fields are needed.
- Scope filters: use `--code`, `--docs`, and `--test` with informative or cleanup queries when the user asks for a precise section. They restrict results to code symbols/source, documentation/imported documents, or tests.
- Cleanup: use `--cleanup` for safe-remove dead code, unused imports, unused variables, and removal candidates. Start with `{command_name} query_context --query "<terms from user request>" --cleanup`, then remove only confirmed candidates. Use the explicit `FINDINGS` query below when reviewing public API, low-confidence, test-local, or validation-required candidates.

## Dependency Exploration

Use `query_explore` when you already know the task target but need the surrounding dependency chain:

```bash
{command_name} query_explore --query "<terms from user request>" --view owners --view code
{command_name} query_explore --query "<terms from user request>" --owners-only
{command_name} query_explore --query "<terms from user request>" --callers-only
{command_name} query_explore --query "<terms from user request>" --serialization-paths-only
{command_name} query_explore --query "<template terms>" --structural-duplicates-only
{command_name} query_explore --query "<terms from user request>" --view owners --view callers --view public_surface
```

Prefer `owners` to find implementation homes, `callers` for impact, `public_surface` before removing or renaming exported symbols, `serialization_paths` before changing model/storage fields, `docs_mentions` for documentation/examples, `structural_duplicates` for template markup refactors, and `code` for working-set and targeted read ranges.

## Answering rules

Use graph output as evidence, not as permission to invent missing links. Cite node ids, source files, source fragments, or REQL rows when making factual claims. The checked-out source is authoritative: inspect it before edits and whenever the graph is stale, ambiguous, or incomplete.

Let graph queries establish a bounded discovery set, while still reading enough implementation, callers, contracts, and tests to understand the behavior being changed.

## Graph-led source inspection

Start with `locate` for a known path or `query_context` for task terms. Use `query_explore`, `query_graph`, `query_memories`, `inspect`, or bounded raw REQL only when the first result leaves a specific relationship or location unresolved.

Keep the working set in the task: record the owner, affected callers, contract, and test targets once, then reuse that map during implementation. Use Agent Workspace for durable task status and decisions when the work spans sessions. Its operational history complements graph evidence; it is not a copy of the project graph.

After REQL returns candidate paths, symbols, owners, source fragments, or line ranges, inspect the current files for verification and implementation. If context is insufficient, make one narrower graph query or use a targeted exact-name/path search. Broad repository scans remain a last resort, but direct source inspection is part of the normal coding workflow.

## Code-Scoped Workflow

When the task asks for an implementation, bug fix, refactor, or behavior change:

1. Build a query from the user request's own feature, behavior, file, command, error, field, endpoint, API, or symbol terms; then run `{command_name} query_context --query "<terms from user request>" --code`.
2. For exact identifiers, legacy names, or one-off removals, try the plain shortest form first, for example `{command_name} query_context --query "graphify"`.
3. For a clear one-file or exact-symbol edit, stop after the first sufficient result or targeted read; skip Agent Workspace and extra graph views unless ambiguity appears.
4. Use rendered files, symbols, line ranges, and structured fields such as `owner_candidates`, `read_plan`, `change_chain`, `contracts`, `impact`, `targeted_reads`, and `test_targets` to choose the initial files and spans.
5. Read enough surrounding implementation to understand invariants and evaluation order. Read a full file when its size or structure makes that safer than stitching together isolated spans.
6. Run `{command_name} query_explore --query "<terms from user request>" --view owners --view code` when the context is noisy or you need owners and code slices before choosing files.
7. If the context still lacks enough code, retrieve exact locations with `{command_name} inspect --node-id NODE_ID --json` or `{command_name} query "RETRIEVE '<terms from user request>' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"`.
8. Confirm references with a targeted exact-name/path search when dynamic use, public exports, generated code, or framework registration may not be represented in the graph.
9. Modify the existing authoritative owner when one exists; preserve contracts and update affected consumers and tests together.
10. If context remains broad or irrelevant after one refinement, state the evidence gap and continue with targeted source inspection instead of repeatedly querying.

## Unused-Code Cleanup

When the user asks to find or remove unused code, start from REQL's deterministic cleanup findings instead of inventing a new repository scanner. Use the compiled graph to collect candidates, then verify each likely removal with targeted source inspection because some symbols can be public APIs, framework callbacks, entry points, reflection targets, or dynamically referenced plugin hooks.

Recommended sequence:

1. Ensure project status is active or bootstrap compile has completed.
2. Retrieve a natural-language context block with `{command_name} query_context --query "unused code dead code cleanup" --cleanup`.
3. List concrete findings with:

```bash
{command_name} query "FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class','possibly_orphan_directory'] RETURN finding_type,severity,cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,directory,file_count,files,line_start,reason,evidence_scope,confidence ORDER BY cleanup_priority LIMIT 100"
```

4. Expand provenance for ambiguous rows with:

```bash
{command_name} query "MATCH (s)-[:HAS_FINDING]->(f:StaticAnalysisFinding) RETURN s.type,s.name,s.qualified_name,f.finding_type,f.relative_path,f.line_start,f.reason LIMIT 100"
```

5. Inspect only the candidate files and nearby callers/importers. Use targeted symbol searches when needed to check entry points, tests, public exports, callbacks, dynamic `getattr`/reflection, and documentation examples.
6. Classify results separately: safe removals, likely dead but public/API-risk, directory-level review items, and false positives. Treat `possibly_unused_function`, `possibly_unused_method`, and `possibly_unused_class` as local cleanup candidates, not whole-program proof.

Prefer high-priority `unused_variable` and `unused_import` findings for direct edits. `possibly_orphan_directory` findings aggregate multiple isolated code files under one containing directory with `file_count` and `files`; validate entrypoints, plugins, scripts, dynamic imports, and external users before deleting that directory. Require stronger evidence before deleting public functions, methods, classes, scripts, generated adapters, CLI/MCP tools, or framework lifecycle methods.

## JSON mode

Use `--json` only when another tool or script needs structured fields, when you must programmatically consume keys such as `owner_candidates`, `working_set`, `read_plan`, `change_chain`, `contracts`, `impact`, `targeted_reads`, `test_targets`, or `cleanup_candidates`, or when rendered text is ambiguous:

```bash
{command_name} query_context --query "<terms from user request>" --code --json
{command_name} query_graph --query "<terms from user request>" --max-depth 2 --json
{command_name} query_explore --query "<terms from user request>" --view owners --view code --json
{command_name} query_memories --query "<terms from user request>" --limit 8 --json
{command_name} inspect --node-id NODE_ID --json
```
"""
    update_watch = f"""# REQL reference: update and watcher troubleshooting

Load this only when post-edit refresh does not behave as expected, watcher status is `stale` or `unknown`, monitor mode or storage is non-default, cache/delta inspection is needed, or a local configuration requirement must be reported.

## Diagnose post-edit refresh

Check watcher state through REQL rather than querying the operating-system process table:

```bash
{command_name} project watch-status --json
```

If the result is `stopped`, refresh the graph with:

```bash
{command_name} project compile
```

This refreshes only changed/deleted artifacts through the incremental cache. If the watcher is `running`, allow it to process the filesystem event instead of starting another compile. For `stale` or `unknown`, inspect the reported lock metadata and use `storage locks`; do not inspect `ps`, `Get-CimInstance`, or equivalent process listings.

Monitor mode and `watch-status` use the same project-local `.reql/memory.reql` store.

## Final change classification

Keep repository changes and machine-local setup separate in the final response:

- Under `Versioned functional changes`, list only source, tests, documentation, examples, and versioned configuration templates that changed in the repository.
- If the implementation adds a required configuration field, verify whether the user's personal `config.json` must supply it. If so, add a separate `Local configuration required` item naming the field path, the expected value or how to obtain it, and the relevant personal config location when known.
- Never include the personal `config.json` in the versioned changed-file list. Do not edit it unless the user explicitly requested that local change.
- If defaults, migration, or backward-compatible loading make a personal update unnecessary, state `Local configuration required: none` rather than implying manual action.

## Watch mode

Use watch mode when the user asked for monitoring/continuous REQL updates or a long-running background process is appropriate:

```bash
{command_name} project compile --watch
```

The watcher performs an initial cache check, then compiles only dirty or deleted artifacts. Use bounded options for scripts and tests:

```bash
{command_name} project compile --watch --watch-iterations 1
{command_name} project compile --watch --watch-interval 2 --watch-debounce 0.5
```

At any time, `{command_name} project watch-status` reports `running`, `stopped`, `stale`, or `unknown`, including PID and liveness when available. It reads the lock sidecar directly, so it works while the watcher owns the graph write lock.

Ask before starting watch mode or `cache clear` unless the user explicitly requested that operation.

## Cache and deltas

Inspect cache state and recent compile changes with:

```bash
{command_name} cache status
{command_name} query "DELTAS LIMIT 10"
{command_name} query "DELTAS WHERE id = 'delta:...' LIMIT 1" --json
```

Use `{command_name} project compile` for manual incremental refreshes as well as bootstrap and normal after-edit refreshes.
"""
    reports_exports = f"""# REQL reference: reports, graph analysis, exports, and MCP

Load this when the task needs project reports, graph analysis records, visual exports, JSON artifacts, or MCP wiring.

## Reports

Write project reports with:

```bash
{command_name} project report --output reports/
```

The report set includes `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and `CACHE_REPORT.md`. Use it when the user asks for an audit-style project summary, cache/delta state, symbols, communities, or hubs.

## Analysis commands

```bash
{command_name} query "COMMUNITIES LIMIT 20"
{command_name} query "HUBS LIMIT 20"
{command_name} query "HUBS TYPE Function,Class LIMIT 10"
{command_name} query "EXPLAIN HUB 'NODE_ID'" --json
```

`COMMUNITIES` and `HUBS` persist analysis records. Treat those REQL statements as write/update operations when approvals are relevant.

## Exports

```bash
{command_name} export --json --out reql-graph-out
{command_name} export --html --out graph.html
{command_name} export --html --json --out reql-graph-out
```

HTML export creates a standalone `graph.html` with embedded data, search, filters, and node inspection. JSON export writes `graph.json`.

## MCP

Start the optional MCP server only when the client needs live tool access:

```bash
reql-mcp --read-only
reql-mcp --config reql.conf --set project.id=agent-a --read-only
```

If the `reql-mcp` console script is unavailable but the REQL Python package is
installed, use `python -m mcp.server --read-only` (and pass the same `--config`
or `--set` options when needed).

Use read-only mode for context retrieval. Use write tools such as compile/watch/hubs only with the same approval discipline as the CLI commands.
"""
    document_semantics = f"""# REQL reference: document structure

Load this only when compiling documents or changing document parsing/linking behavior.

## Default behavior

Project compile is deterministic. Code is parsed structurally. Markdown, plain text, and PDF artifacts are registered and fragmented as source context.

Compile projects with:

```bash
{command_name} project compile
```

Document fragments are linked back to source artifacts. REQL also runs a local deterministic document processor that emits ranked document terms, raw observation events, term co-occurrence edges, and code links when document text explicitly names code symbols.

## Deterministic document processor

The document layer is language-agnostic and structure-agnostic. It tokenizes Unicode text locally, ranks useful terms and compact phrases, records raw `RawEvent` observations below each term, and creates `CO_OCCURS_WITH` relationships for terms seen together in a fragment.

The processor writes:

- `Concept` nodes with `extractor: document_processor`, `rank`, `term_frequency`, `fragment_count`, and `raw_event_count`.
- `RawEvent` nodes with the source fragment, observed term, occurrence count, rank, line range, and evidence text.
- `MENTIONS`, `EVIDENCED_BY`, `DERIVED_FROM`, and `CO_OCCURS_WITH` edges with source provenance.
- `REFERENCES` edges from ranked document terms to code symbols when the same compiled document fragment explicitly mentions a code symbol.

Do not add manual document import steps. The core compile path must remain deterministic and local.
"""
    return (
        SkillResource("agents/openai.yaml", openai_yaml),
        SkillResource("references/bootstrap.md", bootstrap),
        SkillResource("references/query.md", query),
        SkillResource("references/update-watch.md", update_watch),
        SkillResource("references/reports-exports.md", reports_exports),
        SkillResource("references/document-semantics.md", document_semantics),
    )


def _agent_workspace_resource(
    *,
    platform_name: str,
    scope: str,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> SkillResource:
    usage = _command_usage(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    agent_workspace = f"""# REQL reference: Agent Workspace

Load this when using `{command_name} agent` for durable task planning and coordination.

## Command resolution

{usage}

## Dashboard model

The public dashboard at `.reql/agent-dashboard.reql` contains agents, active-task summaries, public context, and drill commands. Each agent has a private store under `.reql/agents/` with tasks, notes, rejected approaches, and sessions. `{command_name} agent dashboard` shows recent rejected, done, and open work for the selected agent and ends with `{command_name} project overview`.

Run `{command_name} project overview` from the project directory when resuming work or coordinating with other agents. It needs no path or agent id and reads the complete rejected, done, and open history of every registered agent alongside the project explanation. Each operational record retains its agent and session; the canonical project graph remains the source of repository facts. Check rejected approaches and their reasons before repeating work. Use current source and tests to verify any code claims in the overview.

Use Agent Workspace when durable planning or recovery is useful. For a small self-contained task, skip it. On a resumed pass, read `project overview` first, then try `agent dashboard`; if it reports that the workspace is uninitialized, initialize a named session and retry. If agent selection is ambiguous, select the intended private agent explicitly; `project overview` remains cross-agent. Do not call `agent finish` unless this pass initialized or resumed a session.

## Workflow

```bash
{command_name} project overview
{command_name} agent init --name "Focused implementation pass"
{command_name} agent dashboard
{command_name} agent task add "Patch serializer error handling"
{command_name} agent note "Check caller contracts first"
{command_name} agent reject "Cache every query" "Results stayed stale after source edits"
{command_name} agent note --agent AGENT_ID "The parser API now returns ParseResult"
{command_name} agent note --public "Use parse_document_v2 for new work"
{command_name} agent task done TASK_ID "Serializer fix completed and tests are passing."
{command_name} agent finish "Focused tests passed; serializer fix is ready."
```

`init` creates or resumes the selected session and registers the agent active. `finish` closes the session, marks the agent finished, and publishes its message as public context without deleting private history. Use `agent terminate AGENT_ID` for stale sessions.

`agent list` returns active agents; add `--all` for finished and terminated agents. `agent search QUERY` searches public context and permitted private dashboard history. `agent dashboard` shows the public dashboard plus the selected private dashboard; use `--agent "agent:AGENT_ID"` only when opening a particular private dashboard. The project overview always includes all registered agents.

Tasks and rejected approaches are stored with their owner. `agent reject APPROACH REASON` records what was tried and why it was discarded; give a concrete failure or constraint, not just a verdict. `agent task done TASK_ID MESSAGE` records the result of completed work. Task completion messages, public notes, and finish messages are public context. Notes are private by default, directed with `note --agent`, and shared with `note --public`.

Keep Agent Workspace small and actionable: add tasks for distinct remaining outcomes, mark them done with the observed result, and record only decisions or rejected approaches that would prevent repeated work. Use `agent note` for short operational context that will matter after a session boundary. Do not paste source files, broad search output, or graph facts into notes; retrieve repository facts from the project graph and verify current source. This preserves useful continuity without making later agents reread bulky scans.

Use normal query commands for code facts:

```bash
{command_name} query_context --query "<task terms>" --code
```

Installed for: {platform_name} ({scope}).
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

REQL is installed ({scope}) as the deterministic memory graph for this workspace.
This generated section is shared by supported coding assistants: {supported_clients}.

{body}
{section_end}
"""


def cursor_rule(*, command_name: str, command_path: Path, fallback_command: str, section_start: str, section_end: str) -> str:
    body = _cursor_body(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    return f"""---
description: Use REQL deterministic memory to establish bounded repository context
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

Use REQL deterministic memory to establish the working set when {client_name} needs repository context.

{body}
{section_end}
"""


def _scope(project: bool) -> str:
    return "project-local" if project else "user-level"


def _embedded_rule_points() -> tuple[str, ...]:
    return (
        "For every task involving this REQL-indexed repository, use the generated `reql-agent` skill or this concise REQL rule to establish the repository working set. `/reql` also invokes it explicitly.",
        *PROJECT_SKILL_SOURCE.rule_points,
        PROJECT_SKILL_SOURCE.deterministic_requirement,
    )


def _command_usage(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return (
        f"Prefer `{command_name}` for REQL commands. If it is not on `PATH`, use the installed shim at "
        f"`{command_path}`. If that is unavailable, use `{fallback_command}`."
    )


def _format_examples(source: SkillSource, command_name: str) -> str:
    width = max(len(f"{command_name} {example.command}") for example in source.command_examples) + 2
    return "\n".join(
        f"{command_name} {example.command}".ljust(width) + f"# {example.description}"
        for example in source.command_examples
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
