// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { CSSProperties } from "react";

import { splitCall } from "@/components/MainPath";
import type { WikiMediaSlot } from "@/lib/api";
import { callPathSteps } from "@/lib/wikiPresentation";

/** A page's call path, drawn from a contract the index supplied. */
export default function CallPathFigure({ slot }: { slot: WikiMediaSlot }) {
  const steps = callPathSteps(slot);
  if (steps.length < 2) return null;
  return (
    <figure className="call-path" aria-label={slot.title}>
      <ol className="call-path-steps">
        {steps.map((step, index) => (
          <li
            className="call-path-step"
            key={step.id}
            style={{ "--depth": Math.min(index, 6) } as CSSProperties}
          >
            <code className="call-path-symbol" title={step.label}>
              {(() => {
                const [owner, name] = splitCall(step.label);
                return (
                  <>
                    {owner && <span className="main-path-owner">{owner}</span>}
                    {owner && <wbr />}
                    <span className="main-path-name">{name}</span>
                  </>
                );
              })()}
            </code>
          </li>
        ))}
      </ol>
      <figcaption className="call-path-caption">
        One call path selected from the code index. This code may have others.
      </figcaption>
    </figure>
  );
}
