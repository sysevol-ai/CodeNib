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
    <a href="#quickstart">Quickstart</a>
    &nbsp;&middot;&nbsp;
    <a href="https://codenib.ai">Website</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/index.md">Documentation</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/mcp.md">MCP</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/language_capabilities.md">Languages</a>
  </p>
  <p>
    <a href="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci.yml"><img src="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
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

Requires Python 3.10+ and Git.

```bash
pip install codenib
codenib wiki /path/to/repository
```

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

See the
[Quickstart](https://github.com/sysevol-ai/CodeNib/blob/main/docs/quickstart.md)
for ports, advanced indexing, and troubleshooting.

<p align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_wiki.png" alt="CodeNib Wiki showing a source-linked overview of the CodeNib repository" width="100%">
</p>

## Serve An Agent

Install the MCP extra, build once, and serve the same repository manifest over
stdio:

```bash
pip install "codenib[mcp]"
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

## What CodeNib Provides

| Surface | Purpose |
|---|---|
| Incremental compiler | Build independently managed views, reuse unchanged content, repair supported transitions, and conservatively rebuild outside those boundaries |
| Agent context runtime | Plan capability-aware retrieval and navigation, then assemble bounded source-linked evidence |
| Retrieval | BM25, dense-vector, regex/trigram, Zoekt, fusion, and reranking paths |
| Structural context | SCIP/LSP-backed symbol graphs with source locations and typed edges |
| MCP and LSP-shaped tools | Serve one manifest to coding agents without tying the runtime to one agent framework |
| Local inspection | Audit the same context through Wiki pages, Ask answers, citations, and the Dependency Map |
| Evaluation harness | Measure retrieval, navigation, incremental maintenance, and context policies on the same artifacts |

Language support varies by surface. The generated
[capability matrix](https://github.com/sysevol-ai/CodeNib/blob/main/docs/language_capabilities.md)
records chunking, graph, incremental, and C++ decoder support.

## Documentation

- [Quickstart](https://github.com/sysevol-ai/CodeNib/blob/main/docs/quickstart.md)
- [MCP Server](https://github.com/sysevol-ai/CodeNib/blob/main/docs/mcp.md)
- [Web UI](https://github.com/sysevol-ai/CodeNib/blob/main/docs/web_demo.md)
- [Language Capabilities](https://github.com/sysevol-ai/CodeNib/blob/main/docs/language_capabilities.md)
- [Architecture and experiments](https://github.com/sysevol-ai/CodeNib/blob/main/docs/index.md)

Build the documentation site locally with:

```bash
pip install "codenib[dev]"
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
[CI/CD](https://github.com/sysevol-ai/CodeNib/blob/main/docs/ci_cd.md) before
running the credential- or toolchain-dependent tiers.

## Status

CodeNib `0.1.0` is a developer preview. The CLI and manifest format are usable,
but public interfaces may still change before a stable release. Historical
research artifacts retain their published dataset identifiers; the maintained
package, import namespace, commands, and repository use `CodeNib`. See
[Naming](https://github.com/sysevol-ai/CodeNib/blob/main/docs/branding.md).

## Project

[Website](https://codenib.ai)
&nbsp;&middot;&nbsp;
[Changelog](https://github.com/sysevol-ai/CodeNib/blob/main/CHANGELOG.md)
&nbsp;&middot;&nbsp;
[Contributing](https://github.com/sysevol-ai/CodeNib/blob/main/CONTRIBUTING.md)
&nbsp;&middot;&nbsp;
[Citation](https://github.com/sysevol-ai/CodeNib/blob/main/CITATION.cff)

## License

CodeNib is licensed under the
[Apache License, Version 2.0](https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE).
