<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Jev decision-based reranking

CodeNib can use TypeSafe's Jev through OpenRouter to score retrieved code.
`OpenRouterDecisions` sends application state and typed questions to
`POST https://openrouter.ai/api/alpha/decisions`. `RerankAgent` asks one relevance
question per candidate and sorts the returned scores in Python.

Jev returns decisions, not generated text. Use it for this reranking stage or
for explicit classification questions. Keep a chat model for Ask answers,
Wiki prose, keyword extraction, and agent tool calling; setting their chat
`--model` to Jev does not enable decisions.

## Configure OpenRouter

From a source checkout with this change:

```bash
pip install -e '.[agent]'
export OPENROUTER_API_KEY='your-openrouter-key'
```

The default model is `~typesafe/jev-latest`. To keep an evaluation reproducible,
pass an available versioned OpenRouter model ID instead. An optional
`openrouter/` prefix is accepted and removed before the HTTP request.

## Rerank existing candidates

```python
from codenib.agent.rerank_agent import RerankAgent
from codenib.llm import OpenRouterDecisions
from codenib.types import NODE_TYPE_FUNCTION, NodeInfo

reranker = RerankAgent(decisions=OpenRouterDecisions())
nodes = [
    NodeInfo(
        node_id="payments.py:retry_payment",
        node_name="retry_payment",
        type=NODE_TYPE_FUNCTION,
        file="payments.py",
        content="def retry_payment(payment): return gateway.retry(payment)",
    ),
    NodeInfo(
        node_id="payments.py:format_amount",
        node_name="format_amount",
        type=NODE_TYPE_FUNCTION,
        file="payments.py",
        content="def format_amount(amount): return f'{amount:.2f}'",
    ),
]
ranked = reranker.rerank_nodes(
    "Where are failed payments retried?", nodes, top_k=1, include_content=True
)
```

The existing `llm_rerank` skill also accepts
`RerankContext(decisions=OpenRouterDecisions())`. It retains the same candidate
limit, output locations, and `return_content` behavior.

For an existing retrieval pipeline, select the backend explicitly:

```python
from codenib.model.retrieve_rerank_pipeline import (
    RetrieveRerankPipeline,
    RetrieveStageConfig,
)

pipeline = RetrieveRerankPipeline(
    repo_path="/path/to/repository",
    index_path="/path/to/index",
    retrieval_mode="sparse",
    retrieval_plan=[RetrieveStageConfig(engine="sparse", top_k=100)],
    rerank_strategy="decisions",
    rerank_model="~typesafe/jev-latest",
    rerank_candidate_top_k=100,
)
results = pipeline.query("Where are failed payments retried?", top_k=5)
```

This configuration retrieves up to 100 BM25 candidates, scores them in batches
of ten with Jev, and returns five results. Candidate K, request batch size,
and result count are separate controls. Increasing only the rerank candidate
cap does not enlarge the retrieval stage's candidate pool.

This pipeline retains its existing retrieval/indexing dependencies (available
with `pip install -e '.[full]'`). The evaluation script
`examples/retrieve_rerank.py` accepts the same selection through
`--rerank-strategy decisions --rerank-model '~typesafe/jev-latest'`.
Omitting `rerank_model` selects Jev for decisions and preserves the existing
Qwen default for chat reranking.

## Scoring and failure behavior

- CodeNib bounds each request to at most ten candidates; this is an integration
  choice, not a claimed API limit. Each candidate's code is
  truncated to 3,000 characters in the request; returned source content is
  preserved. Smaller windows and overlapping windows remain available.
- Each `score` question uses four relevance levels, from unrelated code to a
  direct implementation or likely issue location. Jev's score is the expected
  position on this 0–3 scale. CodeNib divides it by three to produce its 0–1
  relevance score. Confidence is not used as a relevance score.
- Overlapping windows retain the existing score averaging behavior. Equal
  scores retain the first-stage order. Candidates without content remain at
  the end with their original scores.
- HTTP calls have a 30-second per-attempt timeout and at most two retries for
  connection failures, timeouts, HTTP 408/429, and HTTP 5xx. Authentication,
  credit, and invalid-request errors fail immediately. `timeout` and
  `max_retries` can be set on `OpenRouterDecisions`.
- The client checks answer IDs, primitive types, numeric ranges, choices,
  scale legends, and probability distributions, allowing for Jev's rounding
  to two decimal places while preserving the provider's scores and values.
  Invalid responses raise an error instead of influencing a ranking.
  A failed rerank window contributes
  no scores; unscored candidates retain their first-stage order and scores
  after successfully scored candidates. If every window fails, the original
  ranking is preserved.

Chat options such as temperature, `max_tokens`, tools, and RankGPT output
format are not sent to the Decisions API. Actual quality and latency should
be measured on representative code queries before changing a deployment's
default reranker.

## Ask other typed questions

The transport is also usable independently of retrieval. Questions may be
typed objects or API-shaped dictionaries:

```python
from codenib.llm import ChoiceQuestion, NoulQuestion, OpenRouterDecisions

result = OpenRouterDecisions().decide(
    state={"query": "Where is retry_payment defined?"},
    questions={
        "needs_symbol_lookup": NoulQuestion(
            instructions="Does the query ask to locate a named code symbol?"
        ),
        "intent": ChoiceQuestion(
            instructions="What is the query asking for?",
            criteria={
                "definition": "Locate the definition of a named symbol",
                "behavior": "Explain how an implementation behaves",
            },
        ),
    },
)
probability = result.answers["needs_symbol_lookup"].noul
intent = result.answers["intent"].choice
distribution = result.answers["intent"].probabilities
cost = result.usage.get("cost")
```

`NoulQuestion` returns a probability of yes. `ChoiceQuestion` returns a selected
option and the full probability distribution. `ScoreQuestion` returns a scale
position, distribution, legend, and confidence. Callers own thresholds and
workflow decisions. This example does not change CodeNib's retrieval planner.

API references: [OpenRouter Jev guide](https://openrouter.ai/docs/guides/community/jev),
[Decisions request and response](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request),
and [TypeSafe score semantics](https://docs.typesafe.ai/primitives/score).
