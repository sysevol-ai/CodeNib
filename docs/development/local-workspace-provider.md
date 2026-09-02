---
title: Local Workspace Provider
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Local Workspace Provider

`LocalWorkspaceProvider` is the Linux implementation for strict directory
publication. It publishes one fully validated directory into a missing
destination or replaces the exact generation named by an active receipt, then
transfers the new generation to a caller-owned
`PublishedWorkspaceReceiptOwner`.

This is a filesystem publication boundary, not a database or generic storage
backend. The only product database is `codenib.wiki.store.WikiStore` with its
supported SQLite WAL implementation. Repository search state remains in
manifest-bound file artifacts. See `docs/storage_backend_roadmap.md` for the
current scope decision.

Protocol v4 introduced an internal existing-destination replacement primitive:
it captures the incumbent, provisions and authenticates one hidden same-parent
candidate, exchanges the two exact directory bindings with Linux
`renameat2(RENAME_EXCHANGE)`, and returns an opaque receipt token. Protocol v5
keeps that exchange contract and moves a cooperative, parent-wide cross-process
lease ahead of every candidate mutation. The native aggregate owns the parent,
incumbent, and candidate descriptors throughout; every descriptor borrowed by
trusted internal code remains owner-owned and must never be closed by the
borrower. Protocol v6 adds cancellation-aware exact entry points for initial
provisioning, replacement provisioning, and directory sealing. It polls only
between attested plan records, completed directory mutations, or successful
directory `fsync` calls; callback identity is preserved and an error from the
current operation remains primary.

The Python authority and `LocalWorkspaceProvider` now select that primitive for
receipt-bound exact requests. Exact replacement has three explicit authority
phases:

`run_strict_workspace(...)` polls an explicit stop after provider support and
callback-gate activation but before provider entry, then passes that same exact
callback through the provider protocol. An interruptible call therefore fails
closed before a legacy provider body can run if that provider does not accept
`check_cancelled`; a no-callback call retains the previous provider call shape.
Local propagates the callback through plan detachment, native provisioning,
descriptor adoption, skeleton authentication, workspace refresh, and sealing,
while its no-callback native calls also retain their previous shapes.

1. `bind_replacement_source(...)` synchronously consumes an active source
   receipt while a separately supplied protocol-v6 owner already holds the
   captured destination under its native lease. It requires the exact private
   destination-binding object from that receipt, matches both the retained
   parent descriptor and a complete incumbent descriptor scan, revalidates the
   native binding after the scan, then privately claims and binds the one-shot
   replacement permit. No permit, receipt token, or detached replayable
   replacement authorization is returned.
2. `provision_bound_replacement(...)` is the only provisioning entry after
   that handoff. It calls native provisioning itself and adopts the candidate
   from the same native owner, frozen hidden slot, and exact workspace plan.
   Provider code cannot mutate the handed-off owner between provision and
   adoption.
3. After write and seal, `publish_replacement_into(...)` uses a dedicated
   `RENAME_EXCHANGE` path. Candidate and incumbent bytes are read through
   separate retained descriptors. It never enters the generic
   isolate-and-rename helper and never performs a generic post-exchange parent
   sync; the native exchange and receipt settlement own durability ordering.

In exact mode, `provision_timeout_ns` supplies three independent absolute
deadline budgets. The provider mints one before destination capture and lease,
a fresh one after the complete active-source scan and handoff immediately
before candidate provisioning, and the operation mints a third immediately
before the pre-exchange candidate/incumbent scans and staged validation. A valid
long source scan or user build therefore cannot inherit an already expired
provision or publication deadline. Those pre-exchange checks consume the third
budget, and native exchange observes expiry before mutating the namespace. The
post-exchange published validator and receipt/abort settlement retain their
normal reconciliation guarantees but are not governed by that absolute
deadline.

The caller-owned receipt slot remains the linearization point. Before its store,
failure reconciliation invokes native abort, which authenticates and reverses
an exchanged mapping before releasing the lease. After its store, active or
cleanup-retain reconciliation idempotently commits the same native receipt
token. A successful commit switches the live Python authority to candidate-only
descriptor-backed verification of the live destination and parent binding; it
no longer depends on the displaced incumbent, so a later live-name rebind makes
receipt reads fail closed without preventing terminal owner close. The
displaced incumbent is returned as a
`DirectoryOrphan` whose locator deliberately names the ordinary
`linux-renameat2` reopening backend, not the transaction-only native backend.
Its reclamation remains a later cooperative-GC concern.

