# CodeNib Product Roadmap

## Objective

CodeNib should turn a local repository into useful, source-grounded context for
both coding agents and humans without requiring users to understand its
internal index architecture.

The release is ready when a new user can:

1. install one package;
2. run one command against a clean repository;
3. understand which capabilities are available and why;
4. read coherent, source-grounded documentation;
5. explore dependencies and jump back to source;
6. use a provider-native or OpenAI-compatible LiteLLM backend;
7. restart and update the Wiki without rebuilding unrelated views.
8. connect a source-linked CodeGraph to a supported coding agent without
   manually editing MCP configuration.

Passing unit tests or publishing a wheel is necessary but not sufficient.

## North-Star Journey

The 0.2.1 agent path is local, model-free, and one command after installation:

```bash
pip install "codenib[graph,mcp]"
codenib codegraph init /path/to/repository
```

It must keep the checkout clean, delegate client configuration to native CLIs,
remain idempotent, expose a machine-readable readiness report, and reverse only
its own registrations.

The browser path remains:

```bash
pip install "codenib[semantic]"
codenib wiki /path/to/repository
```

The CLI should use hybrid retrieval when the semantic capability is installed
and retain an explicit no-model fallback for smaller environments. Optional
generation must be explicit about model use and cost:

```bash
codenib wiki /path/to/repository --generate \
  --model openai/my-model \
  --api-base http://127.0.0.1:8000/v1 \
  --api-key-env OPENAI_API_KEY
```

The UI must distinguish offline, generated, and degraded states. It must never
silently hide a major feature or silently present fallback prose as generated
documentation.

## Acceptance Gates

### Installation and startup

- The release wheel installs in a fresh Python 3.10+ environment.
- The normal serving path does not require a source checkout.
- The target repository remains clean after indexing and serving.
- The first page reports build progress and actionable failures.
- A second launch reuses compatible views and frontend assets.
- Production serving does not run a framework development server.

### Indexing and capabilities

- Generated, vendored, VCS, cache, and build directories are excluded
  consistently by language detection, chunking, and SCIP/LSP backends.
- Adding or updating one view preserves other fresh manifest entries.
- Capability state distinguishes unavailable, installable, building, fresh,
  stale, and failed.
- Dependency Map is visible when a symbol graph is fresh. When it is not
  available, the UI explains the missing dependency or backend instead of
  removing the feature without explanation.
- A failed optional view does not destroy a usable offline Wiki.

### Generated content

- Every cited file and line range resolves against the indexed commit.
- Generated prose names only source-backed files, symbols, and relationships.
- Page planning uses repository intent and structure, not directory names
  alone.
- Evidence selection combines lexical, dense, and structural views when they
  are available and degrades explicitly when they are not.
- Pages cover distinct subsystem responsibilities with bounded duplication.
- Generation cache identity includes commit, view identity, model, endpoint
  identity, prompt version, and generation policy.
- Generation can be resumed page by page after interruption.

### LiteLLM providers

- Ask, outline, page generation, short narration, and edge labels use one
  injected LiteLLM adapter.
- Model, API base, API key source, timeout, retry, and provider-specific
  options have one precedence rule: CLI, environment, config file, provider
  defaults.
- Secrets are not written to repository configuration or process logs.
- Tests cover a provider-native configuration and an OpenAI-compatible local
  endpoint.
- A doctor command verifies configuration before an expensive Wiki run.

### User experience

- The first screen shows repository identity, indexed commit, available views,
  generation mode, and current work without exposing internal implementation
  jargon.
- Overview, architecture, dependency exploration, search, source citations,
  and generation status form one coherent workflow.
- Empty, loading, degraded, and failed states are deliberate and actionable.
- Desktop and mobile layouts pass screenshot checks without overlap.

## Milestones

### M0: Reproducible product baseline

Status: complete.

- Record fresh-install, first-run, restart, graph-build, and generation
  behavior.
- Add a local acceptance harness that exercises index, API, and frontend
  readiness without relying on slow remote CI.
- Keep representative small repositories for deterministic smoke coverage.

Observed on the TestPyPI 0.1.0 candidate:

- Offline BM25 Wiki builds and serves successfully.
- First frontend setup installs 304 npm packages and consumes about 986 MB.
- The default `fast` preset hides Dependency Map because it omits the symbol
  graph.
- A Python graph build scanned vendored SCIP fixtures and failed after 237
  seconds; limiting analysis to the product package completed in about 32
  seconds and produced 5,491 nodes and 22,449 edges.
- Updating only `symbol_graph` replaced the visible BM25 capability in the
  manifest instead of preserving it.
- The default Wiki is deterministic template output, but the UI does not make
  that mode sufficiently clear.

The production frontend migration removes the first-run npm installation:
release wheel and sdist artifacts are about 1.9 MB each, and an isolated wheel
completed index, Wiki, source-link, and MCP service smoke with Node/npm absent
from `PATH`.

### M1: Correct indexing and capability behavior

Status: complete.

