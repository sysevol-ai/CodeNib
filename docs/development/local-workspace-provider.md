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

The provider is not part of CodeNib's default compiler, import, or runtime
path. The first explicit operator bridge is `codenib artifact materialize`,
which can publish a retained catalog ref or immutable snapshot to a missing
portable-artifact directory. The strict BM25 replacement path still requires
the unsupported `provider-bound-exact` destination mode.

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
  schema inspection, WAL activation, or migration. Its resolved ancestor chain
  and WAL/SHM sidecar namespace must remain trusted and quiescent for the entire
  invocation. These identity checks are not a filesystem sandbox against an
  actor racing arbitrary renames.
- `--cas-root` names a fully preprovisioned strict `LocalCAS` layout. The
  command will not create the root, `sha256` directory, or 256 digest shards.
  Provision the layout once, while its namespace is trusted and quiescent, with
  `LocalCAS.provision(...)`, then close the returned store before invoking the
  CLI.
- `--workspace-root` names an existing Linux directory owned by the current
  effective UID with exact mode `0700`. The catalog path, CAS root, and
  workspace root must not overlap. `--output` must be a missing child below the
  workspace root, and every existing output-parent component must be a real
  directory on the workspace root filesystem. An existing file, directory, or
  symlink is never replaced.

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

This is the first explicit local bridge, not completion of the hybrid-storage
M1 milestone. Default compiler/import/runtime wiring is still missing, as is a
production provider for the strict BM25 producer's `provider-bound-exact`
destination contract.

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
