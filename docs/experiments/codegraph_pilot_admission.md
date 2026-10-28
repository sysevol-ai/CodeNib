# CodeGraph pilot admission

Fifteen tasks and their execution policy are frozen across three source snapshots.
The executable/runtime binding and bounded representative source checks are complete.
All twelve formal workflows execute; the selected original native grader resolves
all twelve captured repairs. Source-explanation acceptance remains open. Runtime
checks retain the native-grading isolation defect, failed
environment profiles and Python graph failure instead of replacing difficult cases. The
[retained receipts](codegraph_admission_2026_10_27.json) contain the selection,
original and corrected controls, exact harnesses, source identities and SDK
configuration probes. The [runtime receipts](codegraph_runtime_2026_10_27.json)
add actual arm64 tool/container checks and the new native controls. Reference
production and test patches are excluded. The
[execution archive](codegraph_execution_2026_10_27.json) retains the exact driver,
native scope, pre-inference binding, source queries and no-model checks.
The [formal execution record](codegraph_matched_run_v1.md) links all workflow
observations, original native outcomes and the remaining acceptance gates.

## Fixed source cohort

The selection uses SWE-bench Verified revision
`c104f840cc67f8b6eec6f759ebc8b2693d585d4a`. Before reading issue statements,
running reference controls or indexing, the controller selected one case per
repository by the smallest SHA-256 of a fixed seed plus instance ID. Eligibility
uses only metadata difficulty under one hour and an already-local native image.
The raw dataset README and projected metadata were used, without an HTML viewer.
These historical benchmark cases are already exposed; no unseen-task claim applies.

| Repository / selected case | Strict CodeNib source-index outcome | Native arm64 admission with corrected isolation |
| --- | --- | --- |
| Requests / `psf__requests-1921` | BM25 and graph fresh; source preserved | Offline HTTPbin: empty production change fails one target; reference passes all 119 collected tests |
| Flask / `pallets__flask-5014` | BM25 and graph fresh; source preserved | Empty production change fails one target; reference passes all 60 collected tests |
| pytest / `pytest-dev__pytest-5809` | BM25 fresh; graph rejected for cyclic containment; source preserved | Empty production change fails one target; reference passes all four selected native tests |

The [frozen task document](codegraph_matched_tasks_v1.json) contains five tasks per
repository: lookup, cross-file flow, bounded change impact, original issue repair
and a controlled post-edit follow-up. Its SHA-256 is
`76f170aaaacee775009fc01d7eedac9bb74d2e73d21c158fe7dee9a78a8d5bd6`.
The [execution policy](codegraph_matched_policy_v3.json) fixes model/settings,
ordering, attempts, timeouts, accounting and acceptance before inference.
Its SHA-256 is `da562c62849d8bc3937dc6dde1c1a2205609ceba7988d6367b2f18e135bebe41`.
The JSON copies preserve the controller files byte for byte; internal path fields
refer to those working filenames. Case selection remains fixed.

Each of twelve repository/arm workflows has a cold setup and lookup session,
then a fresh return session for flow, impact, repair and the same-session post-edit
query. Native read/search/edit/shell tools remain available to all arms; optional
tool use is not forced. The complete five-task wall time includes product setup,
indexing, both session startups and updates. Failed or unattempted tasks retain
their denominator and the frozen timeout penalty. Usage sums final cumulative
per-model counters once per session, including auxiliary models and cache tokens;
SDK estimates remain separate from invoices. The executable driver and final
runtime/source manifest are bound before the first formal query. Completed SDK
execution remains pending source-checked acceptance and native repair grading.

Strict provider probes use the CLI policy: project preparation and partial
language admission are disabled. An earlier Requests probe enabled preparation;
it is retained separately. The pytest failure reproduces #871 in a real project.
SCIP Python 0.6.8 also fails this check; its release notes describe container
updates. No provider upgrade or identity-recovery claim follows. Successful
artifact construction is not a complete correctness audit: relevant query
targets in Requests and Flask now have bounded source checks, described below.

## Installed public-release control

