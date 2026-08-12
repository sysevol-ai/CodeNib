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

The native clangd definition/reference index remains an ephemeral materialized
query view over existing `.idx` artifacts; raw directories and pybind objects
are not catalog state. An opt-in dual-write adapter now requires the bounded
native decoder to bind that snapshot, then emits content-bound per-file
FactBatch units under a complete analyzer/toolchain/build profile. Those units
may be published through the semantic-facts generation coordinator described
in M4/M5. This does not turn the query index into a durable snapshot, advance
generic M2 publication, or weaken the legacy graph serving authority.

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
that identity is lost after the new tree is verified, the new output remains
active; if boundary validation has not succeeded, the suspect new tree is
quarantined first and the active destination remains absent. Deletion failures
after cleanup starts also retain the verified new output and preserve the
partial backup. Secure extraction requires no-follow directory-fd support and
never writes through the replaceable stage pathname. Archive builders publish
the verified open temporary inode with a same-filesystem no-clobber link. The
original destination-to-`.previous-*` rename is the only old-file handoff; after
publication, builders only revalidate that path against the still-open previous
descriptor and never reserve, rename, or unlink it. Controlled M5 GC is
responsible for reclaiming verified old files and missing-destination sentinels.
Generic staged-directory publication now uses the same ownership boundary:
callers capture a bounded, no-follow receipt for the authorized old root before
building, bind the original temporary-root inode, and run their semantic
validator inside an exact tree-token sandwich before and after the atomic
rename.  The token covers the raw relative entry set, exact portable modes,
regular-file sizes and SHA-256 bytes under the view-bundle file, byte, metadata,
path, component, and depth limits.  This avoids relying on directory timestamps,
which are not reliable on every supported filesystem.  Existing non-empty trees
fail closed on platforms without safe directory-fd traversal; a first publish
may proceed only with caller validation and an ownership-checked empty sentinel.
Portable BM25 and vector normalization now exposes a read-only validation
boundary for storage publishers.  Retained JSON must be bounded,
duplicate-safe canonical JSON; decoded metadata and configuration pass the
shared credential and build-path policy.  BM25 validation binds normalized
document paths and artifact fingerprints, while vector validation binds the
provider/model/revision/dimension/metric contract, persistence fingerprints,
level inventories, document counts, and FAISS type/training semantics inside a
bounded full-tree ownership sandwich.  Validation holds a no-follow root
descriptor, authenticates JSON and indexes through descriptor-relative stable
opens, and gives FAISS a callback over the already authenticated index
descriptor rather than reopening an attacker-replaceable path.  The M1 JSON
format has a fixed 16 MiB configuration limit and 256 MiB documents-file limit;
larger repositories require a future streaming or sharded format rather than a
manifest-controlled memory override.  Repository-relative source aliases may
use bounded relative symlinks only when resolution remains inside the pinned
checkout; absolute, outside, looping, raced, and reparse-point paths fail
closed in staging, binding, BM25 reads, and MCP reads.  The shared credential
classifier lives outside both artifacts and storage so the artifact layer does
not depend on the catalog implementation.  These gates close the current
portable context publication surface; they are a prerequisite for, but do not
complete, the remaining legacy manifest import/export adapter or a future
streaming payload revision.

Local filesystem publication now has an explicit strict-authority gate. Regular
files are written through caller-owned staged descriptors, installed with
no-replace rename, authenticated through a retained receipt, and reported
durable only after the parent directory is flushed. Strict `LocalCAS` mode
requires a fully preprovisioned `sha256` layout and retains anchored authorities
for the root, hash directory, and every shard for the store lifetime; these
anchors prevent device/inode reuse from making a replacement generation look
valid. Lazy directory creation remains a cooperative compatibility mode and is
not an adversarial generation boundary. Where owned publication or anchored
directory descriptors are unavailable, default lazy `LocalCAS` retains its
path-checked portable hard-link/atomic-replace backend; strict construction and
provisioning fail before filesystem I/O. The current strict implementation is
POSIX-only, requires callers to close the store to release its generation
anchors, and does not make ordinary same-UID writable files immutable after
publication. Preopened workspace publication applies the same ownership model
to exact, scanner-bounded directory plans. Its side-effect-free capability
probe fails before provisioning on unsupported hosts; descriptor-bound writes
return the authenticated file record, and staged and published validators run
inside the same atomic publication boundary. The caller-owned receipt keeps its
authenticated reader pinned through synchronous consumption. This remains a
provider-neutral foundation: no compiler or context producer is wired to it.

