<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

<div align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_logo.svg" alt="CodeNib" width="560">
  <h1>A Multi-View Data System for Serving Repository Context to Coding Agents</h1>
  <p>
    Incremental compilation, explicit per-view manifests, and agent-native context serving.
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
    <a href="https://docs.codenib.ai/mcp/">MCP</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/agent_integrations/">Agent Integrations</a>
    &nbsp;&middot;&nbsp;
    <a href="https://docs.codenib.ai/language_capabilities/">Languages</a>
  </p>
  <p>
    <a href="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci.yml"><img src="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="https://pypi.org/project/codenib/"><img src="https://img.shields.io/pypi/v/codenib.svg" alt="PyPI version"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB.svg" alt="Python 3.10+"></a>
    <img src="https://img.shields.io/badge/Release-Developer_Preview-EA580C.svg" alt="Developer Preview">
  </p>
</div>

CodeNib is a multi-view data system for serving repository context to coding
agents. Its native runtime compiles a checkout into manifest-linked lexical,
semantic, structural, and static-navigation views, incrementally maintains
supported transitions, and serves bounded source evidence through MCP,
LSP-shaped providers, Python, and HTTP APIs.

The Wiki, Ask view, and Dependency Map are inspection clients of that same
runtime, not the system boundary. The core implementation lives in CodeNib;
optional model endpoints and language servers are providers rather than a host
agent or code-Wiki framework.

## News

- **2026-08-05 — CodeNib 0.2.0 Alpha 2.** Hybrid retrieval is now the default,
  and `codenib toolchain` manages pinned per-language SCIP/LSP providers.
  [Release notes](https://docs.codenib.ai/releases/0.2.0/)
- **2026-08-05 — CodeNib 0.2.0 Alpha 1.** `codenib publish` builds a static
  Wiki and reusable context artifact for Pages or MCP.
  [Pages guide](https://docs.codenib.ai/github_pages/)
- **2026-08-04 — Native repository explorer.** CodeNib's planner now targets
  the SWE-Explore source-region protocol.
  [Validation](docs/evaluation/swe_explore.md)
- **2026-08-03 — Native LocAgent policy.** LocAgent runs directly on CodeNib
  views without LocAgent, LiteLLM, or LlamaIndex dependencies.
  [Support matrix](https://docs.codenib.ai/agent_integrations/)
- **2026-08-02 — OrcaLoca SearchAgent.** Its six-tool search loop now reuses
  CodeNib's symbol graph.
  [Support matrix](https://docs.codenib.ai/agent_integrations/)

## System Architecture

| Layer | Responsibility |
|---|---|
| Incremental compiler | Chunk source and materialize BM25, dense, graph, and navigation views; reuse or repair supported artifacts and rebuild when an update cannot be admitted |
| View manifest | Record repository identity, source fingerprint, builder profile, capabilities, status, and artifact location independently for each view |
| Context serving | Execute lexical, semantic, hybrid, reranked, and structural query plans while preserving repository-relative source locations |
| Agent runtime | Expose capability-aware MCP and LSP-shaped tools, assemble bounded evidence, and return citations that agents and humans can inspect |

```text
repository change
  -> materialize or repair affected views
  -> publish a capability-bearing manifest
  -> plan repository queries
  -> deliver bounded, source-linked context
```

On a later commit, CodeNib can reuse unchanged vector content and patch
supported graph transitions at file or symbol granularity. Unsupported,
inconsistent, or unverified transitions fall back to a fresh build instead of
publishing a partially updated view.

## Quickstart

Requires Python 3.10+ and Git. The recommended local path includes the pinned
CodeRankEmbed model and serves hybrid BM25+dense retrieval:

```bash
python -m pip install "codenib[semantic]==0.2.0a2"
codenib doctor --require core --require wiki
codenib wiki /path/to/repository
```

`codenib wiki` selects the semantic route because the installed environment
contains its dependencies. A smaller `python -m pip install codenib==0.2.0a2`
installation selects the deterministic BM25 fallback and downloads no model.
The [0.2 release notes](https://docs.codenib.ai/releases/0.2.0/) record the
upgrade boundary and verification evidence.

CodeNib detects the repository languages, builds a reusable index under
`~/.codenib/repositories`, launches the local Wiki, and opens
[http://localhost:3000](http://localhost:3000). The wheel includes the
production Wiki frontend, so normal use does not require Node.js or npm and
the target repository stays untouched. This command exercises the same compiler
and serving runtime used by agents. Set `CODENIB_HOME` to relocate state.

Check the environment or index without opening the Wiki:

```bash
codenib doctor --require core --require wiki
codenib index /path/to/repository
```

For a structural view, CodeNib detects the repository languages and manages
only their package-level providers; operating-system and project prerequisites
remain explicit:

```bash
python -m pip install "codenib[graph]==0.2.0a2"
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
that incrementally builds the same manifest, deploys the static site to Pages,
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

Install the MCP extra, build once, and serve the same repository manifest over
stdio:

```bash
python -m pip install "codenib[mcp,semantic]==0.2.0a2"
codenib index /path/to/repository
codenib mcp /path/to/repository
```

The MCP server advertises its full tool set and uses the compiled manifest to
decide which calls have a fresh backing view. An agent can therefore reuse
available repository work instead of rebuilding context through unbounded
`grep` and `read` loops, while unavailable searches fail explicitly. BM25,
semantic, regex, Zoekt, dependency, and static-navigation results retain source
locations for follow-up reads and citations. See
[MCP Server](https://docs.codenib.ai/mcp/)
for client configuration and tool contracts.

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
| Incremental compiler | Build independently managed views, reuse unchanged content, repair supported transitions, and conservatively rebuild outside those boundaries |
| Agent context runtime | Plan capability-aware retrieval and navigation, then assemble bounded source-linked evidence |
| Retrieval | BM25, dense-vector, regex/trigram, Zoekt, fusion, and reranking paths |
| Structural context | SCIP/LSP-backed symbol graphs with source locations and typed edges |
| MCP and LSP-shaped tools | Serve one manifest to coding agents without tying the runtime to one agent framework |
| Agent compatibility | Reuse one manifest across revision-pinned LocAgent, Agentless v1.5.0, CoSIL, and OrcaLoca SearchAgent contracts; see the [support matrix](https://docs.codenib.ai/agent_integrations/) |
| Benchmark compatibility | Evaluate CodeNib's native exploration against pinned external datasets and scorers, including [SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench) |
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

CodeNib `0.1.0` is a developer preview. The CLI and manifest format are usable,
but public interfaces may still change before a stable release. Historical
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
