<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Diagnosing semantic / lexical leak in synthesized retrieval queries

Tooling that operationalizes the Tier-1 and Tier-3 checks proposed in
[issue #130](https://github.com/sysevol-ai/CodeNib/issues/130).

## Why

Running `examples/eval_synthesized_queries.py` with `Salesforce/SweRankEmbed-Small`
against the current 96 synthesized behavioral queries scores file-recall@10 = 0.92
and symbol-recall@10 = 0.73. The score is saturated relative to the same embedder
on calibration data (0.39–0.77 on `codenib-dataset-base`), suggesting the
synthesizer prompt is leaking domain vocabulary into the query. The scripts below
let us measure how much.

## `scripts/diagnose_query_leak.py`

Per-query leak detector and slicer. Stdlib only, runs on a queries directory
plus the `--result-path` JSON of an `eval_synthesized_queries.py` run.

```bash
# All three Tier-3 checks at once
python scripts/diagnose_query_leak.py \
    --queries synthesis_output/ \
    --eval-result eval_results/synthesized_embedding__Salesforce__SweRankEmbed-Small.json \
    --metrics-k 1 5 10 \
    --emit-leak-report eval_results/leak_signals.json \
    --emit-masked-queries synthesis_output_masked/
```

The script reports:

1. **Leak prevalence**: `fn_leak` (GT filename in query), `sym_leak` (last sub-token
   of a GT symbol in query), `path_leak` (any path-segment token in query),
   `any_leak` (union).
2. **Length distribution**: ≤30 / 31–60 / >60 words.
3. **Recall slices** (when `--eval-result` is provided):
   - Leaked vs clean — quantifies how much of recall is carried by literal token leak.
   - Length-stratified — answers whether the 0.92 file-recall lives entirely in the
     long-prose bucket.
4. **Masked emission** (when `--emit-masked-queries` is set): writes a parallel
   queries directory where any GT-derived token has been replaced with `[MASK]`.
   Feed it through `eval_synthesized_queries.py --queries-dir synthesis_output_masked/`
   to obtain the **lexical-leak ceiling**: the drop relative to the unmasked run is
   the maximum recall attributable to literal vocabulary overlap.

## `scripts/synthesize_q2_short.py`

Tier-1 short-paraphrase generator. Per query, an LLM rewrites the long behavioral
description as a 5–30 word symptom-style query under the constraint that **no
banned token** (anything derived from a GT file path or GT symbol) may appear.

```bash
python scripts/synthesize_q2_short.py \
    --input-dir synthesis_output/ \
    --output-dir synthesis_output_q2_short/ \
    --model vertex_ai/gemini-2.5-flash
```

Output entries carry `query_type = "behavioral_short"` and a new
`rewritten_from_query_id` pointer. The directory is drop-in for re-running:

```bash
python examples/eval_synthesized_queries.py \
    --pipeline embedding \
    --queries-dir synthesis_output_q2_short/ \
    --embedding-model Salesforce/SweRankEmbed-Small \
    --embedding-dimension 768 \
    --result-path eval_results/synthesized_embedding__short.json
```

## Recommended sequence for the issue

1. Run the existing eval with `--result-path` set (so the JSON is on disk).
2. Run `diagnose_query_leak.py` — read the leaked-vs-clean and length-stratified
   slices first. If the 0.92 lives entirely in `>60 w` and `any_leak=True`, the
   diagnosis is confirmed.
3. Run the masked re-eval: any drop is a hard lower bound on lexical-leak
   contribution.
4. Generate `_behavioral_q2` short variants and re-eval. Predicted drop per #130:
   file-recall@10: 0.92 → ~0.65, symbol-recall@10: 0.73 → ~0.40.

## Field-name compatibility

Both scripts accept either `gt_files`/`gt_symbols` (older synthesized artifacts)
or `target_files`/`target_symbols` (current synthesizer output).
