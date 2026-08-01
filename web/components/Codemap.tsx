"use client";

import { useEffect, useState } from "react";
import GraphView from "@/components/GraphView";
import HierarchyMap from "@/components/HierarchyMap";
import {
  fetchCodemap,
  fetchModulemap,
  type CodemapResponse,
  type GraphCoverage,
  type ModulemapResponse,
} from "@/lib/api";

type Direction = "both" | "callees" | "callers";
type Depth = 1 | 2;
type View = "graph" | "structure" | "modules";

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

/** Total exact reference sites behind the drawn edges. */
function callSites(edges: CodemapResponse["edges"]): number {
  return edges.reduce((sum, edge) => sum + (edge.weight ?? edge.anchors?.length ?? 0), 0);
}

/** Module edges keep the true total in `call_sites`; `anchors` is a sample. */
function moduleCallSites(edges: ModulemapResponse["edges"]): number {
  return edges.reduce((sum, edge) => sum + (edge.call_sites ?? 0), 0);
}

/**
 * Module view: "which file depends on which", projected from symbol references
 * (see `codenib/web/modulemap.py`). Clicking a module re-centres the map on it;
 * clicking an edge opens the exact call sites behind the dependency, which is
 * the part no import-statement parser can give you.
 */
function ModuleView({
  repoId,
  data,
  loading,
  focus,
  onFocus,
}: {
  repoId: string;
  data: ModulemapResponse | null;
  loading: boolean;
  focus: string;
  onFocus: (path: string) => void;
}) {
  if (loading && !data) {
    return (
      <p className="muted" role="status">
        Projecting module dependencies…
      </p>
    );
  }
  if (!data || !data.available) {
    return <p className="muted">No module dependencies for this repo.</p>;
  }

  const excluded = data.excluded;
  const dropped = (excluded?.test_files ?? 0) + (excluded?.derived_files ?? 0);

  return (
    <>
      <div className="codemap-meta mono">
        {focus ? `${data.focus_label} · ` : ""}
        {data.nodes.length} {data.granularity === "file" ? "files" : "directories"} ·{" "}
        {data.edges.length} dependencies
        {moduleCallSites(data.edges) > 0 &&
          ` · ${moduleCallSites(data.edges)} exact call sites`}
        {data.truncated ? ` · top ${data.nodes.length} of ${data.total_modules}` : ""}
      </div>
      {/* State what the projection left out rather than implying full coverage. */}
      {dropped > 0 && (
        <p className="muted small">
          Excludes {excluded?.test_files ?? 0} test and{" "}
          {excluded?.derived_files ?? 0} generated/declaration files.
        </p>
      )}
      {data.note && <p className="muted small">{data.note}</p>}
      {focus && (
        <p className="muted small">
          Centred on {data.focus_label}.{" "}
          <button type="button" className="linklike" onClick={() => onFocus("")}>
            Show the whole repo
          </button>
        </p>
      )}
      <GraphView
        repoId={repoId}
        data={data}
        variant="modules"
        onFocus={(label) => onFocus(label.replace(/\/$/, ""))}
      />
      <div className="codemap-nodes">
        {data.nodes.map((n) => (
          <button
            key={n.id}
            className={`codemap-chip ${n.is_root ? "root" : ""}`}
            title={`${n.path} — ${n.symbol_count} symbols, referenced by ${n.ref_count ?? 0}`}
            onClick={() => onFocus(n.path)}
          >
            {n.short}
          </button>
        ))}
      </div>
    </>
  );
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
  const [view, setView] = useState<View>("structure");
  const [data, setData] = useState<CodemapResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // Module view: which file/directory the map is centred on ("" = repo-wide).
  const [moduleFocus, setModuleFocus] = useState("");
  const [modules, setModules] = useState<ModulemapResponse | null>(null);
  const [modulesLoading, setModulesLoading] = useState(false);

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

  // The module map is its own fetch: it projects the whole graph rather than
  // walking out from a symbol, so it does not share the symbol controls.
  useEffect(() => {
    if (view !== "modules") return;
    let cancelled = false;
    setModulesLoading(true);
    fetchModulemap(repoId, { focus: moduleFocus || undefined, depth: 2, commit })
      .then((d) => !cancelled && setModules(d))
      .catch(() => !cancelled && setModules(null))
      .finally(() => !cancelled && setModulesLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, view, moduleFocus, commit]);

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
    const setup = data.setup;
    const commands = setup?.commands.join("\n") ?? "";
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
        <div className="codemap-setup" aria-label="Dependency Map setup status">
          <div className="codemap-setup-head">
            <span>Repository-specific setup</span>
            {commands && (
              <button
                type="button"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(commands);
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1200);
                  } catch {}
                }}
              >
                {copied ? "Copied" : "Copy"}
              </button>
            )}
          </div>
          {setup?.languages.length ? (
            <div className="codemap-setup-languages">
              {setup.languages.map((language) => (
                <div className="codemap-setup-language" key={language.language}>
                  <span
                    className={`codemap-setup-state ${language.state}`}
                    aria-label={language.state}
                  />
                  <div>
                    <strong>{language.display_name}</strong>
                    <span>
                      {language.state === "ready"
                        ? `${language.backend} provider ready`
                        : language.state === "unsupported"
                          ? "Symbol graph not supported"
                          : `Missing ${language.missing.join(", ")}`}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          {setup?.install_hints.length ? (
            <ul className="codemap-setup-hints">
              {setup.install_hints.map((hint) => (
                <li key={hint}>{hint}</li>
              ))}
            </ul>
          ) : null}
          {commands && <pre>{commands}</pre>}
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

      {/* The graph answers "what connects to what"; the structure view answers
          "what is in here", which is where a reader who does not yet know the
          repo has to start. */}
      <div className="codemap-views" role="group" aria-label="Map view">
        <button
          type="button"
          className={view === "graph" ? "is-active" : ""}
          aria-pressed={view === "graph"}
          onClick={() => setView("graph")}
        >
          Graph
        </button>
        <button
          type="button"
          className={view === "structure" ? "is-active" : ""}
          aria-pressed={view === "structure"}
          onClick={() => setView("structure")}
        >
          Structure
        </button>
        <button
          type="button"
          className={view === "modules" ? "is-active" : ""}
          aria-pressed={view === "modules"}
          onClick={() => setView("modules")}
        >
          Modules
        </button>
      </div>

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

      {view === "modules" ? (
        <ModuleView
          repoId={repoId}
          data={modules}
          loading={modulesLoading}
          focus={moduleFocus}
          onFocus={setModuleFocus}
        />
      ) : null}

      {loading && view !== "modules" && (
        <p className="muted">Updating dependency map…</p>
      )}
      {err && view !== "modules" && (
        <p className="muted">Couldn&apos;t load the dependency map.</p>
      )}

      {view !== "modules" && data && data.available && (
        <>
          <div className="codemap-meta mono">
            {data.root_label} · {data.nodes.length} symbols · {data.edges.length} edges
            {/* The call-site total is the differentiator: these edges are exact
                SCIP/LSP references, not inferred, and every one opens its line. */}
            {callSites(data.edges) > 0 &&
              ` · ${callSites(data.edges)} exact call sites`}
            {data.truncated ? " · truncated" : ""}
          </div>
          {data.fell_back && (
            <p className="muted small" role="status">
              The selected snapshot could not be loaded. Showing the default graph
              {data.commit ? ` at ${data.commit.slice(0, 8)}` : ""}.
            </p>
          )}
          {data.note && <p className="muted small">{data.note}</p>}
          {view === "structure" && (data.hierarchy?.nodes?.length ?? 0) > 0 ? (
            <HierarchyMap data={data} onNodeClick={(node) => focus(node.label)} />
          ) : (
            <GraphView repoId={repoId} data={data} variant="explore" onFocus={focus} />
          )}
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
