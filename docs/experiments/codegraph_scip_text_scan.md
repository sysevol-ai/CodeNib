# Faster scanning of saved SCIP graph inputs

The shared graph decoder now skips ordinary text spans while preserving quoted
literals and balanced braces. On one complete saved CodeNib Python input,
three fresh decoders per implementation reduce median decode time from
13.316s to 6.409s (51.87%). Every ordered vertex/edge attribute and endpoint
agrees. This is source-checkout component timing, not a complete preparation
or coding-task result.

The [exact packet](codegraph_scip_text_scan_2026_10_30.json) retains 126 text
records in two bounded parts, including all unsuccessful harness attempts.
The full input, output and wheel artifacts remain local and hash-bound.
[Adoption A0](../adoption_roadmap.md) and
[the pilot](codegraph_net_benefit.md) still hold whole-workflow promotion.

## Change and compatibility boundary

The compiler's decoded SCIP text contains many occurrences and symbol strings.
The former helpers visit every character in Python to count braces or decode
the symbol literal. The replacement uses the regex engine to skip ordinary
spans. Quoted literals consume escaped character pairs as one token, so
embedded braces and escaped quotes do not change block depth. Symbols without
escapes return directly; escaped symbols keep the previous decoder's values.

Keyword boundaries, whitespace handling, unknown-escape behavior and incomplete
block handling remain unchanged. This preserves the existing shared decoder
contract; it does not implement a new general TextFormat parser. No source
selection, currentness check, artifact receipt, graph schema, builder identity,
provider route or default decoder backend changes. C++ remains opt-in and its
implementation is unchanged.

## Complete saved-input comparison

Both workers consume the same 156,941,727-byte normalized input for all 1,172
selected Python documents. The baseline substitutes only the exact two original
scanner functions frozen from `e0b91b2b`; all other decoder code is shared.
Each iteration constructs a fresh decoder. File reading and graph construction
are timed; input/output hashing is outside the timer. Baseline and candidate
workers run sequentially, with no concurrent local test or benchmark.

| Scanner | Three decode times (seconds) | Median |
| --- | --- | --- |
| Original | 13.316, 13.334, 13.297 | 13.316s |
| Candidate | 6.564, 6.409, 6.364 | 6.409s |

All six outputs contain 33,178 nodes and 159,676 edges with the same query
surface. A separate untimed check compares every ordered vertex attribute,
edge attribute and endpoint; both canonical outputs have SHA-256
`9bce5bbc19a0f2d1463c32cea7dccb94e9ea053a25363320da19624fde0e3385`.
There is one exposed artifact and three repeats, without independent repository
replication or isolated host-load attribution.

## Verification and installed replay

Twenty focused cases cover escaped quotes/braces, Unicode, empty symbols,
unknown escapes, keyword boundaries, incomplete blocks and long literals.
A deterministic development differential compares both helpers against the
exact original functions on 35,000 ordinary, escaped, malformed and truncated
inputs; all results agree. The SCIP unit tier passes 368 cases, and five
existing synthetic C++ parity cases pass. The full local unit tier passes
9,009 tests, with 24 skips, 221 marker deselections and 46 existing fork
deprecation warnings.

A locally assembled developer wheel replaces all 485 Python entries in the
verified earlier CI archive with exact current source bytes, retaining native
and frontend assets and regenerating `RECORD`. It is neither a source-built
nor CI-produced candidate or public release. From outside the analyzed
checkout, every installed Python/native byte matches that wheel; native
protocol 6 imports successfully without a source overlay.

That installed package replays the normal stages after a fixed raw compiler
artifact: text decoding, Python identity normalization, serial graph
construction, source filtering, derived indexes and persistence. The normalized
text matches exactly; the full query surface, all 786,078 ordered occurrences,
source-selection report and decoded/graph receipts verify. The untouched
2,390-file original profile checkout matches its Git blobs. This replay skips
compiler generation and is functional admission, not a timing endpoint or full
MCP workflow.

| Retained harness failure | Correction |
| --- | --- |
| Wheel assembly's exact source-difference list omits the already merged publication-gate change | Admit exactly the three known CI-to-current Python changes; retain full 485-file/native byte verification. No container was created. |
| Installed attempt 2 compares detail arrays with persisted manifest counts | Compare the existing compact representation exactly and retain both full and compact reports; keep the original failure. |
| Installed attempt 3 sets a private receipt flag that the pipeline resets | Use `capture_artifact_receipts=True`, matching the production caller. Keep the original failure and its outputs. |

The final attempt passes. All three exact owned container identities are
independently absent. No product check is removed or relaxed to fix a harness
assumption.

## Remaining product gate

The [previous complete preparation pair](codegraph_preparation_policy.md)
remains 185.678s → 188.577s for first requests and 217.153s → 212.595s after
editing, with no clear overall gain. Its first full artifact snapshot was not
exported; that prospective quality gap stays recorded. This scanner candidate
is not measured in that pair or the frozen coding cohort.

SCIP generation and source-update costs remain targets. Verify the complete
workflow before claiming a broader gain, and retain authenticated source
selection rather than narrowing coverage or promoting an evaluation-only
provider. No model question, independent success or external participant is
added; all completed cohorts and unopened confirmation material stay intact.
