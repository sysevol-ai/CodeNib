# CodeNib landing page

This directory is a static site with no build step.

Run it locally from the repository root:

```bash
python -m http.server 7870 --directory landing
```

Deploy `landing/` as the document root for `codenib.ai`. The homepage presents
the change guard (`codenib guard`): what it tells a coding agent before it
finishes, one real agent patch that passed SWE-bench and broke another test,
the guard's result on all 68 such regressions, notes anchored to code, and the
install commands. Repository Wikis, MCP search and the blog are secondary links.

Everything on the homepage is static HTML; `assets/guard/landing-guard.js` only
adds the copy button, the chart's case names and the opt-in funnel events. Keep
it that way: the case, the call path, the chart and the commands must read in
full without the script.

The homepage's evidence is generated, not typed:

- The hook message and the call path are actual `codenib guard` output for
  sympy-24443 with the OpenHands patch applied at SWE-bench's base commit.
- The 68 squares per row come from the guard check's per-case results
  (pull request #976): one square per regression, in the same order in both
  rows. Regenerate them with the check's scripts rather than editing by hand.
- Patches and full-suite results are from PatchDiff (Wang, Pradel and Liu,
  ICSE 2026), CC BY 4.0. Keep the attribution next to the case.

- The xarray case is the one the plan named; its caption says why its list is
  long (a module-level import made the guard count the whole file as changed).
- "What it looks like in Claude Code" condenses a real headless session with
  the guard hooks on SWE-bench Verified sympy-24443, the hero case's issue.
  Sanitized transcripts of all three recorded sessions are in
  `assets/demos/guard-session-*.jsonl`; quotes on the page are verbatim.
  Replace it with a session from the #975 in-the-loop A/B when that runs, and
  never with a staged one.

Until a CodeNib release includes `codenib guard`, the install block builds
CodeNib from source (the wheel needs the prebuilt Wiki frontend, so a plain
`pip install git+…` does not work). After that release, replace the first
three lines with `python -m pip install "codenib[graph]"` and update the copy
check in `web/landing_verify.mjs`.

The demo (`web/`) is also the Wiki UI packaged with CodeNib, so its home page
is a deployment setting: the hosted demo's `runtime-config.js` sets
`home: "guard"` to open on the guard cases. Without it, `/` stays the Wiki
home. `/guard` and `/wikis` always reach the two pages.

The Requests example below now serves the Wiki homepage (`/wikis` on the demo).

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

For the homepage's layout at six widths, the page without JavaScript, copying
the install commands, the chart's case names and the opt-in events under Do Not
Track and Global Privacy Control:

```bash
node web/landing_verify.mjs http://127.0.0.1:7870 /tmp/codenib-landing
```

For opening and refreshing the actual homepage while its script is delayed or
fails:

```bash
node web/landing_loading_verify.mjs
node web/landing_loading_verify.mjs https://codenib.ai /tmp/codenib-landing-live
```

The first command starts its own local static server. It checks that the case,
the call path, the chart's 136 squares and the install commands are on the page
before the script arrives, that a failed script leaves no dead copy button, and
that the page never scrolls sideways.

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

The payload contains only a fixed event name, `landing`/`wiki`/`cli`, and a
random identifier (a tab session in a browser). No cookies, URLs, input, source, prompts or keys are
sent. Events are deduplicated per tab session; the collector accepts at most
120 events per minute per worker. This is an intentionally small first-party
baseline, not an analytics platform. To count observed steps from service logs:

```bash
journalctl --user -u codenib-demo-backend --since today --no-pager \
  | python scripts/summarize_experience.py
```

The guard funnel has two steps:

- `install_command_copy`: a visitor copied the install commands on the homepage.
  `guard_case_view` records that the real case was on screen first.
- `guard_first_run`, surface `cli`: an installed guard hook reported for the
  first time. The hook sends it only when the user installed with
  `codenib guard install --share-first-run`, at most once per user, and it
  carries no repository, agent or machine detail. The collector accepts the
  `cli` surface for this event only.

First runs divided by copies is a lower bound on activation; report both counts
and the window, since most installs will not opt in.

For the Wiki, use source inspection (`example_source_open` /
`example_github_open`) followed by `repository_submit` or `agent_setup_open`. The log
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
Benchmark articles should explain the measured mechanism: what each role
receives, how it chooses its next action, and what reaches the next role.
Distinguish search-only coverage from repair outcomes, and fixed role routing
from dynamic model selection. Tie the explanation to the executed policy and
separate design rationale from gains established by a controlled comparison.
Use public documentation URLs and pinned source links; do not link directly
to documents excluded from the public documentation site.

An SVG loaded through `<img>` cannot use the page's web fonts, so a figure
without an embedded font falls back to the reader's system font. Embed a Geist
subset in each figure, give wide figures a narrow `<picture>` variant for
phones, and render the sharing PNG from the same SVG with
`node scripts/render_svg_png.mjs <in.svg> <out.png>`.
`scripts/render_contextbench_blog_figures.py` does this for the ContextBench
article.

Serve `landing/` locally and check the index link, article title, images,
internal anchors, tables, and desktop/mobile layout. Run `mkdocs build --strict`
and `python scripts/check_public_docs.py` for the documentation boundary.
After deployment, verify the article's actual title and asset content types
on `codenib.ai`; a merged Markdown file or a successful MkDocs deployment
does not confirm blog publication.
