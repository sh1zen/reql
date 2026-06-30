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
        "Use this skill for REQL project mode and Agent Workspace mode. REQL is the local deterministic "
        "project graph. Agent Workspace mode is the planning layer for large projects when the coding-agent "
        "context is not enough, and it is still useful on small tasks to keep plans, choices, constraints, "
        "tasks, code targets, and implementation links from drifting."
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
        CommandExample('query_context --query "<terms from user request>" --cleanup', "safe-remove cleanup findings matching the query"),
        CommandExample('query_explore --query "<terms from user request>" --view owners --view code', "function-level owner/code slices for coding agents"),
        CommandExample('query_memories --query "<terms from user request>"', "compact ranked memory/source texts"),
        CommandExample('query_graph --query "<terms from user request>"', "seeds, edges, sources, and compact context"),
        CommandExample("inspect --node-id NODE_ID --json", "resolve a node id to location, sources, and neighbors"),
        CommandExample('query "RETRIEVE \\"<terms from user request>\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"', "source/code text with exact locations"),
        CommandExample('query "HUBS LIMIT 20"', "inspect useful graph hubs"),
        CommandExample("agent init", "create a private agent workspace, return its agent id, and register it on the shared bus"),
        CommandExample("agent bus --json", "read registered agents, shared messages, and handoffs"),
        CommandExample("agent status", "show whether the Agent Workspace exists and what it derives from"),
        CommandExample('agent session start "Focused implementation pass"', "start a current Agent Workspace session"),
        CommandExample('agent add "Read src/memory/cli.py and found argparse command routing"', "save a free-form operational note"),
        CommandExample('agent task add "Patch CLI output filters"', "create an open task"),
        CommandExample("agent task done AGENT_TASK_ID", "mark a task completed"),
        CommandExample('agent decision add "Keep working memory separate from .reql/memory.reql"', "record a technical decision"),
        CommandExample('agent finding add "agent map should only include touched files"', "record an observation about the code"),
        CommandExample("agent sync", "refresh Agent Workspace standard-node references after compile adds new files"),
        CommandExample("agent link AGENT_TASK_ID NODE_ID --relation touches", "connect agent work to a standard graph node"),
        CommandExample("agent link-task --file test-agent/context_savings.py", "connect the latest open task to a file by readable path"),
        CommandExample("agent link-many AGENT_TASK_ID NODE_ID OTHER_NODE_ID --relation touches", "connect one agent item to multiple graph nodes with one write"),
        CommandExample("agent batch --json agent-ops.json", "apply multiple agent notes, tasks, decisions, findings, and links with one workspace lock"),
        CommandExample("agent batch --task task=\"Patch CLI\" --decision decision=\"Use one lock\" --link '$task' implements '$decision'", "apply a small inline planning batch without a JSON file"),
        CommandExample("agent list --type task --status open --json", "list filtered working-memory items"),
        CommandExample('agent search "reset working graph" --json', "search the Agent Workspace"),
        CommandExample("agent show AGENT_TASK_ID --json", "inspect a working-memory node or relation"),
        CommandExample("agent map --session current --json", "summarize the current Agent Workspace session"),
        CommandExample('agent handoff "Worker done; review saved map"', "publish this agent's saved working map to the master bus"),
        CommandExample("agent export --json", "export the Agent Workspace for another coding agent"),
        CommandExample("agent reset", "discard agent-created working memory and re-derive from the current standard graph"),
    ),
    workflow_steps=(
        "Start every repository-context task with `{command_name} project status .` before raw repository reads or broad exploration.",
        (
            "If status reports an active project, use REQL before reading code: `--code` for implementation and fixes, "
            "`--cleanup` for cleanup or dead-code work, and `query_explore --view owners --view code` when context is noisy."
        ),
        (
            "For narrow exact-name removals or checks, `{command_name} query_context --query \"<exact term>\"` is often enough. "
            "Use returned files, symbols, "
            "file spans, and targeted reads first; read whole files only when the returned spans are missing or ambiguous. "
            "Use `{command_name} inspect --node-id NODE_ID --json` or `{command_name} query "
            "\"RETRIEVE \\\"<terms from user request>\\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end\"` "
            "to tighten source/code text with exact locations."
        ),
        (
            "Choose the query_context mode explicitly: no flag (`informative`) for project knowledge, structure, documents, "
            "existence checks, and architecture questions; add `--code`, `--docs`, or `--test` to limit the same mode to a precise section; "
            "`--cleanup` for safe-remove dead code, unused imports, unused variables, and removal candidates; add `--include-risky` only when you intentionally want public API, low-confidence, test-local, or validation-required candidates."
        ),
        (
            "If status reports `Project not found`, immediately run `{command_name} project compile .` from the "
            "runtime workspace root. Do not skip this step and do not continue with broad raw file exploration first. "
            "After compile succeeds, query REQL before reading many files."
        ),
        (
            "If `{command_name} project compile .` fails, report the failure briefly, then continue with targeted raw file reads only as a fallback."
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
            "Use REQL as the repository context index before raw repository scans. Do not run broad `rg`, recursive listings, "
            "custom scanners, `find`, `grep -R`, ad hoc crawlers, or other repository-wide discovery commands to duplicate "
            "`query_context`, `query_explore`, `query_memories`, or `query_graph` results. Raw tools are for exact user-named "
            "paths, REQL-returned candidates, targeted debugging, edits, and tests."
        ),
        (
            "Open raw source only when REQL returned the path, symbol, or span; the user explicitly named the file; or a "
            "test/debug failure requires that file. Prefer the line spans REQL returned over whole files. If more than three "
            "files or roughly 200 raw lines are needed before editing, stop expanding and refine the REQL query instead. "
            "Use file-scoped `rg`/symbol searches for targeted validation."
        ),
        (
            "Before editing, state the REQL query used and why each opened file is needed."
        ),
        (
            "For unused-code or dead-code requests, query REQL cleanup findings first. Remove directly only high-confidence "
            "local findings such as unused imports or variables. Treat public APIs, CLI/MCP commands, hooks, `to_dict`, "
            "serializers, tests, and re-exports as review-needed unless REQL gives stronger evidence. Read `references/query.md`, "
            "use `FINDINGS` or `StaticAnalysisFinding` queries for candidates, then validate candidates with targeted source reads or symbol searches."
        ),
        (
            "Do not add exclusions before the first bootstrap compile unless the user asked for them or the path is an obvious "
            "dependency/cache/build-output directory such as `node_modules/`, `vendor/`, `.tmp/`, `dist/`, or `build/`. "
            "Never exclude framework/source roots needed for the task."
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
        "Use `{command_name} agent status` before relying on working memory.",
        (
            "If the Agent Workspace is missing, run `{command_name} agent init` after the standard project graph "
            "exists. The command returns an `agent_id`, registers that private workspace on the shared bus, and makes it current for later `agent` commands. "
            "If the standard graph is stale or missing, use the project workflow first to compile/update it."
        ),
        (
            "Use Agent Workspace mode for planning on large projects when coding-agent context is too small; use it on small tasks when "
            "links between requirements, decisions, files, and tasks would otherwise get lost."
        ),
        (
            "Start a focused working session with `{command_name} agent session start \"...\"` when old agent history would make `agent map` noisy."
        ),
        (
            "For parallel workers, keep each worker on its own private memory with `{command_name} agent --agent AGENT_ID ...` or `REQL_AGENT_ID=AGENT_ID`. "
            "Use `{command_name} agent bus` to read shared messages and handoffs without merging private working graphs."
        ),
        (
            "Plan: use `{command_name} agent add \"...\"`, `agent decision add`, and `agent finding add` for compact info, choices, "
            "constraints, assumptions, and risks. Keep each entry short and factual."
        ),
        (
            "Task build: create tasks with `{command_name} agent task add \"...\"`; link each task to relevant plan items, decisions, "
            "findings, files, or symbols with `{command_name} agent link ID1 ID2 --relation depends_on|implements|touches|explains|related_to`, "
            "Use `{command_name} agent link-task --file path/to/file.py` when you know a file path but not its graph id. "
            "Use `{command_name} agent link-many TASK_ID ID1 ID2 --relation touches` for repeated links. "
            "For small planning batches, prefer `{command_name} agent batch --task task=\"...\" --decision decision=\"...\" --link '$task' implements '$decision'` over several separate writes."
        ),
        (
            "Quick review: run `{command_name} agent map --session current`, `{command_name} agent map`, or `{command_name} agent map --task TASK_ID` before editing to check open tasks, "
            "choices, constraints, touched files, and missing links."
        ),
        (
            "Code linking: before writing, attach planned code targets to tasks by linking task ids to REQL-returned file/symbol node ids. "
            "After `{command_name} project compile .` adds new files, run `{command_name} agent sync` before linking the new standard nodes. "
            "When useful, store a short `agent add` code note and link it to the task; assemble the implementation by following those links."
        ),
        (
            "Write: edit the project files, mark completed tasks with `{command_name} agent task done AGENT_TASK_ID`, and add findings or "
            "decisions only when they affect the remaining work."
        ),
        (
            "Link agent items to the standard graph whenever possible. Use ids returned by `query_context`, `query_graph`, "
            "`query_memories`, `inspect`, or `agent search`. If those ids are new standard nodes from files added by a recent "
            "`{command_name} project compile .`, run `{command_name} agent sync` before linking them. Then run "
            "`{command_name} agent link ID1 ID2 --relation touches|implements|depends_on|blocks|explains|derived_from|related_to|replaces|conflicts_with`, or "
            "`{command_name} agent link-task --file path/to/file.py` for task-to-file links without a manual id lookup. "
            "Use `{command_name} agent batch --json FILE` or inline `agent batch --task ... --link ...` when several agent writes should share one lock."
        ),
        (
            "Use `{command_name} agent map` after context loss or compaction to reconstruct the current working set. "
            "Use `{command_name} agent map --session current` when a current session exists. Treat `open_tasks`, `decisions`, `files`, `symbols`, and `relations` as the operational handoff."
        ),
        (
            "Use `{command_name} agent list --type ... --status ...` and `{command_name} agent search \"...\"` for focused retrieval. "
            "Use `--json` when another tool or agent needs structured fields. Add `--metadata` only when timestamps, source fields, storage paths, or stored metadata are needed."
        ),
        (
            "When a worker is done, run `{command_name} agent handoff \"summary\"` to publish the current saved working map to the master bus. "
            "The handoff includes open tasks, decisions, files, symbols, and essential relations."
        ),
        (
            "Run `{command_name} agent reset` only when intentionally discarding session working memory. Reset keeps the "
            "standard graph untouched and re-derives the Agent Workspace from the current `.reql/memory.reql`."
        ),
        (
            "Do not use the Agent Workspace as canonical project memory. Project facts belong in the standard graph via "
            "`project compile`; agent notes/tasks/decisions are temporary and session-scoped."
        ),
        (
            "Do not run multiple `reql agent` write commands in parallel. If a command reports that the Agent Workspace "
            "is busy, retry after the other command finishes."
        ),
    ),
    rule_points=(
        "Prefer `{command_name}`. If it is not on `PATH`, use `{command_path}`. If that is unavailable, use `{fallback_command}`.",
        "Start every repository-context task with `{command_name} project status .` before raw repository reads.",
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
            "When status reports an active project, retrieve bounded context with a query built from the user request's own "
            "feature, behavior, file, command, error, field, endpoint, API, or symbol terms. Use `--code` for implementation "
            "and fixes, `--cleanup` for cleanup or dead code, and `query_explore --view owners --view code` when results are noisy. "
            "Use `query_context`, `query_memories`, or `query_graph` before broad source discovery. Do not duplicate that context with broad "
            "`rg`, recursive directory listings, `find`, `grep -R`, custom scanners, ad hoc crawlers, or other repository-wide discovery commands."
        ),
        (
            "Limit raw scans and large reads. Open raw source only when REQL returned the path, symbol, or span; the user "
            "explicitly named the file; or a test/debug failure requires it. Prefer returned line ranges over whole files. "
            "If more than three files or roughly 200 raw lines are needed before editing, refine REQL instead of expanding raw reads."
        ),
        (
            "Choose the query_context mode explicitly: no flag (`informative`) for project knowledge, structure, documents, "
            "existence checks, and architecture questions; add `--code`, `--docs`, or `--test` to limit the same mode to a precise section; "
            "`--cleanup` for safe-remove dead code, unused imports, unused variables, and removal candidates; add `--include-risky` only when you intentionally want public API, low-confidence, test-local, or validation-required candidates."
        ),
        (
            "For code changes, start with `{command_name} query_context --query \"<terms from user request>\" --code` to identify the smallest existing "
            "functions, methods, classes, files, and line ranges. For exact-name cleanup or removal tasks, a plain "
            "`{command_name} query_context --query \"<exact term>\"` can be enough and should be tried before more complex queries. "
            "If you need more code before editing, use `inspect --node-id`, "
            "`RETRIEVE ... RETURN id,type,text,score,relative_path,line_start,line_end`, or `--json` only when structured fields are genuinely needed "
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
            "Before editing, state the REQL query used and why each opened file is needed."
        ),
        (
            "For unused-code cleanup, start with `{command_name} query \"FINDINGS WHERE finding_type IN "
            "[\\\"unused_variable\\\",\\\"unused_import\\\",\\\"possibly_unused_function\\\","
            "\\\"possibly_unused_method\\\",\\\"possibly_unused_class\\\",\\\"possibly_orphan_directory\\\"] RETURN finding_type,severity,"
            "cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,directory,file_count,files,line_start,reason\"`, then "
            "remove directly only high-confidence local findings such as unused imports or variables. Treat public APIs, "
            "CLI/MCP commands, hooks, `to_dict`, serializers, tests, and re-exports as review-needed unless REQL gives stronger evidence."
        ),
        (
            "After modifying project files, run `{command_name} project compile .` once before finishing unless a "
            "`{command_name} project compile . --watch` process was already running and updated the graph."
        ),
        (
            "After `{command_name} project compile .` adds new files, run `{command_name} agent sync` before linking "
            "Agent Workspace items to the new standard graph nodes."
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
        "Use `{command_name} agent status` to check Agent Workspace state.",
        "Use `{command_name} agent init` after the standard project graph exists; it returns an `agent_id`, registers a private workspace, and makes it current on the bus.",
        "Use `{command_name} agent session start \"...\"` to create a focused current session and keep old agent history out of `agent map --session current`.",
        "Use `{command_name} agent bus` to inspect the shared internal bus; use `{command_name} agent publish \"...\"` for short shared messages and `{command_name} agent handoff \"...\"` when a worker should return saved context to master.",
        (
            "Use Agent Workspace mode as a planning layer when project context exceeds the coding-agent window; on small tasks, use it "
            "to preserve links between requirements, choices, constraints, tasks, files, symbols, and implementation notes."
        ),
        (
            "Agent workflow: Plan with `agent add`, `agent decision add`, and `agent finding add`; task build with `agent task add`; "
            "quick review with `agent map --session current` or `agent map`; code linking with `agent link` or `agent link-many` from tasks to files/symbols/fragments/static findings/code notes; write the project code; "
            "then mark done tasks with `agent task done`."
        ),
        "After compile with new files, use `{command_name} agent sync` before linking new standard nodes.",
        "Use `{command_name} agent link` to connect tasks, decisions, findings, files, symbols, risks, plans, and code notes.",
        "Use `{command_name} agent batch --json FILE` or inline `{command_name} agent batch --task task=\"...\" --decision decision=\"...\" --link '$task' implements '$decision'` to add/link several Agent Workspace items with one lock instead of many serial commands.",
        "Use `{command_name} agent map` to recover context after compaction, review links, or assemble work from linked tasks.",
        "Use `{command_name} agent handoff \"summary\"` for master-facing completion payloads and `{command_name} agent export --json` for a compact private workspace export. Add `--metadata` only when a full metadata-bearing workspace dump is required.",
        "Use `{command_name} agent reset` only when intentionally discarding session-scoped working memory.",
        "Avoid parallel `reql agent` write commands; retry if the workspace is busy.",
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
    watch_mode = ""
    if source.name == PROJECT_SKILL_SOURCE.name:
        watch_mode = f"""
## Watch Mode

Use `{command_name} project compile . --watch` as monitor mode while the agent is editing code. Keep one watcher running in the workspace. It prints each poll, reports dirty/deleted artifact counts, and compiles only when changes are detected. Stop it with interrupt when the editing session is over.

Use `--watch-interval`, `--watch-debounce`, and `--watch-iterations` only when a bounded scripted run is needed.
"""
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
{watch_mode}
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
{command_name} agent link-task --file test-agent/context_savings.py
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
    points = (
        _command_preference(command_name=command_name, command_path=command_path, fallback_command=fallback_command),
        "When the user types `/reql`, use the generated `reql-agent` skill or this REQL section before doing broad repository exploration.",
        "Start with `{command_name} project status .` before raw repository reads to check whether this project has graph state.",
        (
            "For codebase questions, first query REQL when project status succeeds. Use `--code` for implementation and fixes, "
            "`--cleanup` for cleanup or dead code, `query_explore --view owners --view code` when context is noisy, "
            "or focused REQL statements for relationship checks."
        ),
        (
            "Use `{command_name} query \"PATH FROM TEXT \\\"A\\\" TO TEXT \\\"B\\\" DEPTH 5\"` or targeted `MATCH`/`FIND`/`EXPLAIN` "
            "statements for relationships and focused concepts when `query_graph` is not specific enough."
        ),
        (
            "For implementation, bug-fix, or refactor tasks, build a query from the user request's own feature, behavior, "
            "file, command, error, field, endpoint, API, or symbol terms, then use `{command_name} query_context --query \"<terms from user request>\" --code` "
            "as bounded code context. For exact identifiers or legacy names, try the shortest plain query first, such as `{command_name} query_context --query \"graphify\"`. Inspect only missing listed line ranges before whole files. Use `{command_name} query_explore --query \"<terms from user request>\" --view owners --view code` "
            "when dependency slices are needed or the context is noisy. If you need exact code before editing, use `{command_name} inspect --node-id NODE_ID --json` "
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
            "discovery commands to duplicate `query_context`, `query_explore`, `query_memories`, or `query_graph` results."
        ),
        (
            "Open raw source only when REQL returned the path, symbol, or span; the user explicitly named the file; or a "
            "test/debug failure requires it. Prefer returned line ranges over whole files. If more than three files or "
            "roughly 200 raw lines are needed before editing, refine the REQL query instead of expanding raw reads."
        ),
        (
            "Before editing, state the REQL query used and why each opened file is needed. For unused-code cleanup requests, "
            "query `StaticAnalysisFinding`/`FINDINGS` first. Remove directly only high-confidence local findings such as "
            "unused imports or variables; treat public APIs, CLI/MCP commands, hooks, `to_dict`, serializers, tests, and "
            "re-exports as review-needed unless REQL gives stronger evidence."
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
        "When the user types `/reql`, use the generated `reql-agent` skill or this REQL rule before broad repository exploration.",
        *PROJECT_SKILL_SOURCE.rule_points,
        "If project status succeeds, query REQL before raw source browsing. Build a short query from the user request's own feature, behavior, file, command, error, field, endpoint, API, or symbol terms; preserve the user's language, identifiers, and exact errors. Use `{command_name} query_context --query \"<terms from user request>\" --code` for implementation and fixes, `{command_name} query_context --query \"<terms from user request>\" --cleanup` for cleanup or dead code, and `{command_name} query_explore --query \"<terms from user request>\" --view owners --view code` when results are noisy. For exact names, a query such as `{command_name} query_context --query \"graphify\"` is preferred over a long synthetic query. Use `{command_name} query_memories --query \"<terms from user request>\"`, `{command_name} query_graph --query \"<terms from user request>\"`, `{command_name} inspect --node-id NODE_ID --json`, and `{command_name} query \"RETRIEVE \\\"<terms from user request>\\\" LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end\"` when the first context query is not enough.",
        "Limit raw scans and large reads. Do not start with workspace-wide `rg`, recursive directory listings, `find`, `grep -R`, or custom crawlers when REQL can answer where to look. After REQL returns candidate files, symbols, or line ranges, use raw tools only in those paths or nearby spans, or for exact user-named files and test/debug verification.",
        "Open raw source only when REQL returned the path, symbol, or span; the user explicitly named the file; or a test/debug failure requires it. Prefer returned line spans over whole files. If more than three files or roughly 200 raw lines are needed before editing, refine the REQL query instead of expanding raw reads.",
        "Before editing, state the REQL query used and why each opened file is needed. For code edits, make REQL return the smallest useful files and line ranges first with `query_context --query \"...\" --code`; use `--json` only when another tool or script genuinely needs structured fields such as `owner_candidates`, `working_set`, or `targeted_reads`.",
        "For unused-code cleanup, start with `{command_name} query \"FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class','possibly_orphan_directory'] RETURN finding_type,severity,cleanup_priority,symbol_name,qualified_name,relative_path,directory,file_count,files,line_start,reason\"`. Remove directly only high-confidence local findings such as unused imports or variables; treat public APIs, CLI/MCP commands, hooks, `to_dict`, serializers, tests, and re-exports as review-needed unless REQL gives stronger evidence.",
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
        "When REQL is invoked for repository context, use the generated `reql-agent` skill. "
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
        f"After compile with new files, run `{command_name} agent sync` before linking Agent Workspace items to the new standard nodes. "
        f"{PROJECT_SKILL_SOURCE.deterministic_requirement}"
    )
