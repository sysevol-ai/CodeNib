# Hybrid Storage Backend Roadmap

## Objective

CodeNib will publish repository context through a transactional catalog and
immutable, content-addressed artifacts without replacing the query engines
that make the artifacts useful. SQLite WAL and the local filesystem SHA-256
CAS are CodeNib's canonical supported storage backends and production defaults,
not temporary bridge implementations. PostgreSQL and S3-compatible object
storage are optional adapters that may be activated independently only after
documented deployment demand and representative benchmark or operational
evidence justify them. Neither adapter is a prerequisite for M1-M5, embedded
completion, or retained-route promotion.

The embedded program is complete only when repository updates, concurrent
readers, multi-version queries, overlays, and garbage collection preserve the
existing `RepoManifest`, MCP, BM25, FAISS, graph, and portable artifact
behavior over those canonical backends. Choosing the canonical backends does
not itself promote retained compiler or runtime routes; A2, B1, and B2 remain
separate gates.

## Architectural Boundaries

The storage system has three independent layers:

1. **Transactional catalog** — repositories, source revisions, profiles,
   immutable view generations, published snapshots, refs, jobs, leases, and
   authorization metadata. SQLite WAL is the canonical supported production
   default; PostgreSQL is an optional future multi-worker adapter.
2. **Content-addressed object store** — large immutable payloads such as BM25
   documents, FAISS indexes and document mappings, graph payloads, and
   portable context artifacts. The local filesystem SHA-256 CAS is the
   canonical supported production default; S3-compatible storage is an
   optional future adapter.
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
manifest-controlled memory override.  Authenticated repository snapshots may
use bounded relative symlinks or absolute symlinks whose raw target has the
pinned lexical checkout root as a complete path-component prefix.  Absolute
targets are rebased and walked again from the retained root descriptor or
HANDLE; Windows containment uses the reparse substitute name and retains the
full reparse payload for later verification.  Unauthenticated path-only reads
and portable staging remain relative-only.  Outside, root-escaping, looping,
raced, and unsupported reparse-point paths continue to fail closed, and the
Docker source fingerprint helper applies the same host-root-to-workspace
mapping.  Repository manifest v1.2 additionally persists one canonical
`repository-source-selection.v1` value and digest across the repository,
last-indexed generation, and every view entry. Exact root-relative subtree
exclusions are part of source fingerprint v2 framing, so policy changes cannot
reuse or relabel a view built for another source surface. Compiler builders,
retained import/export/materialization plans, live source bindings, and
portable runtimes all verify the same selection identity; v1.1 remains readable
only through its exact policy-3/no-selection compatibility path and migrates on
the next successful compilation. Graph and Zoekt artifacts require
authenticated content receipts before deserialization or process startup.
The shared credential
classifier lives outside both artifacts and storage so the artifact layer does
not depend on the catalog implementation.  These gates close the current
portable context publication surface, and the injectable retained materializer
described below now closes the retained filesystem boundary. The explicit local
CLI bridges described below can invoke it for publication and cold start, but
benchmark-backed default compiler/runtime promotion and a future streaming
payload revision remain.

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
authenticated reader pinned through synchronous consumption. The contract
remains provider-neutral, and a concrete `LocalWorkspaceProvider` now supplies
it on Linux for missing or active-receipt-bound exact destinations below one
private, quiescent root owned by the current effective UID with exact mode
`0700`. Native workspace-owner
protocol v6 preserves the v2 publication contract: it pins the namespace and
owns every file descriptor, acquires and writes files without exposing raw file
descriptors to Python, and gates the only forward rename with a one-shot publish
permit. The caller-owned receipt slot is the publication-authority
linearization point: unreceipted same-process failures quarantine the exact
candidate, while a fork child authenticates and closes only its inherited
descriptor pairs. The v3-compatible capture state authenticates the private
root and exact destination name/device/inode binding without mutation. Protocol v4
added a distinct one-shot replacement permit and one aggregate for that
incumbent plus a hidden, same-parent candidate. Protocol v5 retained that atomic
exchange history and inserted a required parent lease before candidate mutation.
Protocol v6 adds exact cancellation-aware provisioning, replacement
provisioning, and directory-sealing entry points while retaining the v5 entry
points unchanged for non-interruptible callers. The native directory-plan and
directory-`fsync` loops poll only after the current record or syscall has been
attested and before future work, preserving both exact callback identity and
storage-error precedence.
Its success states are `destination-captured` -> `destination-leased` ->
`replacement-provisioning` -> `replacement-provisioned` ->
`replacement-adopted` -> `replacement-exchanged-unreceipted` ->
`replacement-receipted`, followed by `closed`; interrupted paths may instead
enter `replacement-recovery-required` or `quarantined`.

Capture is speculative and lock-free. It retains one owner/guard pair for the
borrowable parent authority used by namespace operations and `fsync`, and opens
a separate, never-exposed parent descriptor plus guard for the lease. Native
capture authenticates the hidden pair as the same parent identity, internally
the same open-file-description (OFD), and a different OFD from the borrowable
parent pair. Lease
acquisition applies nonblocking `flock(LOCK_EX | LOCK_NB)` only to that hidden
OFD, then revalidates the complete parent chain and captured destination binding
under the lock. A borrower's `LOCK_UN` on the exposed parent cannot release the
lease. A stale capture is rejected under lock with zero candidate mutation and
releases the lease, remaining `destination-captured`; it neither auto-adopts nor
quarantines the newer mapping. Contention likewise makes no candidate. If an
earlier owner reverses its exchange and restores the captured incumbent,
another owner may acquire successfully on retry. The parent-wide flock provides
one cooperative cross-process single-writer boundary, including different
destination names under the same parent, from before provisioning through
receipt commit or an authenticated reverse exchange and parent `fsync`.

The only exchange is Linux `renameat2(RENAME_EXCHANGE)` for two same-device
directories under the captured parent, with no portable or multi-rename
fallback. Before receipt settlement, same-process recovery can restore the
incumbent and retain the candidate at its pre-existing, caller-supplied
authenticated hidden slot. The primitive validates the hidden basename but
does not generate or prove randomness. A committed exchange leaves the
candidate live and the incumbent at that slot. This is live atomicity without a
crash journal: `SIGKILL`, host failure, and power loss have no promised
rollback. This assumes a private, quiescent `0700` namespace whose cooperative
CodeNib writers honor the guard; it is not protection against hostile same-UID
mutation.

If the mapping becomes unknown, callers must retain and explicitly retry the
recovery owner. Forward parent `fsync` runs inside exchange before a token is
returned; its failure leaves the caller with no token and requires owner
recovery/abort. A reachable receipt-commit dual-binding or `LOCK_UN` failure
instead leaves the already-returned same exact receipt token unconsumed. While
the exact owner remains active and before close, it may retry commit only after
native reclassification proves the exchanged mapping; after close, the token is
no longer a retry capability and commit fails closed. Owner abort remains the
alternative reclassifying/reversing settlement. Every lease-active recovery,
including an operator-restored mapping or receipt retry, performs parent `fsync`
before eventual unlock. If replacement `mkdirat` reports failure before a
candidate identity is confirmed, settlement keeps the lease held while it
authenticates the incumbent and hidden authorities. Only an exact
`fstatat(..., AT_SYMLINK_NOFOLLOW)` `ENOENT` for the configured slot authorizes
no-candidate settlement, parent `fsync`, unlock, configuration reset, and close.
An existing or rebound slot, or any ambiguous stat result, enters or retains
`replacement-recovery-required` with the lease and configuration intact for
retry. A no-candidate unlock/close retry can therefore settle without requiring
an externally stale live name to be restored. If native lease acquisition
returns but Python return is interrupted, the owner remains
`destination-leased` and repeated acquisition is idempotent without taking a
second flock. A fork child cannot mutate, reverse, commit, or unlock the
parent's lease; it only authenticates and closes its own inherited descriptor
pairs.

