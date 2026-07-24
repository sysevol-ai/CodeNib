"use client";

import { useEffect, useState } from "react";
import GraphView from "@/components/GraphView";
import { fetchCodemap, type CodemapResponse } from "@/lib/api";

type Direction = "both" | "callees" | "callers";
type Depth = 1 | 2;

/**
 * Codemap mode: an interactive dependency (call-graph) map for the repo.
 * Walks reference edges out from a focus symbol (or the repo's busiest symbol
 * by default) and renders them via the shared GraphView (Cytoscape graph +
 * source peek). A chip — or "Focus here" in a node's peek — re-roots the map.
 */
export default function Codemap({
  repoId,
  initialSymbol,
  commit,
}: {
  repoId: string;
  initialSymbol?: string;
  // Commit snapshot to render. Undefined = the window's newest commit (or the
  // repo's single indexed graph when no window exists).
  commit?: string;
}) {
  const [symbol, setSymbol] = useState(initialSymbol ?? "");
  const [query, setQuery] = useState(initialSymbol ?? ""); // last submitted focus symbol
  const [direction, setDirection] = useState<Direction>("both");
  const [depth, setDepth] = useState<Depth>(1);
  const [data, setData] = useState<CodemapResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Re-seed when an external focus arrives (e.g. "explore in graph" from a wiki page).
  useEffect(() => {
    if (initialSymbol) {
      setSymbol(initialSymbol);
      setQuery(initialSymbol);
    }
  }, [initialSymbol]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetchCodemap(repoId, {
      symbol: query || undefined,
      direction,
      depth,
      maxNodes: depth === 1 ? 28 : 40,
      commit,
    })
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setErr(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, query, direction, depth, commit]);

  function focus(label: string) {
    setSymbol(label);
    setQuery(label);
  }

  return (
    <div className="codemap">
      <form
        className="codemap-controls"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(symbol.trim());
        }}
      >
        <input
          className="codemap-symbol"
          value={symbol}
          placeholder="Focus symbol (e.g. Class.method) — blank for the busiest"
          onChange={(e) => setSymbol(e.target.value)}
          aria-label="Focus symbol"
        />
        <select
          value={direction}
          onChange={(e) => setDirection(e.target.value as Direction)}
          aria-label="Edge direction"
        >
          <option value="both">callers + callees</option>
          <option value="callees">callees (what it calls)</option>
          <option value="callers">callers (what calls it)</option>
        </select>
        <select
          value={depth}
          onChange={(e) => setDepth(Number(e.target.value) as Depth)}
          aria-label="Traversal depth"
        >
          <option value={1}>1 hop</option>
          <option value={2}>2 hops</option>
        </select>
        <button type="submit">Map</button>
      </form>

      {loading && <p className="muted">Building dependency map…</p>}
      {err && <p className="muted">Couldn&apos;t load the codemap.</p>}

      {data && data.available && (
        <>
          <div className="codemap-meta mono">
            {data.root_label} · {data.nodes.length} symbols · {data.edges.length} edges
            {data.truncated ? " · truncated" : ""}
          </div>
          {data.fell_back && (
            <p className="muted small" role="status">
              The selected snapshot could not be loaded. Showing the default graph
              {data.commit ? ` at ${data.commit.slice(0, 8)}` : ""}.
            </p>
          )}
          {data.note && <p className="muted small">{data.note}</p>}
          <GraphView repoId={repoId} data={data} variant="explore" onFocus={focus} />
          <div className="codemap-nodes">
            {data.nodes.map((n) => (
              <button
                key={n.id}
                className={`codemap-chip ${n.is_root ? "root" : ""}`}
                title={`${n.label}${n.file ? `  (${n.file}:${n.line ?? "?"})` : ""}`}
                onClick={() => focus(n.label)}
              >
                {n.short}
              </button>
            ))}
          </div>
        </>
      )}

      {data && !data.available && (
        <p className="muted">This repo has no symbol graph, so no codemap is available.</p>
      )}
    </div>
  );
}