Callback-scoped directory results are likewise provisional until ordered
reader-validity, exact-ownership, child-namespace, and parent-authority
postconditions have been processed after callback success, failure, or
cancellation. Authenticated-file exit preconstructs cleanup before acquiring a
resource, then drives the retained owner across every registered descriptor or
HANDLE even when cancellation lands before a caller can store the returned
integer. Completion-aware owner cleanup grants a bounded retry window for
cancellation before or during close, then returns any still-incomplete,
idempotent owner on the primary exception for explicit retry. Later independent
cleanup actions still run. An identity-reused foreign resource is diagnosed and
never closed when its immutable identity observably differs from the owned
resource. An exact same-inode descriptor ABA or same-FILE_ID HANDLE reuse is not
distinguishable in Python and remains a native-owner promotion gate. One
C-level trampoline gives runner entry, re-entry, planning, action, and loop
failures the same per-action retry state while keeping Python stack and
diagnostic space constant. Exhausting the nine-attempt window retains an
incomplete idempotent owner on the primary and continues later actions. The
first local callback, postflight, or cleanup failure stays primary; later
failures are diagnostic only. Pure Python cannot guarantee execution when
repeated interruption lands before the trampoline itself starts. A known
primary therefore protects pending owners before that handoff, and the first
outer-entry failure becomes the primary and protects them when no earlier
failure exists. Closing that remaining arbitrary call-entry gap, the raw
resource-return gap, and exact-identity ABA requires native ownership. This
does not complete M1 producer wiring.

Native vector parsing is now a separate local-authority boundary. Portable
validation and normalization remain inert: they authenticate the declared
FAISS bytes and canonical JSON inventory without importing FAISS or
deserializing pickle, and a descriptive `trusted-local` string cannot grant
native access. Native consumers must present a process-local authorization
bound to the exact captured vector tree and semantic configuration before any
embedding model, remote client, pickle decoder, or FAISS parser is created.
Compiler rebuilds write the vector files, incremental state, caches, and update
marker into one descriptor-anchored private generation, then publish the whole
directory through the owned-directory transaction. The live git-diff vector
delta is disabled until it can satisfy the same publication boundary; an
incremental request therefore validates its existing authority and state, then
performs a complete private-generation rebuild. The Web runtime mints local
authorization outside the registry only while holding the hardened compiler
cache lock and revalidating the source-fingerprint-v2 repository, manifest, and
captured vector tree. These compatibility and publication gates preserve the
previous complete vector generation on interruption, but they do not complete
the catalog-backed M2 generation coordinator or durable worker wiring.

Static Wiki export now writes through one bounded `OwnedDirectoryStage`
generation, authenticates the prebuilt frontend before copying it, and checks
the complete staged and published inventories through callback-scoped readers.
Source-derived pages, summaries, and graph metadata are read through one
retained source-fingerprint-v2 binding and reverified before the directory
swap. Legacy build-machine absolute citations can be rerooted only through the
longest exact suffix in the frozen source inventory, and bounded line excerpts
recognize LF, CRLF, and CR while authenticating the complete file. The
compatibility export uses an explicit lexical repository label and omits
ambient Git-origin provenance. Index-manifest and output roots are checked for
both lexical and rejection-only canonical overlap, without using a resolved
path to authorize a read or publication. Frontend, generated metadata,
manifest identities, and JSON lexical/DOM complexity are bounded before the
rename; retained JSON uses UTF-8 without a BOM so every backend observes one
portable encoding. Failures retain only an ownership-described orphan for
later quiescent reclamation instead of recursively deleting an online path.
This is still a local compatibility publication path, not a catalog receipt.
`StrictWorkspaceProvider` builds on the foundation with a callback-scoped
contract for a trusted provider to supply a pre-opened
`OwnedWorkspaceAuthority`, exact workspace plan, destination expectation, and
retained publication receipt owner. Session writes, validation, publication,
and revocation share one cancellation-safe gate so a provider callback cannot
publish after it escapes. The gate records the exact callback result or
`BaseException`; a provider cannot substitute its return value or swallow a
callback failure. The strict BM25 producer now captures one detached,
authenticated repository identity, plans canonical document and metadata bytes
from one retained source generation, and binds the source/output records,
repository fingerprint, and caller configuration into an exact workspace plan.
It then replays and validates the same bytes through that contract before and
after replacement without consulting mutable public source projections. Its
`PlannedBm25View` is a short-lived in-process replay subject, not a catalog
profile or durable job payload. No production provider, compiler integration,
or whole-context producer is wired to the strict contract yet, so M1 remains in
progress and the M2 BM25 profile adapter is still outstanding.