The protocol-v6 Python publication seam retains the v5 replacement lifecycle.
`OwnedWorkspaceAuthority.bind_replacement_source(...)` consumes
an active source receipt and requires its exact private destination-binding
object while the supplied native owner is already `destination-leased`. It
borrows and retains the parent and incumbent descriptors before provisioning,
matches the parent identity and complete incumbent ownership against that same
active generation, repeats native binding and descriptor checks after the tree
scan, and privately binds the one-shot replacement permit. No detached permit
or replayable replacement token leaves the authority. The subsequent
`provision_bound_replacement(...)` call alone provisions and adopts the
candidate from that same owner, hidden slot, and frozen plan.

Sealed replacements publish only through
`publish_replacement_into(...)`, whose dedicated dual-root implementation uses
separate candidate and incumbent descriptor readers and the permit-bound
`RENAME_EXCHANGE`. The generic isolate/rename helper explicitly rejects the
native replacement authority, and no generic parent sync runs after exchange.
The receipt-owner slot store is the Python linearization point: native abort
authenticates and reverses before the store, while active or cleanup-retain
reconciliation idempotently commits after it. After native
`replacement-receipted`, the live authority verifies only the candidate
descriptor and destination binding; terminal close does not depend on that
live-name check. The displaced incumbent receipt uses a normal
`linux-renameat2` orphan locator so it can reopen after the transaction-only
native aggregate closes.

The receipted aggregate seals its path-based live-name verifier and new parent
borrows, while earlier borrowed descriptors remain owner-owned until close.
Abandoning unknown recovery authority deliberately retains the raw descriptors
and cooperative flock until process exit rather than silently discarding the
only authority. Every borrowed incumbent, candidate, parent, or
planned-directory descriptor remains owner-owned and callers must not close it.
Trusted internal code could still use those descriptors with `*at` syscalls, so
the primitive is not full `_TreeOwnership`.
The provider remains an authority boundary for trusted callbacks, not an
in-process Python sandbox.
The explicit retained workflows construct it for operator-requested publication
and cold start; no compiler or runtime route constructs it by default yet.

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
distinguishable in Python and remains a native-owner promotion gate for the
generic POSIX and Windows publishers. The Linux workspace owner closes the
equivalent workspace-file return and OFD-identity gaps for
`LocalWorkspaceProvider` by acquiring, writing, authenticating, and closing the
file inside the native aggregate. One C-level trampoline gives runner entry,
re-entry, planning, action, and loop failures the same per-action retry state
while keeping Python stack and diagnostic space constant. Exhausting the
nine-attempt window retains an incomplete idempotent owner on the primary and
continues later actions. The first local callback, postflight, or cleanup
failure stays primary; later failures are diagnostic only. Pure Python cannot
guarantee execution when repeated interruption lands before the trampoline
itself starts. A known primary therefore protects pending owners before that
handoff, and the first outer-entry failure becomes the primary and protects
them when no earlier failure exists. Closing those remaining gaps outside the
Linux local provider still requires native promotion. This does not complete
M1 producer wiring.

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
`OwnedWorkspaceAuthority`, exact workspace plan, receipt-derived destination
binding (or `None` for a missing destination), and retained publication receipt
owner. `PublishedWorkspaceDestinationBinding` is private-construction and
immutable: an active receipt owner projects the lexical destination, parent
identity, and full `_TreeOwnership` as one unit. Request construction rejects a
different path or any non-exact binding type; generic adoption authenticates
the bound parent and complete live destination tree; and the callback session
requires full binding equality. The categorical destination expectation is now
derived only, so neither a string nor a raw ownership token can authorize
replacement. Session writes, validation, publication, and revocation share one
cancellation-safe gate so a provider callback cannot publish after it escapes.
The gate records the exact callback result or
`BaseException`; a provider cannot substitute its return value or swallow a
callback failure. The strict BM25 producer now captures one detached,
authenticated repository identity, plans canonical document and metadata bytes
from one retained source generation, and binds the source/output records,
repository fingerprint, and caller configuration into an exact workspace plan.
It then replays and validates the same bytes through that contract before and
after replacement without consulting mutable public source projections. Its
`PlannedBm25View` is a short-lived in-process replay subject, not a catalog
profile or durable job payload. Strict whole-context and retained
materialization APIs are now available as injectable library producers.
`LocalWorkspaceProvider` preserves the missing-destination flow and now also
selects the protocol-v6 replacement seam for strict BM25. The immutable request
still contains only its receipt-derived destination binding. The top-level
operation receives the separately active exact source owner and creates a
private PID-bound, callback-scoped one-shot gate. That gate is tied by object
identity to the request binding and operation, consumes the active source into
`bind_replacement_source(...)`, and returns no owner or replayable capability.
The exact callback cannot enter unless the same gate bound the same workspace;
the Local provider then provisions only through the handed-off authority and
the session publishes only through `publish_replacement_into(...)`. Gate C is
complete. Exact mode treats `provision_timeout_ns` as three fresh budgets: one
for capture and lease, one minted after source binding for provisioning, and
one minted after the user build for the pre-exchange publication transaction.
Candidate and incumbent scans, staged validation, and the binding rechecks
consume the last absolute deadline; if they overrun it, native exchange observes
expiry before namespace mutation. Published validation and post-exchange
settlement retain their reconciliation contracts but are not controlled by
that deadline. The explicit retained workflows can now construct the
whole-context producer for one existing catalog selection, publish normal
compiler output, and load one selected generation at MCP cold start, but no
default compiler/runtime route constructs them. M1 remains in progress and the
M2 BM25 profile adapter is still outstanding.

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
that identity is captured. The producer can now use a caller-supplied
`LocalWorkspaceProvider` for a missing destination. The explicit CLI bridge now
constructs that provider and closes its output receipt for one invocation, but
no default compiler/import/runtime route manages either lifecycle. This slice
does not normalize native vector state or route compiler output by default. Its
retained receipt is now accepted by the direct M1 importer below, but it is not
yet an M2 fenced job output.

Retained manifest import now has additional backend-neutral prerequisite
gates. `BlobInfo` is an exact point-in-time CAS receipt, and
the additive `ReceiptVerifyingObjectStore.verify_receipt` capability
revalidates its digest, byte size, and canonical storage key before a metadata
boundary without pretending that the receipt itself is a pin. The additive
`InterruptibleReceiptVerifyingObjectStore.verify_receipt_interruptibly`
capability preserves that legacy one-argument contract while polling before
future object reads. The separate additive
`InterruptibleStreamingObjectStore.put_chunks_interruptibly` capability also
polls across reusable-object authentication and future producer items without
changing the legacy three-argument streaming method. Cancellable retained
compiler preparation fails closed on both capabilities before source or
workspace authority is read; the local worker resource boundary repeats the
gate. Legacy non-cancellable callers continue to use the original exact receipt
and streaming call shapes.

The additive `ReceiptRetainingObjectStore.retain_receipts` callback verifies an
exact receipt set and serializes compliant reclamation until the guarded
operation and attestation finish; retained import adds streaming ingestion to
that narrower read capability. LocalCAS uses its cancellation-safe lifecycle
lock as that fence, which any future local GC must share. A public
physical archive-size gate lets import coordinators reject impossible view
bundles before object-store byte access. Published snapshot summaries also
close namespace and repository identity alongside source, profile, generation,
object, and view identity. Retained materialized-bundle consumption now uses a
caller-owned publication receipt plus tracked streaming-resource lifecycles; no
path or ordinary dataclass is treated as that authority. Outside the bounded
retention callback, receipts remain point-in-time checks rather than lifetime
pins.

