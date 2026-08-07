# Wiki customization — human prior injection

**Status:** design agreed, implementing. **Base:** `codenib/main`. **Written:** 2026-07-22.
**Owner:** Yash. Discussed with Zhongming ("leave space for human prior injection").

## Goal

Let a reader inject their own *prior* — how they want a wiki page written — and see
the page regenerated to match, without touching the grounded generation pipeline.
The default wiki stays authoritative; customization is an ephemeral presentation
layer on top.

## Why a layer, not a rewrite of the generator

`AgentWiki` is a grounded, quality-guarded pipeline (plan → page → repair →
quality, `_PAGE_PROMPT_VERSION = "102"`) producing structured sections with
citations. Threading a free-form human style instruction *through* it would fight
the grounding/coverage guards and is high-risk before launch.

The page already exposes a rendered `markdown` string (what the reader sees). So
customization is a **post-generation transform**: `markdown + prior -> markdown`,
run once on demand, preserving citation markers and factual claims. This is
exactly Zhongming's "leave *space* for human prior injection" — a layer, not a
pipeline change — and it keeps the grounded default untouched by construction.

## The prior

```
Prior = { instruction: str, structure: Optional[list[str]] }
```

- **instruction** — free text: "explain like a blog, add intuition, fewer lists".
- **structure** — optional section skeleton the rewrite must fill:
  `["Intuition", "How it works", "Example", "Gotchas"]`.

Scopes, cascading most-specific-wins:

```
whole-wiki prior   (base house style)
    └─ page prior      (overrides / extends, for one page)
```

Section scope is deferred (needs section addressing + selection UI); noted below.

## Two stores, kept separate

| | Durable default | Ephemeral customization |
|---|---|---|
| Content | grounded wiki (`AgentWiki` disk cache) | active priors + transformed markdown |
| Lives | on disk | **server RAM only** |
| Survives restart | yes | no -> default returns |
| Cleanup | n/a | idle-TTL + LRU cap, swept lazily on access |

Separation makes "customization never corrupts the default" true by construction.

## Components

### `codenib/wiki/customizer.py` (new)
`Customizer.apply(markdown, prior) -> str`. One LiteLLM pass (reuses the wiki
model/creds). Prompt: rewrite the page to the reader's instruction and optional
structure, **preserving every `[n]`/citation marker and all factual claims; add
no new facts**. Fail-soft: on any error return the original markdown unchanged, so
a customization failure degrades to the default rather than an error page.

### `codenib/web/customization_store.py` (new)
Global, in-RAM, behind a narrow interface so a later Redis/SQLite swap (needed
only for multi-worker serving) is one file:

```
set_prior(scope, target, prior)          # scope in {"wiki","page"}; target = page_id or ""
resolve(page_id) -> Prior | None         # cascade: wiki prior <- page prior (most specific wins)
drop_prior(scope, target)
transformed(page_id, prior, produce) -> markdown   # memoized by (page_id, prior_hash)
```

- Priors keyed by `(scope, target)`. `resolve(page_id)` merges the wiki-level and
  page-level priors (page instruction appended after wiki instruction; page
  structure overrides wiki structure if set).
- `transformed()` memoizes the LLM output by `(page_id, sha1(prior))` so repeated
  views don't re-call the model; `produce` is the transform thunk.
- **Cleanup (lazy, on every access):** drop entries idle > `TTL` (30 min), then if
  over `MAX_ENTRIES` (64) evict least-recently-used. No background thread — matters
  for the single-process Spark deploy.
- Global (not per-visitor) for v1: simplest, and "customize then show it" just
  works. Per-visitor isolation is a later add (key by an anonymous session token);
  the store's key already has room for it.

### `codenib/web/app.py`
- `POST /api/repos/{id}/customize` `{scope, target, instruction, structure?}` ->
  store prior, transform the affected page(s), return the new markdown.
- `DELETE /api/repos/{id}/customize` `{scope, target}` -> drop prior; default returns.
- `GET /api/repos/{id}/wiki/{page_id}` -> if `resolve(page_id)` yields a prior,
  serve `transformed(...)`; else the default page unchanged. A `customized: bool`
  flag on the response tells the client which it got.

Whole-wiki scope applies lazily: setting a wiki prior does not eagerly transform
every page; each page transforms on first view and is then memoized.

### Frontend
Evolve the inert "Refresh this wiki" button into a **Customize panel**:
scope selector (This page / Whole wiki), instruction textarea, optional structure
field (one section per line), **Apply** + **Reset to default**. On Apply -> POST,
loading state, swap in the returned markdown. A small "customized view" badge (not
implying grounding was re-verified) marks a transformed page.

## Correctness / honesty posture

- The transform is instructed to preserve citations and add no facts, but it is
  **not** re-run through the grounding guards. The customized view is therefore
  labeled as a reader lens, never presented as grounding-verified — the same
  `verified` vs `checked` discipline used elsewhere in the repo.
- Fail-soft everywhere: a failed transform yields the default; a missing model
  yields the default.

## Scope for this PR

In: core transform, ephemeral store with TTL+LRU, page scope, whole-wiki scope,
structure template, reset, the Customize panel.
Out (follow-ups): section scope + selection UI; per-visitor isolation;
Redis-backed store for multi-worker.

## Tests

- `customizer`: preserves citation markers; returns input unchanged on empty
  prior; fail-soft on client error.
- `customization_store`: cascade resolution (wiki only / page only / both);
  memoization; TTL expiry; LRU eviction; drop.
- endpoints: customize round-trip; reset restores default; `customized` flag.