Strict whole-context publication can now aggregate already-normalized BM25 and
vector generations retained by active workspace receipt owners. The plan binds
the portable `RepoManifest` projection, one detached authenticated
source-fingerprint-v2 identity, every input generation plan and exact file
record, and the complete context output inventory. Replay copies authenticated
source bytes into one missing-only workspace generation; staged and published
callbacks repeat the content-bound portable-view, credential/path,
context-manifest, and exact-tree checks before the caller receives a durable
output receipt. Large canonical documents remain element-streamed, and vector
indexes stay authenticated but native-inert throughout context assembly.
Planning and publication never consult mutable public source projections after
that identity is captured. This slice does not normalize native vector state,
provision a production workspace provider, or route compiler output. Its
retained receipt is now accepted by the direct M1 importer below, but it is not
yet an M2 fenced job output and is not wired into production compiler/runtime
paths.

Retained manifest import now has additional backend-neutral prerequisite
gates. `BlobInfo` is an exact point-in-time CAS receipt, and
the additive `ReceiptVerifyingObjectStore.verify_receipt` capability
revalidates its digest, byte size, and canonical storage key before a metadata
boundary without pretending that the receipt itself is a pin. The additive
`RetainedImportObjectStore.retain_receipts` callback verifies an exact receipt
set and serializes compliant reclamation until catalog publication and
attestation finish; LocalCAS uses its cancellation-safe lifecycle lock as that
fence, which any future local GC must share. A public
physical archive-size gate lets import coordinators reject impossible view
bundles before object-store byte access. Published snapshot summaries also
close namespace and repository identity alongside source, profile, generation,
object, and view identity. Retained materialized-bundle consumption still needs
a non-forgeable owner and tracked streaming-resource lifecycle; no path or
ordinary dataclass is treated as that authority. Outside the bounded retention
callback, receipts remain point-in-time checks rather than lifetime pins.

A pure retained-manifest planning layer now closes the data-only side of that
boundary for current portable `RepoManifest` v1.1 projections. It accepts only
bounded, unambiguous, BOM-free UTF-8 JSON; rejects unknown shape, non-finite or
over-complex values, credential data, diagnostic data, and forged storage or
filesystem authority claims; and selects only current BM25/vector entries whose
artifact-relative paths are exactly `views/bm25` or `views/vector`. Required
views fail closed while optional incompatibilities are recorded explicitly.
Every current builder compatibility field participates in a versioned
`ViewProfile`, unknown or missing fields fail closed, and the vector profile
also records the artifact-safe resolved model revision/trust policy without
filesystem or ambient-environment discovery. Source-fingerprint v1 remains
diagnostic and inert; v2 is merely eligible for a later retained-source check,
and Git commit text remains display provenance rather than source authority.
Planning performs no source or artifact reads, native parsing, CAS/catalog
operation, receipt minting, or ref publication. Execution is a separate
authority-bearing API, so inspecting or serializing a plan cannot publish it.
The retained exporter described below now supplies equivalent canonical v1.1
bytes. Retained filesystem materialization and production runtime wiring remain
outstanding, so M1 and M2 remain in progress.

The retained-import foundation now also exposes schema-v4 compound-generation
identity as one backend-neutral model rule, including the canonical member
object set that participates in both the generation ID and catalog
reachability. `StreamingObjectStore` is an additive capability, so existing
object-store implementations remain compatible while `LocalCAS` can accept a
bounded expected-digest stream, authenticate it before publication, and reuse
an already durable exact object without consuming the producer. Publication
readers can project an authenticated child subtree without filesystem I/O and
share one process-bound lifetime across every facade and opened stream. Streams
that escape a callback are drained and authenticated on success, aborted
without further source reads on failure or cancellation, and retained for
explicit cleanup retry if a descriptor or HANDLE cannot close.

The authenticated reader can now plan and replay byte-identical canonical
`view-bundle v1` archives without reopening a path. Planning performs a CRC
pass and a complete archive-hash pass; replay is bound to the same active
reader and exact projected subtree. A normal short consumer is drained and
authenticated, while a failed consumer causes no additional source read. The
complete ownership token and ZIP layout are rebuilt from exact builtin values,
so forged equality, modes, records, or envelope fields cannot change replayed
bytes.

