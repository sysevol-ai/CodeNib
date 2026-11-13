# Optional file-contextual retrieval

Owner: Yogya. Status: experimental BM25 implementation, not a promoted method.
Started November 12, 2026. Review by December 10, 2026, or the first feature
support decision, whichever occurs first. Record support, narrowing, an explicit
extension, or removal at that review.

The prototype adds short explanations to BM25 ranking text while preserving
ordinary source excerpts and source locations. It uses the existing builder,
source-binding, tokenizer, file artifacts, and compiler compatibility checks.
In a [ten-question CodeNib development pilot](contextual_retrieval_pilot.md),
generated context found the expected file in the top five on 4/10 questions,
versus 3/10 with ordinary BM25 and 4/10 with deterministic context. Ordinary
and generated context both covered 9/10 under the same source-token budget.
Dense integration and an architecture research contribution remain open.

## Current scope and boundaries

`FileContextualizer` accepts a `LiteLLMChat`-compatible client. Model selection,
credentials, endpoint and execution options stay with that client. A frozen
`ContextualizationConfig` controls the input character budget, output token
request, prefix character limit, temperature and versioned prompt. Use
`temperature=None` when a provider requires omitting that option. Generation runs only at
index time; the search path does not invoke the contextualizer.

The initial `file-prefix-and-target-v1` policy includes a complete function or
method chunk and as much authenticated file-prefix context as the configured
input budget permits. It does not promise full-file context for large files.
The receipt records source/input/output digests and file-prefix truncation.
An oversized complete target fails explicitly rather than being silently cut.
No graph dependencies are included in this version.

The generated prefix is not source evidence. It is retained as labeled ranking
metadata and tokenized by the BM25 retriever without modifying ordinary document
text. This also prevents persisted-only identifier scans from treating generated
words as code. Returned code is still read from the existing source boundary.
Invalid or unavailable generation aborts before
the prepared index is persisted. The existing persistence implementation's
other guarantees and limitations are unchanged.

## Programmatic entry point

This example makes model calls when executed. Install the existing `agent`
extra, explicitly select a model/provider, and set execution/spending limits
for the experiment before running it. No model is selected by the feature.

```python
import os

from codenib.compiler.index_builders import BM25IndexBuilder, IndexBuilderRegistry
from codenib.compiler.index_compiler import IndexCompiler, IndexCompilerConfig
from codenib.index.contextual import ContextualizationConfig, FileContextualizer
from codenib.llm.litellm_chat import LiteLLMChat

chat = LiteLLMChat(model=os.environ["CONTEXTUALIZER_MODEL"])
contextualizer = FileContextualizer(chat, ContextualizationConfig())
registry = IndexBuilderRegistry()
registry.register("bm25", BM25IndexBuilder(contextualizer=contextualizer))
compiler = IndexCompiler(registry, IndexCompilerConfig(index_types=["bm25"]))
manifest = compiler.compile_repo(
    os.environ["SOURCE_REPOSITORY"],
    cache_dir=os.environ["INDEX_OUTPUT_DIRECTORY"],
)
```

Use an output directory outside the selected source tree, or the existing
explicit source-exclusion configuration for an in-repository compiler cache.
`manifest` retains build status; callers must check that the requested view is
fresh before claiming success. `contextualizer.usage_tracker` retains reported
response usage even if a later prefix-validation failure aborts the build.
No response means usage/cost may be unknown; token limits alone do not enforce
a total dollar budget. `completion_calls` counts logical chunk completions,
not HTTP attempts or retries. Use separate contextualizer instances for concurrent
builds so their response usage remains attributable.

## Decisions and rationale

