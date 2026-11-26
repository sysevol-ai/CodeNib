// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { CSSProperties, ReactNode } from "react";

import { displaySymbol } from "@/lib/symbols";
import { isBareHandoff, type Journey } from "@/lib/wikiPresentation";

/** Draw a title's `code` spans as code, each symbol by its owner and member:
 *  "From `printf()` to `dragonbox.shorter_interval_case()`". */
function titleWithCode(title: string): ReactNode[] {
  return title.split(/`([^`]+)`/).map((part, index) =>
    index % 2 ? (
      <code key={index} title={part}>
        {displaySymbol(part, { short: true })}
      </code>
    ) : (
      part
    ),
  );
}

/** The main path through the code, one step per recorded call.
 *
 * The steps come from the index, and only the sentence beside each is written.
 * It is one selected path, so the label says so rather than implying a complete
 * trace of how the program runs.
 */
export default function MainPath({
  journey,
  onPick,
  renderText,
  colors,
}: {
  journey: Journey;
  onPick: (pageId: string) => void;
  renderText: (markdown: string) => ReactNode;
  /** Each page's colour on the Wiki map, so a step shows where its code lives. */
  colors?: Map<string, string>;
}) {
  return (
    <figure className="main-path" aria-label={journey.title}>
      <figcaption className="main-path-head">
        <h2 className="main-path-title">{titleWithCode(journey.title)}</h2>
        <p className="main-path-note">
          One path selected from the code index. Each arrow is a call found in the
          source, or a base method leading to the subclass that implements it.
          {colors && " Each colour is the area of the Wiki map where a step's code lives."}
        </p>
      </figcaption>
      <ol className="main-path-steps">
        {journey.stages.map((stage, position) => {
          const area = stage.page ? colors?.get(stage.page.id) : undefined;
          // The line under a step is the call to the next one, in the colour
          // of where that call lands, as on the map.
          const nextStage = journey.stages[position + 1];
          const next = nextStage?.page ? colors?.get(nextStage.page.id) : undefined;
          return (
          <li
            className={`main-path-step${area ? " has-area" : ""}${next ? " has-next" : ""}`}
            key={stage.index}
            style={{ ...(area ? { "--area": area } : {}), ...(next ? { "--next": next } : {}) } as CSSProperties}
          >
            <span className="main-path-index" aria-hidden="true">
              {stage.index}
            </span>
            <div className="main-path-body">
              <div className="main-path-symbol-row">
                <code className="main-path-symbol" title={stage.symbol}>
                  {displaySymbol(stage.symbol, { short: true })}
                </code>
                {stage.page && (
                  <button
                    type="button"
                    className="main-path-area"
                    onClick={() => onPick(stage.page!.id)}
                  >
                    {stage.page.title}
                  </button>
                )}
              </div>
              {/* The arrow already says this when no sentence was admitted. */}
              {!isBareHandoff(stage.text) && (
                <div className="main-path-text">{renderText(stage.text)}</div>
              )}
            </div>
          </li>
          );
        })}
      </ol>
    </figure>
  );
}