A pure retained-manifest planning layer now closes the data-only side of that
boundary for current portable `RepoManifest` v1.2 projections while preserving
strict compatibility with retained v1.1 projections. It accepts only
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
The retained exporter described below now supplies equivalent canonical bytes
for both supported manifest versions, and the injectable retained materializer
closes their filesystem publication boundary. The explicit local CLI bridge
uses the concrete local provider only for an operator-selected retained ref or
snapshot. Explicit compiler publication and cold-start runtime routing now use
the same boundary, while benchmark-backed default compiler/runtime promotion
remains outstanding, so M1 remains in progress. Profile adapters and fenced
publication remain M2 work; production retention and garbage collection remain
M5 work.

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
job-success transaction. Default compiler/import/runtime wiring remains the M1
gap; production retention and garbage collection remain M5 work.

The explicit compiler-cache ingress now connects a selected current BM25,
vector, or combined BM25/vector `IndexCompiler` view set to this executor
without treating mutable cache paths as retained authority. The compatibility
`import_compiler_cache_bm25` wrapper remains, while `import_compiler_cache`
takes the exact canonical view set and one existing-only cooperative cache
lease. It validates the retained source-fingerprint-v2 identity, exact manifest
data, full lowercase 40-character Git commit, and each selected current view.
Before the first workspace mutation, it completes every raw-view recapture
plan, the canonical selected-view portable manifest, and the retained import
plan. While the same lease is held, it publishes one immutable evidence
generation per selected view, then plans and publishes one whole-context
generation. All selected generations therefore enter one context and one
atomic snapshot/ref publication. The coordinator repeats the source, manifest,
byte, and receipt checks, then releases the cache lease before any retained CAS
or catalog data operation, so only the authenticated context receipt crosses
the mutable-cache boundary.

Strict vector ingress requires current raw builder schema 8. Its root commit
binds each FAISS file to a canonical ordered `documents_*.json` array and the
`codenib.vector-documents-array-index.v1` row-mapping contract: JSON array
position is the corresponding FAISS row. Level counts, configuration, and
content fingerprints must agree before publication. The trusted producer also
reopens every generated FAISS file before publishing the compiler-cache
generation and verifies its dimension, row count, metric, index type, training
state, and canonical row IDs; exact root, level, and document semantics are
checked in the same authenticated generation sandwich. Raw schema-7 compiler
caches must be rebuilt; they cannot be recaptured by guessing a row mapping or
loading their pickle state. This producer gate does not revoke compatibility
for already-retained portable vector schema 7, which remains readable beside
schema 8. Portable recapture, retained import, export, and materialization keep
FAISS bytes inert; native parsing still requires the separate process-local
authorization described above. Schema-8 source paths are canonical
repository-relative POSIX paths backed by regular lexical files; contained and
external symlink aliases both fail closed so document paths, node identities,
and retained source records cannot silently name different files.

This cache lease and the resulting hashes prove a bounded, self-consistent
capture, not signed producer provenance. The source and cache namespaces must
be private, trusted, and quiescent except for CodeNib processes honoring the
same lease. An actor able to replace both a cache view and its manifest can
mint matching hashes; strict recapture does not turn that namespace into an
adversarial sandbox. In particular, inert treatment prevents import-time
native parsing but does not assert that arbitrary FAISS bytes came from a
trusted producer.

`codenib artifact import-cache` exposes that coordinator as an explicit local
bootstrap. It requires an existing initialized SQLite catalog, a fully
preprovisioned strict `LocalCAS`, an existing private Linux workspace root with
exact mode `0700`, and a real producer cache with its existing lock. With no
`--view`, it preserves the BM25-only default; repeated or comma-separated
`--view bm25` and `--view vector` options select either or both current views.
Each call chooses a fresh missing-only destination for every selected view plus
one context destination and never recursively removes a published directory
after a later failure. A changed import atomically advances the selected ref
and prints an immutable-snapshot `artifact materialize` handoff without
reserving the suggested output. An exact retry uses fresh destinations but the
original expected ref generation and idempotently returns the already-published
snapshot without another ref advance.

The same coordinator now has a normal compiler composition boundary.
`compile_and_import_repo` performs update-or-create and immutable recapture in
one compiler-cache lease, binds the exact serialized manifest to the returned
compiler result, and releases the lease before the retained importer invokes
CAS/catalog data-plane methods. Support, existing-storage open, and static
contract probes may run before the lease.
The existing `codenib index` command exposes it only when
`--publish-retained` is present with an existing catalog, preprovisioned strict
CAS, private `0700` workspace root, canonical repository key, and optimistic
ref generation. A first build may create the canonical compiler cache. Before
that creation, the CLI authenticates its nearest existing real ancestor and
denies physical storage aliases; inside the sole semantic lease it freezes the
created cache identity, repeats the storage-topology gate before publication,
and verifies it again after recapture. `--rebuild` is incompatible with this
route so an at-least-once retry can reuse the same compiler generation and
resolve to the same snapshot rather than manufacture new bytes. Default index
behavior remains unchanged when the flag is absent.

Both compiler-cache ingress routes are offline publication paths, not
request-path or worker-path primitives. Exact ownership capture, semantic
validation, workspace replay, bundle/CAS ingestion, and receipt revalidation
deliberately make multiple bounded passes over large payloads; a selected FAISS
file may therefore be read more than once. Representative end-to-end corpus,
payload-size, and storage-media benchmarks are required before either path is
enabled by default, used as a latency-sensitive service ingress, or has any
validation pass removed. M1 remains in progress because catalog-selected
cold-start runtime routing is supplied only by the explicit route below and
benchmark-backed default compiler promotion is still absent. Graph and Zoekt
cache ingress, generic builder profiles, and fenced job publication remain M2
or later work, runtime hot switching remains M3 work, and evidence retention
and reclamation remain M5 work.

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
of canonical current `RepoManifest` v1.2 bytes (or exact v1.1 compatibility
bytes when exporting a retained legacy projection) and a point-in-time
observation receipt that carries the attested namespace ID, canonical repository
key, and derived repository ID. It is neither a GC pin nor a
materialization/native-load authority; filesystem output and runtime activation
remain separate authority boundaries rather than implicit powers of the export
receipt.

The first retained context materializer now turns that data-only export into
one exact, query-compatible filesystem generation without treating the export
receipt as authority. It requires an additive receipt-retaining object store,
bounded-reads the retained v2 projection, reconstructs its source, public view,
internal projection, and snapshot identities, and cross-binds the emitted
manifest and point-in-time receipt to that immutable closure. It then
authenticates each canonical view bundle, cross-binds bundle inventory and
member receipts, and preserves canonical `0644`/`0755` member modes under
`views/<type>/`. A PID-bound, serialized, one-shot retention callback is revoked
synchronously before the API can return; that callback spans all member reads,
a final pre-rename receipt sweep, missing-only strict-workspace publication, and
both staged and published semantic verification. Static metadata is `0600`,
directories are `0700`, and the caller-precreated output receipt owner remains
the sole authority for the published generation. The API is backend-neutral
and injectable, and it can use `LocalWorkspaceProvider` for a missing
destination.

