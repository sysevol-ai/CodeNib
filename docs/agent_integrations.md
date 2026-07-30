# Agent Integrations

CodeNib can supply repository search and graph-navigation tools to an external
agent without importing that agent or rebuilding its indexes. Integrations bind
to one existing `repo_manifest.json` through `ServerContext`; they never create
an agent-specific graph, BM25 index, or cache directory.

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
prompt-visible markers. Prose and ambiguous-result tie ordering may differ;
semantic file, class, method, and source ranges are the compatibility
boundary.

Run the local contract and provider tests with:

```bash
pytest -q test/integrations/test_orcaloca.py
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
