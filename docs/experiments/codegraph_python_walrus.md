# Python compiler identity and reference-source admission

The [#933 candidate](https://github.com/sysevol-ai/CodeNib/pull/933) repairs two
source-authenticated Python indexing failures. An installed control now builds
the complete selected Pylint graph and returns byte-exact source around its
recorded incoming references. This establishes construction and selected source
delivery, with no new model call or demonstrated comparative workflow benefit.

## Reproduction and scope

A new model-free source-consumer control uses original Pylint commit
`40cc2ffd7887959157aaf469e09585ec2be7f528`, two CPUs and 4 GiB, with an installed
wheel for merged #931. It asks for source around incoming references; it does
not replay or replace any recorded model observation. All four baseline graph
requests fail before returning relationships. The first error is
`invalid global Python SCIP symbol: 'text.'` in
`tests/functional/a/assign/assignment_expression.py`.

The retained raw compiler input contains two unqualified `text.` occurrences
at a comprehension's read and assignment target. A separate three-line fixture,
indexed by the same pinned producer, reproduces it with a further read after
the comprehension. Its exact source and decoded output are retained in
`test/fixtures/scip_python_identity/module_walrus.*`; replay needs no running
language server or model.

The [control packet](codegraph_python_walrus_2026_10_30.json) retains 58 original
records in four bounded parts, including preparation failures, installed
prototype inputs, failed replies and verification logs. Raw full-project
compiler artifacts stay local with hashes. All eight owned containers are
independently absent, and all 5,294 prior confirmation inputs are unchanged.

The [SCIP symbol grammar](https://github.com/scip-code/scip/blob/main/scip.proto)
requires a global owner before a term descriptor. Python's
[assignment-expression scope rules](https://peps.python.org/pep-0572/#scope-of-the-target)
place a comprehension's assignment target in its containing scope; a lambda
introduces its own scope. Spelling alone cannot establish a global identity.

## Recovery boundary

The existing derived-index normalizer recognizes an exact module-level
assignment-expression target. Qualification requires a compiler-provided
module namespace, a matching source definition range and name, matching
spelling at every occurrence, and no definition of the same malformed ID in
another scope. Function/lambda bindings and ordinary malformed assignment IDs
retain their strict behavior. Identities remain separate across documents.

Only occurrences carrying that same producer ID within the authenticated
document are qualified. Symbol documentation and relationships retain their
metadata. No source read, reference or test is discarded, and no call edge is
invented. The normalizer still publishes its copied candidate only after all
source/target validation succeeds; an error leaves the original input unchanged.
Reports retain original/qualified IDs and source hashes. Documents without
module assignment expressions take the existing path without another
occurrence scan.

Symbol-graph builder identity advances from 8 to 9 so old manifest-bound
artifacts rebuild through the existing path. Persisted graph layout remains
schema 5; serial and native decoders consume the same normalized input. There
is no new backend, storage route or automatic source rebuilding.

## Corroborating decorated field writes

The first installed prototype passes the malformed-symbol stage, then refuses
a write at `tests/utils/unittest_ast_walker.py:58`. Two physical classes named
`Checker` share one compiler field ID. The decorated method cannot inherit the
undecorated-instance fallback merely because its argument is named `self`.

A separate language-service capture at that original write returns both its
constructor field at line 54 and the write itself at line 58. The final
normalizer first collects direct physical definitions from the whole response.
A decorated write requires an independently selected field with the same
compiler ID and physical class, plus a regular first parameter whose exact
receiver anchor is compiler-authenticated and never rebound. It recovers only
the corroborating selected fields. Response order does not matter, and no
decorator behavior or new field definition is inferred.

Every returned target still validates before atomic publication. A write-only
response, an unrelated or other-class target, a different/missing receiver
anchor, a variadic receiver and parameter rebinding fail without changing the
input. Uncorroborated static-method and other-argument writes remain rejected;
their direct compiler-declared field targets remain valid. A new producer/LSP
capture in `decorated_field_writes.*` reproduces the actual shape, including a
later read whose response also selects the decorated write.

## Verification and remaining gate

The original module regression fails with the invalid-global error, and the
new decorated capture fails at its unresolved write before the repair.
The final focused identity/compiler surface passes 532 tests, including
serial/native parity, both response orders, idempotence and atomic rejection.
The complete unit tier passes 8,984 tests with 24 skips, 221 heavier cases
deselected and 46 existing fork deprecation warnings. It runs under `umask 022`
with the existing native decoder and a task-local extracted `libre2` dependency.
Earlier environmental/native diagnostics stay in the original packet.

Two new installed controls use the same pinned Pylint commit, two CPUs and
4 GiB, without network access or model inference. The final control verifies
all 486 selected installed Python/native files against the original #931 wheel
and its two exact saved candidate modules, from outside the analyzed checkout.
It admits all 878 selected compiler documents, 10,180 nodes and 23,148 edges.
Identity recovery answers all 59 positional queries. The existing repository
filter stays unchanged; partial indexing, language fallback and extra test
exclusions remain disabled. Both controls preserve every tracked source byte
and remove only their own label-verified containers.

Four `explore_context` replies use the persisted compiler graph for routes and
incoming dependencies while BM25 supplies lexical candidates. Their 28 source
windows all match original bytes and fingerprints. All 11 returned route or
dependency function definitions are complete in those windows. The two
`_compute_sims` probes include both returned caller locations (393 and 836);
the `_check_astroid_module` probe includes its returned caller at 1173.
These five observations include the repeated fast/balanced pair, not five
distinct callers.

The bare `add_message` reference query returns an ambiguity diagnostic even
though its MCP transport reports no error. Keep that original reply. Exact
names then return 40 references for `BaseChecker`, 26 for
`MessagesHandlerMixIn` and zero for `UnittestLinter`. The first reaches the
requested `top_k=40` cap; none is presented as exhaustive runtime coverage.
Source verification and model-source-read credit are separate: no model
receives these development replies.

The [new admission packet](codegraph_python_identity_source_2026_10_30.json)
retains both installed module snapshots, original replies including the
ambiguity, exact-name followups, source audits, captures and test logs.
Full raw compiler/graph artifacts stay local with byte lengths and hashes.
The earlier failed packet is immutable and remains linked above. Complete
construction and these selected source checks now pass; exact release-artifact
admission, voluntary model use, workflow quality/time, two successful
independent confirmations and external reuse remain open.

All preparation/controller failures and original replies remain retained. The
completed source-payload cohort and hold decision are unchanged; the further
fixed confirmation set remains unopened. No new model call, external user,
independent success or comparative speed claim is produced.
[Adoption A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874) remain active.
