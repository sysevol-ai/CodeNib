# Incremental Graph Benchmark (Protocol v4)

These JSONL files are the final paper inputs for the real-commit incremental
semantic-graph case study. Each row records one first-parent transition and
three independently executed strategies: fresh rebuild, file-level patch, and
symbol-level patch.

An incremental result is admitted only when its vertex and typed anchored-edge
multisets match the fresh target graph both globally and on facts touching
changed files, and deterministic static definition/reference replays are exact.
The semantic projection canonicalizes a commit-valued SCIP package version only
for external vertices with no source range. Source-mapped vertices retain their
raw identities, so a revision difference remains a guard failure. The replay
uses the persisted, reloaded graphs and is an end-to-end serving regression
guard rather than an independent semantic sample.
Speedup is reported only for admitted rows. Per-step incremental time includes
change detection, repair, and persistence; `amortized_t_s` additionally assigns
one fifth of the long-lived LSP startup to each chain position.

Final inputs:

| File | Repository | Source-changing transitions | SHA-256 |
| --- | --- | ---: | --- |
| `sklearn_results_v4.jsonl` | scikit-learn/scikit-learn | 4 (plus one retained no-source transition) | `381fb7896f9502c48ea664e69c385f62c1bc27ece6c6e959053b0b22565b2da8` |
| `caddy_results_v4.jsonl` | caddyserver/caddy | 5 | `e0ce67a304a1e2a7a17d258e9d0886e59f3cd36633e39fe627a59fed13302ee4` |

Reproduce a chain with `scripts/profiling/profile_incremental_graph.py`; the
exact base commits and target transitions are embedded in every row.