- Unify repository exclusion policy across all builders.
- Preserve independently fresh manifest views during partial updates.
- Separate default offline usability from optional view failures.
- Make graph availability and setup visible in CLI and UI.
- Add focused unit and local integration coverage.

Mixed-language graph construction retains successful language views and
records unavailable backends explicitly. The lower-level graph API remains
strict for callers that require complete coverage; the product CLI opts into
partial coverage and surfaces it in both startup output and Dependency Map.

### M2: Provider-independent generation runtime

Status: complete.

- Route every LLM operation through the shared LiteLLM adapter.
- Add explicit generation CLI and configuration.
- Add backend diagnostics, retries, usage reporting, and correct cache keys.
- Validate native-provider and OpenAI-compatible paths.

### M3: Grounded Wiki quality

Status: complete for the current generation path; cross-page quality auditing
remains part of M6 acceptance.

- Build the outline from README, manifest, source hierarchy, and graph
  communities.
- Retrieve diversified evidence per page, then expand relevant structural
  neighbors.
- Generate from an explicit fact plan before producing prose.
- Validate citations and unsupported identifiers before publication.
- Audit coverage and cross-page duplication.

### M4: Product interaction

Status: complete for 0.2.0. Index build progress is explicit in the CLI before
the Wiki opens; in-page progress remains a post-0.2 enhancement.

- Present build and generation progress in the Wiki.
- Integrate Dependency Map, page-local graph snapshots, search, and source
  navigation.
- Expose generation mode and degraded capabilities clearly.
- Add visual regression coverage for core desktop and mobile states.

### M5: Distribution

Status: wheel, Pages, and artifact-backed MCP paths complete for 0.2.0. Docker
remains a post-0.2 distribution path.

- Ship prebuilt frontend assets instead of installing a frontend development
  tree on first use.
- Keep user state outside the target repository by default.
- Provide wheel and Docker paths with the same CLI and configuration model.
- Finish public release documentation only after fresh-machine acceptance.

### M6: Release acceptance

Status: complete. Packaged acceptance covers the wheel, Wiki, MCP, Ask, graph,
Pages, and commit-addressed artifact reuse paths; production publication is
tracked by the release workflow.

- Run the packaged product against representative Python, TypeScript, and
  mixed-language repositories.
- Exercise at least one provider-native backend and one OpenAI-compatible
  backend.
- Review generated content with the source-grounding and subsystem-coverage
  rubric.
- Reconcile related issues and PRs, then publish the accepted artifact.

Acceptance evidence on 2026-07-26:

- A fresh wheel with the MCP extra completed CLI, Wiki, source-link, and MCP
  stdio smoke tests from outside the source checkout.
- A fresh wheel with only the graph extra plus a pinned Python SCIP provider
  completed repository-aware diagnostics, graph construction, caller-to-callee
  edge recovery, source-anchor validation, and the installed Dependency Map API.
- Python, TypeScript, and mixed TypeScript/Python repositories built fresh BM25
  views while leaving `git status --porcelain` empty; a second unchanged build
  reused the manifest without changing its mtime.
- The packaged frontend served without Node/npm and the default entry bundle
  was reduced to about 146 KB (47 KB gzip); graph, source highlighting, and
  page code highlighting load on demand.
- LiteLLM validated both an OpenAI-compatible custom endpoint and a
  provider-native `openai/gpt-4o-mini` probe.
- A generated mixed-language Overview covered all eight planned claims with
  1.0 citation coverage, cited all three source files, and published no unknown
  files, identifiers, or citations.
- On the CodeNib checkout, applying the shared exclusion policy before SCIP
  generation removed nested `web/dist` output from the graph and reduced the
  mixed-language graph build from about 168.9 seconds to 61.89 seconds.
  Python and TypeScript remained available while C/C++, Go, and Rust failures
  were retained as explicit partial-coverage metadata.
- The real-repository v24 Overview published eight of eight planned facts from
  five cited source files with 1.0 citation coverage, three dense sections,
  no duplicate prose blocks, and no repair, fallback, or quality warning.

- Desktop (1440x1000), intermediate (1024x900), and mobile (390x844) Wiki
  captures had no horizontal overflow, console errors, or page errors. The
  Dependency Map rendered a real retrieval-pipeline neighborhood with nine
  symbols, eight edges, and the partial-language coverage notice.

### M7: Agent-ready CodeGraph

Status: implemented for 0.2.1. Publication is independently gated by installed
release acceptance, exact-main verification, and explicit release authorization.

- Add one command that detects languages and clients, installs the safe managed
  subset of graph providers, builds BM25 plus `symbol_graph`, and registers the
  full MCP surface with Codex and Claude Code.
- Keep agent configuration under native client ownership and CodeNib state
  outside the target checkout. Do not create repository instruction or MCP
  files implicitly.
- Require and preserve a clean Git working tree. Install CodeNib-managed
  providers outside the repository, but never run project package managers or
  build-system preparation from the onboarding command.
