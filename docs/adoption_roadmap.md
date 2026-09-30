# Adoption and distribution roadmap

Owner: CodeNib product, retrieval, and documentation maintainers.
Reviewed: 2026-09-29. Status: active.

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
| Agent setup | Released `codegraph init` prepares typed graphs. Source-only `codenib init` now connects OpenRouter and registers grep/Jev through native Claude Code/Codex CLIs, merged in #785. | Published 0.2.4 installation guides use the package; promote OAuth setup after actual provider-consent acceptance. |
| Retrieval evidence | Historical research scores 58.62% → 71.40% Recall@5. Fresh product evaluation in merged [#787](https://github.com/sysevol-ai/CodeNib/pull/787) scores 48.37% → 65.57%, counting one failed case as zero across all 100 attempts. | Keep the two runs distinct; neither measures Claude Code token savings. The multiline correction is separately tracked in #792. |
| OpenRouter | Shared grep/Jev configuration and local PKCE are merged in #782/#783. The opt-in browser trial uses direct provider calls and a short in-memory credential; real-source browser retrieval acceptance uses provider fixtures. Published 0.2.4 CLI retrieval and an 11-chapter visitor Wiki pass with the maintainer’s existing OpenRouter key. Real Linux SecretService and macOS/Windows vault round trips pass with fake entries. | The maintainer chose desktop CLI consent acceptance first; interactive unlock and hosted browser acceptance remain. |
| Wiki demo | Source-grounded stories/graphs and static cached-story publication are merged in #772/#784. The real 15-page Requests corpus passes offline desktop/mobile browsing. An opt-in browser trial adds source-linked grep/Jev results without an operator key. | The maintainer deployed the live Wiki. The #801 short-explanation path is superseded by full visitor Wiki generation, persistence and progress; actual PKCE consent remains unverified. Live operator browsing/Ask can still intentionally generate. |
| Live Wiki cache and maps | Runtime fixes in [#800](https://github.com/sysevol-ai/CodeNib/pull/800) batch authenticated source reads, retry busy generators, and build System Maps from cached outline files plus indexed references before child prose exists. Overview illustrations require admitted architecture plans; unrelated call-flow fallbacks are omitted. Deployed desktop/mobile checks pass, with maps available for 23/27 repositories. After full prewarming, the 2026-09-26 audit reports 1,001 ready pages, 19 degraded pages, and no cold pages across 1,020 entries. | jq/MicroPython graph artifacts fail captured-source validation; Axios/Preact graph coverage lacks usable cross-area implementation references. Repair these four index artifacts and resolve the 19 page-quality failures before declaring the whole demo ready. These data-quality gates remain separate from the runtime fixes. |
| Static distribution | Static export and reusable Pages workflow exist; #784 verifies cached stories, citations, page maps and CodeNib backlinks without a backend. A 15-page public repository is locally curated. | The no-embedding workflow default is released in 0.2.4; the maintainer will deploy the static roots on DGX Spark later. |
| MCP protocol | [#780](https://github.com/sysevol-ai/CodeNib/pull/780) merged as `bcd2c730` after all executed CI passed; its squash message was verified. [#779](https://github.com/sysevol-ai/CodeNib/issues/779) is closed. | Released in 0.2.4; the public PyPI wheel passes installed modern and legacy MCP identity checks. |
| GitHub discovery | `mcp`, `mcp-server`, `claude-code`, and `codex` are set, with the six existing topics retained. The About description now names source context, call navigation, Claude Code, Codex and MCP. | Description, topics and unchanged homepage verified through GitHub metadata on 2026-09-26. |
| Releases | [v0.2.4](https://github.com/sysevol-ai/CodeNib/releases/tag/v0.2.4) is published on PyPI, the official MCP Registry and GitHub, marked latest. Tag and accepted TestPyPI commit are `3f3bc2cf44ba83d1d86e9e5eb74f8da0f8bc63ac`. Both wheels and the sdist match across PyPI, GitHub and SHA256SUMS. | Protected production run [36252998442](https://github.com/sysevol-ai/CodeNib/actions/runs/36252998442) passed. Post-publication docs #798 update installation pins and navigation; real consent and maintainer-owned website deployment remain separate gates. |
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
  Lead the website with a usable, source-pinned example and a clear Wiki entry.
  Keep the verified 71.4% Recall@5 result below the first interaction, with its dataset,
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
  and view reuse/rebuild from file-level incremental repair. The user confirmed
  `colbymchenry/codegraph`; README and the detailed table use that project,
  checked at `ba3c21e50d9129d2f5f3843ec3728868ae6d47a1`. Its local typed graph,
  broad language support and incremental watcher are shared or competing
  capabilities, not gaps to claim. Document its telemetry controls and keep
  upstream agent-token results separate from CodeNib retrieval measurements.
- Make the generated language matrix and pinned method reproductions
  prominent. Describe compatibility separately from reproduced paper scores.
- Add missing GitHub topics and direct GitHub Release links. Audit the
  existing publication chain before making any release automation change.

Acceptance: strict docs build and public-link audit; published claims traced
to committed results; desktop/mobile inspection of the actual landing page;
all first-screen links work; quickstart remains exactly two commands.

#### First-visit activation

Status: the first-visit frontend is publicly deployed at
[codenib.ai](https://codenib.ai/) and [demo.codenib.ai](https://demo.codenib.ai/)
from `2fc8e451` as of 2026-09-28. Its implementation in
[#809](https://github.com/sysevol-ai/CodeNib/pull/809) remains unmerged, stacked
after #808 and its #803 → #804 → #805 → #806 → #807 dependencies. This gives
the marketing and live Wiki homepages one shared, self-contained Requests
question. Four source-pinned symbols and three indexed occurrence anchors
are clickable without an account, model call, graph request or live source
fetch. Mobile reading starts with that example before the repository form.
The example leads into the full existing Wiki and keeps the local-agent setup
and authentic CLI recording available. The research metric is supporting
evidence below the first interaction, not the product's opening promise.

The natural `/owner/repo` route and marketing `?repo=` form resolve against
the ready catalog before offering the existing visitor Wiki generation flow.
Aliases replace browser history, retain chapter links, and expose retry on
catalog failure. They do not infer a missing Wiki from a failed catalog read.
Generation remains explicitly user-funded; this change does not claim free
arbitrary-repository generation, new graph coverage or faster first chapters.
Example links and a rendered homepage social card are available; individual
Wiki social previews remain open.

Local acceptance passes all three source links, keyboard operation, 1440/390/320
pixel layouts, dark mode, reduced motion, catalog-outage exploration, aliases,
refresh and Back navigation. Syntax-colored excerpts retain their exact source
text and aligned call highlights. Switching nodes/calls keeps the card, adjacent
heading and page scroll position stable; long source scrolls inside a fixed-height
pane. The Web unit surface has 608 passing tests and one
skip; all 142 frontend tests pass. Production build, strict MkDocs and the
public-doc boundary pass. Opt-in measurement records fixed events only, with
tab-session deduplication, privacy-signal suppression and a bounded log collector
that stops reading uploads as soon as its 256-byte limit is exceeded.
Events exclude user input, URLs, source and credentials. Counts are partial
observations, separate by origin; source clicking and Wiki reading are leading
indicators, while a successful own-repository agent query remains activation.

Public desktop/mobile acceptance passes source syntax and stable selection,
the marketing repository form's handoff to the prepared Wiki, natural paths,
chapter refresh, the new-repository generation form, homepage social-image
bytes/content type, and the existing blog index. Browsers report no page errors
or failed resources, and verification sends no generation requests. The live
generation API remains on `c7782a9a` without a process restart; the four saved
visitor attempts remain complete. The old frontend and static release remain
available through the deployment's rollback record. The optional event collector
is not deployed or enabled; both public frontend collection switches remain off.

The production frontend opens the real cached Requests redirect chapter
from the example with read-only requests. Existing visitor stop/resume,
refresh, sharing and polling checks pass on desktop and mobile after correcting
their stale pre-#808 graph-response fixture; the paused-run failure reproduced
on the unchanged dependency tip as well. The acceptance criteria are unchanged.

Open gates: merge/restack the dependency chain; deploy and enable hosted
measurement with the matching collector and log-retention policy; establish
a baseline and observe 5–8 unfamiliar users before claiming improved conversion.
Arbitrary-repository cost/latency, broader question examples and per-Wiki social
previews need separate evidence. Existing generation, consent, corpus-quality
and non-Python graph gates remain in A3/A5.

### A2 — Ship grep → Jev as a bounded product route

Status: [#782](https://github.com/sysevol-ai/CodeNib/pull/782) is merged into
`main` as `d6af8f79` and ships as an opt-in route in 0.2.4. CLI `codenib explore` and MCP share a
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
and ships in 0.2.4.
Its 51 focused route tests pass, including Windows newline handling; the
preceding evaluation is not a measurement of that correction. Provider-consent acceptance, paired agent measurement,
default-route promotion remain open. The 71.40%
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
merged into `main` as `f2a805eb` and released in 0.2.4. Browser and headless PKCE use S256,
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
test verifies recovery. A clean installed package also passes real Linux
SecretService save/read/delete against GNOME Keyring 46.1 in a private D-Bus
session and data directory. It uses the unchanged installed auth module, leaves
zero fake entries and never accesses the existing desktop vault or a model.
Interactive desktop unlock and real provider consent remain open.
A real browser directly called OpenRouter key metadata, planning and
Decisions with an existing normal key; all succeeded without exposing it to
the local fixture server, DOM or browser storage. This establishes authenticated
transport, not provider consent or hosted product acceptance. Detailed transport
validation is recorded in #783.

Lightweight installed-package acceptance in
[#793](https://github.com/sysevol-ai/CodeNib/pull/793), merged as `377ea365`,
passes on Linux, macOS and Windows with PATH empty, no embedding/graph/model
SDK packages and fixture-only
provider responses. macOS and Windows also pass real OS keyring save/read/delete
round trips with unique fake entries. The Windows source-read correction in
[#795](https://github.com/sysevol-ai/CodeNib/pull/795), merged as `873f0c64`, preserves lexical HANDLE
bindings for ancestors and full repository-root version/inventory checks.
Its deterministic regressions allow unrelated sibling creation and reject
repository mutation or an ancestor replacement. The combined platform check
passes. Its complete release-artifact, installation and unit checks passed
before merge and the package is published in 0.2.4; real provider consent remains open.

The browser trial in [#796](https://github.com/sysevol-ai/CodeNib/pull/796),
merged as `56472bfe`, exchanges a one-use S256 grant directly with OpenRouter
and holds its key in a private JavaScript field for up to ten minutes. No key
enters UI state, storage, URLs, exported assets or source-service requests.
The nonce-scoped callback supports denial, expiry, replay rejection and
disconnect without requiring a popup opener. Provider metadata must explicitly
confirm an inference key. If exchange or verification fails, the UI retains a
safe provider settings link for revocation, including cancellation and local
agent handoff. Query submission is separate from authorization. Reported-cost
thresholds stop subsequent calls at $0.10/query
and $0.50/connection, with unknown costs and provider revocation stated
explicitly. These thresholds are not billing caps.

Real Chromium acceptance browses all 15 Requests pages under the opt-in CSP,
then enters through the mobile Wiki header and desktop question bar and
exercises actual source capture, ripgrep and chunking with fixture-only provider
responses. Correct method spans link to the pinned commit. Desktop/mobile
checks cover error/disconnect, failed-grant revocation guidance and absence of
credentials from DOM, browser storage, cookies and CodeNib HTTP requests.
The CSP blocks inline
script execution, and callback responses have a separate restrictive policy.
Actual provider consent and production-host headers/callback behavior remain
open. The public browser-trial guide documents same-origin script trust,
callback grant logging, IP-proxy trust and the explicit deployment gate.
The reviewed trial tree includes merged bundled runtime #793 and passes 7,318
unit tests (35 skipped; 190 heavier tests deselected; umask 022), 108 frontend
tests, the production frontend build, strict MkDocs, the public-document audit
and changed-file pre-commit. Provider fixtures
support these results; none makes a new quality or token-savings claim. A fresh
Linux installed-package check runs both MCP retrieval and the public source
API with PATH empty and no model/graph SDKs, credentials or real provider
calls. The source-API check passes on Linux, macOS and Windows in the installation
job. Browser acceptance uses `localhost` against a real IPv6 listener. Export,
service configuration and frontend admission consistently reject IPv6 literals:
browser CSP host sources cannot match them. The guide documents the hostname
route; the script and connection policies remain restrictive. All five review
threads are addressed. All executed checks passed on the accepted head, including
three-platform installed-package coverage, manylinux artifact verification and
Python 3.10–3.14 ABI3 smokes. The merge message was inspected; actual provider
consent and deployment remain open.

Published-package installation guides in
[#798](https://github.com/sysevol-ai/CodeNib/pull/798) use verified 0.2.4 and
its bundled frontend. PyPI, Registry and GitHub Release publication are
accepted; authorization and hosted-trial promotion gates remain explicit.
The maintainer selected real desktop CLI login acceptance before browser
trial acceptance. A fresh installation of the CI-built manylinux x86-64 wheel needs only
the `grep` extra to serve real HTTP planning metadata and source candidates:
MCP, auth, graph and model SDK packages are absent, credential headers are
rejected, and no provider call or credential read occurs.

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
Provider consent and interactive desktop unlock remain open. The combined
installation tree passes 7,286 unit tests (35 skipped; 190 heavier tests
deselected), strict documentation checks and the public-document audit.
Bundled runtime #793 includes both merged dependencies and preserves that
verified product tree.

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

### A5 — Generate complete Repo Wikis and activate coding agents

Status: navigable visitor Wikis, incremental reading and explicit community
publication are on `main`. The current hosted-feedback correction batch is
pending merge/deployment: function declarations in landing/demo excerpts
([#821](https://github.com/sysevol-ai/CodeNib/pull/821)), nested Python indexing
and saved-map backfill ([#820](https://github.com/sysevol-ai/CodeNib/pull/820)),
and upfront source admission with saved-map catalog thumbnails. Landing now
states the hosted size limits; the generation form checks the actual archive
without an API key, saved attempt, index or model call, and gates key entry on
the result. Limits/languages come from the production source constants and
registry. Community and My Wikis cards read their saved System Map, retaining
owner opt-in publication and the separate Featured collection.

The SGLang default branch at `8854857a991e06b3ef9c4caeae34c94fc3588336`
fails the actual 20 MiB hosted archive download check before model calls.
The public smolagents Wiki at `227ef5e49ddd82339295939072f0223249aa8d38`
still reports `index_failed` on the deployed service. Repairing that saved
map and verifying declarations, thumbnails and source rejection on both live
sites remain deployment acceptance gates. This batch makes no paid generation
calls and does not claim broader hosted graph-language coverage.

The source-excerpt correction in
[#821](https://github.com/sysevol-ai/CodeNib/pull/821) is pending merge/deployment.
Landing and Wiki excerpts retain a function declaration above cropped body
lines, with an explicit gap and real file line numbers. Long decorated Python
signatures retain the `def` line; highlight ranges are bounded by actual excerpt
length. Cached prepared pages upgrade through a versioned, model-free refresh
without regenerating prose. Saved visitor pages derive declarations from their
saved citation source without a download or stored-prose write. Live
desktop/mobile declaration checks remain a deployment gate.

Story browsing is merged in
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
An explicit `--trial-api-base` opt-in adds browser-owned OpenRouter queries to
cached story exports with an explicit public repository slug and full commit.
Index-derived previews retain local-agent setup; they reject that opt-in before
repository work because their directory label is not a public source identity.
Wiki header, footer and question-bar controls expose the opted-in trial.
The separate public source app exposes only published repository metadata and
candidate search, reusing the product source authority and grep/chunker path.
It has no credential exchange, model, generation, submission or indexing route.
Allowlisted source identities, origin/host checks, request/response caps and
one-process admission limits bound its work. Source workers own their slots
through cleanup; deterministic concurrent tests verify that limit.

Without the opt-in, static Ask keeps local-agent setup. Existing live Wiki Ask
is unchanged. Local browser acceptance uses real Requests source and fixture
provider calls, with no paid calls. Trial code review is complete in merged
#796 and is released in 0.2.4; actual provider consent, deployment and broader corpus curation
remain open. The CI-built 0.2.4 base wheel exports all 15 cached pages using
its packaged frontend without a CodeNib checkout, frontend override, Node.js,
MCP/auth/graph/model SDKs or a network request. The same installed CLI with
only the `grep` extra also exports the optional trial with a placeholder HTTPS
origin. Both exports leave SQLite, configuration and registry bytes unchanged;
the trial output is acceptance material, not a public deployment.

The maintainer has deployed the live Wiki on their separate DGX Spark.
The acceptance target is a complete, navigable Repo Wiki from an arbitrary
public GitHub URL. #801's bounded source preview and optional one-call
explanation did not meet that target. Its 0.930-second Flask source preview
and $0.025725 explanation are historical measurements, not Wiki generation
latency or cost. The explanation implementation and its dedicated harness
are removed in the visitor-Wiki iteration.

Current iteration: merged in
[#802](https://github.com/sysevol-ai/CodeNib/pull/802), deployed at `63cb8374`
with visitor generation enabled and public desktop/mobile entry checks passing.
URL entry now uses the existing AgentWiki
pipeline with user-funded OpenRouter grep/Jev retrieval, a chapter sidebar,
actual stage/page progress, incremental reading and persisted results. The
maintainer authorized link-readable, unlisted server persistence. Visitors
explicitly consent to that publication and to a server-memory credential for
one bounded generation run; the browser-owned Ask trial is unchanged.
The visitor Wiki database uses the existing WikiStore facade. Temporary
source is bounded and never executed. Refresh reads saved state, while an
explicit owner resume reuses healthy pages without billing them again.

Local acceptance covers archive limits, persistence, cancellation, service
restart/resume, credential isolation, cost stops and a deterministic competing
owner race. Rejected repositories consume no saved-attempt slots; stopping a
queued resume cancels that request before generation. Completed Wikis stop
polling, and a timed-out contender cannot overwrite active progress.
AgentWiki cache identity survives a fresh temporary source download;
ready chapters are recovered without another model call. Provider-free browser
checks cover desktop/mobile creation, progress, stop/resume, read-only sharing
and refresh with no credential persistence.
The complete local unit tier passes with **7,355 passed**, 35 skipped and 190
excluded by marker; the frontend has **138 passing tests**. Production build,
pre-commit, strict MkDocs and public documentation checks pass.

A real run against `pallets/itsdangerous` at
`672971d66a2ef9f85151e53283113f33d642dabd` completed **11/11 chapters**, all
passing source/quality gates: **45 calls**, **$0.838654398** reported by
OpenRouter, first readable chapter in **about 170 seconds**, full completion in
**643.23 seconds**. The $2 run limit was not reached; no charges were unknown.
All 11 persisted chapters pass desktop/mobile navigation, source-citation and
refresh acceptance with only same-origin reads and no model request. These are
one local run's measurements, not a hosted latency guarantee. Source-only
visitor Wikis from that run predate the bounded Python graph path below.
The deployment updates both backend and frontend and enables
`CODENIB_VISITOR_WIKI=1`; the published 0.2.4 wheel predates this path.
Hosted feedback exposed a source admission failure for `AMA-Bench/AMA-Bench`
at `ddfd319e0be33424288c13806f1eafc63e625b59`: a 7.2 MiB PNG stopped the run
before any model call, while empty-chapter UI still implied work was pending.
Oversized files are now skipped before decompression and their paths/sizes
persist with the attempt. Replaying that source preparation retains 65 files
and produces 304 chunks without inference. Stopped runs prominently display
the reason, stop polling, and require an explicit owner retry. Archive path,
retained-size and member-count limits remain enforced. The owner's hosted retry
subsequently completed **14/14 chapters**, all passing the existing grounding
and quality gates: **58 calls**, **$1.177581516** reported cost, no unknown charge,
and **886.09 seconds** from retry start to completion. This establishes hosted
completion for this pinned repository; source preparation alone did not.

New visitor Wikis use a **focused** reading profile: Overview plus at most four
core chapters, with a separate cache identity from full and legacy concise
profiles. The generation form defaults to DeepSeek V4.1 Flash and offers Sonnet
4.6. Model and scope stay pinned on resume. The previous four-claim detail-page
cap was too aggressive: source checks passed while the explanation lost key
mechanisms and sometimes left a fact under the wrong heading. Detail pages now
keep the supported argument and short explanatory leads, remove duplicate
framing/tables/excerpts, and receive one bounded editorial revision when the
existing call budget allows. On new runs, that revision inspects rendered prose
for repeated claims and helper inventories, not just the JSON plan. Revised
plans pass normal source/quality admission. The final page receives the same
newcomer reading rubric as prepared examples; it remains a diagnostic, not a
replacement for source admission or a guarantee of prose quality.
Cancellation and unknown charges cannot fall through to another billed call.

The same native architecture component serves prepared and visitor Wikis.
Existing saved semantic architecture plans become visible without another model
call or source download, with source buttons on roles and connections. These
remain model-authored, source-grounded architecture explanations, not the
compiler-indexed maps. Hosted Python generation now also runs the existing
SCIP indexer against a source-only scratch copy before planning; indexed
relations are available to AgentWiki. It saves the same bounded graph/source
projections used by the prepared reader. CodeGraph opens without a model call
and has pinned source previews. System Map displays indexed chapter regions
even when no cross-chapter calls are recorded, with an explicit explanation.
Completed Wiki pages collapse generation diagnostics and lead with readable content.

Nested Python source indexing and saved-map repair are implemented in
[#820](https://github.com/sysevol-ai/CodeNib/pull/820), pending merge/deployment.
The old Pyright include pattern indexed no files in `src/` layouts. The repair
indexes the captured scratch root and rebuilds saved graphs at each Wiki's
pinned commit, under the existing generation guard, without model calls or
changes to prose, cost or visibility. The operator dry run reuses the private
read-only maintenance snapshot and leaves source database/lock files untouched.
At the saved smolagents commit, a production-database copy now has 75 indexed
files, 2,544 symbols, 6,636 edges, four map regions and three links; all 106
non-graph envelopes remain unchanged. Applying and checking that repair on the
live result remains an explicit deployment acceptance gate.

Browse presents featured and explicitly published community Wikis, plus the
owner's My Wikis collection. The homepage uses Continue reading cards with
chapter status, removing attempt IDs and storage-oriented copy. Only the owner
can publish or unpublish; only completed, source-checked pages can be listed.
Older unlisted Wikis stay unlisted. A reader in a fresh browser can discover a
published Wiki without credentials; unpublishing preserves the existing share
link. The WikiStore publication envelope is separate from progress so late
worker saves cannot undo an unpublish. Local publication and browser acceptance
cover these boundaries without provider calls. My Wikis still uses browser
recovery access; this iteration does not add accounts or cross-device ownership.
Completed owner pages explicitly explain that the result is saved in My Wikis
and still unlisted, with **Publish to Community** as the main action. My Wikis
cards show unlisted status and a publication hint; the form explains the next
step before generation. Failed publication stays unlisted and can be retried.

Real workload evidence: the owner's Flash generation of
`fishmingyu/qrv2-gpu-mode` completed four chapters in **42.76 seconds**, with
**22 OpenRouter calls** and **$0.025278336** reported cost. Its short detail pages
were 224/174/259 whitespace-separated words. This proves production-route
completion speed on one repository, not reader-quality parity with the gallery.

The pipeline audit found concrete differences despite both routes using
`AgentWiki`:

| Surface | Prepared examples | Visitor Wikis |
| --- | --- | --- |
| Source retrieval | Prepared BM25/dense artifacts as available, outline anchors and indexed relations | Model-planned grep/Jev and tree-sitter anchors; bounded SCIP relations for Python when indexing succeeds |
| Search task | Chapter query against the index | New runs use a chapter-specific prompt; old runs used the issue-location prompt |
| Supporting source | Outline and context can include tests | New chapter search includes tests; the legacy grep filter silently excluded them |
| Reader assessment | Enabled by default; diagnostic | Enabled for new runs; absent from the two existing saved Wikis |
| Reading scope | Fuller outline with child chapters | A small chapter list; legacy concise detail claims were also capped |

New attempts pin `wiki_grep_jev_v2` as their retrieval/cache identity. Old
attempts retain their previous retrieval policy and review budget on resume.
The ordinary issue-search, MCP and browser-trial defaults remain unchanged.
A deterministic audit of the pinned Requests source with the same redirect-test
search produces **0 vs 23** grep candidates and **2 vs 12** writer evidence
items before/after enabling supporting source. This isolates the filtering bug;
it includes no model call and does not measure final ranking or prose quality.
It uses the existing pinned checkout and excludes hosted archive admission.

The prose comparison freezes the existing QR run's commit, outline and retrieved
evidence and uses DeepSeek's official API. All four newly composed pages pass
ordinary source/structure checks. The reader rubric gives 7/8, 5/8, 3/8 and 8/8;
the recursive-panel chapter still fails its failure/boundary explanation. Three
of four pass the rubric. Re-reviewing the prior focused output also found
repetition despite passing source checks: extra words alone did not fix quality.
These are model judgements on one repository, not an independent human grade.
The comparison excludes retrieval, benefits from warmed provider caching and
is **not** a new OpenRouter end-to-end timing result. Broader reader-quality
assessment, non-Python hosted graph coverage, and runtime/embedded-kernel
relationships remain open gates. Do not claim gallery parity or invent edges.

The regenerated QR Wiki at commit `527ec55cfffeed8eefa5e1500c80d00265e99721`
exposed a separate render failure: the label “Compact `(H, tau)` artifact” contained inline
code typography and caused the whole architecture contract to be rejected.
The deterministic repair now flattens inline typography inside prose role labels
while still rejecting labels that consist only of source symbols or paths. The
existing six-role plan renders without rewriting prose or using a model. Missing
architecture and graph failures are explicit in the reader instead of silent
omissions behind a ready chapter.

A same-commit source/graph probe took **3.29 seconds**, including download,
chunking, indexing and four reader projections, with no model call. SCIP emitted
365 nodes and 1,345 edges; these include containment and references, not 1,345
calls. Four bounded chapter CodeGraphs are available. The initial Overview view is
limited to its cited symbols and shared bridges (12 nodes / 5 edges), rather
than a large neighbourhood dominated by uncited implementation helpers. No direct cross-chapter
connections were found for the cited symbols; the three System Map regions
therefore have no invented arrows. Python indexing is capped at 60 seconds and
16 MiB of SCIP output; cancellation terminates the owned child process. It uses
only captured Python source, an empty external-package environment, and no
repository configuration, package installation or repository execution.
Unsupported languages, missing tools, timeout and projection failure leave the
source-checked prose available and display the missing graph coverage.

The existing QR result was supplemented under the generation guard, preserving
all four page envelopes, cost accounting and publication state. No visitor key
is reused and old results are never automatically published. Tests cover the
reported typography case, exact reference/source previews, isolated indexing,
stop/timeout cleanup, and read-only persisted graph routes. The Wiki/Web/grep
unit tier passes **1,348 tests**, with one skip; frontend tests pass **142**.
The prepared cache audit preserves 1,001 ready and 19 pre-existing retryable
pages across 27 repositories, with 23 existing System Maps available.

Live feedback still distinguishes model response receipt, worker progress and
server connectivity, with immediate submit feedback, progressive chapter reads,
45-second waiting explanation and stopped animation after 180 seconds without
progress. The extra editorial pass reports its actual review stage. Refresh and
reconnect remain read-only, and existing paid runs must drain before deployment.
The review chain is #803 → #804 → #805 →
[#806](https://github.com/sysevol-ai/CodeNib/pull/806) →
[#807](https://github.com/sysevol-ai/CodeNib/pull/807) →
[#808](https://github.com/sysevol-ai/CodeNib/pull/808); restack it in that order
before merging. Deployment does not imply the stack is merged.

The community, visitor-quality and #808 graph fixes are deployed as of
2026-09-27. The reported QR result now renders its saved six-role architecture,
three indexed chapter regions, and four CodeGraphs at its original URL. Public
desktop/mobile checks pass without provider calls. The graph-node preview,
pinned source link and map-failure retry were also exercised. The three saved
visitor Wikis were preserved, with no automatic publication or prose rewrite.
The main services run `ba27c81b`; the verified staging services were drained and
removed after cutover, and the previous asset hashes and database snapshots are
retained for recovery.
Acceptance passes 1,341 Wiki/web/grep-MCP unit tests (one skipped), 142 frontend
tests, production build, strict docs and the provider-free desktop/mobile
publication and waiting matrices. Publication acceptance includes the completion
invitation and failure/retry path. Public browser checks render the existing visitor architecture with
seven roles and pinned-source navigation, keep completed diagnostics collapsed,
preserve unlisted visibility and refresh without mutations. The two existing
visitor Wikis and all 27 prepared repositories remain readable. Cache coverage
is unchanged: 1,001 ready pages, 19 pre-existing retryable pages, zero cold pages
and 23/27 indexed System Maps available. Database snapshots, service backups and
prior frontend assets are retained; visitor work was idle before the backend
restart. The previous concise prose is not automatically rewritten: owners can
start a new version from the result header with a new funded request.

| User action | New behavior | Who pays / data boundary |
| --- | --- | --- |
| Open home | Paste a GitHub URL, continue reading, or open Browse to discover ready Wikis. | Catalog metadata and static assets; no model call. |
| Open a repository | Precomputed overview and story Wiki, architecture map and source citations. Choose an example task to inspect its recorded evidence. | Static assets; preserve source/model provenance. |
| Explore a map or citation | Browser renders exported page boundaries/graphs and navigates to commit-pinned source. | Static data; disclose missing full runtime graph functionality. |
| Enter a new question | Explain “Connect OpenRouter” and the snippet/cost boundary, or “Run on your machine”. Do not spend the site's key on anonymous requests. | User-authorized provider account; bounded request. |
| Connect an agent | Copy a tested local setup now. Add remote MCP only after A6 passes. | Local user machine and user's agent/model. |
| Submit a public GitHub repository | Open a prepared Wiki, or explicitly generate a focused Wiki with chapter progress and saved links. | Bounded temporary public source on the server; user-funded OpenRouter; unlisted Wiki-only persistence. |

Implementation boundaries:

- Separate public-preview mode from the existing local Wiki server. A local
  user may intentionally configure their own backend model; do not break it
  by globally disabling Ask or generation.
- Reading public pages must not invoke outline generation, Wiki generation,
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
  shared Wiki generation or persist its result across users. Visitor-created
  Wikis are a separate, explicitly consented link-readable publication; their
  credentials stay in the active server run and are never saved.
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
checks the resulting artifact views. The public example pins the accepted v0.2.4 commit and passes `preset: fast`
explicitly. The workflow default is now released; users retaining v0.2.3 still
need the explicit setting to avoid its semantic default. Semantic views remain opt-in. This does
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

[CodeNib 0.2.4](https://github.com/sysevol-ai/CodeNib/releases/tag/v0.2.4)
is published on PyPI, the official MCP Registry and GitHub, with the stable
GitHub Release marked latest. Its annotated tag resolves to
`3f3bc2cf44ba83d1d86e9e5eb74f8da0f8bc63ac`, the exact commit accepted by
[TestPyPI run 36252579501](https://github.com/sysevol-ai/CodeNib/actions/runs/36252579501).
[Production run 36252998442](https://github.com/sysevol-ai/CodeNib/actions/runs/36252998442)
passed all artifact, installed-service, protected ownership, public PyPI and
Registry discovery gates. Both manylinux wheels and the sdist have matching
SHA-256 values in PyPI metadata, GitHub assets and the release checksums.
The #779/#780 MCP version correction is now available in the public package.

Post-publication [#798](https://github.com/sysevol-ai/CodeNib/pull/798) switches
installation pins and their contract check to verified 0.2.4, removes the
candidate navigation label and uses the packaged frontend in preview guides.
All Pages examples pin the full accepted release SHA with the version comment.
The clarified CodeGraph comparison names `colbymchenry/codegraph` and uses its
pinned source and documented capabilities. OAuth onboarding and the browser
trial remain opt-in while real provider consent and interactive unlock are
unverified. The maintainer chose desktop CLI login acceptance first and will
has deployed the live Wiki on their separate DGX Spark. The URL-entry frontend still requires its own deployment acceptance.

The release used the existing protected environments after explicit maintainer
approval. TestPyPI admits `main` and production admits `v*` tags; those rules
remain intact. Manually dispatched
[Docs run 36252581263](https://github.com/sysevol-ai/CodeNib/actions/runs/36252581263)
passed, with verified anonymous docs and README image access before the tag.

Native ARM verification in [#799](https://github.com/sysevol-ai/CodeNib/pull/799)
keeps the complete pinned cibuildwheel step, ownership/protocol smoke and every
other job unchanged. All executed checks passed before merge. The native ARM
job took 2m35s; the preceding accepted QEMU job took 29m34s. This is an observed
pair of CI runs, not a controlled hardware or product benchmark. The accepted
main and TestPyPI runs both use that workflow and pass the complete x86-64 and
AArch64 manylinux verification plus Python 3.10–3.14 installed ABI3 checks.

Local candidate validation passes 7,318 unit tests, 62 release/Registry/CI
contracts, strict public docs and the release all-files pre-commit check.
The local x86-64 ABI3 wheel and sdist pass build and metadata checks; they are
not the production pair of manylinux wheels. Python 3.12 with MCP SDK 2.2.0
reports 0.2.4 in modern and legacy handshakes. Real installed Wiki, checkout MCP
and portable-artifact MCP smokes pass, with a permanent installed-version guard
in the release harness. Fixture-backed source-only MCP and the public source
API pass without embedding/model/graph SDKs or billed calls. Baseline formatter
failures are corrected in a separate commit with unchanged production
non-import ASTs and 85 focused tests. Public TestPyPI and production publication
are complete. Real consent remains open; the next URL-entry deployment acceptance belongs
to the maintainer’s DGX Spark rollout.

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
