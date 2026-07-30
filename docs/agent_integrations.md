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

The base `codenib` package therefore gains no LocAgent or OpenHands dependency.
