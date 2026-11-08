<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Changelog

All notable user-facing changes are recorded here. CodeNib follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- A refused Wiki submission now says why: the key was not accepted, has no
  credit, is a management key, or OpenRouter could not be reached. A refused
  key no longer uses up the one try per minute, other waits count down on the
  form, and "Check saved attempt" appears only when the server may have
  accepted the run. The server logs each refusal's reason, never the key.
- A hosted server whose `PATH` lacks Node.js now reports that the Python
  indexer cannot run, instead of starting it and recording a bare "index
  failed". Failed indexing logs the indexer's exit status.

### Changed

- The writing model list now offers Claude Opus 5.5 and Claude Haiku 5.5 (in
  place of Haiku 4.5), and newer GPT, Gemini, Grok and Mistral models. Claude
  Sonnet 5.5 no longer sends parameters that model rejects, which kept its runs
  from starting. Recorded prices match OpenRouter's current catalog.
- Generating a Wiki for your own repository now uses the new Wiki's map and
  workspace. The form page draws the repository's folders from its source check.
  While the Wiki is written, chapters appear on the map once planned, the one
  being written turns, and each fills in when it passes its source checks, ready
  to open. The reader lists every step with what the server reported, folds into
  one line once a chapter is open, and stops all motion when progress stalls.
- Generating a Wiki no longer has a 50,000 code-chunk limit. Source is found with
  grep and Jev, not a chunk index, so only the download, size and file-count
  limits apply.
- The codenib.ai homepage no longer repeats one layout. The example fills the
  page with its questions above it, the repository form is a short narrow band,
  and the agent setup is one wide terminal that replays a recorded session line
  by line while drawing its caller graph, with every node linked to its source.
  The ContextBench result and its diagram move up from the blog card into their
  own band, and languages run on a strip of icons that says what CodeNib can do
  with each. Hosted limits fold away under the repository form; the OpenRouter
  cost stays visible.
- The blog now matches the homepage: same dark colors, with every figure
  recolored, so moving between them no longer flips from black to white.
- Map labels now wrap onto two lines instead of cutting off long page names.
- On a wide screen a Wiki now opens as a workspace: the page list, a map of the
  Wiki and the reader, side by side. Pages are nodes, and the heavy lines are
  references the code index recorded between areas; click one to read an example
  in the source. The map can be hidden, and narrower windows keep the
  single-column page.
- The Wiki UI opens in the dark theme unless the reader has chosen otherwise.
- Wikis are easier to read. Publication now also checks how a page reads:
  template sections and printed reader questions are gone, a page opens on what
  the code does rather than on an identifier, and limits apply to inline code,
  sentence length and page length. A page that fails gets one rewrite. The model
  reader review now blocks detail pages it rates below its floor. Existing Wiki
  pages regenerate the next time they are opened, or when a prewarm runs.
- The page list opens on the Overview and at most five pages, ranked from the
  recorded call graph. Detail pages wait behind their parent and lower-ranked
  areas behind "More topics"; none are removed, and a parent page links to its
  detail pages under "Go deeper".
- The Overview leads with the main path through the code, one sentence per
  recorded call, chosen from the call graph rather than from what a search
  retrieved. The semantic architecture sits behind "How the parts fit together",
  and a page's callers and calls behind a disclosure.

### Added

- The most important detail pages draw their longest recorded call path, labelled
  as one selected path. No model chooses the arrows.

- `codenib guard check` compares the working tree with a commit and lists the
  tests that reach the changed definitions within three calls, each with its
  call path and call lines. The graph is built from the commit's tracked files
  and cached per commit. `--json` and `--exit-code` serve scripts and CI.
- `codenib guard install` adds SessionStart and Stop hooks for Claude Code and
  Codex. Before the agent finishes, the hook hands back the tests its change
  reaches; it reports each change once and never fails the agent's turn.
- Notes anchored to code: `codenib guard note add|list|confirm` keeps Markdown
  notes in `.codenib/notes/`. The guard shows notes on changed code and flags
  notes whose code changed.
- Constructor edges for the guard's Python call graph: `C(...)` reaches C's own
  or inherited `__init__` and `__new__`, including through aliases.
- The packaged Wiki UI includes real guard cases at `/guard`. A deployment can
  open on them with `home: "guard"` in its runtime configuration.

### Changed

- The opt-in first-party funnel collector accepts `install_command_copy`,
  `guard_case_view` and an opt-in, once-per-user `guard_first_run` event.

## [0.2.5] - 2026-11-04

### Added

- `codenib codegraph check` verifies call-path rules from
  `.codenib/checks.yaml`: every path to a symbol must pass a guard, or a
  function that calls a sanitizer. It reports a witness path per violation,
  limits the report to a diff with `--base`, and exits 1 for CI. Unresolved
  Python calls are matched by name so a missing type cannot hide a path.
