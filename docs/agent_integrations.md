# Reproduced Agents

CodeNib re-runs five published code-localization agents in its own harness:
LocAgent, Agentless, CoSIL, OrcaLoca, and the `jump` tool from RepoNavigator.
Each agent keeps its published prompts and control loop. CodeNib only answers
its repository calls. That serves two purposes:

- **Baselines.** The agents run on the same benchmarks, labels, and scorer as
  CodeNib's own methods, so their results are directly comparable.
- **Backend.** An agent can use CodeNib's index instead of the one it would
  build itself. For LocAgent and OrcaLoca, a
  [paired runner](#paired-provider-compatibility) swaps the two backends under
  a fixed model, case set, and budget. That shows whether CodeNib serves the
  agent as well as the agent's own index does.

No agent builds its own graph, BM25 index, or cache. Each one binds to a single
CodeNib index (`repo_manifest.json`) and loads only the views it needs.

## At a glance

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agents -->
| Agent | Upstream revision | Fidelity | CodeNib serves | Native ↔ CodeNib | Benchmarks | Results |
| --- | --- | --- | --- | --- | --- | --- |
| [Agentless v1.5.0](#agentless) | [`b150f28`](https://github.com/OpenAutoCoder/Agentless/tree/b150f28465a77a81a7f4776384957a4271f5bd69) | Pinned · upstream prompts | `symbol_graph` | — | Base, SWE-bench Lite, Loc-Bench | None published |
| [LocAgent](#locagent) | [`ef17054`](https://github.com/gersteinlab/LocAgent/tree/ef170542a5cca88a1bd8463335ec43de222ed5f9) | Pinned · upstream prompts and loop | `bm25`, `symbol_graph` | [Paired runner](#paired-provider-compatibility) | Base, SWE-bench Lite, Loc-Bench | [SWE-bench Lite, 50 cases, one run: file Acc@1 0.78](#locagent-on-swe-bench-lite) |
| [CoSIL](#cosil) | [`0568e42`](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl) | Pinned · upstream prompts and loop | `symbol_graph` | — | Base, SWE-bench Lite, Loc-Bench | None published |
| [OrcaLoca SearchAgent](#orcaloca) | [`37db289`](https://github.com/fishmingyu/OrcaLoca/tree/37db289be2dc3b7432183fe08b3f06becce87c27) | Pinned · upstream agent | `symbol_graph` | [Paired runner](#paired-provider-compatibility) | Base, SWE-bench Lite, Loc-Bench | [SWE-bench Lite, 4-case paired smoke: both backends matched 4/4](#orcaloca-paired-smoke) |
| [RepoNavigator `jump`](#reponavigator) | [`arXiv v6`](https://arxiv.org/abs/2512.20957v6) | Pinned · upstream tool contract | `symbol_graph`, SCIP occurrences | — | — | None published |
<!-- END CODENIB_SUPPORT_MATRIX:agents -->

**Fidelity** says how closely a run follows the upstream code. *Pinned* means
the vendored prompts, tool schemas, or hooks were checked against the named
upstream revision. It says nothing about quality; see the
[evidence levels](evaluation/index.md#evidence-levels). The
[method × benchmark matrix](evaluation/index.md#reproduced-agents-and-controls)
gives each agent's level on each benchmark. The upstream agents target Python,
and they are validated only on Python repositories.

## Results

### LocAgent on SWE-bench Lite

This is one complete run of LocAgent with CodeNib as its backend, on a fixed
50-case selection from SWE-bench Lite. Every case finished. A failed case would
have counted as a miss.

| *k* | File Accuracy@k | File Recall@k | Function Accuracy@k | Function Recall@k |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 0.78 | 0.78 | 0.54 | 0.56 |
| 3 | 0.88 | 0.88 | 0.75 | 0.76 |
| 5 | 0.88 | 0.88 | 0.79 | 0.82 |

How to read these numbers:

- **Metrics.** Labels come from the golden patch, and the metrics follow the
  [shared definitions](evaluation/harnesses.md#file-and-symbol-metrics).
  File metrics cover all 50 cases. Function metrics cover the 48 cases whose
  patch modifies or deletes a function that exists at the base commit.
- **Case selection.** The cases are a repository-balanced selection (seed
  `codenib-locagent-swebench-lite-50-v1`). They cover every repository in
  SWE-bench Lite, but they do not estimate the score over the whole Lite
  population.
- **Model and budget.** There was one run, with one model (`gpt-5.6-luna`) and
  at most 10 iterations per case. A case used about 282,000 tokens and
  26 seconds on average. The run was recorded on 2026-08-03 at CodeNib commit
  `ba886799`, which was merged in
  [#406](https://github.com/sysevol-ai/CodeNib/pull/406).
- **No native comparison.** LocAgent was not run with its own index on these
  cases. The numbers show how well LocAgent localizes with CodeNib as its
  backend, not whether CodeNib does better than LocAgent's native index.

[Fixed SWE-bench Lite coverage](#fixed-swe-bench-lite-coverage) gives the
commands that rebuild the case set and run it again.

### OrcaLoca paired smoke

On four SWE-bench Lite cases, OrcaLoca ran once with its own backend and once
with CodeNib's, with the same model (`gpt-5.6-luna`), cases, and upstream
sampling. Both backends matched the golden-patch files and functions on all
four cases, by OrcaLoca's File Match and Function Match. This shows that the
backend swap works end to end. Four cases with one sample each support no
comparison of quality or cost. The smoke was recorded on 2026-08-02.

## How a reproduction is checked

LocAgent, Agentless, CoSIL, and OrcaLoca are listed as Pinned only after four
checks. RepoNavigator has released no code, so its listing rests on the first
check, run against the `jump` contract published in the paper.

1. Provider tests exercise the agent's tools without the upstream package
   installed: source identities, ranges, budgets, and failure behavior.
2. An optional probe reads the pinned upstream revision. It compares the
   vendored prompts, the tool signatures, and the private hooks that the agent
   actually calls.
3. Before any model call, a preflight confirms each checkout's commit, the
   index's identity and declared views, and that every view the agent needs
   loads.
4. The benchmark runner records every requested case and keeps failures in the
   denominator. Results record the case set, the CodeNib and upstream
   revisions, the model, and the run options.

These checks support only the stated boundary. They do not cover other upstream
revisions, or the stages listed as not reproduced. CodeNib's graph covers many
more languages than Python. A fixture also exercises the LocAgent tools on a
TypeScript and Go index. That checks the tool boundary, not the Python-only
upstream agents.

Every agent is scored with the shared file and symbol metrics. Some upstream
projects report their own metrics, such as LocAgent's nDCG and MAP or
OrcaLoca's File Match and Function Match. Those stay separate and never
replace the shared ones.

The base `codenib` package gains no dependency from these agents. Upstream
packages such as OrcaLoca and LlamaIndex are imported only for a policy that
still runs inside the upstream runtime, and only when a task runs.

## LocAgent

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agent:locagent -->
| Agent card | |
| --- | --- |
| What it is | An LLM agent that searches a code graph and a BM25 index with three tools until it names the locations to edit. |
| Type | C · agentic tool loop |
| Pinned upstream | [LocAgent `ef17054`](https://github.com/gersteinlab/LocAgent/tree/ef170542a5cca88a1bd8463335ec43de222ed5f9) |
| Fidelity | Pinned: vendored prompts and function schemas, checked against the upstream Git objects. |
| Reproduced | The localization loop, with upstream prompts and function schemas |
| Not reproduced | LocAgent's own index builder, and its nDCG and MAP reports |
| CodeNib serves | `search_code_snippets`, `get_entity_contents`, and `explore_tree_structure`, over `bm25` and `symbol_graph` |
| Native ↔ CodeNib | [Paired runner](#paired-provider-compatibility) |
| Benchmarks | Base: Runnable; SWE-bench Lite: [Pinned](#locagent-on-swe-bench-lite); Loc-Bench: Runnable |
| Results | [SWE-bench Lite, 50 cases, one run: file Acc@1 0.78](#locagent-on-swe-bench-lite) |
| Entry points | `LocAgentAgent`, `examples/locagent_loc_agent.py` |
<!-- END CODENIB_SUPPORT_MATRIX:agent:locagent -->

### Use CodeNib as LocAgent's backend

Build a graph-enabled index, then bind the provider:

```bash
codenib index /path/to/repository --preset graph
```

```python
from codenib.integrations.locagent import (
    LocAgentToolProvider,
    get_locagent_tool_schemas,
)

provider = LocAgentToolProvider.from_manifest(
    "/path/to/repo_manifest.json",
)

tools = get_locagent_tool_schemas()
result = provider.dispatch(
    "get_entity_contents",
    {"entity_names": ["src/service.py:BillingService.calculate_tax"]},
)
```

The standalone example needs no LocAgent installation:

```bash
python examples/integrations/locagent.py \
  --manifest /path/to/repo_manifest.json \
  --search "configuration loader"
```

`provider.bindings()` returns callables with the same names for a runtime
plugin, and `provider.dispatch(name, arguments)` calls a tool directly. The
tool surface is also checked against the
[OpenHands LocAgent implementation](https://github.com/OpenHands/OpenHands/tree/efe287ce3402706a171b3a5fb40f15914e98ef20/openhands/agenthub/loc_agent)
at its recorded revision.

LocAgent and CodeNib name graph relations differently. The provider maps them,
and its traversal output says when it used a broader relation. It never
presents a reference or type-use edge as an exact call or inheritance fact.

| LocAgent relation | CodeNib relation | Fidelity |
| --- | --- | --- |
| `contains` | `contain` | Exact |
| `imports` | `import` | Exact when emitted by the graph backend |
| `invokes` | `reference` | Conservative superset |
| `inherits` | `type-use` | Conservative superset |

The provider also guarantees that:

- source paths are repository-relative and cannot escape the index's root;
- external line numbers are 1-based, while CodeNib's graph ranges stay
  0-based;
- a missing, stale, or failed graph or BM25 view produces an explicit error;
- tool output is deterministic and bounded by result, traversal, source-line,
  and character budgets;
- loading the provider never invokes LocAgent, NetworkX, LlamaIndex, or an
  index builder.

### Run LocAgent on a benchmark

`LocAgentAgent` runs LocAgent's vendored prompts, function schemas, and
reasoning loop in CodeNib, and answers its tool calls with the provider above.
It resolves the final file, class, function, and line records through
CodeNib's graph. It needs neither a LocAgent checkout nor a second Python
environment, and it does not import LocAgent, LiteLLM, or LlamaIndex. OpenAI is
loaded only when the policy runs, and `--base-url` accepts any
OpenAI-compatible endpoint, such as a LiteLLM proxy.

```bash
python -m pip install "codenib[agent,graph]==0.2.4"
codenib toolchain install /path/to/repository --scope graph
```

```python
from codenib.clients.locagent_agent import LocAgentAgent

agent = LocAgentAgent(
    model="openai-compatible-model",
)
result = await agent.locate_code(
    query_text=issue,
    repo_path=checkout,
    context={"package_name": "project"},
)
```

```bash
python examples/locagent_loc_agent.py \
  --dataset codenib_base \
  --model "$LOCAGENT_MODEL" \
  --result-path results/locagent.jsonl
```

Run the provider and runner tests with:

```bash
pytest -q test/integrations/test_locagent.py \
  test/eval/test_locagent_benchmark_adapter.py
```

## Agentless

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agent:agentless -->
| Agent card | |
| --- | --- |
| What it is | Localizes an issue in three fixed LLM stages: rank files, pick classes and functions from file skeletons, then choose edit lines. |
| Type | B · fixed LLM workflow |
| Pinned upstream | [Agentless v1.5.0 `b150f28`](https://github.com/OpenAutoCoder/Agentless/tree/b150f28465a77a81a7f4776384957a4271f5bd69) |
| Fidelity | Pinned: the three vendored prompts match upstream byte for byte. |
| Reproduced | The three localization stages |
| Not reproduced | Repair and patch validation |
| CodeNib serves | The filtered Python project tree, symbol skeletons, and numbered source windows, all from `symbol_graph` |
| Native ↔ CodeNib | — |
| Benchmarks | Base: Runnable; SWE-bench Lite: Runnable; Loc-Bench: Runnable |
| Results | None published |
| Entry points | `AgentlessAgent`, `examples/agentless_loc_agent.py` |
<!-- END CODENIB_SUPPORT_MATRIX:agent:agentless -->

`AgentlessAgent` follows the Agentless v1.5.0 localization sequence:

1. rank files from the filtered Python project tree;
2. pick classes, functions, methods, and variables from compressed files;
3. refine them to edit locations in numbered source windows.

CodeNib supplies all three inputs from one symbol graph and its checkout. It
does not build Agentless's per-case AST artifact or import Agentless or LibCST.
The provider keeps Agentless's Python-only and `test*` directory filters, so on
CodeNib Base only the Python issues are scored. Its skeletons come from
Python's standard `ast` module and keep the same classes, callables, and module
assignments as Agentless's LibCST output, though formatting can differ.

```python
from codenib.clients.agentless_agent import AgentlessAgent

agent = AgentlessAgent(model="openai-compatible-model")
result = await agent.locate_code(
    query_text=issue,
    repo_path=checkout,
)
```

To inspect the exact context delivered to each stage:

```bash
python examples/integrations/agentless.py \
  --manifest /path/to/repo_manifest.json \
  --file src/service.py
```

Run it through the shared benchmark harness:

```bash
python examples/agentless_loc_agent.py \
  --dataset codenib_base \
  --model "$AGENTLESS_MODEL" \
  --result-path results/agentless.jsonl
```

The older `codenib.model.AgentlessPipeline` remains for existing callers. It
uses CodeNib's own structured prompts and is not the pinned Agentless policy.

```bash
pytest -q test/integrations/test_agentless.py \
  test/integrations/test_agentless_policy.py \
  test/model/test_agentless_pipeline.py

AGENTLESS_CHECKOUT=/path/to/Agentless \
pytest -q test/integrations/test_agentless_upstream.py
```

## CoSIL

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agent:cosil -->
| Agent card | |
| --- | --- |
| What it is | Ranks files, checks that ranking against their imports, then inspects candidate classes and functions with four tools. |
| Type | C · agentic tool loop |
| Pinned upstream | [CoSIL `0568e42`](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl) |
| Fidelity | Pinned: vendored prompts, tool schemas, and loop, checked against the upstream tree. |
| Reproduced | File and function localization, the paper's RQ1 path |
| Not reproduced | Line localization, patch generation, and test validation |
| CodeNib serves | `get_code_of_class`, `get_code_of_class_function`, `get_code_of_file_function`, and `exit`, over `symbol_graph` |
| Native ↔ CodeNib | — |
| Benchmarks | Base: Runnable; SWE-bench Lite: Runnable; Loc-Bench: Runnable |
| Results | None published |
| Entry points | `CoSILAgent`, `examples/cosil_loc_agent.py` |
<!-- END CODENIB_SUPPORT_MATRIX:agent:cosil -->

CoSIL works in two steps, following the
[pinned RQ1 scripts](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl):

1. rank files, then check that ranking against their import statements;
2. inspect candidate classes and functions with four tools, optionally prune
   irrelevant observations, and write a final XML location summary.

`CoSILRepositoryProvider` loads only CodeNib's symbol graph. File identity comes
from the graph. A lazy parse of each requested file with Python's `ast` module
restores CoSIL's own class and function classification and source ranges.
The provider does not load CoSIL's `repo_structures/<instance>.json` or build
another graph. A compatibility check compared all 1,791 eligible Python files
in five SWE-bench Lite snapshots (Requests, Flask, Django, SymPy, and Pylint)
with CoSIL's pinned `parse_python_file`. Classes, functions, methods, ranges,
and source matched for every file. That validates the provider, not CoSIL's
localization score.

If the import reflection is malformed, CodeNib keeps the validated initial file
ranking rather than turning an empty reflection into an empty candidate set.

```python
from codenib.integrations.cosil import CoSILRepositoryProvider

provider = CoSILRepositoryProvider.from_manifest(
    "/path/to/repo_manifest.json",
)
source = provider.dispatch(
    "get_code_of_class_function",
    {
        "file_name": "src/service.py",
        "class_name": "Service",
        "func_name": "run",
    },
)
```

To inspect the candidate contract without CoSIL installed:

```bash
python examples/integrations/cosil.py \
  --manifest /path/to/repo_manifest.json \
  --file src/service.py
```

`CoSILAgent` runs the vendored prompts and loop without importing CoSIL,
Agentless, LiteLLM, or a second index:

```bash
python examples/cosil_loc_agent.py \
  --dataset codenib_base \
  --model "$COSIL_MODEL" \
  --result-path results/cosil.jsonl
```

```bash
pytest -q test/integrations/test_cosil.py \
  test/integrations/test_cosil_policy.py

COSIL_CHECKOUT=/path/to/CoSIL \
pytest -q test/integrations/test_cosil_upstream.py
```

## OrcaLoca

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agent:orcaloca -->
| Agent card | |
| --- | --- |
| What it is | OrcaLoca's own search agent, unchanged: it decomposes search actions, schedules them by priority, and decodes the final locations. |
| Type | C · agentic tool loop |
| Pinned upstream | [OrcaLoca `37db289`](https://github.com/fishmingyu/OrcaLoca/tree/37db289be2dc3b7432183fe08b3f06becce87c27) |
| Fidelity | Pinned: tool signatures and the private hooks the agent calls, checked by an upstream probe. |
| Reproduced | `SearchAgent`, with upstream sampling |
| Not reproduced | Trace analysis: the agent receives an empty trace, so this is not the published end-to-end pipeline |
| CodeNib serves | Six search tools and the private hooks `SearchWorker` calls, over `symbol_graph` |
| Native ↔ CodeNib | [Paired runner](#paired-provider-compatibility) |
| Benchmarks | Base: Runnable; SWE-bench Lite: [Pinned](#paired-provider-compatibility); Loc-Bench: Runnable |
| Results | [SWE-bench Lite, 4-case paired smoke: both backends matched 4/4](#orcaloca-paired-smoke) |
| Entry points | `OrcaLocaAgent`, `examples/orcaloca_loc_agent.py` |
<!-- END CODENIB_SUPPORT_MATRIX:agent:orcaloca -->

### Use CodeNib as OrcaLoca's backend

OrcaLoca's model sees six search functions: `search_file_contents`,
`search_source_code`, `search_class`, `search_method_in_class`,
`search_callable`, and `search_file_tree`. Its `SearchWorker` also calls
private hooks for history, distance, exact location, decomposition, and
disambiguation. CodeNib implements both. The private hooks are a compatibility
surface for the pinned revision, not new CodeNib APIs.

```python
from codenib.integrations.orcaloca import (
    make_orcaloca_search_manager_factory,
)

search_manager_factory = make_orcaloca_search_manager_factory(
    "/path/to/repo_manifest.json",
)
```

To inspect the provider without installing OrcaLoca, run the
[standalone example](https://github.com/sysevol-ai/CodeNib/blob/main/examples/integrations/orcaloca.py):

```bash
python examples/integrations/orcaloca.py \
  --manifest /path/to/repo_manifest.json
```

With the small injection point from
[OrcaLoca PR #140](https://github.com/fishmingyu/OrcaLoca/pull/140), pass the
factory to the unchanged search agent:

```python
from Orcar.search_agent import SearchAgent

agent = SearchAgent(
    llm=llm,
    search_input=search_input,
    repo_path="/path/to/repository",
    search_manager_factory=search_manager_factory,
)
```

The factory checks that `repo_path` is the repository the index was built
from. It loads no OrcaLoca graph and creates no `_index_data` directory. Each
agent gets its own lightweight query history, while source, symbol identity,
containment, dependency distance, and ranges all come from the same CodeNib
index.

The adapter keeps OrcaLoca's repository-relative `file::Class::method`
identities, 1-based locations, and the markers its prompts expect. CodeNib's
graph supplies identity and relations. A lazy parse of the source, which runs
no code, restores the pinned agent's symbol kinds, signatures, docstrings,
skeletons, and ranges. Python symbols that the pinned agent does not recognize
are not exposed just because CodeNib's graph has them. Prose, and the order of
tied results, may still differ from upstream.

### Trace analysis is not reproduced

OrcaLoca can analyze an issue's stack traces before it searches. The
reproduction starts after that stage:

```text
issue -> [trace analysis: not reproduced] -> SearchInput -> SearchAgent -> repository backend
```

Both the benchmark adapter and the paired runner give `SearchAgent` an empty
`TraceAnalysisOutput`. That is a valid upstream input, and it is also what
OrcaLoca uses when its trace analysis fails. Holding it fixed isolates the
backend: native and CodeNib runs get the same absence of trace hints, and only
the search manager changes. As a result, this reproduction does not claim
OrcaLoca's full pipeline or its published end-to-end score. A future
trace-enabled comparison should give both backends one fixed, recorded trace
rather than generate traces separately.

### Run OrcaLoca on a benchmark

`OrcaLocaAgent` finds the checkout's CodeNib index, injects the search manager,
runs OrcaLoca with an empty trace, and converts its final `file/class/method`
records into symbols such as `Service.run()`. It imports OrcaLoca and
LlamaIndex only when a task runs.

```python
from codenib.clients.orcaloca_agent import OrcaLocaAgent

agent = OrcaLocaAgent(
    model="openai-compatible-model",
    base_url="https://gateway.example/v1",  # optional
)
result = await agent.locate_code(
    query_text=issue,
    repo_path=checkout,
    context={},
)
```

```bash
python examples/orcaloca_loc_agent.py \
  --dataset codenib_base \
  --model "$ORCALOCA_MODEL" \
  --orcaloca-checkout /path/to/OrcaLoca \
  --result-path results/orcaloca.jsonl
```

The shared runner reports the common file and symbol metrics. OrcaLoca's own
[File Match and Function Match](evaluation/harnesses.md#orcaloca-file-match-and-function-match)
are golden-patch subset metrics, implemented without dependencies in
`codenib.eval.benchmarks.orcaloca`:

```python
from codenib.eval.benchmarks.orcaloca import (
    OrcaLocaGroundTruth,
    parse_orcaloca_locations,
    score_orcaloca_locations,
)

ground_truth = OrcaLocaGroundTruth(
    files=("src/service.py",),
    functions=("src/service.py:Service.run",),
)
locations = parse_orcaloca_locations(search_agent_output)
score = score_orcaloca_locations(ground_truth, locations)
```

Their labels must come from the golden patch. Trajectory-read labels, such as
SWE-Explore's `read_core_*` fields, measure a different target.

```bash
pytest -q test/integrations/test_orcaloca.py \
  test/eval/test_orcaloca_metrics.py \
  test/eval/test_orcaloca_benchmark_adapter.py
```

To exercise OrcaLoca's actual decomposition, priority queue, and final-location
decoder against a pinned upstream checkout:

```bash
ORCALOCA_CHECKOUT=/path/to/OrcaLoca \
  pytest -q test/integrations/test_orcaloca_upstream.py
```

The upstream probe is marked `integration`. Without `ORCALOCA_CHECKOUT`, it
skips before importing OrcaLoca.

## RepoNavigator

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agent:reponavigator -->
| Agent card | |
| --- | --- |
| What it is | One `jump` tool that resolves a symbol occurrence to the source of its definition. The caller brings the agent. |
| Type | C · agentic tool loop |
| Pinned upstream | [RepoNavigator paper `arXiv v6`](https://arxiv.org/abs/2512.20957v6) |
| Fidelity | Pinned: the `jump` signature published in the paper; no upstream code exists. |
| Reproduced | The paper's `jump` tool |
| Not reproduced | The agent loop, prompts, training, and reported scores |
| CodeNib serves | `jump`, over `symbol_graph` and persisted LSP occurrences |
| Native ↔ CodeNib | — |
| Benchmarks | None; the caller supplies the agent |
| Results | None published |
| Entry points | `RepoNavigatorRepositoryProvider` |
<!-- END CODENIB_SUPPORT_MATRIX:agent:reponavigator -->

RepoNavigator (paper revision `arXiv:2512.20957v6`) gives its agent one
repository tool:

```text
jump(file_path, symbol, index?) -> definition source snippet and file path
```

`RepoNavigatorRepositoryProvider` serves that tool from an existing
graph-enabled index. It finds the chosen occurrence of `symbol` in
`file_path`, asks a language-server-style definition provider where it is
defined, and returns the containing definition's source and path. It builds no
RepoNavigator-specific index.

The definition must come from a semantic source. By default, `from_manifest`
loads the persisted `lsp_index.pkl` next to `graph.pkl`; a caller may instead
inject a live language server. An index with only symbol-graph position
heuristics is rejected, unless the caller explicitly accepts degraded behavior.

```python
from codenib.integrations.reponavigator import (
    RepoNavigatorRepositoryProvider,
    get_reponavigator_tool_schemas,
)

provider = RepoNavigatorRepositoryProvider.from_manifest(
    "/path/to/repo_manifest.json",
)
tools = get_reponavigator_tool_schemas()
signal = provider.signal_metadata()
observation = provider.dispatch(
    "jump",
    {"file_path": "src/service.py", "symbol": "calculate_tax", "index": 0},
)
```

- `file_path` is the file that refers to the symbol, not the file that defines
  it. `index` counts resolvable occurrences from zero and defaults to `0`.
- Persisted SCIP occurrences skip comments and other non-code text.
  Definition source is capped at 400 lines and 32,000 characters.
- Invalid paths, missing occurrences, unavailable definitions, and
  out-of-range indices return an explicit `Jump failed: ...` observation.

To inspect the contract without RepoNavigator:

```bash
python examples/integrations/reponavigator.py \
  --manifest /path/to/repo_manifest.json \
  --file-path src/service.py \
  --symbol calculate_tax \
  --index 0
```

Pass `--allow-graph-fallback` only when degraded, graph-position behavior is
acceptable. The opt-in is enforced on every call as well as at startup: a SCIP
lookup that falls back to the symbol graph at run time is rejected otherwise.
Before the first call, `provider.signal_metadata()` describes the configured
signal; after it, the backend that served the latest call. It distinguishes
persisted SCIP and native occurrence signals, a live language server,
caller-attested external signals, and graph fallback.

This provider follows the paper's published contract only. RepoNavigator has
released no agent code, so CodeNib claims nothing about its loop, prompts,
error wording, GRPO training, or reported scores.

```bash
pytest -q test/integrations/test_reponavigator.py
```

## Paired provider compatibility

The paired runner answers one question: does an agent localize as well with
CodeNib as its backend as with its own? It keeps the upstream policy, model,
prompt, case set, and iteration budget fixed, and swaps only the backend,
`native` or `codenib`. LocAgent and OrcaLoca support it.

```bash
python scripts/analysis/compare_agent_integrations.py locagent \
  --cases /path/to/cases.json \
  --output-dir results/locagent-paired \
  --provider both \
  --locagent-checkout /path/to/LocAgent \
  --locagent-python /path/to/locagent-env/bin/python \
  --native-index-dir /path/to/locagent-index \
  --model "$LOCAGENT_MODEL"

python scripts/analysis/compare_agent_integrations.py score-locagent \
  --cases /path/to/cases.json \
  --results-dir results/locagent-paired \
  --output results/locagent-summary.json

python scripts/analysis/compare_agent_integrations.py orcaloca \
  --cases /path/to/cases.json \
  --output-dir results/orcaloca-paired \
  --provider both \
  --orcaloca-checkout /path/to/OrcaLoca \
  --model "$ORCALOCA_MODEL"

python scripts/analysis/compare_agent_integrations.py score-orcaloca \
  --cases /path/to/cases.json \
  --results-dir results/orcaloca-paired \
  --output results/orcaloca-summary.json
```

The LocAgent checkout, Python, and native-index flags are needed only for the
native arm. A CodeNib-only run uses `--provider codenib`, omits all three, and
has no LlamaIndex dependency.

`score-locagent` reports the shared ranked accuracy, precision, and recall at
configurable cutoffs. `score-orcaloca` reports OrcaLoca's unranked File Match
and Function Match. The two agents are never compared under one metric. Both
scorers need explicit golden-patch `gold_files` and `gold_functions` in every
case.

The runner keeps upstream sampling defaults, including OrcaLoca's
`temperature=1.0`, and records one trajectory per case. Its token and time
ratios therefore describe a single run. They are not evidence of a speedup. A
performance comparison needs repeated paired trials, or an endpoint with a
documented deterministic sampling contract. For OrcaLoca, older result cells
can be reused; the summary reports any provenance they lack.

### Fixed SWE-bench Lite coverage

This builds the fixed 50-case selection used in the
[LocAgent result](#locagent-on-swe-bench-lite) and runs LocAgent on it with the
CodeNib backend:

```bash
codenib toolchain install . --language python --scope graph
export CODENIB_SCIP_PYTHON_INDEX_TIMEOUT_SECONDS=900

python scripts/analysis/prepare_swebench_policy_cases.py \
  --dataset-json ~/.codenib/princeton-nlp__SWE-bench_Lite_test.json \
  --output-dir results/locagent-swebench-lite-50 \
  --count 50 \
  --jobs 4

python scripts/analysis/compare_agent_integrations.py locagent \
  --cases results/locagent-swebench-lite-50/cases.json \
  --output-dir results/locagent-swebench-lite-50/results \
  --provider codenib \
  --model "$LOCAGENT_MODEL" \
  --max-iterations 10

python scripts/analysis/compare_agent_integrations.py score-locagent \
  --cases results/locagent-swebench-lite-50/cases.json \
  --results-dir results/locagent-swebench-lite-50/results \
  --provider codenib \
  --model "$LOCAGENT_MODEL" \
  --output results/locagent-swebench-lite-50/summary.json
```

**The selection.** Preparation pins the SWE-bench Lite dataset revision and the
digest of the local dataset file. It picks cases with a seeded,
repository-balanced selection that never looks at the labels. It then creates
a separate checkout at every base commit, derives labels from the golden
patch, and builds the BM25 and symbol-graph views. The sample covers every
repository; it is not weighted to estimate the whole Lite population.
Preparation makes no model requests.

**Graph coverage.** Before a case is accepted, its graph must represent at
least 95% of the commit's visible Python files, and the checkout must still be
clean at its base commit. When `scip-python` times out, preparation keeps the
documents it had already emitted and fills every missing tracked Python file
with a tree-sitter fallback. If no usable document exists, the fallback covers
the whole tree. The fallback adds definitions and containment only, not
reference edges. Each graph is labeled `compiler`, `compiler-prefix`,
`compiler-prefix+syntax`, or `syntax`, and a graph built with fallback is
rebuilt in full on a later commit instead of being patched incrementally.

**Files written.** The run writes `selection.json`, `prepare_report.json`,
`preparation_environment.json`, and `preflight.json`, and publishes
`cases.json` only after every selected case is eligible. Each case report
records the SCIP producer and version read from the index; a failed build
leaves a resumable `build_failure.json`. Re-running the command resumes valid
artifacts but rejects a changed dataset or selection configuration.

**Scoring.** File metrics check predictions against every tracked file and keep
all 50 cases in the denominator, including failed model runs. Function rankings
count only locations that name a function; a class-only location remains a
file prediction. Function metrics cover only cases whose golden patch modifies
or deletes a function present at the base commit, so pure additions and
non-function changes are not scored as function misses.

**Before and after the run.** Before any model call, the runner checks every
checkout commit, tracked-file state, index commit, declared capability, and that
each required view loads. A graph that predates the current graph schema fails
this check and must be rebuilt, so older successful runs do not count as
current evidence. Result files record the case-set digest, the CodeNib and
upstream revisions, the model, and the run options. The scorer keeps missing
and failed cases in the denominator, checks recorded case digests against the
active case set, and rejects results from mixed models. `--allow-incomplete`
writes an explicitly partial audit; it does not turn a partial run into a
complete one.
