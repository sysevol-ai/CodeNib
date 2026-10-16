# ContextBench retrieval experiments

The experiments are closed for now. The useful improvement was **reading missing
source**, especially complete functions, after a strong model's iterative search.
Decision reranking helped under the earlier 4K delivery constraint but contributed
almost nothing once most observed source fit in 16K/32K. These are development
results, not a leaderboard submission or a SOTA claim.

## Matched 24-task results

The sample contains three tasks per language, 24 repositories and eight languages,
selected deterministically from the earlier 100-task development cohort. Every
task remains in the denominator, and all results below have 24 valid official
evaluations. Recall, precision and F1 are macro means of per-task line scores.

| Route / final context | Recall | Precision | F1 |
| --- | ---: | ---: | ---: |
| Opus native agent, complete observed trajectory | 57.50% | 25.92% | 29.76% |
| Opus native agent, 16K delivery | 56.68% | 27.12% | 30.81% |
| Same observations + decision rerank, 16K | 56.76% | 27.09% | 30.81% |
| Same search + function completion, 16K | **64.97%** | 23.12% | **29.06%** |
| Opus native agent, 32K delivery | 57.26% | 26.06% | 29.94% |
| Same observations + decision rerank, 32K | 57.24% | 26.06% | 29.94% |
| Same search + function completion, 32K | **66.95%** | 21.09% | **27.42%** |
| Published JiuwenSwarm, same task IDs | 64.46% | 22.13% | 27.45% |

The public reference comes from the
[JiuwenSwarm submission](https://github.com/ContextBench/contextbench.github.io/tree/main/submissions/2026-09-12_openjiuwen-jiuwenswarm).
It includes a complete solving agent; our routes retrieve context only. The
leaderboard's full-population 75.3% Recall is a different denominator and must
not be directly compared with this 24-task estimate.

Function completion uses the existing language chunkers to read the entire
function or method touched by an observed source line, capped at 1500 lines per
callable. It does not complete whole classes/files or recursively expand from
new lines. The added source is part of a new serving stage; it is never credited
retroactively to the original Opus trajectory. This stage makes no model calls.

At 16K, completion improves Recall by 8.29 points over unchanged source delivery.
Its point estimates exceed the matched reference in Recall and F1, but the paired
95% Recall interval versus that reference is [-7.71, +7.61] points. At 32K the
Recall difference is +2.48 points, with interval [-5.75, +9.37]. Both intervals
cross zero, and this subset was used for development. Independent validation and
the official verified 500 have not been run for these policies.

## What the decision model contributed

The earlier 100-task, fixed-4K experiment is a different population:

| Opus grep route, 100 selected tasks | Recall | F1 |
| --- | ---: | ---: |
| Raw grep candidate order | 39.88% | 22.60% |
| Decision reranking | 45.70% | 27.84% |
| Graph expansion + decisions | 47.64% | 28.95% |

Two checkout failures remain zero in that denominator; graphs were available on
48 tasks. These results support reranking when a tight delivery budget forces
selection. They do not establish that decisions should control search actions or
stopping. In the later 24-task native agent, most observations already fit in the
larger context, leaving little benefit for another ordering pass.

Earlier chunk-based feedback loops also exposed the output bottleneck: using the
same candidates and scores, removing the ten-chunk cap and increasing delivery
from 4K to 32K raised Recall from 37.90% to 55.11%, while F1 fell from 30.04% to
19.72%. Increasing volume without better source selection is therefore not a
complete solution. No new decision-based product default is introduced.

## Cost and Claude subscription support

The completed 24-task native Opus run used 278 successful model calls and cost
**$23.7864** in recorded OpenRouter API charges, or $0.9911/task. Successful calls
were reused during recovery rather than billed twice. The extra decision-only
selection pass cost **$0.05941** in total; function completion cost no additional
API dollars. These exclude local compute and a downstream solver. This strong
search reference is not an ultra-low-cost serving result.

The source-checkout experiment also supports the official Claude Agent SDK with
an existing Claude Code subscription login. A Claude Max smoke completed with
actual **Opus 5.5**, eight turns and seven source-tool calls, using the same
confined `read`, `grep` and `glob` tools and the official scorer. It did not call
OpenRouter. SDK-reported dollar metering is kept separate from actual billing:
the smoke reported $0.116873 as an estimate; actual billed dollars are unknown.
Subscription limits still apply. See the
[official subscription guidance](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

The subsequent SDK batch was stopped at the user's request after three completed
tasks and one interrupted task; twenty were not run. It is not reported as a
24-task quality result. No further model experiment is running or scheduled.

## Evidence and reproduction

The [per-case CSV](../assets/contextbench_development_24.csv) and
[summary JSON](../assets/contextbench_development_summary.json) contain the exact
24-task metrics, costs, revisions and uncertainty estimates. They exclude source
text, prompts, provider identifiers and credentials. The raw local receipts remain
under ignored `verification/` directories; these summary exports are not a complete
official submission artifact bundle.

The dataset revision is `c2855792b006af41c67202d33883fb9d46362853` and evaluator
revision is `1436c28a8eb95496da4ea69ad458b9f8a8eb7d61`. The recorded native and
refinement experiments preceded integration with current `main`; archived
implementation hashes are retained with their receipts. See
[ContextBench setup and scoring](contextbench.md) and
[dataset/evaluation ownership](architecture.md).

To rerun the same development IDs from a source checkout, first follow the
[installation steps](contextbench.md#installation), then export the pinned tasks
in the CSV's recorded order:

```bash
python - <<'PY'
import csv
import json
from pathlib import Path
from codenib.dataset.contextbench import load_contextbench_cases

with Path("docs/assets/contextbench_development_24.csv").open() as stream:
    ids = [row["instance_id"] for row in csv.DictReader(stream)]
cases = load_contextbench_cases(config="contextbench_verified", instance_ids=ids)
by_id = {case.instance_id: case for case in cases}
output = Path("results/contextbench-dev")
output.mkdir(parents=True, exist_ok=True)
(output / "selected.jsonl").write_text(
    "".join(json.dumps(by_id[key].to_record()) + "\n" for key in ids)
)
PY
python scripts/benchmark_context_agent.py prepare \
  --cases results/contextbench-dev/selected.jsonl \
  --output results/contextbench-dev/prepared
```

The following `run` explicitly enables new OpenRouter charges. Its cost guards
use observed spending; in-flight requests can exceed the thresholds. Function
completion and evaluation make no model calls:

```bash
python scripts/benchmark_context_agent.py run \
  --prepared results/contextbench-dev/prepared \
  --output results/contextbench-dev/native \
  --max-turns 20 --workers 4 --max-cost-usd 30 --case-cost-usd 3 --live
python scripts/benchmark_context_agent.py refine \
  --input results/contextbench-dev/native \
  --output results/contextbench-dev/functions --method functions
python scripts/benchmark_context_agent.py evaluate \
  --output results/contextbench-dev/functions \
  --upstream /tmp/ContextBench \
  --evaluator-python /tmp/contextbench-evaluator/bin/python
```

The SDK uses an optional `claude-agent-sdk==0.2.165` environment and an existing
logged-in Claude Code CLI; the smoke used CLI 2.1.296. Model calls require an
explicit `--live` flag. `benchmark_context_claude.py --help` describes the alternate
subscription runner; it uses a different agent loop and is not a transport-only
ablation. Reproduction runs use the current implementation and live model endpoint,
so they need not reproduce the historical scores exactly. A frozen independent
32-task selection excludes all 105 earlier
development IDs, but remains unrun. Future leaderboard claims require renewed
evaluation, matched failure accounting and actual solver outcomes for Pass@1.
