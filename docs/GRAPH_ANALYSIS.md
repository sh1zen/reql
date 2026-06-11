# Graph Analysis

REQL includes deterministic graph analysis for communities, hubs, and bridge
signals. The implementation is dependency-light and does not require NetworkX,
Leiden, or an LLM.

## Communities

Community detection uses bounded deterministic label propagation over active
graph nodes. It ignores maintenance records such as reports, deltas, cache
entries, and prior community nodes. The detector writes `Community` nodes with:

- `id`
- `project_id`
- `label`
- `size`
- `density`
- `salience`
- `status`
- `created_at`
- `updated_at`

Members are connected with `BELONGS_TO_COMMUNITY`. Nodes that touch neighboring
communities receive `BRIDGES_COMMUNITY` edges.

## Hub Analysis

Hub analysis detects high-degree graph nodes, but REQL avoids
promoting generic high-degree nodes by requiring both structural centrality and
semantic specificity.

The score is:

```text
hub_score =
    0.20 * normalized_degree
  + 0.15 * weighted_degree
  + 0.15 * activation_frequency
  + 0.15 * retrieval_usefulness
  + 0.10 * salience
  + 0.10 * community_bridge_score
  + 0.10 * specificity
  + 0.05 * confidence
  - 0.20 * generic_penalty
```

If activation or retrieval usefulness is not present on a node, the analyzer
uses zero. It does not synthesize usage data.

The specificity scorer is deterministic. It rewards concise topical labels,
specific node types, focused neighborhoods, and known successful usage. It
penalizes stopword-heavy or generic vocabulary, generic node types, high degree
without topical focus, unknown/binary artifacts, and high entropy across
neighbor types.

Hub analysis updates node properties:

- `hub_score`
- `centrality_score`
- `specificity_score`
- `community_bridge_score`
- `is_hub`
- `hub_rank`
- `hub_reason`

For scalability, `HUBS` does not run full community detection by default and
does not materialize the whole graph. It first asks the storage adapter for a
bounded top-degree candidate set, then computes centrality over the candidate
subgraph with bounded incident-edge queries. Existing `BELONGS_TO_COMMUNITY`
edges are still used for community bridge scoring. Run `COMMUNITIES` explicitly
when community assignments need to be refreshed before hub analysis.

## CLI

```bash
reql query "COMMUNITIES LIMIT 20"
reql query "HUBS LIMIT 20"
reql query "HUBS TYPE Function,Class LIMIT 10"
reql query "EXPLAIN HUB 'node_id'" --json
```

Use `--json` on `reql query` for structured output.

## REQL

```text
COMMUNITIES LIMIT 20
HUBS LIMIT 20
HUBS TYPE Topic,Concept,Function LIMIT 10
EXPLAIN HUB "node_id"
```

## Limitations

The community detector is designed for local graphs with thousands of nodes. It
is deterministic and fast, but it is not a full modularity optimizer. Hub and
bridge analysis are based on current graph structure and stored usage signals;
absent usage signals remain absent.


