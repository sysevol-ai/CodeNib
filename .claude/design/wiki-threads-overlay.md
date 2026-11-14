# Wiki × Threads: what is happening on the code a page explains

Design memo for joining `codenib threads` with the published Wiki. Records the
thesis, the join, the delivery path, the risks, and what has been measured.

## Status — join, hot-file discount and page panel implemented (2026-11-14)

- `codenib/threads/wiki_overlay.py` joins Wiki page citations with stored
  threads, indexed open pull requests and recent first-parent commits, and
  writes a per-page overlay. No model, no code graph. Hot files are discounted
  (see Noise control).
- `codenib threads overlay --wiki <repo dir>` runs it on a published Wiki
  directory (`wiki.json` + `pages/`) or a directory of page JSON. Without
  `--output` it writes `wiki_overlay.json` into the repository's thread store.
- The Web app serves that file at `/api/repos/{id}/threads` (404 when absent,
  for another repository, or another overlay version); a static export reads
  `data/repos/<id>/threads.json`. The export does not write that file yet.
- `web/components/CodeActivity.tsx` renders "Activity on this code" below a
  page's prose and above "Read next": open issues, open pull requests and
  recent changes, newest first, symbol matches listed and file matches folded.
  It renders nothing without an overlay or without items for the page.
- Tests: `test/threads/test_wiki_overlay.py`,
  `test/web/test_threads_overlay_route.py`, `web/lib/codeActivity.test.ts`;
  `web/reading_layout_verify.mjs` answers the new route with 404.
- Not built yet: map markers, the Wiki Action input, writing `threads.json` in
  the static export, and the link from `THREAD_CONTEXT.md` back to the page.

## Thesis

Most people working with coding agents use git and GitHub, not a code graph.
Positioning threads as "graph-aware" asks them to adopt something first. The
Wiki is where the graph already reaches them: the Wiki Action builds it in CI
and they only read pages. So:

- **Threads stays git-native.** Code locations come from git + tree-sitter at a
  pinned commit (`anchor.py`, `touched.py`). Nothing to install or index.
- **The Wiki is the surface.** A page explains what the code is; the overlay
  says what is happening on it now (open issues, open pull requests) and why it
  changed recently (merged pull requests). Static docs become live docs.
- **Every published Wiki carries the feature.** Maintainers and contributors
  see "3 open pull requests change this page's code" each time they open it,
  which is the distribution loop.

## The join

Both sides already carry 1-based, inclusive line ranges:

| Side | Shape | Source |
|---|---|---|
| Wiki page | `citations[]`: `file`, `start_line`, `end_line`, `node_name` | page JSON |
| Thread | `CodeAnchor`: `path`, `symbol`, `start_line`, `end_line`, `cites` | `AnchorResolver.resolve` over the thread's own posts |
| Open pull request | `files`: path → base line ranges | `in_flight.py` index |
| Recent change | changed chunk ids `path:qualified.name` | `touched_between(parent, commit)` |

A match is graded:

- `symbol` — the item's lines overlap a cited range, or it names the cited
  symbol in the cited file. Names are compared by last component
  (`Checker::visit_expr`, `impl Checker.visit_expr`, `visit_expr()` all match),
  so a match survives lines moving between the Wiki's commit and the item's.
- `file` — the item reaches a cited file elsewhere.

Noise control:

- Only product source counts (`is_source_path`): tests, fixtures, snapshots
  and docs are shared by unrelated changes.
- A file is hot when more than `HOT_FILE_SHARE` (3%) of the last
  `HEAT_WINDOW` (300) first-parent commits touched it, and more than
  `HOT_FILE_FLOOR` (5). File-level matches on a hot cited file are dropped;
  symbol-level matches on it stay. `--heat-window 0` turns this off.
- An item that reaches more than `WIDE_FILE_PAGES` (4) pages only at file level
  is dropped from those pages; its symbol-level hits stay.
- A thread is placed by what its own posts say; linked threads are not
  followed, so an issue does not inherit the files of a refactoring it links.
- An open pull request in the index is placed by its diff, not its thread.
- Per page, at most 8 items of each kind, `symbol` first, then by symbols
  reached, then most recent.

## Overlay schema (`threads.json`, version 1)

```json
{
  "version": 1,
  "repo": "owner/name",
  "rev": "<sha the Wiki describes>",
  "generated_at": "2026-11-14T20:00:00+00:00",
  "pages": {
    "<page id>": {
      "issues":  [{"number": 7, "title": "...", "state": "open", "author": "...",
                   "at": "...", "strength": "symbol", "symbols": ["parse_config"],
                   "files": ["src/config.py"], "cites": ["#7/opened/7"],
                   "url": "https://github.com/owner/name/issues/7"}],
      "pulls":   [{"...": "same, plus draft"}],
      "changes": [{"number": 30, "commit": "<sha>", "title": "...", "at": "...",
                   "strength": "symbol", "symbols": ["..."], "files": ["..."],
                   "url": "https://github.com/owner/name/pull/30"}]
    }
  },
  "summary": {"pages": 41, "pages_with_items": 0, "threads_placed": 0,
              "open_pulls_indexed": 0, "changes_placed": 0}
}
```

Pages with nothing are absent. `changes` without a `(#N)` subject have
`number: 0` and no `url`.

## Delivery

