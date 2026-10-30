# Reduce repeated parsing in Python's first graph query

Python graph preparation converts its binary compiler index to text, then
parses and scans that large text again. This iteration removes redundant
consumer work while preserving source identity, graph contents and exact
definition/reference positions. It is an engineering control for the active
[net-benefit objective](https://github.com/sysevol-ai/CodeNib/issues/874), not a
new model comparison or an independent confirmation set.

## Input authority and publication

The decoded file remains authoritative. A raw binary index can replace text
parsing only when serializing it reproduces **all decoded bytes exactly**.
Missing, malformed or different raw data uses the existing text parser. No
parsed object survives the call or becomes a second persistent cache.

Identity recovery produces one normalized index for both consumers. The graph
decoder still reads its text artifact; the exact-position index reads the
already parsed structure. Their handoff includes the normalized text's size and
SHA-256. The shared consumer checks it before decoding and after both consumers,
even when artifact receipts are not requested. A mismatch clears receipts and
prevents graph saving. The existing source checks and atomic normalized-file
replacement remain in force.

The persisted graph and occurrence-index schemas do not change. Negative
coordinates are rejected in both occurrence constructors, so a malformed
four-number range cannot lose its negative entry and become a different
three-number selection. Other language routes keep their existing text path.

## Captured-input diagnostic

A model-free control uses the exact prior CodeNib index under two CPUs and
4 GiB. It retains 69,666,898 binary bytes and 157,336,322 normalized text bytes.
The text occurrence scan takes 28.383 seconds; the prototype structure path
takes 4.122 seconds. All 782,272 occurrence rows, file/symbol lookup tables and
encoding agree. Text-to-structure parsing separately costs 22.601 seconds,
while binary parsing and text serialization cost 0.093 and 5.508 seconds.
These are component observations, not first-query or whole-task savings.

## Complete queries and fixed-input processing

The [execution evidence](codegraph_python_first_query_2026_10_30.json) retains
one fresh-query pair on the same source/image under two CPUs and 4 GiB, followed
by a reverse-order pair on one identical binary artifact. The second control
uses an explicitly preinstalled provider and excludes compiler generation and
installation. These are exposed development controls, not model task runs.

| Observed endpoint | Unmodified baseline | Candidate |
| --- | --- | --- |
| First complete MCP exploration | 277.044 s | 205.341 s |
| Initialization, connection and first exploration | 286.423 s | 214.798 s |
| Following query in the same connection | 0.843 s | 0.818 s |
| Same-binary decode and processing | 92.862 s | 51.372 s |
| Normalized-cache reprocessing | 87.087 s | 66.027 s |

The first-query difference is 71.703 seconds (25.9%), but the unchanged compiler
also varies by 30.564 seconds: 127.617 versus 97.053 seconds. Local unit
verification overlaps the baseline on a 20-CPU host; container quotas do not
pin CPU affinity or establish identical host load. Do not attribute the whole
observed difference to this patch. After unit verification ends, the fixed-input
reverse-order control removes compiler generation: first processing saves
41.490 seconds (44.7%) and cached reprocessing saves 21.060 seconds (24.2%).
One pair per setup does not establish general performance or task benefit.

Both fresh conditions preserve all 1,757 tracked source files plus the same
native helper. Their raw and normalized indexes agree byte for byte; all
33,056 ordered vertices and 159,157 edges agree, including attributes and the
canonical query digest. All 782,272 occurrence rows and both lookup tables
agree. Both actual MCP replies are byte-identical: the first includes seven
files/three bounded relationships, and the following query five files/two
relationships. These operator replies do not establish model delivery through
the stopped SDK bridge. Both fixed-input phases reproduce the complete prior
normalized bytes, graph surface and position payload.

The position artifact falls from 104,730,061 to 80,330,353 bytes because the
structure path shares one normalized file-path string per document. Pickle
object memoization differs while all persisted schema keys and values agree;
this is not byte identity of the pickle or a storage-boundary change.

Each installed runtime is hash-checked outside the target checkout before
initialization. The original failed admission is retained: a `python -c` proof
imports checkout source through its working directory, and the module check
rejects it before any product query. No original product/model outcome is
replaced.

## Verification and remaining gates

Seventy-eight targeted controls cover local/global symbols, conditional targets, enclosing
ranges, escapes, path filtering, encodings, cached recovery and persisted
positions. Serial and C++ graph consumers retain the same ordered query
surface. Deterministic replacements before or during graph decoding reject
publication, including calls without receipt capture.

The initial unit run lacks the required local workspace extension. Its next
run reaches an unchanged file-mode assertion under `umask 002`; the same case
fails on unmodified source. Preserve both failures and the verified ABI3/C
source binding. The controlled unit environment uses `umask 022` and passes
8,922 cases (24 skips), without changing product code or tests to hide those
outcomes. The MCP 2.3 surface passes 432 cases (seven skips). Three actual
container finalizers also recover created-but-unrecorded containers while
preserving other owners. The first recovery-test harness lacks its clock
binding; retain that failure and the successful corrected harness separately.

Cold-query execution and full graph/position parity pass for this source.
The evidence packet reconstructs 89 original files in two parts, verifies
5,855 earlier sealed inputs and 413 preceding JSON records unchanged, and
confirms all 17 owned control containers absent. Original packaging cannot
read private root-owned container outputs; ownership export changes only those
output owners, checking identical contents in the helper and host before the
successful packet. Retain that failed packaging attempt separately.
Authenticated MCP spill-file delivery, useful complete model work,
representative updates, both independent
whole-task confirmations and external seven-day reuse remain separate open
gates. Preserve the stopped task-fit round and every prior negative outcome.