The first explicit local bridge over this API is now
`codenib artifact materialize`. It requires an existing initialized CodeNib
SQLite catalog, a fully preprovisioned strict `LocalCAS`, an existing private
Linux workspace root owned by the current effective UID with exact mode `0700`,
and a missing output below that root. The three storage/authority locations
must not overlap or resolve through physical mount aliases. Existing-only
catalog open is read-write rather than read-only: before opening the original
namespace, it descriptor-copies a bounded, no-follow main/WAL snapshot and
requires the complete canonical SQLite schema for the recorded v1-v4 migration
level. It rejects rollback journals, unsafe or oversized WAL/SHM sidecars, and
missing, empty, foreign, or corrupt databases without enabling WAL or migrating
the original. The database must be a single-linked regular file. Existing-only
open binds its captured `(st_dev, st_ino, st_nlink=1)` identity across
`sqlite3.connect` and repeats the canonical schema check before WAL activation
or migration. Its resolved ancestor chain and WAL/SHM sidecar namespace remain
an explicit trusted, quiescent deployment boundary for the whole invocation;
the identity checks do not claim a path-based sandbox against arbitrary
concurrent renames. The CLI retains opened filesystem authorities for the CAS,
workspace, and output parent through publication, revalidates them immediately
before provisioning, and passes the retained output-parent identity into the
native workspace owner so its pinned publication parent must match before any
callback can publish. It rejects same-object, bind-mount, workspace re-entry,
ambiguous stacked-mount, and catalog file-mount aliases before opening storage.
Every existing output-parent component must be a real directory on the
workspace root filesystem.
The command resolves either one ref (optionally guarded by its expected
generation) or one explicit immutable snapshot, materializes the retained
portable generation, then closes the CLI-owned caller receipt, strict CAS
anchors, and catalog connection before it prints success. Receipt closure
releases native authority without deleting the published output. The printed
handoff starts a separate query-only process with `codenib mcp --artifact
<output> --repository <owner/repository>`. A post-publication failure may
therefore leave a valid missing-only output; the CLI warns when the path exists
rather than deleting it.

The first catalog-selected cold-start route is now exposed by `codenib mcp`
with the same explicit catalog, preprovisioned CAS, private workspace,
repository, ref-or-snapshot, and missing-output inputs. It resolves one ref
exactly once (or selects one immutable snapshot), materializes the portable
generation, and creates the query binding through the publication receipt's
synchronous authenticated reader rather than reopening an unbound path. The
complete `ServerContext` is installed in a caller-precreated, PID-bound,
one-shot runtime owner while that reader is active. Catalog, CAS, and retained
path authorities close before stdio serving begins; the workspace receipt and
any selected repository-source binding stay active for the entire `mcp.run`
lifetime. Shutdown first detaches the global context, then closes the runtime
context, source authority, and receipt in that order. A forked child revokes
source before receipt without entering an inherited context lock. Receipt
closure does not delete the materialized output.

Without `--repo`, this startup remains query-only and source-disabled. With an
explicit `--repo <checkout>`, the CLI pins the checkout's lexical anchor, every
ancestor, and root before provider or storage work. It rejects lexical,
same-object, ancestry, and Linux mapped-physical overlap with the catalog, CAS,
workspace, or output. Source capture then opens its independent retained read
authority while the artifact publication reader is active and proves that the
new anchor-to-root chain is the same still-live chain as the preflight pin
before scanning. The preflight pin closes before serving; the independently
captured source authority supplies authenticated BM25 content and `read_source`
for the server lifetime.

A selected BM25 view loads from persisted canonical documents. A selected
portable vector remains FAISS-parser inert and reports its existing
authorization error, and a source-bound portable context still does not gain
native LSP authorization or commit attestation. The route therefore requires
BM25 and does not advertise a vector-only runtime or full ordinary-manifest
equivalence. It never mints native vector authorization from cache-ingress
hashes. The selected receipt fixes one local generation but is not a catalog
snapshot lease or CAS GC pin. The route does not poll or re-resolve a ref,
replace a live context, hold storage connections while serving, provision
storage, delete output, or add request-level pins. Those replacement and
in-flight lifetime contracts remain M3 work, while durable retention and
reclamation remain M5 work.

The reader-native source seam is now exposed by the explicit retained
`codenib mcp --repo` route. `bind_context_artifact_reader` captures and verifies
source fingerprint v2 while the publication receipt's authenticated reader is
active, and the retained ref/snapshot loaders accept the live preflight root
authority in addition to its lexical path. The one-shot runtime owner retains
source authority across cancellation, closes context, source, and receipt in
that order in the parent, and revokes source before receipt in a forked child
without taking an inherited context lock. Portable vector payloads remain
native-parser inert. The v3 report-only protocol now records source-bound BM25
and full-runtime compatibility projections separately. Canonical source-bound
measurements, ratified budgets, and any decision about full ordinary-manifest
compatibility are still missing; this explicit route does not change a default.

The standalone materialization command and retained MCP cold start do not
initialize or populate the control plane, import a legacy cache, build views,
advance refs, or make retained storage the default. Benchmark-backed promotion
of the explicit compiler and runtime routes remains outstanding M1 work; graph
and Zoekt cache ingress and fenced job publication remain M2 or later work.
Strict BM25 replacement now has its `provider-bound-exact` production provider.
The strict producer passes its separately active source owner outside the
immutable request; a private one-shot gate binds it to the same request,
destination-binding object, operation, native owner, and workspace before Local
candidate mutation. Local then selects the protocol-v6 dual-root provision and
publication seam. This completes Gate C without changing any compiler/runtime
default, automatic orphan reclamation, crash recovery, or the remaining M1
evidence requirements.

#### Retained storage promotion protocol

M1 promotion is split into evidence, policy, and configured-default changes so
that collecting a fast result cannot silently approve a production route:

| Gate | Required result | Current status |
| --- | --- | --- |
| A1 | Run the fixed, report-only retained storage harness and record route-specific BM25-plan, public-query, source-read, and runtime-authority projections without conflating their scopes. | Implemented as a five-cell v3 evidence protocol; it cannot promote a route. |
| A2 compiler | Record canonical compiler receipts for the approved subject/media matrix and ratify numeric compiler policy from measured results. | Pending real measurements. |
| A2 query-only runtime | Record canonical direct-artifact versus retained receipts and ratify numeric query-only runtime policy. | Pending real measurements. |
| A2 source-bound BM25 runtime | Record canonical ordinary-manifest versus retained-with-source receipts and ratify a narrowly scoped source-bound BM25 policy. | Pending v3 canonical measurements; this scope is narrower than ordinary-manifest equivalence. |
| A2 manifest compatibility | Preserve ordinary manifest MCP public behavior, provenance, and authority when selecting retained storage. | The explicit source-bound CLI route is implemented, but portable provenance, native-LSP policy, and incomplete non-BM25 coverage keep full manifest replacement blocked. |
| B1 | Promote BM25 compiler publication to a configured default. | Pending A2 compiler. |
| B2 | Promote query-only BM25 retained cold start to a configured default. | Pending A2 query-only runtime; this does not replace source-bound manifest MCP. |
| B2 source-bound | Promote a specifically scoped source-bound BM25 retained cold start. | Pending A2 source-bound BM25 runtime; it cannot satisfy the full manifest-compatibility gate. |
| C | Supply the `provider-bound-exact` strict BM25 native provider. | Complete. The request still carries only its immutable receipt-derived binding. `run_strict_workspace(...)` accepts the separately active exact source owner and gives Local only a PID-bound, callback-scoped one-shot gate tied by object identity to that request, binding, operation, native owner, and workspace. Local captures and leases before the gate synchronously binds the source, provisions only through the handed-off workspace, and publishes only through the protocol-v6 dual-root seam. The displaced incumbent remains a reopenable `linux-renameat2` orphan. Automatic GC, a crash journal, hostile same-UID defense, and default-route promotion remain outside Gate C. |

