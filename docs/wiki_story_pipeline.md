<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeWiki: how a page becomes a story

This note records the editorial rules behind `codenib/wiki/agent_wiki.py`
after the 2026-09 rework. The short version: **graph facts are the skeleton,
model prose is the annotation, and "why" is required rather than banned.**

## Layers of a page

| Layer | Source | Rendered as |
|---|---|---|
| Thesis and reader question | model plan, admitted against evidence | opening paragraph and callout |
| Architecture (Overview only) | model synthesis of source evidence and static relations | repository-specific roles, one primary path, supporting roles, and explicit boundaries |
| Relations | graph edges with call-site anchors | evidence beneath semantic architecture on Overview; optional `**Interactions**` rows on detail pages |
| Claims | model plan, one role each (`purpose`, `entry`, `flow`, `responsibility`, `contract`, `rationale`, `component`) | one paragraph per section; `flow` claims that cite an `R#` become rows, never sentences |
| Excerpt (detail pages only) | the cited body, windowed on the lines the section names | fenced code with `hl=` marked lines and a *What to notice* caption |
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

Advisory only (reported under `narrative_advisories` / `story_advisories`):
narrative density, section synthesis, novelty, role progression, transition
coverage, and the reader question. No template question is ever generated; a
page without a question simply has none.

Model-judged: `quality.parse_story_review` scores the rendered page against
a newcomer rubric (problem, path in/out, key decision, failure case, plus
pipeline-jargon and repetition flags). It runs when `AgentWiki` is built with
`story_review=True` (the web app enables it unless
`CODENIB_WIKI_STORY_REVIEW=0`), is stored as `quality.story_review`, and is
shown in the page provenance line as `reader review N/8`.

## Frontend rules

- Overview renders one native semantic-architecture card. It never turns a
  static call graph, storyboard, Mermaid block, or subsystem inventory into a
  second architecture view. When no architecture plan is admitted, the
  recorded entry path is rendered as a flow card instead, so the landing page
  is never blank; a missing architecture is a composition warning, not a
  reason to withhold the page.
- Child pages do not receive automatic architecture or flow media. Their
  source-backed prose may still use a compact interaction row when the handoff
  itself is the idea being explained.
- Trailing citations in a paragraph or list item are grouped into
  `.cite-group`.
- A fence info string such as `python hl=3,5-6` marks the excerpt lines the
  prose is about.
- The subsystem map card never advertises "0 relationships"; without edges it
  reads "Cited symbols" and lists them.
