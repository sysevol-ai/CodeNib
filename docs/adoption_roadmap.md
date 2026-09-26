# Adoption and distribution roadmap

Owner: CodeNib product, retrieval, and documentation maintainers.
Reviewed: 2026-09-26. Status: active.

## Outcome and product decision

A new visitor should understand why CodeNib helps their coding agent, see
source-linked output, and try it on their own repository. Public Wiki pages
are a shareable preview and an acquisition channel. The primary activation
event is a successful, source-linked agent query on the user's repository.

The next retrieval path is model-planned grep followed by Jev reranking,
using the user's OpenRouter account for both model operations. It must not
require embeddings, a GPU, or a completed semantic graph before the first
query. Typed graph navigation remains a separate strength, available when
its language provider is ready. BM25 remains supported; it is not the new
quality claim or the proposed default onboarding experience.

The shipping `codegraph init` path and the experimental grep/Jev result must
remain clearly distinguished until that route passes the product gates.
Using the caller's agent to plan grep is a separate, unevaluated variant.

## Audited starting point

| Surface | Current outcome | Open gate / dependency |
| --- | --- | --- |
| Agent setup | Released `codegraph init` prepares typed graphs. Source-only `codenib init` now connects OpenRouter and registers grep/Jev through native Claude Code/Codex CLIs, merged in #785. | Publish only after retrieval/auth gates; keep source-checkout instructions distinct from the released package. |
| Retrieval evidence | Historical research scores 58.62% → 71.40% Recall@5. Fresh product evaluation in merged [#787](https://github.com/sysevol-ai/CodeNib/pull/787) scores 48.37% → 65.57%, counting one failed case as zero across all 100 attempts. | Keep the two runs distinct; neither measures Claude Code token savings. The multiline correction is separately tracked in #792. |
| OpenRouter | Shared grep/Jev configuration and local PKCE are merged in #782/#783. The opt-in browser trial now uses direct provider calls and a short in-memory credential; real-source browser acceptance uses provider fixtures. | Actual provider consent, Linux desktop-vault acceptance, release and HTTPS-host verification remain. |
| Wiki demo | Source-grounded stories/graphs and static cached-story publication are merged in #772/#784. The real 15-page Requests corpus passes offline desktop/mobile browsing. An opt-in browser trial adds source-linked grep/Jev results without an operator key. | Producer fixes #789/#790/#791 are merged; browser trial review, actual consent and deployment remain. Live operator browsing/Ask can still intentionally generate. |
| Static distribution | Static export and reusable Pages workflow exist; #784 verifies cached stories, citations, page maps and CodeNib backlinks without a backend. A 15-page public repository is locally curated. | Deployment and hosted browser authorization remain; the no-embedding workflow default is implemented but not released. |
| MCP protocol | [#780](https://github.com/sysevol-ai/CodeNib/pull/780) merged as `bcd2c730` after all executed CI passed; its squash message was verified. [#779](https://github.com/sysevol-ai/CodeNib/issues/779) is closed. | The installed-version handshake fix is on main, not yet published; do not tell reporters it is released before publication. |
| GitHub discovery | `mcp`, `mcp-server`, `claude-code`, and `codex` are now set, with the six existing topics retained. | Verified through GitHub repository metadata on 2026-09-26. |
| Releases | Five GitHub releases exist; v0.2.3 is latest. `release.yml` already publishes GitHub assets after PyPI verification and MCP registry publication. | Improve visibility; inspect retry/failure behavior. Do not introduce a second release pipeline. |
| Related work | #777/#778 improve the Jev blog evidence; #773 changes blog layout. | Preserve their ownership; do not duplicate their chart or hardware corrections. #770/#752 are drafts and are not merge prerequisites. |

## Delivery order and acceptance gates

### A1 — Make the existing product understandable

Status: [#781](https://github.com/sysevol-ai/CodeNib/pull/781) merged into
`main` as `28910cc9`; production deployment verification remains. README has
two setup commands, an early authentic product image, a
sourced comparison and prominent language/reproduction links. Landing uses a
static preview instead of live demo iframes. Public method, comparison and
architecture pages are added. Strict MkDocs build, public-doc boundary audit,
pre-commit checks and desktop/mobile/no-JavaScript browser checks pass. The
landing page makes zero external requests and its setup links reach the two
commands without overflow. The recorded agent clip and source-linked poster
are merged in #788 (A4) as `dc2b4df0`, including user-controlled playback. A fresh
published-package recording and paired token comparison remain open;
production grep/Jev authorization remains in A2/A3.

- Rewrite README and website around finding source context for coding agents.
  Put the verified 71.4% Recall@5 result in the first screen with its dataset,
  comparator (+12.8 percentage points), experiment label, and public method.
  Never substitute issue resolution rate, token savings, or a Spark latency.
- Keep README Quickstart to installation and `codegraph init`. Link advanced
  source policies, prerequisites, diagnostics, Wiki setup, and architecture
  to docs. Do not silently change the shipped retrieval default in copy.
- Put an existing authentic product image above the long-form content now.
  Replace/augment it with the actual agent recording at A4, not fabricated
  terminal output or a mock represented as a real run.
- Add a compact, sourced comparison with grep/read, Serena, DeepWiki, and an
  explicitly identified CodeGraph project. Distinguish local tool execution
  from the agent's model, persisted typed graphs from LSP symbol navigation,
  and view reuse/rebuild from file-level incremental repair. The user's
  intended CodeGraph repository is still to be confirmed. The current table
  explicitly compares `Lordymine/codegraph` as one named example.
- Make the generated language matrix and pinned method reproductions
  prominent. Describe compatibility separately from reproduced paper scores.
- Add missing GitHub topics and direct GitHub Release links. Audit the
  existing publication chain before making any release automation change.

Acceptance: strict docs build and public-link audit; published claims traced
to committed results; desktop/mobile inspection of the actual landing page;
all first-screen links work; quickstart remains exactly two commands.

### A2 — Ship grep → Jev as a bounded product route

Status: [#782](https://github.com/sysevol-ai/CodeNib/pull/782) is merged into
`main` as `d6af8f79`, not yet released. CLI `codenib explore` and MCP share a
bounded source-only grep/Jev runtime, one OpenRouter credential, reported usage,
source/exclusion checks and cancellation. There are no automatic model retries;
existing indexed routes remain the released defaults.

The candidate audit in [#787](https://github.com/sysevol-ai/CodeNib/pull/787),
merged as `714218ab`, completes all 100 frozen plans with HTTP disabled. Ordered candidate text and
corrected spans match in 94 cases; six pools differ. Frozen-plan grep retains
58.62% Recall@5; aggregate frozen reranking is unset because three pools have
new unscored text. The separate range audit preserves the historical research
orderings' 58.62% / 71.40%.

Fresh product evaluation in #787 attempts all 100 cases: 99 succeed and one
rejects a multiline planned expression. Under the predeclared failure-zero
policy, Recall@5 is 48.37% for grep and 65.57% after Jev (+17.20 points;
repository-bootstrap 95% interval +8.88 to +25.44). Reported cost is
$1.043885892 with no unknown call charges. Every case and the separate earlier
incomplete attempt are retained without retries or substitutions. These are
localization results, not an agent/token benchmark.

The multiline correction is implemented in
[#792](https://github.com/sysevol-ai/CodeNib/pull/792), merged as `fbfe1fbb`
but not yet released.
Its 51 focused route tests pass, including Windows newline handling; the
preceding evaluation is not a measurement of that correction. Provider-consent acceptance, paired agent measurement,
default-route promotion and release reconciliation remain open. The 71.40%
historical research result stays distinct from the fresh product measurement.

- Extract the already measured plan/search/chunk/rerank path from the
  research runner into the existing repository-context runtime. Do not
  import experiment scripts from installed product code.
- Give CLI and MCP one explicit route/configuration. Plan regex/glob queries,
  execute `rg` locally without a shell, map hits to source-checked chunks,
  deduplicate/cap candidates, then call Jev Decisions. Preserve locations,
  source identity, cancellation, timeouts, and observable provider failures.
- Match the measured limits initially: at most six planned searches, 100
  candidates, 3,000 visible characters per candidate, batches of ten for Jev.
  Verify current runner defaults before freezing a product protocol.
- Use one OpenRouter credential/configuration for planning and reranking.
  Keep the backend dependency small; no default embedding download, GPU, or
  full graph build. Report explicit no-credential/offline behavior; do not
  quietly present BM25 as equivalent to the selected route.
- Avoid shell injection, arbitrary filesystem access, symlink escape, and
  ignored/excluded source leakage. Reuse existing source authority and path
  policy, not a second repository database or lifecycle.

Acceptance: deterministic tests exercise the real production route with
provider fixtures; source/candidate parity against the frozen experiment;
fresh CPU-only install; bounded timeout/cost/cancellation behavior; explicit
live smoke with recorded model identities before promoting it as default.

### A3 — Connect OpenRouter without collecting a master key

Status: local CLI/MCP authorization is implemented in
[#783](https://github.com/sysevol-ai/CodeNib/pull/783),
merged into `main` as `f2a805eb`; not yet released. Browser and headless PKCE use S256,
one-use in-memory verifiers and direct OpenRouter exchange. Saved keys resolve
for both retrieval stages. Default storage uses a supported OS keyring;
an explicit POSIX file fallback enforces 0700/0600, no links, no Git checkout
placement and atomic replacement. Imports reject management/provisioning keys.
Status and logout distinguish local presence, provider verification and provider
revocation; keys never appear in ordinary output or repository/MCP config.

Local callback behavior, provider metadata access and lightweight installation
are verified; detailed validation is recorded in #783. Logout handles malformed
files and unavailable keyrings explicitly. Credential preparation and logout
recover owned temporaries under the existing directory lock; a process-kill
test verifies recovery. Real provider consent and Linux desktop-vault acceptance remain
open. A real browser directly called OpenRouter key metadata, planning and
Decisions with an existing normal key; all succeeded without exposing it to
the local fixture server, DOM or browser storage. This establishes authenticated
transport, not provider consent or hosted product acceptance. Detailed transport
validation is recorded in #783.

Lightweight installed-package acceptance in
[#793](https://github.com/sysevol-ai/CodeNib/pull/793) passes on Linux, macOS and
Windows with PATH empty, no embedding/graph/model SDK packages and fixture-only
provider responses. macOS and Windows also pass real OS keyring save/read/delete
round trips with unique fake entries. The Windows source-read correction in
[#795](https://github.com/sysevol-ai/CodeNib/pull/795), merged as `873f0c64`, preserves lexical HANDLE
bindings for ancestors and full repository-root version/inventory checks.
Its deterministic regressions allow unrelated sibling creation and reject
repository mutation or an ancestor replacement. The combined platform check
passes; bundled-runtime release reconciliation and real provider consent remain open.

The browser trial in [#796](https://github.com/sysevol-ai/CodeNib/pull/796)
now exchanges a one-use S256 grant directly with OpenRouter
and holds its key in a private JavaScript field for up to ten minutes. No key
enters UI state, storage, URLs, exported assets or source-service requests.
The nonce-scoped callback supports denial, expiry, replay rejection and
disconnect without requiring a popup opener. Query submission is separate from
authorization. Reported-cost thresholds stop subsequent calls at $0.10/query
and $0.50/connection, with unknown costs and provider revocation stated
explicitly. These thresholds are not billing caps.

Real Chromium acceptance browses all 15 Requests pages under the opt-in CSP,
then exercises actual source capture, ripgrep and chunking with fixture-only
provider responses. Correct method spans link to the pinned commit; desktop
and mobile checks cover error/disconnect and absence of credentials from
DOM, browser storage, cookies and CodeNib HTTP requests. The CSP blocks inline
script execution, and callback responses have a separate restrictive policy.
Actual provider consent and production-host headers/callback behavior remain
open. The public browser-trial guide documents same-origin script trust,
callback grant logging, IP-proxy trust and the explicit deployment gate.
The trial branch passes 7,307 unit tests (35 skipped; 190 heavier tests
deselected), 95 frontend tests, the production frontend build, strict MkDocs,
the public-document audit and changed-file pre-commit. Provider fixtures
support these results; none makes a new quality or token-savings claim.

Native registration is merged in
[#785](https://github.com/sysevol-ai/CodeNib/pull/785) as `50215406`. `codenib init` checks or
requests authorization and registers source-only MCP through Claude Code/Codex
without indexing or model calls. Independent server names let CodeGraph coexist.
The existing pending receipt supports recovery and refuses unmanaged/drifted
configuration. Registration reloads ownership under the existing directory
lock and merges concurrent client selections. Status inspects each client
independently; uninstall preserves source and credentials. Native CLI setup
and MCP discovery are verified in isolated real clients; detailed versions
and checks live in #785. A real Claude Code query on pinned Requests source
used the source-only route, found both authentication/redirect methods and
returned verified source without creating an index. Source and user profiles
were preserved. This is a connectivity smoke, not a quality or token benchmark.
Provider consent and Linux desktop-vault acceptance remain open. The combined
installation tree passes 7,286 unit tests (35 skipped; 190 heavier tests
deselected), strict documentation checks and the public-document audit. Restacking
the bundled runtime onto both merged dependencies preserves that complete tree;
the remaining changes reconcile this roadmap.

The lightweight `grep` extra in
[#793](https://github.com/sysevol-ai/CodeNib/pull/793) includes `ripgrep-bin`.
CLI startup, source capture entry and the retriever use its installed executable even when the
agent has not activated the Python environment; an existing system `rg` is a
fallback. A clean Linux installation verifies actual source-linked retrieval
with PATH empty, no embedding/graph/model SDK packages and fixture-only provider
responses. The installed-package smoke is scheduled on Linux, macOS and Windows;
the latter two also exercise the real OS credential store with a unique fake
entry that is removed afterward. The three platform jobs and macOS/Windows
vault checks pass. Actual provider consent remains an acceptance gate. These installation checks do not establish
retrieval quality or agent token savings.

- Prefer OpenRouter OAuth PKCE (S256), using a fresh verifier and one-time
  local callback. Bind callback state to the initiating session, validate
  the callback host/path, and reject replay. Test against OpenRouter's actual
  authorization protocol rather than assuming generic OAuth fields work.
- Local CLI/MCP calls OpenRouter directly. Keep credentials in the OS keyring
  where available; offer an explicit permission-restricted user file only
  when needed. No credential in repositories, command arguments, telemetry,
  ordinary logs, exports, URLs, or browser `localStorage`.
- Show that query text and selected source snippets go to the configured
  planning/scoring providers. Explain the difference between local source
  search and remote model processing before connecting private code.
- Support disconnect/revocation and an application spending limit. Provider
  credit caps and application estimates are different guarantees; verify
  provider support for OAuth-created keys. Do not request a Management API
  key. An OAuth-issued key is not automatically ephemeral or narrowly scoped.
- For a hosted trial, first evaluate a browser session calling OpenRouter
  directly while the server supplies bounded, public-repository candidates.
  Gate on real CORS/Decisions support and XSS/CSP review. If that cannot work,
  make a session-only backend proxy explicit: it can read the key, keeps it
  out of logs/storage, limits lifetime and request budgets, and deletes it on
  disconnect. Do not claim encryption makes the key inaccessible to us.

Acceptance: success, denial, replay, expiry, cancellation and disconnect
tests; redaction coverage at observable HTTP/log/export boundaries; private
code disclosure; no automatic billed retry after budget exhaustion.

### A4 — Show the benefit on the user's own repository

Status: the actual CodeGraph/Claude Code recording and first-screen media are
merged in [#788](https://github.com/sysevol-ai/CodeNib/pull/788) as `dc2b4df0`,
independently of A2. The clip replays selected CLI output on pinned Requests source; waits
are condensed and source/profile preservation is verified. GIF, WebM, MP4 and
a static poster are served locally, with transcript/provenance and a renderer
that makes no model calls. The source build includes merged #780; a newly
published package install remains to be recorded. A paired agent/token study
and website deployment remain open. Detailed versions and validation
live in #788 and the public CodeGraph recording guide.

- Record about 15 seconds of real `codegraph init`, a Claude Code
  `explore_context` call, and source-linked results. Retain the repository,
  commit, command, tool result, and recording procedure. Label edited waits;
  a 15-second clip is not a promise of 15-second cold setup.
- Build a small opt-in comparison command on the existing evaluation
  machinery only after its metric contract is clear. Compare the same
  repository revision, tasks, model, and budgets with/without CodeNib.
  Report input/output/cache tokens separately, localization quality,
  retries/failures, wall time and recorded cost. A shorter tool response
  alone is not a measured end-to-end token saving.
- Publish one reproducible real-agent result before advertising token
  savings; retain the grep/Jev localization result until then.

Acceptance: the GIF contains actual tool output with usable source locations;
clean-repo two-command setup verified; user bench defaults never trigger
billed calls without an explicit opt-in; paired results include failures.

### A5 — Turn the Wiki demo into a preview and activation path

Status: story browsing is merged in
[#772](https://github.com/sysevol-ai/CodeNib/pull/772) as `f0a4cd00`; static
publication is merged in [#784](https://github.com/sysevol-ai/CodeNib/pull/784)
as `ae4225f9`, not yet deployed. A real 15-page Requests corpus exports from SQLite
with all pages generated and grounding-valid. Export verifies 159 citation
ranges, retains 148 inline excerpts and omits 11 credential-shaped previews
while preserving their repository links. Source/model identity, cache prompt
versions and file hashes accompany the static artifact.

Main includes the graph-boundary, evidence-serialization
and source-range fixes in [#789](https://github.com/sysevol-ai/CodeNib/pull/789),
[#790](https://github.com/sysevol-ai/CodeNib/pull/790) and
[#791](https://github.com/sysevol-ai/CodeNib/pull/791). Desktop and mobile checks
cover all 15 pages, citations, unavailable pages and the local-agent handoff,
with no backend/external requests, browser errors or horizontal overflow.
Export makes no network calls and preserves its source database/configuration.
The local corpus and export are verified, and their dependencies are merged.
An explicit `--trial-api-base` opt-in adds browser-owned OpenRouter queries.
The separate public source app exposes only published repository metadata and
candidate search, reusing the product source authority and grep/chunker path.
It has no credential exchange, model, generation, submission or indexing route.
Allowlisted source identities, origin/host checks, request/response caps and
one-process admission limits bound its work. Source workers own their slots
through cleanup; deterministic concurrent tests verify that limit.

Without the opt-in, static Ask keeps local-agent setup. Existing live Wiki Ask
is unchanged. Local browser acceptance uses real Requests source and fixture
provider calls, with no paid calls. Trial code review, actual provider consent,
release/deployment and broader corpus curation remain open.

The public demo must remain useful without authentication or a live LLM.

| User action | New behavior | Who pays / data boundary |
| --- | --- | --- |
| Open home | Curated repository cards, an example question/task per repository, languages, indexed commit, and a clear “Use with your agent” link. | Static assets; no model call. |
| Open a repository | Precomputed overview and story Wiki, architecture map and source citations. Choose an example task to inspect its recorded evidence. | Static assets; preserve source/model provenance. |
| Explore a map or citation | Browser renders exported page boundaries/graphs and navigates to commit-pinned source. | Static data; disclose missing full runtime graph functionality. |
| Enter a new question | Explain “Connect OpenRouter” and the snippet/cost boundary, or “Run on your machine”. Do not spend the site's key on anonymous requests. | User-authorized provider account; bounded request. |
| Connect an agent | Copy a tested local setup now. Add remote MCP only after A6 passes. | Local user machine and user's agent/model. |
| Submit an arbitrary repository | Initially link to local setup or the existing self-owned Pages workflow. | User CPU/CI quota; hosted indexing remains demand-gated. |

Implementation boundaries:

- Separate public-preview mode from the existing local Wiki server. A local
  user may intentionally configure their own backend model; do not break it
  by globally disabling Ask or generation.
- Public navigation must not invoke outline generation, Wiki generation,
  an LLM fallback, or graph label generation. Missing precomputed pages show
  a useful unavailable state. Offline generation runs with an explicit
  operator budget and a versioned input/model/prompt identity.
- Reuse the SQLiteWikiStore facade for Wiki persistence and the existing
  static exporter. Verify story prose, source snippets, page boundaries and
  system-map export parity with #772; deterministic WikiBuilder output is
  not automatically the generated story cache.
- Do not embed all live demo pages as landing-page iframes: use a static
  preview, then let a deliberate click open the demo. Visits should not
  implicitly hit Spark or start model work.
- Distinguish public examples, session-specific Ask results, and saved
  operator-generated Wiki content. A visitor's key must not silently finance
  shared Wiki generation or persist its result across users.
- Check desktop/mobile, empty/error/auth/budget states and keyboard/source
  navigation. Test the static build with backend access blocked.

Acceptance: browse all exported pages with zero inference calls; 0 backend
requests from the marketing preview; reliable commit-pinned citations;
successful disconnect and budget exhaustion; one clear action from preview
to local agent activation. The public site remains usable during Spark outage.

### A6 — Expand distribution with measured demand

Status: the existing Pages workflow and composite Action default to `fast`,
which produces a static index-derived preview and portable BM25 artifact without
an embedding download. The real publication smoke exercises this default and
checks the resulting artifact views. The public example passes `preset: fast`
explicitly so the model-free path also works with released v0.2.3; the changed
workflow default is not released yet. Semantic views remain opt-in. This does
not substitute BM25 for the selected grep/Jev query route. Cached-story
generation in Actions, curated-corpus expansion and hosted services remain separate gates.

1. Improve the existing Pages workflow and generated-site backlink. Pin all
   external actions. Offer a no-embedding preview and explicit OpenRouter
   generation using repository Actions secrets; never export credentials.
2. Start with 5–10 useful public repositories, covering the measured language
   groups. Precompute pages and graph payloads once, publish static assets,
   measure click-through and successful agent starts, then consider 100–200.
   Check size, source licensing and refresh cost before each expansion.
3. Prototype remote MCP for that curated corpus only. Reuse existing query
   runtime over commit-bound artifacts; do not add a generic storage system.
   Test Streamable HTTP, OAuth/bearer behavior, origin/host validation,
   per-user/IP limits, request/result caps, timeouts and disabled arbitrary
   tool execution. Most model generation stays with the user's agent, but
   OpenRouter planning/Jev remain paid operations requiring their own budget.
4. Add arbitrary hosted indexing only after demand justifies it: repository
   access verification, size/time limits, daily global cap, bounded queue and
   cancellation. “Request a repository” is the first lower-cost alternative.

Acceptance: static traffic does not reach Spark; artifact commit verification
survives updates; generated backlinks work; a real remote MCP client connects
before publishing `mcp.codenib.ai` setup instructions. No speculative service
or unbounded inference is required to finish A1–A5.

## Measurement and rollout

Record baseline and subsequent counts for documentation-to-install clicks,
successful local setup, first source-linked query, repeat use, Pages backlink
visits, and OpenRouter connection completion. Prefer opt-in local reports and
aggregate web events; do not collect code, prompts or credentials. Establish
baseline measurements before assigning numerical conversion targets.

Release in focused PRs: evidence/onboarding, production retrieval,
authorization, demo mode/export, and distribution. Keep native onboarding
stacked on #783; reconcile the Wiki producer fixes before #784. Each milestone lists current
outcomes, remaining gates and PRs rather than an implementation diary.

The broad goal stays active until the requested core surface is implemented,
locally verified, documented, and reconciled with relevant PRs/issues. A draft
plan, one merged README, or a green unit suite alone is not completion.

## Evidence and external contracts

- Public [grep/Jev report](https://codenib.ai/blogs/jev-model-grep-reranking/);
  internal committed source: `docs/experiments/jev_dense.md`, results JSON
  and per-case CSV. Keep excluded experiment paths out of public-page links.
- [OpenRouter OAuth PKCE](https://openrouter.ai/docs/guides/overview/auth/oauth)
  and [Jev Decisions](https://openrouter.ai/docs/guides/community/jev).
- [Language capabilities](language_capabilities.md),
  [method compatibility](agent_integrations.md), and
  [DGX Spark deployment](guides/reference-deployments/dgx-spark.md).
  A deployment recipe is not evidence of a token-saving claim.
