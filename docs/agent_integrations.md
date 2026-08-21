# Agent Integrations

CodeNib can supply repository search and graph-navigation tools to an external
agent without importing that agent or rebuilding its indexes. Integrations bind
to one existing `repo_manifest.json` through `ServerContext`; they never create
an agent-specific graph, BM25 index, or cache directory.

## Support Matrix

The support levels below are intentionally separate. **Provider** means CodeNib
implements the repository-call contract. **Policy** means a revision-pinned
agent loop executes with that provider. **Paired evaluation** means the native
and CodeNib providers can be swapped under one fixed case, model, prompt, and
budget contract. None of these labels alone claims end-to-end task quality.
Integration links point to the exact upstream revisions checked by the probes.

| Integration | Status | Required views | Provider contract | Policy and evaluation | Boundary |
| --- | --- | --- | --- | --- | --- |
| [LocAgent](https://github.com/gersteinlab/LocAgent/tree/ef170542a5cca88a1bd8463335ec43de222ed5f9) | CodeNib-native, revision-pinned | Symbol graph + BM25 | Pinned three-tool contract | Vendored prompts and function-calling loop; paired runner with strict common file/function@k scoring | Python SWE-bench repositories; reference and type-use are disclosed as conservative relation mappings |
| [Agentless v1.5.0](https://github.com/OpenAutoCoder/Agentless/tree/b150f28465a77a81a7f4776384957a4271f5bd69) | CodeNib-native, revision-pinned | Symbol graph | Python tree, symbol skeleton, and line-window context | Vendored three-stage localization prompts; shared ranked file/symbol scoring | Localization only; repair and patch validation are excluded |
| [CoSIL](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl) | CodeNib-native, revision-pinned | Symbol graph | Pinned four-tool contract | Vendored file reflection, function tool loop, prune policy, and shared localization scoring | RQ1 file/function localization only; line localization and patch generation are excluded |
| [OrcaLoca SearchAgent](https://github.com/fishmingyu/OrcaLoca/tree/37db289be2dc3b7432183fe08b3f06becce87c27) | Revision-pinned supported | Symbol graph | Pinned six-tool and private-hook contract | Upstream `SearchAgent`; fixed-case paired runner and native File/Function Match scorer | Python SWE-bench repositories; empty `TraceAnalysisOutput`; upstream trace generation is not included |

### What Supported Means

A revision-pinned integration must pass four separate gates:

1. Dependency-free provider tests exercise its public tools, source identities,
   ranges, budgets, and failure behavior.
2. An upstream probe checks the pinned tool signatures and the private hooks
   that the policy actually invokes.
3. Benchmark preflight verifies clean checkout commits, manifest identity,
   index-visible untracked files, builder profiles, declared capabilities, and
   successful loading of every required runtime view before a model call.
4. The paired runner records every requested cell, keeps failures in the
   denominator, and binds the case-set, CodeNib, upstream, model, and run-option
   identities into result provenance.

Passing these gates supports the stated provider and policy boundary. It does
not imply compatibility with an arbitrary upstream revision or with stages
explicitly excluded by the matrix.

Optional upstream packages are required only where the policy still executes
inside the upstream runtime. Provider imports and standalone provider examples
remain dependency-free. Exact pinned
revisions, fidelity limits, commands, and measured evidence follow below.
Provider startup also selects only its declared runtime views: LocAgent loads
the symbol graph and BM25, while Agentless, CoSIL, and OrcaLoca load only the
symbol graph. Dense and Zoekt runtimes are not imported or started for these
contracts.
CodeNib's graph providers can represent additional languages, but this matrix
does not extend a Python-scoped upstream policy or native comparator beyond its
validated domain.

A deterministic CodeNib-only contract fixture additionally exercises the
LocAgent provider over one TypeScript/Go manifest, including source ranges,
indexed search, and graph traversal. This checks that the provider boundary is
language-agnostic; it is not evidence that the pinned Python-only LocAgent or
OrcaLoca native implementations support those languages.

### Shared LSP navigation provider

The definition, reference, and route agent skills resolve the same provider
injected into `ExpandContext`. In 0.2.2 manifest-bound contexts, including
C/C++-only builds, select the verified persisted symbol graph. Native clangd
fact-query postings remain an independently benchmarked implementation, but
their mutable project-local `.idx` generation has no source-selection receipt
or allowed-file proof yet and is not admitted by the agent runtime. Portable
artifacts and mixed-language contexts use the same persisted route. Provider
results retain backend,
fallback, capability, and snapshot metadata so an agent trace can explain the
route without changing the public location shape.

## Benchmark Compatibility

These integrations evaluate CodeNib's native repository algorithms against
pinned external datasets and scorer contracts. They join benchmark data,
translate source evidence at the protocol boundary, and run the corresponding
scorer. Coverage retains preparation failures; quality is success-conditioned
with its denominator stated explicitly.

### [SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench)

CodeNib's native `RepositoryContextExplorer` owns route planning, retrieval,
graph expansion, reranking, and source validation. The SWE-Explore
compatibility layer preserves that execution path and converts the resulting
0-based evidence into the benchmark's repository-relative, 1-based inclusive
`ContextRegion` records for official evaluation:

```python
from codenib.integrations.swe_explore import CodeNibSWEExploreExplorer

explorer = CodeNibSWEExploreExplorer.from_repository(
    "/path/to/checkout",
    policy="auto",
)
results = explorer.explore(
    instance_id="org__repo-123",
    query="issue text",
    top_k=20,
)
```

`auto` plans against manifest-advertised BM25, vector, and symbol-graph
capabilities and loads only the selected query route. Stable ablation policies
are `bm25`, `dense`, `hybrid`, `hybrid_rerank`, and `graph`. Index construction
remains a separate operation; the strict runner materializes and records the
exact views required by the selected policy.

Compatibility is pinned to SWE-Explore revision
`3c12dc5a551937038afcbdb6eb6bbf19f3ddd8c1` and released dataset revision
`bdb0ae45d7c337d9e1dc3ebfe2a0af6bc7c1fbd9`. The release rows contain regions
but not issue text or `base_commit`, so CodeNib joins them by `instance_id` to
pinned SWE-bench Verified, Multilingual, and Pro source revisions. The Pro-only
`instance_` prefix is normalized at that join boundary.

Two different cutoffs remain explicit:

- `top_k` limits the number of ranked regions returned by an explorer.
- Recall/nDCG at 100, 300, or 500 applies an accumulated source-line budget in
  the evaluator.

CodeNib's dependency-free scorer reproduces all 17 metrics registered by the
pinned official runner. Differential checks matched the official evaluator on
250 generated cases and on all `60` cells from the fixed real-repository run,
for `1,020/1,020` exact real-output metric values. The loader intentionally
preserves malformed optional trajectory ranges under upstream semantics: the
released data contains 455 reversed optional ranges and 8 with `end=0`.
Silently repairing them would make CodeNib's scores incomparable.

The released compatibility result is the explicit `bm25` control on 20
base-commit checkouts across
Python, Go, Rust, TypeScript, JavaScript, C, and C++. All 20 passed checkout,
benchmark-digest, BM25-profile, build, load, and query gates. Snapshot checks
include index-visible untracked and gitignored source files. On the validation machine, median
first-build, view-load, and query times were 0.81 s, 64 ms, and 67 ms. These are
compatibility measurements, not a population estimate or a claim of task-level
agent improvement. The [case set](assets/swe_explore_cases.json)
and [validation report](evaluation/swe_explore.md) record the exact
scope.

Run the strict CodeNib harness after preparing clean detached checkouts at the
joined source commits:

```bash
codenib-swe-explore-benchmark \
  --bench /path/to/bench.final.public.jsonl \
  --case-set docs/assets/swe_explore_cases.json \
  --repos-root /path/to/repos \
  --output results/codenib-swe-explore.json \
  --policy auto \
  --top-k 5,10,20
```

Use `--policy bm25` to reproduce the published compatibility control. The
runner records the policy, materialized view set, selected per-query plan, and
runtime trace; no native `auto` quality number is claimed until that arm is
executed on the pinned case set.

The upstream SWE-Explore runner can also select `--explorers codenib`. Its
native BM25 control has a Python/document-oriented extension allowlist, so the
seven-language aggregate is useful as an integration smoke test but not as a
fair cross-language algorithm comparison.

## [LocAgent](https://github.com/gersteinlab/LocAgent)

The LocAgent provider implements the policy's three repository functions:

- `search_code_snippets`
- `get_entity_contents`
- `explore_tree_structure`

Build a graph-enabled repository index, then bind the provider:

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

The standalone provider example requires no LocAgent installation:

```bash
python examples/integrations/locagent.py \
  --manifest /path/to/repo_manifest.json \
  --search "configuration loader"
```

`provider.bindings()` returns the same-named Python callables for a runtime
plugin. Modern runtimes can use `provider.dispatch(name, arguments)` directly.
The supported behavior is pinned to
[LocAgent revision `ef170542`](https://github.com/gersteinlab/LocAgent/tree/ef170542a5cca88a1bd8463335ec43de222ed5f9).
The tool surface is also checked against the
[OpenHands LocAgent implementation](https://github.com/OpenHands/OpenHands/tree/efe287ce3402706a171b3a5fb40f15914e98ef20/openhands/agenthub/loc_agent)
at its recorded revision.

### Relation Semantics

LocAgent and CodeNib use different graph relation vocabularies. The provider
maps them at the integration boundary:

| LocAgent relation | CodeNib relation | Fidelity |
| --- | --- | --- |
| `contains` | `contain` | Exact |
| `imports` | `import` | Exact when emitted by the graph backend |
| `invokes` | `reference` | Conservative superset |
| `inherits` | `type-use` | Conservative superset |

Traversal output states when a broader relation was used. It does not present
reference or type-use edges as exact call or inheritance facts.

### Runtime Guarantees

- Source paths are repository-relative and cannot escape the manifest root.
- External line numbers are 1-based; CodeNib graph ranges remain 0-based.
- Missing, stale, or failed graph and BM25 views produce explicit errors.
- Tool output is deterministic and bounded by provider-level result, traversal,
  source-line, and character budgets.
- Loading the provider never invokes LocAgent, NetworkX, LlamaIndex, or an
  index builder.

### Shared Benchmark Interface

`LocAgentAgent` retains LocAgent's pinned prompts, function schemas, and
reasoning loop while replacing its repository data plane with
`LocAgentToolProvider`. It implements the same
`BaselineTask -> locate_code() -> BaselineRunResult` contract as the other
localization baselines:

```bash
python -m pip install "codenib[agent,graph]==0.2.2"
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

The adapter vendors the prompts and function schemas from the pinned revision,
runs the policy loop in CodeNib, and resolves final file, class, function, and
1-based line records through CodeNib's language-neutral graph. Delivery does
not require a LocAgent checkout or a second Python environment, and it does
not import LocAgent, LiteLLM, or LlamaIndex. The optional source-level probe
above checks the vendored contract against pinned Git objects without importing
upstream packages. OpenAI is loaded only when the policy executes, and
`--base-url` accepts an
OpenAI-compatible endpoint such as a LiteLLM proxy.

The common runner reports the same ranked file/symbol accuracy, precision, and
recall for every policy. LocAgent's additional native nDCG and MAP reports
remain artifact-specific; they do not silently replace the shared metric
contract.

```bash
python examples/locagent_loc_agent.py \
  --dataset codenib_base \
  --model "$LOCAGENT_MODEL" \
  --result-path results/locagent.jsonl
```

Run the provider and common-runner tests with:

```bash
pytest -q test/integrations/test_locagent.py \
  test/eval/test_locagent_benchmark_adapter.py
```

## [Agentless](https://github.com/OpenAutoCoder/Agentless)

`AgentlessAgent` preserves the classic Agentless v1.5.0 localization sequence:

1. rank files from the filtered Python project tree;
2. identify classes, functions, methods, and variables from compressed files;
3. refine them to source-linked edit locations in numbered context windows.

CodeNib supplies all three inputs from one manifest-backed symbol graph and its
bound checkout. It does not build Agentless's per-case AST artifact, import
Agentless or LibCST, or run the downstream repair and patch-validation phases.
The output is normalized to `BaselineRunResult`, so file and symbol ranking use
the same denominator and metrics as other localization policies.

```python
from codenib.clients.agentless_agent import AgentlessAgent

agent = AgentlessAgent(model="openai-compatible-model")
result = await agent.locate_code(
    query_text=issue,
    repo_path=checkout,
)
```

The standalone provider can inspect the exact context delivered to each stage:

```bash
python examples/integrations/agentless.py \
  --manifest /path/to/repo_manifest.json \
  --file src/service.py
```

Run the policy through the shared benchmark harness with:

```bash
python examples/agentless_loc_agent.py \
  --dataset codenib_base \
  --model "$AGENTLESS_MODEL" \
  --result-path results/agentless.jsonl
```

Compatibility is pinned to
[Agentless `v1.5.0`](https://github.com/OpenAutoCoder/Agentless/tree/b150f28465a77a81a7f4776384957a4271f5bd69).
The optional upstream probe reads
that Git object and checks the three vendored prompts byte-for-byte. The
provider intentionally keeps Agentless's Python-only and `test*` subtree
filters. Its stdlib-AST skeleton preserves the policy's classes, callables, and
module assignments, but formatting can differ from Agentless's LibCST output;
that distinction is treated as provider fidelity, not exact artifact equality.

The older `codenib.model.AgentlessPipeline` remains available for compatibility
with existing callers. It uses CodeNib-specific structured prompts and is not
the revision-pinned Agentless policy described in this matrix.

```bash
pytest -q test/integrations/test_agentless.py \
  test/integrations/test_agentless_policy.py \
  test/model/test_agentless_pipeline.py

AGENTLESS_CHECKOUT=/path/to/Agentless \
pytest -q test/integrations/test_agentless_upstream.py
```

## [CoSIL](https://github.com/ZhonghaoJiang/CoSIL)

The CoSIL integration follows the two scripts used by the
[pinned public RQ1 path](https://github.com/ZhonghaoJiang/CoSIL/tree/0568e423735b399d5b089996961fea9ae142e4c7/CoSIL/fl):

1. rank files and reflect that ranking against their import statements;
2. inspect candidate classes and functions through four tools, optionally prune
   irrelevant observations, and emit a final XML location summary.

`CoSILRepositoryProvider` implements the pinned tools:

- `get_code_of_class`
- `get_code_of_class_function`
- `get_code_of_file_function`
- `exit`

The provider loads only CodeNib's symbol graph. File identity comes from that
manifest view, while a lazy Python AST parse of requested candidate files
restores CoSIL's exact class/function classification and source ranges. It does
not load CoSIL's `repo_structures/<instance>.json` or build another graph.

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

Inspect the candidate contract without CoSIL installed:

```bash
python examples/integrations/cosil.py \
  --manifest /path/to/repo_manifest.json \
  --file src/service.py
```

`CoSILAgent` vendors the file/reflection prompts, four-tool loop, optional
per-result prune loop, and XML summary prompt from revision
`0568e423735b399d5b089996961fea9ae142e4c7`. The optional upstream probe compares
those prompt and schema objects directly with the pinned Git tree. The runtime
does not import CoSIL, Agentless, LiteLLM, or a second index implementation.

```bash
python examples/cosil_loc_agent.py \
  --dataset codenib_base \
  --model "$COSIL_MODEL" \
  --result-path results/cosil.jsonl
```

This boundary matches CoSIL's file and function localization experiment. It
does not claim compatibility with its line-localization, patch-generation, or
test-validation stages. If import reflection is malformed, CodeNib retains the
validated initial file ranking rather than turning an empty reflection into an
empty candidate set.

The AST compatibility check compared all 1,791 eligible Python files from five
SWE-bench Lite repository snapshots (Requests, Flask, Django, SymPy, and
Pylint) against CoSIL's pinned `parse_python_file`; class, function, method,
range, and source records matched for every file. This validates the provider
contract, not CoSIL's model-dependent localization score.

```bash
pytest -q test/integrations/test_cosil.py \
  test/integrations/test_cosil_policy.py

COSIL_CHECKOUT=/path/to/CoSIL \
pytest -q test/integrations/test_cosil_upstream.py
```

## [RepoNavigator](https://arxiv.org/abs/2512.20957v6)

RepoNavigator paper revision `arXiv:2512.20957v6` publishes one repository
application tool:

```text
jump(file_path, symbol, index?) -> definition source snippet and file path
```

`RepoNavigatorRepositoryProvider` supplies that tool from an existing
graph-enabled manifest. It finds the selected occurrence in the referring
source file, converts it to a 0-based line and provider-specific UTF-8, UTF-16,
or UTF-32 character offset, delegates to an LSP-shaped definition provider, and
returns the containing definition source together with its repository path. It
does not build a RepoNavigator-specific index.

The semantic definition signal is part of the compatibility gate. By default,
`from_manifest` loads the persisted `lsp_index.pkl` beside `graph.pkl`; callers
may instead inject a live language-server provider. A manifest with only
symbol-graph position heuristics is rejected unless the caller explicitly opts
into degraded behavior.

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

The schema contains only the paper's lowercase `jump` name. `file_path` is the
referencing file, not the definition file; `index` is a zero-based resolvable
occurrence index and defaults to `0`. Persisted SCIP occurrences filter comments
and other non-semantic text. Definition source is bounded to 400 lines and
32,000 characters. Invalid paths, missing occurrences, unavailable definitions,
and out-of-range indices return explicit `Jump failed: ...` observations.

Inspect the contract without installing RepoNavigator:

```bash
python examples/integrations/reponavigator.py \
  --manifest /path/to/repo_manifest.json \
  --file-path src/service.py \
  --symbol calculate_tax \
  --index 0
```

Use `--allow-graph-fallback` only when degraded graph-position behavior is
acceptable. The opt-in is enforced for each definition call as well as at
startup: a SCIP occurrence lookup that dynamically falls back to the symbol
graph is rejected otherwise. Before the first call,
`provider.signal_metadata()` describes the configured signal; afterwards it
describes the backend that served the most recent call. It distinguishes
persisted SCIP and native occurrence signals, live LSP, caller-attested
external, and graph-fallback behavior. This is a paper-contract provider, not
a revision-pinned upstream integration: CodeNib does not claim compatibility
with an unreleased agent loop, prompt, error wording, GRPO training, or reported
benchmark scores.

```bash
pytest -q test/integrations/test_reponavigator.py
```

## [OrcaLoca](https://github.com/fishmingyu/OrcaLoca)

The OrcaLoca provider replaces its repository data plane while retaining the
upstream search policy. It implements the six functions exposed to the model:

- `search_file_contents`
- `search_source_code`
- `search_class`
- `search_method_in_class`
- `search_callable`
- `search_file_tree`

It also implements the history, distance, exact-location, decomposition, and
disambiguation hooks that OrcaLoca's `SearchWorker` calls directly. Those
private hooks are a revision-scoped compatibility surface, not new CodeNib core
APIs.

Build a graph-enabled manifest and create the factory:

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

With the small injection seam from
[OrcaLoca PR #140](https://github.com/fishmingyu/OrcaLoca/pull/140), pass that
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

The factory verifies that `repo_path` is the repository bound by the manifest.
It loads no OrcaLoca graph and creates no `_index_data` directory. Each agent
gets isolated lightweight query history, while source, symbol identity,
containment, dependency distance, and ranges all come from the same
manifest-backed `ServerContext`.

Compatibility is pinned to
[OrcaLoca revision `37db289`](https://github.com/fishmingyu/OrcaLoca/tree/37db289be2dc3b7432183fe08b3f06becce87c27).
The adapter preserves its
repository-relative `file::Class::method` identities, 1-based locations, and
prompt-visible markers. CodeNib's graph supplies identity and relations; a
lazy, non-executing parse of the manifest-bound Python source restores the
pinned policy's symbol kinds, signatures, docstrings, skeletons, and source
ranges. Python symbols that the pinned visitor does not recognize are not
exposed merely because CodeNib's graph is richer. Prose and ambiguous-result
tie ordering may differ outside this semantic output boundary.

### Trace-Analysis Boundary

The supported OrcaLoca integration begins at `SearchAgent`, after the optional
upstream trace-analysis stage:

```text
issue -> [trace analysis: out of scope] -> SearchInput -> SearchAgent -> repository provider
```

Both the generic adapter and paired runner construct `SearchInput` with an
empty `TraceAnalysisOutput`. This is a valid upstream input and is also
OrcaLoca's fallback when its trace-analysis stage raises an exception. Holding
that input fixed isolates the repository provider: native and CodeNib runs
receive the same absence of trace-derived hints, while only the search manager
changes. Consequently, the integration supports OrcaLoca SearchAgent execution
and provider compatibility; it does not claim to reproduce OrcaLoca's complete
trace-analysis pipeline or its published end-to-end score. A future
trace-enabled experiment should supply one fixed, recorded trace to both
providers rather than regenerate traces independently.

### Evaluation Metrics

The dependency-free scorer under `codenib.eval.benchmarks.orcaloca` implements
OrcaLoca's published File Match and Function Match definitions. Both are
golden-patch subset metrics; extra predictions affect the separately reported
precision but not the binary match.

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

Ground-truth fields must come from the benchmark's golden patch. Trajectory-read
labels such as SWE-Explore's `read_core_*` fields measure a different target.

### Shared Benchmark Interface

`OrcaLocaAgent` adapts the upstream policy to the same
`BaselineTask -> locate_code() -> BaselineRunResult` contract used by the
Claude and Codex localization baselines:

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

The adapter resolves the checkout's CodeNib manifest, injects the
manifest-backed search manager, runs OrcaLoca with an empty trace-analysis
input, and converts final `file/class/method` records into canonical generic
symbols such as `Service.run()`. It imports OrcaLoca and LlamaIndex only when a
task runs.

The generic runner reports the common file/symbol top-k metrics. OrcaLoca's
published File Match and Function Match remain separate because they use
golden-patch subset semantics rather than the generic ranking metric. This
prevents an external policy's native metric from silently changing the common
benchmark contract.

Run the common dataset loop with:

```bash
python examples/orcaloca_loc_agent.py \
  --dataset codenib_base \
  --model "$ORCALOCA_MODEL" \
  --orcaloca-checkout /path/to/OrcaLoca \
  --result-path results/orcaloca.jsonl
```

Run the local contract and provider tests with:

```bash
pytest -q test/integrations/test_orcaloca.py \
  test/eval/test_orcaloca_metrics.py \
  test/eval/test_orcaloca_benchmark_adapter.py
```

To compare against a pinned upstream checkout and exercise OrcaLoca's actual
decomposition, priority queue, and final-location decoder:

```bash
ORCALOCA_CHECKOUT=/path/to/OrcaLoca \
  pytest -q test/integrations/test_orcaloca_upstream.py
```

The upstream probe is optional and marked `integration`; without
`ORCALOCA_CHECKOUT`, it skips before importing OrcaLoca.

The base `codenib` package therefore gains no LocAgent, OrcaLoca, LlamaIndex,
pandas, or NetworkX dependency.

## Paired Provider Compatibility

The generic examples above measure each CodeNib-backed policy against common
localization targets. The paired compatibility driver instead keeps a pinned
upstream policy, model, prompt, case set, and iteration budget fixed while
swapping only the repository provider between `native` and `codenib`:

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

The LocAgent checkout, Python, and native-index flags belong only to the
optional upstream-native comparison cell. A CodeNib-only run uses
`--provider codenib`, omits all three, and has no LlamaIndex dependency.

### Fixed SWE-bench Lite coverage

Prepare a broad, reproducible CodeNib-only coverage set before running the
policy:

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

The preparation command pins the official SWE-bench Lite dataset revision and
the local dataset-file digest. Its label-independent, seeded selection covers
every repository stratum, creates an isolated checkout for every base commit,
derives labels from the golden patch, and builds the required BM25 and symbol
graph views. A label-independent guard requires the graph to represent at
least 95% of the commit's visible Python source files and rejects paths outside
that commit. The final audit also requires the checkout to remain clean at its
declared base commit. Each case report reads the actual SCIP producer and
version from the persisted index; a failed build leaves a resumable
`build_failure.json` sidecar. The run writes `selection.json`, `prepare_report.json`,
`preparation_environment.json`, and `preflight.json`; `cases.json` is published
only after every selected case is eligible. Re-running the command resumes
valid artifacts but rejects a changed dataset or selection configuration.
Preparation performs no model requests.
The sample is deliberately repository-balanced coverage, not a
population-weighted estimate over all SWE-bench Lite tasks.

When `scip-python` times out after emitting parseable, path-addressable protobuf
documents, preparation retains that compiler-derived prefix and applies the
same label-independent tree-sitter fallback to every missing tracked Python
file. If no usable compiler document exists, the malformed or metadata-only
artifact is rejected and the same fallback covers the full tracked surface.
Each compiler attempt removes any earlier `index.scip` first, so retained
documents can only come from that attempt. A graph built with source-coverage
fallback is fully rebuilt on a later commit instead of entering the incremental
patch path, which keeps its compiler/fallback provenance complete.
The report distinguishes compiler availability and coverage from supplemented
files, symbols, and final Git-surface coverage. The manifest also records
whether generation completed or retained a partial compiler prefix. The
fallback adds definitions and containment only; it does not synthesize
compiler reference edges. Graph audits label the resulting surface as
`compiler`, `compiler-prefix`, `compiler-prefix+syntax`, or `syntax`.

File metrics validate predictions against every tracked repository file and
retain all 50 selected cases in the denominator, including failed model cells.
Function rankings include only locations that explicitly name a function;
class-only locations remain file predictions. Function metrics use the declared
subset whose golden patch modifies or deletes a function present in the base
snapshot; pure additions and non-function changes are not silently scored as
function misses.

Before any model call, the driver checks every requested checkout commit,
tracked-file state, manifest commit, required capability, and actual loading of
the required runtime views. Result files bind the selected-case digest, CodeNib
revision, pinned upstream revisions, model, and run options. The scorer retains
missing and failed cells in the requested denominator, verifies recorded case
digests against the active case set, and rejects mixed-model aggregation.
`--allow-incomplete` writes an explicit partial audit; it does not turn a
partial matrix into a complete one.

Both scorers require explicit golden-patch `gold_files` and `gold_functions`
in each case. `score-locagent` reports the common ranked accuracy, precision,
and recall contract at configurable cutoffs; `score-orcaloca` reports
OrcaLoca's unranked File Match and Function Match contract. They intentionally
do not compare the two policies under one metric. A manifest whose persisted
graph predates the current graph schema fails preflight and must be rebuilt;
historical successful cells do not count as current delivery evidence.

The OrcaLoca comparison follows the
[trace-analysis boundary](#trace-analysis-boundary). File Match and Function
Match use golden-patch labels and remain separate from the common ranked
file/symbol metrics. Legacy result cells may be reused, but the summary reports
their missing provenance explicitly.

Provider semantics and model-run cost are separate gates. Provider fixtures and
the pinned upstream probe check the declared tool and helper semantics. The
paired model runner preserves upstream sampling defaults (including OrcaLoca's
`temperature=1.0`) and records one trajectory per cell, so its token and
wall-time ratios are descriptive smoke data, not evidence of a performance
improvement. A performance comparison requires repeated paired trials or an
endpoint with a documented deterministic sampling contract.
