<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable user-facing changes are recorded here. CodeNib follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-07-26

### Added

- A unified `codenib` CLI with `index`, `wiki`, `mcp`, and `doctor` commands.
- A two-command local Wiki path with automatic language detection and reusable
  repository manifests.
- Fast, semantic, and full indexing presets with explicit optional
  dependencies.
- A deterministic static Wiki that requires no LLM credentials.
- A source-linked MCP server for retrieval, graph, and static navigation tools.
- Multi-view incremental maintenance and evaluation infrastructure.

### Changed

- Renamed the maintained package, command, state, and protocol namespace to
  CodeNib.
- Reduced the default installation to the BM25 and Wiki runtime; model, graph,
  dataset, MCP, and agent dependencies now use named extras.
- Made public package imports lazy so CLI startup does not initialize model or
  graph runtimes.

### Security

- Prepared secretless PyPI publishing through a dedicated GitHub Actions OIDC
  workflow.

[Unreleased]: https://github.com/sysevol-ai/CodeNib/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sysevol-ai/CodeNib/releases/tag/v0.1.0