The first retained `RepoManifest` executor now imports strict-context receipts
as ready schema-v4 snapshots. Its one-shot API requires both an active
`PublishedWorkspaceReceiptOwner` and an independently retained
source-fingerprint-v2 `RepositorySourceBinding`; a generic workspace receipt
alone is not trusted as producer provenance. Inside one receipt callback the
executor verifies the context envelope, validates every selected BM25/vector
subtree against the retained source, and completes every bundle plan before the
first CAS write. It then streams each canonical bundle and every unique
per-file payload into the additive `RetainedImportObjectStore`, preserving
member reachability while vector native bytes remain inert. Only after the
callback's reader, exact-tree, child-namespace, and parent-authority postflight
return may the importer acquire the exact object-retention scope and run the
first catalog operation.

Each import also stores a portable
`codenib.internal.repo-manifest.v2` projection containing the canonical
complete portable manifest, explicit required/optional/selected/skipped
selection, plan digest, profile/generation identities, semantic object digests
and sizes, and display provenance without backend storage keys. The internal
generation reaches all imported objects; each public generation independently
reaches its per-file objects. Every receipt is revalidated before the first
catalog write and immediately before publication. Source identity is always
read from the binding's private authenticated snapshot rather than its
caller-visible projection. Retained-import catalog response extensions share
capability-specific public bounds and are ignored only outside the exact
identity core. SQLite schema v4 then atomically promotes staged generations,
seals the snapshot, and advances its generation-counted ref; the executor
re-resolves the ref and exact manifest summary before releasing object
retention and returning. Earlier failures may leave unreachable CAS objects or
staged catalog rows for future GC; validation and receipt failures before
publication cannot move the old ref. If cancellation lands after SQLite commits
but before the caller observes the result, the direct M1 call is at-least-once:
an exact retry idempotently resolves to the already-published snapshot without
advancing the ref again. This is the direct M1 bootstrap path, not the M2 fenced
job-success transaction. Retained
materialization/export, production compiler/runtime wiring, and a production
GC implementation and policy remain outstanding, so M1 remains in progress.

The first read-only retained `RepoManifest` exporter now closes the data-only
round trip without introducing path authority. It accepts the additive
`RetainedSnapshotCatalog` capability plus a receipt-verifying object store,
either resolves one ref exactly once or reads one explicit immutable snapshot,
and rebuilds the complete namespace, repository, source, profile, generation,
member, and snapshot identity closure before object access. It authenticates
the internal v2 projection, re-plans the complete portable manifest and its
selection, cross-binds every schema-v4 dependency envelope, and verifies each
canonical bundle plus every per-file receipt. BM25 and vector payload semantics
are checked source-free while native vector indexes remain inert. Unselected
entries are omitted from the emitted manifest, unknown capability extensions
are preserved, and the built-in sparse/dense/hybrid/symbol capabilities are
recomputed from the retained selection. A final receipt sweep precedes return
of canonical `RepoManifest` v1.1 bytes and a point-in-time observation receipt.
That receipt is neither a GC pin nor a materialization/native-load authority;
filesystem output and runtime activation remain separate future boundaries.

Schema v2 now adds
canonical idempotent job requests, immutable
per-view request mappings, bounded retry state, and database-clock fenced
per-ref leases.  Catalog reads revalidate the normalized view rows against the
canonical request; the M2 publication transaction must repeat that gate before
associating outputs.  An explicit acquire may atomically retire an expired
holder while taking over its slot; this slice adds no background reaper and is
not wired to the compiler or Web workers. Retained materialization and
production compiler/runtime wiring remain the outstanding M1 deliverables;
fenced job publication remains M2 work.

The shared compiler-cache lock is a cooperative serialization boundary for
compiler and importer processes using a cache namespace private to one OS
account.  It opens the fixed coordination entry without truncation, validates
it as a single-link regular inode, and binds its visible identity before
entering the operation.  It does not protect the manifest or view tree from a
user who actively replaces paths while the lock is held.  POSIX opens the lock
read/write for `flock` implementations that require write access on NFS, using
`O_PATH` for Linux cache-directory descriptors, `O_SEARCH` where another POSIX
exposes it, and an explicit read-only directory fallback otherwise.  Windows
retains a cooperative `msvcrt` byte lock with pre-entry reparse/link and
identity checks; a future handle-based implementation is required before
adversarial junction or path-replacement races can enter its contract.

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

