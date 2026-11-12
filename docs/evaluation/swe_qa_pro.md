# SWE-QA-Pro

Answer SWE-QA-Pro's released repository questions with the CodeNib agent and
score saved answers with the official evaluator. Select tasks from the fixed
data release, prepare their source revisions, and use the same selection for
answering and judging.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:swe_qa_pro -->
| Benchmark card | |
| --- | --- |
| Layer | QA Answer quality: released questions answered against reference answers using an LLM judge |
| Origin | External |
| Task | Answer repository-level code questions with source exploration |
| Ground truth | Released reference answers |
| Granularity | Question and answer |
| Languages | Python repositories |
| Size | 260 questions across 26 repositories |
| Pinned revisions | Evaluator `93ac6a4f`, dataset `596892da` |
| Scorer | Official evaluator; five 1–10 dimensions |
| Setup | Pinned release, source checkouts, CodeNib indexes and provider keys |
| CodeNib entry point | `python -m codenib.eval.benchmarks.swe_qa_pro` |
| Evidence | Runnable; CodeNib answers with official scoring |
| Upstream | <https://github.com/TIGER-AI-Lab/SWE-QA-Pro> |
<!-- END CODENIB_SUPPORT_MATRIX:card:swe_qa_pro -->

## Prepare questions and source

Install CodeNib with its `agent`, `graph` and `datasets` extras. Download the
release and export a question selection:

```bash
hf download TIGER-Lab/SWE-QA-Pro-Bench --repo-type dataset \
  --revision 596892dac60b6f500f01a7dc2becb9f66593b7b7 --local-dir /tmp/pro-data

python -m codenib.eval.benchmarks.swe_qa_pro prepare \
  --data /tmp/pro-data/data/test.jsonl --repo qibo --limit 3 \
  --output results/pro-selection.jsonl

git clone --filter=blob:none --sparse https://github.com/TIGER-AI-Lab/SWE-QA-Pro.git /tmp/SWE-QA-Pro
git -C /tmp/SWE-QA-Pro sparse-checkout set eval
git -C /tmp/SWE-QA-Pro checkout 93ac6a4f3af3fe3f86580f62142f47e97e2cc897
```

`--repo` accepts a basename or owner/name. Repeat `--question-id` to select
specific tasks. IDs contain the basename and original 1-based JSONL row number;
`cluster` and `qa_type` retain the released metadata. The task export carries
questions and source revisions; references join at scoring.

Clone evaluated repositories into `/tmp/repos/` at the commits in upstream's
`eval/repos.txt`. Build each selected repository's index:

```bash
codenib index /tmp/repos/qibo --preset graph
```

## Generate answers

Configure the answering provider's credentials, such as `OPENAI_API_KEY` for
GPT-5.1:

```bash
python -m codenib.eval.benchmarks.swe_qa_pro run \
  --data /tmp/pro-data/data/test.jsonl --repo qibo --limit 3 \
  --repos-root /tmp/repos --model openai/gpt-5.1 --live \
  --output results/pro-answers.jsonl
```

The [QA harness reference](harnesses.md#repository-question-answering) describes
retrieval policies, answer records, path presentation and resume behavior.

## Score saved answers

Configure the judge provider's credentials and use the same task selection:

```bash
python -m codenib.eval.benchmarks.swe_qa_pro evaluate \
  --data /tmp/pro-data/data/test.jsonl --repo qibo --limit 3 \
  --predictions results/pro-answers.jsonl --upstream /tmp/SWE-QA-Pro \
  --output results/pro-scores --live
```

The evaluator writes `scores.jsonl` as questions complete. `summary.json`
aggregates all completed scores, including successful rows from earlier attempts.

Choose DeepSeek with `--judge deepseek`, and override either provider's model
with `--judge-model`. Judge concurrency defaults to four workers.

??? note "Scoring and development reference"

    The pinned `sweqapro.scoring.judge.evaluate_jsonl()` defines the prompt,
    provider calls and five integer scores from 1 to 10, for a total of 5–50.
    Its OpenAI default is `gpt-5-2025-08-07`, with reasoning effort and text
    verbosity set to `low`. The DeepSeek branch uses the selected service's
    thinking and output-token defaults; `--reasoning-effort` applies to OpenAI.

    `mean_successful_total` averages successfully judged answers. Inspect
    `selected`, `scored` and `failed` together before comparing results.
    `judge-input.jsonl` retains the scoring input, including reference answers.

    ```bash
    SWE_QA_PRO_DATA=/tmp/pro-data/data/test.jsonl \
    SWE_QA_PRO_CHECKOUT=/tmp/SWE-QA-Pro python -m pytest \
      test/dataset/test_swe_qa_pro_dataset.py test/eval/test_swe_qa_pro_benchmark.py \
      test/integrations/test_qa_agent.py test/eval/test_baseline_batch.py -q
    ```
