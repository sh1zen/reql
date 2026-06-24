# Code Analysis

Code analysis is the `compile` mode for projects. It builds a deterministic
technical graph from compile-time scanning and AST/static analysis.

Compile mode does not use generic topic extraction as the primary graph model
for code. Technical edges are deterministic, have `confidence=1.0`, and carry
provenance in edge properties: `source_id`, `target_id`, `type`,
`confidence`, `source_file`, `line_start`, `line_end`, `extractor`,
`evidence`, `created_at`, `updated_at`, `mode=compile`,
`is_semantic=false`, and `is_technical=true`.

`compile_project` uses the project scanner, parsers, artifact cache, and static
code/document graph compiler. Non-code text documents remain structural
`SourceFragment` records for provenance and code-context linking.
Projects may contain multiple programming languages in the same compile run.
Language detection, Tree-sitter grammar selection, and extractor dispatch happen
per `SourceArtifact`, so a Python file, TypeScript file, Solidity contract, and
JavaScript file in the same project each produce their own language-specific
module, symbols, imports, and calls.

## Language Support

REQL recognizes code artifacts for common programming languages including
Python, TypeScript/JavaScript, Solidity, Go, Rust, Java, C/C++, Ruby, C#, Kotlin, Scala,
PHP, Swift, Lua, Zig, PowerShell, Elixir, Julia, Verilog, Fortran, Bash, SQL,
Terraform, Apex, Pascal, Razor, and related file extensions. Recognition means
the scanner registers the file as a `code` artifact with a normalized language
instead of treating it as plain text or unknown content.

## Parsers

Recognized code languages use Tree-sitter AST grammars exclusively. Python,
JavaScript, TypeScript, and Solidity use language-specific adapters, extracting imports,
class/function/method/meaningful-variable symbols, comments, docstrings for
Python, decorators/modifiers, type hints where available, and call targets from the AST,
including the nearest function or method owner for each call. Solidity extraction
emits contract, interface, library, struct, enum, event, constructor, modifier,
receive/fallback, and function symbols; resolves relative imports and Foundry
remappings from `remappings.txt` and `foundry.toml`; links resolvable imports to
real `File` nodes; records `EMITS` for events, `USES` for `using` directives,
`INSTANTIATES` for `new` expressions, and filters Solidity builtins such as
`require`, `assert`, `revert`, `msg`, `abi`, `block`, and `tx`.
Other recognized languages use the reusable Tree-sitter profile extractor with
a profile declared on each language class. The generic engine owns only the
traversal and result assembly; language-specific AST node names, import forms,
call forms, builtins, variables, and macro-style declarations live in the
corresponding `memory.code_analysis.languages.<language>` module.
These profiles emit modules, classes/types, functions, methods, local variables
where the grammar exposes clear writes, imports/includes and idiomatic import
calls such as `require`, `source`, `Import-Module`, and `@import`, comments,
source fragments, call targets, and read/write/return/raise references. This
keeps each non-Python/Solidity language independently extensible while sharing
the common Tree-sitter graph assembly code.

The Tree-sitter extraction layer is structured directly under
`memory.code_analysis`. `TreeSitterCodeParser` and shared AST primitives live in
`memory.code_analysis.base`, while
language cataloging, extension detection, aliases, and Tree-sitter grammar
loading live in `memory.code_analysis.catalog`. `TreeSitterExtractorBase` owns shared state
and result assembly, but it does not contain language walkers or fallback AST
profiles. Language-specific classes live one file per language under
`memory.code_analysis.languages` and are registered in the extractor
factory. Languages without a handwritten walker still have their own class and
declare a language-specific `AstProfile` in that file while inheriting the
reusable `GenericProfileTreeSitterExtractor`. Classes for languages with direct
Tree-sitter wheels declare the corresponding grammar module.

Tree-sitter is a mandatory runtime dependency. REQL does not fall back to the
standard-library Python `ast` parser, regex parsing, or any other code parser.
Syntax errors are reported as compile errors for the artifact instead of being
reparsed by a secondary parser.

## Extracted Graph

Primary technical node types:

- `Project`
- `Directory`
- `File`
- `Package`
- `Module`
- `Class`
- `Interface`
- `Function`
- `Method`
- `Variable`
- `Import`
- `Dependency`
- `Concept`
- `Endpoint`
- `Schema`
- `Config`
- `Test`
- `Comment`
- `Docstring`
- `StaticAnalysisFinding`

Primary technical relations:

- `Project/Directory -CONTAINS-> Directory/File`
- `File -DEFINES-> Module/Function/Class/Interface/Method/Variable`
- `SourceArtifact -DEFINES-> Module`
- `SourceArtifact -DEFINES-> Function/Class/Method`
- `Module -CONTAINS-> Function/Class/Interface/Method/Variable`
- `Class/Interface -METHOD-> Method`
- `File/Module -IMPORTS-> Import`
- `File -DEPENDS_ON-> Dependency`
- `Module -IMPORTS_FROM-> Dependency`
- `Module -RE_EXPORTS-> Import` for imports exposed by package `__init__.py`
- `Function/Method -CALLS-> Function/Method` when locally resolvable
- `Function/Method -INSTANTIATES-> Class/Interface` for constructor calls
- `Function/Method -EMITS-> Event symbol` for Solidity event emission
- `Class/Interface/Module -USES-> Symbol` for Solidity using directives
- `Function/Method -READS-> Variable`
- `Function/Method -WRITES-> Variable`
- `Function/Method -RETURNS-> Symbol`
- `Function/Method -RAISES-> Symbol`
- `Class/Interface -INHERITS-> Class/CodeSymbol`
- `Class -IMPLEMENTS-> Interface`
- `Function/Method -HANDLES_ROUTE-> Endpoint`
- `Module/Function/Class/Interface/Method -EVIDENCED_BY-> SourceFragment`
- `Function/Class/Method -HAS_DOCSTRING-> Docstring`
- `Module/Function/Class/Method -HAS_COMMENT-> Comment`
- `Function/Class/Method -DECORATED_BY-> CodeSymbol`
- `SourceArtifact/Symbol/Import -HAS_FINDING-> StaticAnalysisFinding`
- `SourceArtifact/SourceFragment -CONTAINS-> Concept` for explicit document headings

