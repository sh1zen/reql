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
Both `done` and `resolved` task states run these completion checks.
REQL does not infer human acceptance, implementation correctness or task completion
from a commit or a passing test. The agent records the observed result.

`agent finish` automatically saves a shared checkpoint with the supplied outcome,
up to 64 session evidence paths, up to 32 work links (unfinished work first), and next actions from unfinished
tasks. It then closes the session and releases private scratch when no activity
remains. Plain finishing never completes tasks. An explicit assigned-task finish,
`agent finish SUMMARY --task ID --revision N`, first completes that task and clears
its next action. The same revision, prerequisite, conflict and obsolete-dependency
checks apply as for `agent record`; rejected completion leaves the session open.
A later agent reads shared work without
selecting the previous agent or restoring its private store, then claims/reconciles
the same ids under its own session. Revision snapshots explain the handoff.
Finish returns the saved `checkpoint` receipt (`id`, `revision`, `status`)
in Python and CLI JSON, and prints its id/revision in text. Batch finish includes
the same receipt under `finish.checkpoint`. Recover with `project context --task
CHECKPOINT_ID`: linked records reflect current status, not an old plan copy.
The receipt comes directly from the checkpoint write without opening the canonical graph.

For managed parallel work, one coordinator can own shared records while workers
use its graph-backed briefs and return observed files, checks and blockers.
Workers need no private sessions for this mode. The coordinator sets assigned
tasks to `in_progress`, reviews actual changes, runs integration checks, then
accepts verified outcomes in dependency order through `agent batch`. Task results
attribute worker evidence in the rationale; the accepting coordinator remains the
record's author. Each write still checks the observed revision, prerequisites,
conflicts and obsolete dependencies. Failure retains partial receipts for recovery.
The host owns worker dispatch and source-file ownership. REQL revision checks
apply to work records; source changes require normal review and verification.

For independently assigned work, `agent init --name NAME --task ID --revision N` claims the
observed revision and returns the new revision with bounded dependency context.
Taking in-progress work under a different agent or session advances its revision.
Retries by its current owner/session remain idempotent.
Reuse a supplied graph-backed working set; do not repeat discovery or copy the
same result into observations, public notes and checkpoints. Workers run focused
checks; the coordinator runs integration checks after their changes join.
The coordinator also refreshes the source graph once after parallel integration.
When the documented full suite includes worker tests, run it once after integration.
Repeat focused checks for failures, changed dependencies or coverage outside that suite.
Keep any checks required by repository instructions. A supplied passing baseline
is reusable only when its tested source and dependencies remain unchanged.
Assigned-session setup already returns scoped context. Use `project context
--task ID` when that handoff lacks required evidence. Focused text retains complete
content, rationale, next actions and evidence paths within its budget; inspect a marked incomplete record
with `agent show ID --json`. That command also includes revision history.
Do not reload every historical record for a complete handoff.

For linked plans, `agent batch --name NAME --file .reql/plan.json --finish SUMMARY`
applies 1–40 record objects through the normal record owner. Use Python API field
names, including `record_id`, `expected_revision`, `rationale` and `next_action`.
Creation needs `kind` and `content`. Updates can send only `record_id`,
`expected_revision` and changed fields. The store resolves omitted text from the
current record inside the same write transaction; callers need not copy it back.
An optional `ref` names an earlier result; `{"ref":"name"}` can supply a later
record's `parent`, `record_id` or a relation-list target. These aliases exist only
within the request. They do not create another persistent plan representation.
The CLI accepts UTF-8 files inside the project, or stdin with `--file -`.
Keep plan input under `.reql/` to exclude operational text from the source graph.
Prefer piping the JSON array to the existing stdin input in one command. The
generated skill includes a PowerShell here-string example; Bash can use a quoted
here-document. Pass receipt ids and the finish checkpoint to the next agent,
instead of writing and rereading another receipt or handoff file. Keep separate
evidence files when they contain additional information, such as complete test logs.

Each record is atomic; the complete batch is **not** atomic. On failure, earlier
writes remain committed, subsequent writes stop, and no finish is attempted.
The error includes committed ids/revisions and the failed entry index. A finish
failure also preserves receipts. Reconcile those records before retrying.
Receipts describe each operation's result; later operations can change its revision.
Use the returned ids and revisions for handoffs and subsequent updates instead of
querying all records to confirm each successful write. A concurrent change or stale
revision requires reading the affected current record before retrying. Plain single
`agent record` writes also return id, kind, status and revision without echoing
the submitted content. Explicit `--json` retains the complete saved record and history.
The same partial-update contract applies to single `agent record` writes,
for example `agent record --id TASK_ID --revision N --status blocked --why REASON`.
To replace content, supply kind and content as positional arguments.
Plain `agent show ID` returns current outcome text within the budget,
with explicit truncation when the complete record does not fit.

Private stores contain sessions, `private_note` and `external_note` records.
Durable work belongs in shared records, including `observation` and `failure`.
Completed-store cleanup uses lifecycle locks and removes private scratch.
`agent task add/done/list`, `agent reject`, dashboards, search and exports
use the shared work owner. `agent reset` discards private sessions and notes;
shared engineering records remain available.

## Retrieval and overview

