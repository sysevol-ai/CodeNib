# SWE-QA

Answer SWE-QA's released repository questions with the CodeNib agent, then score
saved answers with the upstream judge. Select a few questions to check the flow,
or use the same entry point for a larger selection.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:swe_qa -->
| Benchmark card | |
| --- | --- |
| Layer | QA Answer quality: released questions answered against reference answers using an LLM judge |
| Origin | External |
| Task | Answer repository-level code questions |
| Ground truth | Released reference answers |
| Granularity | Question and answer |
| Languages | Python repositories |
| Size | 720 questions across 15 repositories at the pinned release |
| Pinned revisions | Dataset, agent and judge `c13deac7` |
| Scorer | Original upstream judge prompt/parser; CodeNib provider transport |
| Setup | Pinned upstream, repository checkouts, CodeNib indexes and API credentials |
| CodeNib entry point | `python -m codenib.eval.benchmarks.swe_qa` |
| Evidence | Runnable; CodeNib answers with upstream judging |
| Upstream | <https://github.com/peng-weihan/SWE-QA-Bench> |
<!-- END CODENIB_SUPPORT_MATRIX:card:swe_qa -->

## Prepare questions and source

Install CodeNib with its `agent`, `graph` and `datasets` extras. Download the
benchmark release and export the questions you want to run:

```bash
git clone https://github.com/peng-weihan/SWE-QA-Bench.git /tmp/SWE-QA-Bench
git -C /tmp/SWE-QA-Bench checkout c13deac7a0d99b0ca2e593e004c4739475785b08
python -m codenib.eval.benchmarks.swe_qa prepare \
  --upstream /tmp/SWE-QA-Bench --repo pytest --limit 3 \
  --output results/swe-qa-selection.jsonl
```

`--repo` selects repository basenames. Repeat `--question-id pytest:1` to select
specific questions; IDs retain their original 1-based JSONL row number. The
export contains the questions and source revisions; references join at scoring.

Clone evaluated repositories into `/tmp/repos/` at the revisions in the release's
`repo_commit.txt`. Build each selected repository's index:

```bash
codenib index /tmp/repos/pytest --preset graph
```

## Generate answers

Configure the selected answering provider's credentials. This example uses
`OPENAI_API_KEY` for GPT-5.1:

```bash
python -m codenib.eval.benchmarks.swe_qa run \
  --upstream /tmp/SWE-QA-Bench --repos-root /tmp/repos \
  --repo pytest --limit 3 --model openai/gpt-5.1 --live \
  --output results/swe-qa-answers.jsonl
```

The [QA harness reference](harnesses.md#repository-question-answering) describes
retrieval policies, answer records, path presentation and resume behavior.

## Score saved answers

Configure `ANTHROPIC_API_KEY` for Sonnet 4.6 and use the same question selection:

```bash
python -m codenib.eval.benchmarks.swe_qa evaluate \
  --upstream /tmp/SWE-QA-Bench --repo pytest --limit 3 \
  --predictions results/swe-qa-answers.jsonl \
  --judge-model anthropic/claude-sonnet-4-6 --live \
  --output results/swe-qa-scores.json
```

Scoring saves each completed question to the output report.

??? note "Scoring and development reference"

    The upstream judge defines five integer dimensions from 1 to 20:
    correctness, completeness, relevance, clarity and reasoning. The adapter
    totals those dimensions and records invalid responses as scoring failures.

    CodeNib forwards the upstream judge prompt through LiteLLMChat and omits its
    service-specific `thinking` body. Sonnet 4.6 changes the original judge model
    and service configuration; retain the chosen configuration with comparisons.

    `mean_successful_total` averages successfully judged answers. Inspect
    `selected`, `scored` and `failed` together before comparing results. A limited
    selection measures that selection.

    ```bash
    SWE_QA_CHECKOUT=/tmp/SWE-QA-Bench python -m pytest \
      test/dataset/test_swe_qa_dataset.py test/eval/test_swe_qa_benchmark.py \
      test/integrations/test_qa_agent.py test/eval/test_baseline_batch.py -q
    ```
