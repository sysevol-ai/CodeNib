// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useLayoutEffect, useRef, type CSSProperties, type ReactNode } from "react";

import type { Citation } from "@/lib/api";
import { callLabel } from "@/lib/mapRoute";
import { displaySymbol } from "@/lib/symbols";
import { isBareHandoff, type Journey } from "@/lib/wikiPresentation";

/** A call's owner and its own name, so the name can stand forward:
 *  `Session.` + `request()`, `dragonbox::` + `to_decimal()`. */
export function splitCall(symbol: string): [string, string] {
  const open = symbol.indexOf("(");
  const head = open >= 0 ? symbol.slice(0, open) : symbol;
  // A dot inside the argument list is not the owner's.
  const cut = Math.max(head.lastIndexOf("."), head.lastIndexOf("::") + 1);
  if (cut <= 0 || cut >= symbol.length - 1) return ["", symbol];
  return [symbol.slice(0, cut + 1), symbol.slice(cut + 1)];
}

function Call({ symbol, breakable = true }: { symbol: string; breakable?: boolean }) {
  const [owner, name] = splitCall(symbol);
  return (
    <>
      {owner && <span className="main-path-owner">{owner}</span>}
      {owner && breakable && <wbr />}
      <span className="main-path-name">{name}</span>
    </>
  );
}

/** Draw a title's `code` spans as calls, each by its owner and its name,
 *  and each held to the word before it ("From get()", "to HTTPAdapter.send()"),
 *  so a heading breaks between them, never inside a call. */
