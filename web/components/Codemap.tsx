"use client";

import { useEffect, useState } from "react";
import Mermaid from "@/components/Mermaid";
import { fetchCodemap, type CodemapResponse } from "@/lib/api";

type Direction = "both" | "callees" | "callers";

/**
 * Codemap mode: an interactive dependency (call-graph) map for the repo.
 * Walks reference edges out from a focus symbol (or the repo's busiest symbol
 * by default) and renders them with the shared Mermaid component. Clicking a
 * symbol chip refocuses the map on it.
 */
export default function Codemap({ repoId }: { repoId: string }) {
  const [symbol, setSymbol] = useState("");
  const [query, setQuery] = useState(""); // last submitted focus symbol
  const [direction, setDirection] = useState<Direction>("both");
  const [data, setData] = useState<CodemapResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetchCodemap(repoId, {
      symbol: query || undefined,
      direction,
      depth: 2,
      maxNodes: 18,
    })
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setErr(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, query, direction]);

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
          {data.note && <p className="muted small">{data.note}</p>}
          <Mermaid chart={data.mermaid} />
          <p className="muted small">
            Reference (call) edges. Click a symbol to refocus the map.
          </p>
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
