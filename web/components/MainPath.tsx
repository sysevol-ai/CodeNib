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
        </p>
      </figcaption>
      <ol className="main-path-steps">
        {journey.stages.map((stage) => (
          <li
            className={`main-path-step${stage.page && colors?.get(stage.page.id) ? " has-area" : ""}`}
            key={stage.index}
            style={
              stage.page && colors?.get(stage.page.id)
                ? ({ "--area": colors.get(stage.page.id) } as CSSProperties)
                : undefined
            }
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
                    <span className="main-path-area-dot" aria-hidden="true" />
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
        ))}
      </ol>
    </figure>
  );
}
