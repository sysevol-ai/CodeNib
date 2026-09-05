<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Cognitive-Debt Reduction Roadmap

## Objective

CodeNib should remain understandable as a product while its language and query
capabilities grow. This program reduces the number of independent concepts,
execution paths, and lifecycle states a contributor must understand. A smaller
file or a moved symbol is not progress unless it also removes an implementation,
an invariant, a public contract, or a required test matrix.

The protected product journeys are:

1. `codenib codegraph init` -> compiler -> manifest -> MCP;
2. `codenib wiki` -> compiler -> manifest -> local Wiki and Ask;
3. `RepositoryContextExplorer` -> manifest-backed repository context.

Subtraction must preserve these journeys, source identity, repository-relative
locations, manifest compatibility, multi-language routing, and explicit
capability degradation.

## Baseline

The 2026-08-24 audit of `main` at
`c95ce9a2d1f1ab6bb4c3b2a8e79a699531b21b85` found:

| Surface | Python lines | Observation |
| --- | ---: | --- |
| `codenib/` | 225,398 | Up from 88,165 at `v0.1.0` |
| `test/` | 202,130 | Up from 59,931 at `v0.1.0` |
| `scripts/` | 41,280 | 37 scripts / 9,650 lines had no maintained entrypoint or documentation reference |
| research and compatibility packages | 48,123 | `eval`, `dataset`, `clients`, `integrations`, and speculative `serving` ship in the stable namespace |
| persistence packages | 24,469 | `storage` and `artifacts` implement a stronger threat model than the default local product paths use |

The audit also found three independent persisted-view loaders, three retrieval
execution paths, package-level `compiler`/`artifacts` and `web`/`wiki`
dependency cycles, and experimental implementations retained after their
promotion gates had closed.

## Lifecycle Classes

Every top-level package or substantial feature must have one current class:

| Class | Meaning | Compatibility expectation |
| --- | --- | --- |
| `core` | Required by every protected product journey | Stable internal direction; strict regression gates |
| `product` | User-facing adapter or application | May depend on core, never the reverse |
| `optional` | Supported backend selected by an explicit capability | Tested contract; no default-path import cost |
| `experimental` | Time-bounded candidate with a named promotion gate | No stable re-export; owner and exit date required |
| `labs` | Evaluation, research, or upstream-policy compatibility | Separate install/test surface from the product core |
| `legacy` | Compatibility-only surface awaiting removal | Deprecation owner and removal release required |

Unknown is not a valid long-term class. New experimental code must declare its
class before it lands.

## Subtraction Rules

1. A failed or superseded experiment retains its decision, receipts, and
   aggregate evidence in a durable document. Its implementation, executable
   harness, private ABI, environment switches, and dedicated tests are removed
   unless a new approved gate names them as inputs.
2. No experiment remains indefinitely because it is default-off. Default-off
   code still consumes review, documentation, build, and test attention.
3. Trivial helpers are not centralized merely to reduce line count. Extract a
   shared component only when it owns a repeated workflow or invariant.
4. A new facade must replace at least one old path before the iteration closes.
   Parallel migration paths require a removal milestone.
5. Stable public exports require a documented product consumer. Research
   scripts and examples import explicit submodules or a labs package.
6. Tests for deleted behavior are deleted. Historical evidence belongs in the
   roadmap, not in permanently runnable tests for an unavailable candidate.
7. Each iteration reports production lines, test lines, scripts, public entry
   points, and architectural paths removed. Net line count alone is not the
   acceptance criterion.

## Target Dependency Direction

```text
source and manifest contracts
  -> view builders and language backends
  -> repository runtime and query executor
  -> product adapters (CLI, MCP, Wiki, Python API)

Wiki consumer -> WikiStore -> SQLiteWikiStore
manifest-bound views -> BM25 / FAISS / igraph / portable file artifacts
source-only H1 experiment -> published artifact contracts
labs and experiments -> published contracts only
```

`web` must not own Wiki domain behavior, MCP must not own the reusable
repository runtime, and `compiler` and `artifacts` must not import each other.
Generic catalog, snapshot, job, lease, and object-store machinery is not on a
protected product path. The bounded H1 experiment is evidence-gathering outside
the installed package, not another product runtime.

## Workstreams

### S0: High-confidence subtraction

Status: in progress.

- [x] Remove the unreferenced `codenib.agent.utils` module.
- [x] Retire the rejected per-file native Python chunk POC.
- [x] Retire the rejected repository-batch chunk gate implementation while
      retaining its recorded result in the SCIP roadmap.
- [x] Remove every build switch, environment variable, test, profiler, and
      active documentation instruction owned only by those experiments.
