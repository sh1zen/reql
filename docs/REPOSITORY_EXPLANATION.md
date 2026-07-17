# Repository Explanation

## Purpose

The repository explanation layer turns REQL's technical code graph into a
source-backed business view. It is intended for two related uses:

- give a coding agent a compact model of the responsibilities and likely change
  boundaries in a repository;
- explain the repository to a developer without starting from a raw file tree
  or a list of symbols.

The technical graph remains the source of truth. The explanation is a read-only
projection and is never a replacement for code evidence.

## Command and API

Compile the repository once, then request an overview or focus the explanation
on a feature, behavior, or business concept:

```bash
reql project compile .
reql project explain .
reql project explain . --focus "order refund"
reql project explain . --focus "order refund" --json
```

The Python facade exposes the same projection:

```python
from api import MemoryGraph

graph = MemoryGraph.open(".reql/memory.reql", read_only=True)
try:
    explanation = graph.explain_project(
        ".",
        focus="order refund",
        max_capabilities=12,
        max_workflows=8,
    )
    print(explanation.to_markdown())
    payload = explanation.to_dict()
finally:
    graph.close()
```

## Projection Model

The output contains:

- `project`: the compiled project identity and root;
- `basis`: counts and explicit guarantees about the deterministic, read-only
  derivation;
- `layers`: architectural roles and their capability ids;
- `capabilities`: purpose, responsibilities, primary paths, dependencies,
  owners, tests, and ranking score;
- `workflows`: schema-version-2 semantic entities with name, intent, trigger,
  inputs, outputs, invariants, participants, and source-backed evidence;
- `change_guide`: focus-ranked starting points and verification paths.

Every code evidence record contains a graph node id, node type, label, relative
path, and available line range. JSON consumers can therefore move from the
business view back to exact code.

Workflow participants are explicit `implemented_by` relations whose targets are
code evidence records. A workflow does not expose an ordered `steps` path:
technical edges support membership, but their traversal order is not presented
as business behavior.

## Deterministic Inference

The service derives the view without model calls:

1. Resolve the registered `Project` and query its active artifacts, modules,
   public code symbols, and indexed graph metrics.
2. Remove test modules from capability ownership while retaining test paths for
   verification guidance.
3. Detect a dominant source package and group code below that boundary into
   stable capability areas.
4. Rank owners with graph degree and optional lexical focus matches.
5. Assign architectural layers from package and directory roles.
6. Resolve inter-capability dependencies from technical graph edges and local
   resolved imports.
7. Admit workflows only from explicit endpoints, entrypoint roles, or public
   functions and methods at interface boundaries. Focus affects ranking but
   never turns a symbol into a workflow trigger.
8. Aggregate implementing participants from all outgoing `CALLS`,
   `HANDLES_ROUTE`, `INSTANTIATES`, and `WRAPS` evidence, then corroborate them
   with semantically aligned imported symbols referenced in implementation
   bodies, capability boundaries, signatures, docstrings, and ingested
   documentation when available. Type-only imports and unrelated utilities are
   not promoted to workflow participants.
9. Rank change starting points with the existing lexical index and attach tests
   whose paths match the capability's responsibility vocabulary.

Stable ids for capabilities and workflows are derived from the project and
their graph-backed members. The projection itself is not stored, so running it
does not change node counts, edge counts, salience, or usage.

The projection evaluates every matching active node, degree-ranked node,
lexical match, and relevant relationship in the selected project. It does not
apply hidden per-type, hub, relationship, or per-node edge-count limits.
Workflow membership uses a documented four-hop semantic boundary to avoid
absorbing unrelated shared utilities into one use case.
`max_capabilities` and `max_workflows` only control the size of the rendered
result after the complete graph evidence has been evaluated.

## Interpretation and Limits

The layer describes what the compiled code supports, not product intent that
has no representation in source or ingested documents. Dynamic dispatch,
reflection, dependency injection, generated code, and runtime configuration
can hide relationships from static analysis. Such gaps reduce workflow detail
but do not produce fabricated edges.

Tests, modules, constructors, focus-only matches, `METHOD`, `READS`, and inverse
ownership edges cannot create workflow entities. This prevents structural
paths such as test fixture setup or class-to-module ownership from being
presented as domain behavior.

`--focus` is recommended for modification planning. It ranks the most relevant
symbols and capabilities, but the returned tests and dependencies remain
evidence for investigation rather than a guarantee of complete impact coverage.
