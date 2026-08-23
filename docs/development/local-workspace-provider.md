---
title: Local Workspace Provider
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Local Workspace Provider

`LocalWorkspaceProvider` is CodeNib's production Linux implementation of the
strict workspace contract. It publishes one fully validated directory into a
missing destination and transfers the generation to a caller-owned
`PublishedWorkspaceReceiptOwner`.

The provider is not enabled by CodeNib's default compiler or runtime path. Four
explicit routes use it: `codenib index --publish-retained` builds and publishes
current BM25/vector views in one compiler-cache lease, `codenib artifact
import-cache` recaptures an already existing selected cache, `codenib artifact
materialize` publishes a retained catalog ref or immutable snapshot to a
missing portable-artifact directory, and retained `codenib mcp` cold-start
materializes and holds one such generation for a single stdio server lifetime.
The strict BM25 replacement path still requires the unsupported
`provider-bound-exact` destination mode.

## Publish a normal index build

Use `--publish-retained` on the normal `codenib index` command to build or
update BM25, vector, or both and publish the exact compiler result as one ready
snapshot. This is the first production compiler-to-retained route; without the
flag, `codenib index` keeps its existing local-cache behavior and output.

The route requires the same existing initialized SQLite catalog, fully
preprovisioned strict `LocalCAS`, and private owner-only Linux workspace root
described below. It never provisions those authorities. It uses CodeNib's
canonical per-repository compiler cache; a first invocation may create that
cache, but the cache's nearest existing real ancestor and physical storage
topology are checked before creation. Inside the single compiler lease, the
route freezes the newly created cache identity before the build, verifies it
before publication and again after recapture, and rejects a changed or aliased
cache instead of importing a different generation.

All retained options are opt-in as one group. `--rebuild` is intentionally
incompatible because a post-commit retry must reuse the current compiler
generation rather than force new bytes. Selected views must be exactly BM25,
vector, or BM25 plus vector; graph and Zoekt publication remain later
milestones. For a first BM25-only publication:

```bash
codenib index /srv/src/repository \
  --preset fast \
  --publish-retained \
  --catalog /var/lib/codenib/catalog.sqlite3 \
  --cas-root /var/lib/codenib/cas \
  --workspace-root /var/lib/codenib/workspaces \
  --repository owner/repository \
  --ref main \
  --expected-generation 0
```

Use `--preset semantic` or `--view bm25,vector` with a configured embedding
route for the combined portable view set. The command captures the retained
source before compilation, then performs update-or-create, exact serialized
manifest binding, all selected view plans and publications, and one context
publication under the same cache lease. The retained importer performs no CAS
or catalog data-plane operation until that lease is released; support,
existing-storage open, and static contract probes may run earlier. A failed or
stale selected view stops before retained publication.

Each invocation uses fresh missing-only evidence destinations. Published
evidence is warned about rather than deleted if a later step fails. On success,
the normal index summary is followed by the snapshot, ref generation, evidence
paths, and an immutable-snapshot `artifact materialize` command. If a result
might have committed before it was observed, retry without `--rebuild`, with
the original `--expected-generation`; unchanged source and compiler
configuration resolve to the same snapshot with `changed=False` rather than
advancing the ref again.

This remains explicit offline dual-write. Keep the source/cache and storage
namespaces trusted and quiescent, and apply the payload-size and storage-media
benchmark gate described below before enabling it by default or treating it as
a latency-sensitive worker path. Catalog-selected MCP cold start is available
only through the explicit route below; runtime hot switching remains M3.

## Import compiler query views

`codenib artifact import-cache` imports an exact current BM25 view, vector view,
or both from an existing `IndexCompiler` cache as one ready retained snapshot.
Omitting `--view` preserves the BM25-only default. Repeat `--view`, or pass a
comma-separated value such as `--view bm25,vector`, to select views explicitly;
the CLI canonicalizes the result to BM25 then vector. This remains an offline
bootstrap for an already existing cache: it does not enable retained
publication for an `index` invocation that lacks `--publish-retained`, import
graph or Zoekt views, run an M2 fenced job, or hot-switch a runtime.

Prepare these authorities before running it:

- The positional repository must be the source used to build the cache. The
  retained source-fingerprint-v2 identity must match the manifest, whose commit
  must be a full lowercase 40-character Git SHA. The manifest must contain an
  exact current entry for every selected view. BM25 must have exact
  fingerprints for `bm25/documents.json` and `bm25/bm25_metadata.json`.
  Vector ingress requires raw builder schema 8 and the exact schema-8
  persistence inventory described below. Schema-8 vector documents name
  regular lexical repository-relative POSIX files; source symlink aliases are
  rejected instead of being rewritten to a different path identity.
