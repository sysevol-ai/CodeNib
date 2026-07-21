# CodeMiner Figure Style

`plot_style.py` is the single source of truth for paper figure typography,
colors, dimensions, axes, legends, hatches, and export settings.

## Layout

- Use a 7.0-inch double-column canvas for multi-panel paper figures.
- Keep profile point clouds on the 7.0-inch double-column canvas and use
  transparency rather than an oversized presentation canvas.
- Prefer horizontal `1 x 2` or `1 x 3` layouts for one experiment family.
- Use `2 x 2` only when all four panels are necessary to the same argument.
  Prefer a `1 x 3` main result plus standalone single-column audit views when
  one row would duplicate a result already established elsewhere.
- Use 3.4 inches only for a genuinely single-panel, single-column figure.
- Do not add a figure-level title. Use concise `(a)`, `(b)`, `(c)` panel titles.
- Keep explanatory prose in the caption. In-plot notes should identify only
  encodings or experimental conditions needed to interpret the marks.

## Visual Encoding

- Embedding model colors and names come from `plot_style.py`.
- Use color for embedding identity consistently across every figure.
- Use marker shape or line style for plans, rerankers, and backends.
- Do not encode model identity with unlabeled within-category offsets. Put each
  model on an axis or identify every offset in a legend.
- Label correctness-filtered counts as `admitted / measured`, not
  `pass / total`; reserve pass/fail for executable test outcomes.
- Distinguish measured observations from accounting projections. Use marks for
  observations or the trace-supported scale and lines/bands for projections;
  do not render every projected x value as a filled observation.
- Use major grid lines by default. Minor grids are reserved for log-scale plots
  where they materially improve value lookup.
- Put multi-panel legends above the axes and share them at figure level.
  Avoid repeating direct labels when the shared legend already identifies a
  series.

## Palette And Emphasis

- Category series use the low-saturation pastel palette in `MODEL_COLORS`:
  blue, apricot, sage, mauve, and terracotta.
- Reserve `PRIMARY_COLOR` (deep purple) for the proposed, canonical, or most
  important configuration.
- Use `BASELINE_COLOR`, `BASELINE_MID`, and `BASELINE_LIGHT` as the
  brown-to-orange baseline family. Do not introduce another saturated accent.
- Neutral references, exact methods, grids, and Pareto frontiers use gray.
- Never rely on color alone: pair model color with `MODEL_MARKERS`,
  `MODEL_LINESTYLES`, or `MODEL_HATCHES`.

## Print-Safe Fills

- Use diagonal, cross, dot, or grid hatches for categorical bars, boxes, and
  protocol blocks. Hatches should remain visible when printed in grayscale.
- Keep the fill pastel and the edge/line darker. The compact runtime figure
  uses solid fill for consumed tokens and hatched fill for saved tokens.
- Scatter and line plots should use distinct marker and line styles instead of
  artificial filled areas.

## Dual Axes

- Use dual axes only when the metrics have different units and the shared x
  categories make the comparison materially clearer.
- The preferred pattern is a left-axis stacked bar for composition/cost plus a
  right-axis line for improvement. Color the right spine and label to match the
  improvement line, keep only one grid, and draw explicit zero/guardrail lines.
- State the bar, hatch, and line encodings inside the panel in one short note.

## Typography And Export

- Figures use the shared Arial/Helvetica/Liberation Sans fallback stack.
- Base text is 7.0 pt, panel titles are 8.1 pt, and legends are 6.1 pt.
- Every main figure is exported as a 300 dpi PNG and a font-embedded PDF;
  rasterized artists inside the PDF must also be rendered at 300 dpi.
- Paper sources should include the PDF; PNG is retained for previews.

## Output Ownership

| Output prefix | Owning script |
| --- | --- |
| `eval_recall_at_k` | `draw_eval_accuracy.py` |
| `profile_l0_vs_l2` | `draw_profile.py` |
| `profile_query_time` | `draw_query_profile.py` |
| `faiss_index_ablation` | `draw_faiss_index_ablation.py` |
| `pareto_rerank` | `draw_pareto_rerank.py` |
| `lsp_replay_100` | `draw_lsp_replay.py` |
| `lsp_replay_latency` | `draw_lsp_replay_latency.py` |
| `graphrag_vs_rerank` | `draw_graphrag_rerank.py` |
| `retrieval_ablation_suite` | `draw_retrieval_ablation_suite.py` |
| `agent_runtime`, `agent_runtime_breakdown` | `draw_agent_runtime.py` |
| `agent_runtime_schematic` | `draw_agent_runtime_schematic.py` |
| `materialized_trace` | `draw_materialized_trace.py` |
| `lifecycle_stage_costs` | `draw_lifecycle_stage_costs.py` |
| `maintenance_lifecycle` | `draw_maintenance_lifecycle.py` |
| `serving_lifecycle_suite` | `draw_serving_lifecycle_suite.py` |
| `pareto_accuracy_vs_time` | `draw_pareto_dual.py` (legacy) |

`draw_pareto.py` and `draw_pareto_dual.py` are legacy exploratory analyses;
their outputs are not included by the paper sources.