| Decision | Reason | Cost / revisit condition |
| --- | --- | --- |
| BM25 before dense | Validate the representation and source boundary without loading an embedding model. | Sparse gains may not transfer to dense; measure separately. |
| Existing builder API first | A working consumer without adding CLI flags or a second configuration framework. | Add a CLI surface only after the profile contract and evaluation are stable. |
| Full target plus bounded file prefix | A deterministic, language-independent starting policy with explicit input bounds. | Can miss useful nearby or cross-file definitions; compare better selection later. |
| No generated text in returned code | Preserve exact source evidence and isolate ranking changes. | Showing labeled explanations to an agent is a separate experiment. |
| Explicit ordinary/contextual identities | The compiler compares expected identity fields, so an absent ordinary-profile marker can accidentally accept an enriched view. | Both mode switches and legacy ordinary compatibility need coverage. |
| Contextual schema 11; ordinary schema 10 | Older ordinary builders expect schema 10 and should rebuild a contextual generation. | Raw BM25 file loading remains an explicit operation, not a profile-compatibility check. |
| Conservative whole-source freshness | Avoid bypassing the existing source-authority restriction on incremental builds. | More regeneration after edits; invalidation optimization requires its own evidence. |

## Completion gates

- M0 completed: scoped design, source/compatibility decisions and baseline checks.
- M1 completed locally: contextual BM25 with contract tests and real-provider
  replay; broader supported-environment verification and review remain open.
- M2 deferred: dense encoding using the same representations and unchanged
  canonical returned chunks, subject to evidence of workload benefit.
- M3 completed: development comparison against current and deterministic-context
  baselines, with failures, usage and source/token budgets retained.
- M4 open: held-out comparison and a support/narrowing/stop decision.

Mocked generation tests establish plumbing and invariants, not better retrieval.
The current macOS environment also exposes existing descriptor-path limitations
in vector/Zoekt/compiler tests; retain upstream comparison evidence and validate
those surfaces in a supported environment before calling broader checks green.

## Verification surfaces

New behavior lives in `test/index/test_contextual_bm25.py`, including the real
local retrieval/persistence path and compiler mode-switch reuse. Existing
compiler, manifest, source-selection and sparse tests remain regressions.
Use the marker tiers in `test/CLAUDE.md`; do not run shared-repository mutation
tests in the parallel integration tier. Live provider/model checks belong to
`slow` and are explicitly selected after model and budget configuration.

## Verification status: November 13, 2026

The focused feature/harness/chunker/sparse/manifest/compiler run passes 261
cases, including 76 feature and 13 pilot-harness cases, with two platform
skips. The existing serial graph-backed BM25 suite passes all four cases.
Contract tests use recording chat fakes and real local indexes. The separate
paid pilot records 1,415 real responses replayed through the production builder;
its small file-level result does not establish function or answer correctness.

```bash
pytest test/index/test_contextual_bm25.py test/scripts/test_benchmark_contextual_retrieval.py test/chunker/test_repo_chunking_config.py test/index/sparse_idx test/compiler/test_manifest.py test/compiler/test_artifact_fingerprints.py test/compiler/test_index_compiler.py::TestBM25IndexBuilder -m "not slow and not integration and not integration_serial and not integration_serial_consumer" -q --tb=short
pytest test/index/sparse_idx/test_bm25_index.py -q --tb=short
mkdocs build --strict
python scripts/check_public_docs.py
python scripts/support_matrix.py --check
uv lock --check
```

Strict documentation, public-document boundaries, support-matrix validation,
the dependency lock, and changed-file pinned pre-commit hooks pass. The
all-files hook run exposes existing trailing whitespace in
`web/app/globals.css`; that unrelated edit is excluded from this change.

The broader matched compiler/action/source cohort has 251 passes, 47 failures
and two skips on both this implementation and untouched upstream commit
`26be3c4819b20280c2098e38d0326f13970f6c22`, with identical failing case identities.
The full unit attempt stops at the same existing publish-action path-resolution
failure found in the control. Existing vector/Zoekt descriptor-path limitations
account for many broader macOS failures. Full supported-environment/Linux
verification remains open. The real-model development pilot is complete;
held-out retrieval and downstream validation remain open.
