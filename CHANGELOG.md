<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable user-facing changes are recorded here. CodeNib follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0a2] - 2026-08-05

### Added

- A repository-aware `codenib toolchain` command that detects graph and LSP
  providers from the language registry, installs pinned package-managed tools
  under `~/.codenib/toolchains`, and reports system or project prerequisites
  without invoking `sudo` or mutating the target checkout.

### Changed

- Local `index` and `wiki` commands now select semantic hybrid retrieval when
  the semantic extra is installed and explicitly report the no-model BM25
  fallback otherwise. The reusable Pages workflow selects semantic retrieval
  by default and caches its pinned local embedding model; `fast` remains an
  explicit no-model route.
- CodeNib processes automatically prefer the managed tool directory, removing
  the shell `PATH` export previously required after source-checkout installs.
- The public quickstart recommends BM25+dense serving and uses a normal,
  version-pinned PyPI command instead of exposing a TestPyPI wheel URL.

## [0.2.0a1] - 2026-08-05

### Added

- A reusable GitHub Pages workflow and composite Action that build a no-model
  static Wiki plus a commit-addressed BM25 context artifact. Semantic builds
  can use a pinned local CodeRankEmbed model or an explicit OpenAI-compatible
  embedding endpoint.
- Portable context artifacts with complete file inventories, source identity,
  capability metadata, and an artifact-backed MCP path that reuses BM25 and
  vector views without rebuilding the repository.
- Secret-free inference route identities for local and remote embeddings,
  compatibility fingerprints, provider-aware diagnostics, and fail-closed
  handling of the retired GitHub Models service.
- A native repository explorer with selective view loading and revision-pinned
  compatibility layers for SWE-Explore, Agentless, CoSIL, LocAgent, and
  OrcaLoca SearchAgent contracts.
- Quality-constrained cost reports that pair localization success with token,
  USD, and amortized build cost rather than reporting cost without a quality
  denominator.
- Graph schema 5 records semantic symbol kinds, explicit definition
  provenance, and anchored TypeScript/TSX import and re-export edges.

### Changed

- Static Wiki exports now preserve mount paths, source citations, generated
  pages, and dependency data while keeping query engines in the local or MCP
  runtime.
- Pull requests build and inspect distributions once; the complete
  cross-version and installed-service release matrix runs on `main`, release
  tags, and explicit TestPyPI dispatches.
- Static navigation and source adapters no longer treat reference-only symbol
  provenance as a definition location. Existing schema 4 graph artifacts need
  a one-time `codenib index <repo> --preset graph --rebuild`.

### Security

- Downloaded context artifacts reject pickle payloads, unsafe archive paths,
  symbolic and special files, unbounded expansion, inventory mismatches, and
  source checkouts that do not match the recorded commit and fingerprint.
- GitHub artifact downloads verify the API digest and never forward the GitHub
  bearer token to redirects. Pages publication scans outputs for configured
  credentials and rejects secret-bearing fork execution paths.

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

[Unreleased]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.0a2...HEAD
[0.2.0a2]: https://github.com/sysevol-ai/CodeNib/compare/620a82d...v0.2.0a2
[0.2.0a1]: https://github.com/sysevol-ai/CodeNib/tree/620a82d
[0.1.0]: https://github.com/sysevol-ai/CodeNib/releases/tag/v0.1.0
