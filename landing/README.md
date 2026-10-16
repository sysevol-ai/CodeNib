# CodeNib landing page

This directory is a static site with no build step.

Run it locally from the repository root:

```bash
python -m http.server 7870 --directory landing
```

Deploy `landing/` as the document root for `codenib.ai`. The homepage leads
with the same interactive Requests example as the Wiki homepage: a question,
an explanation, four steps and clickable source. The marketing page offers
three questions about authentication, request flow and caller impact, all
bounded to that selected path. Its four source excerpts and three recorded
call sites are bundled with the site; exploring it
makes no request to the live Wiki service or a model. Visiting a Wiki is an
explicit link. The repository form needs the matching Wiki frontend's `?repo=`
resolver; deploy that frontend before publishing the new marketing form.

When changing the shared example component, build the Wiki frontend with
`cd web && npm run build` from the same commit and deploy its `dist/` alongside
the static homepage. Preserve the hosted `runtime-config.js`. After publishing,
verify both example fragment links, the source controls and the repository
handoff on desktop and mobile, as well as the real page title and asset content
types described below. Record live acceptance in the first-visit milestone in
`docs/adoption_roadmap.md`; a merged PR alone does not confirm publication.

Keep the reading order: try the example, explore a public repository, connect
a local coding agent, then inspect technical evidence and documentation. The
repository entry must show OpenRouter funding, the spending-limit boundary and
hosted file/chunk limits before handoff. Keep the example above the repository
form on phones; task choices follow the example there. `/#example-source` and
`/#requests-auth` both reach the example. Hero, repository and agent sections
share their content edges and column split; preserve those alignments when
changing copy. Local Agent setup makes no CodeNib model call; the user's
agent still uses its own model.

The example is one selected call path, not a runtime trace or a whole-repository
architecture. `assets/explore/requests.js` records the full source commit,
index projection endpoint, excerpt ranges and SHA-256 values. To verify it
against the same prepared Requests index, without generating any prose:

```bash
python scripts/build_first_visit_example.py --api-base http://127.0.0.1:8001 --check
node scripts/highlight_first_visit_example.mjs --check
node scripts/render_first_visit_share.mjs
```

Remove `--check` only when deliberately updating the evidence. Review the
commit, selected symbols, graph anchors and ranges together. Source excerpts
are from Requests under Apache-2.0; its pinned license URL is in the artifact.
After changing excerpts, regenerate their syntax asset with
`node scripts/highlight_first_visit_example.mjs` using the installed Web
dependencies. It uses the Wiki's existing highlight.js Python grammar, preserves
the excerpt text, and adds no browser dependency or network request. The source
pane keeps its height across selections; long code scrolls inside the pane.
The renderer produces the real example's 1200×630 homepage sharing image.
Repository-specific social images remain a separate task.

Provider-free desktop/mobile, keyboard, routing, sharing and event checks:

```bash
node web/first_visit_verify.mjs http://127.0.0.1:3012 http://127.0.0.1:7882
```

For the marketing page's task choices, source selection, alignment, repository
handoff, legacy anchors, no-JavaScript fallback and opt-in event behavior:

```bash
node web/landing_verify.mjs http://127.0.0.1:7870 /tmp/codenib-landing
```

For opening and refreshing the actual homepage while the example's style
dependency is delayed or fails:

```bash
node web/landing_loading_verify.mjs
node web/landing_loading_verify.mjs https://codenib.ai /tmp/codenib-landing-live
```

The first command starts its own local static server. The regression samples
painted frames at desktop/mobile widths and keeps a readable Wiki link when
the example cannot load. Shared styles live in `repo-explorer-styles.js` and
are installed synchronously with the shadow markup; do not replace them with
an external shadow stylesheet that can expose raw code before it loads.

For the Wiki reading column and shared map's first paint, including delayed,
unavailable and failed visual responses while opening and refreshing:

```bash
npm --prefix web run build
node web/reading_layout_verify.mjs
```

The verifier starts its own local preview and intercepts every API request with
fixtures. It uses no repository, model credentials or Wiki database. Keep the
reading column fixed from the loading state onward; render prose with its
leading visual and measure responsive maps before their first paint.

