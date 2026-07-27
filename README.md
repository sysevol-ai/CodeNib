<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

<div align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_logo.svg" alt="CodeNib" width="560">
  <h1>Repository context, ready for agents and humans</h1>
  <p>
    Build a source-linked Wiki and reusable search indexes from any local repository.
  </p>
  <p>
    <a href="#quickstart">Quickstart</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/index.md">Documentation</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/mcp.md">MCP</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/docs/language_capabilities.md">Languages</a>
    &nbsp;&middot;&nbsp;
    <a href="https://github.com/sysevol-ai/CodeNib">GitHub</a>
  </p>
  <p>
    <a href="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci.yml"><img src="https://github.com/sysevol-ai/CodeNib/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0"></a>
    <a href="https://github.com/sysevol-ai/CodeNib/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB.svg" alt="Python 3.10+"></a>
    <img src="https://img.shields.io/badge/Release-Developer_Preview-EA580C.svg" alt="Developer Preview">
  </p>
</div>

CodeNib compiles a repository into aligned lexical, semantic, and structural
views, then serves source-linked context through a local Wiki, a Python API,
and Model Context Protocol (MCP) tools. The default installation stays small:
it can build a deterministic Wiki with BM25 search and no API key.

## Quickstart

Requires Python 3.10+ and Git.

```bash
pip install codenib
codenib wiki /path/to/repository
```

CodeNib detects the repository languages, builds a reusable index under
`<repo>/.codenib_cache`, launches the local Wiki, and opens
[http://localhost:3000](http://localhost:3000). The wheel includes the
production Wiki frontend, so normal use does not require Node.js or npm.

Check the environment or index without opening the Wiki:

```bash
codenib doctor --require core --require wiki
codenib index /path/to/repository
```

See the
[Quickstart](https://github.com/sysevol-ai/CodeNib/blob/main/docs/quickstart.md)
for ports, reusable manifests, presets, and troubleshooting.

<p align="center">
  <img src="https://raw.githubusercontent.com/sysevol-ai/CodeNib/main/assets/codenib_wiki.png" alt="CodeNib Wiki showing a source-linked overview of the CodeNib repository" width="100%">
</p>

## Choose A Profile

| Profile | Install | Views | Best for |
|---|---|---|---|
| `fast` (default) | `pip install codenib` | BM25 | A quick local Wiki with no model download |
| `semantic` | `pip install "codenib[semantic]"` | BM25 + dense vectors | Natural-language repository search |
| `full` | `pip install "codenib[full]"` | BM25 + vectors + symbol graph + Zoekt | Advanced source and graph workflows |

Select a profile when indexing or launching:

```bash
codenib wiki /path/to/repository --preset semantic
```

The `full` profile also needs the relevant SCIP/LSP and Zoekt binaries. Backend
availability differs by language; consult the
[Language Capabilities](https://github.com/sysevol-ai/CodeNib/blob/main/docs/language_capabilities.md)
matrix and
[SCIP setup](https://github.com/sysevol-ai/CodeNib/blob/main/docs/scip_index.md).

## Connect An Agent

Install the MCP extra, build once, and serve the same repository manifest over
stdio:

```bash
pip install "codenib[mcp]"
codenib index /path/to/repository
codenib mcp /path/to/repository
```

The MCP server exposes BM25, semantic, regex, Zoekt, dependency, and static
navigation tools when their backing views are available. See
[MCP Server](https://github.com/sysevol-ai/CodeNib/blob/main/docs/mcp.md)
for client configuration and tool contracts.

## What CodeNib Provides

| Surface | Purpose |
|---|---|
| Local Wiki | Browse deterministic, source-linked repository pages; optionally enable LLM-authored conceptual pages |
| Index compiler | Build and update a manifest of independently managed repository views |
| Retrieval | BM25, dense-vector, regex/trigram, Zoekt, fusion, and reranking paths |
| Structural context | SCIP/LSP-backed symbol graphs with source locations and typed edges |
| MCP server | Reuse one manifest from MCP-capable coding agents |
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

[Changelog](https://github.com/sysevol-ai/CodeNib/blob/main/CHANGELOG.md)
&nbsp;&middot;&nbsp;
[Contributing](https://github.com/sysevol-ai/CodeNib/blob/main/CONTRIBUTING.md)
&nbsp;&middot;&nbsp;
[Security](https://github.com/sysevol-ai/CodeNib/blob/main/SECURITY.md)
&nbsp;&middot;&nbsp;
[Citation](https://github.com/sysevol-ai/CodeNib/blob/main/CITATION.cff)

## License

CodeNib is licensed under the
[Apache License, Version 2.0](https://github.com/sysevol-ai/CodeNib/blob/main/LICENSE).
