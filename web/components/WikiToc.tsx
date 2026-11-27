// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useState, type CSSProperties } from "react";

import type { WikiPageRef } from "@/lib/api";
import type { WikiPageState } from "@/lib/visitorWiki";
import { NEUTRAL_COLOR } from "@/lib/wikiGraph";
import { pagePath, partitionWikiNav } from "@/lib/wikiNav";

const STATE_LABELS: Record<WikiPageState, string> = {
  pending: "Planned",
  running: "Being written",
  ready: "Ready",
  needs_review: "Withheld after source checks",
};

// The README and the first few documents show; the rest wait behind a toggle.
const DOCS_SHOWN = 6;

const CACHE_LABELS = {
  ready: "Cached page",
  cold: "First load will generate this page",
  retryable: "Quality retry queued",
  degraded: "Page needs review",
} as const;

/** The page list, opening on the pages that matter most.
 *
 * Detail pages wait behind their parent, and lower-ranked areas behind "More
 * topics". Whatever holds the current page opens by itself, and a choice the
 * reader made about a group overrides that.
 */
export default function WikiToc({
  pages,
  activeId,
  onPick,
  colors,
  states,
  walks,
  activeWalk,
  onWalk,
  docs,
}: {
  pages: WikiPageRef[];
  activeId: string;
  onPick: (id: string) => void;
  /** Each page's colour on the map, when the map is showing. */
  colors?: Map<string, string>;
  /** A Wiki being written: each dot shows its page's state, as on the map. */
  states?: Record<string, WikiPageState>;
  /** Walkthroughs built for this repository, listed after the main pages. */
  walks?: { id: string; label: string }[];
  activeWalk?: string;
  onWalk?: (id: string) => void;
  /** The repository's own README and docs/ files, read as pages. */
  docs?: { id: string; title: string; path: string }[];
}) {
  const [choices, setChoices] = useState<Record<string, boolean>>({});
  const [allDocs, setAllDocs] = useState(false);
  const [moreChoice, setMoreChoice] = useState<boolean | null>(null);
  const split = partitionWikiNav(pages);
  // An area in colour on the map (those the Overview's path enters take the
  // hues first) is never hidden under "More topics" while grey ones show.
  const hued = (page: WikiPageRef) => {
    const color = colors?.get(page.id);
    return !!color && color !== NEUTRAL_COLOR;
  };
  const promoted = new Set(split.more.filter(hued).map((page) => page.id));
  const shown = new Set([...split.primary.map((page) => page.id), ...promoted]);
  const primary = promoted.size ? pages.filter((page) => shown.has(page.id)) : split.primary;
  const more = promoted.size ? split.more.filter((page) => !promoted.has(page.id)) : split.more;
  const activePath = pagePath(pages, activeId);
  const moreOpen = moreChoice ?? more.some((page) => activePath.includes(page.id));

  const renderPages = (items: WikiPageRef[]) => (
    <ul className="toc-tree">
      {items.map((page) => {
        const open = choices[page.id] ?? activePath.includes(page.id);
        const detailCount = page.children.length;
        const state = states ? states[page.id] ?? "pending" : undefined;
        return (
          <li key={page.id} className={state ? `is-${state}` : undefined}>
            <div className="toc-row">
              <button
                className={`toc-link ${page.id === activeId ? "active" : ""}`}
                aria-current={page.id === activeId ? "page" : undefined}
                title={page.title}
                onClick={() => onPick(page.id)}
              >
                {state ? (
                  <span
                    className={`toc-dot is-${state}`}
                    style={{ "--dot": colors?.get(page.id) ?? NEUTRAL_COLOR } as CSSProperties}
                    aria-hidden="true"
                  />
                ) : (
                  colors?.has(page.id) && (
                    <span className="toc-dot" style={{ background: colors.get(page.id) }} aria-hidden="true" />
                  )
                )}
                <span className="toc-label">{page.title}</span>
                {state && state !== "ready" && <span className="sr-only">, {STATE_LABELS[state]}</span>}
                {!state && page.cache_state && page.cache_state !== "ready" && (
                  // Ready is the normal state; only pages that still need work
                  // carry a marker, so the marker means something.
                  <span
                    className={`toc-cache-state ${page.cache_state}`}
                    aria-label={CACHE_LABELS[page.cache_state]}
                    title={CACHE_LABELS[page.cache_state]}
                  />
                )}
              </button>
              {detailCount > 0 && (
                <button
                  type="button"
                  className={`toc-group-toggle ${open ? "open" : ""}`}
                  aria-expanded={open}
                  aria-label={`${open ? "Hide" : "Show"} ${detailCount} detail ${
                    detailCount === 1 ? "page" : "pages"
                  } under ${page.title}`}
                  title={`${detailCount} detail ${detailCount === 1 ? "page" : "pages"}`}
                  onClick={() =>
                    setChoices((current) => ({ ...current, [page.id]: !open }))
                  }
                >
                  <span aria-hidden="true">›</span>
                </button>
              )}
            </div>
            {open && detailCount > 0 && renderPages(page.children)}
          </li>
        );
      })}
    </ul>
  );

  return (
    <>
      {renderPages(primary)}
      {docs && docs.length > 0 && (
        <div className="toc-walks toc-docs">
          <div className="toc-walks-title" id="toc-docs-title">
            Repository docs
          </div>
          <ul className="toc-tree" aria-labelledby="toc-docs-title">
            {(allDocs || docs.length <= DOCS_SHOWN
              ? docs
              : docs.filter(
                  (doc, index) => index < DOCS_SHOWN - 1 || doc.id === activeId,
                )
            ).map((doc) => (
              <li key={doc.id}>
                <div className="toc-row">
                  <button
                    type="button"
                    className={`toc-link ${doc.id === activeId ? "active" : ""}`}
                    aria-current={doc.id === activeId ? "page" : undefined}
                    title={doc.path}
                    onClick={() => onPick(doc.id)}
                  >
                    <span className="toc-label">{doc.title}</span>
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {docs.length > DOCS_SHOWN && (
            <button
              type="button"
              className={`toc-more-toggle ${allDocs ? "open" : ""}`}
              aria-expanded={allDocs}
              onClick={() => setAllDocs(!allDocs)}
            >
              <span aria-hidden="true">›</span>{" "}
              {allDocs ? "Fewer documents" : `All documents (${docs.length})`}
            </button>
          )}
        </div>
      )}
      {walks && walks.length > 0 && onWalk && (
        <div className="toc-walks">
          <div className="toc-walks-title" id="toc-walks-title">
            Walkthroughs
          </div>
          <ul className="toc-tree" aria-labelledby="toc-walks-title">
            {walks.map((walk) => (
              <li key={walk.id}>
                <div className="toc-row">
                  <button
                    type="button"
                    className={`toc-link ${walk.id === activeWalk ? "active" : ""}`}
                    aria-current={walk.id === activeWalk ? "page" : undefined}
                    title={walk.label}
                    onClick={() => onWalk(walk.id)}
                  >
                    <span className="toc-label">{walk.label}</span>
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
      {more.length > 0 && (
        <div className="toc-more">
          <button
            type="button"
            className={`toc-more-toggle ${moreOpen ? "open" : ""}`}
            aria-expanded={moreOpen}
            onClick={() => setMoreChoice(!moreOpen)}
          >
            <span aria-hidden="true">›</span> More topics ({more.length})
          </button>
          {moreOpen && renderPages(more)}
        </div>
      )}
    </>
  );
}
