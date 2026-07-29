# Solo trial analysis

This directory stores post-hoc analyses of DeepSWE solo trials used to design
context-injection and Guardian experiments.

Each task directory contains:

- `report.md`: human-readable cross-trial analysis;
- `trials.json`: normalized per-trial diagnoses for later aggregation;
- `oracle_ceiling_prompt.md`: task-specific, oracle-informed context for a
  diagnostic upper-bound experiment;
- `generic_review_prompt.md`: benchmark-generic context distilled from the
  observed capability gaps.

## Completed analyses

| Task | Passes | Primary review problem |
| --- | ---: | --- |
| `igel-persist-feature-schema` | 3/12 | Lifecycle/options/integration seams |
| `textual-kitty-key-phases` | 4/12 | Protocol grammar versus public semantics |
| `ipython-session-bundle-replay` | 8/12 | Host-framework behavioral boundaries |
| `fastapi-implicit-head-options` | 4/12 | Framework-wide propagation and invariants |
| `sqlite-utils-safe-import-checkpoints` | 8/12 | Transaction ownership and CLI integration |

See `cross_task_synthesis.md` for the 60-trial comparison and
`universal_generic_prompt.md` for the proposed non-oracle hardcoded
intervention.

## Evidence labels

- **Trace**: evidence from the agent transcript, submitted patch, authored
  tests, or commands it ran.
- **Verifier**: observed public outcome and per-test result from the held-out
  verifier.
- **Oracle**: information learned from the reference solution or held-out test
  implementation after the trial completed.
- **Inference**: an interpretation supported by the preceding evidence.

Oracle-informed prompts are deliberately separated from generic prompts.
Results obtained with an oracle-informed prompt are diagnostic ceiling results,
not valid benchmark scores and not evidence that a generic Guardian discovered
the same information.

## Diagnosis taxonomy

- `option_semantics`: the implementation invented, rejected, or omitted a
  composition or boundary value not justified by the objective.
- `error_propagation`: validation detected the problem but an outer boundary
  swallowed, replaced, or mistranslated the error.
- `mode_coverage`: a required operation, model family, or lifecycle mode took
  a distinct path that was not exercised.
- `override_compatibility`: a change broke caller-controlled paths,
  configuration, dependency injection, or another existing integration seam.
- `complete`: all held-out feature and regression checks passed.

Task-specific JSON files also use narrower categories where that distinction is
analytically useful, such as `protocol_grammar`, `capture_lifecycle`,
`dispatch_contract`, and `transaction_ownership`.
