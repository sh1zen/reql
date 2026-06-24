# Reporting

REQL has one project Markdown reporting mode:

- `reql project report PATH --output reports/` produces project-level
  Markdown reports for a scanned or compiled project.

Project reports are deterministic and do not call an LLM. They are built from
the graph store plus project-local cache metadata. The generator reads project,
artifact, fragment, cache, compilation-run, delta, code-node, and call-edge
records. It also attempts deterministic community detection and hub analysis for
the selected project; if those analyses cannot run or have no data, the relevant
report sections show `No data yet`.

## Project Report Files

`project report` writes three files:

- `GRAPH_REPORT.md`
- `GRAPH_DELTAS.md`
- `CACHE_REPORT.md`

## GRAPH_REPORT.md

The main graph report includes:

- project id, root path, artifact counts, artifact type distribution, and
  language distribution;
- latest compilation run summary, including changed, skipped, deleted files,
  graph updates, and errors;
- cache summary, dirty artifact count, parser versions, chunking versions, and
  skipped file count;
- artifact ingestion details, including top artifacts by size, parser used,
  parser errors, partially readable artifacts, needs OCR, and needs parser;
- code graph summary for modules, classes, functions, methods, variables,
  imports, static-analysis findings, call edges, top symbols, and top cleanup
  candidates;
- communities with size, density, and salience when community data is available;
- `God nodes / hubs` with ranked hub score and reasons when hub analysis returns
  results;
- generic hub warnings when analysis detects overly generic high-degree nodes;
- memory health counts for stale, archived, and overly generic nodes, plus
  recommended actions.

Sections or lists with no matching data show `No data yet`.

## GRAPH_DELTAS.md

The delta report lists recent `GraphDelta` records for the project, including
run id, artifact id, created time, added/updated/archived node counts,
added/updated/archived edge counts, and affected node ids.

## CACHE_REPORT.md

The cache report includes:

- project id;
- cached, dirty, deleted, and skipped artifact counts;
- parser version counts;
- chunking version counts;
- up to 50 cache entries sorted by relative path, including status, compiled
  timestamp, and parser version.

It does not delete graph data or clear cache state; it only reports current
metadata.

## CLI

```bash
reql project report . --output reports/
reql project report . --output reports/ --json
```

The JSON form prints the generated file paths as `graph_report`,
`graph_deltas`, and `cache_report`.

## HTML Graph Export

`reql export --html` writes a standalone `graph.html` for the current store.
Use it when you want to inspect the stored graph visually without starting a
service.

The HTML export is for exploration and sharing. Use `reql export --json` when
you need the complete graph payload for backup, automation, or data exchange.
Use `reql export --html --json` when you want both the browser view and the
machine-readable graph files in the same output location. Large stores render a
bounded connected visual core so the browser view stays dense and responsive;
the layout is static and does not run automatic movement. Isolated records
remain available in the JSON export.


