# Dataset and evaluation architecture

The data layer defines **which task and source snapshot to evaluate**. The
evaluation layer defines **how to run a method and interpret its output**.
Retrieval and agent execution remain shared runtime capabilities.

## Ownership

| Area | Owns | Examples |
| --- | --- | --- |
| `codenib/dataset/` | Dataset loading, task identities, selection, repository metadata, and ground-truth preparation | SWE-bench, CodeNib Base, local JSON, ContextBench, patch-to-location extraction in `gt_locate.py` |
| `codenib/agent/`, `codenib/model/` | Model calls, tools, retrieval, and agent execution | `AgentRunner`, `RepositoryContextExplorer`, retrieval pipelines |
| `codenib/integrations/`, `codenib/clients/` | External protocol and agent-policy adapters | SWE-Explore ranked regions; Agentless and LocAgent policies |
| `codenib/eval/benchmarks/` | Benchmark-specific output conversion, official metric contracts, and task orchestration | ContextBench trajectories and evaluator bridge; SWE-Explore metrics |
| `codenib/eval/agent_runner/` | Reusable experiment execution, case/context preparation, traces, and result analysis | Batch runs, policy sweeps, LSP studies, shared skill-context loading |
| `codenib/eval/reports/` | Interpretation of retained results | Quality/cost aggregation and pricing |
| `scripts/` | Developer experiment entry points | Fixed retrieval experiments and smoke runs |

The intended dependency is `evaluation -> dataset + runtime`. Dataset modules
must not import the evaluation package. Runtime code must not know a task's
gold annotations or the benchmark scorer. Ground truth reaches scoring only
after predictions are produced.

`DatasetBase` is a shared convenience for older dataset wrappers, not a
universal benchmark interface. Its `process()`/summary methods do not describe
all modern datasets. ContextBench therefore exposes an explicit task type and
loader rather than inventing placeholder implementations of those methods.

## ContextBench call path

1. `dataset/contextbench.py` reads a pinned release or local table, preserves
   curated/original IDs, validates labels, and selects tasks. Its case record
   keeps `problem_statement` separate from `gold_context`.
2. `eval/benchmarks/contextbench_runner.py` prepares a private checkout at the
   recorded commit and audits it. Run-specific checkout ownership belongs here;
   loading a dataset does not clone repositories or issue model calls.
3. The runner uses `eval/agent_runner/contexts.py` to load native retrieval
   skills and calls `agent/runner.py`. Only the issue text is the user prompt.
4. `eval/benchmarks/contextbench.py` converts delivered tool messages into the
   upstream trajectory contract and converts annotations into official gold
   JSON. It does not implement a competing metric definition.
5. `_contextbench_evaluate.py` runs the pinned upstream scorer in a subprocess.
   The runner records metrics, failures, usage, and reproducibility metadata.

This keeps the dataset usable without running an agent and permits rescoring
saved predictions without calling a model. See [ContextBench](contextbench.md)
for runnable commands and output schemas.

## Existing inconsistencies

The repository already follows this layering in many paths, but has not
completed a package-wide cleanup:

| Current boundary | Consequence |
| --- | --- |
| Several older dataset wrappers combine loading, checkout mutation, and ground-truth processing | Loading and execution lifecycles vary by dataset; do not assume `process_instance()` is a read-only operation. |
| `eval/benchmarks/swe_explore.py` combines task schemas, input joins, and scoring | Its task-loading responsibility remains inside evaluation. ContextBench does not extend that pattern. |
| Some `clients/` policies import evaluation result types and benchmark parsers | Runtime/policy and evaluator contracts are coupled in those adapters. They need a separate migration before claiming a one-way package graph. |
| Package initializers re-export older dataset and benchmark implementations eagerly | Importing a small adapter can import optional dependencies from unrelated adapters. |
| `eval/agent_runner/` includes both reusable helpers and named experiment workflows | The directory is an experiment toolbox, not a single agent runtime; the actual agent loop lives in `agent/`. |

New benchmark integrations should follow the ContextBench separation. Moving
the older implementations requires preserving their entry points and stored
result contracts, with tests for their actual consumers. The ContextBench
integration does not claim that this wider migration has happened.
