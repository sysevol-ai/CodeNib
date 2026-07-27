"use client";

import { useEffect, useState } from "react";
import GraphView from "@/components/GraphView";
import {
  fetchCodemap,
  type CodemapResponse,
  type GraphCoverage,
} from "@/lib/api";

type Direction = "both" | "callees" | "callers";
type Depth = 1 | 2;

const LANGUAGE_NAMES: Record<string, string> = {
  cpp: "C / C++",
  csharp: "C#",
  go: "Go",
  java: "Java",
  javascript: "JavaScript",
  kotlin: "Kotlin",
  php: "PHP",
  python: "Python",
  ruby: "Ruby",
  rust: "Rust",
  ts: "TypeScript / JavaScript",
  typescript: "TypeScript",
};

function languageNames(languages: string[]): string {
  return languages.map((language) => LANGUAGE_NAMES[language] ?? language).join(", ");
}

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
  coverage,
}: {
  repoId: string;
  initialSymbol?: string;
  // Commit snapshot to render. Undefined = the window's newest commit (or the
  // repo's single indexed graph when no window exists).
  commit?: string;
  coverage?: GraphCoverage | null;
}) {
  const [symbol, setSymbol] = useState(initialSymbol ?? "");
  const [query, setQuery] = useState(initialSymbol ?? ""); // last submitted focus symbol
  const [direction, setDirection] = useState<Direction>("both");
  const [depth, setDepth] = useState<Depth>(1);
  const [data, setData] = useState<CodemapResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

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

  if (loading && data === null) {
    return (
      <div className="codemap codemap-loading" role="status">
        Loading dependency map…
      </div>
    );
  }

  if (data && !data.available) {
    const setup = [
      'pip install "codenib[graph]"',
      "codenib doctor --require graph",
      "codenib wiki . --preset graph",
    ].join("\n");
    const reason = data.note?.replace(/\.\s*$/, "");
    return (
      <div className="codemap codemap-unavailable">
        <div className="codemap-unavailable-icon" aria-hidden>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
            <circle cx="6" cy="6" r="2.5" />
            <circle cx="18" cy="7" r="2.5" />
            <circle cx="12" cy="18" r="2.5" />
            <path d="m8.3 7 7.2-.1M7.5 8.2l3.3 7.4m5.7-6.3-3.2 6.4" />
          </svg>
        </div>
        <div className="codemap-unavailable-copy">
          <span className="codemap-unavailable-kicker">Optional graph view</span>
          <h2>Build the repository dependency map</h2>
          <p>
            This Wiki is ready for search and source browsing. Dependency
            exploration needs a language-aware symbol graph for this commit.
          </p>
          {reason && <p className="small muted">{reason}.</p>}
        </div>
        <div className="codemap-setup">
          <div className="codemap-setup-head">
            <span>Run from the repository root</span>
            <button
              type="button"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(setup);
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1200);
                } catch {}
              }}
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <pre>{setup}</pre>
        </div>
      </div>
    );
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

      {coverage?.partial && (
        <div className="codemap-coverage" role="status">
          <span className="codemap-coverage-title">Partial language coverage</span>
          <span>
            Indexed: {languageNames(coverage.available_languages) || "none"}
          </span>
          <span>
            Unavailable: {languageNames(coverage.unavailable_languages) || "none"}
          </span>
        </div>
      )}

      {loading && <p className="muted">Updating dependency map…</p>}
      {err && <p className="muted">Couldn&apos;t load the dependency map.</p>}

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
    </div>
  );
}