The public PyPI 0.2.4 aarch64 wheel is separately installed and its SHA-256 and
import origin verified. It shares warm dependencies and the installed SCIP 0.6.6
toolchain; its isolated registration client is a stand-in, while MCP uses actual
stdio. No coding model or real client profile is involved. The clean cold/reuse
fixture passes. Dirty initialization rejects the checkout, and the existing MCP
session still returns `calculate_total()` after source changes to
`calculate_discounted_total()`. These observations independently reproduce the
released workflow failures addressed by #877/#878. Fixture timings are retained
without claiming package-install cold cost or net task benefit.

## Native container boundary

[#882](https://github.com/sysevol-ai/CodeNib/issues/882) reproduces why the previous
Verified adapter failed to enforce its declared limits: DockerClient returns a
fresh container collection on each access. Patching one collection did not guard
later native-harness calls. A real Docker SDK 7.2.0 create-only probe observed
bridge networking and no CPU, memory or process limit.

The corrected adapter guards the stable API creation boundary and inspects the
daemon response before allowing the native harness to start the container.
The unique recorded name remains the cleanup owner if inspection fails.
New resolution reports retain `observed_container_limits`.

A real harmless container confirms network `none`, no default route, 4 GiB,
2 CPUs and 256 PIDs. Kernel cgroup files confirm the resource limits; cleanup
is verified. Fresh-collection and failed-inspection regressions cover the
reproduced defect and owned recovery.

All six original native controls remain frozen. Their declared isolation was
not enforced; their raw outcomes are not relabeled as correctly isolated runs.
Requests' original reference control passes all six annotated failure targets
but fails two selected regression tests with HTTPS certificate errors; another
unselected test has an existing pytest compatibility failure. Flask and pytest
were rerun in new directories after the fix: both collect and execute the intended
tests, empty production changes fail, and references pass. Every new report
records enforced limits and successful removal.

Requests now passes native admission with real HTTP and HTTPS HTTPbin services
on loopback inside the network-isolated container. Both controls collect and
execute all 119 tests. The empty production change fails the intended header
defect while all 107 selected regression targets pass; the reference passes all
119 tests. The legacy test environment uses pytest 4.6.11 and pluggy 0.13.1 with
external plugin autoload disabled. Test IDs, test code, native parser and grading
rules stay unchanged.

The first offline profile still fails the mixed-case HTTPS regression because
direct `Session.send()` uses the bundled CA file instead of the environment CA
setting. Its failure is retained. The admitted v2 profile temporarily extends
only that trust-store data with the fixture CA; hashes and exact restoration
are observed after both controls. This is an explicit test-environment
adaptation, not an unchanged-source claim during execution. Only owned listeners
and worker process groups are stopped; the uniquely owned container remains the
recovery boundary and is removed. These amd64 controls run through emulation on
the arm64 controller and do not establish comparative task timing. No historical
score is overwritten.

The original controller also looked for the wrong result filename after grading.
The original errors and authoritative `resolution.json` outcomes are retained.
Recovering those files did not repeat or change their grading.

## SDK configuration and usage

The official Claude SDK with existing Max authentication completes a controlled
isolation probe. Native Read rejects a canary outside the owned checkout, native
Bash executes inside an isolated container, and an in-scope read succeeds.
Delivered tool results, effective `medium` effort and actual `claude-opus-5-5`
main model are captured. This forced diagnostic is excluded from the fifteen tasks.

A no-source probe verifies the output-limit mechanism: four streamed responses
stop at the requested 1,024 tokens. The SDK then returns an output-limit error;
cumulative main output is 4,096 tokens. The prospective diagnostic accepts a
limit stop as mechanism evidence while retaining the failed SDK terminal.
It does not turn a capped matched task into a success. The first cap observer
used the wrong stream-event attribute; its failed attempt and missing usage
remain visible.

Use final cumulative per-model counters for accounting. The SDK also reports
an auxiliary `claude-haiku-5-5` model although no subagent is enabled. Preserve
both models; avoid summing duplicated or incomplete assistant-message slices,
and count thinking once within output. The model-capability field
`maxOutputTokens=128000` does not describe the observed 1,024-token request cap.
SDK turns are not HTTP-request counts. Successful scope and corrected cap probes
confirm included-only rate events. SDK dollar estimates remain separate from
unreconciled invoices; the first failed cap observer has missing usage.

## Native arm64 runtime checks

All three neutral project environments and separately installed product runtimes
run as arm64. The source-candidate CodeNib wheel is built from `fcc02fe1`, imports
from its installed container location and is distinct from the public wheel.
The shared Python/Node/uv/agent runtimes are dependency-warm prerequisites, not
measured product-cold setup. Product readiness timings and tiny fixtures do not
enter the formal matched result.

Actual MCP checks run in separate, observed network-none containers with only
their own source checkout mounted. Serena's v2 REPL sees the renamed definition
without reconnecting; a host-only canary is absent through its Python REPL.
CodeGraph runs without the direct-mode override, sees the edit in the same
connection and retains its default background process after MCP disconnects.
CodeNib rejects the stale query, rebuilds using its own installed CLI and actual
container-local Claude registration, and returns the new callee after reconnecting.
All owned containers are removed. Controller setup/schema errors remain retained.

The [native SDK scope receipts](codegraph_sdk_scope_2026_10_27.json) confirm that
Read, Grep, Glob, Write, Edit and container-executed Bash succeed while
the intended outside-scope attempts are denied. Large shell output remains usable:
Read and Grep admit only the hashed file named by native Bash metadata in that
exact SDK session's output directory. Its verified read-only copy is also available
to Bash inside the owned container. The corrected multi-line diagnostic confirms
actual content delivery through all three tools. It records 1,336 then 90 streamed
main output tokens and a final cumulative count of 1,426; summing
the two result totals would double count. Rate updates are validated when emitted,
not required to repeat unchanged in every user turn. Earlier diagnostic/controller
failures stay visible.

The native adapter now selects the harness architecture from the pinned image
instead of always requesting x86_64. Requests and Flask arm64 controls execute
the same selected tests as their amd64 controls. Requests' reference passes all
119 collected tests; Flask's passes 60. Empty production changes fail their
intended targets. The first pytest arm64 profile fails before collection:
offline build dependencies are unavailable and the tag-free source checkout
produces a `0.1` version that fails pytest's own minimum-version check. A further
preparation failure exposes missing `importlib-metadata`; neither is a model result.

The corrected pytest environment stages build/runtime wheels in the target
testbed's own pip configuration and supplies the version observed in the
original pinned image through pytest's distribution-specific SCM setting.
It preserves the one-commit source and the original test code/parser/grading.
All four selected native tests execute: empty fails the intended target with
three regression passes, and reference passes all four. Earlier raw failures
and every amd64 result remain unchanged.

Still open: complete source-explanation acceptance, broader provider correctness
and the five-user observation gate. Frozen policy v1's duplicate
workflow labels were corrected in v2 before inference; arm order and experimental
settings did not change. Policy v3 restores ordinary Read/Grep/Bash access to
owned SDK output files after a single oversized line exposed a delivery failure;
the actual marker delivery is checked, not just the hook permission.

## Executable binding and representative queries

Before the first matched query, the execution binding records SHA-256 identities
for the executable, native scope, task/policy files, CLI binary, candidate wheel,
controller helpers and readiness evidence. It pins all three arm64 task images.
Each workflow verifies those identities before starting. The actual prepare-only
run verifies fresh Serena install/index, network isolation, a container-created
file edited by native host tools, and owned-container cleanup. It uses the retained
draft executable; focused no-model checks bind the final executable and verify
committed-change capture and missing-latest-query usage accounting.

Fifteen observations check five Requests/Flask definitions through exact names
and source positions, plus bounded graph roots and selected callees. Graph nodes
without source locations remain explicit. The earlier short names `Session.send`
and `HTTPAdapter.send` return ambiguity payloads; exact symbol names resolve.
Those failures are retained, and formal task retries keep their time cost.
These checks do not establish complete graph accuracy or product benefit.

Formal execution follows the frozen twelve-workflow order with automatic tool
choice and no controller retries. All sixty tasks complete their SDK execution.
An available optional tool that the model does not use remains an outcome.
Native repair grading runs after model execution and resolves all twelve captured
repairs. No grading feedback or reference patch enters a later model input.
The 48 explanation tasks still require source-rubric review before benefit scoring.
