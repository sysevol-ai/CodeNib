# Incremental Maintenance Workload v1

`transitions.json` is the frozen workload shared by the graph and vector
incremental-maintenance experiments. It contains eight repository-disjoint
chains across Go, Python, Rust, and TypeScript/JavaScript, with five
first-parent transitions per chain.

Regenerate and validate it with the command in
`docs/experiments/incremental_maintenance.md`. Result rows must carry the
manifest identity returned by `codeminer.eval.maintenance_manifest.manifest_id`.
Graph and vector runners also write independent protocol versions; aggregators
must reject missing transitions and must not pool rows across incompatible
protocol or experiment-configuration identities.

## Frozen result population

`results/` contains the complete paper-facing population:

| File | Contents | SHA-256 |
| --- | --- | --- |
| `graph_v21.jsonl` | 40 graph transitions, protocol 21 | `5eef63bdcc4516e8997e58a29d3915223f3151d1cdbda1ac00ec529c7e9383d8` |
| `vector_v4.jsonl` | 40 vector transitions, protocol 4 | `f8466a8d52f104cac197bd12bd206a778cb59eef9b9a140b3a632b84d62f394d` |
| `graph_stability_v1.jsonl` | Four independent same-commit rebuild comparisons | `49b720d9c588f474cae3b0c0ec0977df10b70d86ef24689d015285f0dace1f42` |
| `summary_v2.json` | Schema-2 aggregate consumed by the paper and Figure 9 | `0dc5b2774b31d61a52bda97ae7d087c93c976bd960a073d142a91d917dfbb444` |

The transition manifest SHA-256 is
`4fdd5d392b723185e3a7edceeb1f69d9aa3310b68e33c7e75a91b9d37a4f0914`.
Regenerating `summary_v2.json` from the three raw result files must reproduce
its hash byte-for-byte.
