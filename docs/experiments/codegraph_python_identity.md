<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Recover Python physical definitions before graph construction

Keep the active SCIP route and recover source-bound definition identities in
its derived index. This addresses [#871](https://github.com/sysevol-ai/CodeNib/issues/871)
without promoting the generic LSP graph or changing the frozen benefit study.
The source fixes are unreleased; the published 0.2.4 wheel keeps its original
behavior. The broader objective [#874](https://github.com/sysevol-ai/CodeNib/issues/874)
remains active and promotion remains held.

## Decision and evidence boundary

The pinned upstream function-symbol implementation can skip intervening
function scopes when it selects an enclosing class. Its
[implementation](https://github.com/sourcegraph/scip-python/blob/3b50a0697bddf623f6d879b53bf66b0656309db3/packages/pyright-scip/src/treeVisitor.ts#L1094-L1100)
explains why one compiler ID can represent an outer method and several inner
functions. The earlier containment guard rejects cycles but cannot recover
the lost definitions, callers or noncyclic collisions.

Source syntax now inventories the physical class and function definitions in
indexed documents, plus compiler-declared bindings and owned instance fields.
Compiler observations bind parameters and references to those locations.
Definitions omitted by the provider, such as `_`, are explicitly reported as
source-only; their reference evidence is not synthesized.

A repeated declaration within one lexical binding retains its static set of
source targets. This includes property accessors and members of alternative
declarations of one class binding. Document-local IDs remain scoped to their
file. When an ID crosses different source scopes, positional definitions from
the existing basedpyright provider must match indexed source anchors. Parameter
reassignments, explicit `nonlocal` writes and starred parameter selections
retain the owning physical parameter. Unresolved targets fail the candidate.
These rules do not perform runtime dispatch or control-flow analysis.

LSP can also select a later instance-field assignment rather than its existing
constructor definition. Recovery requires the same compiler field at the exact
write anchor and an unchanged compiler-anchored first parameter of an
undecorated method in the same physical class. Existing field declarations
retain their static union; the write does not become a definition. Assignment,
loop, alias, deletion and match-pattern rebinding cannot prove that receiver.
Other parameters and decorated methods can still use direct declared targets,
but cannot supply this field-write fallback.

Both decoders consume the same normalized `index.decoded`. The original
`index.scip` is retained. Position columns become UTF-8 bytes; the pinned
Python provider's unspecified legacy columns are UTF-16. Legacy encoded test
files keep their original bytes, with the compiler/LSP's UTF-8 replacement
view explicitly reported. Source bytes are rechecked before publishing the
candidate, and the existing compiler source receipt remains the authority.

The graph's persisted field layout stays at schema 5. Symbol-graph builder
schema 8 invalidates manifest-bound artifacts produced by the older identity
algorithm. Managed graph setup, the source Make target and release smoke now
include basedpyright 1.39.8 alongside SCIP Python 0.6.6. The language server is
acquired once, only when needed, with bounded queries and cleanup registered
before startup. Saved source previews and queries do not start it.

## Validation

The [owned-field execution record](codegraph_python_owned_writes_2026_10_30.json)
retains the two failed self-repository admissions before inference. A captured
19-line provider reproduction fails before the fix; source-bound recovery and
receiver-refusal controls pass afterward. Fifty-three identity tests include
idempotence and serial/native fixture parity; the full unit tier passes 8,904
cases with 24 skips. A fresh product graph indexes all 1,168 selected Python
documents, producing 33,056 nodes and 159,157 edges, while all 1,757 tracked
source files retain their bytes. Real MCP returns the complete requested
function and two bounded static relationships with verified source. The first
graph request takes 247.965 seconds with prepared project/product runtimes,
two CPUs and 4 GiB, including provider installation; retain that entire cost.

The complete graph's decoder values and edges agree. The native decoder also
materializes an all-null optional `symbol_kind` column that serial decoding
omits; the initial strict-key comparison remains recorded separately. This
does not change either decoder or the persisted schema. The final additional
receiver guard replays the original raw compiler bytes and reproduces the
entire successful normalized index byte for byte. Original missing-native-extension and
standalone resolver-path preparation failures remain retained. No model run,
publication or task-benefit decision changes. Both metadata conditions must
use this same backend before a newly admitted and sealed development round.
Changed-file pinned hooks, strict docs, public-document boundaries and support
tables pass. A whole-repository hook run reports existing violations in twelve
unchanged files; its output and base-source hashes remain in the record, and
formatter changes outside this repair were reverted.

The [execution record](codegraph_python_identity_2026_10_28.json) retains
successful product controls, failed developer attempts and the implementation
hashes used for them. These are model-free correctness controls on previously
exposed sources with warm toolchains; their build times are not task-benefit
measurements. Sources and Git status must survive each build and query.

The IPython control uses `cd483f2f927c09be71dd5885618d7b2443a81b7a` and checks
the physical `ExecutionMagics.run` definitions at lines 541, 837 and 845. Exact
MCP lookups distinguish the outer method from both conditional inner targets;
the first inner function owns the `safe_run_module` reference. The pytest
control uses `8aba863a634f40560e25055d179220f0eefabe9a`, the source of the
failed admission, and checks the repeated `rep` class and assignment binding.
Serial/native parity compares names, graph edges, source anchors and vertex
attributes on the complete saved graphs. Neither graph has a containment
cycle or a vertex with several containment parents.

| Final product control | Indexed documents | Nodes | Edges | Successful MCP calls |
| --- | --- | --- | --- | --- |
| IPython | 294 | 6,629 | 22,372 | 11 |
| pytest | 199 | 5,687 | 11,285 | 5 |

Captured regression inputs also cover noncyclic collisions, Unicode and emoji
columns, class and attribute field collisions, local ID reuse across files,
property accessors, parameter reassignments and varargs. Deterministic checks
reject partial publication, source edits during lookup, failed startup and
cancellation. A native regression retains the IPython module-alias mismatch
found during parity validation. The fixture README records the provider pins
and the distinction between compiler references and static target sets.

The final unit tier passes 8,808 checks. The native tier passes five C++
executables and 252 Python checks; four cached-repository parity controls skip
locally. Keep those skips separate from the fresh IPython/pytest parity and
the fixture gates. The added decoded-input consistency regression is covered
by the final unit tier and the fresh product controls. Broader provider accuracy, representative update cost,
automatic rebuilding and observed agent use remain open under A0.

No model sessions are added and no frozen v1 timing, source grade, fallback or
usage observation is replaced. The original cohort still provides insufficient
evidence for a 20% gain. External recruitment remains unavailable and enrolled
participants remain zero.
