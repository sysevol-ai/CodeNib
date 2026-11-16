# Optional contextual BM25 retrieval

Contextual retrieval adds short explanations of functions and methods to BM25
ranking text. It can help match a description of behavior to code whose own
words are different. Enable it explicitly through the existing builder API.
Returned results keep their original code and source locations.

## Usage

Install the existing `agent` extra and configure your chosen chat provider.
This example makes model calls while indexing; set execution and spending
limits before running it.

```python
import os

from codenib.compiler.index_builders import BM25IndexBuilder, IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.index.contextual import ContextualizationConfig, FileContextualizer
from codenib.llm.litellm_chat import LiteLLMChat

chat = LiteLLMChat(model=os.environ["CONTEXTUALIZER_MODEL"])
contextualizer = FileContextualizer(
    chat, ContextualizationConfig(temperature=None)
)
registry = IndexBuilderRegistry()
registry.register("bm25", BM25IndexBuilder(contextualizer=contextualizer))
compiler = IndexCompiler(registry, IndexCompilerConfig(index_types=["bm25"]))
manifest = compiler.compile_repo(
    os.environ["SOURCE_REPOSITORY"],
    cache_dir=os.environ["INDEX_OUTPUT_DIRECTORY"],
)
```

Use an output directory outside the source tree. Check the returned manifest's
build status before using the requested view. Model choice, credentials,
endpoint, timeouts and retries belong to the existing chat client. The example
omits temperature; select a numeric temperature instead when appropriate for
your provider. Ordinary indexing remains available without a contextualizer.

## Scope and tradeoffs

Each explanation uses the complete target chunk and a bounded beginning of its
source file. Large files may lose useful nearby context, and this method does
not resolve cross-file dependencies. Input and prefix limits are characters;
the output limit is a model token request. Adjust them through
`ContextualizationConfig` for the selected model. These limits do not enforce a
total dollar budget.

Generated explanations are labeled ranking metadata, not source evidence.
They can be inaccurate. They add model cost and indexing time, increase index
size, and may require regeneration after source edits. They make no additional
model calls during search. Dense embeddings and graph-conditioned context are
outside this method's scope.

The compiler checks the source and contextualization settings before reusing a
view. Invalid output or provider failure aborts preparation before replacing
an existing index. The contextualizer retains reported response usage through
`usage_tracker`, including responses rejected by prefix validation. Missing
usage is unknown rather than zero; completion counts are logical chunk calls,
not HTTP retry counts. Use a separate contextualizer instance per concurrent
build to keep usage attributable.

## Status and verification

This is an optional community method. A small development pilot showed mixed
retrieval results, so it remains opt-in. Its 76 regression cases cover real
local indexing and persistence, source preservation, configuration and mode
switches, invalid generation, cancellation and source changes. These contracts
do not establish explanation accuracy or better downstream answers.

Owner: Yogya. Review support, narrowing or removal by December 10, 2026, or the
first feature support decision, whichever occurs first.
