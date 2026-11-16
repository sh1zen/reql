# Artifact Ingestion

Artifact ingestion turns registered `SourceArtifact` files into parsed
`SourceFragment` nodes with provenance and document-specific relations.

## Supported Inputs

- Markdown: headings, paragraphs, lists, fenced code blocks, inline links, and
  simple pipe tables.
- Plain text and text-like artifacts: paragraph chunks with preserved line
  ranges and character offsets, including unknown UTF-8 text files. Recognized
  code formats use the code parser instead.
- PDF: optional `pypdf` extraction when available. If it is not installed,
  metadata is recorded and the artifact is marked as needing a parser.
- Code: registered Tree-sitter parsers create technical graph facts and source
  fragments. [Code analysis](CODE_ANALYSIS.md#language-support) owns the language
  list, grammar requirements and extraction details.
- Image and video files are skipped during project compilation and are not parsed,
  registered, or compiled.

Document classification includes `readme.txt`, changelog and FAQ files,
WordPress readme headings (`=== Plugin ===`, `== Description ==`), and
extensionless files with Markdown structure. These sections participate in
`query_context --docs`. Configured formats and document policy are described in
[Configuration](CONFIGURATION.md#behavior).

## Parser Interface

Concrete parsers live under `memory.document_ingestion.formats` and inherit
`BaseDocumentParser`, the common document parser layer. Format-specific modules
are organized by parser name, for example
`memory.document_ingestion.formats.markdown.MarkdownParser`.

Parsers implement:

```text
supports(artifact) -> bool
parse(artifact, content) -> DocumentParseResult
```

`DocumentParseResult` includes title, parser metadata, fragments, discovered
links, tables, parser errors, parser name, and parser version. Links and tables
remain `DocumentLink` and `DocumentTable` objects through parsing and graph
compilation; they become dictionaries only at serialization boundaries.

`DocumentFragment` records:

- fragment type;
- text;
- line and byte-offset ranges;
- page number when relevant;
- section path;
- content hash;
- confidence;
- parser metadata.

Code parsers similarly return one authoritative `CodeParseResult.symbols`
collection. The `classes`, `functions`, and `methods` views are derived from it,
so parser stages cannot maintain contradictory symbol lists.

## Graph Output

The compiler creates or updates deterministic `SourceFragment` nodes. Fragments
are deduplicated by artifact and structural hash, so changed text updates an
existing same-position fragment instead of duplicating it.

Text documents in compile mode (`markdown`, `text`, `config`, `data`, and
unknown UTF-8 text) are compiled structurally into fragments and document
relations. Document text remains a lower-level source layer used for
provenance, code linking, and query context around the code graph.

Relations:

- `SourceArtifact -CONTAINS_FRAGMENT-> SourceFragment`
- `SourceArtifact -HAS_SECTION-> SourceFragment` for headings
- `SourceFragment -LINKS_TO-> URI`
- `SourceFragment -HAS_CODE_BLOCK-> SourceFragment`
- `SourceFragment -DERIVED_FROM-> SourceArtifact`
- `SourceArtifact -DEFINES-> Module/Function/Class/Method`
- `Function/Method -CALLS-> Function/Method` for resolvable code calls
- `SourceFragment -REFERENCES-> Module/Function/Class/Method/...` when a
  document explicitly mentions a high-signal compiled code target such as a
  qualified module, function, class, method, route, endpoint, schema, filename,
  or path-like symbol. Document-code linking is capped at 8 targets per
  fragment and ignores generic headings or low-signal short terms such as
  `usage`, `config`, `api`, `project`, and `architecture`.

Parser metadata and parser errors are stored on both the artifact and fragment
properties where relevant. Parser failures are reported in `CompilationRun`
errors and do not stop the full project compile.

## Fallbacks

PDF parsing is controlled by `compile.documents.pdf`. When that toggle is
`true`, PDF parsing uses optional dependencies.
Missing dependencies do not become mandatory runtime requirements. The compiler
records metadata-only fragments and marks artifact properties such as
`needs_parser` or `partially_readable` where appropriate. Image and video files
are skipped before parsing.

Tree-sitter is required for supported code parsing.
Unrecognized code-like artifacts compile conservatively as project
structure/source artifacts only and are not reported as parser failures.
