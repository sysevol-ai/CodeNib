# CodeGraph pilot admission

Three source snapshots are selected, but matched inference is not admitted yet.
The checks exposed a native-grading isolation defect and a Python graph failure.
Keep those outcomes instead of replacing difficult cases. The
[retained receipts](codegraph_admission_2026_10_27.json) contain the selection,
original and corrected controls, exact harnesses, source identities and SDK
configuration probes. Reference production and test patches are excluded.

## Fixed source cohort

The selection uses SWE-bench Verified revision
`c104f840cc67f8b6eec6f759ebc8b2693d585d4a`. Before reading issue statements,
running reference controls or indexing, the controller selected one case per
repository by the smallest SHA-256 of a fixed seed plus instance ID. Eligibility
uses only metadata difficulty under one hour and an already-local native image.
The raw dataset README and projected metadata were used, without an HTML viewer.
These historical benchmark cases are already exposed; no unseen-task claim applies.

| Repository / selected case | Strict CodeNib source-index outcome | Native admission with corrected isolation |
| --- | --- | --- |
| Requests / `psf__requests-1921` | BM25 and graph fresh; source preserved | Open: needs an offline HTTPbin fixture |
| Flask / `pallets__flask-5014` | BM25 and graph fresh; source preserved | Empty production change fails one target; reference passes all 60 collected tests |
| pytest / `pytest-dev__pytest-5809` | BM25 fresh; graph rejected for cyclic containment; source preserved | Empty production change fails one target; reference passes all four selected native tests |

The planned shape is five tasks per repository: lookup, cross-file flow, bounded
change impact, original issue repair and a controlled post-edit follow-up.
The fifteen prompts, acceptance rubric and full execution manifest are not
frozen yet. Case selection remains fixed after these checks.

Strict provider probes use the CLI policy: project preparation and partial
language admission are disabled. An earlier Requests probe enabled preparation;
it is retained separately. The pytest failure reproduces #871 in a real project.
SCIP Python 0.6.8 also fails this check; its release notes describe container
updates. No provider upgrade or identity-recovery claim follows. Successful
artifact construction is not a complete correctness audit: relevant query
targets in Requests and Flask still need source checks.

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
records enforced limits and successful removal. Requests remains explicitly
unrun under the corrected protocol until its offline fixture is ready. Test IDs,
test patches, native parser and grading rules stay unchanged. No historical
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

Still open: Requests' offline environment, full task/model/tool manifest and
usage normalization, peer isolation/default daemon, matched inference and the
five-user observation gate.
