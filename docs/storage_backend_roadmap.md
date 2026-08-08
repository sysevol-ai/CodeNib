# Hybrid Storage Backend Roadmap

## Objective

CodeNib will publish repository context through a transactional catalog and
immutable, content-addressed artifacts without replacing the query engines
that make the artifacts useful.  The embedded deployment uses SQLite in WAL
mode plus a local filesystem object store.  A server deployment may replace
those two implementations with PostgreSQL and an S3-compatible object store
without changing compiler or retrieval contracts.

The program is complete only when repository updates, concurrent readers,
multi-version queries, overlays, garbage collection, and server-backed
storage all preserve the existing `RepoManifest`, MCP, BM25, FAISS, graph, and
portable artifact behavior.

## Architectural Boundaries

The storage system has three independent layers:

1. **Transactional catalog** — repositories, source revisions, profiles,
   immutable view generations, published snapshots, refs, jobs, leases, and
   authorization metadata.  SQLite is the embedded implementation;
   PostgreSQL is the future multi-worker implementation.
2. **Content-addressed object store** — large immutable payloads such as BM25
   documents, FAISS indexes and document mappings, graph payloads, and
   portable context artifacts.  The embedded implementation uses regular
   files; the server implementation uses object storage.
3. **Materialized query views** — BM25, FAISS, and igraph remain the execution
   formats used by a pinned snapshot.  They are read models, not the catalog
   or the source of versioning semantics.

DuckDB may be used for offline analysis, but not as the online job/ref
catalog.  A graph database is not part of the design.  `pgvector` or another
shared ANN backend remains optional and must be justified by a serving
benchmark; it cannot replace portable vector artifacts.

## Invariants

- A builder never modifies an artifact reachable from a published ref.
- A request resolves `ref -> snapshot_id` once and pins that snapshot for its
  entire lifetime.
- A snapshot never silently combines views from different source identities.
  Missing or stale views degrade independently and explicitly.
- A failed build cannot move a ref or make the previous snapshot unreadable.
- Source identity preserves both Git commit/tree identity and the existing
  dirty-worktree `source_fingerprint` contract.
- Artifact compatibility includes builder schema, language/toolchain context,
  graph schema, and embedding provider/model/revision/dimension/metric.
- Large payloads do not live in SQL rows.  The catalog stores their digest,
  size, media type, schema, trust class, and storage key.
- Content digests do not grant access.  Every artifact lookup is authorized
  through its namespace, repository, and reachable snapshot.
- The embedded deployment creates a non-null default namespace.  Future RLS
  and cache isolation must not depend on nullable tenant identifiers.
- Legacy `RepoManifest` v1.1 remains importable and exportable while consumers
  migrate.  Optional views continue to fail independently.

## Identity Model

```text
SourceRevision
  clean: repository + commit + tree
  dirty: repository + commit + source_fingerprint

ViewProfile
  canonical digest of view type + builder/schema/toolchain/model configuration

ViewGeneration
  immutable output for one SourceRevision + ViewProfile

PublishedSnapshot
  atomic set of compatible ViewGenerations for one SourceRevision

Ref
  compare-and-swap pointer to one PublishedSnapshot
```

Per-file analysis reuse cannot be keyed by a raw Git blob hash alone.  Paths,
package/module identity, build flags, dependencies, decoder/toolchain versions,
and the analysis profile may affect facts for identical bytes.  The safe
identity is a digest of the source blob plus semantic context and profile, or
the digest of the normalized facts themselves.

`ViewProfile` stays generic because each view has different compatibility
axes, but its builder adapter must emit a versioned, secret-free compatibility
object and reject omissions.  The top level must identify the profile contract,
builder and artifact schemas.  Vector profiles additionally bind provider,
model/revision (or an immutable compatibility fingerprint), dimension, metric,
normalization, and options; graph profiles bind graph schema, route, language
analyzers/grammars/toolchains, and build flags; BM25 profiles bind tokenizer,
chunker, filter, and language schemas.  A legacy import uses an explicit
`legacy` contract instead of guessed defaults.  No builder may reuse or publish
a generation through this catalog until positive and negative tests show that
every semantic compatibility axis changes the profile identity and that a
missing required axis fails closed.

## Publication Protocol