Schema v3 closes the equivalent replacement gap across the published aggregate
even when a raw SQLite client disables foreign keys and recursive triggers.
Duplicate objects, generations, snapshots, and refs are rejected before
`REPLACE` can erase their dependency rows; referenced objects, generations, and
snapshots cannot be deleted, and refs are persistent.  Unreferenced object rows
remain deletable for the future explicit GC policy.  Ref gates require a ready
snapshot in the same repository, while manifest reads compare raw membership
with fully joined dependencies and recompute every content identity.  Snapshot
publication also requires exact staged/ready timestamp pairing, canonical UTC
publication timestamps, canonical repository/source fields, and positive
integer persisted ref generations.

Schema v4 adds generic compound-view reachability. A view generation may name
sorted immutable member-object digests in identity-bearing reserved metadata;
the normalized membership table references the same registered objects.
Publication revalidates exact metadata/table membership, ready memberships are
immutable, and member objects remain protected even for raw SQLite clients
with foreign keys and recursive triggers disabled. Existing v3 generations
migrate to an exact empty member set.

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

Status: in progress. The provider-neutral fact contract, clangd dual-write
adapter, durable FactBatch publication, and local convergence gate are
implemented; public graph-query cutover remains pending.

Foundation now available:

- [`FactBatch v1`](semantic_fact_batches.md) stores immutable per-file
  definitions, exact occurrences,
  resolved or unresolved edges, diagnostics, provider completeness, content
  identity, profile identity, and position encoding.
- Compatibility adapters project the existing Python `CodeGraph` and SCIP
  occurrence index without changing the persisted graph schema or C++ decoder.
- Ordered resolver passes resolve only unique monikers and require framework or
  heuristic edges to retain provenance, confidence, and the synthesizing
  resolver name.
- File-unit overlay tests compare incremental state with a clean rebuild using
  semantic digests; provider/profile identities can also be checked in strict
  mode. The durable generation coordinator in M5 owns publication of those
  units.
- `codenib.fact-batch-artifact.v1` publishes canonical per-file FactBatch JSON
  through the existing `ObjectStore`. Its reuse key binds schema, canonical
  path, language, content digest, profile digest, and provider. Loads verify the
  caller-held receipt, object-store receipt, object SHA-256, embedded batch
  digest, reuse identity, size bound, duplicate JSON keys, and canonical bytes.
  A repeated key producing different facts fails as an incomplete profile or
  nondeterministic analyzer instead of silently replacing the prior result.
- The strict clangd adapter retains exact definition/occurrence ranges and
  unresolved cross-file SymbolID monikers after the bounded native RIFF reader
  validates the same content snapshot. Its profile binds analyzer, target,
  toolchain, compilation database, build context, position encoding, RIFF
  contract, normalization, adapter schema, and FactBatch schema.
- `codenib.fact-batch-generation.v1` publishes a source-bound manifest plus
  explicit catalog member objects. Whole-file upserts/deletes reuse unchanged
  path/content/profile/provider units, and a failed ref CAS leaves the previous
  ready generation authoritative.
- Snapshot-local definition lookup now has a bounded LRU keyed by the complete
  snapshot ID, so unresolved edges never inherit a target from another
  generation.

Remaining publication and cutover work:

- Establish parity gates for every public graph query and supported language.
- Remove eager incoming-edge reconnection only after parity is demonstrated.

### M5: Per-file versions, retention, and overlays

Status: in progress.

The first durable per-file slice is implemented. Caller-owned
`FactBatchReuseCache` maps remain non-authoritative, but the semantic-facts
coordinator now composes verified receipts into one catalog generation. Schema
v4 makes every unit an explicit generation member/GC root. Incremental
publication carries forward unchanged units and applies path-aware upserts and
deletes without resolving cross-file monikers into reusable artifacts. Tests
cover clean/incremental convergence, profile invalidation, failed publication,
receipt tampering, member reachability, migration, and cross-snapshot cache
isolation.

- Extend the current path/content identity with portable file mode and package
  identity where future non-clangd adapters require them.
- Add snapshot leases, pins, retention policy, mark-and-sweep GC, and crash
  recovery.
- Discover and ownership-validate view-bundle `.previous-*` files before
  reclaiming verified old outputs and missing-destination sentinels.
- Generalize the clangd semantic-facts upsert/delete generation to the
  remaining adapters and enforce owner isolation.

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