- Bounded static entry paths in `explore_context`, with source locations,
  explicit ambiguity and omitted-path diagnostics, and readable text output.
- Public-repository visitor Wikis with linked chapters, persistent generation
  progress, resumable saved results, model selection, share cards and explicit
  owner publication to Browse. Saved results begin unlisted.
- Source-bound architecture diagrams, saved code maps, a seekable system-map
  film and a self-contained first-visit repository example.
- OKF v0.2 export of complete cached Wikis, with source excerpts, evidence
  sidecars, version identities and a hashed file inventory. Explicit
  `--agent-dir` maintains Wiki discovery pointers in agent instruction files.
- Optional BM25-first CodeGraph initialization with `--defer-graph`, a composed
  exploration tool surface and on-demand graph preparation. Deferred Python
  full-tool sessions can return bounded source definition candidates.
- ContextBench and Agent Retrieval Bench adapters with reproducible inputs,
  source-delivery receipts and benchmark-native scoring. Their reported results
  remain separate from CodeGraph workflow-benefit claims.

### Changed

- Use pinned Pyrefly 1.4.0 as the default Python graph provider, with explicit
  scip-python selection and fallback, source-scope checks and native parity.
- Prepare deferred Python graphs in the background and answer from source
  retrieval while graph preparation is still running.
- Accept dirty checkouts during CodeGraph setup, reject stale indexed queries
  after edits and reload explicitly rebuilt indexes in the same MCP session.
- Share the architecture reader between prepared and visitor Wikis, show
  unavailable visual coverage and preserve source-pane layout during navigation.
- Add camelCase parts to BM25 tokens and retain validation-test candidates
  without crowding out implementation matches.

### Fixed

- Refresh compatible locked HTTP/crypto dependencies and frontend build/test
  dependencies for the release candidate.
- Refresh deferred retrieval after source edits, retain stale-source detection
  across reloads, and cancel owned graph builds during MCP shutdown.
- Preserve call-chain source lines, delivery budgets and delegation; retain
  uncertainty for bounded or ambiguous same-named method searches.
- Resolve Python names in enclosing function scopes, reject rebound receiver
  fields, and prevent PEP 695 names from inheriting their owner's identity.
- Recover Python module, field and wildcard-reference identities; bind retrieval
  seeds to indexed spans and retain complete requested definitions within source
  delivery limits.
- Cover Go test files and nested modules, preserve LSP replies across reads and
  idle drains, and bound language-server teardown.
- Preserve source authority across ignored cache churn and publish scip-python
  indexes only after the indexer finishes, without analyzing caller site-packages.
- Bound SCIP test-fixture retries and failure cleanup; repair schematic map
  routing and title/tag overlap.

### Removed

- Retired the source-only H1 hybrid-index experiment. Product storage remains
  Wiki-only; manifest-bound indexes and portable artifacts remain file-based.

## [0.2.4] - 2026-09-26

### Added

- Source-only model-planned grep followed by Jev reranking, using one
  user-owned OpenRouter credential from CLI or MCP. The optional `grep` extra
  bundles ripgrep; the route does not require embeddings, a GPU or an index.
- OpenRouter S256 PKCE login and credential management, with supported OS
  keyrings and an explicit private POSIX file fallback. `codenib init`
  registers the source-only route through native Claude Code and Codex CLIs.
- Static export of precomputed story Wikis with source citations, page maps,
  architecture visuals and CodeNib backlinks. Public browsing needs no model
  calls or live Wiki backend.
- Optional browser OpenRouter trials on explicitly enabled static public Wikis.
  The browser owns authorization and model requests; a separate bounded CPU
  service returns source-checked grep candidates without receiving a model key.
  Default static browsing still makes no model calls. Actual provider consent
  and production-host acceptance remain preview deployment gates.
- Jev reranking through OpenRouter's Decisions API, selectable with
  `rerank_strategy="decisions"`. The typed client supports Noul, Choice, and
  Score questions; reranking uses bounded batches and normalized relevance
  scores while preserving first-stage candidates when a request fails.

### Changed

- GitHub Pages previews default to the `fast` preset, avoiding embedding
  downloads. Generated story Wikis remain a separate, explicit generation and
  cached-export path.
- Present source-linked agent setup, a recorded agent session, retrieval
  evidence, language capabilities and sourced tool comparisons prominently
  in the README and homepage.
- Bind the retrieve/rerank pipeline's BM25 source reads to its repository root
  so rerankers receive candidate code when invoked from another directory.
- Bound each built-in Wiki generation-lock acquisition to a 30-second wait. A
  timed-out waiter does not interrupt the owner or start duplicate generation,
  and a later request can reuse the owner's published result.
