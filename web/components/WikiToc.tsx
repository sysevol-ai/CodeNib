// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";

import type { WikiPageRef } from "@/lib/api";
import { pagePath, partitionWikiNav } from "@/lib/wikiNav";

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
}: {
  pages: WikiPageRef[];
  activeId: string;
  onPick: (id: string) => void;
  /** Each page's colour on the map, when the map is showing. */
  colors?: Map<string, string>;
}) {
  const [choices, setChoices] = useState<Record<string, boolean>>({});
  const [moreChoice, setMoreChoice] = useState<boolean | null>(null);
  const { primary, more } = partitionWikiNav(pages);
  const activePath = pagePath(pages, activeId);
  const moreOpen = moreChoice ?? more.some((page) => activePath.includes(page.id));

  const renderPages = (items: WikiPageRef[]) => (
    <ul className="toc-tree">
      {items.map((page) => {
        const open = choices[page.id] ?? activePath.includes(page.id);
        const detailCount = page.children.length;
        return (
          <li key={page.id}>
            <div className="toc-row">
              <button
                className={`toc-link ${page.id === activeId ? "active" : ""}`}
                aria-current={page.id === activeId ? "page" : undefined}
                title={page.title}
                onClick={() => onPick(page.id)}
              >
                {colors?.has(page.id) && (
                  <span className="toc-dot" style={{ background: colors.get(page.id) }} aria-hidden="true" />
                )}
                <span className="toc-label">{page.title}</span>
                {page.cache_state && page.cache_state !== "ready" && (
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