- [x] Retire the unowned RFC #133 retrieval comparator and manual skill smoke,
      plus the standalone metric recomputation superseded by
      `codenib.eval.retrieval_eval`.
- [ ] Inventory scripts by command entrypoint, module consumer, documentation,
      tests, and lifecycle owner; a missing literal path reference alone is not
      dead-code evidence. Delete confirmed orphans in independently reviewable
      groups.
- [ ] Retire the failed, default-off SCIP MCP consumer provider and its gate
      without removing the promoted `FactQueryIndex` and clangd consumers;
      retain the exact negative M2 receipts in the SCIP roadmap.

### S1: Wiki-only database boundary

Status: in progress.

- [x] Declare the Wiki-only `codenib.storage.WikiStore` facade and its SQLite
      WAL adapter as the only product database boundary.
- [x] Keep `RepoManifest`, BM25, FAISS, igraph, and portable context payloads
      as manifest-bound file artifacts rather than catalog records.
- [x] Remove the optional Web `index_storage` configuration, retained job
      orchestration, reconciliation, and runtime-activation path.
- [x] Freeze the generic v0.2.2 `codenib.storage` contents and explicit
      retained-storage CLI commands while their consumers and release boundary
      are audited; add no
      PostgreSQL, S3, generic GC, or backend discovery.
- [x] Remove the unreleased audit, direct MCP, index dual-write, benchmark-gate,
      durable-execution slices, and the two unreachable retained BM25
      generation plan/replay helpers left by that execution stack. The orphan
      closure removed 231 production lines, zero test lines, zero scripts, and
      zero public entry points. At that stage, the frozen CLI surface contained
      only the two published `artifact import-cache` and `artifact materialize`
      compatibility bridges.
- [x] Use v0.2.3 as the removal boundary for the complete frozen generic
      surface: replace the package with a single Wiki-only
      `codenib.storage` facade, and delete the two retained artifact commands,
      retained compiler bridges, and catalog-backed FactBatch publication
      experiment. Existing catalogs must be materialized with a pinned v0.2.2
      environment before upgrade.
- [x] Move portable-artifact validation contracts into the artifact-neutral
      `_bounded_json` module so default Web imports no longer cross a generic
      storage namespace.
- [x] End the v0.2.3 Wiki JSON migration window by removing legacy cache
      lookup, parsing, writes, adoption, file locks, provenance bookkeeping,
      and file-based audit accounting. Keep existing SQLite rows readable and
      make a missing `WikiStore` explicitly memory-only. The closure removes
      354 net production lines and 256 net test lines while deleting zero
      scripts and zero public entry points.
- [ ] Bound Wiki generation-lock waiting and make maintenance inspection
      physically read-only without broadening the Wiki protocol.

Exit condition: protected product journeys use only the Wiki database facade
and manifest-bound artifacts; no generic storage surface remains.

### S1E: Time-bounded hybrid-index evidence

Status: experimental through 2026-10-04.

Owner: index/artifact maintainers.

- Keep the candidate under `scripts/experimental/hybrid_index/`, outside the
  installed `codenib` namespace and every default product import.
- Accept exactly one verified BM25-only portable artifact and use exactly one
  SQLite WAL catalog plus local SHA-256 CAS implementation.
- Add no stable export, official CLI/configuration, Web/MCP route, backend
  registry, additional view, job, lease, GC, overlay, or remote adapter.
- Treat a real non-test consumer, a representative benchmark, portable
  round-trip compatibility, and the recorded failure matrix as promotion
  prerequisites rather than follow-up work.
- By 2026-10-04, make an explicit promotion decision or delete the
  implementation, executable script, and dedicated tests while retaining the
  evidence record.

Exit condition: H1 is either promoted by a new scope decision that names the
old path it replaces, or fully removed. Source-only and default-off code does
not receive an indefinite exception.

### S2: Stable versus labs packaging

Status: pending.

- Correct the package-discovery boundary: `exclude = ["eval*"]` does not match
  the shipped `codenib.eval` namespace, and stable console scripts currently
  expose evaluation and benchmark commands. Treat their removal as a
  release-managed compatibility change rather than incidental cleanup.
- Classify `eval`, `dataset`, `clients`, `integrations`, `serving`, legacy model
  pipelines, and graph incremental patching.
- Move research console scripts and dependencies out of the default wheel.
- Preserve upstream compatibility claims only where a maintained product or
  labs package owns their verification.

Exit condition: a normal CodeGraph or Wiki install does not ship research-only
commands or speculative model serving.

### S3: One repository runtime

Status: in progress.

- [x] Make Web graph eligibility use canonical `RepoManifest` freshness and
      remove its weaker local implementation and the unusable legacy vector
      sidecar discovery path.
