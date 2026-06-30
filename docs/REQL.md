# REQL

REQL can query all graph node and edge types because storage uses generic
property-graph nodes and edges.

## First-Class Graph Commands

Project and artifact records:

```text
PROJECTS
ARTIFACTS LIMIT 20
ARTIFACTS WHERE artifact_type = "code" LIMIT 20
FRAGMENTS WHERE fragment_type = "heading" LIMIT 20
```

Code graph records:

```text
SYMBOLS TYPE Function WHERE name CONTAINS "compile" LIMIT 20
SYMBOLS TYPE Class,Method ORDER BY relative_path ASC LIMIT 50
FINDINGS WHERE finding_type = "unused_variable" LIMIT 20
```

Analysis records:

```text
COMMUNITIES LIMIT 20
HUBS LIMIT 20
HUBS TYPE Topic,Concept,Function LIMIT 10
```

Incremental compilation records:

```text
DELTAS LIMIT 10
CACHE STATUS
```

These commands support `WHERE`, `LIMIT`, and `ORDER BY` where applicable. Fields
resolve against node properties first-class, so artifact fields such as
`artifact_type` and symbol fields such as `name` can be filtered directly.

`WHERE` supports boolean composition with `AND`, `OR`, and `NOT`, comparison
operators (`=`, `!=`, `>`, `>=`, `<`, `<=`), list membership with `IN`, text
operators (`CONTAINS`, `STARTS WITH`, `ENDS WITH`, `LIKE`, `ILIKE`, `REGEX` or
`MATCHES`), ranges with `BETWEEN ... AND ...`, and null checks with `IS NULL`
or `IS NOT NULL`.

```text
FIND nodes TYPE Function WHERE text ILIKE "%payment%" LIMIT 10
FIND nodes TYPE Function WHERE name REGEX "^compile_.*" RETURN name,relative_path
FIND nodes WHERE salience BETWEEN 0.5 AND 1.0 AND source_url IS NULL LIMIT 20
FIND edges WHERE type IN ["CALLS","REFERENCES"] RETURN from_id,to_id,type LIMIT 50
```

## Code Graph Examples

List functions defined by source artifacts:

```text
MATCH (a:SourceArtifact)-[:DEFINES]->(f:Function)
RETURN a.path,f.name,f.start_line
```

List resolved function calls:

```text
MATCH (f:Function)-[:CALLS]->(g)
RETURN f.name,g.name
```

Find imported modules:

```text
FIND nodes TYPE Import RETURN relative_path,module,name,line LIMIT 50
```

Find classes and methods:

```text
FIND nodes TYPES Class,Method RETURN name,qualified_name,start_line,end_line LIMIT 50
```

Find compile-time static analysis findings:

```text
FINDINGS WHERE finding_type = "unused_import"
RETURN symbol_name,relative_path,line_start,cleanup_priority,reason
```

`FINDINGS` sorts active findings by numeric `cleanup_rank` by default, so direct
cleanup candidates appear before low-confidence or test-local noise. Explicit
`ORDER BY cleanup_priority` uses the same high/medium/low rank.

Generate a deterministic verification bundle for a specific finding before
opening files:

```text
VERIFY FINDING static-analysis-finding:abc123
```

The bundle returns the finding summary, a minimal graph-backed snippet, all
usage edges found for the associated symbol, checked scopes, risks, and a
recommended action. It uses compiled graph nodes and relations only; it does
not read source files on demand.

## Retrieval Query Examples

Use `query_context` when an agent needs compact next-action context. It is
informative by default for structure/documentation/existence questions and
renders one compact block with file/line references plus raw-query references
for extended research. Add `--code`, `--docs`, or `--test` to restrict the
same query to code, documentation/imported documents, or tests. Pass
`--cleanup` for dead-code and unused-symbol cleanup; cleanup output shows only
safe-remove `StaticAnalysisFinding` candidates before removals. Add
`--include-risky` to include public API, low-confidence, test-local, and
validate/risky candidates. Generic memory candidates, generated package docs,
and secondary test/docs paths are suppressed when a production owner is
available. Use `reql query_context --code --json` or `reql query_context
--cleanup --json` when another tool should consume the compact payload directly;
payloads include `query_mode`, `scopes`, `cleanup_filter`, `owner_candidates`,
`cleanup_candidates`, `working_set`, and `targeted_reads`.
In cleanup mode, `targeted_reads` includes per-finding read kinds such as
`import_block`, `symbol_body`, `finding_context`, `caller_ref`, `importer_ref`,
`doc_ref`, and `test_ref`, plus a sufficiency reason explaining whether the
listed reads are enough before opening broader source files.

Use `query_explore` when an agent needs the dependency chain for a concrete code
target before editing. It returns focused owners, callers, public surface,
serialization paths, docs mentions, and code working-set sections with usage
guidance, snippets, and targeted reads; pass repeated
`--view` flags such as `--view owners --view code`, or shortcuts such as `--owners-only` and
`--serialization-paths-only` to keep output small.

Use raw `reql query "..."` statements when you need deterministic rows instead
of a synthesized context block. `RETRIEVE` is the raw query form for ranked
source/code rows: it runs deterministic retrieval, includes connected source
texts by default, and returns rows that can be consumed by agents or scripts.
Ask for only the columns needed for the next decision, include `LIMIT`, and add
`relative_path`, `line_start`, `line_end`, `source_for`, `relation`, or
`direction` when provenance matters.

```text
RETRIEVE "office plant" LIMIT 8
RETRIEVE "compile document fragments" TOP 20 DEPTH 2 LIMIT 10
RETRIEVE "payment workflow" TYPE Function,Method NO SOURCES RETURN id,type,label,text,score
RETRIEVE "source provenance" RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start,line_end
```

## Graph Analysis Examples

List detected communities:

```text
COMMUNITIES LIMIT 20
```

List top hubs:

```text
HUBS LIMIT 20
```

Limit hubs to selected node types:

```text
HUBS TYPE Topic,Concept,Function LIMIT 10
```

Explain why a node was or was not scored as a hub:

```text
EXPLAIN HUB "node_id"
```

## Path And Match Examples

List artifact-defined functions:

```text
MATCH (a:SourceArtifact)-[:DEFINES]->(f:Function)
RETURN a.path,f.name,f.start_line
ORDER BY a.path ASC
```

List resolved calls:

```text
MATCH (f:Function)-[:CALLS]->(g)
RETURN f.name,g.name
```

Find a bounded path between text-selected concepts:

```text
PATH FROM TEXT "incremental compiler" TO TEXT "activation engine" DEPTH 5
```


