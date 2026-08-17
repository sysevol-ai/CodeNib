<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# One Repository Index for Humans and Agents

_Running a source-linked Wiki, CodeGraph, and MCP context locally on NVIDIA
DGX Spark._

**Published August 17, 2026**

Most code RAG systems end with chunks in a vector database and one chatbot.
That is useful, but it leaves every other consumer to build another partial
view of the same repository. A developer browses one representation, an agent
searches another, and a graph tool quietly works from a third snapshot. Their
answers may look plausible even after the checkout has changed.

We built CodeNib around a different unit: a verified repository artifact.
CodeNib compiles a checkout into independently managed retrieval, source, and
structural views, publishes their capabilities through one manifest, and
serves that same artifact to both people and agents.

The result is one local code-intelligence layer with several consumers:

- a searchable, source-linked Wiki for people;
- BM25, dense, regex/trigram, Zoekt, fusion, and reranking paths;
- definitions, references, callers, callees, and dependency neighborhoods;
- an MCP server for Codex, Claude Code, and generic MCP clients; and
- research and evaluation adapters such as LocAgent, RepoNavigator, OrcaLoca,
  and SWE-Explore.

The repository compiler and CodeGraph path do not require an LLM. Generation
is an optional consumer of the compiled context, so the same artifact can be
used with a local model, a hosted model, or no model at all.

## The Artifact Is the Product Boundary

The architecture is deliberately split between compilation and serving:

```text
repository@commit
        |
        v
incremental repository compiler
        |
        +-- BM25
        +-- dense vectors
        +-- source chunks
        +-- symbol graph and SCIP/LSP navigation
        +-- optional Zoekt and reranking views
        |
        v
verified, capability-bearing manifest
        |
        +-- source-linked Wiki
        +-- local Ask
        +-- CodeGraph and Dependency Map
        +-- MCP clients and coding agents
```

Each view has its own builder configuration, state, and artifact location.
Adding a dense view does not require rebuilding a current graph. A compatible
source change can reuse unchanged vector content or repair a supported graph
transition. If CodeNib cannot verify an incremental transition, it rebuilds
instead of publishing a partially updated view.

The manifest binds those views to repository identity, commit, filtered source
fingerprint, and artifact hashes. The Wiki and MCP server recheck the live
checkout before exposing source. If the source no longer matches, CodeNib
refuses to silently present the old index as current context.

That fail-closed behavior matters more than the choice of model. A stronger
model cannot repair evidence retrieved from the wrong revision.

## A Fully Local Reference Deployment

We run one end-to-end local profile on an NVIDIA DGX Spark. This is a reference
deployment, not a hardware requirement: BM25 and CodeGraph are model-free, and
smaller models or hosted OpenAI-compatible endpoints can consume the same
manifest.

| Layer | Local reference profile |
| --- | --- |
| Hardware | NVIDIA DGX Spark, GB10, 128 GB unified memory, Arm64 |
| Generation | `Qwen/Qwen3.6-35B-A3B-FP8` |
| Embeddings | `Qwen/Qwen3-Embedding-0.6B` |
| Model runtime | Two loopback-only vLLM OpenAI-compatible endpoints |
| Repository runtime | CodeNib 0.2.1 Wiki, CodeGraph, and MCP |
| Generation endpoint | `http://127.0.0.1:8080/v1` |
| Embedding endpoint | `http://127.0.0.1:8081/v1` |