- Define the supported `RepositoryContextExplorer` context protocol, migrate
  duck-typed consumers to canonical manifests, and only then remove its
  status-only freshness compatibility fallback.
- Extract one neutral manifest/source/view loader from MCP `ServerContext`, Web
  `RepoRegistry`, and `compiler.skill_context`.
- Make MCP, Web, Wiki, and `RepositoryContextExplorer` adapters over that
  runtime.
- Delete the superseded loading and authorization paths in the same program.

Exit condition: BM25, vector, graph, source authority, and cleanup semantics
have one implementation and one conformance suite.

### S4: One query executor

Status: pending.

- Centralize sparse/dense execution, fusion, graph expansion, reranking, and
  source validation behind one executor.
- Keep Wiki page diversification as a policy over the shared executor rather
  than another retrieval engine.

Exit condition: MCP search, native exploration, and Wiki evidence cannot drift
on the same plan and manifest.

### S5: Restore ownership boundaries

Status: pending.

- Move page graph projection out of `web` so Wiki does not import a Web adapter.
- Give publication contracts one owner and remove the
  `compiler`/`artifacts` package cycle.
- Split the CLI into command modules after the underlying ownership boundaries
  are stable.
- Add repository-wide import-boundary tests for the target direction.

### S6: Repeated workflow extraction

Status: pending.

- Replace repeated chunker traversal with declarative grammar profiles and
  narrow language hooks.
- Share dataset checkout/cache/filter machinery.
- Share strict view-publication state machines while retaining format-specific
  validation.
- Share agent-client lifecycle adapters without weakening SDK-specific safety.

## Decision Log

| Date | Decision | Evidence retained | Implementation disposition |
| --- | --- | --- | --- |
| 2026-08-24 | Begin the cognitive-debt program with pure subtraction | This baseline and the SCIP acceleration receipts | First subtraction iteration opened |
| 2026-08-24 | Treat failed Python chunk acceleration candidates as closed experiments | Exact revisions, parity, timings, RSS, and gate decisions remain in `scip_multilanguage_roadmap.md` | Remove POC/gate code, ABIs, commands, tests, and active instructions |
| 2026-08-24 | Retire three unowned retrieval evaluation scripts | RFC #133 remains closed and canonical retrieval metrics remain covered in `codenib.eval.retrieval_eval` | Remove the transitional comparator, manual smoke, and duplicate metric recomputation |
| 2026-08-24 | Start runtime convergence at manifest freshness | `RepoManifest.index_is_current` already owns commit, fingerprint, and source-selection identity; authenticated graph loading requires a `symbol_graph` entry | Delete the weaker Web-only freshness rule and the unusable vector-sidecar discovery path; add rejection regressions |
| 2026-09-01 | Make WikiStore the only product database and stop the generic backend program | Storage audit counts, protected-journey consumer tracing, and the one-table Wiki conformance results are summarized in `storage_backend_roadmap.md` | Remove the Web retained-storage vertical; freeze generic storage/CLI compatibility code and delete it by proven consumer; close PostgreSQL, S3, generic GC, and dynamic-registry plans |
| 2026-09-02 | Use the next release as the generic-storage removal boundary | The consumer audit found only the two retained artifact bridges and one catalog-backed FactBatch profile; ordinary product routes were independent | Delete the complete generic package and callers; keep v0.2.2 only as the pre-upgrade materialization tool |
| 2026-09-03 | Publish the subtraction as v0.2.3 and reserve `codenib.storage` for the Wiki database | `WikiStore` is the only product database contract, while the old package name remains the clearest public entry point | Add one lazy Wiki-only module with an exact export list; keep all generic submodules, protocols, catalogs, and CAS implementations retired |
| 2026-09-03 | Authorize one source-only BM25 H1 evidence experiment without reopening the stable storage boundary | `docs/experiments/hybrid_index_h1.md` records its schema, linearization points, verified failure matrix, commands, and diagnostic benchmark | Keep it outside the wheel and product routes; promote through an explicit demand-backed decision or delete code, script, and tests by 2026-10-04 |
| 2026-09-05 | End the one-release Wiki JSON compatibility window | The v0.2.3 release notes and #749 record the migration window; production compositions already inject the Wiki store | Delete the legacy implementation and compatibility tests; preserve SQLite data and the `WikiStore` API |

## Completion Gate

The program is complete only when:

- protected product journeys pass packaged acceptance;
- stable code has one persisted-view loader and one query executor;
- package dependency direction is enforced without lazy-import cycles;
- protected product paths do not import or configure generic storage machinery;
- every non-core surface has a lifecycle class and owner;
- failed experiments leave durable evidence but no dormant implementation;
- stable installation, public API, and required CI exclude labs-only work.
