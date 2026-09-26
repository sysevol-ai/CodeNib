// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";

import { repoRelative, type BoundaryRow, type PageBoundary as Boundary } from "@/lib/api";
import { splitSymbolLabel } from "@/lib/symbols";

function Site({ row }: { row: BoundaryRow }) {
  const anchor = row.anchors[0];
  if (!anchor) return null;
  const file = repoRelative(anchor.file) ?? anchor.file;
  const more = row.count > 1 ? ` · ${row.count} sites` : "";
  return (
    <span className="boundary-site mono" title={`${file}${anchor.line ? `:${anchor.line}` : ""}`}>
      {file.split("/").pop() || file}
      {anchor.line != null && <span className="cite-src-loc">:{anchor.line}</span>}
      {more}
    </span>
  );
}

function Side({
  title,
  rows,
  direction,
  onFocus,
}: {
  title: string;
  rows: BoundaryRow[];
  direction: "in" | "out";
  onFocus?: (symbol: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? rows : rows.slice(0, VISIBLE_ROWS);
  const hidden = rows.length - shown.length;
  return (
    <section className={`boundary-side boundary-${direction}`} aria-label={title}>
      <h3 className="boundary-heading">{title}</h3>
      {rows.length === 0 ? (
        <p className="boundary-empty">
          {direction === "in"
            ? "No indexed caller outside this page."
            : "No indexed call leaves this page."}
        </p>
      ) : (
        <ul className="boundary-rows">
          {shown.map((row) => {
            const far = splitSymbolLabel(row.symbol).symbol;
            const near = splitSymbolLabel(row.page_symbol).symbol;
            return (
              <li key={`${row.symbol}->${row.page_symbol}`} className="boundary-row">
                <button
                  type="button"
                  className="boundary-symbol"
                  onClick={onFocus ? () => onFocus(row.symbol) : undefined}
                  disabled={!onFocus}
                  title={onFocus ? `Open ${far} in the dependency map` : row.symbol}
                >
                  <code>{far}</code>
                </button>
                <span className="boundary-link">
                  {direction === "in" ? (
                    <>
                      {row.call ? "calls" : "reads"} <code>{near}</code>
                    </>
                  ) : (
                    <>
                      {row.call ? "called by" : "read by"} <code>{near}</code>
                    </>
                  )}
                </span>
                <Site row={row} />
              </li>
            );
          })}
        </ul>
      )}
      {hidden > 0 && (
        <button type="button" className="boundary-more" onClick={() => setExpanded(true)}>
          Show {hidden} more
        </button>
      )}
    </section>
  );
}

// Rows each side shows before the reader asks for the rest.
const VISIBLE_ROWS = 4;

/** Where a page's symbols sit in the indexed graph: who calls in, what they
 *  call out, each with its recorded call site. No model writes any of it. */
export default function PageBoundary({
  boundary,
  commit,
  onFocus,
  onOpenMap,
}: {
  boundary: Boundary;
  commit?: string | null;
  onFocus?: (symbol: string) => void;
  onOpenMap?: () => void;
}) {
  if (!boundary.available) return null;
  return (
    <aside className="page-boundary" aria-label="Where this page sits in the code graph">
      <header className="boundary-head">
        <span className="boundary-kicker">In the code graph</span>
        <span className="boundary-meta">
          From the index{commit ? ` at ${commit}` : ""}, not written by a model
        </span>
      </header>
      <div className="boundary-grid">
        <Side title="Called from" rows={boundary.inbound} direction="in" onFocus={onFocus} />
        <Side title="Calls into" rows={boundary.outbound} direction="out" onFocus={onFocus} />
      </div>
      {boundary.truncated && onOpenMap && (
        <button type="button" className="boundary-more" onClick={onOpenMap}>
          More connections in the dependency map
        </button>
      )}
    </aside>
  );
}