- `--cache-dir` must be an existing cache produced by the current compiler. It
  must already contain `repo_manifest.json`, every selected fixed view tree,
  and the single-link regular `.index-compiler.lock`. Run or update the cache
  with the current `IndexCompiler`; do not add a lock file by hand. Import opens
  the lease existing-only and never creates the cache or lock. The importer
  owns that lease, so a library caller must not wrap it in another lock for the
  same cache; same-thread re-entry fails fast. The lock serializes cooperating
  CodeNib compiler and importer processes in a private cache namespace. It is
  not a sandbox against an actor actively replacing cache paths while the lock
  is held.
- `--catalog`, `--cas-root`, and `--workspace-root` have the same strict
  requirements documented for materialization below: an existing initialized
  SQLite catalog, a fully preprovisioned strict `LocalCAS`, and an existing
  private Linux workspace root owned by the current effective UID with exact
  mode `0700`. Repository and cache paths must neither overlap nor physically
  alias those storage authorities; the cache must not contain the repository.

`codenib index` prints its `repo_manifest.json` location; use that file's
parent as `--cache-dir`. For example, import the first generation of `main`:

```bash
codenib artifact import-cache /srv/src/repository \
  --cache-dir /var/lib/codenib/compiler-cache/repository \
  --catalog /var/lib/codenib/catalog.sqlite3 \
  --cas-root /var/lib/codenib/cas \
  --workspace-root /var/lib/codenib/workspaces \
  --repository owner/repository \
  --ref main \
  --expected-generation 0
```

That command imports only BM25. To import one combined view set into the same
snapshot, add the explicit selection:

```bash
codenib artifact import-cache /srv/src/repository \
  --cache-dir /var/lib/codenib/compiler-cache/repository \
  --view bm25,vector \
  --catalog /var/lib/codenib/catalog.sqlite3 \
  --cas-root /var/lib/codenib/cas \
  --workspace-root /var/lib/codenib/workspaces \
  --repository owner/repository \
  --ref main \
  --expected-generation 0
```

Under the cooperative cache lease, the command authenticates the source,
manifest, and every selected raw view. Before the first workspace mutation it
completes all selected recapture plans, the canonical selected-view portable
manifest, and the retained import plan. Under that same existing-only lease it
publishes a fresh missing-only generation for each selected view, then plans
and publishes one context containing exactly those views. It revalidates the
source, manifest, and published bytes before releasing the lease; retained CAS
ingestion and the single atomic snapshot/ref publication happen only afterward.

Current vector producers use builder schema 8. Each non-empty level stores a
canonical ordered `documents_*.json` array beside its FAISS index, and the root
config commits both files plus
`row_mapping = "codenib.vector-documents-array-index.v1"`. Array position is
the FAISS row. Before publishing a current compiler-cache generation, the
trusted producer reopens each generated FAISS file and verifies its dimension,
row count, metric, index type, training state, and canonical row IDs together
with the exact root, level, and document contract. The later recapture can then
validate document counts, configuration, and content fingerprints without
deserializing legacy pickle or parsing FAISS. A raw schema-7 compiler cache must
be rebuilt before import; already-retained portable schema-7 vector generations
remain readable for compatibility. FAISS also remains inert during import,
export, and materialization. Loading it for a native query is a separate,
explicitly authorized local operation.

The lease and content receipts establish self-consistency, not signed
provenance. Use a cache namespace private to one trusted OS account and keep
the source/cache namespace quiescent except for CodeNib processes honoring the
same lock. An actor that can replace both raw view bytes and the manifest can
compute matching hashes; neither the cooperative lock nor schema 8 claims to
sandbox that actor or attest who produced the FAISS bytes.

Every invocation allocates a random
`.codenib-cache-import-<nonce>-<view>` directory for each selected view and one
`.codenib-cache-import-<nonce>-context` directory below the workspace root.
They remain immutable generation evidence after their receipt owners close,
including when a later stage fails; the command warns instead of deleting
them. Their future ownership-aware reclamation belongs to M5. On success, the
CLI prints the selected view set, snapshot, ref generation, every evidence
path, and a copyable `codenib artifact materialize --snapshot ...` command. Its
suggested `.codenib-cache-import-<nonce>-materialized` output is not created or
reserved.

Strict ownership capture, semantic validation, workspace replay, bundle/CAS
ingestion, and receipt revalidation make multiple bounded reads of selected
payloads. Large FAISS indexes can therefore be read more than once. Treat this
as an offline maintenance operation and measure representative repositories,
payload sizes, and storage media before scheduling it at scale. Default or
latency-sensitive service use requires an end-to-end benchmark gate; do not
remove an authentication pass merely to improve an unmeasured result.

