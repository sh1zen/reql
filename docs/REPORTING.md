# Reporting

REQL has one project Markdown reporting mode:

- `reql project report PATH --output reports/` produces project-level
  reports for compiled repositories.

Project reports are deterministic and do not call an LLM. They read from the
graph store, cache metadata, compilation runs, graph deltas, artifact records,
code graph nodes, communities, hubs, and bridge signals.

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
- cache summary, parser versions, chunking versions, and skipped files;
- artifact ingestion details, including top artifacts by size, parser used,
  parser errors, partially readable artifacts, needs OCR, and needs parser;
- code graph summary for modules, classes, functions, methods, imports, call
  edges, and top symbols;
- top communities with size, density, and salience;
- top hubs with hub score and explanation;
- bridge signals from graph analysis;
- memory health signals and recommended actions.

Sections with no data show `No data yet`.

## GRAPH_DELTAS.md

The delta report lists recent `GraphDelta` records for the project, including
run id, artifact id, created time, added/updated/archived node counts,
added/updated/archived edge counts, and affected node ids.

## CACHE_REPORT.md

The cache report summarizes active and archived cache entries, dirty and deleted
artifact counts when the project path can be scanned, parser versions, chunking
versions, and recent cache entries.

## CLI

```bash
reql project report . --output reports/
reql project report . --output reports/ --json
```

The JSON form prints the generated file paths.

## HTML Graph Export

`reql export --html` writes a standalone `graph.html` for the current store.
Use it when you want to inspect the memory graph visually: important nodes,
relations, neighbors, communities, and source paths are available in one browser
view without starting a service.

The HTML export is for exploration and sharing. Use `reql export --json` when
you need the complete graph payload for backup, automation, or data exchange.
Use `reql export --html --json` when you want both the browser view and the
machine-readable graph files in the same output location. Large stores render a
bounded connected visual core so the browser view stays dense and responsive;
the layout is static and does not run automatic movement. Isolated records
remain available in the JSON export.