An initial v3 1x0 readiness smoke stopped before completion because strict BM25
applied per-document node and token defaults to its whole-file lexical prepass.
The merged correction derives only the aggregate node and token budgets from
the authenticated `documents.json` record size. After #682 merged on main at
`4c907bf3588b40cbf67d3dd98c3db389ab093e62` with tree
`50e7455cac76e224df46c8a6b603c54645d43b89`, a fresh 1x0 readiness run
completed all 30 cells, observed 60/60 expected inner route processes with all
60 unique, and recorded 480/480 true per-sample safety values. The 30 cells'
240/240 aggregate safety summaries were also true; those summaries fold the
same per-sample values and are not additional checks. Of the 30 primary cell
projections, 24 passed. The six `runtime-cold` source-disabled compatibility
sentinel cells, whose candidate arm is query-only, were red on their primary
full-runtime projection. Separately, all six `runtime-cold-source-bound` cells
passed their primary content-authority projection while their full-runtime
projection remained red because manifest provenance differs. Consequently both
`manifest-runtime-compatibility` and `source-bound-manifest-compatibility`
remain blocked.

The sanitized, path- and PID-free
[aggregate readiness receipt](experiments/artifacts/retained_storage_a2_readiness_v3_1x0.json)
is checked in with the roadmap. It retains the source report SHA-256
`b4b2246982a29de0860dcaeaee5d1e1363e30f2002aa7576f6c8dcdc8c02a29a`
and sidecar SHA-256
`33575263b8dd37324b4c8ec2f9a87f49a64c6c9ca3ad007dad384d69f3432aa9`.
The controller return code 1 was solely the expected `measurement protocol is
not canonical v3` sentinel because the 1x0 iterations, warmups, and resulting
sample count are noncanonical; it was not an operational, isolation, or safety
failure.
The prepass correction keeps the whole-file byte, depth, key, string, and atom
limits and the per-document complexity caps unchanged. This readiness receipt
is not A2 numeric evidence, ratifies no budget, and promotes no compiler or
runtime route or default. The canonical 20x4 measurement was not launched
because an unrelated `selftune` CPU workload made the host non-representative;
it remains pending a quiet-host window.

The A1 harness fixes the BM25 `fast` compiler/runtime comparison rather than
accepting arbitrary route substitutions. For compiler cold start, arm A runs
`codenib index --preset fast` from an empty cache and arm B runs the same build
with retained publication; storage provisioning is outside the measured route.
For compiler current-cache behavior, both arms start from equivalent verified
current caches: A measures the ordinary update and B measures the retained
exact retry with its original expected ref generation, which must not advance
the ref again. Runtime cold start deliberately has three cells. The original
source-disabled sentinel keeps arm A on the ordinary source-bound manifest MCP
context and arm B on the source-disabled retained context. The query-only cell
prepares a direct portable context artifact outside the stopwatch, then
compares its source-disabled MCP startup with retained ref resolution and
materialization. The source-bound cell keeps arm A on ordinary manifest MCP
startup and gives arm B the retained ref plus the same explicit repository
checkout. It measures the end-to-end source-bound replacement question, but
reports narrow BM25/content-authority parity separately from full runtime
compatibility. All three cells use the real parser and command handler. The
harness replaces only `mcp.run` with a ready callback that executes the fixed
BM25 queries, captures public `get_manifest`, and attempts one fixed public
source-read probe. Source-bound arms must return the authenticated window;
query-only arms must return the exact source-unavailable response. All of this
work and normal context cleanup remain inside the runtime stopwatch.

Every paired round alternates AB/BA order. For each arm, the controller starts
a short-lived outer sample worker. Every arm receives a fresh `CODENIB_HOME`
and compiler cache below the selected media root; candidate arms additionally
provision a fresh catalog, CAS, workspace, and output, while legacy arms never
touch those retained authorities. The worker then launches a fresh inner route
process. The canonical matrix fixes one small, medium, and large subject plus
exactly two approved, physically distinct media classes, with four warmups
followed by 20 measured rounds.
Five cells, two arms, three subjects, and two media classes therefore require
1,440 fresh inner route processes. The source-bound cell adds 288 samples, or
25 percent, over v2; the query-only and source-bound cells together add 576
samples over the original three-cell protocol.
Each route receipt records
`route_wall_seconds`, `process_wall_seconds`, `cpu_seconds`, `peak_rss_bytes`,
`io_read_bytes`, `io_write_bytes`, `payload_bytes`, and `payload_files`.
Peak RSS uses Linux `VmHWM`, I/O uses `/proc/self/io`, and aggregates use the
median for p50 and nearest-rank p95.

The cold labels deliberately describe CodeNib state, not the operating-system
page cache: compiler cold starts from an empty compiler cache, while runtime
cold starts a fresh process with no loaded context. The harness does not flush
or control filesystem page-cache state. AB/BA ordering balances that nuisance,
and the report records it as `uncontrolled`; A2 must assess its receipts with
that limitation intact.

The normalized BM25 plan identity is artifact evidence, not a claim that the
public manifest payloads are equal. Runtime receipts separately preserve the
complete ordered BM25 results, including optional source `content`, scores,
and locations. Query-only arms must both reject `read_source`; source-bound
arms execute the same bounded public source-read probe. Its content window is
part of the narrow content-authority projection, while the complete public
response, including repository provenance, remains in the full-runtime
projection. Public manifest state, artifact origin, source authority, loaded
views and errors, LSP policy, native authorization, and commit-attestation
state likewise remain explicit full-runtime evidence. Before hashing, the
controller validates the exact public response and masks only authenticated
per-sample paths and finite build timestamps; it does not remove artifact or
runtime fields. The harness never drops `content`, hides delivery provenance,
or hard-codes a particular mismatch.

The v3 report aggregates compiler, query-only runtime, the original
source-disabled manifest sentinel, source-bound BM25/content-authority, and
source-bound full-runtime compatibility as independent projection-aware
tracks. Each projection reports within-arm stability and pair equality; stable
but different arms remain red. Both compatibility tracks also keep
`scope_complete=false`, so even future pair equality cannot authorize
ordinary-manifest replacement without broader view and tool coverage.
Completing all samples safely produces a complete report even when a
compatibility track is red. Operational, schema, isolation, cleanup, source,
or postflight failures instead fail the measurement. A complete report is not
a passing compatibility decision and never authorizes promotion. This
separation lets A2 compiler, query-only runtime, and narrowly scoped
source-bound BM25 evidence advance independently without laundering the
blocked full-runtime replacement track.
The controller therefore exits zero after successfully writing a report whose
status is `complete`, even if top-level `passed` is false. It exits one after
successfully writing a `failed` measurement report. Exit two is reserved for
hidden-worker failures and command/report-output failures that cannot be
represented by a successfully written report. Red parity or incomplete
compatibility scope alone remains exit zero.

A1 intentionally has no approved numeric thresholds or threshold override.
Its policy is unratified and every report sets `promotion_eligible=false`, even
when a track passes and all measurements complete. Only A2 measurements over
the fixed approved subject/media matrix may establish canonical receipts and
ratify numeric thresholds. B1 requires the compiler track; query-only B2
requires the query-only runtime track. A future source-bound BM25 default
requires its own A2 policy and remains narrower than ordinary-manifest
equivalence. The reader-native source seam and topology-safe explicit route now
exist, but replacing ordinary source-bound manifest MCP remains blocked until
retained startup preserves the same public behavior, artifact provenance,
native capability policy, and required view coverage in compatibility
evidence. Landing either the route or the harness does not approve a default.
This leaves M1 in progress.