function titleWithCode(title: string): ReactNode[] {
  const parts = title.split(/`([^`]+)`/);
  const out: ReactNode[] = [];
  parts.forEach((part, index) => {
    if (index % 2 === 0) {
      // The last word before a call goes with the call.
      const next = parts[index + 1];
      const lead = next !== undefined ? /(\S+\s*)$/.exec(part) : null;
      if (lead) out.push(part.slice(0, part.length - lead[1].length));
      else out.push(part);
      return;
    }
    const before = /(\S+\s*)$/.exec(parts[index - 1] ?? "");
    out.push(
      <span className="main-path-title-unit" key={index}>
        {/* A no-break space keeps "to" with its call, even on a phone. */}
        {before ? before[1].replace(/\s+$/, "\u00a0") : ""}
        <code title={part}>
          <Call symbol={displaySymbol(part, { short: true })} breakable={false} />
        </code>
      </span>,
    );
  });
  return out;
}

/** Each step sits right of the one that calls it; a drop into another area
 *  goes twice as far. The whole stair stays within this width. */
const CASCADE_PX = 216;
const CASCADE_STEP_PX = 36;

/** The main path through the code, one step per recorded call.
 *
 * Each call sits a step to the right of its caller, joined to it by a line in
 * the colour of where the call lands, so the path reads as a stack unfolding
 * downwards. The steps come from the index, and only the sentence beside each
 * is written. It is one selected path, so the label says so rather than
 * implying a complete trace of how the program runs.
 */
export default function MainPath({
  journey,
  onPick,
  renderText,
  colors,
  citations,
  onSource,
  roles = false,
  renderAfter,
}: {
  journey: Journey;
  onPick: (pageId: string) => void;
  renderText: (markdown: string) => ReactNode;
  /** Each page's colour on the Wiki map, so a step shows where its code lives. */
  colors?: Map<string, string>;
  /** The page's citations, so each step names the file and line it is defined at. */
  citations?: Citation[];
  /** Open a citation's source, by its index in `citations`. */
  onSource?: (index: number) => void;
  /** The steps are the system's parts, named in words, not calls. */
  roles?: boolean;
  /** What a step hands to the next, set under its sentence. */
  renderAfter?: (position: number) => ReactNode;
}) {
  // A step's move right, in units: one for a call within an area, two where
  // the path drops into another.
  // A drop is any change of area, into the code outside every area too.
  const moves: number[] = journey.stages.map((stage, position) =>
    position === 0 ? 0 : (stage.page?.id ?? null) !== (journey.stages[position - 1]?.page?.id ?? null) ? 2 : 1,
  );
  const units = moves.reduce((sum, move) => sum + move, 0);
  const step = units > 0 ? Math.min(CASCADE_STEP_PX, CASCADE_PX / units) : 0;
  let offset = 0;
  const listRef = useRef<HTMLOListElement>(null);
  // Where any call's file:line would fall onto a line of its own, every
  // step sets its place under its call, so the rows stay alike.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list || typeof ResizeObserver === "undefined") return undefined;
    const check = () => {
      const rows = [...list.querySelectorAll<HTMLElement>(".main-path-symbol-row")];
      const tight = rows.some((row) => {
        const call = row.querySelector<HTMLElement>(".main-path-symbol");
        const place = row.querySelector<HTMLElement>(".main-path-source");
        if (!call || !place) return false;
        // The call's own width on one line: its owner and name, each kept whole.
        const width = [...call.children].reduce((sum, part) => sum + part.getBoundingClientRect().width, 0);
        return width + place.scrollWidth + 16 > row.clientWidth;
      });
      list.classList.toggle("is-stacked", tight);
    };
    check();
    const observer = new ResizeObserver(check);
    observer.observe(list);
    return () => observer.disconnect();
  }, [journey]);
  // Each call's line draws down from its caller and turns into the step as
  // that step comes into view, so the path pours down the page as it is
  // read. Steps already in view when the page opens draw in turn, once.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list || typeof IntersectionObserver === "undefined") return undefined;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return undefined;
    list.classList.add("is-armed");
    const steps = [...list.querySelectorAll<HTMLElement>(".main-path-step")];
    const draw = (step: HTMLElement, delay: number) => {
      step.style.setProperty("--draw-delay", `${delay}s`);
      step.classList.add("is-drawn");
    };
    // Every step whose ring is on screen at load draws, in order, once the
    // page settles; the rest draw as they reach three quarters of the screen.
    const later = steps.filter((step) => {
      const ring = step.querySelector(".main-path-index")?.getBoundingClientRect();
      return !ring || ring.top > window.innerHeight;
    });
    steps.filter((step) => !later.includes(step)).forEach((step, order) => draw(step, 0.35 + order * 0.24));
    const observer = new IntersectionObserver(
      (entries) => {
        entries
          .filter((entry) => entry.isIntersecting)
          .map((entry) => entry.target as HTMLElement)
          .sort((a, b) => steps.indexOf(a) - steps.indexOf(b))
          .forEach((step, order) => {
            draw(step, order * 0.24);
            observer.unobserve(step);
          });
      },
      { rootMargin: "0px 0px -22% 0px" },
    );
    later.forEach((step) => observer.observe(step));
    return () => observer.disconnect();
  }, [journey]);
  return (
    <figure className="main-path" aria-label={journey.title}>
      <figcaption className="main-path-head">
        <h2 className="main-path-title">{titleWithCode(journey.title)}</h2>
        {roles ? (
          <p className="main-path-note">
            The parts in the order work passes through them, each grounded in its cited source
            {colors ? "; each line takes the colour of the area it enters." : "."}
          </p>
        ) : (
          <p
            className="main-path-note"
            title="Each step is a call found in the source, or a base method leading to the subclass that implements it."
          >
            One selected path from the code index{colors ? "; each line takes the colour of the area it enters." : "."}
          </p>
        )}
      </figcaption>
      <ol
        ref={listRef}
        className={`main-path-steps${colors ? " is-coloured" : ""}${roles ? " is-roles" : ""}`}
        style={{ "--cascade": `${step}px`, "--units": Math.max(units, 1) } as CSSProperties}
      >
        {journey.stages.map((stage, position) => {
          const area = stage.page ? colors?.get(stage.page.id) : undefined;
          // The line under a step is the call to the next one, in the colour
          // of where that call lands, as on the map.
          const nextStage = journey.stages[position + 1];
          const next = nextStage?.page ? colors?.get(nextStage.page.id) : undefined;
          const cited = stage.evidence
            .map((id) => Number(id.replace(/^E/, "")) - 1)
            .find((index) => index >= 0 && !!citations?.[index]?.file);
          const source = cited !== undefined && citations ? citations[cited] : null;
          const file = source?.file ? source.file.split("/").pop() : null;
          // The path names an area where it enters one; the steps after it
          // keep its colour on their rings and lines.
          const entry = !!stage.page && journey.stages[position - 1]?.page?.id !== stage.page.id;
          // Code in no area of the map (an entry point, a vendored library)
          // says so where the path reaches it, so its grey reads as meant.
          const outside =
            !!colors && !stage.page && (position === 0 || !!journey.stages[position - 1]?.page);
          offset += moves[position];
          // The line down from a step leaves in its own area's colour and
          // turns the colour of the next step's area at the elbow.
          const own = area;
          return (
            <li
              className={`main-path-step${area ? " has-area" : ""}${next ? " has-next" : ""}${source ? " has-source" : ""}${entry || outside ? " is-entry" : ""}`}
              key={stage.index}
              style={
                {
                  "--depth": position,
                  "--offset": offset,
                  "--move": moves[position],
                  ...(area ? { "--area": area } : {}),
                  ...(own ? { "--down": own } : {}),
                  ...(next ? { "--next": next } : {}),
                } as CSSProperties
              }
            >
              {entry && stage.page && (
                <button
                  type="button"
                  className="main-path-area"
                  onClick={() => onPick(stage.page!.id)}
                >
                  {stage.page.title}
                </button>
              )}
              {outside && <span className="main-path-area is-outside">Outside the map's areas</span>}
              <span className="main-path-index" aria-hidden="true">
                {stage.index}
              </span>
              <div className="main-path-body">
                <div className="main-path-symbol-row">
                  {roles ? (
                    <h3 className="main-path-role">{stage.symbol}</h3>
                  ) : (
                    <code className="main-path-symbol" title={stage.symbol}>
                      <Call symbol={callLabel(displaySymbol(stage.symbol, { short: true }))} />
                    </code>
                  )}
                  {source && file && (
                    <button
                      type="button"
                      className="main-path-source"
                      title={`${source.file}:${source.start_line}`}
                      onClick={() => onSource?.(cited!)}
                    >
                      {file}
                      <span className="main-path-source-line">:{source.start_line}</span>
                    </button>
                  )}
                </div>
                {/* The arrow already says this when no sentence was admitted. */}
                {!isBareHandoff(stage.text) && (
                  <div className="main-path-text">{renderText(stage.text)}</div>
                )}
                {renderAfter?.(position)}
              </div>
            </li>
          );
        })}
      </ol>
    </figure>
  );
}