The strict request seam represents a missing destination only as
`destination_binding=None`; an existing destination requires an immutable
`PublishedWorkspaceDestinationBinding` projected by an active
`PublishedWorkspaceReceiptOwner`. That binding freezes the lexical destination,
the receipt's parent identity, and its full `_TreeOwnership`. Request
construction requires the binding path to equal the normalized destination,
generic authority adoption verifies the same parent and complete live tree, and
the callback session compares the complete adopted binding with the request.
The `destination_expectation` label remains only a derived diagnostic property,
so a string or raw ownership token cannot select replacement. Strict BM25 now
derives this binding from `source_generation` and cross-checks the borrowed
receipt. It passes that still-active owner separately from the request. A
private PID-bound, one-shot replacement-source gate proves exact binding and
operation identity, synchronously invokes `bind_replacement_source(...)`, and
spends its one-shot bind authority before candidate provisioning. The provider
receives the gate, never the source owner or a replayable capability. An exact
callback cannot enter unless that same gate bound the same adopted workspace;
the full callback lifetime is revoked before the provider call escapes.
`LocalWorkspaceProvider` then provisions only through the handed-off workspace
and the session publishes only through `publish_replacement_into(...)`. This
completes the historical Gate C implementation. Automatic orphan GC, a crash
journal, protection from hostile same-UID mutation, and route promotion are
outside this frozen compatibility seam.

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
- Missing destinations, or one exact existing directory generation bound by
  the separately active source receipt. It never replaces an unbound existing
  file, directory, or symlink.
- One absolute authority root owned by the current effective UID, with exact
  mode `0700`. The root must be a private, quiescent namespace rather than a
  directory another same-UID process actively mutates.
- A complete protocol-v6 native extension and a successful Linux ownership
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

The protocol-v6 native aggregate preserves the protocol-v2 missing-destination
publication contract. In that publication mode it owns the namespace and file
descriptors for the whole operation, creates and writes files without returning
raw file descriptors to Python, pins the root and planned directory identities,
and performs one no-replace forward rename under a one-shot permit. The
caller's receipt-slot store is the authority linearization point:

- before the store, failure quarantines the exact candidate;
- after the store, while the exact native authority remains active and before
  its close, recovery commits the same native receipt token idempotently;
- a fork child only authenticates and closes its inherited descriptor pairs and
  cannot rename, quarantine, or commit the parent generation.

Staged and published validators run inside that transaction. The returned
receipt therefore names one exact, durably published generation rather than a
path checked after the fact.

Protocol v4 extended capture with a primitive-only replacement transaction.
Protocol v5 preserves that history and adds a required pre-mutation lease
transition. Its success path has these native owner states:

```text
destination-captured
  -> destination-leased
  -> replacement-provisioning
  -> replacement-provisioned
  -> replacement-adopted
  -> replacement-exchanged-unreceipted
  -> replacement-receipted
  -> closed
```

Protocol v4 added `claim_owner_replacement_permit_exact`,
`provision_owner_replacement_exact`,
`verify_owner_replacement_binding_exact`, and
`exchange_owner_replacement_exact`. Protocol v5 adds
`acquire_owner_replacement_lease_exact` and requires it after capture and before
permit claim or provisioning. The fail-closed facade exposes the same
operations without `_exact`, including `acquire_owner_replacement_lease`.
Protocol v6 adds `provision_owner_interruptibly_exact`,
`provision_owner_replacement_interruptibly_exact`, and
`seal_owner_directories_interruptibly_exact`; the facade selects those calls
only when an explicit cancellation callback is present and otherwise retains
the legacy exact call shape.
Claim returns a distinct opaque
`WorkspaceReplacementPermit`; exchange consumes that permit and returns an
opaque `WorkspaceReceiptToken` for the existing `commit_owner_receipt`
operation. Permits and receipt tokens do not expose a separate public state
API; `owner_state` reports the aggregate state above.

`destination-captured` is speculative and does not hold the lease. Capture
retains one owner/guard pair for the borrowable parent authority used by
namespace operations and `fsync`, and separately opens a never-exposed parent
descriptor plus guard for the lease. The lease pair is authenticated as the
same parent identity, internally the same open-file-description (OFD), and a
different OFD from the
borrowable parent pair. Acquisition flocks only that hidden OFD and then
revalidates the complete captured parent chain and incumbent
name/device/inode binding under the lock. Only success enters
`destination-leased`. A borrower's `flock(LOCK_UN)` on the exposed parent
therefore cannot release the replacement lease. Lease contention returns cleanly in
`destination-captured`, and a stale capture is rejected under lock with zero
candidate mutation before the flock is released. If an earlier owner instead
reverses its exchange and restores the captured incumbent, a waiting owner may
acquire normally on a later retry.

