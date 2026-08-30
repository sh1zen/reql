# Configuration

REQL keeps runtime defaults in the packaged
`src/memory/config/conf.yaml`. Projects use `reql.conf`; `reql config init`
creates a project template with that name. Every project setting can override
the corresponding internal value. By default, `scan.include` and
`scan.exclude` are joined without duplicates; `scan.ignore_defaults: true`
makes the project lists replace the defaults instead.

Create a sample file:

```bash
reql config init
```

Inspect the effective configuration:

```bash
reql config show
reql --config path/to/reql.conf config show
```

## Example

```yaml
project:
  id: default

scan:
  max_file_size_mb: 10
  use_gitignore: false
  ignore_defaults: false
  include: []
  exclude:
    - .tmp/
    - .pytest-tmp/

compile:
  ingest_documents: true
  documents:
    markdown: true
    pdf: false

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
  project compile/update, watch mode, and cache status. `scan.include` retains
  glob matching. `scan.exclude` uses one strict, scope-aware grammar: a literal
  relative path, optionally prefixed with `./` and optionally followed by `/`;
  `*suffix` is allowed only as the final path segment. A rule prefixed with
  `./` is anchored to the directory containing that `reql.conf`; an unprefixed
  rule can match at any depth below it. Thus `./dir`, `./file.py`, and
  `./*.generated` match only directly in the config directory, while `dir`,
  `file.py`, and `*.generated` match at every depth. `dir/*.generated` matches
  files anywhere inside every directory named `dir`; `./dir/*generated`
  matches anywhere inside only the config directory's direct `dir` child. A trailing
  slash does not change matching. Absolute paths, `..`, empty segments,
  backslashes, and every other glob form are configuration errors.
- `scan.ignore_defaults` controls list merging. With `false` (the default),
  project `include` and `exclude` entries are appended to the internal lists
  and deduplicated. With `true`, both internal lists and the scanner's internal
  ignore matcher are discarded, so the project lists replace them completely.
  In this mode, add operational paths such as `.reql/` and `.git/` explicitly
  when they must remain excluded.
- `scan.use_gitignore: true` loads the `.gitignore` in the compiled project
  root. Its rules are joined with protected internal ignores and the effective
  `scan.exclude` list; protected and local exclusions cannot be negated by
  `.gitignore`. Blank lines, comments, directory rules,
  anchored rules, globs, and `!` negations are supported. The option affects
  compile, update, watch mode, and cache status.
- `compile.ingest_documents` is the global document-ingestion switch.
  `compile.documents` maps format names to booleans; every built-in format is
  disabled by default, and a project `reql.conf` only needs to list the formats
  it wants to change. The protected `compile.document_formats` registry in the
  internal `conf.yaml` maps each format to its `extensions` and optional
  `filenames`. Projects may override these definitions too. Tree-sitter is
  always used for supported code parsing. PDF
  parsing is attempted only when `compile.documents.pdf` is `true`; text
  extraction requires the optional `pypdf` extra. During
  compile, ingested documents are processed locally by the deterministic
  document processor. It creates ranked `Concept` nodes, underlying `RawEvent`
  observations, `CO_OCCURS_WITH` term relations, and `REFERENCES` edges from
  document terms to code symbols when the same fragment explicitly mentions a
  compiled symbol.
  The effective compile and scan settings are represented internally by one
  immutable `CompilationOptions` object, including normalized typed
  `DocumentPolicy` entries. The same object drives scanning, parsing, watch
  mode, and the cache options hash.
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
or `reql cache status [PATH]`, REQL searches upward for the nearest `reql.conf`
from that project path. Commands with an optional path use the current working
directory when `PATH` is omitted. If no project config exists, only the
packaged internal `conf.yaml` is used. A project-level `conf.yaml` is never
discovered.

When a parent project is compiled, REQL also discovers `reql.conf` files in
visited subdirectories. Their `scan.exclude` entries are added only for that
subtree and are resolved relative to the directory containing the nested file.
Directories already excluded by a parent rule are not visited. Other settings
in a nested config are validated, but affect a command only when that subpath is
itself used as the command root.

For example, a subdirectory can contain only the exclusions it needs:

```yaml
# services/search/reql.conf
scan:
  exclude:
    - ./generated/
    - fixtures/*.snapshot
```

Passing `--config path/to/reql.conf` explicitly selects that project config.

Precedence is:

1. Packaged internal `conf.yaml` defaults.
2. The nearest project `reql.conf`, or the file selected with `--config` /
   `REQL_CONFIG`.
3. `REQL_CONFIG_OVERRIDES`.
4. Explicit caller overrides such as CLI `--set` or MCP `config_overrides`.
5. Command-specific flags such as `--max-file-size-mb` and `--output`.

At every merge step, scalar values use the later value. `scan.include` and
`scan.exclude` are joined in order and deduplicated unless
`scan.ignore_defaults` is `true`, in which case they are replaced.
`compile.documents` is merged by format, so a local file can toggle one entry
without repeating the others. `compile.document_formats` is the separate,
overridable format-to-extension registry. `compile.ingest_documents` is
directly overridable.

CLI override examples:

```bash
reql --set project.id=team-a config show
reql --set scan.max_file_size_mb=2 --set cache.enabled=false project compile .
```

Environment overrides:

```bash
REQL_CONFIG=./reql.conf reql config show
REQL_CONFIG_OVERRIDES='{"project": {"id": "agent-a"}, "cache.enabled": false}' reql config show
REQL_CONFIG_OVERRIDES='project.id=agent-a; scan.max_file_size_mb=2' reql config show
```

MCP tools accept `config_path` and `config_overrides`, and `reql-mcp` also
accepts `--config` and repeated `--set` flags at startup.

## Loader

```python
from memory.config import load_config, load_effective_config

config = load_config("reql.conf")
effective = load_effective_config("reql.conf", overrides={"scan.max_file_size_mb": 2})
```

REQL uses a small built-in parser for the YAML subset used by both config files; no
external YAML dependency is required.

Invalid sections, unknown options, or wrong value types raise a clear
`ConfigError`.