These gates do not move the existing milestone boundaries. The generic fenced
publication primitive is now part of M2, while generic prepare-only
builder/profile adapters and production entry-point wiring remain; live bundle
replacement and in-flight request pins remain M3, and evidence retention and
reclamation remain M5. Gate C may proceed in parallel, but the report-only
harness neither implements nor waives it.

Schema v2 now adds
canonical idempotent job requests, immutable
per-view request mappings, bounded retry state, and database-clock fenced
per-ref leases.  Catalog reads revalidate the normalized view rows against the
canonical request; the M2 publication transaction now repeats that gate before
associating outputs.  An explicit acquire may atomically retire an expired
holder while taking over its slot; this slice adds no background reaper and is
not wired to the compiler or Web entry points. Catalog-selected cold-start
runtime is now available only through the explicit local route above, while
benchmark-backed default compiler/runtime promotion remains an outstanding M1
deliverable; fenced publication is not yet wired into production builders or
entry points.

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
The compiler-cache importer borrows this lease existing-only: both the cache
directory and `.index-compiler.lock` must already have been created by a real
producer run, and import never creates either one. Manually adding a lock file
cannot establish prior cooperative discipline. The importer acquires the lease
itself; same-thread re-entry for the same cache inode fails fast, while nested
leases for different caches remain valid.

Schema v2 deliberately retains complete job aggregates.  Duplicate-insert
guards reject `REPLACE` of jobs, requested views, and persistent lease slots
even from ordinary SQLite connections whose connection-local recursive trigger
setting is disabled.  Catalog connections additionally enable recursive
triggers; direct deletion of requested view rows and cascading deletion of
their parent job remain blocked.  A future retention/GC milestone must add an
explicit aggregate-deletion migration and policy rather than bypassing these
audit guards. Schema v5 replaces the temporary update-to-success gate with the
publication-closure gate described below; inserting a job directly in the
successful state remains forbidden.

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

Schema v5 adds the versioned `codenib.index-job-publication.v1` audit closure
and an atomic `publish_job_outputs` primitive. One `BEGIN IMMEDIATE`
transaction revalidates the canonical job request, requested view/profile
bindings, live lease owner and fencing token, cancellation state, expected ref
generation, exact primary/member object metadata, staged generations, complete
snapshot, and retained-response bounds before changing the ref. It then records
the immutable closure, succeeds the job, and releases the lease in the same
commit. Exact replay authenticates the stored owner, fence, canonical closure,
snapshot, and historical ref outcome before consulting the now-released lease;
it returns the committed result without advancing the ref, including after a
later publication has advanced that ref.

Schema v6 adds the local durable execution-control slice without selecting a
production builder. The backend-neutral prepare-only worker described in M3
consumes that contract without granting executors catalog publication
authority. Lease acquisition now records an immutable attempt start before the
job becomes running. Requeue, failure, and cancellation write one immutable
non-success closure; success still has no generic finish API and can only come
from the schema-v5 publication transaction. Existing v5 history is preserved
behind an immutable per-job legacy attempt baseline. In addition to the hidden
attempt count, that baseline attests the initial creation time, the legacy
content high-water mark, and any legacy start time. Only a currently active v5
attempt is backfilled, while later v6 starts must form a complete contiguous
history. Exact, unambiguous v5 non-running lease half-states are closed during
migration; ambiguous state fails migration and reopen closed.

Schema v6 also maintains one private durable execution-content clock. Its
singleton value must equal the exact maximum of the immutable baseline,
attempt-start, cancellation, event, completion, and publication witnesses.
Fresh creation, acquisition (including expired-holder retirement),
cancellation, event, completion, and publication paths authenticate exact or
terminal replay first. A genuinely new mutation then advances the singleton to
one database-clock tick at or above its causal floor and binds every related
execution-content time in that transaction to the frozen value. A backward or
moving clock
therefore produces a stable catalog conflict and rolls back both the clock and
domain rows; exact replay returns committed history without consulting or
advancing the clock.

The v6 runnable scan is deterministic and advisory, using the database clock
and `(created_at_ms, job_id)` keyset pages; lease acquisition remains the only
claim. Newly inserted jobs are exact queued, zero-attempt records at one
content-clock tick. The scan hides jobs whose durable times are ahead of the
current wall clock. Existing-only reopen does not compare history with the
current wall clock: it checks the exact immutable-witness maximum and every
derived-time ceiling against the durable singleton. Canonical history thus
reopens during clock rollback, while a bypassed future value without matching
immutable evidence fails closed. New active lease slots bind acquisition,
heartbeat, update time, and bounded duration to the same content-clock claim;
released slots can arise only through fenced release, retain a positive fence,
and remain below the durable content ceiling without becoming a separate fresh
mutation floor. Heartbeat atomically renews exact owner/fence/attempt authority
and observes cooperative cancellation. It remains an independent lease-clock
domain and may advance beyond the content singleton; the next related fresh
content mutation must wait until the database clock reaches that heartbeat.
An immutable internal cancellation marker records whether v6 cancellation
terminalized a queued job or observed exact running
attempt/owner/fence/heartbeat authority. A cancelled modeled attempt can close
only as cancelled; requeue and failure require an uncancelled attempt. The
marker prevents a raw flag rewrite from erasing already-recorded cancellation
while the canonical schema and marker remain present. Migrated v5 cancellation
is explicitly legacy-unattested rather than inventing earlier heartbeat
evidence. This is corruption detection, not a security boundary against a
same-user writer who can remove both guards and evidence and reconstruct the
schema.

Attempt events are immutable `progress` or per-view `view_result` records with
attempt-local idempotency keys, database-assigned sequence/time, and at most
one result per attempt/view. Each attempt is limited to 256 events. Canonical
payload JSON is limited to 16 KiB, depth 16, 1,024 nodes, and 128-character
keys, and uses the shared secret-field classifier. Root mappings are detached
with bounded iteration without trusting reported length; nested containers and
scalars must be exact JSON values. Event sequence numbers are allocator-only;
attempt capacity and signed-int64 allocator exhaustion conflict before any
insert. Every non-success completion and successful publication atomically
records an immutable closure frontier containing the exact event count,
maximum sequence, and maximum event time. Exact replay and reopen recompute
that frontier, so even an equal-millisecond post-closure event fails closed.
Within an attempt, event, cancellation, and closure times obey a nondecreasing
database-clock causal order. Heartbeat remains a separate nondecreasing
lease-clock domain: after database-clock rollback it may lag an already
committed content/cancellation high-water mark, but cannot authorize a later
event, completion, or publication below that content floor. Reopen also
revalidates legacy baselines, contiguous starts, exactly one closure per closed
v6 attempt, active-lease/open-attempt equivalence, terminal lease absence,
event bounds, and canonical payloads. New attempt, sequence, page, cursor,
duration, and fencing boundaries require exact integers; persisted identities
reject values outside signed SQLite int64 before SQL execution.

The receipt-retaining coordinator freezes exact artifact receipts and keeps
them retained across the complete catalog transaction and returned-result
attestation. Required views must be present, optional views may be omitted, and
extra, duplicate, or profile-mismatched outputs fail closed. Publication is
bounded to 64 output views and 32,768 aggregate compound members, with the full
closure also subject to the retained response node/text budget. Per-view
semantic compatibility remains the responsibility of the outstanding adapters;
an explicit profile label alone does not establish byte-level semantics.

