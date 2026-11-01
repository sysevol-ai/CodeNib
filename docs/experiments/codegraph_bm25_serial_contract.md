# Verify test exclusion before the candidate limit

The serial BM25 graph test now checks that filtering retains implementation
results and fills the available eligible slots. Filtering before a candidate
limit can replace test hits with later source hits, so the returned count can
stay at the requested limit.

## Main failure and corrected contract

Merged [#951](https://github.com/sysevol-ai/CodeNib/pull/951) repairs test
discovery and moves test exclusion before both candidate limits. Its main
commit `c25f191c` passes all twenty executed Release verifiers and Docs.
Trusted [Full CI](https://github.com/sysevol-ai/CodeNib/actions/runs/41244942108)
passes preflight, vendored core build, unit and integration, then stops in the
[serial job](https://github.com/sysevol-ai/CodeNib/actions/runs/41244942108/job/124667934973).
The old test expects filtering to make the result list shorter and fails at
`20 < 20`. SCIP/core and graph-consumer jobs are consequently skipped.

The failure reproduces locally. The corrected test requires actual test hits
in the fixture, excludes test files, retains a result from the sample
implementation, fills the eligible result capacity and preserves the original
implementation prefix. The test notes also require the real SCIP-backed
module when BM25 eligibility or candidate limits change; the unit tier excludes
this serial surface.

## Verification and remaining gates

The same stronger test fails its implementation-presence assertion on actual
prior CI wheel `737d768c` and passes on repaired CI wheel `8b3ca291`. Each
execution verifies every packaged product byte and imports only its selected
installed prefix. The earlier controller descriptor predicted the later
capacity assertion; the observed earlier failure and descriptor note remain
retained without changing the test or outcome.

The focused graph and real-ranker tests pass 13 cases. After installing the
declared Python language-service pins through the repository CLI, the complete
local serial tier passes 26 cases with 23 conditional skips, 9,190 marker
deselections and five warnings. Original PATH and missing-language-service
failures remain retained. The skipped local cases do not establish the trusted
runner's native/consumer coverage.

The [exact packet](codegraph_bm25_serial_contract_2026_10_31.json) retains 28
public records and the hash-bound complete original CI log. Product code,
build inputs and dependencies are unchanged. [#952](https://github.com/sysevol-ai/CodeNib/pull/952)
is merged as `abeab984` after three executed final-head checks pass with one
explicit Docs deploy skip, no unresolved review thread, an identical reviewed
tree and an inspected real-newline squash message.

The corrected [trusted run](https://github.com/sysevol-ai/CodeNib/actions/runs/41250167117)
passes preflight, vendored core build, unit, integration, serial integration
(38 passed, 11 skipped, 9,190 deselected, five warnings), SCIP/core and graph
consumers. All seven executed jobs pass and slow is explicitly skipped; the
repaired commit's complete chain is terminal success. All twenty executed
Release verifiers and Docs pass. Later main changes have separate runs. The original
failed run remains a failure. The existing Ask check remains 2/3 and no model
SDK question is part of this test correction. The separate
[root-maintenance diagnostic](codegraph_root_maintenance.md) retains its own
four new questions and failed acceptance. The broad benefit objective stays
active.
