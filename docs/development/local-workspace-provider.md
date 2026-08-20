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

The provider is currently a library boundary. CodeNib's compiler and runtime do
not construct one automatically, and the strict BM25 replacement path still
requires the unsupported `provider-bound-exact` destination mode. The portable
whole-context producer can use the provider when its destination is missing.

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
