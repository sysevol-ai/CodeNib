<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Isolated Agent Execution

CodeNib's pure parsing and retrieval paths do not execute repository code, but
some SCIP/LSP view builders invoke repository-selected package managers and
toolchains. Those can execute lifecycle or build scripts. For a public issue
bot, the repository, indexing job, issue text, build scripts, dependencies, and
generated commands must all be treated as untrusted.

The `codenib.sandbox` package provides the execution boundary for CodeNib's four
default agent tools; it does not automatically isolate indexing, retrieval, the
LLM client, or application control-plane code. A `SandboxProvider` creates an isolated
`SandboxSession`; the session owns command execution, bounded file operations,
diff generation, artifact export, and cleanup. GitHub webhooks, queues, tokens,
and pull-request publication remain application-layer responsibilities.

## GitHub Issue Bot Flow

1. The control plane validates a webhook and materializes an exact commit
   without exposing the GitHub installation token to repository-controlled
   processes.
2. One disposable VM/microVM (or an equivalently strong per-job sandbox) builds
   CodeNib views and runs the query. It has a job-exclusive runtime socket and
   storage, no other tenants, and no control-plane secrets. A plain OS process
   is not an isolation boundary. Never run SCIP/LSP view construction for an
   untrusted repository on the service host. Current graph/vector artifacts can
   contain pickle data, so the control plane must not deserialize artifacts
   produced by an untrusted indexing worker; keep indexing and retrieval in the
   same isolated worker.
3. It creates an execution sandbox from a service-approved, digest-pinned image.
4. `AgentRunner(..., sandbox=session)` replaces all four default tools—`read`,
   `grep`, `glob`, and `bash`—with sandbox-backed executors.
5. The agent edits and tests only the copied workspace. The immutable baseline
   is never mounted into model-controlled commands.
6. The control plane collects the patch and selected data-only artifacts,
   verifies size and digests, runs `git apply --check` against a fresh checkout
   of the same commit, and creates a pull request after policy or human approval.

Never call `agent_working_directory()` for concurrent bot jobs. It changes the
process-wide current directory and is intended only for trusted local harnesses.
`SkillRegistry` is also process-global today, so run exactly one issue job per
worker process; do not construct or run two `query()` jobs concurrently.
`SandboxLimits.command_timeout_seconds` is a per-command cap, not a job lease.
The worker/control plane must also enforce a total wall-clock deadline, command
and LLM-call budgets, and cancellation tied to the external lease reaper.

## Docker Backend

The initial backend uses rootless Docker and one short-lived container per
command. Workspace state persists in a private named volume; process state does
not. On timeout or output exhaustion, CodeNib kills and removes the uniquely
named container, which also removes its descendant processes.

The provider pins every Docker CLI call to one configured local Unix socket,
an absolute client binary, and a minimal environment; host contexts, Docker
config, and credential helpers are not inherited. It fails closed unless:

- the configured Docker endpoint is an absolute local Unix socket;
- rootless mode, seccomp, cgroup v2, and the systemd cgroup driver are active;
- a fixed preflight container observes the requested CPU, memory, and PID
  limits in its own cgroup;
- the image is on the control-plane allowlist and pinned by SHA-256 digest;
- the image OS and architecture match the requested platform;
- the image does not declare volumes or secret-like environment variables; and
- the source checkout HEAD matches the requested full Git commit.

For revision-pinned jobs, the baseline is exported from that commit's Git
objects inside the fixed bootstrap container. Ignored files, untracked files,
and working-tree modifications are never copied. The provider rejects commits
containing Git submodules, tracked symlinks, or `export-ignore`/`export-subst`
attributes, then fingerprints the sealed baseline with CodeNib's repository
filter policy. `query(..., sandbox=...)` requires both that fingerprint and the
commit to match its trusted manifest. Git LFS pointer files remain pointers;
bot jobs that require symlink, submodule, or LFS contents are unsupported by
this MVP and must be skipped.

Every agent command gets a read-only root filesystem, a non-root user, all Linux
capabilities dropped, `no-new-privileges`, the built-in seccomp profile, private
IPC/cgroup namespaces, CPU/memory/PID limits, bounded tmpfs, and no network by
default. The host Docker socket, host home directory, SSH agent, cloud
credentials, and GitHub token are never mounted or forwarded.

The approved image must provide `/bin/sh`, `python3`, `git`, `rg`, and `tar`.
Pre-build project toolchains into the image; ordinary agent commands should not
install system packages.

