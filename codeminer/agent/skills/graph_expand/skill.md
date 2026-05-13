<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# graph_expand

Expand seed code blocks along the symbol graph to discover structurally related symbols in bulk.

## When to use

- After an initial search (BM25, embedding, or regex) has identified seed code locations
- When you need to find callers, callees, class members, imported dependencies, or other structurally related code
- When you want to enlarge the context around known functions/classes at the code-chunk level

## When NOT to use

- As a first retrieval step (requires seed nodes from an upstream search)
- For semantic similarity search (use `embedding_search` instead)
- When the symbol graph has not been built

## Parameters

| Parameter     | Type       | Default | Description                                              |
|---------------|------------|---------|----------------------------------------------------------|
| seed_nodes    | List[QueriedNode] | required | Seed nodes from upstream search results         |
| method        | str        | bfs     | `"bfs"` for k-hop BFS or `"ppr"` for Personalized PageRank |
| top_k         | int        | 50      | Maximum number of expanded nodes to return               |
| hops          | int        | 2       | Number of hops for BFS expansion                         |
| direction     | str        | both    | `"forward"`, `"backward"`, or `"both"`                   |
| damping       | float      | 0.85    | PPR damping factor (only for method=ppr)                 |
| filter_tests  | bool       | true    | Exclude test files from results                          |
| edge_types    | List[str]  | null    | Edge types to traverse (null = all)                      |
| node_types    | List[str]  | null    | Node types to include (null = all)                       |

## Output

`List[QueriedNode]` — expanded related symbols with file path, line range, score, and source content.

## Methods

- **BFS** (`method: bfs`): Local k-hop neighborhood expansion. Good for finding directly related code within a bounded radius.
- **PPR** (`method: ppr`): Personalized PageRank over the full graph seeded from the input nodes. Good for finding globally relevant code weighted by structural importance.
