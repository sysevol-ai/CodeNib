<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Quality-constrained localization cost

## Claim boundary

`codenib-quality-cost-report` measures cost per **successful localization**. It
does not measure patch correctness, issue resolution, Copilot premium requests,
or self-hosted GPU cost. One attempted query repetition is successful when its
`answer_blocks.recall@5` reaches the declared threshold. Misses and recorded
infrastructure failures stay in the denominator.

An arm is eligible for cost selection only when the lower bound of its paired,
repository-clustered 95% confidence interval is above the declared recall
margin relative to `grep_only`. The report then minimizes tokens per successful
localization among eligible arms. This ordering prevents a cheap but
quality-regressing arm from winning.

## Accounting rules

- Inputs are joined by exact `(model, query_id, rep, arm)` identity. Duplicate
  cells and unmatched arm denominators fail by default.
- The compared arms can be pinned with repeated `--arm` options. Unrelated
  policy variants in the same experiment roots cannot enter the selection.
- Retry failures are linked to the final cell within the same input root. If a
  retry lacks token or cost data, the affected arm's complete cost is marked
  unavailable rather than undercounted.
- `total_tokens` is the primary historical measure. New query sweeps also
  preserve prompt, completion, cache-read, and cache-write token classes.
- USD is either an explicitly labeled, unpinned runtime estimate or an offline
  projection from a content-hashed pricing snapshot. A zero emitted for an
  unpriced local model is unavailable, not free.
- Shared index build cost is optional and reported separately at declared query
  horizons. It is never silently added to model-call cost.

## Five-model validation

The retained 500-query synthesis study uses the same three predeclared arms and
the paper's 5 percentage-point recall margin:

| model | selected policy | tokens / successful localization | reduction vs. grep/read | USD status |
| --- | --- | ---: | ---: | --- |
| Claude Haiku 4.5 | eager | 114,855 | 49.2% | recorded runtime estimate |
| Gemini 2.5 Flash | compact | 20,240 | 68.1% | recorded runtime estimate |
| Gemma 4 12B | compact | 20,145 | 87.5% | unavailable |
| Qwen3.5 9B | compact | 115,404 | 53.9% | unavailable |
| Qwen3.5 27B | compact | 74,772 | 54.4% | unavailable |

The Qwen3.5-27B source contains one failed eager retry without usage fields.
The report therefore withholds complete eager cost for that model; the selected
compact arm is unaffected. Cloud USD values are historical LiteLLM estimates,
not immutable repricing and not GitHub or Copilot credits.

## Reproduce without model calls

```bash
codenib-quality-cost-report \
  "${CODENIB_RESULTS_DIR}/haiku_compare_py" \
  "${CODENIB_RESULTS_DIR}/haiku_compare_multilang" \
  "${CODENIB_RESULTS_DIR}/haiku_compare_cpp" \
  "${CODENIB_RESULTS_DIR}/haiku_synth_compact" \
  "${CODENIB_RESULTS_DIR}/gemini25_flash_synth_runtime_v1" \
  "${CODENIB_RESULTS_DIR}/gemma4_12b_synth_runtime_v1" \
  "${CODENIB_RESULTS_DIR}/qwen35_9b_synth_runtime_v1" \
  "${CODENIB_RESULTS_DIR}/qwen35_27b_synth_runtime_v1" \
  --output-dir "${CODENIB_RESULTS_DIR}/quality_cost_v1/five_models" \
  --arm grep_only \
  --arm preinj_eager \
  --arm preinj_eager_compact \
  --noninferiority-margin 0.05 \
  --bootstrap-samples 5000 \
  --bootstrap-seed 0
```

The command emits `quality_cost.json` and `quality_cost.md`. The JSON preserves
the metric contract, bootstrap settings, per-input model counts, denominator
audits, retry coverage, arm-level measurements, and the constrained optimum.