```python
import os
from pathlib import Path

from codenib.agent import AgentRunner
from codenib.sandbox import DockerSandboxProvider, SandboxSpec

image = os.environ["CODENIB_SANDBOX_IMAGE"]  # full name@sha256:... reference
revision = os.environ["GITHUB_SHA"]          # full 40-character commit

provider = DockerSandboxProvider(
    allowed_images={image},
    docker_host=f"unix:///run/user/{os.getuid()}/docker.sock",
    work_root=Path("/var/lib/codenib/sandbox-audit"),
    retain_audit_logs=True,
)

spec = SandboxSpec(
    source_dir=Path("/srv/codenib/checkouts/issue-123"),
    source_revision=revision,
    image=image,
    platform="linux/amd64",
    task_id="issue-123",
)

with provider.create(spec) as session:
    runner = AgentRunner(llm=my_llm, sandbox=session)
    result = runner.run("Reproduce and fix issue #123, then run targeted tests.")
    patch = session.get_diff()
    bundle = session.collect_artifacts(
        ["test-results.xml"],
        Path("/srv/codenib/artifacts/issue-123.zip"),
    )
```

The caller owns the session lifecycle so it can retrieve the diff and artifacts
after the model finishes. `close()` is idempotent and removes both private
volumes. This minimal example demonstrates isolated default tools; a retrieval
job must additionally use a trusted manifest whose commit matches the sandbox
revision and fingerprint. `AgentRunner` rejects a non-empty skill registry when
a sandbox is used without such a manifest, preventing stale process-global
contexts from leaking into this default-tools-only path.

The copied workspace intentionally contains no `.git` metadata. Commands such
as `git status`, version derivation through `setuptools-scm`/`hatch-vcs`, and
tests that require repository history will not work in this MVP. CodeNib
produces the baseline diff through the provider instead.

## Network And Secrets

`NetworkMode.NONE` is the production default. Docker's normal bridge network
cannot enforce a hostname allowlist, block DNS rebinding, or reliably exclude
cloud metadata and private address ranges. `NetworkMode.BRIDGE` therefore means
full container egress and should be restricted to trusted development.

If dependencies must be fetched, use a separate provisioning stage behind a
policy-enforcing egress proxy. This MVP has no sealed-snapshot or dependency
volume import API, so production jobs currently need toolchains/dependencies
prebuilt into the approved image or available offline in the tracked tree. Do
not pass registry, GitHub, package-manager, or cloud credentials into general
agent commands.

## Deployment Gates

This package is the sandbox foundation, not a claim that a public GitHub bot is
ready to expose. Before accepting arbitrary repositories:

- run checkout, dependency provisioning, SCIP/LSP indexing, retrieval, and the
  agent in a disposable VM/microVM with job-exclusive runtime/storage and no
  GitHub installation token or other control-plane secret;
- never load an untrusted worker's current manifest, graph pickle, vector
  docstore, or other executable serialization in the control plane;
- define a strictly data-only, path-confined, size-bounded, digest-verified
  artifact format before moving indexes across a trust boundary;
- use a narrow authenticated LLM proxy rather than placing model credentials in
  the worker; and
- validate every complete patch on a fresh, revision-matched checkout before
  publication. Diff output that reaches its byte limit fails instead of
  returning a partial patch.

## Security Boundary

Rootless Docker is a practical initial backend, not a strong hostile
multi-tenant boundary. Before accepting arbitrary public repositories at scale,
run workers on dedicated hosts or VMs and add a stronger provider backed by
gVisor or microVM isolation. Keep the same provider/session API so this does not
change CodeNib's agent or indexing layers.

Named volumes also do not enforce workspace disk quotas. Before accepting jobs,
production workers must place Docker storage on a quota-limited task filesystem
and run an external lease reaper for containers and volumes bearing the
`ai.codenib.sandbox` label. A cleanup verification failure must quarantine the
worker until that reaper confirms the container is gone. Output, stdin,
artifacts, time, memory, CPU, and PID counts are bounded by the library; disk is
an infrastructure responsibility.

Audit events contain full command argv, and bounded stdout/stderr or selected
artifacts can contain source, issue text, and repository secrets. Keep
`work_root` controller-only with encryption, access control, a total storage
quota, and an explicit retention/deletion policy.

For the runtime prerequisites, see Docker's documentation for
[rootless mode](https://docs.docker.com/engine/security/rootless/),
[UID/GID mapping](https://docs.docker.com/engine/security/rootless/uid-gid-mapping/),
and the [default seccomp profile](https://docs.docker.com/engine/security/seccomp/).
