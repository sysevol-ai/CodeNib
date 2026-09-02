<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable user-facing changes are recorded here. CodeNib follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Repository-bound BM25 source-attempt shards with cooperative shared writer
  and exclusive reaper leases, plus a default-off post-operation-return cleanup
  pass that retains legacy workspace cleanup as an explicit compatibility step.
- A domain-local `WikiStore` harness and SQLite WAL implementation with
  versioned schema initialization, bounded canonical JSON, integrity checks,
  atomic publication, and cross-process generation guards.
### Changed

- AgentWiki, the web service, cache audit, prewarming, and Wiki benchmarking
  now use `wiki_cache/wiki.sqlite3`. Eligible source-compatible JSON entries
  remain available through read-through compatibility and are not rewritten or
  deleted.
- The supported product database boundary is now the domain-local `WikiStore`
  with its SQLite WAL implementation. `RepoManifest`, BM25, FAISS, igraph, and
  portable context payloads remain file artifacts; generic retained storage and
  its explicit CLI commands are frozen as experimental/compatibility surfaces.

### Removed

- The optional Web `index_storage` configuration and its catalog-backed local
  runtime, retained BM25 activation, reconciliation, durable index-job
  orchestration, `/api/index-jobs` write/read routes, and index-status job or
  update-capability fields. Index status remains available as a read-only
  `RepoManifest` projection.

### Security

- Native directory descriptors are committed to opaque owners before returning
  to Python, and writer/reaper leases remain coupled to retryable cleanup and
  exact repository, workspace, and shard identities. Attempt-pool bootstrap
  rejects nested or independently selected mount views before shard creation
  within its documented controlled, quiescent path-and-mount contract.

## [0.2.2] - 2026-08-21

### Added

- Persistent, exact repository-relative source exclusions through
  `--exclude-dir` and `--clear-exclude-dirs`, with one recorded policy reused
  by CodeGraph, indexing, MCP, Wiki, Web, and public compiler entry points.
- Manifest 1.2 source-selection identities and builder receipts, while retaining
  strict read compatibility for manifest 1.1 artifacts.
- Authenticated optional SCIP occurrence-sidecar receipts bound to the same
  owned symbol-graph generation as `graph.pkl`.

### Changed

- Authenticated indexing now accepts absolute symbolic links only when their
  lexical target remains inside the same pinned repository authority. Expo,
  CocoaPods, and pnpm-style contained link layouts no longer require a broad
  default-ignore rule.
- CodeGraph status reports the effective source selection, and Wiki, publish,
  doctor, and toolchain flows reuse it instead of silently widening the source
  surface.
- Manifest-bound C and C++ queries temporarily use the verified persisted
  symbol graph instead of consuming project-local clangd postings directly.

### Security

- Source paths, graph artifacts, native query routes, and retained manifests
  are checked against the same authenticated source selection before they can
  be published or served. Unproven native routes fail closed to the persisted
  graph.
- Zoekt builds use a fixed commit-tree contract, private generation
  publication, and an authenticated shard-tree receipt; custom exclusions are
  rejected until the backend can prove that narrower surface end to end.
- Authenticated MCP `search_zoekt` serving uses a retained Linux `/proc`
  descriptor snapshot. Other platforms fail closed until they have an
  equivalent immutable runtime handoff.

## [0.2.1] - 2026-08-14

### Added

- A one-command `codenib codegraph init` path that installs repository-aware
  graph providers, builds BM25 and symbol-graph views, and configures installed
  Codex and Claude Code clients through their native MCP CLIs.
- Machine-readable CodeGraph status, idempotent client receipts, safe uninstall,
  and a no-write dry run.
- Installed-wheel black-box coverage for `explore_context`,
  `dependency_subgraph`, both client contracts, repeat initialization, and
  checkout cleanliness.

### Changed

- The primary agent quickstart now leads with the local, model-free CodeGraph;
  the Wiki and semantic routes remain available as independent product paths.

### Security

- Client setup refuses unmanaged name collisions and drifted managed
  registrations. Uninstall removes only receipt-owned entries and preserves
  repository indexes.

## [0.2.0] - 2026-08-05

### Added

- A build-once distribution path that publishes a static Wiki and a verified,
  commit-addressed context artifact for local or MCP reuse.
- Capability-aware `search_context` serving through the official
  `ai.codenib/codenib` MCP package, plus managed package-level SCIP and LSP
  providers selected from repository languages.
- Native repository-exploration adapters and quality-constrained token and
  cost reports for supported agent localization policies.

### Changed

- Hybrid BM25+dense retrieval is the recommended installed default when the
  semantic extra is present; the base package retains an explicit no-model
  BM25 path.
- Repository filtering, graph definition provenance, and TypeScript/TSX import
  edges use updated builder contracts. Incompatible 0.1 views rebuild once;
  compatible 0.2 views remain reusable.

### Security

- Downloaded context artifacts are inventory-checked, source-bound, and reject
  unsafe archive members before any view is loaded.

## [0.2.0a2] - 2026-08-05

### Added

- A capability-aware `search_context` MCP tool and version-matched metadata for
  publishing the local stdio server as `ai.codenib/codenib` in the official MCP
  Registry.
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

[Unreleased]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/sysevol-ai/CodeNib/compare/v0.1.0...v0.2.0
[0.2.0a2]: https://github.com/sysevol-ai/CodeNib/compare/620a82d...78e12ac
[0.2.0a1]: https://github.com/sysevol-ai/CodeNib/tree/620a82d
[0.1.0]: https://github.com/sysevol-ai/CodeNib/releases/tag/v0.1.0