Declarative triggers make publication rows and their published job/snapshot
closure immutable and reject invalid partial publication states where SQLite
can express the invariant. Arbitrary same-user writes through a raw SQLite
connection are not a security boundary: a client capable of reproducing a
fully valid transaction is outside this contract. Catalog replay and
existing-only reopen nevertheless revalidate the complete durable aggregate
and fail closed when publication dependencies or canonical closure data have
been corrupted.

The first M2 builder-profile adapter now publishes one exact current BM25
compiler-cache view into a caller-created, caller-acquired fenced job. It
requires a single required `full` BM25 request whose repository, retained
source revision, and complete portable builder profile match the strict
recapture/import plan. The mutable cache lease encloses raw BM25 recapture and
whole-context evidence publication, then releases before the retained context
is converted to one canonical view-bundle receipt plus every independently
reachable member receipt. `publish_job_artifacts` remains the sole catalog
mutation: it retains that complete receipt set through the atomic job/snapshot/
ref transaction and returned-result attestation. The job path publishes only
the BM25 generation, not the direct M1 importer's internal manifest projection.
The caller still owns repository/source/profile registration, job creation,
lease acquisition, and all receipt owners; the adapter adds no worker, CLI,
runtime, vector, graph, or default-route wiring. Exact retry remains historical:
a committed BM25 job returns its original snapshot without moving a ref that a
later valid publication has already advanced.

The matching vector adapter now publishes one exact current schema-8 compiler
cache into the same caller-created, caller-acquired fenced job boundary. It
requires exactly one required `full` vector request and derives the complete
provider/model/route/revision/load-policy compatibility profile from the
canonical cache import plan rather than accepting those identities as caller
options. Strict recapture copies the canonical root config and each non-empty
level's config, ordered JSON documents, and FAISS bytes; the resulting bundle
and every per-file member receipt remain retained through atomic publication.
FAISS bytes stay parser-inert, legacy pickle state is excluded, and this path
neither loads an embedding model or client nor grants native vector runtime
authorization. Exact replay returns the job's historical snapshot after later
ref advancement. This remains an explicit ingress adapter only: it adds no
builder, worker, CLI, runtime, graph, or default-route wiring.

The same BM25 and schema-8 vector recapture paths now also expose an explicit
prepare-only worker bridge. It authenticates the complete running job request,
requires exactly one required `full` view, recaptures and ingests the immutable
bundle closure into the worker's retained object store, and returns an
`IndexJobExecutionResult` without accepting a catalog, lease owner, fencing
token, ref, or generation authority. The durable worker therefore remains the
only final publisher. This bridge consumes an already-current compiler cache;
it is not a source builder or production resolver, and every attempt still
requires fresh caller-owned source, workspace receipt, and destination
authorities. The existing self-publishing adapters remain compatibility entry
points rather than being called from the worker. Its stop token is propagated
through cache-lock waits, repository inventories and read sessions, portable
artifact scans, workspace refresh/seal/staged validation, bundle planning, and
CAS ingestion. Provider support and gate activation precede one final
pre-entry poll; the interruptible provider protocol then requires the same
callback, so a legacy provider cannot silently run without it. Local carries
the stop through plan detachment, replacement-source receipt and incumbent
scans, native provision/replacement loops, descriptor adoption, skeleton
capture, and native directory sealing. Returned records, receipts, and
receipt-consumption postflights are attested before a newly observed stop can
win; the staged namespace transition through authenticated receipt creation
remains an uninterruptible commit section, and an exact cooperative stop does
not poison the retained repository source.

The compiler-cache bridge now also has a resource-scoped resolver seam. It
attests the canonical running request before opening attempt resources, accepts
only one required `full` BM25 or vector view, binds the exact
receipt-retaining object-store instance supplied to the enclosing worker, and
enters a caller-provided context manager for fresh source and workspace
authorities. The scope exits on success and every executor failure; a factory
that returns an executor attached to a different object store fails as an
integrity error before cache, workspace, or CAS work. This seam remains the
lifecycle boundary rather than a path-discovery capability; the local binding
below must receive its trusted targets explicitly.

The first trusted local resource factory now implements that resolver seam for
an explicit, caller-authorized target set. Each target freezes one canonical
namespace/repository identity, repository root, existing compiler cache, private
local workspace provider, and environment snapshot; durable repository IDs are
looked up only in that set and never interpreted as paths. Every attempt gets a
fresh retained source binding, receipt owners, and nonce destinations. Scope
exit closes all authorities and atomically isolates each exact owned output for
later quiescent GC instead of recursively deleting by path; an incomplete
cleanup is promoted to a storage-integrity failure with a retryable owner. The
factory rechecks cooperative stop state around cache selection and source
capture and fails before workspace/CAS work when the current source revision no
longer matches the job.

The production CLI binding exposes that target as `codenib jobs run-once` and
`codenib jobs run`. Both freeze the same existing-only repository/cache/catalog/
CAS/workspace topology used by retained cache import, open independent exact
SQLite sessions for each main pass and heartbeat, and retain the strict local
CAS for the whole invocation. A trusted candidate filter runs on a detached
canonical job before owner allocation or lease acquisition, so foreign
repositories and unsupported view requests remain untouched. `run-once`
examines one bounded advisory page and executes at most one eligible job. The
continuous scheduler freezes an attested catalog insertion watermark for each
cycle, carries an attested keyset cursor across its bounded pages, wraps only
after that frozen runnable keyspace is exhausted, backs off after complete idle
cycles, and supports cooperative shutdown. Jobs inserted after the watermark
are deferred to the next cycle, so continuous larger-key writes cannot starve a
wrapped retry or make a configured cycle limit unbounded. SQLite schema 7
allocates that watermark from an explicit immutable `AUTOINCREMENT` job
sequence, validates a gap-free job-to-sequence closure, and backfills existing
jobs in canonical creation order; it therefore remains stable across `VACUUM`
instead of depending on mutable implicit rowids. Cache-building adapters,
job-triggered runtime registration, and default routing remain absent. The Web
runtime now has the generation-safe refresh boundary described under M3.

### M2: Immutable generation publication

Status: in progress. The receipt-retained, fenced SQLite publication primitive
and the explicit BM25 and schema-8 vector compiler-cache adapters, including
their prepare-only worker bridge, are implemented; graph, generic source
builders, cache-building execution, and remaining runtime wiring remain.

- Make every builder write to a unique staging generation.
- Add per-view profile adapters that fail closed on incomplete compatibility
  inputs and prove every semantic axis participates in the profile identity.
  The exact BM25 and schema-8 vector compiler-cache profile paths are
  implemented; graph and generic builder adapters remain.
- Publish whole-view BM25, FAISS, and graph artifacts through the catalog and
  object store without changing their ranking/query semantics. BM25 and
  parser-inert FAISS now have explicit fenced compiler-cache ingress; graph
  ingress remains.
- Use the implemented receipt-retaining coordinator for each adapter so exact
  digest, size, storage key, and media type remain retained through atomic
  catalog publication; catalog metadata alone must never make missing bytes
  publishable.
- Move refs only after object and compatibility validation.
- Prove interrupted and failed builds leave the previous snapshot usable.

### M3: Jobs and runtime hot switching