1. **Wiki Action** (`sysevol-ai/wiki-action`, reusable `codenib-pages.yml`):
   add `threads: true`. The job already has `GITHUB_TOKEN`; add
   `issues: read` and `pull-requests: read`. After the Wiki is exported:
   `codenib threads sync <open issues and PRs>`, `sync --open-pulls`, then
   `codenib threads overlay --wiki <export>/data/repos/<id>` written beside
   `wiki.json`.
2. **Refresh without regenerating the Wiki.** Pages are model-written once per
   push; the overlay is code-only. A `schedule:` job (hourly) rebuilds
   `threads.json` against the published commit and redeploys only that file.
   Cost: GitHub requests, no model calls.
3. **Frontend.** Each page gets a "On this code now" panel (issues, pulls,
   recent changes, with `symbol` matches named), and the Wiki map marks areas
   with open items and areas where two open pull requests meet. Shows
   "as of `generated_at`".
4. **Agent direction.** `THREAD_CONTEXT.md` names the Wiki page(s) its anchors
   fall on, so an agent taking an issue also gets the design intent of that
   code. Reuses `PageIndex.match`.

## Risks

- **Private repositories.** A Pages site can be public while the repository is
  private. The Action must default `threads` off for private repositories and
  require an explicit opt-in.
- **Untrusted text.** Titles come from anyone who can open an issue. Render as
  text, never markup; the overlay is JSON, never inlined into HTML.
- **Coverage of issues.** Feature requests often name no code and will not be
  placed. Measure on a real repository before leading with issues (see below).
- **Staleness.** Always show `generated_at`; drop the panel when the overlay
  is older than its refresh interval by a wide margin.
- **Line drift.** Open pull request ranges are on their base commit, not the
  Wiki's. Symbol names absorb drift for changes; for pull requests, a range
  that drifted out of a citation degrades to a `file` match.

## Measured

### ruff, pull requests merged after the demo Wiki's commit

Demo Wiki `astral-sh__ruff` at `8bbdb8e3` (2026-07-19): 41 pages, 414
citations. The 300 first-parent commits after it are squash-merged pull
requests, most of them open while the Wiki was written. Placed with
`add_recent_changes` (name matching, `WIDE_FILE_PAGES` = 4).

Window 2026-07-20 to 2026-08-04, 300 pull requests; the run takes 26 s.

| | Pull requests | Share |
|---|---|---|
| Change no product source (tests, mdtest, docs only) | 70 | 23% |
| Touch a file some page cites | 127 | 42% |
| Placed on a page (after the wide-item filter) | 107 | 36% |
| Placed on a page by symbol | 26 | 9% |

- 35 of 41 pages receive at least one pull request; 26 receive one by symbol.
  A placed pull request lands on a median of 2 pages (max 11).
- `WIDE_FILE_PAGES` trades reach for noise: 2 → 88 placed, 4 → 107,
  8 → 122, no limit → 127.
- File-level matches are often still on topic ("Insert a space after the
  colon in Ruff suppression comments" on *Noqa Suppression*), but hot files
  carry little: `ty_python_semantic/src/types.rs` was touched by 23 of the 300
  commits and `types/infer/builder.rs`, cited by 6 pages, by 20.
- The hot-file discount, same window (rows: share of the 300 commits above
  which a cited file is hot):

  | Hot above | Hot cited files | Placed | File matches | Symbol matches | Pages |
  |---|---|---|---|---|---|
  | off | 0 | 107 | 187 | 63 | 35 |
  | 5% | 2 | 115 | 200 | 63 | 35 |
  | **3% (default)** | 12 | 75 | 119 | 63 | 35 |
  | 2% | 20 | 62 | 86 | 63 | 34 |

  At 3% file matches fall by 36% while every symbol match and every page
  stays. At 5% more are placed than with no discount: dropping a hot file
  brings some items back under the wide-item limit. A sample of the remaining
  file matches was on topic (e.g. "Fix double visit / missing elif visit" on
  *AST Visitors and Helpers*).
- Symbol matches are few because a page cites a handful of symbols per file;
  most changes to a cited file change something else in it. The panel should
  lead with symbol matches and show file matches collapsed.
- Bare-name matching inside one file can confuse two `new()` in different
  `impl` blocks of that file.

Reproduce: clone `astral-sh/ruff` with history back to 2026-07-18, fetch the
demo Wiki's pages from `demo.codenib.ai/api/repos/astral-sh__ruff/wiki/<id>`,
then `codenib threads overlay --wiki <pages dir> --checkout <ruff> --rev
<300th first-parent commit after 8bbdb8e3> --history 300 --no-threads`.

### Not yet measured

- **Issues.** This environment cannot read ruff's issues through the GitHub
  API, so how many open issues land on a page is unknown. Run
  `codenib threads sync` on the open issues of a repository with a published
  Wiki, then `codenib threads overlay --history 0`, and count
  `summary.threads_placed`.
- **Open pull requests by diff.** Same blocker; the merged-history window
  above stands in for them.

## Next steps

1. Measure issue coverage on one repository with a published Wiki.
2. Map markers; publish the ruff overlay on the demo (`wiki_overlay.json` in
   the demo host's thread store).
3. `threads: true` in the Wiki Action, off for private repositories, with the
   scheduled refresh.
4. Wiki page links in `THREAD_CONTEXT.md`.
