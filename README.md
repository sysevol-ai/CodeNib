<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->
<!-- mcp-name: ai.codenib/codenib -->

<div align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_logo.svg" alt="CodeNib" width="560">
  <h1>Searchable codebase wikis and context for coding agents</h1>
  <p>
    Point it at any repo, get a searchable Wiki and an MCP server for your coding agent.
  </p>
  <p>
    <a href="https://arxiv.org/abs/2607.25431"><img src="https://img.shields.io/badge/arXiv-2607.25431-b31b1b.svg?logo=arxiv&amp;logoColor=white" alt="arXiv 2607.25431"></a>
    <a href="https://huggingface.co/papers/2607.25431"><img src="https://img.shields.io/badge/Hugging_Face-%232_Paper_of_the_Day-FFD21E.svg?logo=huggingface&amp;logoColor=black" alt="Hugging Face: #2 Paper of the Day"></a>
  </p>
  <p>
    <a href="#quickstart">Quickstart</a>
    &nbsp;&middot;&nbsp;
    <a href="https://codenib.ai">Website</a>
    &nbsp;&middot;&nbsp;
    <a href="https://discord.gg/ySer6CGn4">Discord</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/">Documentation</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/codegraph/">CodeGraph</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/mcp/">MCP</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/agent_integrations/">Agent Integrations</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/language_capabilities/">Languages</a>
  </p>
  <p>
    <a href="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci-full.yml"><img src="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci-full.yml/badge.svg" alt="CI"></a>
    <a href="https://pypi.org/project/codenib/"><img src="https://img.shields.io/pypi/v/codenib.svg?cacheSeconds=300" alt="PyPI version"></a>
    <a href="https://docs.codenib.ai/codegraph/#one-command-setup"><img src="https://img.shields.io/badge/Claude_Code-D97757?logo=claude&amp;logoColor=fff" alt="Claude Code supported"></a>
    <a href="https://docs.codenib.ai/codegraph/#one-command-setup"><img src="https://img.shields.io/badge/Codex-000?logo=openai&amp;logoColor=fff" alt="Codex supported"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB.svg" alt="Python 3.10+"></a>
    <img src="https://img.shields.io/badge/Release-0.2.2-2563EB.svg" alt="CodeNib 0.2.2">
  </p>
</div>

```bash
python -m pip install "codenib[graph,mcp]==0.2.2"
codenib codegraph init /path/to/your/repo
```

That one command detects the repository languages, installs the pinned
package-level graph providers it can manage, builds BM25 plus a source-linked
symbol graph, and registers the resulting MCP server with installed Codex and
Claude Code clients. It is local, open source, requires no model or cloud, and
does not write configuration or indexes into the target repository.

## News

