# Reproduced Agents

CodeNib re-runs four published code-localization agents in its own harness:
LocAgent, Agentless, CoSIL, and OrcaLoca. Each keeps its published prompts and
control loop, and CodeNib answers its repository calls. CodeNib also serves the
`jump` tool from the RepoNavigator paper, which has no released agent code;
you bring the agent.

The four agents are useful in two ways:

- **As baselines.** They run through the same harnesses, labels, and scorer as
  CodeNib's own methods, so a run of each on the same cases compares them
  directly.
- **On CodeNib's index.** An agent can read the index CodeNib already built for
  a repository instead of building its own. For LocAgent and OrcaLoca, the
  [paired runner](#paired-provider-compatibility) checks that the agent does as
  well on CodeNib's index as on its own.

## At a glance

<!-- BEGIN CODENIB_SUPPORT_MATRIX:agents -->
| Agent | Upstream revision | Fidelity | CodeNib serves | Native ↔ CodeNib | Benchmarks | Results |
| --- | --- | --- | --- | --- | --- | --- |
| [Agentless v1.5.0](#agentless) | [`b150f28`](https://github.com/OpenAutoCoder/Agentless/tree/b150f28465a77a81a7f4776384957a4271f5bd69) | Pinned · upstream prompts | `symbol_graph` | — | Base, SWE-bench Lite, Loc-Bench | None published |
| [LocAgent](#locagent) | [`ef17054`](https://github.com/gersteinlab/LocAgent/tree/ef170542a5cca88a1bd8463335ec43de222ed5f9) | Pinned · upstream prompts and loop | `bm25`, `symbol_graph` | [Paired runner](#paired-provider-compatibility) | Base, SWE-bench Lite, Loc-Bench | [SWE-bench Lite, 50 issues: file Accuracy@1 0.78](#locagent-on-swe-bench-lite) |
| [CoSIL](#cosil) | [`0568e42`](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl) | Pinned · upstream prompts and loop | `symbol_graph` | — | Base, SWE-bench Lite, Loc-Bench | None published |
| [OrcaLoca SearchAgent](#orcaloca) | [`37db289`](https://github.com/fishmingyu/OrcaLoca/tree/37db289be2dc3b7432183fe08b3f06becce87c27) | Pinned · upstream agent | `symbol_graph` | [Paired runner](#paired-provider-compatibility) | Base, SWE-bench Lite, Loc-Bench | None published |
| [RepoNavigator `jump`](#reponavigator) | [`arXiv v6`](https://arxiv.org/abs/2512.20957v6) | Pinned · upstream tool contract | `symbol_graph`, SCIP occurrences | — | — | None published |
<!-- END CODENIB_SUPPORT_MATRIX:agents -->

*Pinned* means CodeNib's copies of the prompts, tool schemas, or hooks were
checked against the named upstream revision; it is not a quality claim. The
upstream agents target Python, and the base `codenib` package does not depend on
any of the upstream projects.

## Results

### LocAgent on SWE-bench Lite

With CodeNib as its backend, LocAgent ranked the changed file first on 78% of
a fixed 50-issue sample from SWE-bench Lite, and had every changed file in its
top three on 88%.

| *k* | File Accuracy@k | Function Accuracy@k |
| ---: | ---: | ---: |
| 1 | 0.78 | 0.54 |
| 3 | 0.88 | 0.75 |
| 5 | 0.88 | 0.79 |

This is one run with one model, and LocAgent has not yet been run with its own
index on the same issues, so the result shows that LocAgent works on CodeNib's
index, not that it works better there.

??? note "Run details"

    - Labels come from the golden patch; the metrics follow the
      [shared definitions](evaluation/harnesses.md#file-and-symbol-metrics).
      Function metrics cover the 48 issues whose patch changes a function that
      exists at the base commit.
    - The 50 issues are a repository-balanced selection (seed
      `codenib-locagent-swebench-lite-50-v1`), so they cover every repository
      but do not estimate a score over all of SWE-bench Lite.
    - Model `gpt-5.6-luna`, at most 10 iterations; about 282,000 tokens and
      26 seconds per issue. All 50 issues completed.
    - Recorded on 2026-08-03 at CodeNib commit `ba886799`, merged in
      [#406](https://github.com/sysevol-ai/CodeNib/pull/406).
    - [Fixed SWE-bench Lite coverage](#fixed-swe-bench-lite-coverage) rebuilds
      the selection and reruns it.

## How the reproductions are checked

Tests exercise each agent's tools without the upstream package installed, and
an optional probe compares the vendored prompts, tool schemas, and hooks with
the pinned upstream revision. Benchmark runs count a failed case as a miss.
Every agent is scored with the shared file and symbol metrics; metrics that an
upstream project reports itself, such as LocAgent's nDCG or OrcaLoca's File
Match, stay separate. RepoNavigator has no released code, so only its `jump`
contract is tested.

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
| Results | [SWE-bench Lite, 50 issues: file Accuracy@1 0.78](#locagent-on-swe-bench-lite) |
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
plugin, and `provider.dispatch(name, arguments)` calls a tool directly.

LocAgent and CodeNib name graph relations differently. The provider maps them,
and says in its output when it used a broader relation:

| LocAgent relation | CodeNib relation | Fidelity |
| --- | --- | --- |
| `contains` | `contain` | Exact |
| `imports` | `import` | Exact when emitted by the graph backend |
| `invokes` | `reference` | Conservative superset |
| `inherits` | `type-use` | Conservative superset |

??? note "Provider guarantees"

    - Source paths are repository-relative and cannot escape the index's root.
    - External line numbers are 1-based; CodeNib's graph ranges stay 0-based.
    - A missing, stale, or failed graph or BM25 view produces an explicit
      error.
    - Tool output is deterministic and bounded by result, traversal,
      source-line, and character budgets.
    - Loading the provider never invokes LocAgent, NetworkX, LlamaIndex, or an
      index builder.
    - The tool surface is also checked against the
      [OpenHands LocAgent implementation](https://github.com/OpenHands/OpenHands/tree/efe287ce3402706a171b3a5fb40f15914e98ef20/openhands/agenthub/loc_agent)
      at its recorded revision.

### Run LocAgent on a benchmark

`LocAgentAgent` runs LocAgent's prompts and reasoning loop in CodeNib and
answers its tool calls with the provider above. It needs no LocAgent checkout
and does not import LocAgent, LiteLLM, or LlamaIndex. `--base-url` accepts any
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

??? note "Tests"

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

CodeNib supplies all three inputs from its symbol graph and the checkout,
without Agentless or LibCST installed. Agentless's Python-only and `test*`
directory filters are kept, so on CodeNib Base only the Python issues are
scored.

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
uses CodeNib's own prompts and is not the pinned Agentless policy.

??? note "Fidelity notes and tests"

    File skeletons come from Python's standard `ast` module. They keep the same
    classes, callables, and module assignments as Agentless's LibCST output,
    though formatting can differ.

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

CoSIL works in two steps, following its
[RQ1 scripts](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl):

1. rank files, then check that ranking against their import statements;
2. inspect candidate classes and functions with four tools, optionally prune
   irrelevant observations, and write a final XML location summary.

`CoSILRepositoryProvider` answers the four tools from CodeNib's symbol graph,
and reads requested files with Python's `ast` module to match CoSIL's own
class and function boundaries. It builds no graph of its own.

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
Agentless, or LiteLLM:

```bash
python examples/cosil_loc_agent.py \
  --dataset codenib_base \
  --model "$COSIL_MODEL" \
  --result-path results/cosil.jsonl
```

??? note "Fidelity notes and tests"

    The provider's class, function, method, range, and source records matched
    CoSIL's own parser on all 1,791 eligible Python files in five SWE-bench
    Lite snapshots (Requests, Flask, Django, SymPy, and Pylint). If CoSIL's
    import reflection comes back malformed, CodeNib keeps the initial file
    ranking rather than an empty candidate set.

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
| Results | None published |
| Entry points | `OrcaLocaAgent`, `examples/orcaloca_loc_agent.py` |
<!-- END CODENIB_SUPPORT_MATRIX:agent:orcaloca -->

### Use CodeNib as OrcaLoca's backend

OrcaLoca's model calls six search functions (`search_file_contents`,
`search_source_code`, `search_class`, `search_method_in_class`,
`search_callable`, and `search_file_tree`), and its `SearchWorker` calls a few
private hooks for history, distance, and disambiguation. CodeNib implements
both from its index:

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
from, and it creates no OrcaLoca index directory. Results keep OrcaLoca's
`file::Class::method` identities and 1-based locations.

### What is not reproduced

OrcaLoca can analyze an issue's stack traces before it searches. CodeNib starts
after that stage and gives `SearchAgent` an empty trace analysis, which is also
what OrcaLoca falls back to when its own analysis fails. Results therefore
reflect OrcaLoca's search, not its full published pipeline.

```text
issue -> [trace analysis: not reproduced] -> SearchAgent -> repository backend
```

### Run OrcaLoca on a benchmark

`OrcaLocaAgent` finds the checkout's CodeNib index, runs OrcaLoca with it, and
converts the final locations into symbols such as `Service.run()`. It imports
OrcaLoca and LlamaIndex only when a task runs.

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
are available separately:

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

??? note "Fidelity notes and tests"

    CodeNib's graph supplies identities and relations; a parse of the source,
    which runs no code, restores the pinned agent's symbol kinds, signatures,
    docstrings, and ranges. Symbols the pinned agent would not recognize are
    not exposed. Prose and the order of tied results may differ from upstream.
    The private hooks are a compatibility surface for the pinned revision, not
    CodeNib APIs. File Match and Function Match need golden-patch labels;
    trajectory-read labels such as SWE-Explore's `read_core_*` measure a
    different target.

    ```bash
    pytest -q test/integrations/test_orcaloca.py \
      test/eval/test_orcaloca_metrics.py \
      test/eval/test_orcaloca_benchmark_adapter.py

    ORCALOCA_CHECKOUT=/path/to/OrcaLoca \
      pytest -q test/integrations/test_orcaloca_upstream.py
    ```

    The upstream probe is marked `integration` and skips without
    `ORCALOCA_CHECKOUT`.

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

The RepoNavigator paper (`arXiv:2512.20957v6`) gives its agent one repository
tool:

```text
jump(file_path, symbol, index?) -> definition source snippet and file path
```

`RepoNavigatorRepositoryProvider` serves it from a graph-enabled index: it
finds the chosen occurrence of `symbol` in `file_path` and returns the source
and path of its definition. Definitions come from the persisted language-server
index (`lsp_index.pkl`) or a live language server you inject; an index that
could only guess from graph positions is rejected unless you allow it.

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
- Definition source is capped at 400 lines and 32,000 characters.
- Failures return an explicit `Jump failed: ...` observation.

To try it without RepoNavigator:

```bash
python examples/integrations/reponavigator.py \
  --manifest /path/to/repo_manifest.json \
  --file-path src/service.py \
  --symbol calculate_tax \
  --index 0
```

Pass `--allow-graph-fallback` only if guessed definitions are acceptable.
`provider.signal_metadata()` reports which source served the latest call:
persisted SCIP or native occurrences, a live language server, an external
signal, or graph fallback. CodeNib follows only the paper's published `jump`
contract; it makes no claim about RepoNavigator's agent, prompts, training, or
scores.

??? note "Tests"

    ```bash
    pytest -q test/integrations/test_reponavigator.py
    ```

## Paired provider compatibility

The paired runner checks whether an agent localizes as well on CodeNib's index
as on its own. It holds the agent, model, prompt, cases, and iteration budget
fixed, and swaps only the backend: `native` or `codenib`. It supports LocAgent
and OrcaLoca. In a four-issue OrcaLoca run, both backends matched the
golden-patch files and functions by OrcaLoca's File Match and Function Match,
which confirms the swap works end to end.

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

The LocAgent checkout, Python, and native-index flags are only for the native
arm; a CodeNib-only run uses `--provider codenib` and needs no LlamaIndex.
`score-locagent` reports the shared ranked metrics, and `score-orcaloca`
reports OrcaLoca's File Match and Function Match.

??? note "Reading paired results"

    The runner keeps upstream sampling defaults, including OrcaLoca's
    `temperature=1.0`, and records one trajectory per case. Token and time
    ratios therefore describe a single run, not a speedup; a performance
    comparison needs repeated trials or deterministic sampling. Both scorers
    need golden-patch `gold_files` and `gold_functions` in every case. Older
    OrcaLoca result cells can be reused; the summary reports any provenance
    they lack.

### Fixed SWE-bench Lite coverage

These commands build the fixed 50-issue selection behind the
[LocAgent result](#locagent-on-swe-bench-lite) and run LocAgent on it with the
CodeNib backend. Preparation makes no model calls.

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

??? note "What preparation and scoring guarantee"

    - **Selection.** The dataset revision and file digest are pinned. Issues
      are chosen by a seeded, repository-balanced selection that never reads
      the labels, each gets its own checkout at the base commit, and labels
      come from the golden patch.
    - **Graph coverage.** A case is accepted only if its graph covers at least
      95% of the commit's visible Python files. If `scip-python` times out, the
      documents it emitted are kept and a tree-sitter fallback fills the rest;
      the fallback adds definitions and containment, not references, and the
      graph is labeled `compiler`, `compiler-prefix`, `compiler-prefix+syntax`,
      or `syntax`.
    - **Files.** The run writes `selection.json`, `prepare_report.json`,
      `preparation_environment.json`, and `preflight.json`, and publishes
      `cases.json` only once every case is eligible. Re-running resumes valid
      work and rejects a changed dataset or selection.
    - **Scoring.** File metrics count all 50 issues, including failed runs.
      Function metrics count only issues whose patch changes a function that
      exists at the base commit; a class-only location counts as a file
      prediction.
    - **Checks.** Before any model call, the runner verifies each checkout,
      the index, and that the required views load; a graph built under an older
      schema must be rebuilt. Results record the case-set digest, revisions,
      model, and run options, mixed-model results are rejected, and
      `--allow-incomplete` writes an explicitly partial audit.
