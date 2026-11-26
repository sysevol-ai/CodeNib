// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState, type CSSProperties, type ReactNode } from "react";

import { fetchWalkthroughs } from "@/lib/api";
import { assetUrl } from "@/lib/runtime";
import { stopArea, stopInfo, type Walk, type WalkthroughSet } from "@/lib/walkthrough";
import { NEUTRAL_COLOR } from "@/lib/wikiGraph";
import "./WalkShareCard.css";

/** Rows the 630 px image holds; a longer walk shows its start and its end. */
const MAX_ROWS = 7;

interface Row {
  name: string;
  place: string | null;
  area: string | null;
  color: string;
  own: boolean;
}

function rowsOf(set: WalkthroughSet, walk: Walk): (Row | { gap: number })[] {
  const all: Row[] = walk.stops.map((_, index) => {
    const stop = stopInfo(walk, index);
    const areaId = index === 0 ? null : stopArea(set, walk, index);
    const area = areaId ? set.areas.find((item) => item.id === areaId) : null;
    return {
      name: stop.full,
      place: stop.ln,
      // The full name the Wiki gives the area; stops are labelled by colour.
      area: area?.title ?? area?.name ?? null,
      color: area?.color ?? NEUTRAL_COLOR,
      own: index === 0,
    };
  });
  if (all.length <= MAX_ROWS) return all;
  const head = all.slice(0, MAX_ROWS - 3);
  const tail = all.slice(-2);
  return [...head, { gap: all.length - head.length - tail.length }, ...tail];
}

const CODE_IN_TITLE = /([\w.]+\([^)]*\))/;

/**
 * A title in sentence case. Generated titles sometimes arrive in Title Case
 * ("Builds the Tree"); when most longer words are capitalised, the ones that
 * are plain capitalised words are lowered. Code is left alone.
 */
export function sentenceCase(title: string): string {
  const parts = title.split(CODE_IN_TITLE);
  const words = parts.filter((_, index) => index % 2 === 0).join(" ").split(/\s+/).slice(1);
  const long = words.filter((word) => /^[A-Za-z]{4,}$/.test(word));
  const capitalised = long.filter((word) => /^[A-Z][a-z]+$/.test(word));
  if (long.length < 2 || capitalised.length / long.length < 0.6) return title;
  let first = true;
  return parts
    .map((part, index) => {
      if (index % 2) {
        first = false;
        return part;
      }
      return part.replace(/[A-Za-z]+/g, (word) => {
        const keep = first;
        first = false;
        return keep || !/^[A-Z][a-z]+$/.test(word) ? word : word.toLowerCase();
      });
    })
    .join("");
}

/** Code in a title, such as `requests.get()`, set as code that never breaks. */
function titleWithCode(title: string): ReactNode[] {
  return sentenceCase(title).split(CODE_IN_TITLE).map((part, index) =>
    index % 2 ? (
      <code key={index}>{part}</code>
    ) : (
      part
    ),
  );
}

/** A title size at which its longest piece of code fits the 500 px column. */
function titleSize(title: string): number {
  const longest = Math.max(0, ...(title.match(new RegExp(CODE_IN_TITLE, "g")) ?? []).map((code) => code.length));
  // Geist Mono at 0.86em is about 0.52 em per character.
  return Math.min(46, Math.floor(500 / Math.max(1, longest * 0.52)));
}

/** The line below a stop is the call that leaves it, in the colour of where it lands. */
function nextColor(rows: (Row | { gap: number })[], index: number): string | undefined {
  const next = rows.slice(index + 1).find((row): row is Row => !("gap" in row));
  return index + 1 < rows.length ? next?.color : undefined;
}

/**
 * The 1200x630 image a shared walkthrough link shows: the question and the
 * areas it passes through on the left, the functions it follows on the
 * right, each dot in its map area's colour. `render-share-cards.mjs --walks` screenshots it.
 */
export default function WalkShareCard({ repoId, walkId, host }: { repoId: string; walkId: string; host: string }) {
  const [set, setSet] = useState<WalkthroughSet | null | undefined>(undefined);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    fetchWalkthroughs(repoId)
      .catch(() => null)
      .then((value) => {
        if (active) setSet(value);
      });
    return () => {
      active = false;
    };
  }, [repoId]);

  const walk = set?.walks.find((item) => item.id === walkId) ?? null;
  useEffect(() => {
    if (set === undefined) return;
    void document.fonts.ready.then(() => requestAnimationFrame(() => requestAnimationFrame(() => setReady(true))));
  }, [set]);

  if (set !== undefined && !walk) {
    return <div className="walk-card" data-share-ready={ready ? "missing" : undefined} />;
  }
  const rows = set && walk ? rowsOf(set, walk) : [];
  // The areas in the order the walk first reaches them.
  const areas = rows.reduce<{ name: string; color: string }[]>((found, row) => {
    if (!("gap" in row) && row.area && !found.some((area) => area.name === row.area)) {
      found.push({ name: row.area, color: row.color });
    }
    return found;
  }, []);
  const calls = walk ? walk.stops.length - 1 : 0;
  return (
    <div className="walk-card" data-share-ready={ready && walk ? "true" : undefined}>
      <div className="walk-card-text">
        <span className="walk-card-brand">
          <img src={assetUrl("/codenib-icon.svg")} alt="" /> CodeNib Wiki
        </span>
        <p className="walk-card-repo">{set?.repo.slug}</p>
        <h1 style={walk ? { fontSize: titleSize(walk.title) } : undefined}>{walk ? titleWithCode(walk.title) : null}</h1>
        {areas.length > 0 && (
          <ul className="walk-card-areas" aria-label="Areas of the code it passes through">
            <li className="is-own">
              <span className="walk-card-dot" aria-hidden="true" />
              Your call
            </li>
            {areas.map((area) => (
              <li key={area.name} style={{ "--area": area.color } as CSSProperties}>
                <span className="walk-card-dot" aria-hidden="true" />
                {area.name}
              </li>
            ))}
          </ul>
        )}
        <p className="walk-card-foot">
          <span>
            {calls} {calls === 1 ? "step" : "steps"} traced in the code index
          </span>

        </p>
      </div>
      <ol
        className="walk-card-trace"
        // A short walk spreads out over the column instead of leaving it empty.
        style={(() => {
          const gap = `${Math.round(Math.min(56, Math.max(20, (440 - rows.length * 36) / Math.max(1, rows.length - 1))))}px`;
          return { gap, "--gap": gap } as CSSProperties;
        })()}
      >
        {rows.map((row, index) =>
          "gap" in row ? (
            <li key={`gap-${index}`} className="walk-card-gap" style={{ "--next": nextColor(rows, index) } as CSSProperties}>
              {row.gap} more {row.gap === 1 ? "step" : "steps"}
            </li>
          ) : (
            <li
              key={index}
              className={`walk-card-stop${row.own ? " is-own" : ""}`}
              style={{ "--area": row.color, "--next": nextColor(rows, index) } as CSSProperties}
            >
              <span className="walk-card-dot" aria-hidden="true" />
              <span className="walk-card-name">{row.name}</span>
            </li>
          ),
        )}
      </ol>
    </div>
  );
}
