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
| Agent setup | `codegraph init` installs applicable package-level providers and registers local MCP with native Claude Code/Codex CLIs. | Clean-checkout and system/project prerequisites; verify fresh install and persistent executable registration before recommending `uvx`. |
| Retrieval evidence | Model-planned grep → Jev scores 71.40% macro code-block Recall@5 on the complete 100-issue CodeNib Base test split; unchanged grep ordering scores 58.62%. | Research runner, not a released CLI/MCP route; no corresponding measured Claude Code token saving. |
| OpenRouter | Production Jev Decisions adapter exists. The research grep planner uses OpenRouter chat completions. | Shared product configuration, authorization, budget handling, and source disclosure are missing. |
| Wiki demo | Live browsing/Ask can invoke generation; source-grounded story and graph work is in [#772](https://github.com/sysevol-ai/CodeNib/pull/772). | Review/reconcile #772 before changes to its shared UI, boundary endpoints, and export format. Do not restore legacy JSON cache adoption. |
| Static distribution | Static export and reusable GitHub Pages workflow already exist. | Confirm generated story/cache export parity and badge/link behavior; default semantic indexing still downloads a model. |
| MCP protocol | Empty server version fix is in [#780](https://github.com/sysevol-ai/CodeNib/pull/780), addressing [#779](https://github.com/sysevol-ai/CodeNib/issues/779). | Green reviewed patch still needs release reconciliation; do not tell reporters it is released before publication. |
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
are implemented in #788 (A4), including user-controlled playback. A fresh
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

Status: implemented in the `feat/grep-jev` source-checkout preview, stacked
on #781; not merged or released. CLI `codenib explore` and MCP
`--retrieval-route grep-jev` share a bounded source-only runtime. Both calls use
one OpenRouter credential, preserve reported usage on failure, disable retries,
and stop later work on cancellation or budget exhaustion. Existing indexed
routes remain the released defaults. No new storage abstraction is introduced.

435 relevant unit checks pass (seven optional Zoekt checks skip), plus 15
public-doc boundary tests, strict MkDocs and pre-commit. The tests cover real
rg/chunking, CLI/MCP delivery, source edits, exclusions, cancellation, invalid
provider responses and cost limits. A clean
`[grep,mcp]` environment installs neither torch, FAISS, LiteLLM, igraph nor
sentence-transformers; all 41 new route tests also pass there with MCP 2.2.
A live query over CodeNib's six-file LLM module returned
verified source in 4.747 seconds with $0.003869574 reported usage. This is a
connectivity smoke, not a benchmark. The initial planning attempt timed out
at 45 seconds with unknown usage; it was not automatically retried. After
restoring the experiment's exact wire schema, the separate live check passed;
this does not establish the timeout's cause.

The visible-range regression found synthetic chunk headers counted as source
lines. Product ranges now exclude them. `scripts/audit_jev_visible_ranges.py`
rechecks frozen artifact hashes and all 100 cases without API calls: 173 of
1,747 ranges shorten, with no change to the frozen orderings' 58.62% / 71.40%
Recall@5. Evidence is in `docs/assets/grep_jev_range_audit.json`.

The product candidate audit in
[#787](https://github.com/sysevol-ai/CodeNib/pull/787) replays all 100 frozen
plans through the product runtime with HTTP disabled. All cases complete;
94 match ordered text and corrected spans exactly, and six pools differ.
Frozen-plan grep retains 58.62% Recall@5. Three pools contain new unscored
text, so aggregate reranked quality is deliberately unset.

The first fresh-model attempt completed 19 cases before a scoring failure on
Caddy #5870 left cost unreported. Its shared budget stopped the remaining 80
cases; recorded usage is $0.225604746 plus the unknown failed-call cost. The
published status report keeps all 100 cases and leaves aggregate recall unset.
There are no automatic retries or case substitutions. Full fresh-model
quality, authorization/onboarding acceptance and main/release reconciliation
remain open; 71.40% is still a research claim.

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

Status: planned; local authorization precedes hosted account custody.

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
implemented in [#788](https://github.com/sysevol-ai/CodeNib/pull/788), independently
of A2. The clip replays selected CLI output on pinned Requests source; waits
are condensed and source/profile preservation is verified. GIF, WebM, MP4 and
a static poster are served locally, with transcript/provenance and a renderer
that makes no model calls. The source build includes merged #780; a newly
published package install remains to be recorded. A paired agent/token study,
merge and website deployment remain open. Detailed versions and validation
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

Status: the story and graph UI is implemented in
[#772](https://github.com/sysevol-ai/CodeNib/pull/772), rebased onto the current
`main`; it is not merged or released. The restack preserves the existing Wiki
changes and the static marketing preview introduced by #781. Web/Wiki/CLI
verification and the public-doc checks pass. Static cache publication and
agent activation are in dependent #784; a current SQLite corpus, bounded
operator generation and production deployment remain open gates. Legacy JSON
Wiki caches are not read or migrated.

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

Status: planned after the activation path works; service deployment is gated.

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
authorization, demo mode/export, and distribution. Keep #772's shared UI
changes separate until its base is reconciled. Each milestone lists current
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