`query_context` performs source retrieval using lexical/identifier seeds,
bounded code-graph expansion and chain-aware ranking. Its shared Python/CLI/MCP
service also retrieves up to eight relevant engineering records. The schema-v2
payload includes `engineering_context` with a separate content revision; repository
graph revision and confidence retain their existing source-only meaning.
Code queries use their selected working-set files as work evidence anchors.
Their attached work text is capped at 2,400 characters, with marked omissions;
JSON preserves all selected records. Use scoped `project context` for the fuller
handoff rather than repeatedly retrieving code to recover missing operational detail.

`project context` reads work independently of the canonical graph. Its anchors
are normalized text with inverse-frequency weighting, evidence path overlap,
explicit task id or workstream. An explicit `--task` selects causal scope rather
than adding a lexical boost: unrelated matches cannot crowd out its prerequisites,
even if many share its file or query terms. Importance, decision/constraint/failure kinds,
unresolved status and day-level recency adjust ranking. Current file-scoped work
outranks completed summaries for that file, even when a summary has stronger text
overlap. An explicit task skips lexical processing of backlog prose. Two causal hops add
prerequisites, parent goals, conflicts and replacements. Direct anchors also add
their dependent work and child tasks. Reached prerequisites/parents do not fan
out into sibling consumers; query that shared contract directly for its impact.
This keeps assigned-task recovery scoped without changing readiness checks. Reverse
relations are indexed once per read. Prerequisites and conflicts rank ahead of
consumers and historical summaries. Expansion admits at most 40 distinct records
in the first hop and 80 overall, with 12 next-hop nodes. A large set of direct
lexical/file matches does not disable causal expansion. Output defaults to eight records.
`expansion_truncated` and its text warning identify candidate/frontier limits.
`total_matches` and `omitted` count known matches, including discovered records
outside that allowance; a limited expansion cannot count undiscovered causal work.
Returned scores and reasons explain inclusion. Text output is capped at 6,000
characters. With `--task`, the requested record comes first and selected causal
records retain complete text while space permits. Other text views keep short
summaries. Both modes mark truncated fields, omitted records/files and the exact
`agent show ID --json` command for full evidence. Coordination identifiers are
not silently cut. JSON preserves full selected records and omission counts.
Text combines repeated relation targets with their roles, such as
`waiting+depends: ID`. Exactly equal fields can refer to an earlier field in the
same response by row/section, keeping each record's id and revision visible. A shorter or budget-cut
preview cannot replace more complete evidence. Truncation warnings remain explicit;
different text, including differences after a common prefix, stays distinct.
Repeated attribution can use `(owner1=agent:...)` on the first emitted row and
`(owner1)` on later rows in that response. Keys are local to the response and
retain exact agent identity; commands and structured data always use the full id.

Default retrieval excludes superseded/invalidated/abandoned records. Current
replacements expose the old ids and replacement rationale. Recovery follows
replacement chains even when the current decision shares no query terms or files.
An obsolete requested id resolves to its current replacement and exposes work
still dependent on the requested assumption. Task dependencies are never rewired
automatically: obsolete assumptions still require review and block completion.
`--history` admits
obsolete records for audit. Claims are scoped engineering evidence, not verified
source facts. Unrelated recency alone cannot anchor a focused query. Retrieval
requires no embeddings, provider calls or additional dependencies; it cannot
discover unrecorded intent or paraphrases with no lexical/structural connection.

`project overview` combines a compact source-backed architecture with goals,
current decisions/constraints, workstreams, completed and active outcomes,
blockers/questions, failures, checkpoints and ready next steps. Section counts
show omitted work; `--limit` adjusts the default five rows. `--details` expands
the architecture and inspects available private sessions for per-agent details;
JSON also includes their retained notes. `project explain` drills into architecture;
`project context --task ID` and `agent show ID --json` drill into work and evidence.
When the same complete snapshot appears in several sections, later sections retain
its id, revision and execution state and point to the first displayed section.
Changed dependency state is rendered again even if the record revision is unchanged.
Budget-cut rows cannot become references. Omission counts include records excluded
by `--limit` as well as those that do not fit the section's text budget.

## Normal coding workflow

```bash
reql project status
reql query_context --query "serializer" --code
reql agent init --name "Serializer contract"
reql agent record goal "Preserve serializer round trips" --key serializer
reql agent record task "Verify empty payload" --parent GOAL_ID --file src/codec.py --status in_progress
reql agent record decision "Keep empty fields" --parent TASK_ID --why "Consumers distinguish absent and empty"
reql agent finish "Empty fields preserved; codec and consumer checks passed; malformed-input question remains" --task TASK_ID --revision N
```

Use the ids/revisions actually returned by your commands for `GOAL_ID`, `TASK_ID` and `N`.
The example assumes the source query supplies sufficient context. Use `project overview`
for unknown direction or scoped `project context` for missing operational evidence.
Record at natural
boundaries: a decision, failed approach, substantial tested change, blocker or
interruption. Query and reconcile before creating another record. Do not persist
secrets, personal data, source dumps or repetitive tool output. Use private notes
for scratch, shared records for knowledge a later agent needs.

## Retention

Live work and referenced causal evidence survive session retention. Finish keeps
the latest 200 unreferenced terminal outcomes; superseded evidence referenced by
current decisions remains. Pruning never leaves dangling relations. Eight snapshots
bound per-record revision history. `retention.agent_sessions` bounds
public messages and roster entries only, even at zero. Long-lived active
or referenced records can accumulate and should be reconciled when obsolete.

For installed skills, routed references and hooks, see
[Assistant installs](CLI.md#assistant-installs).