Status: in progress. Schema-v6 durable execution control is implemented for the
local SQLite catalog. The backend-neutral prepare-only whole-job worker is
implemented and verified with file-backed SQLite integration coverage. An
explicit one-view compiler-cache executor now supplies parser-inert BM25 or
schema-8 vector artifacts without catalog authority. Its resource-scoped
resolver and trusted local target factory guarantee attempt-local cleanup,
same-store binding, and exact configured repository/source identity, while
the explicit CLI can run one bounded pass or a cursor-fair continuous scheduler
over the current cache. The Web registry now supports complete-candidate RCU
replacement and request-lifetime generation leases. Source builders, the #266
job handoff and update path, success-triggered runtime refresh, MCP, UI controls,
and default routing remain absent.

- The backend-neutral worker now owns advisory scan/claim, per-attempt task
  authority, independent heartbeat sessions, cancellation precedence, bounded
  progress and per-view events, non-success closure, and exactly one
  receipt-retained whole-job publication. Resolvers and executors prepare
  artifacts only and receive no supported catalog publication capability. This
  API boundary is not a sandbox for deliberately introspective in-process
  Python code.
- One `run_once` pass examines one bounded advisory page and tries its candidates
  in canonical order. An optional trusted filter now attests a detached
  candidate before owner allocation or claim, rejects non-boolean, failing, or
  mutating filters as integrity alarms, and lets a scoped worker leave foreign
  or unsupported jobs untouched. The separate `run_page` surface returns the
  selected candidate cursor after work or the attested final-page continuation
  after a fully examined page; either continuation must advance beyond its
  input. Before traversal the local scheduler freezes the catalog's current
  immutable job-insertion sequence and supplies that exact cycle token to every
  page. SQLite schema 7 persists this as a gap-free explicit sequence rather
  than an implicit rowid, including deterministic migration backfill and
  `VACUUM`-stable allocation. The scheduler therefore traverses a finite
  keyspace even while larger-key jobs are continuously inserted, wraps requeues
  into the next fair cycle,
  exponentially backs off only after complete idle cycles, and stops
  cooperatively without making the legacy one-page call unbounded.
- File-backed SQLite sessions in one interpreter coordinate existing-only
  validation, transactions, and connection close by resolved catalog path, so
  a heartbeat cannot invalidate a concurrent cancellation or worker-session
  startup copy. Exact same-inode main/WAL/SHM drift is recaptured with a bounded
  attempt count, and each retry start and backoff is limited by a
  busy-timeout-derived deadline; structural namespace, schema, identity, and
  cleanup failures remain immediate fail-closed errors. Independent processes
  must pre-open their SQLite sessions or quiesce high-frequency writers until
  the future runtime adds a cross-process startup coordinator.
- Ordinary response loss is reconciled against the immutable attempt closure
  before the worker reports a disposition. Explicit storage-integrity alarms
  remain infrastructure failures even when the catalog mutation committed;
  they are never downgraded to an ordinary success or builder failure. A
  retaining-store cleanup failure after an attested publication callback is
  surfaced as an integrity alarm rather than mistaken for catalog response
  loss, while direct publication callers retain their established exception
  identity contract.
- Publication preflight and exact receipt revalidation remain covered by the
  attempt heartbeat. The pump settles only inside the retained callback, where
  the worker checks heartbeat fault, cancellation, and authority, performs one
  final fenced heartbeat, and then publishes before releasing receipt
  retention. Slow object hashing therefore cannot expire an otherwise healthy
  worker and force a duplicate attempt.
- Add prepare-only source-builder adapters, then wire the worker into runtime,
  Web, MCP, and default routes. The trusted local factory and continuous CLI can
  recapture an already-current single BM25 or vector cache view under the
  worker, while the older self-publishing ingress APIs remain compatibility
  paths and are never nested inside the worker.
- Complete the #266 job and update APIs with accurate incremental versus rebuild
  behavior; the first read-only status slice is described below.
- `RepoRegistry.load_all()` now reconciles each complete registry snapshot,
  retires repositories removed from that snapshot, and leaves a healthy
  generation live when its still-declared replacement fails. First startup
  remains lazy; any replacement of an active generation authenticates and
  prepares its complete retrieval, Ask, and advertised graph surface before a
  single lock-protected pointer swap. Snapshot reload, explicit refresh, and
  shutdown are serialized. Web requests pin one coherent generation through
  every offloaded operation and response construction, including cancellation
  settlement; list metadata and incremental statistics use one pinned snapshot.
  Old, removed, or unpublished generations retain vector/source cleanup
  authority until the final lease and all retryable cleanup complete, with
  cancellation-class cleanup failures never demoted behind ordinary errors.
  Bundle-derived Wiki, edge-label, and commit-window helpers are generation
  keyed and stale entries are pruned after replacement, removal, and shutdown.
  The remaining #266 work is to invoke this refresh boundary only after an
  attested update job succeeds and expose its status/results to the UI.
- The first #266 read slice exposes one pinned, detached status snapshot for
  exactly BM25, vector, and symbol-graph surfaces. It reports manifest/current
  commit drift and retained incremental metrics without exposing mutable bundle
  state. Update capability is explicit and defaults to `unavailable` until an
  owning Web job service is configured, so the read API never implies that the
  current server can write or incrementally patch an index.
- The durable job catalog now has an additive, read-only active-job query for
  Web status consumers. It prefers the fenced running attempt and otherwise
  returns the oldest queued job for the exact repository/ref, while terminal
  jobs and bounded events remain addressable by job ID. This avoids an
  in-memory repo-to-job map that would lose state across Web process restarts.
- An explicitly injected Web reader can now project authorized durable jobs and
  at most 64 events without exposing worker owner IDs, fencing tokens, or raw
  executor errors. Each event is rebound to the exact job, visible attempt, and
  requested view; only explicitly public scalar metrics cross the Web boundary.
  Repository status overlays queued/running jobs only after releasing its RCU
  bundle pin. The default server still has no job reader or writer, so this read
  API returns unavailable until production storage bindings are configured.
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
- Symbol-graph builder schema v6 binds the optional persisted SCIP occurrence
  index with an exact `lsp_occurrence_artifact` receipt. Graph consumers copy
  and authenticate that sidecar from the same owned generation as `graph.pkl`
  before deserializing it; an unreceipted ambient sidecar is never discovered.
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

### M6: Optional server storage adapters

Status: deferred; demand gate not activated.

- Activate the PostgreSQL catalog and S3-compatible object-store gates
  independently. Neither adapter requires the other.
- Before activating either gate, document a concrete deployment constraint
  that the canonical SQLite WAL catalog or local filesystem CAS cannot meet,
  and retain representative workload plus operational acceptance evidence.
- Once activated, implement the same `IndexCatalog` or `ObjectStore` contract;
  PostgreSQL retains transactions, row locking/`SKIP LOCKED`, leases, and
  namespace-scoped authorization, while S3/MinIO retains publication,
  verification, materialization-cache, and deletion-recovery semantics.
- Validate contention, backup/restore, migration, quotas, audit, and failure
  injection before declaring an activated adapter supported.
- Keep M1-M5 and embedded completion independent of this deferred milestone.

### M7: Managed semantic and optional shared ANN

Status: pending and benchmark-gated.

- Keep managed embeddings keyed by namespace, immutable embedding fingerprint,
  and strong input digest.
- Preserve local, BYO, managed, no-model, and artifact-only routes.
- Add a shared ANN read model only if measured workload demonstrates that local
  FAISS materialization is the limiting cost.
- Keep artifact export and local fallback as compatibility gates.

## Completion Gate

The embedded storage program is complete when M0-M5 are implemented, locally
and remotely verified at their required test tiers, documented, and reconciled
with their issues and PRs. M6 and M7 are optional extensions and do not hold
embedded completion open unless their demand or benchmark gates are explicitly
activated. Passing one backend test, landing the catalog schema, or publishing
a single snapshot is not completion of the embedded objective.
