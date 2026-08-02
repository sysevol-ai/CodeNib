# Agent Integrations

CodeNib can supply repository search and graph-navigation tools to an external
agent without importing that agent or rebuilding its indexes. Integrations bind
to one existing `repo_manifest.json` through `ServerContext`; they never create
an agent-specific graph, BM25 index, or cache directory.

## Support Matrix

The three support levels below are intentionally separate. **Provider** means
CodeNib implements the repository-call contract. **Policy** means the pinned
agent loop can execute with that provider. **Paired** means the repository
provider can be swapped between native and CodeNib under one fixed case, model,
prompt, and budget contract; it is not a general quality claim.

| Integration | Provider | Policy | Paired evaluation | Upstream domain | Boundary |
| --- | --- | --- | --- | --- | --- |
| LocAgent | Pinned three-tool contract | Pinned prompts and function-calling loop | Fixed-case runner | Python SWE-bench repositories; symbol graph + BM25 | Reference and type-use are disclosed as conservative relation mappings |
| Historical OpenHands LocAgent plugin | Python bindings for the pinned plugin revision | Not present in current OpenHands CLI | Covered through the LocAgent contract | Same Python contract as LocAgent | No claim of compatibility with every OpenHands revision |
| OrcaLoca | Pinned six-tool and private-hook contract | Upstream `SearchAgent` with an injected manager | Fixed-case runner + native File/Function Match scorer | Python SWE-bench repositories; symbol graph | Empty `TraceAnalysisOutput`; TraceAnalysis generation is out of scope |
| SWE-Explore | No adapter | No | No | N/A | Referenced only to distinguish its trajectory-read labels from golden-patch localization metrics |

Optional upstream packages are required only for policy execution. Provider
imports and standalone provider examples remain dependency-free. Exact pinned
revisions, fidelity limits, commands, and measured evidence follow below.
CodeNib's graph providers can represent additional languages, but this matrix
does not extend a Python-scoped upstream policy or native comparator beyond its
validated domain.

## LocAgent And OpenHands

The LocAgent provider implements the three repository functions used by
LocAgent and the historical OpenHands integration:

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
plugin. This follows the separation in
[OpenHands PR #7371](https://github.com/OpenHands/OpenHands/pull/7371): the
agent owns function schemas and action conversion, while the runtime supplies
the repository functions. CodeNib dispatches calls directly and does not copy
the historical IPython string-evaluation bridge.

For the historical OpenHands runtime plugin, the complete provider-binding
change is:

```python
import os

from codenib.integrations.locagent import LocAgentToolProvider

_provider = LocAgentToolProvider.from_manifest(os.environ["CODENIB_MANIFEST"])
get_entity_contents = _provider.get_entity_contents
search_code_snippets = _provider.search_code_snippets
explore_tree_structure = _provider.explore_tree_structure
```

The agent-side schemas and action loop remain unchanged. Modern runtimes can
use `provider.dispatch(name, arguments)` directly instead of constructing
Python source from model-supplied arguments.

Compatibility is revision-scoped:

| Contract | Pinned revision |
| --- | --- |
| LocAgent behavior | `ef170542a5cca88a1bd8463335ec43de222ed5f9` |
| OpenHands integration | `efe287ce3402706a171b3a5fb40f15914e98ef20` |

Current OpenHands CLI does not contain that historical LocAgent module. The pin
describes the supported tool contract; it is not a claim that every OpenHands
CLI revision exposes LocAgent.

Run the optional source-level probe against local upstream checkouts with:

```bash
LOCAGENT_CHECKOUT=/path/to/LocAgent \
OPENHANDS_CHECKOUT=/path/to/OpenHands \
  pytest -q test/integrations/test_locagent_upstream.py
```

The probe reads files directly from the pinned Git objects and does not import
either project. To advance compatibility, update the two revision constants,
refresh the YAML fixture from the reviewed upstream schemas, and run both this
probe and `test/integrations/test_locagent.py` in the same change.

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
- Loading the provider never invokes LocAgent, OpenHands ACI, NetworkX,
  LlamaIndex, or an index builder.

### Shared Benchmark Interface

`LocAgentAgent` retains LocAgent's pinned prompts, function schemas, and
reasoning loop while replacing its repository data plane with
`LocAgentToolProvider`. It implements the same
`BaselineTask -> locate_code() -> BaselineRunResult` contract as the other
localization baselines:

```python
from codenib.clients.locagent_agent import LocAgentAgent

agent = LocAgentAgent(
    model="openai-compatible-model",
    locagent_checkout="/path/to/LocAgent",
)
result = await agent.locate_code(
    query_text=issue,
    repo_path=checkout,
    context={"package_name": "project"},
)
```

The adapter extracts the upstream protocol lazily in an isolated subprocess
and resolves final file, class, function, and 1-based line records through
CodeNib's language-neutral graph. The main process never imports LocAgent or
LiteLLM; `--locagent-python` can select a dedicated upstream environment.
OpenAI is loaded only when the policy executes, and `--base-url` accepts an
OpenAI-compatible endpoint such as a LiteLLM proxy.

The common runner reports the same ranked file/symbol accuracy, precision, and
recall for every policy. LocAgent's additional native nDCG and MAP reports
remain artifact-specific; they do not silently replace the shared metric
contract.

```bash
python examples/locagent_loc_agent.py \
  --dataset codenib_base \
  --model "$LOCAGENT_MODEL" \
  --locagent-checkout /path/to/LocAgent \
  --result-path results/locagent.jsonl
```

Run the provider and common-runner tests with:

```bash
pytest -q test/integrations/test_locagent.py \
  test/eval/test_locagent_benchmark_adapter.py
```

## OrcaLoca

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

Compatibility is pinned to OrcaLoca revision
`37db289be2dc3b7432183fe08b3f06becce87c27`. The adapter preserves its
repository-relative `file::Class::method` identities, 1-based locations, and
prompt-visible markers. CodeNib's graph supplies identity and relations; a
lazy, non-executing parse of the manifest-bound Python source restores the
pinned policy's symbol kinds, signatures, docstrings, skeletons, and source
ranges. Python symbols that the pinned visitor does not recognize are not
exposed merely because CodeNib's graph is richer. Prose and ambiguous-result
tie ordering may differ outside this semantic output boundary.

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

The base `codenib` package therefore gains no LocAgent, OpenHands, OrcaLoca,
LlamaIndex, pandas, or NetworkX dependency.

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

Before any model call, the driver checks every requested checkout commit,
tracked-file state, manifest commit, and required capability. Result files bind
the selected-case digest, CodeNib revision, pinned upstream revisions, model,
and run options. The scorer retains missing and failed cells in the requested
denominator, verifies recorded case digests against the active case set, and
rejects mixed-model aggregation. `--allow-incomplete` writes an explicit
partial audit; it does not turn a partial matrix into a complete one.

The OrcaLoca comparison covers its search policy with an empty
`TraceAnalysisOutput`; TraceAnalysis generation is outside this compatibility
experiment. File Match and Function Match use golden-patch labels and remain
separate from the common ranked file/symbol metrics. Legacy result cells may be
reused, but the summary reports their missing provenance explicitly.
