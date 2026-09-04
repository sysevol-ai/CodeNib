<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Hybrid Index Storage Roadmap

## Stable Product Boundary

CodeNib has one public product database boundary: `codenib.storage.WikiStore`.
The single-file facade points to the Wiki-owned contract and its supported
`SQLiteWikiStore` implementation. Constructor injection plus the Wiki
conformance suite is the complete stable pluggability boundary; there is no
stable storage registry, catalog, CAS, or remote backend API.

Repository search state keeps its existing portable contract:

- `RepoManifest` records source identity and available views;
- BM25, FAISS, igraph, and portable context payloads remain file artifacts;
  those files stay bound to `RepoManifest` identity;
- ordinary compiler, artifact, and MCP readers remain authoritative.

The source-only experiment described below does not change those statements.
In particular, it must not turn `codenib.storage` back into a package or make
a database necessary to query a portable artifact.

## H0: v0.2.3 Subtraction Baseline

Status: complete and released on 2026-09-03.

The v0.2.3 boundary is the baseline, not an implementation to undo:

- #748 added the narrow Wiki store.
- #753 through #762 removed the zero-consumer retained-storage product routes,
  workers, benchmark, generic catalog/CAS package, and catalog-backed FactBatch
  experiment.
- #763 reserved the public `codenib.storage` name for the Wiki-only facade.
- #764 released that boundary as v0.2.3.

The earlier storage RFC #199 is closed historical evidence. Existing v0.2.2
catalogs must be materialized with a pinned v0.2.2 environment before upgrading;
the resulting portable artifact remains supported. No catalog rows migrate
into `WikiStore`.

No generic catalog, CAS, or database compatibility layer remains in the
v0.2.3 product.

## Closed Plans

CodeNib no longer plans PostgreSQL, S3-compatible object storage, generic GC,
distributed leases, a shared ANN database, or backend discovery as approved
product work. H2-H7 are only gates for reconsidering a narrowly evidenced need;
their presence in this roadmap is not approval to implement them.

## Promotion Rule

Each later milestone is demand-gated. A design document, test fixture, or
default-off implementation is not a product consumer. Before a milestone moves
from `gated` to `in progress`, the roadmap must name:

1. the current non-test consumer and user-visible problem;
2. the deployment or workload constraint the existing artifact path cannot
   meet;
3. a representative baseline and a success threshold fixed before measurement;
4. the old path that promotion replaces or the new path's deletion date.

Stable promotion additionally requires RepoManifest, direct portable-artifact,
and ordinary MCP compatibility. Failed experiments keep aggregate evidence but
delete implementation, scripts, switches, and dedicated tests.

## Milestones

| Milestone | Status | Authorized result |
| --- | --- | --- |
| H1: BM25 publication proof | Experimental through 2026-10-04 | One source-only SQLite WAL + local CAS round trip |
| H2: Portable multi-view snapshot | Gated on H1 promotion and a second real view consumer | Immutable BM25/FAISS/igraph generations without changing their query formats |
| H3: Jobs and hot switch | Gated on continuous-update product demand | Bounded jobs and validated request-pinned activation |
| H4: Cross-file xref de-materialization | Gated on measured graph-update cost and parity plan | Unresolved monikers plus bounded query-time resolution |
| H5: Per-file content addressing | Gated on H4 parity and reuse evidence | Context-bound reusable file units and version manifests |
| H6: GC and overlays | Gated on retained-byte and dirty-workspace evidence | Audited reachability, safe reclamation, and path-aware overlays |
| H7: Server adapters | Gated on a deployment that exceeds the embedded backend | PostgreSQL control-plane and object-store adapters for a proven contract |

### H1: BM25 Publication Proof

Status: in progress as an `experimental` source-checkout surface.

Owner: index/artifact maintainers. Decision deadline: 2026-10-04.

Allowed surface:

