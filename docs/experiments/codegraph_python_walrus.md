# Python module assignment-expression identities

This [draft #933](https://github.com/sysevol-ai/CodeNib/pull/933) repairs a
source-authenticated Python indexing failure: scip-python
0.6.6 can omit the module owner from a variable assigned with `:=` inside a
module comprehension. The complete Pylint graph remains unadmitted because a
later decorated instance-field write fails the existing ownership guard. No
model or task benefit is established.

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

## Verification and remaining gate

The regression fails with the original invalid-global error. The focused
identity/compiler surface passes 261 tests, including serial/native parity on
the captured fixture, document isolation, idempotence, preserved metadata and
atomic rejection. Initial native import failures and an owner-only diagnostic
with native skips remain recorded; later native-enabled verification executes
the decoder tests with a task-local extracted Ubuntu `libre2` dependency.
The complete unit tier passes 8,971 tests with 24 skips and 221 heavier
cases deselected. An initial run stops at an unrelated file-mode assertion
under the shell's group-writable umask; the unchanged test passes under the
repository verification `umask 022`. Pinned formatting, strict MkDocs, public
document boundaries and support-matrix checks pass. Formatting preserves the
Python ASTs exercised by the complete unit run.

The installed two-module prototype passes the malformed-symbol stage on the
same complete Pylint source, then refuses an unresolved field-write target at
`tests/utils/unittest_ast_walker.py:58`. That method is decorated and belongs
to a class whose name also appears in another physical scope. The conservative
receiver guard is unchanged. Complete construction and source/reference checks
require evidence binding this write to the correct existing class field. Do
not exclude the test file, accept partial indexing or count disappearance of
the first error as graph success.

All preparation/controller failures and original replies remain retained. The
completed source-payload cohort and hold decision are unchanged; the further
fixed confirmation set remains unopened. No new model call, external user,
independent success or comparative speed claim is produced.
[Adoption A0](../adoption_roadmap.md) and
[#874](https://github.com/sysevol-ai/CodeNib/issues/874) remain active.
