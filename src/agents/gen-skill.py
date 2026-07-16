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
    title="REQL Project",
    description=(
        "A Python graph-native and storage-agnostic memory engine. Use when {platform_name} "
        "needs to implement, review, document, inspect, or extend a project with bounded "
        "repository graph context, or when {platform_name} needs persistent coding-agent "
        "working memory with `reql agent` for tasks, notes, decisions, findings, risks, "
        "plans, links, recovery, and export, while preserving deterministic core behavior."
    ),
    summary=(
        "Use this skill for REQL project mode and Agent Workspace mode. REQL is the local deterministic project graph; "
        "Agent Workspace is the optional planning layer for multi-step, cross-file, recoverable, or delegated work."
    ),
    command_examples=(
        CommandExample("project status .", "check whether this project has a compiled REQL graph"),
        CommandExample("project compile .", "bootstrap or refresh the graph, including once after edits"),
        CommandExample("project compile . --watch", "keep one incremental monitor running when continuous updates are appropriate"),
        CommandExample("project history . --limit 5", "inspect the latest immutable project revisions"),
        CommandExample("project diff .", "show file-level hash transitions in the latest revision"),
        CommandExample('locate "path/to/known/readme"', "resolve an exact project-relative path without semantic ranking"),
        CommandExample('query_context --query "<terms from user request>"', "compact informative context"),
        CommandExample('query_context --query "<terms from user request>" --code', "compact code-scoped context with files, symbols, and targeted reads"),
        CommandExample('query_context --query "<terms from user request>" --cleanup', "safe-remove cleanup findings matching the query"),
        CommandExample('query_explore --query "<terms from user request>" --view owners --view code', "function-level owner/code slices for coding agents"),
        CommandExample("inspect --node-id NODE_ID --json", "resolve a node id to location, sources, and neighbors"),
        CommandExample('query "RETRIEVE \\"<terms from user request>\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"', "source/code text with exact locations"),
        CommandExample("agent status", "show whether the Agent Workspace exists and what it derives from"),
        CommandExample("agent init", "create a private workspace when multi-step work needs durable planning context"),
        CommandExample('agent session start "Focused implementation pass"', "start a current Agent Workspace session"),
        CommandExample("agent batch --task task=\"Patch CLI\" --decision decision=\"Use one lock\" --link '$task' implements '$decision'", "apply a small inline planning batch without a JSON file"),
        CommandExample("agent map --session current --json", "summarize the current Agent Workspace session"),
        CommandExample("agent sync", "refresh standard graph references after compile adds files"),
        CommandExample("agent export --json", "export the Agent Workspace for another coding agent"),
    ),
    workflow_steps=(
        (
            "Start with `{command_name} project status .`. If it reports `Project not found`, immediately run "
            "`{command_name} project compile .`; if compile fails, report it and fall back to targeted raw reads."
        ),
        (
            "For an active graph, query before browsing source. Use informative `query_context` for project knowledge; "
            "use `--code`, `--docs`, or `--test` for scoped context, `--cleanup` for removal candidates, and "
            "`query_explore --view owners --view code` when dependency context is noisy."
        ),
        (
            "Use a lightweight path for a clear one-file or exact-symbol edit: status, one focused query, then the returned "
            "file spans or targeted reads. A short `{command_name} query_context --query \"<exact term>\"` is preferred "
            "over a synthetic query. Do not initialize Agent Workspace for this case."
        ),
        (
            "For code edits, treat `owner_candidates`, `read_plan`, `change_chain`, `contracts`, `impact`, `targeted_reads`, "
            "and `test_targets` as evidence. Use `inspect` or bounded `RETRIEVE` rows only to fill missing locations; use the "
            "repository's documented tests and state the REQL query plus why each raw file is opened."
        ),
        (
            "Use REQL as the repository context index. Do not run broad `rg`, recursive listings, `find`, `grep -R`, or custom "
            "crawlers to rediscover its results. Raw tools are for user-named paths, REQL-returned paths, and targeted test/debug work; "
            "prefer file-scoped `rg`/symbol searches and refine the query before opening more than three files or about 200 lines."
        ),
        (
            "For unused-code or dead-code requests, start with cleanup findings. Remove only high-confidence local findings; "
            "treat public APIs, hooks, serializers, tests, re-exports, and CLI/MCP commands as review-needed. Detailed query shapes live in `references/query.md`."
        ),
        (
            "Add exclusions only when explicit or clearly dependency/cache/build output. Never exclude framework/source roots, "
            "never use workspace-wide patterns, and pass all patterns to one `{command_name} project exclude` call."
        ),
        (
            "Use at most one `{command_name} project compile . --watch` process, and ask before starting it or other long-running/non-bootstrap writes. "
            "The required first-time one-shot compile after `Project not found` does not need confirmation. If a watcher already exists, query its maintained graph; "
            "otherwise run one `{command_name} project compile .` after edits."
        ),
        (
            "Before the final response for changed files, confirm and report that the watcher captured them or that the one-shot compile completed. "
            "Document processing is local and part of compile; REQL remains optional and deterministic without mandatory LLM calls."
        ),
        (
            "Use Agent Workspace only for multi-step, cross-file, ambiguous, recoverable, or delegated work. Check `agent status`; initialize it only after "
            "the standard graph exists, and start a focused session when older history would make the map noisy."
        ),
        (
            "Plan: use compact notes, decisions, and findings. Task build: create executable tasks, preferably with one `agent batch`. "
            "Quick review: inspect `agent map --session current`. Code linking: connect tasks to REQL-returned files or symbols. "
            "Write: edit the project files and mark completed tasks done."
        ),
        (
            "After `{command_name} project compile .` adds new files, run `{command_name} agent sync` before linking them. "
            "Use `agent map` after context loss, private workspaces per parallel worker, and never run Agent Workspace writes in parallel."
        ),
        (
            "Use `agent handoff` to return saved context, `agent export --json` for transfer, and `agent reset` only to intentionally discard temporary memory. "
            "Canonical project facts belong in the standard graph, not Agent Workspace."
        ),
    ),
    rule_points=(
        "Prefer `{command_name}`. If it is not on `PATH`, use `{command_path}`. If that is unavailable, use `{fallback_command}`.",
        (
            "Start with `{command_name} project status .`. If status reports `Project not found`, immediately run "
            "`{command_name} project compile .`; if compile fails, report it and use targeted raw reads."
        ),
        (
            "For an active graph, build a short query from the user's feature, behavior, file, command, error, field, endpoint, API, or symbol terms. "
            "Use `query_context --code` for implementation, `--cleanup` for dead code, and `query_explore --view owners --view code` when context is noisy."
        ),
        (
            "Do not duplicate REQL context with broad `rg`, recursive listings, `find`, `grep -R`, or custom crawlers. Read only user-named or "
            "REQL-returned paths and spans; refine the query before expanding beyond three files or about 200 lines."
        ),
        (
            "For code edits, use returned owners, read plans, change chains, file spans, targeted reads, impact, and tests as evidence. "
            "State the query and why each raw file is needed; use bounded `inspect` or `RETRIEVE` only for missing locations."
        ),
        (
            "For cleanup, remove only high-confidence local findings and review public APIs, hooks, serializers, tests, re-exports, and CLI/MCP commands. "
            "For exclusions, never exclude source roots or use workspace-wide patterns; make one bounded exclude call."
        ),
        (
            "Use one `{command_name} project compile . --watch` process at most and ask before starting long-running/non-bootstrap writes, but not before the required "
            "one-shot bootstrap compile. After edits, let the watcher update the graph or run one compile; confirm the update before the final response."
        ),
        (
            "Use Agent Workspace only when multi-step work needs persistent planning or handoff. Follow plan, task build, quick review, code linking, and write; "
            "sync after compile adds files, hand off when done, and keep project facts in the standard graph."
        ),
        "Detailed bootstrap, query, update/watch, reporting, document, and Agent Workspace procedures live in the generated `references/` files; load only the reference relevant to the task.",
    ),
    deterministic_requirement="Keep REQL optional and deterministic; document processing runs in the local compiler, and Agent Workspace operations stay local and separate from the standard REQL graph.",
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
    scope = _scope(project)
    usage = _command_usage(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    examples = _format_examples(source, command_name)
    workflow = _numbered(source.workflow_steps, command_name=command_name)
    workflow_heading = "Required Project and Agent Workspace Workflow" if source.name == PROJECT_SKILL_SOURCE.name else "Required Agent Workflow"
    reference_routing = _reference_routing(source.name)
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

## {workflow_heading}

{workflow}
## Reference Routing

{reference_routing}

## Ground Rules

- Treat REQL as a bounded context index, not as a replacement for exact source edits or tests.
- Cite files, node ids, source fragments, or REQL rows when using graph-derived facts.
- If the graph lacks evidence for a claim, say that and inspect targeted files instead of inventing relationships.
- Keep the deterministic core path usable without LLM calls.

Installed for: {platform_name} ({scope}).
"""


def _reference_routing(source_name: str) -> str:
    return "\n".join(
        [
            "- Read `references/bootstrap.md` when checking project state, compiling for the first time, handling exclusions, or deciding whether to fall back to raw files.",
            "- Read `references/query.md` when answering a repository question from an existing REQL graph or choosing between `query_context`, `query_memories`, `query_graph`, and REQL statements.",
            "- Read `references/update-watch.md` after modifying files, when a watcher is running, or when cache/delta state matters.",
            "- Read `references/reports-exports.md` when generating reports, exporting graph artifacts, inspecting hubs/communities, or wiring MCP.",
            "- Read `references/document-semantics.md` only when the task involves document ingestion or local document processing.",
            "- Read `references/agent-workspace.md` when using `reql agent` commands, recovering working context, linking agent tasks to standard graph nodes, or exporting/resetting the Agent Workspace.",
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
    openai_yaml = """display_name: REQL Project
short_description: Use REQL graph context and agent memory.
default_prompt: Use REQL to inspect this project, compile it if needed, answer from bounded graph context, and persist working-memory tasks, decisions, and findings when useful.
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

Project commands search for `reql.conf` from the target path upward and join its lists with protected internal defaults. Use `--config path/to/reql.conf` or repeated global `--set section.option=value` only when the task needs a different configuration. The core compile path must remain deterministic and usable without model providers.

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

- Informative: use no mode flag for project knowledge, structure, documents, architecture, existence checks, and "is there anything like X" questions. Prefer `{command_name} query_context --query "<terms from user request>"`, `{command_name} query_memories --query "<terms from user request>"`, or `{command_name} query_graph --query "<terms from user request>" --max-depth 2`. Use the rendered files, line references, source evidence, graph links, and embedded raw-query research references.
- Scope filters: use `--code`, `--docs`, and `--test` with informative or cleanup queries when the user asks for a precise section. They restrict results to code symbols/source, documentation/imported documents, or tests.
- Cleanup: use `--cleanup` for safe-remove dead code, unused imports, unused variables, and removal candidates. Start with `{command_name} query_context --query "<terms from user request>" --cleanup` or the `FINDINGS` query below, then remove only confirmed candidates. Add `--include-risky` only when you intentionally want public API, low-confidence, test-local, or validation-required candidates.

## Dependency Exploration

Use `query_explore` to reduce broad manual scanning when you already know the task target but need the surrounding dependency chain:

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

Use graph output as evidence, not as permission to invent missing links. Cite node ids, source files, source fragments, or REQL rows when making factual claims. If the graph lacks enough evidence, say what is missing and read the specific files identified by REQL or by the user's exact target.

Prefer graph queries over broad repository scans, but still run targeted tests and inspect exact files before editing code.

## Raw tool limits

Do not use workspace-wide `rg`, recursive directory listings, `find`, `grep -R`, custom scanners, or ad hoc crawlers as the first way to understand the repository. Start with `query_context`, `query_explore`, `query_memories`, `query_graph`, `inspect`, or bounded raw REQL statements.

After REQL returns candidate paths, symbols, owners, source fragments, or line ranges, raw tools may be used for targeted verification: file-scoped `rg`, nearby line reads, exact user-named files, focused caller/import checks, and tests/debugging. If a raw search starts expanding across unrelated directories, stop and refine the REQL query instead.

## Code-Scoped Workflow

When the task asks for an implementation, bug fix, refactor, or behavior change:

1. Build a query from the user request's own feature, behavior, file, command, error, field, endpoint, API, or symbol terms; then run `{command_name} query_context --query "<terms from user request>" --code`.
2. For exact identifiers, legacy names, or one-off removals, try the plain shortest form first, for example `{command_name} query_context --query "graphify"`.
3. For a clear one-file or exact-symbol edit, stop after the first sufficient bounded result or targeted file read; skip Agent Workspace, reference docs, and extra graph views unless ambiguity appears.
4. Use rendered files, symbols, line ranges, and structured fields such as `owner_candidates`, `read_plan`, `change_chain`, `contracts`, `impact`, `targeted_reads`, and `test_targets` to choose the smallest files and spans to inspect.
5. When more source is required, read only the missing spans. Do not read entire files unless the line ranges are missing, ambiguous, stale, or tests/debugging require more context.
6. Run `{command_name} query_explore --query "<terms from user request>" --view owners --view code` when the context is noisy or you need owners and code slices before choosing files.
7. If the context still lacks enough code, retrieve exact locations with `{command_name} inspect --node-id NODE_ID --json` or `{command_name} query "RETRIEVE '<terms from user request>' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"`.
8. Read only the files and line ranges identified by rendered context, linked `SourceFragment` evidence, or raw REQL rows.
9. Modify existing owner symbols first. Do not add wrappers, override layers, new parallel services, or duplicate configuration until REQL shows that no suitable owner exists.
10. If the context is too broad or irrelevant, refine the query with concrete nouns from the request and rerun `query_context`, `query_explore`, or `query_graph` before broad raw search.

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
reql-mcp --config reql.conf --set project.id=agent-a --read-only
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

Load this when using `reql agent` to persist coding-agent working memory, recover context after compaction, link operational tasks to graph nodes, or export/reset session-scoped memory.

## Command resolution

{usage}

## Purpose

`{command_name} agent` writes to a private project-local graph for the current agent. CLI-created worker memories live under `.reql/agents/AGENT_ID.reql`; the Python API follows the bus current agent when one exists, and otherwise falls back to the compatible master workspace at `.reql/agent.reql`. The standard project graph remains `.reql/memory.reql` and is not modified by agent notes, tasks, decisions, findings, plans, risks, or links.

All agents share an internal bus at `.reql/agent-bus.reql`. The bus stores registered agents, short shared messages, and handoffs. Use it to coordinate workers without merging their private working graphs.

Use the standard graph for stable project facts. Use Agent Workspace mode as the planning layer when a project is too large for the coding-agent context window. It is also useful on small tasks when requirements, files, choices, and implementation steps need explicit links.

Store only durable operational memory:

- files and symbols read during this session;
- decisions and why they were made;
- findings, assumptions, risks, and blockers;
- tasks, plans, completed work, and follow-up work;
- links between tasks, decisions, findings, code notes, files, symbols, and standard graph nodes.

## Bootstrap

Check state:

```bash
{command_name} agent status
```

Initialize from the current standard graph:

```bash
{command_name} agent init
```

`agent init` returns an `agent_id`, registers that private memory on the shared bus, and makes it current for later `agent` commands in the same project. A simple single-agent run does not need extra flags. Parallel workers can reuse their id explicitly:

```bash
{command_name} agent --agent AGENT_ID status
REQL_AGENT_ID=AGENT_ID {command_name} agent map --session current
```

If the standard graph does not exist or is stale, use the `reql-agent` skill first:

```bash
{command_name} project status .
{command_name} project compile .
```

After `{command_name} project compile .` adds new files, run `{command_name} agent sync` before linking Agent Workspace items to the new standard nodes.

## Required Agent Workflow

Keep entries short and factual. Prefer one useful sentence over repeated status prose.

### 1. Plan

Add information, choices, constraints, assumptions, risks, and blockers:

```bash
{command_name} agent bus
{command_name} agent session start "Focused implementation pass"
{command_name} agent add "Read src/memory/cli.py; argparse owns command routing"
{command_name} agent decision add "Keep .reql/agent.reql separate from .reql/memory.reql"
{command_name} agent finding add "agent list should not dump standard relations"
```

### 2. Task Build

Create the task list and link tasks to plan elements:

```bash
{command_name} agent task add "Patch agent map to show only touched files"
{command_name} agent link AGENT_TASK_ID AGENT_DECISION_ID --relation implements
{command_name} agent link AGENT_TASK_ID AGENT_FINDING_ID --relation depends_on
{command_name} agent link-many AGENT_TASK_ID STANDARD_FILE_ID STANDARD_SYMBOL_ID --relation touches
{command_name} agent batch --task task="Patch agent map" --decision decision="Use one workspace lock" --link '$task' implements '$decision'
```

Use task descriptions as executable work items, not summaries. Each task should point to the plan item, constraint, file, or symbol that explains it.
When several items or links are known at once, prefer `{command_name} agent batch --json FILE` or inline `agent batch --task ... --link ...` so the Agent Workspace takes one lock.

### 3. Quick Review

Before editing, check that the map has enough structure to recover the work:

```bash
{command_name} agent map
{command_name} agent map --session current
{command_name} agent map --task AGENT_TASK_ID
```

Review open tasks, choices, constraints, touched files, and missing links. Use `--session current` when old agent history is not relevant. Add only the missing facts.

### 4. Code Linking

After REQL returns file or symbol ids, link planned code targets to tasks. If `{command_name} project compile .` created new file or symbol nodes, run `{command_name} agent sync` before linking those new standard nodes. Use this to assemble the implementation from the task graph before writing:

```bash
{command_name} agent sync
{command_name} agent link AGENT_TASK_ID STANDARD_FILE_OR_SYMBOL_ID --relation touches
{command_name} agent link-task --task TASK_ID --file test-agent/context_savings.py
{command_name} agent add "Code note: update _agent_workspace_resource to describe plan/task/review/link/write flow"
{command_name} agent link AGENT_TASK_ID AGENT_NOTE_ID --relation implements
{command_name} agent link-many AGENT_TASK_ID STANDARD_FILE_ID STANDARD_SYMBOL_ID --relation touches
```

Code notes are for short target-specific intent, not long code dumps. The actual code belongs in project files.

### 5. Write

Edit the project, then update task state:

```bash
{command_name} agent task done AGENT_TASK_ID
```

Add new decisions or findings only when they change remaining work.

### 6. Handoff To Master

When a worker has saved the facts the master needs, publish a handoff:

```bash
{command_name} agent handoff "Worker finished parser review"
{command_name} agent bus --json
```

The handoff snapshots the current saved map: open tasks, decisions, files, symbols, and essential relations. The master can read it from the bus and decide the next step without opening the worker's private store directly.

## Link Agent Items

Use ids returned by `query_context`, `query_graph`, `query_memories`, `inspect`, `agent list`, or `agent search`. After compile with new files, run sync before linking new standard nodes:

```bash
{command_name} agent sync
{command_name} agent link AGENT_TASK_ID STANDARD_NODE_ID --relation touches
{command_name} agent link AGENT_TASK_ID AGENT_DECISION_ID --relation implements
{command_name} agent link AGENT_FINDING_ID STANDARD_SYMBOL_ID --relation explains
{command_name} agent link-many AGENT_TASK_ID STANDARD_FILE_ID STANDARD_SYMBOL_ID --relation touches
```

Supported relation types:

- `depends_on`
- `blocks`
- `implements`
- `touches`
- `explains`
- `derived_from`
- `related_to`
- `replaces`
- `conflicts_with`

## Recover Context

Use the map after context loss, thread compaction, or a long pause:

```bash
{command_name} agent map
{command_name} agent map --session current
{command_name} agent map --json
```

The map is intentionally operational and compact: open tasks, decisions, files directly touched by agent relations, symbols, and essential agent-created relations. It should not dump findings, fragments, metadata, or every derived standard file unless metadata is explicitly requested.

Search and inspect:

```bash
{command_name} agent list --type task --status open --json
{command_name} agent search "reset working graph" --json
{command_name} agent search "reset working graph" --json --metadata
{command_name} agent show AGENT_TASK_ID --json
{command_name} agent bus --json
```

`agent list` keeps relation output focused on agent-created relations and, when node filters are present, relations connected to the listed nodes.

## Export and Reset

Export for another coding agent:

```bash
{command_name} agent handoff "Summary for master"
{command_name} agent export --json
{command_name} agent export --json --metadata
```

Reset only when intentionally discarding session-scoped working memory:

```bash
{command_name} agent reset
```

Reset recreates `.reql/agent.reql` from the current standard graph and deletes agent-created notes/tasks/decisions/findings/links. It does not modify `.reql/memory.reql`.

## Concurrency

Do not run multiple `reql agent` write commands in parallel. If a command reports that the Agent Workspace is busy, retry after the other command finishes. Read commands retry briefly; write commands fail fast with a clear busy message to avoid hidden hangs.

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
    body = _bullets(
        _embedded_rule_points(),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return f"""{section_start}
# REQL

Use REQL deterministic memory when {client_name} needs repository context before broad source exploration.

{body}
{section_end}
"""


def _scope(project: bool) -> str:
    return "project-local" if project else "user-level"


def _embedded_rule_points() -> tuple[str, ...]:
    return (
        "When the user types `/reql`, use the generated `reql-agent` skill or this concise REQL rule before broad repository exploration.",
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
