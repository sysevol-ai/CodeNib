// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { CSSProperties } from "react";

import type { WikiArea, WikiPageRef } from "@/lib/api";
import { NEUTRAL_COLOR } from "@/lib/wikiGraph";
import type { PageLink } from "@/lib/wikiPresentation";

import "./ChapterCards.css";

/** Most pages a chapter's mark draws; the count says the rest. */
const MARK_PAGES = 7;

/**
 * A chapter drawn as the Wiki map draws it: the area's node in its colour and
 * one dot per page under it, so the card and the map name a chapter alike.
 */
export function ChapterMark({ pages }: { pages: number }) {
  const shown = Math.min(pages, MARK_PAGES);
  const dots = Array.from({ length: shown }, (_, index) => {
    // Fanned to the right of the node, alternately near and far, as the
    // map's page dots sit around their area.
    const spread = shown <= 3 ? 70 : 116;
    const angle = shown === 1 ? 0 : -spread / 2 + (spread * index) / (shown - 1);
    const reach = shown > 4 ? (index % 2 ? 30 : 22) : 25;
    const radians = (angle * Math.PI) / 180;
    return { x: 14 + reach * Math.cos(radians), y: 24 + reach * Math.sin(radians) };
  });
  return (
    <svg className="chapter-mark" width="64" height="48" viewBox="0 0 64 48" aria-hidden="true">
      {dots.map((dot, index) => (
        <line key={`l${index}`} x1="14" y1="24" x2={dot.x} y2={dot.y} />
      ))}
      {dots.map((dot, index) => (
        <circle key={`d${index}`} className="chapter-mark-page" cx={dot.x} cy={dot.y} r="2.6" />
      ))}
      <circle className="chapter-mark-area" cx="14" cy="24" r="7" />
    </svg>
  );
}

/**
 * The Overview's chapters as cards: each names its pages and what the index
 * holds for it. Only the Overview lists every chapter; other pages keep the
 * quieter Read next rows.
 */
export default function ChapterCards({
  links,
  pages,
  areas,
  colors,
  onPick,
}: {
  links: PageLink[];
  pages: WikiPageRef[];
  areas?: WikiArea[];
  colors: Map<string, string>;
  onPick: (pageId: string) => void;
}) {
  if (links.length === 0) return null;
  return (
    <nav className="chapter-cards" aria-label="Chapters">
      <h2>Chapters</h2>
      <ul>
        {links.map((link) => {
          const children = pages.find((page) => page.id === link.id)?.children ?? [];
          const area = areas?.find((item) => item.id === link.id);
          const named = children.slice(0, 2).map((child) => child.title);
          const more = children.length - named.length;
          return (
            <li key={link.id}>
              <button
                type="button"
                className="chapter-card"
                onClick={() => onPick(link.id)}
                style={{ "--area": colors.get(link.id) ?? NEUTRAL_COLOR } as CSSProperties}
              >
                <ChapterMark pages={children.length} />
                <span className="chapter-title">{link.title}</span>
                {named.length > 0 && (
                  <span className="chapter-pages">
                    {named.join(" · ")}
                    {more > 0 && ` · ${more} more`}
                  </span>
                )}
                <span className="chapter-meta">
                  <span>
                    {area && area.symbols > 0
                      ? `${area.symbols.toLocaleString("en-US")} symbols · ${area.files.toLocaleString("en-US")} ${area.files === 1 ? "file" : "files"}`
                      : `${children.length + 1} ${children.length ? "pages" : "page"}`}
                  </span>
                  <svg
                    className="chapter-arrow"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M5 12h14M13 6l6 6-6 6" />
                  </svg>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