- **2026-08-21 — CodeNib 0.2.2 repository source authority.** Authenticated
  indexing now admits absolute symlinks only when they resolve inside the same
  pinned checkout, and exact root-relative exclusions persist across indexing,
  CodeGraph, MCP, Wiki, and Web runtimes.
  [Release notes](https://docs.codenib.ai/releases/0.2.2/)
- **2026-08-13 — CodeNib 0.2.1 CodeGraph onboarding.** One command prepares a
  repository graph and connects it to Codex and Claude Code, with idempotent
  status, safe uninstall, and installed-wheel MCP graph verification.
  [CodeGraph guide](https://docs.codenib.ai/codegraph/)
- **2026-08-08 — RepoNavigator Jump adapter.** The published single-tool
  [RepoNavigator](https://arxiv.org/abs/2512.20957v6) contract now resolves
  symbol definitions through a persisted SCIP occurrence or injected LSP
  signal, with graph-only resolution disclosed as a degraded fallback.
  [Support boundary](https://docs.codenib.ai/agent_integrations/#reponavigator)
- **2026-08-05 — CodeNib 0.2.0.** Build a static Wiki and reusable context
  artifact once, then serve it through Pages or the official MCP package.
  Hybrid retrieval and managed SCIP/LSP providers ship in the same CLI.
  [Release notes](https://docs.codenib.ai/releases/0.2.0/)
- **2026-08-05 — SweRank recipe.** Run
  [SweRank](https://github.com/SalesforceAIResearch/SweRank) retrieval and
  reranking over a local checkout. [Example](examples/swerank_retrieve_rerank.py)
- **2026-08-04 — Native repository explorer.** CodeNib's planner now targets
  the [SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench)
  source-region protocol.
  [Validation](docs/evaluation/swe_explore.md)
- **2026-08-03 — Native LocAgent policy.**
  [LocAgent](https://github.com/gersteinlab/LocAgent) runs directly on CodeNib
  views without LocAgent, LiteLLM, or LlamaIndex dependencies.
  [Support matrix](https://docs.codenib.ai/agent_integrations/)
- **2026-08-02 — OrcaLoca SearchAgent.**
  [OrcaLoca](https://github.com/fishmingyu/OrcaLoca)'s six-tool search loop
  now reuses CodeNib's symbol graph.
  [Support matrix](https://docs.codenib.ai/agent_integrations/)

## System Architecture

| Layer | Responsibility |
|---|---|
| View compiler | Chunk source and materialize BM25, dense, graph, and navigation views; reuse current artifacts and atomically rebuild affected views |
| View manifest | Record repository identity, source fingerprint, builder profile, capabilities, status, and artifact location independently for each view |
| Context serving | Execute lexical, semantic, hybrid, reranked, and structural query plans while preserving repository-relative source locations |
| Agent runtime | Expose capability-aware MCP and LSP-shaped tools, assemble bounded evidence, and return citations that agents and humans can inspect |

```text
repository change
  -> reuse current views or rebuild affected views
  -> publish a capability-bearing manifest
  -> plan repository queries
  -> deliver bounded, source-linked context
```

On a later commit, CodeNib reuses views whose source and builder identities are
still current. A requested view affected by source or policy changes currently
rebuilds in an isolated generation; file- and symbol-level delta repair remains
disabled until it can use the same pinned source authority.

## Quickstart

Requires Python 3.10+ and Git. For an agent-ready CodeGraph with no model or API
key, install the graph and MCP extras and run one command:

```bash
python -m pip install "codenib[graph,mcp]==0.2.2"
codenib codegraph init /path/to/repository
```

`codegraph init` detects Codex and Claude Code, installs only the detected
languages' package-managed providers, builds reusable `bm25` and
`symbol_graph` views, and asks each native client CLI to own its configuration.
Run `codenib codegraph status /path/to/repository` to diagnose the complete
path or `codenib codegraph uninstall /path/to/repository` to remove only the
managed client registrations. The index remains reusable.

Repositories can persist exact root-relative subtree exclusions when generated
or vendored content should not be indexed:

```bash
codenib codegraph init /path/to/repository \
  --exclude-dir ios/Pods \
  --exclude-dir generated/api
```

Paths use repository-relative POSIX spelling (`/`) and name exact subtrees,
not globs. Repeat `--exclude-dir` to replace the complete custom exclusion set. Later
`init` and `index` runs reuse that policy from the manifest; use
`--clear-exclude-dirs` to return to the default source surface. CodeNib does not
implicitly treat `.gitignore` as an indexing policy, because tracked and local
ignored source can still be intentional input.

Zoekt supports the default source policy only when its fixed commit tree
exactly matches the authenticated checkout and contains no tracked path that
the default policy excludes. It does not currently support a non-empty custom
exclusion set. A command that explicitly requests the `zoekt` view, including
`--preset full`, fails closed when either condition is unmet; use the
CodeGraph, `fast`, or semantic paths instead.
In 0.2.2 the authenticated MCP `search_zoekt` runtime requires Linux `/proc`
so the child process can inherit the exact retained shard generation; other
platforms fail closed instead of reopening a mutable shard path.

For a browser Wiki with hybrid BM25+dense retrieval, install the semantic extra:

```bash
python -m pip install "codenib[semantic]==0.2.2"
codenib wiki /path/to/repository
```

Both paths keep reusable state under `~/.codenib/repositories` and leave the
target checkout unchanged. Set `CODENIB_HOME` to relocate state. The
[0.2.2 release notes](https://docs.codenib.ai/releases/0.2.2/) record the
upgrade boundary and installed-product evidence.

Preview the CodeGraph operations without installing, indexing, or configuring
an agent:

```bash
codenib codegraph init /path/to/repository --dry-run
```

For a structural view, CodeNib detects the repository languages and manages
only their package-level providers; operating-system and project prerequisites
remain explicit:

```bash
python -m pip install "codenib[graph]==0.2.2"
codenib toolchain install /path/to/repository --scope graph
codenib doctor /path/to/repository --require graph
```

Export that indexed commit as a serverless Wiki when a live Ask backend is not
needed:

```bash
codenib export /path/to/repository --output /tmp/repository-wiki
```

The export contains a versioned provenance manifest, precomputed Wiki pages,
source citations, and available page-level dependency data. It contains no
provider credential; interactive Ask and runtime graph exploration remain on
the local or MCP serving path.

For a repository-hosted Wiki, CodeNib also ships a reusable GitHub workflow
that builds or reuses the same manifest, deploys the static site to Pages,
and uploads the matching commit-addressed context artifact. Its default
`semantic` route builds BM25 and vector views with a cached local Hugging Face
model and needs no API key. An explicit `fast` route avoids the model download;
a BYO OpenAI-compatible endpoint can replace local embedding. Query-time search
remains in the local or MCP runtime. See
[GitHub Pages](https://docs.codenib.ai/github_pages/).
The published BM25/vector artifact can then be verified against an exact local
checkout and served through MCP without rebuilding the repository views.

See the
[Quickstart](https://docs.codenib.ai/quickstart/)
for ports, advanced indexing, and troubleshooting.

<p align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_wiki.png" alt="CodeNib Wiki showing the source-linked reverse proxy guide for Caddy" width="100%">
</p>

## Serve An Agent

The recommended path configures installed Codex and Claude Code clients through
their native CLIs:

```bash
python -m pip install "codenib[graph,mcp]==0.2.2"
codenib codegraph init /path/to/repository
```

Ask the agent to start with `explore_context` for bounded, source-verified
repository context and use `dependency_subgraph` for caller impact or callee
dependencies. The MCP server also exposes ranked BM25, regex, definition,
reference, route, and bounded source-read tools. See the
[CodeGraph guide](https://docs.codenib.ai/codegraph/) for client scopes,
diagnostics, uninstall behavior, and language prerequisites, and the
[MCP Server](https://docs.codenib.ai/mcp/) for the complete tool contract.

The same planner is available directly to Python agents:

```python
from codenib.agent import RepositoryContextExplorer

with RepositoryContextExplorer.from_repository(
    "/path/to/repository", policy="auto"
) as explorer:
    result = explorer.explore("where is request retry behavior implemented?", top_k=10)
```

Each result includes source-validated evidence plus the selected plan,
capabilities, loaded views, fusion, graph, and reranking trace.

## What CodeNib Provides

| Surface | Purpose |
|---|---|
| View compiler | Build independently managed views, reuse current generations, and atomically rebuild changed views under one source identity |
| Agent context runtime | Plan capability-aware retrieval and navigation, then assemble bounded source-linked evidence |
| Retrieval | BM25, dense-vector, regex/trigram, Zoekt, fusion, and reranking paths; see the [validated model matrix](https://docs.codenib.ai/rag_ops/#validated-models) |
| Structural context | SCIP/LSP-backed symbol graphs with source locations and typed edges |
| MCP and LSP-shaped tools | Serve one manifest to coding agents without tying the runtime to one agent framework |
| Agent compatibility | Reuse one manifest across revision-pinned [LocAgent](https://github.com/gersteinlab/LocAgent), [Agentless](https://github.com/OpenAutoCoder/Agentless), [CoSIL](https://github.com/ZhonghaoJiang/CoSIL), and [OrcaLoca](https://github.com/fishmingyu/OrcaLoca) contracts, plus the published [RepoNavigator](https://arxiv.org/abs/2512.20957v6) `jump` contract over SCIP/LSP definition signals; see the [support matrix](https://docs.codenib.ai/agent_integrations/) |
| Benchmark compatibility | Evaluate native exploration against pinned external datasets and scorers, including [SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench); see the [dataset and benchmark matrix](https://docs.codenib.ai/evaluation/) |
| Local inspection | Audit the same context through Wiki pages, Ask answers, citations, and the Dependency Map |
| Evaluation harness | Measure retrieval, navigation, incremental maintenance, and context policies on the same artifacts |

Language support varies by surface. The generated
[capability matrix](https://docs.codenib.ai/language_capabilities/)
records chunking, graph, incremental, and C++ decoder support.

## Documentation

- [Quickstart](https://docs.codenib.ai/quickstart/)
- [GitHub Pages](https://docs.codenib.ai/github_pages/)
- [MCP Server](https://docs.codenib.ai/mcp/)
- [Agent Integrations](https://docs.codenib.ai/agent_integrations/)
- [RAG Models and Planner](https://docs.codenib.ai/rag_ops/)
- [Benchmarks and Evaluation](https://docs.codenib.ai/evaluation/)
- [Web UI](https://docs.codenib.ai/web_demo/)
- [Language Capabilities](https://docs.codenib.ai/language_capabilities/)
- [Concepts and development guides](https://docs.codenib.ai/)

Build the documentation site locally with:

```bash
python -m pip install -e ".[dev]"
mkdocs serve
```

## Development

```bash
git clone https://github.com/sysevol-ai/CodeNib.git
cd CodeNib
make dev
make test
```

The test suite is split into unit, integration, serial integration, core,
graph-consumer, and slow tiers. See
[CI/CD](https://docs.codenib.ai/ci_cd/) before
running the credential- or toolchain-dependent tiers.

## Status

CodeNib `0.2.2` is a beta release. The CLI and manifest format are usable, but
public interfaces may still change before a stable release. Historical
research artifacts retain their published dataset identifiers; the maintained
package, import namespace, commands, and repository use `CodeNib`. See
[Naming](https://docs.codenib.ai/branding/).

## Citation

If you use CodeNib in your research, please cite our
[arXiv paper](https://arxiv.org/abs/2607.25431):

```bibtex
@misc{yu2026codenibmultiviewdataserving,
      title={CodeNib: A Multi-View Data System for Serving Repository Context to Coding Agents},
      author={Zhongming Yu and Hengjia Yu and Boqin Yuan and Shuting Zhao and Yizhao Chen and Aryan Dokania and Mihir Jagtap and Jiayu Chang and Yitong Ma and Yash Jayswal and Wentao Ni and Hejia Zhang and Zhaoling Chen and Gangda Deng and Jishen Zhao},
      year={2026},
      eprint={2607.25431},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2607.25431},
}
```

## Project

[Website](https://codenib.ai)
&nbsp;&middot;&nbsp;
[Changelog](https://github.com/sysevol-ai/CodeNib/blob/main/CHANGELOG.md)
&nbsp;&middot;&nbsp;
[Contributing](https://github.com/sysevol-ai/CodeNib/blob/main/CONTRIBUTING.md)

## License

CodeNib is licensed under the
[Apache License, Version 2.0](https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE).