- `scripts/experimental/hybrid_index/`;
- `scripts/experimental/index_persistence.py`;
- focused tests under `test/scripts/hybrid_index/` and the developer-script
  command test;
- the evidence record in `docs/experiments/hybrid_index_h1.md`.

H1 accepts exactly one verified BM25-only portable context artifact. It stores
one deterministic archive in a local SHA-256 CAS and records one immutable
generation, one single-generation snapshot, and a compare-and-swap ref in a
private four-table SQLite WAL catalog. It can export the pinned snapshot back
to the ordinary portable format.

H1 has no stable API, package export, official CLI command, configuration,
Web/MCP route, jobs, leases, GC, overlay, remote adapter, or backend registry.
The experiment uses constructor composition with exactly one implementation.

Open gates:

- [ ] name a current non-test consumer;
- [x] record representative direct-artifact versus persisted-round-trip
      latency, RSS, and retained-byte results;
- [x] pass the failure and concurrency matrix in the H1 evidence record;
- [x] prove the exported artifact passes the existing verifier and ordinary
      BM25/MCP query path without the store;
- [x] prove wheels and stable imports contain no experimental module or command;
- [ ] make an explicit promote-or-delete decision by 2026-10-04.

If any promotion gate remains open at the deadline, delete the H1
implementation, executable script, and dedicated tests. H2 must not start from
an unpromoted H1.

### H2: Portable Multi-view Snapshot

Status: gated; not started.

Demand gate: at least two current view consumers must need the same immutable
publication lifecycle. Promotion must keep BM25 and FAISS in their existing
portable query formats and prove direct-artifact parity. Because igraph is not
currently portable, graph support would first need a separately verified
non-pickle artifact contract. Do not add a generic view protocol merely because
formats share a directory.

### H3: Jobs and Hot Switch

Status: gated; not started.

Demand gate: a product route must require asynchronous repeated builds rather
than an ordinary explicit `index` or `artifact pack` operation. Only then
define the minimum persisted job states. Activation must validate the complete
new artifact before one request-pinned pointer swap; failures keep the previous
snapshot active. Coordinate any Web work with open issue #266.

### H4: Cross-file Xref De-materialization

Status: gated; not started.

Demand gate: representative repositories must show that eager cross-file edge
reconnection blocks incremental indexing goals. Retain unresolved SCIP/LSP
monikers per immutable unit, resolve only within a pinned snapshot, and require
graph-query parity before changing the legacy materialized graph authority.

### H5: Per-file Content Addressing

Status: gated; not started.

Demand gate: H4 parity plus measured reuse must justify the extra identity
surface. A file unit identity must include path, mode, source/package context,
build flags and dependencies, analyzer/toolchain version, and profile where
they affect facts; raw content hash alone is insufficient.

### H6: GC and Overlays

Status: gated; not started.

Demand gate: first measure retained bytes and identify a real dirty-workspace
consumer. GC begins with a non-deleting reachability report and explicit pins,
grace periods, quiescence, and recovery ownership. Overlays use path-aware
upserts and delete tombstones over a pinned base; they are not set union.

### H7: Server Adapters

Status: gated; not started.

Demand gate: document a multi-process or remote deployment that SQLite WAL and
the local filesystem cannot serve, with concurrency, durability, latency, and
cost evidence. PostgreSQL and object storage may then implement the proven
domain contract. They do not justify a dynamic registry, shared ANN database,
or speculative authorization system.

## Tracking

Issue #765 owns the bounded H1 evidence and its 2026-10-04
promote-or-delete decision; PR #766 carries the source-only implementation and
evidence. The tracker references historical #199, foundation #535, and
subtraction #753-#762 without reopening those scopes. Do not use the stale
`feat/storage-catalog-foundation` branch as a base.

The detailed H1 schema, linearization points, failure matrix, commands, and
benchmark receipt live in
`docs/experiments/hybrid_index_h1.md`. Replace milestone status with current
evidence; keep implementation chronology in Git history and PRs.
