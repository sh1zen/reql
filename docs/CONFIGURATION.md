# Configuration

REQL reads project settings from `conf.yaml`. The repository root `conf.yaml`
is the canonical sample copied by `reql config init`; it contains every
supported project config section. The runtime also has deterministic in-memory
defaults in `memory.config.models` for callers that construct a config directly.

Create a sample file:

```bash
reql config init
```

Inspect the effective configuration:

```bash
reql config show
reql --config path/to/conf.yaml config show
```

## Example

```yaml
project:
  id: default

scan:
  max_file_size_mb: 10
  include: []
  exclude:
    - "__pycache__/"
    - ".reql/"
    - ".cache/"
    - ".tmp/"
    - ".git"

compile:
  ingest_documents: true
  documents:
    - {"format": "markdown", "extensions": [".md", ".markdown"], "ingest": true}
    - {"format": "plain_text", "extensions": [".txt"], "filenames": ["LICENSE", "NOTICE"], "ingest": false}
    - {"format": "restructured_text", "extensions": [".rst"], "ingest": true}
    - {"format": "html", "extensions": [".html", ".htm"], "ingest": true}
    - {"format": "log", "extensions": [".log"], "ingest": false}
    - {"format": "pdf", "extensions": [".pdf"], "ingest": false}
    - {"format": "json", "extensions": [".json"], "ingest": true}
    - {"format": "toml", "extensions": [".toml"], "ingest": true}
    - {"format": "yaml", "extensions": [".yaml", ".yml"], "ingest": true}
    - {"format": "ini", "extensions": [".ini", ".cfg", ".conf"], "ingest": true}
    - {"format": "csv", "extensions": [".csv"], "ingest": true}
    - {"format": "tsv", "extensions": [".tsv"], "ingest": true}
    - {"format": "xml", "extensions": [".xml"], "ingest": true}
    - {"format": "ndjson", "extensions": [".ndjson"], "ingest": true}

cache:
  enabled: true
  fingerprint_strategy: sha256

analysis:
  enable_hubs: true
  enable_communities: true

reporting:
  output_dir: reports

diagnostics:
  enabled: false
  path: ""
```

## Behavior

- `scan.max_file_size_mb`, `scan.include`, and `scan.exclude` are used by
  project compile/update, watch mode, and cache status. Add project-specific
  ignored paths to `scan.exclude` in this YAML config.
- `compile.ingest_documents` controls project documentation parsing into
  `SourceFragment` nodes and document-code links. `compile.documents` is the
  supported document policy list; each item names a real `format`, the matching
  `extensions` and optional `filenames`, and whether to `ingest` it
  structurally. Tree-sitter is always used for supported code parsing. PDF
  parsing is attempted only when the `compile.documents` policy for `pdf` has
  `ingest: true`; text extraction requires the optional `pypdf` extra. During
  compile, ingested documents are processed locally by the deterministic
  document processor. It creates ranked `Concept` nodes, underlying `RawEvent`
  observations, `CO_OCCURS_WITH` term relations, and `REFERENCES` edges from
  document terms to code symbols when the same fragment explicitly mentions a
  compiled symbol.
- `cache.enabled = false` disables incremental skip decisions. Compilation
  still runs, but `.reql/artifact-cache.json` is not read or updated.
- `analysis.enable_hubs` is respected by the MCP `reql_hubs` tool. CLI REQL
  statements such as `HUBS` and `COMMUNITIES` are explicit analysis requests
  and run when invoked.
- `reporting.output_dir` is used by `project report` when `--output` is not
  provided.
- `diagnostics.enabled` controls structured JSONL performance logging.
  `diagnostics.path` is required when diagnostics are enabled.

REQL never downloads parser dependencies at runtime. Project compile and
document processing are deterministic local operations.

## Overrides

When a command receives a project path, for example `reql project compile PATH`
or `reql cache status [PATH]`, REQL searches upward for `conf.yaml` from that
project path. Commands with an optional path use the current working directory
when `PATH` is omitted. If no project config exists, REQL falls back to the
canonical `conf.yaml` shipped with the package or source checkout.

Precedence is:

1. `conf.yaml`, discovered upward from the project path or working path, or
   selected explicitly with `--config` / `REQL_CONFIG`.
2. `REQL_CONFIG_OVERRIDES`.
3. Explicit caller overrides such as CLI `--set` or MCP `config_overrides`.
4. Command-specific flags such as `--max-file-size-mb` and `--output`.

CLI override examples:

```bash
reql --set project.id=team-a config show
reql --set scan.max_file_size_mb=2 --set cache.enabled=false project compile .
```

Environment overrides:

```bash
REQL_CONFIG=./conf.yaml reql config show
REQL_CONFIG_OVERRIDES='{"project": {"id": "agent-a"}, "cache.enabled": false}' reql config show
REQL_CONFIG_OVERRIDES='project.id=agent-a; scan.max_file_size_mb=2' reql config show
```

MCP tools accept `config_path` and `config_overrides`, and `reql-mcp` also
accepts `--config` and repeated `--set` flags at startup.

## Loader

```python
from memory.config import load_config, load_effective_config

config = load_config("conf.yaml")
effective = load_effective_config("conf.yaml", overrides={"scan.max_file_size_mb": 2})
```

REQL uses a small built-in parser for the YAML subset used by `conf.yaml`; no
external YAML dependency is required.

Invalid sections, unknown options, or wrong value types raise a clear
`ConfigError`.
