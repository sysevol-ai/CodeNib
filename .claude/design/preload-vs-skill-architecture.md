# Pre-load vs Skill — confirmed architecture for agent-compile

Status: **confirmed** (2026-06-05). Supersedes the "offer-a-skill" framing of the
agent-compile sweep. See also `line-span-eval-harness` memory + the span-eval
work in `codeminer/eval/retrieval_eval.py`.

## Problem (evidence-backed)

The agent-compile arms are defined by which **skills** the agent is *offered*.
But the agent *chooses* whether to use them, and under the neutral prompt it
almost never does:

| skill | adoption (E0 100×3 / subset 15) |
|------|------|
| `embedding_search` | 4% / **0%** |
| `hybrid_search` | 0% / 0% |
| `codeminer_context` (composer) | ~21% |
| `find_callers/callees/trace` | offered-but-ignored |

It localizes with `read`/`bash`/`grep` instead (a strong pretrain prior —
Claude is post-trained on exactly this agentic-coding loop). Consequence: the
`embedding`/`hybrid`/`composer` arms largely measured **"agent + default tools"**,
not their headline skill. The arms collapse together and skill value is
unmeasurable. This is NOT a wiring bug — the tools are offered; the agent
declines.

Yet the retrieval is genuinely useful on the **hard tail**: E0 hard-subset
files@5 went 0.133 (baseline) → 0.283 (bm25_embedding). The tragedy: adoption is
lowest exactly where value is highest.

## Decision

Split by **division of labor**:

- **Retrieval = recall, done UP FRONT as context assembly (pre-load).** It solves
  cold-start / semantic-gap / "where to even look" — the part the agent won't
  reliably invoke and that is most valuable at the start. Don't make it an
  optional tool; **pre-compute candidates and inject them into the opening
  context.**
- **Agent + grep/read = precision, in the loop.** Confirm the candidate, follow
  references, read the exact lines. This is the agent's pretrain strength — keep
  it.

**agent-compile becomes a ROUTER over the pre-load recipe** (per scenario:
language / stacktrace / repo size → which retrievers, top-k, graph expansion on,
rerank on), not a chooser of which tools to offer.

### Classification

| capability | home | note |
|-----------|------|------|
| `embedding_search` | **pre-load** | semantic candidates (primary) |
| `codeminer_context` / graph expansion | **pre-load** | orientation map (was an under-adopted tool) |
| `hybrid`/fusion | **pre-load stage** | fuse embedding+bm25; drop as a standalone tool |
| `bm25` | **pre-load stage** (compile may disable) | most redundant with grep |
| `llm_rerank` | **NOT in the agent** | the LLM reranks candidates itself via reasoning over context; rerank is a RAG-pipeline concern (used only for the `retrieval_rec` RAG baseline, never in the agent pre-load). Double-reranking is pointless. |
| `grep`/`read`/`glob`/`bash` | **skill (always-on)** | the confirm/precision loop |
| `find_callers`/`callees`/`trace` | **skill + auto-trigger** | needs a mid-loop symbol binding; auto-expand when the agent reads a symbol, else it is ignored too |
| `code_to_query` | **drop** | marginal |

> **No rerank in the agent pre-load** (decided 2026-06-05): the agent LLM already
> understands the injected candidates and reasons/confirms over them with grep —
> a separate `llm_rerank` stage just re-sorts what the model will re-judge anyway.
> Pre-load = pure *retrieve* (embedding [+bm25] [+graph expand]). Rerank stays in
> the RAG-pipeline lane for the `retrieval_rec` comparison only.

## Metrics (how to report — see `retrieval_eval.py`)

Single ruler = **span overlap recall@k** (line-range overlap vs `gt_code_blocks`).
- **Headline: `answer_rec@k` / `answer_acc@k`** — the agent's committed final
  answer. This is the deliverable; it is the point of having an agent.
- **`retrieval_rec@k`** — the retriever's ranked spans (nodes only). The
  cross-system ALIGNMENT axis: identical to a plain RAG pipeline's recall@k
  (node_id match == span overlap, verified per-instance |diff|=0). Diagnostic.
- Legacy string `symbols@k*` kept with `*` (≈0 on agent prose; back-reference).

## Test validity — avoid "looks used but fell back to grep"

A measurement where the capability-under-test silently fell back to grep == no
measurement. Two confirmed gates:

1. **Adoption gate.** Every cell logs whether the capability-under-test was
   actually exercised. A skill arm whose adoption < threshold (e.g. 50%) is
   reported as **`N/A — not exercised`**, never a misleading number.
2. **Pre-load contribution attribution.** For pre-loaded context, measure the
   fraction of the committed answer's spans that overlap a pre-injected candidate
   (came from pre-load) vs found independently by grep. Distinguishes "pre-load
   helped" from "pre-load ignored, grep did it anyway."

(Considered but NOT adopted: a no-grep counterfactual arm.)

### Decisive comparison (one `answer_rec@k` ruler)

1. `grep_only` — defaults only (the fallback itself; baseline).
2. `preinjected` — retrieval candidates (file:span) in the opening prompt + grep.
   Report `answer_rec@k` AND pre-load contribution rate.

If (2) ≫ (1): the pre-load architecture is validated and the skills' value lives
in **pre-injection, not tool-offering**. If they tie: grep/read is the real
ceiling on this dataset → prune unused skills.

## Implementation roadmap (incremental)

1. Pre-load assembler: a function that runs the compiled retrieval recipe
   (embedding [+bm25] [+graph expand]; NO rerank) → top-k `(file, span, snippet)`
   → render into the opening user/system context. Reuse `nodes_to_spans`.
2. Runner: accept pre-injected context; keep grep/read loop unchanged.
3. Harness: `preinjected` arm + adoption gate + contribution attribution in the
   persisted cell; aggregate renders `N/A` for un-exercised skill arms.
4. design_space: replace skill-offer arms with pre-load-recipe arms; drop
   `hybrid`/`code_to_query`; add `grep_only` and `preinjected`.
5. Validate on the 15-instance subset (answer_rec + contribution rate), then full
   100×3.