`replacement-provisioning` is the in-call construction state; a successful
provision call returns in `replacement-provisioned`. Claiming the distinct
one-shot replacement permit does not change `destination-leased`. After the
candidate is adopted, written, sealed, and rebound to the aggregate, exchange
returns an opaque receipt token. While the exact owner authority remains active
and before close, committing that token is idempotent, releases the cooperative
flock lease while retaining the authenticated parent descriptor and guard until
owner/receipt close, and enters `replacement-receipted`. The receipted state is
sealed against a path-based live-name verifier: replacement binding verification
and new parent-descriptor borrows are unavailable there, while descriptors
borrowed earlier remain owner-owned until close. Closing a receipted owner only
closes its handles and does not mutate either path; after close, the token is no
longer a retry capability and commit fails closed.
The forward parent `fsync` occurs inside exchange before a token is returned.
If it fails, the caller has no token: the aggregate enters
`replacement-recovery-required`, retains its lease and descriptors, and must be
settled through owner recovery/abort. In a reachable receipt-commit path, a
dual-binding validation or `LOCK_UN` failure also enters recovery-required but
leaves the already-returned token unconsumed. That same exact token may retry
`commit_owner_receipt` only while the owner remains active and after native
reclassification proves the exchanged mapping. `abort_owner` is the alternative
settlement authority; it reclassifies the mapping and reverses the exchange
when required. Every lease-active
recovery, including an operator-restored mapping or receipt retry, completes a
parent `fsync` before eventual unlock. Other interrupted paths may enter
`replacement-recovery-required`, `quarantined`, or `closed`.

The exchange contract is deliberately narrow. It is Linux-only, requires the
incumbent and hidden candidate to be distinct directories under the same
captured parent and on the same device, and uses exactly
`renameat2(RENAME_EXCHANGE)`. There is no portable or multi-rename fallback.
The separately opened, hidden parent open-file-description (OFD) is acquired
nonblocking with `flock(LOCK_EX | LOCK_NB)` before candidate mutation and is held across
provisioning, adoption, the live exchange, and return settlement until either
the receipt is committed or an authenticated reverse exchange plus
parent-directory `fsync` completes. Namespace operations and durability syncs
continue to use the original parent authority; the hidden lease pair is never
borrowed. Because the flock covers the parent rather than one basename, it
provides one cooperative cross-process single-writer boundary even for
different destination names under that parent. Capture itself remains
speculative, but all live capture revalidation occurs under the lease before
mutation. If two owners capture the same incumbent and the first commits, the
second later rejects its stale capture during lease acquisition, performs zero
candidate mutation, releases its newly acquired lease, and remains
`destination-captured`; it does not auto-adopt or quarantine the newer mapping.
The containing `0700` root must remain private and quiescent. This is a
cooperative/private threat model, not protection against a hostile same-UID
process that ignores the guard.

Before receipt settlement, same-process abort verifies the swapped incumbent
and candidate mappings, reverses the exchange, flushes the parent, restores the
incumbent at the destination, and retains the candidate at its pre-existing
caller-supplied, authenticated hidden replacement slot for quarantine. The
primitive validates the hidden basename but does not generate or prove
randomness. A committed exchange leaves the new candidate at the destination
and the old incumbent at that slot; the primitive does not reclaim it. Readers
observing the live namespace see one complete binding or the other at the
single exchange point, but this is live
atomicity, not crash recovery. There is no crash journal and no promise to roll back
after `SIGKILL`, host failure, or power loss. A caller that observes
`replacement-recovery-required` must retain the owner and explicitly retry
recovery, plus the same receipt token when a reachable receipt-commit attempt
failed. If all references are abandoned while the mapping is unknown, native
deallocation deliberately retains the raw descriptors and cooperative flock
until process exit rather than silently releasing the only exclusion and
authority; restarting is the final recovery boundary. If acquisition or
pre-mutation provisioning reported an error before a candidate was confirmed,
settlement first authenticates the incumbent and hidden authorities while the
lease remains held. It treats the attempt as no-candidate only when
`fstatat(..., AT_SYMLINK_NOFOLLOW)` reports exact `ENOENT` for the configured
slot, then syncs the parent before unlock. An existing or rebound slot, or any
ambiguous stat result, enters or retains `replacement-recovery-required` with
the lease and configuration intact for retry. Unlock or close interruption is
handled by the same retained-authority retry rather than requiring an
externally stale live name to be restored. An interruption after the native
lease return but before Python observes it leaves `destination-leased`;
repeating acquisition is idempotent and
does not take a second flock. A fork child cannot mutate, reverse, commit, or
unlock the parent's lease. It only authenticates and closes its inherited
descriptor pairs, leaving the parent process's authority and lock intact.

All borrowed incumbent, candidate-root, parent, and planned-directory
descriptors remain owned by the aggregate and are valid only for its lifetime;
borrowers must never close them. The primitive remains an authority boundary
for trusted internal code, not a Python sandbox or full `_TreeOwnership`.
`LocalWorkspaceProvider` now integrates it for the non-`None` binding whose
derived expectation is `provider-bound-exact`, using the separately active
source owner only through the private one-shot gate. The historical Gate C
implementation is complete. This compatibility surface has no remaining
promotion gate; its withdrawal follows the current storage subtraction
roadmap.
