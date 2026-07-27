# CodeNib Product Roadmap

## Objective

CodeNib should turn a local repository into a useful, source-grounded Wiki and
repository exploration surface without requiring users to understand its
internal index architecture.

The release is ready when a new user can:

1. install one package;
2. run one command against a clean repository;
3. understand which capabilities are available and why;
4. read coherent, source-grounded documentation;
5. explore dependencies and jump back to source;
6. use a provider-native or OpenAI-compatible LiteLLM backend;
7. restart and update the Wiki without rebuilding unrelated views.

Passing unit tests or publishing a wheel is necessary but not sufficient.

## North-Star Journey

```bash
pip install codenib
codenib wiki /path/to/repository
```

The default command must produce a useful offline Wiki. Optional generation
must be explicit about model use and cost:

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
release wheel and sdist artifacts are about 2.2 MB each, and an isolated wheel
completed index, Wiki, source-link, and MCP service smoke with Node/npm absent
from `PATH`.

### M1: Correct indexing and capability behavior

Status: complete.

- Unify repository exclusion policy across all builders.
- Preserve independently fresh manifest views during partial updates.
- Separate default offline usability from optional view failures.
- Make graph availability and setup visible in CLI and UI.
- Add focused unit and local integration coverage.

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

Status: in progress.

- Present build and generation progress in the Wiki.
- Integrate Dependency Map, page-local graph snapshots, search, and source
  navigation.
- Expose generation mode and degraded capabilities clearly.
- Add visual regression coverage for core desktop and mobile states.

### M5: Distribution

Status: in progress. The Python-only production frontend path is complete;
Docker and public documentation remain.

- Ship prebuilt frontend assets instead of installing a frontend development
  tree on first use.
- Keep user state outside the target repository by default.
- Provide wheel and Docker paths with the same CLI and configuration model.
- Finish public release documentation only after fresh-machine acceptance.

### M6: Release acceptance

- Run the packaged product against representative Python, TypeScript, and
  mixed-language repositories.
- Exercise at least one provider-native backend and one OpenAI-compatible
  backend.
- Review generated content with the source-grounding and subsystem-coverage
  rubric.
- Reconcile related issues and PRs, then publish the accepted artifact.

## Completion Rule

The product goal remains active until all acceptance gates relevant to the
Developer Preview are implemented, locally verified, documented, and
reproduced from the packaged artifact. A single successful demo repository or
green remote CI run does not complete the goal.
