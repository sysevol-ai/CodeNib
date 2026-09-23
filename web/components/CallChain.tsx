// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";

import { edgeCallSite, type ChainStep } from "@/lib/flowchart";
import { repoRelative, type WikiRelationItem } from "@/lib/api";

/** A straight call path read top to bottom: symbol, the call it makes, the
 *  next symbol. Each arrow names its recorded call site when there is one. */
export default function CallChain({
  paths,
  relations,
  renderSymbol,
}: {
  paths: ChainStep[][];
  relations?: WikiRelationItem[];
  /** Lets the page turn a symbol into a jump-to-source chip. */
  renderSymbol?: (label: string) => ReactNode;
}) {
  return (
    <div className="call-chains">
      {paths.map((steps, index) => (
        <Path
          key={steps[0]?.node.id || index}
          steps={steps}
          relations={relations}
          renderSymbol={renderSymbol}
        />
      ))}
    </div>
  );
}

function Path({
  steps,
  relations,
  renderSymbol,
}: {
  steps: ChainStep[];
  relations?: WikiRelationItem[];
  renderSymbol?: (label: string) => ReactNode;
}) {
  const byId = new Map(steps.map((step) => [step.node.id, step.node]));
  return (
    <ol className="call-chain" aria-label="Call path">
      {steps.map((step) => {
        const target = step.next ? byId.get(step.next.to) : undefined;
        const site =
          step.next && target
            ? edgeCallSite(step.node.label, target.label, relations)
            : null;
        const at = site ? site.lastIndexOf(":") : -1;
        const file = site && at > 0 ? site.slice(0, at) : site || "";
        const line = site && at > 0 ? site.slice(at + 1) : "";
        const shown = file ? repoRelative(file) ?? file : "";
        return (
          <li key={step.node.id} className="call-chain-step">
            <span className="call-chain-symbol">
              {renderSymbol ? renderSymbol(step.node.label) : <code>{step.node.label}</code>}
            </span>
            {step.next && (
              <span className="call-chain-call">
                <span className="call-chain-label">{step.next.label || "calls"}</span>
                {site && (
                  <span className="cite-src" title={`${shown}${line ? `:${line}` : ""}`}>
                    {shown.split("/").pop() || shown}
                    {line && <span className="cite-src-loc">:{line}</span>}
                  </span>
                )}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