- Wiki cache audits and prewarm dry runs now copy an existing, quiescent
  `wiki.sqlite3` into a private temporary snapshot and inspect that snapshot in
  immutable read-only mode. They create no lock or SQLite sidecar beside the
  source database, do not initialize or change its schema or lock directory,
  and reject existing transaction sidecars or a source change while the
  snapshot is captured.

### Fixed

- Pass the installed package version explicitly to MCP initialization so
  `serverInfo.version` is populated with MCP Python SDK 2.2.0 (#779, #780).
- Support bounded multiline grep plans and Windows newline handling. Preserve
  Windows source authority when unrelated sibling directories change while
  retaining repository-root and ancestor-identity checks.
- Validate Wiki graph references separately from source paths, persist evidence
  as strict JSON and limit evidence ranges to the returned source lines.

### Removed

- Retired the one-release `agentwiki_*.json` compatibility path tracked by
  #749. AgentWiki no longer discovers, reads, writes, adopts, locks, or audits
  JSON cache files; persistence requires an injected `WikiStore`, while
  store-less callers remain in memory. Existing `wiki.sqlite3` entries and
  legacy files are not deleted. The Wiki cache audit report is now schema v2
  and reports only database entry and payload-byte accounting.

## [0.2.3] - 2026-09-03

### Added

- A Wiki-only `codenib.storage` facade exposing the domain-local `WikiStore`
  harness and SQLite WAL implementation, with versioned schema initialization,
  bounded canonical JSON, integrity checks, atomic publication, and
  cross-process generation guards.

### Changed

- AgentWiki, the web service, cache audit, prewarming, and Wiki benchmarking
  now use `wiki_cache/wiki.sqlite3`. Eligible source-compatible JSON entries
  remain available through read-through compatibility and are not rewritten or
  deleted.
- The supported product database boundary is now
  `codenib.storage.WikiStore`, with `SQLiteWikiStore` as its supported adapter.
  `RepoManifest`, BM25, FAISS, igraph, and portable context payloads remain file
  artifacts. Existing compiler caches use `artifact pack`; portable artifacts
  continue through `artifact verify` and `mcp --artifact`.
- The v0.2.3 release intentionally narrows the former storage API and CLI.
  Users who still need data from a v0.2.2 SQLite catalog must run `artifact
  materialize` with a pinned v0.2.2 environment before upgrading; the resulting
  portable artifact remains supported.

### Removed

- The optional Web `index_storage` configuration and its catalog-backed local
  runtime, retained BM25 activation, reconciliation, durable index-job
  orchestration, `/api/index-jobs` write/read routes, and index-status job or
  update-capability fields. Index status remains available as a read-only
  `RepoManifest` projection.
- The unreleased catalog-selected `codenib mcp` cold-start mode. Portable
  artifacts remain served through `codenib mcp --artifact [--repo]`.
- The unreleased `storage audit` command and `index --publish-retained` path.
  Those two batches removed 1,407 production lines and 1,742 dedicated test
  lines, including the audit execution path, eight index-only options, and
  three compiler exports; ordinary indexing remained unchanged in that batch.
- The post-v0.2.2 retained-storage benchmark gate, its manual Make target, and
  noncanonical readiness receipt. The report-only experiment had no product or
  CI workflow caller and could never report `promotion_eligible=true`; removing
  it deleted a 4,913-line harness, 3,468 test lines, 367 evidence lines, 148
  documentation lines, eight Make variables, and one manual target.
- The unreleased `codenib jobs` execution stack and catalog schema v5 through
  v8. This removed eight production modules (9,615 lines), twelve dedicated
  test modules (22,170 lines), 73 lazy exports, two interruptible CAS methods,
  one command with two execution paths, and four schema migrations; changes
  inside retained files bring the batch total to 21,214 production and 29,352
  test lines removed.
- The generic retained-storage surface formerly published by v0.2.2 through
  `codenib.storage`: its schema-v4 SQLite catalog and local CAS, `artifact
  import-cache`, `artifact materialize`, and the retained compiler bridge. The
  name `codenib.storage` now resolves only to the Wiki database facade. Normal
  index, Wiki, portable-artifact, publish, and MCP paths are unchanged.
- The catalog-backed clangd FactBatch publication/reuse experiment and its
  profiling target. Provider-neutral FactBatch models, resolvers, native buffer
  transport, and the promoted clangd query index remain independent of storage.

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

[Unreleased]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/sysevol-ai/CodeNib/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/sysevol-ai/CodeNib/compare/v0.1.0...v0.2.0
[0.2.0a2]: https://github.com/sysevol-ai/CodeNib/compare/620a82d...78e12ac
[0.2.0a1]: https://github.com/sysevol-ai/CodeNib/tree/620a82d
[0.1.0]: https://github.com/sysevol-ai/CodeNib/releases/tag/v0.1.0
