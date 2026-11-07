// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

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
          <li className="call-path-step" key={step.id}>
            {index > 0 && <span className="call-path-arrow" aria-hidden="true" />}
            <code className="call-path-symbol">{step.label}</code>
          </li>
        ))}
      </ol>
      <figcaption className="call-path-caption">
        One call path selected from the code index. This code may have others.
      </figcaption>
    </figure>
  );
}