- Make repeated initialization a no-op for matching registrations. Fail closed
  on unmanaged collisions and managed drift.
- Provide human and JSON status plus receipt-scoped uninstall that preserves the
  reusable index.
- Exercise installed `explore_context` and `dependency_subgraph` calls, source
  verification, both client contracts, idempotency, and uninstall in the
  release graph smoke.

## Hosted Distribution Program

Status: complete for 0.2.0. The implementation and acceptance evidence are
recorded in [RFC #415](https://github.com/sysevol-ai/CodeNib/issues/415).

The local product baseline above established the compiler and runtime. The
distribution stage makes their output reusable across deployment surfaces:

> Build repository context once, then serve the same provenance-checked
> artifact to people and coding agents.

The program has two user-facing surfaces that share one artifact contract:

1. **Static Wiki.** A public repository can build or incrementally update its
   views in GitHub Actions and publish a useful, serverless Pages site.
2. **Agent runtime.** Local and team MCP clients can load the artifact for the
   exact repository commit instead of compiling another private copy.

Generation and embeddings are build-time provider capabilities in the static
surface. A Pages export never contains a provider credential, and it does not
claim query-time semantic search when no authenticated runtime exists. The
Pages surface remains useful through source navigation, pre-generated pages,
and dependency data when those views are available. The matching artifact adds
lexical and optional semantic search when loaded by the local or MCP runtime.

### H1: Static artifact contract ([#416](https://github.com/sysevol-ai/CodeNib/issues/416))

- Export deterministic, API-shaped Wiki data and the packaged frontend.
- Record repository, commit, source fingerprint, schema, builder profile,
  capabilities, source-address semantics, and generation provenance.
- Support Pages base paths and reject path traversal or secret-bearing output.
- Verify an export through local static-server and browser smoke tests.

### H2: Local and BYO inference routes ([#419](https://github.com/sysevol-ai/CodeNib/issues/419))

- Keep the zero-credential path model-free; it must not depend on a hosted
  inference product to remain useful.
- Let semantic builds use either a pinned local embedding model or an explicit
  OpenAI-compatible endpoint. Agent-authored pages use provider-native LiteLLM
  routes or an explicit BYO endpoint.
- Apply one precedence rule across CLI, environment, config, and provider
  defaults; record provider and endpoint identity without credentials.
- Validate model, embedding dimension, prompt version, timeout, and budget
  before an expensive build, and reject the retired GitHub Models route rather
  than silently sending requests to a dead service.

### H3: GitHub Action and Pages ([#422](https://github.com/sysevol-ai/CodeNib/issues/422))

- Restore a compatible prior artifact, update only invalidated views, and
  publish both a commit-addressed context artifact and a static Wiki.
- Use a local embedding model when the semantic preset requests it, accept an
  explicit BYO secret for remote embeddings, and degrade deterministically to
  the model-free product path when no inference route is selected.
- Never run secret-bearing generation against untrusted fork code or expose
  an Actions token in browser assets.

### H4: Artifact-backed MCP ([#424](https://github.com/sysevol-ai/CodeNib/issues/424))

- Generate client configuration for supported MCP hosts.
- Resolve an artifact by repository identity and commit, verify compatibility,
  then load it without rebuilding the repository.
- Keep tool results bounded, source-linked, and explicit about unavailable
  views.

### H5: Cost-quality accounting ([#426](https://github.com/sysevol-ai/CodeNib/issues/426))

- Report localization quality and cost on the same paired query set.
- Use **cost per successfully localized query**, not cost per resolved issue,
  until patch generation and test outcomes are measured.
- Separate cached and uncached model input, embedding and reranking calls,
  amortized build cost, and provider price snapshots. Copilot premium requests
  remain a separate billing unit rather than a token-cost proxy.

### H6: Hosted release acceptance

- Deploy a fresh public repository from a pinned CodeNib release.
- Reuse its downloaded artifact through MCP at the indexed commit.
- Exercise the no-model path, a pinned local embedding model, and a BYO
  endpoint.
- Publish security, provider, migration, and artifact compatibility guidance.

### Post-release: Optional managed semantic plane

The open-source release does not require a CodeNib-operated model service.
After H6, a managed embedding endpoint may remove model downloads for teams
that want a hosted route. It must preserve the same inference-route and vector
compatibility contract, use content-addressed batching and caching, isolate
tenants, enforce explicit retention and rate limits, and expose measured cost.
Static Pages and BM25/MCP serving must continue to work when that service is
absent. This is a separate service milestone, not a hidden prerequisite for the
open-source product.

Each hosted milestone lands as an independently reviewable PR. Local contract
tests and a focused end-to-end smoke run precede slow remote CI.

## Completion Rule

The 0.2 product baseline is complete only when H1-H6 are implemented, locally
verified, documented, and reproduced from pinned release artifacts. That bar
was met by the pinned TestPyPI candidate and the matching release matrix; the
stable tag remains a publication operation. Future hosted inference and
enterprise storage work remains independent of the open-source 0.2 release.