Code artifacts also produce `SourceFragment` nodes for major supported-language
symbols. Code block fragment IDs are based on the artifact and symbol qualified
name rather than line offsets, so moving a function or class does not create a
new fragment for the same symbol. Repeated compiles do not duplicate symbols or
fragments. Unresolved calls do not create standalone call-site nodes;
non-builtin unresolved call targets are stored as `unresolved_calls`
summaries on the owning function or method. Language builtins and calls through
names imported by the artifact, such as `os.getcwd` after `import os`, are
filtered out as low-signal graph noise.

Compile also records deterministic local static-analysis findings for dead-code
signals that are useful during repository queries. These findings are stored as
`StaticAnalysisFinding` nodes with `finding_type`, `severity`, `symbol_type`,
`symbol_name`, `qualified_name`, `relative_path`, line properties,
`evidence_scope`, `confidence`, `cleanup_priority`, and numeric
`cleanup_rank`. Findings also include cleanup rationale fields:
`removal_safety` (`safe`, `validate`, or `risky`), `removal_reason`,
`validation_reason`, and `blocking_signals`. `FINDINGS` lists sort by
`cleanup_rank` by default, and
`ORDER BY cleanup_priority` uses the same high/medium/low ranking instead of
alphabetical text order. Current finding types include `unused_variable`,
`unused_import`,
`possibly_unused_function`, `possibly_unused_method`, and
`possibly_unused_class`. Function, method, and class findings are intentionally
scoped to references detected in the compiled artifact, so they should be read
as local cleanup candidates rather than whole-program proof. Public functions,
classes, and methods are marked as API-risk candidates with lower confidence and
`cleanup_priority=low`, and test artifacts do not emit `possibly_unused_*`
findings for test classes or test methods. Test-local unused imports and
variables use `evidence_scope=test_local_artifact` and low cleanup priority so
product-code cleanup queries are not dominated by test fixtures. Local variable
findings are limited to variables written inside functions or methods, so
dataclass fields, schema attributes, and module-level model fields do not
dominate cleanup queries.
Imports used only by type annotations, decorators, `__future__` features, or
package `__init__.py` re-export surfaces are filtered out. Imports and symbols
used only in Python default arguments, class bases, and exception handlers are
also treated as real usage. Local variables that are read only inside their
owner are summarized instead of persisted as standalone graph nodes; unused
local variables remain persisted so cleanup queries can target them.

`query_context --cleanup` uses the rationale fields to put direct local cleanup
first, while API, entrypoint, framework lifecycle, and dynamic-reference
candidates are marked for validation instead of direct removal.

Package `__init__.py` imports are also marked on `Import` nodes with
`is_re_export=true` and linked from the module with `RE_EXPORTS`, so public API
surfaces are queryable without treating them as direct removal candidates.

Markdown and other structured documents keep their `SourceFragment` headings and
also materialize stable `Concept` nodes for those explicit headings. These
concepts are deterministic compile artifacts, not LLM guesses, and are archived
when their source heading disappears.

Unrecognized code-like artifacts are handled conservatively: compile registers
only safe project structure such as `Project`, `Directory`, `File`,
`SourceArtifact`, and `CONTAINS`. It does not invent modules, symbols, calls,
source fragments, or semantic relations when no normalized project language is
known, and their presence is not treated as a compile error.

External or synthetic `CodeSymbol` nodes are only created for meaningful names
such as unresolved decorators, base classes, or type hints. Null-like names,
builtins, names already represented by imports, and builtin generic forms such
as `list[str]` are ignored, and external symbols are excluded from default hub
rankings and top-symbol report sections so they do not drown out real project
functions, classes, and methods.

## Tree-sitter Dependencies

The package declares Tree-sitter, direct grammar packages for the recognized
languages that publish Python wheels, and `tree-sitter-language-pack` as a
fallback for grammars that do not have a stable standalone wheel. A source
checkout can install them with:

```bash
python -m pip install -e .
```

## Example Queries

```text
MATCH (a:SourceArtifact)-[:DEFINES]->(f:Function)
RETURN a.path,f.name,f.start_line

MATCH (f:Function)-[:CALLS]->(g)
RETURN f.name,g.name

MATCH (file:File)-[:DEFINES]->(fn:Function)
RETURN file.relative_path,fn.name

FINDINGS WHERE finding_type = "unused_variable"
RETURN symbol_name,relative_path,line_start,reason

MATCH (s:Variable)-[:HAS_FINDING]->(f:StaticAnalysisFinding)
RETURN s.name,f.finding_type,f.relative_path,f.line_start
```