1. Create or reuse an idempotent index job and obtain its lease.
2. Build each requested view in a job-owned staging generation.
3. Write immutable objects, validate their SHA-256 digests and compatibility,
   and record short-lived orphan candidates if publication has not completed.
4. In one catalog transaction, register objects and ready generations, create
   the snapshot manifest, compare-and-swap the ref generation, and mark the job
   successful.
5. Load and validate the new runtime bundle before atomically swapping the
   process pointer.  Requests already using the previous bundle keep it pinned.
6. Reclaim unreachable snapshots and objects only after leases, explicit pins,
   overlay heads, running jobs, retention rules, and a grace period are honored.

The database transaction is never committed before required objects are
durably available.

## Graph Versioning Pivot

The current global igraph materializes cross-file references as resolved
vertex-to-vertex edges.  Updating one file can therefore mutate inbound edges
associated with unchanged files.  Per-file content reuse and natural snapshot
isolation require a different canonical representation:

- immutable per-file symbols and intra-file edges;
- unresolved cross-file references
  `(src_local_symbol, target_moniker, anchor)`;
- a definitions index mapping monikers to local symbols in active file units;
- bounded query-time resolution and a discardable hot-edge cache keyed by the
  complete snapshot or manifest digest.

The new facts will be dual-written beside the legacy materialized graph.  The
legacy graph remains the serving authority until range, neighborhood,
dependency, incoming/outgoing, and incremental replay parity gates pass.

## Overlay Semantics

An overlay pins a base snapshot and publishes immutable overlay generations.
Overlay entries are path operations, not a set union:

```text
overlay_files(path, operation = upsert | delete, analysis_unit_id?)
```

An upsert shadows the base path and a delete is a tombstone.  Each overlay head
is updated with compare-and-swap, is scoped to its owner/namespace, and has an
explicit TTL unless pinned.

## Milestones

### M0: Contract and acceptance matrix

Status: complete.

- Record the architecture, identities, publication protocol, graph pivot, and
  compatibility invariants in this roadmap.
- Define the first `IndexCatalog`/`ObjectStore` contracts without routing existing
  consumers through them.
- Add unit-level conformance tests for digest validation, schema migration,
  ref compare-and-swap, and crash-safe publication boundaries.
- Reconcile the plan with issues #199, #266, and #431.

The backend boundary, identity rules, publication ordering, graph pivot, and
compatibility gates are now durable here.  The initial protocols and
failure-injection/conformance tests establish the acceptance surface without
routing production builders or readers through an unfinished backend.

### M1: Embedded catalog and object store

Status: in progress.

- Implement a versioned SQLite schema with WAL, foreign keys, explicit
  transactions, and forward-only migrations.
- Implement a filesystem SHA-256 object store with atomic writes and verified
  materialization.
- Encode multi-file query views as deterministic, bounded `view-bundle v1`
  objects (`bundle.json` plus `payload/`) before registering them in the object
  store; materialization must verify the complete canonical inventory first.
- Store namespaces, repositories, source revisions, profiles, objects,
  generations, snapshots, snapshot views, refs, and initial job/lease records.
- Import one existing cache/manifest as a ready snapshot and export an
  equivalent `RepoManifest` v1.1.

The SQLite/CAS foundation through refs and atomic snapshot sealing is
implemented.  The deterministic single-object view-bundle bridge now preserves
multi-file view paths, bytes, and normalized executable modes with bounded,
fail-closed verification and atomic materialization.  Materialized metadata is
exactly mode `0644`, payload files are exactly `0644` or `0755` as declared,
and directory modes are not a portable bundle contract. The destination rename
is the old tree's ownership linearization point; both the moved old tree and the
published new tree are revalidated against their full ownership tokens before
cleanup. Cleanup is fully preflighted: failure before its first unlink/rmdir can
roll back only while the backup retains the captured moved-root identity. If
that identity is lost, or deletion has started, the verified new output remains
committed and the backup path is preserved for recovery. Secure extraction
requires no-follow directory-fd support and never writes through the replaceable
stage pathname. Archive builders publish the verified open temporary inode with
a same-filesystem no-clobber link, move the previous file to a discoverable
`.previous-orphan-*` name, and revalidate it against the still-open descriptor.
Publication never unlinks that orphan pathname, so a concurrent replacement is
also preserved; controlled M5 GC is responsible for reclaiming verified old
files and missing-destination sentinels.
Schema v2 now adds
canonical idempotent job requests, immutable
per-view request mappings, bounded retry state, and database-clock fenced
per-ref leases.  Catalog reads revalidate the normalized view rows against the
canonical request; the M2 publication transaction must repeat that gate before
associating outputs.  An explicit acquire may atomically retire an expired
holder while taking over its slot; this slice adds no background reaper and is
not wired to the compiler or Web workers.  Legacy manifest import/export
remains the outstanding M1 deliverable.