For first-party funnel counts, explicitly enable both
`CODENIB_EXPERIENCE_EVENTS=1` on the API and `experienceEvents: true` in the
hosted Wiki's `runtime-config.js`. Static Wiki exports never enable collection.
The marketing host may separately proxy `/api/experience-events` to this API
and set `<meta name="codenib-experience-endpoint" content="/api/experience-events">`.
Without that opt-in, neither site sends analytics. Do Not Track and Global
Privacy Control suppress collection. Keep deployment settings outside source
defaults; retain ordinary service logs according to the operator's policy.

The payload contains only a fixed event name, `landing`/`wiki`, and a random
tab-session identifier. No cookies, URLs, input, source, prompts or keys are
sent. Events are deduplicated per tab session; the collector accepts at most
120 events per minute per worker. This is an intentionally small first-party
baseline, not an analytics platform. To count observed steps from service logs:

```bash
journalctl --user -u codenib-demo-backend --since today --no-pager \
  | python scripts/summarize_experience.py
```

Use source inspection (`example_source_open` / `example_github_open`) followed
by `repository_submit` or `agent_setup_open` as the marketing funnel. The log
summary reports both `source_inspection` cohorts using each tab's first event
positions, and counts the next step only when it appears later in the service
log. Viewing the hero or switching its question does not count as inspecting
source. Do not use the primary button's click count alone to claim activation.

Report counts and the observation window before interpreting ratios. These
are partial tab-session observations, not unique people: origins have separate
sessions, privacy controls and blocked requests omit visits, and reopening a
saved Wiki can produce a read without a generation event. A ready first chapter
is a browser observation, not proof of a successful coding-agent query.

The 15-second CLI replay is rendered from selected fields of
`assets/demos/codegraph-claude.json`, an actual CodeGraph/Claude Code run.
It condenses waits and does not imitate the interactive Claude UI. With the
Web dev dependencies, Playwright Chromium and ffmpeg installed, regenerate
the GIF, WebM, MP4 and static poster with `node scripts/render_agent_demo.mjs`
from the repository root. This command makes no model calls. Keep the
recorded source/CodeNib commits and cost/measurement limitations intact.

## Static routes

The site uses real directory indexes for nested routes:

- `/` is `landing/index.html`;
- `/blogs/` is `landing/blogs/index.html`;
- `/blogs/jev-model-grep-reranking/` compares Jev and Qwen rerankers on
  BM25 and model-planned grep candidates;
- `/blogs/local-code-intelligence-dgx-spark/` is the reproducible local
  deployment reference;
- each article lives at `landing/blogs/<slug>/index.html`.

Nested pages should reference shared assets from `/assets/...` so the same URL
works at every route depth. The local Caddy configuration falls back to the
homepage for unknown paths, so a `200` response alone does not prove that a
new route or asset exists. Check the page title and asset content type as part
of deployment verification:

```bash
curl -s https://codenib.ai/blogs/ | grep '<title>CodeNib Blog'
curl -sI https://codenib.ai/assets/blogs/fact-query-index-fact-batch.png \
  | grep -i 'content-type: image/png'
```

## Publishing an article

Public blog posts, summaries, figure labels, and accessibility metadata are
written in English, matching the rest of the site.

Markdown under `codenib/blogs/` is a repository research record. It is not
automatically rendered or published to `codenib.ai`; the MkDocs workflow
publishes the separate `docs.codenib.ai` site.

To publish a blog post, add `landing/blogs/<slug>/index.html`, add its card to
`landing/blogs/index.html` in newest-first order, refresh the homepage's
featured article, and copy its public images into `landing/assets/blogs/`.
Follow the existing article template for the canonical URL, language,
publication date, social metadata, and JSON-LD.
Use public documentation URLs and pinned source links; do not link directly
to documents excluded from the public documentation site.

Serve `landing/` locally and check the index link, article title, images,
internal anchors, tables, and desktop/mobile layout. Run `mkdocs build --strict`
and `python scripts/check_public_docs.py` for the documentation boundary.
After deployment, verify the article's actual title and asset content types
on `codenib.ai`; a merged Markdown file or a successful MkDocs deployment
does not confirm blog publication.
