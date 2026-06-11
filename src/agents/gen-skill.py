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
    name="reql-project",
    title="REQL Project",
    description=(
        "A Python graph-native and storage-agnostic memory engine. Use when {platform_name} "
        "needs to implement, review, document, inspect, or extend a project with bounded "
        "repository graph context while preserving deterministic core behavior."
    ),
    summary=(
        "Use this skill for project mode. REQL compiles repository artifacts into a deterministic "
        "graph for bounded context, retrieval, REQL queries, graph reports, communities, hubs, "
        "and bridge analysis."
    ),
    command_examples=(
        CommandExample("project status .", "check whether this project has a compiled REQL graph"),
        CommandExample("project compile .", "build or incrementally refresh the graph once, including after edits without watch"),
        CommandExample("project compile . --watch", "monitor this project and compile only changed/deleted files"),
        CommandExample("project compile . --watch --watch-iterations 1", "bounded watch check for automation/tests"),
        CommandExample("project update .", "manual incremental refresh of a previously compiled project"),
        CommandExample('project exclude "path/or/glob"', "add a scan.exclude pattern to the project config"),
        CommandExample("cache status .", "inspect total, cached, dirty, and deleted artifacts"),
        CommandExample('query "DELTAS LIMIT 10"', "list recent compilation graph deltas"),
        CommandExample("project report . --output reports/", "write GRAPH_REPORT.md, GRAPH_DELTAS.md, CACHE_REPORT.md"),
        CommandExample('query_context --query "<terms from user request>"', "compact informative context"),
        CommandExample('query_context --query "<terms from user request>" --code', "compact code-scoped context with files, symbols, and targeted reads"),
        CommandExample('query_context --query "<terms from user request>" --docs', "limit context to documentation and imported documents"),
        CommandExample('query_context --query "<terms from user request>" --test', "limit context to tests"),
        CommandExample('query_context --query "<terms from user request>" --cleanup', "cleanup findings matching the query"),
        CommandExample('query_explore --query "<terms from user request>" --view owners --view code', "function-level owner/code slices for coding agents"),
        CommandExample('query_memories --query "<terms from user request>"', "compact ranked memory/source texts"),
        CommandExample('query_graph --query "<terms from user request>"', "seeds, edges, sources, and compact context"),
        CommandExample("inspect --node-id NODE_ID --json", "resolve a node id to location, sources, and neighbors"),
        CommandExample('query "RETRIEVE \\"<terms from user request>\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"', "source/code text with exact locations"),
        CommandExample('query "HUBS LIMIT 20"', "inspect useful graph hubs"),
    ),
    workflow_steps=(
        "Start with `{command_name} project status .` before broad repository exploration.",
        (
            "If status succeeds, treat the graph as already built. Use `{command_name} query_context --query \"...\"`, "
            "`{command_name} query_explore --query \"...\"`, `{command_name} query_memories --query \"...\"`, `{command_name} query_graph --query \"...\"`, "
            "`{command_name} inspect --node-id NODE_ID --json`, or `{command_name} query \"...\"` for repository context."
        ),
        (
            "For implementation or bug-fix tasks, start with the simplest useful query, usually "
            "`{command_name} query_context --query \"<terms from user request>\" --code`. For narrow exact-name removals or checks, "
            "`{command_name} query_context --query \"<exact term>\"` is often enough. Use the files, symbols, and line ranges it renders "
            "as the starting point. If more source is needed, read only the missing spans. Inspect the listed symbols and line ranges "
            "before reading whole files or adding new modules, wrappers, overrides, or parallel implementations. Use `{command_name} query_explore --query "
            "\"<terms from user request>\" --view owners --view code --view callers --view public_surface` when "
            "you need dependency slices before reading source files. When the rendered context lacks enough code, use "
            "`{command_name} query_context --query \"<terms from user request>\" --code`, "
            "`{command_name} inspect --node-id NODE_ID --json`, or `{command_name} query "
            "\"RETRIEVE \\\"<terms from user request>\\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end\"` "
            "to get exact locations, then read only the local file spans that still need source-level inspection."
        ),
        (
            "Choose the query_context mode explicitly: no flag (`informative`) for project knowledge, structure, documents, "
            "existence checks, and architecture questions; add `--code`, `--docs`, or `--test` to limit the same mode to a precise section; "
            "`--cleanup` for dead code, unused imports, unused variables, and removal candidates."
        ),
        (
            "If status reports `Project not found`, immediately run `{command_name} project compile .` from the "
            "runtime workspace root. Do not skip this step and do not continue with broad raw file exploration first. "
            "After compile succeeds, query REQL before reading many files."
        ),
        (
            "If `{command_name} project compile .` fails, report the failure briefly, then continue with targeted raw "
            "file reads only as a fallback."
        ),
        "Document processing runs locally inside `{command_name} project compile .`.",
        (
            "During active edit sessions, prefer one `{command_name} project compile . --watch` monitor instead of "
            "repeated manual compiles. Start watch mode only when the user asked for monitoring/continuous REQL updates "
            "or when the current environment supports a long-running background process; otherwise the one-shot compile "
            "bootstrap is the required project setup."
        ),
        (
            "If a watch process is already running for this workspace, do not start another compile or rebuild loop. "
            "Query the maintained graph instead."
        ),
        (
            "Use REQL as the repository context index before raw repository scans. Do not run broad `rg`, recursive "
            "directory listings, custom scanners, `find`, `grep -R`, ad hoc Python/Node crawlers, or other repository-wide "
            "discovery commands to duplicate `query_context`, `query_explore`, `query_memories`, or `query_graph` "
            "results. Read specific files only after REQL identifies them, when the user names an exact file/path, "
            "or when exact source edits, targeted debugging, or tests require them. Use raw `{command_name} query \"...\"` "
            "statements when you need exact rows, custom columns, explicit filters, provenance fields, or graph checks that "
            "the higher-level `query_*` commands do not render."
        ),
        (
            "When raw tools are needed, keep them narrow: inspect REQL-returned paths and line ranges first, use "
            "file-scoped `rg`/symbol searches rather than workspace-wide scans, and stop expanding once there is enough "
            "evidence to choose the owner file or edit location."
        ),
        (
            "For unused-code or dead-code requests, query REQL cleanup findings first. Read `references/query.md`, "
            "use `FINDINGS` or `StaticAnalysisFinding` queries for candidates, then validate candidates with targeted "
            "source reads or symbol searches before recommending removals."
        ),
        (
            "Do not add exclusions before the first bootstrap compile unless the user explicitly asked for exclusions "
            "or the path is an obvious dependency/cache/build-output directory such as `node_modules/`, `vendor/`, "
            "`.tmp/`, `dist/`, or `build/`. Never exclude framework/source roots needed for the task, such as "
            "`wp-content/`, application directories, plugin/theme directories, or broad core directories, just to make "
            "indexing smaller."
        ),
        (
            "When exclusions are needed, call `{command_name} project exclude` once with all patterns in one command. "
            "Do not run multiple `project exclude` commands in parallel. Do not use workspace-wide patterns such as "
            "`*`, `**`, or `**/*`."
        ),
        (
            "After modifying project files, if no `{command_name} project compile . --watch` process was running, run "
            "`{command_name} project compile .` once before finishing so the REQL graph reflects the completed edits. "
            "If watch is running, let it update the graph and query the maintained graph instead."
        ),
        (
            "Before the final response for any task that changed files, confirm the graph update path: either the "
            "`{command_name} project compile . --watch` process already captured the edits, or run "
            "`{command_name} project compile .` once and report the result briefly."
        ),
        (
            "Ask before starting long-running or non-bootstrap write/update commands such as "
            "`{command_name} project compile . --watch`, `{command_name} project update .`, or "
            "`{command_name} cache clear .`. Do not ask before the required one-shot "
            "`{command_name} project compile .` bootstrap after `Project not found`."
        ),
        "Keep REQL optional and deterministic: compile, document processing, query, retrieval, reports, hubs, and communities run locally.",
    ),
    rule_points=(
        "Prefer `{command_name}`. If it is not on `PATH`, use `{command_path}`. If that is unavailable, use `{fallback_command}`.",
        "Start with `{command_name} project status .` to check graph state.",
        (
            "If status reports `Project not found`, immediately run `{command_name} project compile .` from the "
            "runtime workspace root before broad raw file exploration. If compile fails, report the failure and use "
            "targeted raw file reads only as a fallback."
        ),
        (
            "Use `{command_name} project exclude \"path/or/glob\"` only for explicit exclusions or obvious "
            "dependency/cache/build-output directories. Do not exclude framework/source roots needed for the task, "
            "do not use workspace-wide patterns such as `*`, `**`, or `**/*`, and do not run multiple exclude commands "
            "in parallel; pass all patterns in one command."
        ),
        (
            "Retrieve bounded context with a query built from the user request's own feature, behavior, file, command, "
            "error, field, endpoint, API, or symbol terms. Keep the user's language, preserve identifiers and exact errors, "
            "and use the simplest query that can answer where to look, usually `{command_name} query_context --query \"<terms from user request>\"`. "
            "Use `{command_name} query_explore --query \"<terms from user request>\"`, "
            "`{command_name} query_memories --query \"<terms from user request>\"`, or "
            "`{command_name} query_graph --query \"<terms from user request>\"`. Do not duplicate that context with broad "
            "`rg`, recursive directory listings, `find`, `grep -R`, custom scanners, ad hoc crawlers, or other repository-wide discovery commands."
        ),
        (
            "Limit raw scans and large reads. Do not start with workspace-wide raw tools when REQL can answer where to look. "
            "After REQL returns candidate files, symbols, or line ranges, use raw tools only in those paths or nearby spans, "
            "or for exact user-named files and test/debug verification."
        ),
        (
            "Choose the query_context mode explicitly: no flag (`informative`) for project knowledge, structure, documents, "
            "existence checks, and architecture questions; add `--code`, `--docs`, or `--test` to limit the same mode to a precise section; "
            "`--cleanup` for dead code, unused imports, unused variables, and removal candidates."
        ),
        (
            "For code changes, start with `{command_name} query_context --query \"<terms from user request>\" --code` to identify the smallest existing "
            "functions, methods, classes, files, and line ranges. For exact-name cleanup or removal tasks, a plain "
            "`{command_name} query_context --query \"<exact term>\"` can be enough and should be tried before more complex queries. "
            "Prefer returned line ranges over reading whole files. Use `{command_name} "
            "query_explore --query \"<terms from user request>\" --view owners --view code` when the context "
            "is noisy or you need a tighter function-level slice before editing. If you need more code before editing, "
            "use `inspect --node-id`, `RETRIEVE ... RETURN id,type,text,score,relative_path,line_start,line_end`, or `--json` only when structured fields are genuinely needed "
            "to collect locations before opening files, and avoid creating parallel implementations until "
            "REQL shows no suitable owner."
        ),
        (
            "Use raw `{command_name} query \"...\"` statements for deterministic row-level checks: `RETRIEVE ... RETURN ...` "
            "for exact source/code rows and locations, `FIND nodes WHERE ... RETURN ...` for filtered node lists, `MATCH` "
            "for explicit relationships, `FINDINGS` for cleanup candidates, `SYMBOLS`/`FRAGMENTS` for scoped source indexes, "
            "and `HUBS`/`CACHE STATUS` for graph/cache diagnostics. Keep raw queries bounded with `LIMIT`, request only "
            "columns needed for the next step, and include `relative_path`, `line_start`, `line_end`, `source_for`, "
            "`relation`, or `direction` when provenance matters."
        ),
        (
            "For unused-code cleanup, start with `{command_name} query \"FINDINGS WHERE finding_type IN "
            "[\\\"unused_variable\\\",\\\"unused_import\\\",\\\"possibly_unused_function\\\","
            "\\\"possibly_unused_method\\\",\\\"possibly_unused_class\\\"] RETURN finding_type,severity,"
            "cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,line_start,reason\"`, then "
            "cross-check likely removals with targeted source reads or symbol searches."
        ),
        (
            "After modifying project files, run `{command_name} project compile .` once before finishing unless a "
            "`{command_name} project compile . --watch` process was already running and updated the graph."
        ),
        "For document processing, run `{command_name} project compile .` normally. REQL applies configured document policies internally and uses its local deterministic processor.",
        (
            "Before the final response for any task that changed files, confirm the graph update path: either the "
            "`{command_name} project compile . --watch` process already captured the edits, or run "
            "`{command_name} project compile .` once and report the result briefly."
        ),
        (
            "Ask before long-running or non-bootstrap write/update operations such as "
            "`{command_name} project compile . --watch`, `{command_name} project update .`, or "
            "`{command_name} cache clear .`. Do not ask before the required one-shot "
            "`{command_name} project compile .` bootstrap after `Project not found`."
        ),
        (
            "Prefer one running `{command_name} project compile . --watch` process during active editing instead of "
            "repeated full rebuilds; start watch mode when monitoring/continuous updates are requested or a "
            "long-running process is appropriate."
        ),
    ),
    deterministic_requirement="Keep REQL optional and deterministic; document processing runs in the local compiler.",
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
    resources = _project_skill_resources(
        platform_name=platform_name,
        scope=_scope(project),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return tuple((PROJECT_SKILL_SOURCE.name, item.path, item.content) for item in resources)


def skill_markdown(
    source: SkillSource,
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> str:
    scope = _scope(project)
    usage = _command_usage(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    examples = _format_examples(source, command_name)
    workflow = _numbered(source.workflow_steps, command_name=command_name)
    watch_mode = ""
    if source.name == PROJECT_SKILL_SOURCE.name:
        watch_mode = f"""
## Watch Mode

Use `{command_name} project compile . --watch` as monitor mode while the agent is editing code. Keep one watcher running in the workspace. It prints each poll, reports dirty/deleted artifact counts, and compiles only when changes are detected. Stop it with interrupt when the editing session is over.

Use `--watch-interval`, `--watch-debounce`, and `--watch-iterations` only when a bounded scripted run is needed.
"""
    return f"""---
name: {source.name}
description: {source.description.format(platform_name=platform_name)}
---

# {source.title}

{source.summary}

## Usage

{usage}

```bash
{examples}

reql-mcp --read-only                                    # optional MCP server for clients that support tools
```

## Required Agent Workflow

{workflow}
{watch_mode}
## Reference Routing

- Read `references/bootstrap.md` when checking project state, compiling for the first time, handling exclusions, or deciding whether to fall back to raw files.
- Read `references/query.md` when answering a repository question from an existing REQL graph or choosing between `query_context`, `query_memories`, `query_graph`, and REQL statements.
- Read `references/update-watch.md` after modifying files, when a watcher is running, or when cache/delta state matters.
- Read `references/reports-exports.md` when generating reports, exporting graph artifacts, inspecting hubs/communities, or wiring MCP.
- Read `references/document-semantics.md` only when the task involves document ingestion or local document processing.

## Ground Rules

- Treat REQL as a bounded context index, not as a replacement for exact source edits or tests.
- Cite files, node ids, source fragments, or REQL rows when using graph-derived facts.
- If the graph lacks evidence for a claim, say that and inspect targeted files instead of inventing relationships.
- Keep the deterministic core path usable without LLM calls.

Installed for: {platform_name} ({scope}).
"""


def _project_skill_resources(
    *,
    platform_name: str,
    scope: str,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[SkillResource, ...]:
    usage = _command_usage(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    openai_yaml = """display_name: REQL Project
short_description: Use REQL deterministic memory before broad repository exploration.
default_prompt: Use REQL to inspect this project, compile it if needed, and answer from bounded graph context with cited evidence.
"""
    bootstrap = f"""# REQL reference: bootstrap and project state

Load this when checking whether a workspace already has REQL graph context, when first compiling a project, or when deciding whether raw file exploration is still needed.

## Command resolution

{usage}

## Fast path: existing graph

Run this before broad repository exploration:

```bash
{command_name} project status .
```

If status succeeds, treat `.reql/memory.reql` as the repository context index. Do not rebuild just because the user asked a natural-language codebase question. Query the graph first, then read exact files only when edits, debugging, or tests require them.

## First-time bootstrap

If status reports `Project not found`, run a one-shot compile from the runtime workspace root:

```bash
{command_name} project compile .
```

Do this before broad `rg`, recursive listings, custom scanners, or manually reading many files. The one-shot bootstrap is allowed without asking again because the installed workflow selected REQL project mode. If compile fails, report the error briefly and continue with targeted raw file reads as a fallback.

## Raw tool limits

Use REQL to decide where to look before using raw repository tools. Avoid workspace-wide `rg`, recursive directory listings, `find`, `grep -R`, custom scanners, or ad hoc crawlers while REQL can provide candidate files, symbols, owners, or line ranges.

Raw tools are appropriate after REQL has identified specific paths or spans, when the user names an exact file/path, or when tests/debugging require local verification. Keep those commands scoped to the candidate files or nearby directories, and stop expanding once you have enough evidence to choose the owner file or edit location.

## Exclusions

Do not add exclusions before the first bootstrap compile unless the user asked for them or the path is an obvious dependency/cache/build-output directory such as `node_modules/`, `vendor/`, `.tmp/`, `dist/`, or `build/`.

Use one command with all patterns:

```bash
{command_name} project exclude "path/or/glob" "another/path/"
```

Never use workspace-wide patterns such as `*`, `**`, or `**/*`. Never exclude source/framework roots needed for the task just to make indexing smaller.

## Configuration

Project commands search for `conf.yaml` from the target path upward. Use `--config path/to/conf.yaml` or repeated global `--set section.option=value` only when the task needs a different configuration. The core compile path must remain deterministic and usable without model providers.

Installed for: {platform_name} ({scope}).
"""
    query = f"""# REQL reference: querying existing graph context

Load this when the user asks a question about a compiled project, architecture, dependencies, symbols, reports, memories, or source evidence.

## Choose the narrowest query

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
{command_name} query "FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class'] RETURN finding_type,severity,cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,line_start,reason"
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

- Informative: use no mode flag for project knowledge, structure, documents, architecture, existence checks, and "is there anything like X" questions. Prefer `{command_name} query_context --query "<terms from user request>"`, `{command_name} query_memories --query "<terms from user request>"`, or `{command_name} query_graph --query "<terms from user request>" --max-depth 2`. Use the rendered files, line references, source evidence, graph links, and embedded raw-query research references.
- Scope filters: use `--code`, `--docs`, and `--test` with informative or cleanup queries when the user asks for a precise section. They restrict results to code symbols/source, documentation/imported documents, or tests.
- Cleanup: use `--cleanup` for dead code, unused imports, unused variables, possibly-unused functions/classes/methods, and removal candidates. Start with `{command_name} query_context --query "<terms from user request>" --cleanup` or the `FINDINGS` query below, then remove only confirmed candidates.

## Dependency Exploration

Use `query_explore` to reduce broad manual scanning when you already know the task target but need the surrounding dependency chain:

```bash
{command_name} query_explore --query "<terms from user request>" --view owners --view code
{command_name} query_explore --query "<terms from user request>" --owners-only
{command_name} query_explore --query "<terms from user request>" --callers-only
{command_name} query_explore --query "<terms from user request>" --serialization-paths-only
{command_name} query_explore --query "<terms from user request>" --view owners --view callers --view public_surface
```

Prefer `owners` to find implementation homes, `callers` for impact, `public_surface` before removing or renaming exported symbols, `serialization_paths` before changing model/storage fields, `docs_mentions` for documentation/examples, and `code` for working-set and targeted read ranges.

## Answering rules

Use graph output as evidence, not as permission to invent missing links. Cite node ids, source files, source fragments, or REQL rows when making factual claims. If the graph lacks enough evidence, say what is missing and read the specific files identified by REQL or by the user's exact target.

Prefer graph queries over broad repository scans, but still run targeted tests and inspect exact files before editing code.

## Raw tool limits

Do not use workspace-wide `rg`, recursive directory listings, `find`, `grep -R`, custom scanners, or ad hoc crawlers as the first way to understand the repository. Start with `query_context`, `query_explore`, `query_memories`, `query_graph`, `inspect`, or bounded raw REQL statements.

After REQL returns candidate paths, symbols, owners, source fragments, or line ranges, raw tools may be used for targeted verification: file-scoped `rg`, nearby line reads, exact user-named files, focused caller/import checks, and tests/debugging. If a raw search starts expanding across unrelated directories, stop and refine the REQL query instead.

## Code-Scoped Workflow

When the task asks for an implementation, bug fix, refactor, or behavior change:

1. Build a query from the user request's own feature, behavior, file, command, error, field, endpoint, API, or symbol terms; then run `{command_name} query_context --query "<terms from user request>" --code`.
2. For exact identifiers, legacy names, or one-off removals, try the plain shortest form first, for example `{command_name} query_context --query "graphify"`.
3. Use rendered files, symbols, and line ranges to choose the smallest files and spans to inspect.
4. When more source is required, read only the missing spans. Do not read entire files unless the line ranges are missing, ambiguous, stale, or tests/debugging require more context.
5. Run `{command_name} query_explore --query "<terms from user request>" --view owners --view code` when the context is noisy or you need owners and code slices before choosing files.
6. If the context still lacks enough code, retrieve exact locations with `{command_name} inspect --node-id NODE_ID --json` or `{command_name} query "RETRIEVE '<terms from user request>' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"`.
7. Read only the files and line ranges identified by rendered context, linked `SourceFragment` evidence, or raw REQL rows.
8. Modify existing owner symbols first. Do not add wrappers, override layers, new parallel services, or duplicate configuration until REQL shows that no suitable owner exists.
9. If the context is too broad or irrelevant, refine the query with concrete nouns from the request and rerun `query_context`, `query_explore`, or `query_graph` before broad raw search.

## Unused-Code Cleanup

When the user asks to find or remove unused code, start from REQL's deterministic cleanup findings instead of inventing a new repository scanner. Use the compiled graph to collect candidates, then verify each likely removal with targeted source inspection because some symbols can be public APIs, framework callbacks, entry points, reflection targets, or dynamically referenced plugin hooks.

Recommended sequence:

1. Ensure project status is active or bootstrap compile has completed.
2. Retrieve a natural-language context block with `{command_name} query_context --query "unused code dead code cleanup" --cleanup`.
3. List concrete findings with:

```bash
{command_name} query "FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class'] RETURN finding_type,severity,cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,line_start,reason,evidence_scope,confidence ORDER BY cleanup_priority LIMIT 100"
```

4. Expand provenance for ambiguous rows with:

```bash
{command_name} query "MATCH (s)-[:HAS_FINDING]->(f:StaticAnalysisFinding) RETURN s.type,s.name,s.qualified_name,f.finding_type,f.relative_path,f.line_start,f.reason LIMIT 100"
```

5. Inspect only the candidate files and nearby callers/importers. Use targeted symbol searches when needed to check entry points, tests, public exports, callbacks, dynamic `getattr`/reflection, and documentation examples.
6. Classify results separately: safe removals, likely dead but public/API-risk, and false positives. Treat `possibly_unused_function`, `possibly_unused_method`, and `possibly_unused_class` as local cleanup candidates, not whole-program proof.

Prefer high-priority `unused_variable` and `unused_import` findings for direct edits. Require stronger evidence before deleting public functions, methods, classes, scripts, generated adapters, CLI/MCP tools, or framework lifecycle methods.

## JSON mode

Use `--json` only when another tool or script needs structured fields, when you must programmatically consume keys such as `owner_candidates`, `working_set`, `targeted_reads`, or `cleanup_candidates`, or when rendered text is ambiguous:

```bash
{command_name} query_context --query "<terms from user request>" --code --json
{command_name} query_graph --query "<terms from user request>" --max-depth 2 --json
{command_name} query_explore --query "<terms from user request>" --view owners --view code --json
{command_name} query_memories --query "<terms from user request>" --limit 8 --json
{command_name} inspect --node-id NODE_ID --json
```
"""
    update_watch = f"""# REQL reference: updates, watch mode, cache, and deltas

Load this after modifying project files, when a watcher is running, or when the user asks about incremental behavior.

## After edits

If no `{command_name} project compile . --watch` process is already maintaining the workspace graph, run:

```bash
{command_name} project compile .
```

This refreshes only changed/deleted artifacts through the incremental cache and keeps the graph aligned with completed edits. If a watcher is already running, do not start another compile loop; query the maintained graph instead.

Before the final response for any task that changed files, confirm the graph update path: either the watcher already captured the edits, or run the one-shot compile above and report the result briefly.

## Watch mode

Use watch mode when the user asked for monitoring/continuous REQL updates or a long-running background process is appropriate:

```bash
{command_name} project compile . --watch
```

The watcher performs an initial cache check, then compiles only dirty or deleted artifacts. Use bounded options for scripts and tests:

```bash
{command_name} project compile . --watch --watch-iterations 1
{command_name} project compile . --watch --watch-interval 2 --watch-debounce 0.5
```

Ask before starting watch mode, manual `project update`, or `cache clear` unless the user explicitly requested that operation.

## Cache and deltas

Inspect cache state and recent compile changes with:

```bash
{command_name} cache status .
{command_name} query "DELTAS LIMIT 10"
{command_name} query "DELTAS WHERE id = 'delta:...' LIMIT 1" --json
```

Use `{command_name} project update .` only when the user explicitly asks for a manual incremental refresh of a previously compiled project. Prefer `project compile .` for bootstrap and normal after-edit refresh because it handles both first-time and incremental cases.
"""
    reports_exports = f"""# REQL reference: reports, graph analysis, exports, and MCP

Load this when the task needs project reports, graph analysis records, visual exports, JSON artifacts, or MCP wiring.

## Reports

Write project reports with:

```bash
{command_name} project report . --output reports/
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
reql-mcp --config conf.yaml --set project.id=agent-a --read-only
```

Use read-only mode for context retrieval. Use write tools such as compile/watch/hubs only with the same approval discipline as the CLI commands.
"""
    document_semantics = f"""# REQL reference: document structure

Load this only when compiling documents or changing document parsing/linking behavior.

## Default behavior

Project compile is deterministic. Code is parsed structurally. Markdown, plain text, and PDF artifacts are registered and fragmented as source context.

Compile projects with:

```bash
{command_name} project compile .
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
    points = (
        _command_preference(command_name=command_name, command_path=command_path, fallback_command=fallback_command),
        "When the user types `/reql`, use the generated `reql-project` skill or this REQL section before doing broad repository exploration.",
        "Start with `{command_name} project status .` to check whether this project has graph state.",
        (
            "For codebase questions, first query REQL when project status succeeds. Use "
            "`{command_name} query_context --query \"...\"`, `{command_name} query_explore --query \"...\"`, "
            "`{command_name} query_memories --query \"...\"`, `{command_name} query_graph --query \"...\"`, "
            "or focused REQL statements before raw source browsing."
        ),
        (
            "Use `{command_name} query \"PATH FROM TEXT \\\"A\\\" TO TEXT \\\"B\\\" DEPTH 5\"` or targeted `MATCH`/`FIND`/`EXPLAIN` "
            "statements for relationships and focused concepts when `query_graph` is not specific enough."
        ),
        (
            "For implementation, bug-fix, or refactor tasks, build a query from the user request's own feature, behavior, "
            "file, command, error, field, endpoint, API, or symbol terms, then use `{command_name} query_context --query \"<terms from user request>\" --code` "
            "as bounded code context. For exact identifiers or legacy names, try the shortest plain query first, such as `{command_name} query_context --query \"graphify\"`. Inspect only missing listed line ranges before whole files. Use `{command_name} query_explore --query \"<terms from user request>\" --view owners --view code` "
            "when dependency slices are needed or the context is noisy. If you need exact code before editing, use "
            "`{command_name} query_context --query \"<terms from user request>\" --code`, `{command_name} inspect --node-id NODE_ID --json`, "
            "or `{command_name} query \"RETRIEVE \\\"<terms from user request>\\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end\"`, "
            "then read only the local file spans that still need source-level inspection. Avoid adding wrappers, overrides, or parallel implementations before confirming "
            "there is no suitable owner in the graph."
        ),
        (
            "If status reports `Project not found`, immediately run `{command_name} project compile .` from the "
            "runtime workspace root before broad raw file exploration. If compile fails, report the failure briefly "
            "and continue with targeted raw file reads only as a fallback."
        ),
        "Document processing is part of `{command_name} project compile .` and runs locally.",
        (
            "If a watch process is already running for this workspace, do not start another compile/rebuild loop. "
            "Query the maintained graph instead."
        ),
        (
            "Dirty `.reql/` storage, cache, report, or export files are expected after compile/watch/update. Dirty REQL "
            "files are not a reason to skip REQL. Skip REQL only when the task is about stale/incorrect graph output "
            "or the user explicitly says not to use it."
        ),
        (
            "Prefer `{command_name} project compile . --watch` as monitor mode during active editing; it performs an "
            "initial cache check, monitors filesystem changes, and updates memory from detected dirty/deleted files "
            "instead of rebuilding on every interaction."
        ),
        (
            "Use REQL as the repository context index before raw repository scans. Do not run broad `rg`, recursive "
            "directory listings, custom scanners, `find`, `grep -R`, ad hoc Python/Node crawlers, or other repository-wide "
            "discovery commands to duplicate `query_context`, `query_explore`, `query_memories`, or `query_graph` "
            "results. Use raw tools only after REQL identifies candidate paths/symbols, when the user names an "
            "exact file/path, or when targeted verification, editing, debugging, or tests require local source reads."
        ),
        (
            "When raw tools are needed, keep them narrow: inspect the REQL-returned paths and line ranges first, use "
            "file-scoped `rg`/symbol searches rather than workspace-wide scans, and stop expanding once there is enough "
            "evidence to choose the owner file or edit location."
        ),
        (
            "For unused-code cleanup requests, query `StaticAnalysisFinding`/`FINDINGS` first, then validate "
            "candidates with targeted source reads or symbol searches before recommending removals."
        ),
        (
            "Use `{command_name} project exclude \"path/or/glob\"` only for explicit exclusions or obvious "
            "dependency/cache/build-output directories. Do not exclude framework/source roots needed for the task, "
            "do not use workspace-wide patterns such as `*`, `**`, or `**/*`, and do not run multiple exclude commands "
            "in parallel; pass all patterns in one command."
        ),
        (
            "Use `{command_name} query_context --query \"...\"`, `{command_name} query_memories --query \"...\"`, "
            "`{command_name} query_explore --query \"...\"`, or `{command_name} query_graph --query \"...\"` "
            "for bounded context before broad source exploration."
        ),
        (
            "Use `{command_name} query \"PROJECTS\"`, `{command_name} query \"ARTIFACTS LIMIT 20\"`, "
            "and `{command_name} query \"HUBS LIMIT 20\"` for graph inspection."
        ),
        (
            "Read `reports/GRAPH_REPORT.md` only for broad architecture review or when bounded REQL queries do not "
            "surface enough context. Prefer query outputs for normal codebase questions."
        ),
        (
            "After modifying project files, if no `{command_name} project compile . --watch` process was running, run "
            "`{command_name} project compile .` once before finishing so the REQL graph reflects the completed edits. "
            "If watch is running, let it update the graph and query the maintained graph instead."
        ),
        (
            "Before the final response for any task that changed files, confirm the graph update path: either the "
            "`{command_name} project compile . --watch` process already captured the edits, or run "
            "`{command_name} project compile .` once and report the result briefly."
        ),
        (
            "After project mode is selected, ask before long-running or non-bootstrap write/update operations such as "
            "`{command_name} project compile . --watch`, `{command_name} project update .`, "
            "or `{command_name} cache clear .`. Do not ask before the required one-shot `{command_name} project compile .` "
            "bootstrap after `Project not found`."
        ),
        "Keep the core memory workflow deterministic; document processing runs in the local compiler.",
    )
    body = _bullets(points, command_name=command_name)
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
description: Use REQL deterministic memory before broad repository exploration
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
    points = (
        "When the user types `/reql`, use the generated `reql-project` skill or this REQL rule before broad repository exploration.",
        *PROJECT_SKILL_SOURCE.rule_points,
        "If project status succeeds, query REQL before raw source browsing. Build a short query from the user request's own feature, behavior, file, command, error, field, endpoint, API, or symbol terms; preserve the user's language, identifiers, and exact errors. Use the simplest bounded command that can answer where to look, usually `{command_name} query_context --query \"<terms from user request>\"`; for exact names, a query such as `{command_name} query_context --query \"graphify\"` is preferred over a long synthetic query. Add `--code`, `--docs`, `--test`, or `--cleanup` only when useful. Use `{command_name} query_explore --query \"<terms from user request>\"`, `{command_name} query_memories --query \"<terms from user request>\"`, `{command_name} query_graph --query \"<terms from user request>\"`, `{command_name} inspect --node-id NODE_ID --json`, and `{command_name} query \"RETRIEVE \\\"<terms from user request>\\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end\"` when the first context query is not enough.",
        "Limit raw scans and large reads. Do not start with workspace-wide `rg`, recursive directory listings, `find`, `grep -R`, or custom crawlers when REQL can answer where to look. After REQL returns candidate files, symbols, or line ranges, use raw tools only in those paths or nearby spans, or for exact user-named files and test/debug verification.",
        "For code edits, make REQL return the smallest useful files and line ranges first with `query_context --query \"...\" --code`; use `--json` only when another tool or script genuinely needs structured fields such as `owner_candidates`, `working_set`, or `targeted_reads`.",
        "For unused-code cleanup, start with `{command_name} query \"FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class'] RETURN finding_type,severity,cleanup_priority,symbol_name,qualified_name,relative_path,line_start,reason\"` and validate removals with targeted source reads or symbol searches.",
        "Dirty `.reql/` storage, cache, report, or export files are expected after compile/watch/update and are not a reason to skip REQL.",
        "Read `reports/GRAPH_REPORT.md` only for broad architecture review or when bounded queries do not surface enough context.",
        PROJECT_SKILL_SOURCE.deterministic_requirement,
    )
    body = _bullets(points, command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    return f"""{section_start}
# REQL

Use REQL deterministic memory when {client_name} needs repository context before broad source exploration.

{body}
{section_end}
"""


def _scope(project: bool) -> str:
    return "project-local" if project else "user-level"


def _command_usage(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return (
        f"Prefer `{command_name}` for REQL commands. If it is not on `PATH`, use the installed shim at "
        f"`{command_path}`. If that is unavailable, use `{fallback_command}`."
    )


def _command_preference(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return f"Prefer `{command_name}` for REQL commands. If it is not on `PATH`, use the installed shim `{command_path}`. If that is unavailable, use `{fallback_command}`."


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
    preference = _bullets(
        (
            _command_preference(command_name=command_name, command_path=command_path, fallback_command=fallback_command),
            "`{command_name} project status .` for project mode",
            "`{command_name} query_context --query \"<terms from user request>\"`",
            "`{command_name} query_context --query \"<exact term>\"` for exact identifiers, legacy names, or one-off removals",
            "`{command_name} query_context --query \"<terms from user request>\" --code`",
            "`{command_name} query_context --query \"<terms from user request>\" --docs`",
            "`{command_name} query_context --query \"<terms from user request>\" --test`",
            "`{command_name} query_context --query \"<terms from user request>\" --cleanup`",
            "`{command_name} query_explore --query \"<terms from user request>\" --view owners --view code`",
            "`{command_name} query_memories --query \"<terms from user request>\"`",
            "`{command_name} query_graph --query \"<terms from user request>\"`",
            "`{command_name} query \"RETRIEVE \\\"<terms from user request>\\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end\"`",
        ),
        command_name=command_name,
    )
    return (
        "When REQL is invoked for repository context, use the generated `reql-project` skill. "
        "Before broad repository exploration, check whether REQL has graph context:\n\n"
        f"{preference}\n\n"
        f"If status reports `Project not found`, immediately run `{command_name} project compile .` from the runtime "
        "workspace root before broad raw file exploration. If compile fails, report the failure briefly and continue "
        f"with targeted raw file reads only as a fallback. Use REQL as the repository context index; do not run broad "
        "`rg`, recursive directory listings, `find`, `grep -R`, custom scanners, ad hoc crawlers, or other repository-wide discovery commands to duplicate "
        f"`{command_name} query_context`, `{command_name} query_explore`, `{command_name} query_memories`, or `{command_name} query_graph` "
        "results. For edits, start with `query_context --query \"...\" --code`; use `--json` only when another tool or script genuinely needs structured fields. Use "
        "`inspect --node-id` and `RETRIEVE ... RETURN id,type,text,score,relative_path,line_start,line_end` to get "
        "owner symbols, paths, and line ranges before opening source. Read specific files only after REQL "
        "identifies them or when exact edits, targeted debugging, or tests require them. Keep raw tool use file-scoped "
        f"or line-range-scoped once REQL has identified candidate locations. Use `{command_name} project exclude \"path/or/glob\"` only "
        "for explicit exclusions or obvious dependency/cache/build-output directories. Do not exclude framework/source "
        "roots needed for the task, do not use workspace-wide patterns such as `*`, `**`, or `**/*`, and do not run "
        "multiple exclude commands in parallel; pass all patterns in one command. During active edit sessions, start "
        f"`{command_name} project compile . --watch` only when monitoring/continuous updates are requested or a "
        "long-running process is appropriate. If a watch process is already running, do not start another compile/rebuild "
        f"loop. Query the maintained graph instead. After modifying project files, run `{command_name} project compile .` "
        "once before finishing unless a watch process was already running and updated the graph. Before the final response "
        "for any task that changed files, confirm the graph update path: either the watch process already captured the "
        f"edits, or run `{command_name} project compile .` once and report the result briefly. "
        f"{PROJECT_SKILL_SOURCE.deterministic_requirement}"
    )