Schema v2 deliberately retains complete job aggregates.  Duplicate-insert
guards reject `REPLACE` of jobs, requested views, and persistent lease slots
even from ordinary SQLite connections whose connection-local recursive trigger
setting is disabled.  Catalog connections additionally enable recursive
triggers; direct deletion of requested view rows and cascading deletion of
their parent job remain blocked.  A future retention/GC milestone must add an
explicit aggregate-deletion migration and policy rather than bypassing these
audit guards.  Schema v2 also rejects successful job rows; M2 must remove that
temporary gate only inside the migration which introduces atomic
`publish_job_snapshot` completion.

### M2: Immutable generation publication

Status: pending.

- Make every builder write to a unique staging generation.
- Add per-view profile adapters that fail closed on incomplete compatibility
  inputs and prove every semantic axis participates in the profile identity.
- Publish whole-view BM25, FAISS, and graph artifacts through the catalog and
  object store without changing their ranking/query semantics.
- Add the publication coordinator that verifies each object-store receipt and
  matches digest, size, and storage key before catalog registration; catalog
  metadata alone must never make missing bytes publishable.
- Move refs only after object and compatibility validation.
- Prove interrupted and failed builds leave the previous snapshot usable.

### M3: Jobs and runtime hot switching

Status: pending.

- Wire the M1 idempotent jobs, heartbeats, cancellation, and fenced per-ref
  leases into workers; add progress/events without weakening the catalog state
  machine.
- Expose the #266 status and update APIs with accurate incremental versus
  rebuild behavior.
- Load a complete new bundle and swap it RCU-style; pin old bundles for in-flight
  requests.
- Keep read-only/prebuilt paths safe through copy-on-write or explicit refusal.

### M4: Cross-file reference de-materialization

Status: pending.

- Preserve definitions and unresolved target monikers in Python and C++ decode
  paths while continuing to emit the legacy graph.
- Add snapshot-aware definition/reference resolution and bounded hot caches.
- Establish parity gates for every public graph query and supported language.
- Remove eager incoming-edge reconnection only after parity is demonstrated.

### M5: Per-file versions, retention, and overlays

Status: pending.

- Store path/mode/blob identity and content-addressed analysis units per source
  revision.
- Reuse unchanged analysis units across commits without cross-version edge
  contamination.
- Add snapshot leases, pins, retention policy, mark-and-sweep GC, and crash
  recovery.
- Discover and ownership-validate view-bundle `.previous-orphan-*` files before
  reclaiming verified old outputs and missing-destination sentinels.
- Add path-aware overlay upsert/delete generations and owner isolation.

### M6: PostgreSQL and object-storage deployment

Status: pending and demand-gated.

- Implement the same catalog semantics with PostgreSQL transactions, row
  locking/`SKIP LOCKED`, leases, and namespace-scoped authorization.
- Implement S3/MinIO object publication, verification, materialization cache,
  and deletion recovery.
- Validate multi-worker contention, backup/restore, migration, quotas, audit,
  and failure injection before making the server backend supported.

### M7: Managed semantic and optional shared ANN

Status: pending and benchmark-gated.

- Keep managed embeddings keyed by namespace, immutable embedding fingerprint,
  and strong input digest.
- Preserve local, BYO, managed, no-model, and artifact-only routes.
- Add a shared ANN read model only if measured workload demonstrates that local
  FAISS materialization is the limiting cost.
- Keep artifact export and local fallback as compatibility gates.

## Completion Gate

The program is complete when all milestones are implemented, locally and
remotely verified at their required test tiers, documented, and reconciled
with their issues and PRs.  Passing one backend test, landing the catalog
schema, or publishing a single snapshot is not completion of the objective.