DGX Spark provides a GB10 and 128 GB of coherent unified system memory; the
current hardware specification is available in the
[NVIDIA DGX Spark documentation](https://docs.nvidia.com/dgx/dgx-spark/hardware.html).
Qwen3.6-35B-A3B has 35 billion total parameters with 3 billion activated, and
its FP8 release documents vLLM and SGLang serving in the
[official model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B-FP8).

In this profile, repository source, retrieval, embeddings, generation, the web
application, and MCP all stay on the same machine. The model servers bind to
loopback because neither endpoint needs to be exposed to the network.

## Start the Model Endpoints

Qwen recommends a current vLLM release for Qwen3.6. We use a text-only,
single-GPU endpoint and reduce the maximum context from the model's native
limit to reserve memory for CodeNib, embeddings, and KV cache on the same
machine:

```bash
vllm serve Qwen/Qwen3.6-35B-A3B-FP8 \
  --host 127.0.0.1 \
  --port 8080 \
  --served-model-name qwen3.6-35b \
  --max-model-len 65536 \
  --language-model-only \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":1}'
```

The MTP option is an optimization, not a CodeNib requirement. Remove
`--speculative-config` when establishing a baseline or when using a vLLM build
without the corresponding Qwen MTP support.

Run embeddings on a second local endpoint:

```bash
vllm serve Qwen/Qwen3-Embedding-0.6B \
  --host 127.0.0.1 \
  --port 8081 \
  --runner pooling \
  --gpu-memory-utilization 0.08
```

Confirm both OpenAI-compatible services before starting CodeNib:

```bash
curl -s http://127.0.0.1:8080/v1/models | jq .
curl -s http://127.0.0.1:8081/v1/models | jq .
```

Runtime releases, kernels, model revisions, context length, and memory
utilization should be pinned for a reproducible benchmark. The commands above
show the deployment shape; they are not a universal performance preset for
every vLLM release or model revision.

## Compile the Repository Once

Install the graph, MCP, model-backed Wiki, and remote-compatible embedding
clients. The embedding service is local, but CodeNib uses its OpenAI-compatible
protocol, hence the `semantic-remote` extra:

```bash
python -m pip install \
  "codenib[agent,graph,mcp,semantic-remote]==0.2.1"
```

First build the model-free CodeGraph path. It detects supported languages,
builds BM25 plus a source-linked symbol graph, and registers the local MCP
server with installed Codex and Claude Code clients:

```bash
export REPOSITORY=/absolute/path/to/repository
codenib codegraph init "$REPOSITORY"
codenib codegraph status "$REPOSITORY"
```

Then add the dense view and launch the generated Wiki against the two local
model endpoints. vLLM does not require a key when bound locally, but the
OpenAI-compatible clients expect a non-empty credential name, so this example
uses a non-secret placeholder:

```bash
export CODENIB_LOCAL_API_KEY=local-only

codenib wiki "$REPOSITORY" \
  --preset semantic \
  --generate \
  --model openai/qwen3.6-35b \
  --api-base http://127.0.0.1:8080/v1 \
  --api-key-env CODENIB_LOCAL_API_KEY \
  --embedding-provider openai \
  --embedding-model Qwen/Qwen3-Embedding-0.6B \
  --embedding-dimension 1024 \
  --embedding-endpoint http://127.0.0.1:8081/v1 \
  --embedding-api-key-env CODENIB_LOCAL_API_KEY
```

The semantic update preserves the independently current graph view produced by
`codegraph init`. Subsequent runs reuse current artifacts and update affected
views when the source changes.

For a generic MCP client, the same repository is served over stdio:

```json
{
  "mcpServers": {
    "codenib": {
      "command": "codenib",
      "args": ["mcp", "/absolute/path/to/repository"]
    }
  }
}
```

No inference endpoint is required for MCP retrieval. Tools such as
`explore_context`, `dependency_subgraph`, `search_bm25`, `search_regex`,
`lsp_definition`, `lsp_references`, and `read_source` operate on the verified
manifest and checkout.

## One Question, Three Surfaces

Consider this repository question:

> How does CodeNib prevent an index built for one commit from being used after
> the repository changes?

In the Wiki, the answer is prose with clickable source paths and line ranges.
Following a citation opens the exact code span beside the explanation.

In CodeGraph, the same investigation can start from the verification symbols
and expand toward callers, callees, references, and dependency neighborhoods.

Through MCP, an agent can begin with `explore_context`, inspect the returned
source identity and verified ranges, and call `read_source` only for the exact
windows it needs. It does not need to regenerate a private vector index for
every session or trust prose detached from the checkout.

All three surfaces consume the same manifest. They differ in presentation and
query plan, not in repository identity.

## Where the Waiting Time Goes

"Local" does not mean every first request is instantaneous. There are three
different cold paths that should not be collapsed into one loading spinner:

1. **Repository authorization and artifact loading.** CodeNib recaptures the
   filtered source identity and opens the selected views.
2. **Query retrieval.** Warm BM25, dense, and graph queries are normally much
   cheaper than generation.
3. **Uncached prose generation.** A first Wiki page or Ask answer may require a
   model call; a source-linked cached page does not.

Our August 2026 demo acceptance smoke covered 26 repositories. Warm dense
queries took 11-25 ms. After a backend restart, already generated Overview
pages were served in roughly 3-4 ms on their first cache read and around 0.7 ms
warm. Repository authorization and full-view loading ranged from about 0.5
seconds for Requests and bat to 16.2 seconds for Babel. Three fixed local
Qwen3.6 Ask cases completed in 19.6-23.5 seconds end to end.

Those numbers are operational smoke evidence from one host, not a general
model leaderboard. The sample of generation questions is too small for a
quality or throughput claim. It does establish an important diagnostic split:
an old 17-second bat page wait was not a 17-second vector query. Most of that
path was uncached page generation and application orchestration; the repository
views loaded in about 0.6 seconds.

For published performance comparisons, we will report model revision,
runtime, context length, TTFT, output tokens per second, cold index time,
incremental update time, cache state, and end-to-end latency separately.

## Local and Hosted Are Deployment Profiles

CodeNib does not force the inference backend. Our public hosted demo currently
uses DeepSeek for user-facing generation to keep interactive latency more
predictable, while retaining the same source-bound retrieval architecture. The
recorded DGX Spark profile uses Qwen3.6 and local embeddings with no hosted
generation call.

This split is intentional. Earlier malformed Wiki pages were not caused by an
OpenAI/DeepSeek request incompatibility. They came from stale or unauthorized
vector fallback, cold outline and page generation, and internal diagnostic
prose reaching the reader. The repaired path bounds model calls, rejects
invalid candidates, caches source-linked pages, and keeps degraded generation
states explicit.

## What Comes Next

The next compatibility targets are current llama.cpp stdio MCP clients,
Ollama, and OpenCode. They do not need another repository index: the useful
test is whether each client can consume the existing MCP surface reliably and
preserve its source citations.

CodeNib also includes an experimental retrieval-augmented speculative serving
runtime with an OpenAI-compatible endpoint. We treat that as a separate
acceleration track. The deployment above uses vLLM as the stable model-serving
layer; speculative serving will be promoted only with reproducible wall-clock
comparisons and clearly stated request limitations.

The product principle remains simple:

> Index a codebase once. Explore it as a Wiki, search it as context, and serve
> it to any coding agent.

Start with the [CodeGraph guide](../codegraph.md), connect an agent through the
[MCP guide](../mcp.md), or inspect the
[local and hosted deployment profiles](../running-locally.md). CodeNib is
Apache-2.0 licensed at
[github.com/sysevol-ai/CodeNib](https://github.com/sysevol-ai/CodeNib).
