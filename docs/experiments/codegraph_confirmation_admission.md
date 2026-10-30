# Independent CodeGraph confirmation: membership and admission

This record fixes three historical repositories before preparing a new
four-arm comparison. It establishes task membership and source provenance;
native grading is ready, while the runnable protocol, product admissions and
model execution are still open.
No new model call or comparative result is recorded here.

The earlier complete cohorts and the permanently stopped task-fit round retain
their original outcomes. The engineering fixes in
[#917](https://github.com/sysevol-ai/CodeNib/pull/917) and
[#918](https://github.com/sysevol-ai/CodeNib/pull/918) do not rescore them.
The overall objective and open gates remain in [adoption A0](../adoption_roadmap.md).

## Membership fixed before reading issues or answers

Selection reads only `instance_id`, `repo`, `base_commit`, `version` and
`created_at` from the pinned 500-row SWE-bench Verified Arrow artifact. Its
dataset revision is `78f471bf655a3137b2e8a75af1501690ec009ec3`. The raw
dataset README is checked for example IDs without opening an HTML dataset
viewer. No example IDs are present in that captured README.

Exclude the complete Flask, Requests and pytest repositories used by earlier
cohorts. Order the remaining repositories by SHA-256 of the seed, a NUL byte,
`repo`, a NUL byte and the repository name. Within each repository, order cases
the same way with `case` and the instance ID. The seed is
`codenib-confirmation-2026-10-30-v1`. Choose the first case in each of the first
three repositories. Retain the next three repositories as the second set;
their issues, source and grading material have not been opened.

The selection rule is written before selection, with SHA-256
`01c2ed0b472673bad03f47907e8913c0d89439e4daefba1d0c95d6deb4d1561e`.
The complete metadata, executable selection scripts and receipts are retained
in the [admission evidence](codegraph_confirmation_admission_2026_10_30.json).

| First-set case | Repository | Original source commit |
| --- | --- | --- |
| `astropy__astropy-13579` | `astropy/astropy` | `0df94ff7097961e92fd7812036a24b145bc13ca8` |
| `sympy__sympy-21596` | `sympy/sympy` | `110997fe18b9f7d5ba7d22f624d156a29bf40759` |
| `mwaskom__seaborn-3187` | `mwaskom/seaborn` | `22cdfb0c93f8ec78492d87edb810f10cb7f57a31` |

Do not replace a selected case because its environment, native tests, provider
coverage or model outcome is difficult. Preserve preparation failures and fix
the environment before inference. These public historical cases are new to
this program; model pretraining exposure is unknown. They carry no
unseen-model claim.

## Source and grading separation

All three shallow checkouts pass exact commit/tree, clean-worktree and
single-commit-object checks. The source fixtures expose no subsequent Git
history. Only after fixing membership are the first-set public issue statements
read. Original reference patches, test patches and selected test IDs remain
in a separate private grading directory outside model mounts. No second-set
issue, source or answer is read.

The existing pinned SWE-bench 4.1.0 builder prepares three native arm64
environments and separate private grading images. The first controller attempt
uses a nonexistent script property and fails before creating a container; its
original executable and failure receipt remain retained. The corrected attempt
uses the installed harness's original repository script. Neither attempt
changes the test patches, parser, selected outcomes or grading methodology.

Both original-production and reference controls now collect and execute every
selected test, with no missing status or collection error. The original
production control fails behavioral cases that pass with the reference; all
reference selected outcomes pass. This closes native readiness and establishes
no model repair, graph advantage or answer-quality result.

| Case | Selected tests executed in each control | Original-production outcome | Reference outcome |
| --- | --- | --- | --- |
| Astropy | 41 | Behavioral failure retained | All pass |
| SymPy | 47 | Behavioral failure retained | All pass |
| Seaborn | 250 | Behavioral failure retained | All pass |

Exact original logs and grading receipts remain private and hash-bound by the
published summary. All six owned grading containers are independently absent.
The four actual product admissions remain open. Any later collection failure,
reference-only dependency or model/native test-patch conflict remains explicit;
a reference pass alone does not establish readiness.

## Remaining gates before inference

The plan is five tasks per repository and the same Native, Serena,
colbymchenry/codegraph and CodeNib conditions: fifteen tasks per arm, twelve
complete workflows and sixty task attempts. Exact prompts, source criteria,
task order and update material still require a separate pre-inference seal.
Use the existing runner and native grading adapter. The merged source
CodeNib backend includes #917; the withdrawn exploration-wording candidate
stays outside product code. Apply #918's authenticated result-file policy to
all optional products while leaving native tools available and calls voluntary.

Before execution, freeze actual installed products/providers, clean per-run
indexes, main and auxiliary model settings, SDK/CLI bytes, effective native
subscription authentication, time/usage caps and failure/stop rules. No
unrecorded API-key fallback is allowed. The authorized paid-API ceiling remains
$500; prior recorded SDK estimates are a lower bound of $24.3351375 plus
unknown usage, distinct from invoice charges. This preparation makes no new
model/provider inference request.

Keep complete workflow wall time as the primary endpoint, including first use,
setup, indexing, updates, failures and retries. Require correctness parity,
no additional serious error, at least 20% improvement against the strongest
eligible alternative and no more than 10% normalized model-usage regression.
A second independent frozen set must confirm any advantage. External
enrollment remains 0/5, with no outreach authorized.

## Merged repair acceptance

Main's [#917 Full CI](https://github.com/sysevol-ai/CodeNib/actions/runs/41067183408)
now passes unit, integration, serial integration, native SCIP and graph-consumer
jobs; model-dependent slow work is conditionally skipped. This closes the
previous queued engineering gate without establishing coding-task benefit.

Main's [#918 Full CI](https://github.com/sysevol-ai/CodeNib/actions/runs/41078886216)
passes unit and integration. Its serial/native/consumer jobs are conditionally
skipped for the helper-only diff; they do not repeat #917's full-chain checks.
All twenty [release verifier jobs](https://github.com/sysevol-ai/CodeNib/actions/runs/41078886398)
and [Docs](https://github.com/sysevol-ai/CodeNib/actions/runs/41078886614)
pass, with five tag-only publication skips. The product source remains
identical to #917; no public package release or new efficacy result is claimed.
