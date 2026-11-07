<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeWiki: how a page becomes a story

This note records the editorial rules behind `codenib/wiki/agent_wiki.py`.
The short version: **graph facts are the skeleton, model prose is the
annotation, "why" is required rather than banned, and a page has to be one
people want to read.**

Users told us the Wiki helps but that it is too fine-grained, that nobody wants
to read it, and that it sounds machine-written and is hard to picture. Every
earlier publication gate protected accuracy, so nothing stopped that. The gates
below now protect the reader as well.

## Layers of a page

| Layer | Source | Rendered as |
|---|---|---|
| Thesis and purpose | model plan, admitted against evidence | opening paragraphs, with no heading |
| Architecture (Overview only) | model synthesis of source evidence and static relations | repository-specific roles, one primary path, supporting roles, and explicit boundaries |
| Relations | graph edges with call-site anchors | evidence beneath semantic architecture on Overview; optional `**Interactions**` rows on detail pages |
| Claims | model plan, one role each (`purpose`, `entry`, `flow`, `responsibility`, `contract`, `rationale`, `component`) | one paragraph per section; `flow` claims that cite an `R#` become rows, never sentences |
| Excerpt (detail pages only) | the cited body, windowed on the lines the section names | fenced code with `hl=` marked lines and a *What to notice* caption |
| Selected call path (primary and key pages) | the longest call path among the page's recorded relations | a figure after the opening, labelled as one selected path |
| Journey (Overview only) | the call path the index records from the public entry inward (`AgentWiki._entry_path`), narrated one admitted sentence per stage by a separate small call | numbered stages linking to the owning area page; also the visual fallback (`overview-entry-path` flow card) when no architecture plan is admitted |
| Explore the system (Overview only) | outline areas | a short child-page link list, never a second architecture or file inventory |

## Evidence that states a reason

`codenib/wiki/context_evidence.py` adds two deterministic evidence kinds to
every page pack:

- `test`: names of tests whose body mentions a cited symbol, grouped per file;
- `history`: subjects of the commits that last changed a cited span
  (`git blame`), only on full clones.

The plan prompt asks for one `rationale` claim per section whenever a
docstring, comment, test name, or commit subject states a reason. The
promotional-language filter (`evidence._PROMOTIONAL_RE`) now bans only
evaluations (*powerful*, *efficient*, *elegant*, …); causal verbs (*allows*,
*ensures*, *so that*, *because*) are ordinary claims and are admitted or
rejected on their evidence like any other.

## What gates publication

Deterministic and blocking:

- grounding (`evidence.grounding_report`): every prose block cites, every
  identifier and path resolves to evidence;
- structural quality (`quality.page_quality_report`): sections rendered,
  claim coverage, no duplicate blocks or thin sections, prose integrity,
  intra-section repetition, plan role integrity, editorial budget;
- story structure (`story.story_quality_report`): beats align with rendered
  sections and each beat has evidence.

- readability (`quality.readability_report`): measured against 108 published
  pages of four prepared Wikis.

| Limit | Value | Published pages that broke it |
|---|---|---|
| Template sections (`Purpose and scope`, `At a glance`) | none | 97 of 108 |
| Opening sentence | no leading identifier or pronoun; on an Overview, no code at all | 81 of 108 |
| Printed `Reader question` callout | none | 79 of 108 |
| Inline code | at most 15 spans per 100 words | 8 of 108 |
| Sentences that begin with an identifier | at most 60% | 18 of 108 |
| Sentences with no code | at least 15% | 31 of 108 |
| Longest sentence | 45 words | none |
| Length | 40 to 450 words on an Overview, 40 to 800 elsewhere | none |

None of the 108 pages passed all of these. On the same repository at the same
commit, the rewritten pipeline passed 12 of 13.

The ratio limits apply from four sentences up, and an Overview's numbered main
path counts as content for the minimum. Code excerpts, tables, relation rows, the
journey list and navigation are not prose and are not measured. When a page fails,
the model gets one chance to rewrite the thesis, purpose, leads and claims, and each
new sentence must pass the same source-support check as the old one. Flow claims
are left alone, because they have to name both ends of a call.

Advisory only (reported under `narrative_advisories` / `story_advisories`):
narrative density, section synthesis, novelty, role progression, and transition
coverage. The story's reader question steers the plan and is never printed.

Model-judged: `quality.parse_story_review` scores the rendered page against
a newcomer rubric (problem, path in/out, key decision, failure case, plus
pipeline-jargon and repetition flags). It runs when `AgentWiki` is built with
`story_review=True` (the web app enables it unless
`CODENIB_WIKI_STORY_REVIEW=0`), is stored as `quality.story_review`, and is
shown in the page provenance line as `reader review N/8`. It blocks a detail page
below 4/8. It never blocks an Overview: its questions about failure cases rarely
fit a landing page, and a withheld Overview leaves a reader no way in. A low score
or a jargon or repetition flag earns one rewrite, kept only if the reviewer rates
it higher. Those flags do not block, because the model flagged repetition on 78 of
104 published detail pages and cannot be the only judge of that. A review scoring
zero on every question is discarded: asked six times about one page at temperature
0, the demo model answered 6, 8, 6, 0, 0, 0. Without a model review the
deterministic limits still hold.

## What decides importance

`codenib/wiki/importance.py` ranks pages from the recorded call graph, with no
model call. A symbol weighs more when code reaches it and when it reaches other
code, so a dispatcher outranks a leaf helper that everything calls. The rank
sets each page's tier:

| Tier | What the reader sees |
|---|---|
| `primary` | Listed up front (the Overview plus at most five pages); drawn with a figure |
| `key` | Listed once its parent opens; drawn with a figure |
| `more` | Behind "More topics" in the page list |
| `detail` | Listed once its parent opens, and linked from "Go deeper" |

Nothing is removed: every page is still generated and still has an address.
Without a usable graph, tiers follow outline order and are not stored.

The same weights choose where the Overview's main path starts: the public callable
that nothing else calls and whose heaviest walk through the code weighs the most,
preferring a manifest entry point when one has a path. Each hop still follows a
recorded call. On Requests this starts at `get()` in `api.py`, whichever code the
page's search happened to retrieve.

## Frontend rules

- Overview leads with the real path through the code: the poster, then the main
  path figure with one sentence per recorded call, labelled as one selected
  path. The semantic-architecture card sits behind "How the parts fit together".
  It is never built from a static call graph. A missing architecture is a
  composition warning, not a reason to withhold the page.
- `primary` and `key` pages draw their longest recorded call path of at least
  three steps, and draw nothing when there is none. No model chooses the arrows,
  and only calls between callables in non-test code qualify.
- The page list opens on `primary` pages. Whatever holds the current page opens
  by itself, so the page being read never disappears from its own navigation.
- A parent page ends with "Go deeper" links to its detail pages. Callers and
  calls in the code graph wait behind a disclosure instead of filling the first
  screen.
- Trailing citations in a paragraph or list item are grouped into
  `.cite-group`.
- A fence info string such as `python hl=3,5-6` marks the excerpt lines the
  prose is about.
- The subsystem map card never advertises "0 relationships"; without edges it
  reads "Cited symbols" and lists them.
