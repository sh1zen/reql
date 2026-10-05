# Engineering coordination

REQL has three distinct owners: the compiled graph owns repository facts,
shared work owns engineering intent and execution, and private agent stores own
scratch notes and session lifecycle. Neither work records nor checkpoints copy
source nodes. Repository-relative paths connect intent to source-backed retrieval;
the current source and tests remain authoritative.

## Work records

`.reql/agent-dashboard.reql` stores `work_record` nodes alongside the existing
roster and public messages. `memory.services.coordination.CoordinationStore` owns
their validation, transactions, reconciliation, retrieval and projections.
Agent Workspace supplies the current agent/session provenance. Construction does
not open stores or create files. Reads of absent work storage return empty context.

Kinds are goal, task, decision, constraint, failure, question, change, observation
and checkpoint. A record has stable identity, content, status, rationale, evidence
paths, optional workstream, parent, dependencies, conflicts, replacements,
importance, next action, revision and origin/latest agent/session attribution.
Only kind and content are required for simple work. Decisions and failures require
a rationale. Files are repository-relative evidence pointers, never inputs to
filesystem operations.

MCP exposes `reql_work_context`, `reql_work_overview` and the optional write tool
`reql_work_record`; read-only mode hides/rejects writes. Direct writes carry explicit
agent/session provenance and use the same revision semantics, without creating
private scratch. MCP-only clients save explicit checkpoints at session boundaries.

The default identity normalizes content within kind/workstream/parent. `--key` supplies
a stable name when wording will evolve. Repeating an identical request is a
no-op. Changed records require `--id` and the observed `--revision`. Stale writes
fail with reconciliation guidance. The block-store writer lease and transaction
make validation, revision checks, replacement and updates atomic across processes.

Statuses are active, open, in_progress, blocked, done, resolved, superseded,
invalidated and abandoned. Blocked/obsolete transitions and reopening require
a rationale. `--supersedes` atomically retires the old record with replacement id
and reason. Up to eight prior snapshots retain the earlier content, assumptions,
status and provenance. Use `agent show ID --json` to inspect them.

Relations are stored once, as ids on their owning record:

- `parent` groups outcomes under tasks/goals without a second plan document.
- `depends_on` links prerequisites and assumptions across workstreams.
- `contradicts` exposes a conflict from either endpoint.
- `supersedes` records a change of direction and its cause.
- `summarizes` links a checkpoint to its evidence without treating that work as
  a prerequisite for ending the session.

Each record accepts up to 64 evidence paths. Targets must exist; self links and cycles in hierarchy, dependencies and
replacement are rejected. Each relation accepts at most 32 targets. Repeated CLI
flags add multiple targets; the Python API accepts empty lists to clear reconciled
links. Replacement does not silently rewrite dependent tasks: they require review.

## Planning and continuation

The plan is the current connected work, not a manually synchronized TODO file.
Execution is projected on read: an open task is ready when its task/goal/question
prerequisites are complete; waiting prerequisites block it; obsolete dependencies
and explicit conflicts require review. Conflicting dependency assumptions also
require review. Completing a task with those unresolved conditions is rejected.
REQL does not infer human acceptance, implementation correctness or task completion
from a commit or a passing test. The agent records the observed result.

`agent finish` automatically saves a shared checkpoint with the supplied outcome,
up to 64 session evidence paths, up to 32 work links (unfinished work first), and next actions from unfinished
tasks. It then closes the session and releases private scratch when no activity
remains. Finishing never completes tasks. A later agent reads shared work without
selecting the previous agent or restoring its private store, then claims/reconciles
the same ids under its own session. Revision snapshots explain the handoff.

Legacy private tasks, decisions, findings and rejected approaches migrate with
their ids and provenance on resume or lifecycle cleanup. Private/directed notes
remain private and disposable. Already-deleted private history cannot be recovered.
Existing `agent task add/done/list`, `agent reject`, dashboards, search and exports
continue to work against the shared owner. Reset affects private scratch only.

## Retrieval and overview

`query_context` still performs source retrieval using lexical/identifier seeds,
bounded code-graph expansion and chain-aware ranking. Its shared Python/CLI/MCP
service also retrieves up to eight relevant engineering records. The schema-v2
payload adds `engineering_context` with a separate content revision; repository
graph revision and confidence retain their existing source-only meaning.

`project context` reads work independently of the canonical graph. Its anchors
are normalized text with inverse-frequency weighting, evidence path overlap,
explicit task id or workstream. Importance, decision/constraint/failure kinds,
unresolved status and day-level recency adjust ranking. Two causal hops add
prerequisites, parent goals, conflicts, replacements, task discoveries and
dependent work. Parent goals do not fan out into unrelated siblings. Expansion
is bounded to 80 candidates and 12 next-hop nodes; output defaults to eight records.
Returned scores and reasons explain inclusion. Text output is capped at 6,000
characters, while JSON preserves full selected records and omission counts.

Default retrieval excludes superseded/invalidated/abandoned records. Current
replacements expose the old ids and replacement rationale; `--history` admits
obsolete records for audit. Claims are scoped engineering evidence, not verified
source facts. Unrelated recency alone cannot anchor a focused query. Retrieval
requires no embeddings, provider calls or additional dependencies; it cannot
discover unrecorded intent or paraphrases with no lexical/structural connection.

`project overview` combines a compact source-backed architecture with goals,
current decisions/constraints, workstreams, completed and active outcomes,
blockers/questions, failures, checkpoints and ready next steps. Section counts
show omitted work; `--limit` adjusts the default five rows. `--details` includes
full architecture and legacy history. `project explain` drills into architecture;
`project context --task ID` and `agent show ID --json` drill into work and evidence.

## Normal coding workflow

```bash
reql project status
reql project overview
reql query_context --query "serializer" --code
reql project context --file src/codec.py
reql agent init --name "Serializer contract"
reql agent record goal "Preserve serializer round trips" --key serializer
reql agent record task "Verify empty payload" --parent GOAL_ID --file src/codec.py
reql agent record decision "Keep empty fields" --parent TASK_ID --why "Consumers distinguish absent and empty"
reql agent show TASK_ID --json
reql agent record task "Verify empty payload" --id TASK_ID --revision 1 --status in_progress
reql agent task done TASK_ID "Codec regression and consumer checks passed"
reql agent finish "Empty fields preserved; malformed-input question remains"
```

Use the ids/revisions actually returned by your commands. Record at natural
boundaries: a decision, failed approach, substantial tested change, blocker or
interruption. Query and reconcile before creating another record. Do not persist
secrets, personal data, source dumps or repetitive tool output. Use private notes
for scratch, shared records for knowledge a later agent needs.

## Retention

Live work and referenced causal evidence survive session retention. Finish keeps
the latest 200 unreferenced terminal outcomes; superseded evidence referenced by
current decisions remains. Pruning never leaves dangling relations. Eight snapshots
bound per-record revision history. `retention.agent_sessions` continues to bound
legacy public messages and roster entries only, even at zero. Long-lived active
or referenced records can accumulate and should be reconciled when obsolete.

The generated skill and all assistant rules come from `src/agents/gen-skill.py`;
`src/agents/install.py` writes platform skills, routed references, rules and hooks.
Hooks provide guidance; they do not parse transcripts or guess intent. The main
skill teaches bounded discovery and durable reconciliation, while the routed
workspace reference documents the exact commands and lifecycle.
