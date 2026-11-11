// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { CSSProperties } from "react";

import { NEUTRAL_COLOR } from "@/lib/wikiGraph";
import type { PageLink } from "@/lib/wikiPresentation";

/**
 * The pages to read after this one, listed once. The generator writes a
 * "Related pages" list and the outline knows the detail pages below; shown
 * separately they were two lists of the same titles. Each row carries its
 * area's colour from the map, so the list and the map name pages alike.
 */
export default function ReadNext({
  title,
  links,
  colors,
  onPick,
}: {
  title: string;
  links: PageLink[];
  colors?: Map<string, string>;
  onPick: (pageId: string) => void;
}) {
  if (links.length === 0) return null;
  return (
    <nav className="wiki-read-next" aria-label={title}>
      <h2>{title}</h2>
      <ul>
        {links.map((link) => (
          <li key={link.id}>
            <button
              type="button"
              onClick={() => onPick(link.id)}
              style={{ "--area": colors?.get(link.id) ?? NEUTRAL_COLOR } as CSSProperties}
            >
              <span className="wiki-read-next-dot" aria-hidden="true" />
              <span className="wiki-read-next-title">{link.title}</span>
              <svg
                className="wiki-read-next-arrow"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