Use the current ref generation for a changed cache. If a call might have
committed before its result was observed, retry the exact same source, cache,
repository, namespace, and ref with the original `--expected-generation`.
The retry gets fresh missing evidence destinations but resolves the same
snapshot and generation without advancing the ref again.

## Materialize a retained artifact

This command is for a control plane that has already been populated. It does
not initialize a catalog, provision a CAS, import an existing cache, build a
view, or advance a ref. Prepare all three authorities before running it:

- `--catalog` names an existing, initialized CodeNib SQLite catalog. Existing-
  only means that a missing, empty, foreign, or corrupt database is rejected;
  it does **not** mean read-only. CodeNib opens a recognized catalog read-write,
  enables WAL, and may apply forward migrations before reading the selection.
  The database file must be a single-linked regular file. The CLI binds its
  captured `(st_dev, st_ino, st_nlink=1)` identity into the existing-only open;
  SQLite rechecks it immediately before and after `sqlite3.connect`, before
  complete claimed-version schema authentication, WAL activation, or
  migration. Its resolved ancestor chain and WAL/SHM sidecar namespace must
  remain trusted and quiescent for the entire invocation. These identity checks
  are not a filesystem sandbox against an actor racing arbitrary renames.
- `--cas-root` names a fully preprovisioned strict `LocalCAS` layout. The
  command will not create the root, `sha256` directory, or 256 digest shards.
  Provision the layout once, while its namespace is trusted and quiescent, with
  `LocalCAS.provision(...)`, then close the returned store before invoking the
  CLI.
- `--workspace-root` names an existing Linux directory owned by the current
  effective UID with exact mode `0700`. The catalog path, CAS root, and
  workspace root must not overlap or resolve through distinct directory names
  to the same filesystem identity. `--output` must be a missing child below the
  workspace root, and every existing output-parent component must be a real
  directory on the workspace root filesystem without a nested mount point. An
  existing file, directory, or symlink is never replaced.

For example, materialize the current generation of the `main` ref:

```bash
install -d -m 0700 /var/lib/codenib/workspaces

codenib artifact materialize \
  --catalog /var/lib/codenib/catalog.sqlite3 \
  --cas-root /var/lib/codenib/cas \
  --workspace-root /var/lib/codenib/workspaces \
  --repository owner/repository \
  --ref main \
  --expected-generation 7 \
  --output /var/lib/codenib/workspaces/repository-context-v1
```

Omit `--ref` to use `main`, or replace it with `--snapshot <snapshot-id>` to
select one immutable snapshot. `--expected-generation` is valid only for ref
selection. `--namespace` defaults to `default`.

The CLI creates the caller-owned publication receipt for this invocation. It
closes that receipt, the strict CAS anchors, and the catalog connection before
printing success and returning. Closing the receipt releases its native
handles; it does not delete the published artifact. The success output includes
the separate query-only handoff:

```bash
codenib mcp \
  --artifact /var/lib/codenib/workspaces/repository-context-v1 \
  --repository owner/repository
```

A failure can occur after the no-replace publication has committed. If the CLI
warns that the output now exists, do not assume that the path is disposable or
retry over it; verify the retained artifact before reuse or reclaim it through
an ownership-aware workflow.

## Serve a retained snapshot directly

The retained `codenib mcp` mode combines selection, materialization, and one
query-server lifetime without serializing a receipt or reopening the artifact
as an unrelated path capability. It accepts the same prepared authorities as
`artifact materialize`, plus an explicit missing output:

```bash
codenib mcp \
  --catalog /var/lib/codenib/catalog.sqlite3 \
  --cas-root /var/lib/codenib/cas \
  --workspace-root /var/lib/codenib/workspaces \
  --repository owner/repository \
  --ref main \
  --expected-generation 7 \
  --output /var/lib/codenib/workspaces/repository-mcp-v1
```

Omit `--ref` to resolve `main` once at startup, or use `--snapshot
<snapshot-id>`. `--expected-generation` is ref-only. These storage arguments
form one all-or-none mode and cannot be combined with a positional manifest,
`--artifact`, or `--repo`. The catalog must already be initialized, the strict
CAS preprovisioned, the workspace private and exact `0700`, and the output
missing under that workspace; this command does not provision or replace any
of them.

