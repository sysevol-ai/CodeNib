// Map, Impact and Walkthrough on the repository's own index. Every number,
// name and line comes from the local service; where it has nothing to say,
// the view says so instead of filling in.

import { ArrowLeft, ArrowRight, ChevronDown } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Kbd } from "@/components/ui/kbd";
import { message } from "@/lib/backend";
import { areaPages, hueFor } from "@/lib/areas";
import { flattenPages, type Source } from "@/lib/model";
import { repoPath, useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { CodeLines } from "./CodeLines";
import { SourceMark } from "./SourceMark";

interface Anchor {
  file: string;
  line: number;
}

interface GraphNode {
  id: string;
  name: string;
  short: string;
  label?: string;
  file: string;
  line: number;
  end_line?: number | null;
  kind?: string;
  depth: number;
  is_root?: boolean;
  external?: boolean;
  symbol_count?: number;
  importance?: number;
}

interface GraphEdge {
  source: string;
  target: string;
  anchors?: Anchor[];
  weight?: number;
  call_sites?: number;
}

interface Graph {
  available: boolean;
  root?: string;
  truncated?: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
  note?: string;
}

/** The thing a view fetches: loading, its value, or why it has none. */
function useFetch<T>(repo: string, path: string | null) {
  const { call } = useStore();
  const [state, setState] = useState<{ value?: T; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    if (!path) return;
    let live = true;
    setState({ loading: true });
    call<T>(repo, "GET", path)
      .then((value) => live && setState({ value, loading: false }))
      .catch((error) => live && setState({ error: message(error), loading: false }));
    return () => {
      live = false;
    };
  }, [repo, path, call]);
  return state;
}

/** Which area a file belongs to: the module page whose folder holds it. */
function useAreaOf(repo: string) {
  const { data } = useStore();
  const d = data[repo];
  return useMemo(() => {
    const pages = flattenPages(d?.tree?.pages ?? []);
    const areas = areaPages(d?.tree?.pages ?? [], d?.mapAreas ?? null);
    const modules = pages
      .filter((p) => p.id.startsWith("mod__"))
      .map((p) => ({ id: p.id, folder: p.title.replace(/\/$/, "") }))
      .sort((a, b) => b.folder.length - a.folder.length);
    return (file: string) => {
      const found = modules.find((m) => file === m.folder || file.startsWith(`${m.folder}/`));
      return found
        ? { id: found.id, title: found.folder, hue: hueFor(found.id, areas) }
        : { id: "", title: "No area", hue: "var(--area-none)" };
    };
  }, [d?.tree, d?.mapAreas]);
}

/** A long code name may break after a dot or a slash, never through a word. */
function Name({ text, className }: { text: string; className?: string }) {
  // Split after each dot or slash without lookbehind, which Safari 15 lacks.
  const parts = text.replace(/([./])/g, "$1\u0000").split("\u0000").filter(Boolean);
  return (
    <code className={className}>
      {parts.map((p, i) => (
        <span key={i}>
          {p}
          {i < parts.length - 1 && <wbr />}
        </span>
      ))}
    </code>
  );
}

const shortName = (name: string) => name.split(":").pop() ?? name;

function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="view">
      <h1 className="view-title">
        {title} <span className="two-tone">{children}</span>
      </h1>
    </div>
  );
}

// ---------------------------------------------------------------- Map

interface MapNode {
  id: string;
  title: string;
  meta: string;
  hue: string;
  page: string;
  here: boolean;
}

interface MapLink {
  source: string;
  target: string;
  calls: number;
  example?: Anchor;
  label?: string;
}

