# Source-first exploration in deferred graph mode

The candidate makes an ordinary cold `explore_context` query return verified
ranked source before asking the user to pay for a complete graph. It changes
the default work performed by the opted-in deferred mode; it does not accelerate
an equivalent graph computation or establish an advantage over another product.
Complete coding-task benefit and both independent confirmation gates remain open.

## Product behavior

In deferred mode, omitting `include_dependencies` or passing JSON `null` uses
source retrieval when no complete graph is loaded. Explicit symbols or
`include_dependencies=true` still request complete graph preparation. Once a
complete graph is published, an ordinary query reuses it. Explicit `false`
continues to omit dependency expansion. Prebuilt mode preserves its previous
default. The existing source checks, generation lock, delivery ledger and
same-connection reload own these transitions; no background builder, new
provider, partial graph or storage boundary is introduced.

The tool schema, description, instructions and public guide state this policy.
A source-only reply reports dependency expansion disabled and graph/provider
coverage unavailable. It must not be described as a delivered call graph.

## Why the current full-graph route remains expensive

The fixed Astropy, SymPy and Seaborn admissions retain all selected sources,
including tests. Native tools, Serena and Codegraph each return the selected
complete definition on all three repositories. CodeNib's original strict
full-graph admission succeeds on Astropy and Seaborn, but SymPy fails under the
same two-CPU, 4 GiB container and 600-second compiler limit. The original
612.660-second MCP failure includes a 600.982-second compiler timeout.
Provider and controller failures remain separate in the evidence.

A separate SymPy heap diagnostic with a 2,560 MiB V8 heap still fails under
4 GiB, with a reported V8 out-of-memory termination after about 93 seconds.
A 32 GiB reference container with the existing 16 GiB heap allowance completes
the full graph in an 852.887-second first MCP request; sampled Node RSS peaks
at 11,490,196 KiB. The latter is a semantic/resource reference, not an admitted
4 GiB workflow, an acceleration result or authorization to raise the product
memory requirement. No heap or full-graph coverage change is shipped.

Separate probes of the existing live LSP client find two Astropy references
but no references for the selected SymPy and Seaborn probes. Empty replies do
not prove the absence of references. No live LSP product route is promoted.

## Real installed controls

The parent and candidate wheels retain the same 485 Python files and native
extension identity, except for the two intentional server/prompt changes.
Every installed Python/native hash is checked outside the analyzed checkout.
Each control gets its own pinned, single-commit source and common neutral
project environment, fresh index state, and two-CPU/4 GiB container. Providers
are initially absent. Complete original JSON-RPC replies and source versions
are retained; no model, subscription session or paid API call is made.

The identical first RPC contains only `query`. The candidate returns the
complete requested definition, a verified source fingerprint and disabled
dependency expansion on all three sources, leaving provider discovery and the
BM25-only manifest unchanged. The original first candidate observations are
0.487 / 0.715 / 0.192 seconds for Astropy / SymPy / Seaborn. These measure the
tool response after installation and BM25 preparation. They exclude those
setup costs and do not score recall, correctness of a model answer, or a
complete workflow.

Fresh parent-wheel controls send the same `query`-only arguments and retain
their original default graph work. Their first responses take 128.513 seconds
on Astropy and 34.894 seconds on Seaborn; SymPy returns a graph-preparation
error after 309.321 seconds with its compiler killed by SIGKILL. The candidate
returns source without those relationships, so these are different output
contracts rather than equal-output graph acceleration. Per-control setup and
observed first-use totals remain in the receipts, outside the isolated query
times above. No speedup percentage is inferred.

Seaborn's final same-connection control additionally checks explicit complete
graph preparation, automatic warm reuse, explicit `false`, rejection after a
controlled source addition, and exact new helper source after an explicit
deferred rebuild. The old graph remains in the manifest as stale; it is not
used to expand the new source. A separate real compiler-timeout control checks
source recovery in the original connection with a prospectively selected
10-second timeout. It does not replace or retime the original 600-second
resource failures.

Controller mistakes remain failed: an explore-only connection cannot call
`get_manifest`, fresh graph status is `fresh`, and rebuilding or failing a
graph retains a stale/failed manifest entry. Corrected, separately versioned
controls test these existing contracts without changing product code or
discarding the original observations. Early repeated diagnostic log names
can overwrite an earlier status log; those original per-command digests are
not complete retained stdout evidence. The immutable JSON-RPC replies and
per-phase manifest snapshots remain the source of the reported observations.
Later controls preserve distinct log names.

## Verification and remaining gates

The candidate passes 8,946 unit cases with 24 skips, and 433 MCP 2.3 cases with
seven skips. Strict MkDocs, public-document boundaries and the support matrix
pass. The installation/controller/resource observations, original failures and
exact scripts are retained in the [evidence packet](codegraph_source_default_2026_10_30.json).
Local controls do not establish remote release acceptance or a public package
release.

The first old-input integrity check verifies all 5,855 original entries. A
subsequent check detects that the shared `~/.local/bin/claude` launcher now
resolves to 2.1.314. It appears in two old seals: 5,853 live-path entries still
match, and the exact original 2.1.310 binary remains retained at its versioned
path, verifying all 5,855 original payloads. Preserve both seals and the first
packet, disclose the path drift, and leave the user's current launcher alone.
The new runner already uses the versioned 2.1.310 path; these model-free
controls invoke no model session. No old model score or timing is replaced.

The first selected set is now development admission: its resource failures
informed this candidate before any model inference. Keep its fixed membership,
all negative outcomes and the original native grading. Results on it cannot
satisfy either independent success confirmation. The reserved second set stays
unopened; fix another disjoint set before any independent confirmation runs.
The initial admission packet remains immutable and records the original plan.

Fifteen draft tasks and the common four-arm runner exist, but there is no new
pre-inference model/runtime/resource seal or comparative result. Keep the
whole-workflow endpoint, correctness parity, serious-error rule, 20% improvement
threshold and normalized-usage guard. The previous stopped cohort stays closed.
Five-user external enrollment remains zero and no outreach is authorized.