During startup CodeNib materializes the selected immutable generation, builds
the query binding through the active publication receipt's authenticated
reader, and loads the complete `ServerContext` before the reader callback ends.
It then closes SQLite, CAS, and path-topology authorities before starting MCP
stdio. The caller-owned workspace receipt remains active until `mcp.run`
returns or fails. Shutdown first removes the module-global context, then closes
the runtime context, and only after that releases the receipt. The materialized
output persists after normal shutdown and after receipt cleanup; a failure
after publication warns about the existing path rather than deleting it.

This is a source-disabled, query-only cold start. It currently requires a BM25
view, which loads from canonical persisted documents. A selected portable
vector remains native-parser inert and reports its authorization error; the
route never treats cache hashes as permission to parse FAISS, and vector-only
snapshots are rejected for this runtime. The ref is not polled or re-resolved,
the receipt is not a catalog/CAS GC pin, and no live context replacement occurs.
Those in-flight request pins and atomic swaps remain M3 work; durable evidence
retention and reclamation remain M5 work.

These retained-read routes and the explicit BM25/vector ingress above do not
complete the hybrid-storage M1 milestone. Benchmark-backed promotion of the
opt-in compiler and runtime routes is still missing, as is a production
provider for the strict BM25 replacement producer's `provider-bound-exact`
destination contract. Graph and Zoekt ingress remain M2 or later work; fenced
jobs and runtime hot switching remain separate milestones.

## Lifecycle

Create one private authority root and one empty receipt owner before calling a
strict producer:

```python
from pathlib import Path

from codenib import LocalWorkspaceProvider, PublishedWorkspaceReceiptOwner
from codenib.artifacts import stage_context_artifact_strict

root = Path("/var/lib/codenib/workspaces")
root.mkdir(mode=0o700, parents=True, exist_ok=True)
root.chmod(0o700)

provider = LocalWorkspaceProvider(root)
output_owner = PublishedWorkspaceReceiptOwner()
try:
    result = stage_context_artifact_strict(
        root / "repository-context-v1",
        manifest=manifest,
        repository="owner/repository",
        repository_source=repository_source,
        view_generations=view_generations,
        workspace_provider=provider,
        output_receipt_owner=output_owner,
        environ={},
    )
    assert output_owner.active
    use(result)
finally:
    output_owner.close()
```

The owner must begin empty and cannot cross a PID boundary. A successful
publication leaves it active even if a later caller callback is interrupted.
Close it in `finally` (or use it as a context manager) to release the retained
native handles. Closing the receipt owner does not delete the published output.

## Support boundary

The provider deliberately has a narrow first release:

- Linux only, with `cp310-abi3-manylinux_2_28` wheels for x86-64 and AArch64.
- Missing destinations only; it never replaces an existing file, directory, or
  symlink.
- One absolute authority root owned by the current effective UID, with exact
  mode `0700`. The root must be a private, quiescent namespace rather than a
  directory another same-UID process actively mutates.
- A complete protocol-v2 native extension and a successful Linux ownership
  support probe before the first namespace mutation.
- A plan small enough for the process descriptor limit. The format permits up
  to 100,000 directories, but the native `RLIMIT_NOFILE` preflight may reject a
  much smaller plan.

macOS, Windows, and installations without a compatible native extension keep
the rest of CodeNib usable, but `LocalWorkspaceProvider.require_support()`
fails closed before provisioning. CodeNib does not publish a prebuilt musllinux
wheel; a source build on Linux may provide the extension when a compatible C
toolchain is available. Container and seccomp policies must permit the
ownership probe, including `kcmp`; the release smoke container grants the
required ptrace capability.

This is an authority boundary for trusted CodeNib callbacks, not a Python
sandbox. Callbacks share the interpreter and must not reflectively mutate
CodeNib's private implementation state. If a process tightens seccomp after
acquisition and blocks both `kcmp` and the native `F_SETFL` OFD comparison,
cleanup preserves the exact unresolved descriptor pair and attaches a retryable
cleanup owner to the raised primary error. Retain and retry that owner after the
policy permits cleanup; discarding it forfeits recoverability.

## Publication guarantees

The protocol-v2 native aggregate owns the namespace and file descriptors for
the whole operation. It creates and writes files without returning raw file
descriptors to Python, pins the root and planned directory identities, and
performs one no-replace forward rename under a one-shot permit. The caller's
receipt-slot store is the authority linearization point:

- before the store, failure quarantines the exact candidate;
- after the store, recovery commits the same native receipt token
  idempotently;
- a fork child only authenticates and closes its inherited descriptor pairs and
  cannot rename, quarantine, or commit the parent generation.

Staged and published validators run inside that transaction. The returned
receipt therefore names one exact, durably published generation rather than a
path checked after the fact.