function MapCanvas({ nodes, links, onOpen }: { nodes: MapNode[]; links: MapLink[]; onOpen: (n: MapNode) => void }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<{ key: string; d: string; link: MapLink; hue: string }[]>([]);
  const [hover, setHover] = useState<MapLink | null>(null);
  const columns = nodes.length <= 4 ? nodes.length : nodes.length <= 9 ? 3 : 4;
  const max = Math.max(1, ...links.map((l) => l.calls));

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => {
      const box = el.getBoundingClientRect();
      const rect = (id: string) => el.querySelector<HTMLElement>(`[data-map="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      const next: typeof paths = [];
      for (const link of links) {
        const a = rect(link.source);
        const b = rect(link.target);
        if (!a || !b) continue;
        const ax = a.left + a.width / 2 - box.left;
        const ay = a.top + a.height / 2 - box.top;
        const bx = b.left + b.width / 2 - box.left;
        const by = b.top + b.height / 2 - box.top;
        // Leave each box from its edge facing the other, and bow the line so
        // a pair's two directions stay apart.
        const dx = bx - ax;
        const dy = by - ay;
        const horizontal = Math.abs(dx) * a.height > Math.abs(dy) * a.width;
        const x1 = horizontal ? ax + Math.sign(dx) * (a.width / 2) : ax;
        const y1 = horizontal ? ay : ay + Math.sign(dy) * (a.height / 2);
        const x2 = horizontal ? bx - Math.sign(dx) * (b.width / 2 + 6) : bx;
        const y2 = horizontal ? by : by - Math.sign(dy) * (b.height / 2 + 6);
        const mx = (x1 + x2) / 2 - dy * 0.06;
        const my = (y1 + y2) / 2 + dx * 0.06;
        const hue = el.querySelector<HTMLElement>(`[data-map="${CSS.escape(link.source)}"]`)?.style.getPropertyValue("--hue") ?? "";
        next.push({ key: `${link.source}>${link.target}`, d: `M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`, link, hue });
      }
      setPaths(next);
    };
    measure();
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    });
    observer.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [nodes, links]);

  const title = (id: string) => nodes.find((n) => n.id === id)?.title ?? id;
  return (
    <section className="map-section" aria-label="Areas and the calls between them">
      <div className="map-grid" ref={wrap} style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        <svg className="map-links" aria-hidden="true">
          {paths.map((p) => (
            <path
              key={p.key}
              d={p.d}
              className={cn("map-link", hover && hover !== p.link && "is-dim")}
              style={{
                stroke: p.hue,
                strokeWidth: 1 + 4 * Math.sqrt(p.link.calls / max),
                opacity: hover === p.link ? 1 : 0.35 + 0.55 * Math.sqrt(p.link.calls / max),
              }}
            />
          ))}
          {paths.map((p) => (
            <path
              key={`${p.key}-hit`}
              d={p.d}
              className="map-hit"
              onMouseEnter={() => setHover(p.link)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        </svg>
        {nodes.map((n) => (
          <button
            key={n.id}
            type="button"
            data-map={n.id}
            className={cn("map-area", n.here && "is-here")}
            style={{ "--hue": n.hue } as CSSProperties}
            onClick={() => onOpen(n)}
          >
            <span className="map-area-title">
              <span className="area-mark" aria-hidden="true" />
              <Name text={n.title} />
            </span>
            <span className="map-area-meta">{n.meta}</span>
          </button>
        ))}
      </div>
      <p className="map-caption" aria-live="polite">
        {hover ? (
          <>
            <strong>{title(hover.source)}</strong> calls <strong>{title(hover.target)}</strong> {hover.calls}{" "}
            {hover.calls === 1 ? "time" : "times"}.
            {hover.example && (
              <>
                {" "}
                For example at{" "}
                <span className="mono">
                  {hover.example.file.split("/").pop()}:{hover.example.line}
                </span>
                .
              </>
            )}
          </>
        ) : (
          <span className="map-hint">Point at a line to see how many calls it carries; select a box to open its page.</span>
        )}
      </p>
    </section>
  );
}

function PageCalls({ repo, pageId }: { repo: string; pageId: string }) {
  const { data } = useStore();
  const graph = useFetch<Graph>(repo, repoPath(data[repo], `/wiki/${encodeURIComponent(pageId)}/graph`));
  const areaOf = useAreaOf(repo);
  if (graph.loading || !graph.value?.available) return null;
  const byId = new Map(graph.value.nodes.map((n) => [n.id, n]));
  const calls = graph.value.edges.filter((e) => byId.has(e.source) && byId.has(e.target));
  return (
    <section className="pg-section" aria-label="Calls inside this page">
      <h2 className="view-h2">
        Inside this page.{" "}
        <span className="two-tone">
          {graph.value.nodes.length} symbols and {calls.length === 0 ? "no recorded calls" : `the ${calls.length} calls`} between them.
        </span>
      </h2>
      <ul className="impact-rows">
        {calls.map((e, i) => {
          const a = byId.get(e.source)!;
          const b = byId.get(e.target)!;
          const site = e.anchors?.[0];
          return (
            <li key={i} className="impact-row">
              <span className="area-mark" style={{ background: areaOf(a.file).hue }} aria-hidden="true" />
              <span className="impact-row-main">
                <Name text={shortName(a.name)} className="impact-row-name" />
                <span className="impact-row-what">
                  calls <code>{shortName(b.name)}</code>
                  {site && (
                    <>
                      {" "}
                      at <SourceMark repo={repo} file={site.file} start={site.line} />
                    </>
                  )}
                </span>
              </span>
              <span className="impact-row-area">{areaOf(a.file).title}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function MapView({ repo, pageId }: { repo: string; pageId: string }) {
  const { data, go, repos } = useStore();
  const d = data[repo];
  const name = repos.find((r) => r.path === repo)?.name ?? "";
  const outline = useFetch<{ available: boolean; areas: { id: string; title: string; symbols: number; files: number }[]; links: { source: string; target: string; calls: number; example?: { anchor?: Anchor } }[] }>(
    repo,
    repoPath(d, "/wiki-map"),
  );
  const modules = useFetch<Graph & { total_modules?: number }>(
    repo,
    outline.loading || outline.value?.available ? null : repoPath(d, "/modulemap"),
  );
  const areaOf = useAreaOf(repo);
  const areas = areaPages(d?.tree?.pages ?? [], d?.mapAreas ?? null);
  const pageAreaFolder = flattenPages(d?.tree?.pages ?? []).find((p) => p.id === pageId)?.title;

  const view = useMemo(() => {
    if (outline.value?.available) {
      return {
        title: `${outline.value.areas.length} areas of ${name} and the calls between them.`,
        note: "",
        nodes: outline.value.areas.map<MapNode>((a) => ({
          id: a.id,
          title: a.title,
          meta: `${a.symbols} symbols · ${a.files} files`,
          hue: hueFor(a.id, areas),
          page: a.id,
          here: a.id === pageId,
        })),
        links: outline.value.links.map<MapLink>((l) => ({
          source: l.source,
          target: l.target,
          calls: l.calls,
          example: l.example?.anchor,
        })),
      };
    }
    const graph = modules.value;
    if (!graph?.available) return null;
    const files = graph.nodes
      .filter((n) => !n.external && (n.kind ?? "file") === "file")
      .sort((a, b) => (b.symbol_count ?? 0) - (a.symbol_count ?? 0))
      .slice(0, 12);
    const shown = new Set(files.map((f) => f.id));
    const between = graph.edges
      .filter((e) => shown.has(e.source) && shown.has(e.target) && e.source !== e.target)
      .sort((a, b) => (b.call_sites ?? b.weight ?? 1) - (a.call_sites ?? a.weight ?? 1));
    // Every link at once is a tangle; the busiest ones carry the shape.
    const kept = between.slice(0, 16);
    return {
      title: `The ${files.length} largest files of ${name} and the calls between them.`,
      note: between.length > kept.length ? `The ${kept.length} busiest of ${between.length} links are drawn.` : "",
      nodes: files.map<MapNode>((f) => {
        const area = areaOf(f.file);
        return {
          id: f.id,
          title: f.file.split("/").pop() ?? f.file,
          meta: `${f.symbol_count ?? 0} symbols · ${area.id ? area.title : f.file.split("/").slice(0, -1).join("/") || "top level"}`,
          hue: area.hue,
          page: area.id,
          here: !!pageAreaFolder && f.file.startsWith(`${pageAreaFolder}/`),
        };
      }),
      links: kept.map<MapLink>((e) => ({
          source: e.source,
          target: e.target,
          calls: e.call_sites ?? e.weight ?? 1,
          example: e.anchors?.[0],
        })),
    };
  }, [outline.value, modules.value, name, areas, pageId, areaOf, pageAreaFolder]);

  if (outline.loading || (!outline.value?.available && modules.loading)) return <Empty title="Map.">Reading the index…</Empty>;
  if (!view)
    return (
      <Empty title="Map.">
        {modules.error || "This repository has no symbol graph yet. Add it again with Build analysis tools on."}
      </Empty>
    );
  return (
    <div className="view">
      <h1 className="view-title">
        Map. <span className="two-tone">{view.title}</span>
      </h1>
      <p className="view-sub">
        Read from the index{d?.info?.commit_short ? ` at ${d.info.commit_short}` : ""}. A line's width follows how
        many calls it carries; its colour is the calling area.{view.note ? ` ${view.note}` : ""}
      </p>
      <MapCanvas
        nodes={view.nodes}
        links={view.links}
        onOpen={(n) => n.page && go({ kind: "page", repo, page: n.page })}
      />
      <PageCalls repo={repo} pageId={pageId} />
    </div>
  );
}

// ---------------------------------------------------------------- Impact

const DISTANCE = ["", "1 call away", "2 calls away", "3 calls away"];

function words(n: number) {
  return ["no", "one", "two", "three", "four", "five", "six"][n] ?? String(n);
}

export function ImpactView({ repo, pageId }: { repo: string; pageId: string }) {
  const { data } = useStore();
  const d = data[repo];
  const boundary = useFetch<{ available: boolean; focus: { symbol: string; file: string; line: number; degree: number }[] }>(
    repo,
    repoPath(d, `/wiki/${encodeURIComponent(pageId)}/boundary`),
  );
  const [symbol, setSymbol] = useState<string | null>(null);
  const chosen = symbol ?? boundary.value?.focus?.[0]?.symbol ?? null;
  const callers = useFetch<Graph>(
    repo,
    chosen ? repoPath(d, `/codemap?symbol=${encodeURIComponent(chosen)}&direction=callers&depth=3&max_nodes=60`) : null,
  );
  const callees = useFetch<Graph>(
    repo,
    chosen ? repoPath(d, `/codemap?symbol=${encodeURIComponent(chosen)}&direction=callees&depth=1&max_nodes=30`) : null,
  );
  const areaOf = useAreaOf(repo);
  const [focus, setFocus] = useState<string | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const [lines, setLines] = useState<{ key: string; d: string; hue: string; from: string }[]>([]);

  const graph = callers.value;
  const root = graph?.nodes.find((n) => n.depth === 0);
  const rows = useMemo(() => {
    if (!graph) return [];
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    return graph.nodes
      .filter((n) => n.depth > 0 && n.depth <= 3 && (n.kind ?? "") !== "file")
      .map((n) => {
        const edge = graph.edges.find((e) => e.source === n.id && byId.get(e.target)?.depth === n.depth - 1);
        return { node: n, calls: edge ? byId.get(edge.target) : undefined, site: edge?.anchors?.[0] };
      });
  }, [graph]);
  const areaCount = new Set(rows.map((r) => areaOf(r.node.file).id || "none")).size;

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => {
      const box = el.getBoundingClientRect();
      const pos = (id: string) => el.querySelector<HTMLElement>(`[data-node="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      const next: typeof lines = [];
      for (const r of rows) {
        if (!r.calls) continue;
        const a = pos(r.node.id);
        const b = pos(r.calls.id);
        if (!a || !b) continue;
        const x1 = a.right - box.left;
        const y1 = a.top + a.height / 2 - box.top;
        const x2 = b.left - box.left - 6;
        const y2 = b.top + b.height / 2 - box.top;
        const mid = (x1 + x2) / 2;
        next.push({ key: r.node.id, d: `M ${x1} ${y1} C ${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`, hue: areaOf(r.node.file).hue, from: r.node.id });
      }
      setLines(next);
    };
    measure();
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    });
    observer.observe(el);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [rows, areaOf]);

  const lit = useMemo(() => {
    const on = new Set<string>();
    let current = rows.find((r) => r.node.id === focus);
    while (current) {
      on.add(current.node.id);
      if (!current.calls) break;
      on.add(current.calls.id);
      const next = current.calls.id;
      current = rows.find((r) => r.node.id === next);
    }
    return on;
  }, [rows, focus]);

  if (boundary.loading) return <Empty title="Impact.">Reading the index…</Empty>;
  if (!boundary.value?.available || !boundary.value.focus?.length)
    return <Empty title="Impact.">This page names no symbols the symbol graph knows, so there is nothing to trace yet.</Empty>;

  const least = graph?.truncated ? "at least " : "";
  const n = rows.length;
  const columns = [3, 2, 1].map((depth) => ({ depth, rows: rows.filter((r) => r.node.depth === depth) })).filter((c) => c.rows.length);
  const target = root ? shortName(root.name) : chosen ? shortName(chosen) : "";
  const calledBy = (callees.value?.nodes ?? []).filter((x) => x.depth === 1);

  return (
    <div className="view">
      <h1 className="view-title">
        Impact.{" "}
        <span className="two-tone">
          {callers.loading
            ? "Tracing the callers…"
            : n === 0
              ? `Nothing in the index calls ${target}.`
              : `A change to ${target} reaches ${least}${n} caller${n === 1 ? "" : "s"} in ${words(areaCount)} area${areaCount === 1 ? "" : "s"}.`}
        </span>
      </h1>
      <div className="impact-pick">
        <span className="view-sub-inline">If you change</span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" className="pick" aria-label="Choose the symbol you would change">
              <span className="area-mark" style={{ background: areaOf(root?.file ?? "").hue }} aria-hidden="true" />
              <code>{target}</code>
              <ChevronDown />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="pick-menu">
            <DropdownMenuLabel>Symbols this page names</DropdownMenuLabel>
            {boundary.value.focus.map((f) => (
              <DropdownMenuItem key={f.symbol} onSelect={() => setSymbol(f.symbol)}>
                <span className="area-mark" style={{ background: areaOf(f.file).hue }} aria-hidden="true" />
                <code>{shortName(f.symbol)}</code>
                <span className="pick-n">{f.degree} links</span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        <span className="view-sub-inline">
          these callers run it, up to three calls away. They are the calls the index records
          {d?.info?.commit_short ? (
            <>
              {" "}
              at <span className="mono">{d.info.commit_short}</span>
            </>
          ) : null}
          .
        </span>
      </div>

      {callers.error && <p className="view-sub">{callers.error}</p>}
      {n > 0 && root && (
        <div className="impact-diagram" ref={wrap}>
          <svg className="impact-lines" aria-hidden="true">
            {lines.map((l) => (
              <path key={l.key} d={l.d} className={cn("impact-line", focus && !lit.has(l.from) && "is-dim")} style={{ stroke: l.hue }} />
            ))}
          </svg>
          {columns.map((col) => (
            <div key={col.depth} className="impact-col">
              <div className="impact-col-head">{DISTANCE[col.depth]}</div>
              {col.rows.map((r) => (
                <button
                  key={r.node.id}
                  type="button"
                  data-node={r.node.id}
                  className={cn("impact-node", focus && !lit.has(r.node.id) && "is-dim", focus === r.node.id && "is-focus")}
                  style={{ "--hue": areaOf(r.node.file).hue } as CSSProperties}
                  onMouseEnter={() => setFocus(r.node.id)}
                  onMouseLeave={() => setFocus(null)}
                  onFocus={() => setFocus(r.node.id)}
                  onBlur={() => setFocus(null)}
                >
                  <span className="area-mark" aria-hidden="true" />
                  {shortName(r.node.name)}
                </button>
              ))}
            </div>
          ))}
          <div className="impact-col is-target">
            <div className="impact-col-head">The change</div>
            <div data-node={root.id} className="impact-node is-target" style={{ "--hue": areaOf(root.file).hue } as CSSProperties}>
              <span className="area-mark" aria-hidden="true" />
              {shortName(root.name)}
            </div>
            <SourceMark repo={repo} file={root.file} start={root.line} end={root.end_line ?? undefined} className="impact-target-mark" />
          </div>
        </div>
      )}

      <div className="impact-lists">
        <section aria-label="Callers">
          <h2 className="view-h2">
            Callers. <span className="two-tone">Where each one calls in.</span>
          </h2>
          {[1, 2, 3].map((depth) => {
            const group = rows.filter((r) => r.node.depth === depth);
            if (!group.length) return null;
            return (
              <div key={depth} className="impact-group">
                <div className="impact-group-head">
                  {DISTANCE[depth]} <span className="inbox-n">{group.length}</span>
                </div>
                <ul className="impact-rows">
                  {group.map((r) => (
                    <li
                      key={r.node.id}
                      className={cn("impact-row", focus === r.node.id && "is-focus")}
                      onMouseEnter={() => setFocus(r.node.id)}
                      onMouseLeave={() => setFocus(null)}
                    >
                      <span className="area-mark" style={{ background: areaOf(r.node.file).hue }} aria-hidden="true" />
                      <span className="impact-row-main">
                        <Name text={shortName(r.node.name)} className="impact-row-name" />
                        {r.calls && (
                          <span className="impact-row-what">
                            calls <code>{shortName(r.calls.name)}</code>
                            {r.site && (
                              <>
                                {" "}
                                at <SourceMark repo={repo} file={r.site.file} start={r.site.line} />
                              </>
                            )}
                          </span>
                        )}
                      </span>
                      <span className="impact-row-area">{areaOf(r.node.file).title}</span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </section>
        <section aria-label="What it calls">
          <h2 className="view-h2">
            It uses. <span className="two-tone">What {target} calls or names.</span>
          </h2>
          {calledBy.length === 0 && <p className="view-sub">The index records nothing it calls or names.</p>}
          <ul className="impact-rows">
            {calledBy.map((c) => {
              const edge = callees.value?.edges.find((e) => e.target === c.id);
              const site = edge?.anchors?.[0];
              return (
                <li key={c.id} className="impact-row is-callee">
                  <span className="area-mark" style={{ background: areaOf(c.file).hue }} aria-hidden="true" />
                  <span className="impact-row-main">
                    <Name text={shortName(c.name)} className="impact-row-name" />
                    {site && (
                      <span className="impact-row-what">
                        at <SourceMark repo={repo} file={site.file} start={site.line} />
                      </span>
                    )}
                  </span>
                  <span className="impact-row-area">{areaOf(c.file).title}</span>
                </li>
              );
            })}
          </ul>
        </section>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Walkthrough

interface Step {
  node: GraphNode;
  next?: { node: GraphNode; site?: Anchor };
}

/** The longest chain of calls through the page's own graph, at most six. */
function chainOf(graph: Graph): Step[] {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const out = new Map<string, GraphEdge[]>();
  const into = new Set<string>();
  for (const e of graph.edges) {
    if (!byId.has(e.source) || !byId.has(e.target) || e.source === e.target) continue;
    out.set(e.source, [...(out.get(e.source) ?? []), e]);
    into.add(e.target);
  }
  let best: string[] = [];
  const walk = (id: string, path: string[]) => {
    if (path.length > best.length) best = path;
    if (path.length >= 6) return;
    for (const e of out.get(id) ?? []) if (!path.includes(e.target)) walk(e.target, [...path, e.target]);
  };
  const starts = graph.nodes.filter((n) => out.has(n.id) && !into.has(n.id));
  for (const s of starts.length ? starts : graph.nodes.filter((n) => out.has(n.id))) walk(s.id, [s.id]);
  return best.map((id, i) => {
    const nextId = best[i + 1];
    const edge = nextId ? (out.get(id) ?? []).find((e) => e.target === nextId) : undefined;
    return {
      node: byId.get(id)!,
      next: nextId ? { node: byId.get(nextId)!, site: edge?.anchors?.[0] } : undefined,
    };
  });
}

function StepCode({ repo, node }: { repo: string; node: GraphNode }) {
  const { data } = useStore();
  const end = Math.min(node.end_line && node.end_line >= node.line ? node.end_line : node.line + 20, node.line + 30);
  const source = useFetch<Source>(
    repo,
    repoPath(data[repo], `/source?file=${encodeURIComponent(node.file)}&start=${node.line}&end=${end}`),
  );
  const areaOf = useAreaOf(repo);
  return (
    <div className="code-card walk-code" style={{ "--hue": areaOf(node.file).hue } as CSSProperties}>
      <div className="code-head">
        <span className="mono">{node.file}</span>
        <span>
          lines {node.line}–{end}
        </span>
      </div>
      {source.value ? (
        <CodeLines
          text={source.value.content}
          start={source.value.start_line}
          from={Math.max(node.line, source.value.start_line)}
          to={Math.min(end, source.value.end_line)}
        />
      ) : (
        <p className="src-pop-wait">{source.error || "Reading the code…"}</p>
      )}
    </div>
  );
}

export function WalkthroughView({ repo, pageId }: { repo: string; pageId: string }) {
  const { data } = useStore();
  const graph = useFetch<Graph>(repo, repoPath(data[repo], `/wiki/${encodeURIComponent(pageId)}/graph`));
  const steps = useMemo(() => (graph.value?.available ? chainOf(graph.value) : []), [graph.value]);
  const [at, setAt] = useState(0);
  const areaOf = useAreaOf(repo);
  const move = useCallback((delta: number) => setAt((i) => Math.max(0, Math.min(steps.length - 1, i + delta))), [steps.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "ArrowRight") move(1);
      if (e.key === "ArrowLeft") move(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [move]);

  if (graph.loading) return <Empty title="Walkthrough.">Reading the page's calls…</Empty>;
  if (steps.length < 2)
    return (
      <Empty title="Walkthrough.">
        This page's functions record no chain of calls to follow. Try a page about one part of the code.
      </Empty>
    );
  const step = steps[Math.min(at, steps.length - 1)];
  const first = shortName(steps[0].node.name);
  const last = shortName(steps[steps.length - 1].node.name);

  return (
    <div className="view walk">
      <h1 className="view-title">
        Walkthrough.{" "}
        <span className="two-tone">
          From {first} to {last}.
        </span>
      </h1>
      <p className="view-sub">
        Built from this page's call graph: {steps.length} functions, one step each, each with the code it runs and
        the call that leads on. The code is read from the repository on this Mac.
      </p>
      <div className="walk-body">
        <ol className="walk-rail" aria-label="Steps">
          {steps.map((s, i) => (
            <li key={s.node.id} className={cn("walk-stop", i === at && "is-on", i < at && "is-past")}>
              <span
                className="walk-seg"
                style={{ background: i < steps.length - 1 ? areaOf(steps[i + 1].node.file).hue : "transparent" }}
                aria-hidden="true"
              />
              <button type="button" onClick={() => setAt(i)} aria-current={i === at ? "step" : undefined}>
                <span className="walk-dot" style={{ "--hue": areaOf(s.node.file).hue } as CSSProperties} aria-hidden="true" />
                <span className="walk-stop-text">
                  <Name text={shortName(s.node.name)} className="walk-stop-name" />
                  <span className="walk-stop-area">{areaOf(s.node.file).title}</span>
                </span>
              </button>
            </li>
          ))}
        </ol>
        <article className="walk-step" aria-live="polite">
          <div className="walk-meta">
            <span>
              Step {at + 1} of {steps.length}
            </span>
            <span className="walk-area">
              <span className="area-mark" style={{ background: areaOf(step.node.file).hue }} aria-hidden="true" />
              {areaOf(step.node.file).title}
            </span>
            <SourceMark repo={repo} file={step.node.file} start={step.node.line} end={step.node.end_line ?? undefined} />
          </div>
          <h2 className="walk-name">
            <code>{shortName(step.node.name)}</code>
          </h2>
          <StepCode key={step.node.id} repo={repo} node={step.node} />
          <div className="walk-next">
            {step.next ? (
              <span>
                It calls <code>{shortName(step.next.node.name)}</code>
                {step.next.site && (
                  <>
                    {" "}
                    at <SourceMark repo={repo} file={step.next.site.file} start={step.next.site.line} />
                  </>
                )}
                .
              </span>
            ) : (
              <span>This is where the page's chain of calls ends.</span>
            )}
          </div>
          <div className="walk-nav">
            <Button variant="outline" size="sm" disabled={at === 0} onClick={() => move(-1)}>
              <ArrowLeft />
              Previous
            </Button>
            <span className="walk-keys">
              <Kbd>
                <ArrowLeft />
              </Kbd>
              <Kbd>
                <ArrowRight />
              </Kbd>
              to move
            </span>
            <Button size="sm" disabled={at === steps.length - 1} onClick={() => move(1)}>
              Next
              <ArrowRight />
            </Button>
          </div>
        </article>
      </div>
    </div>
  );
}
