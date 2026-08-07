"use client";

import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { EdgeClickInfo, GraphNodeInfo } from "@/components/CodeGraph";
import HighlightedCode from "@/components/HighlightedCode";
import HierarchyMap from "@/components/HierarchyMap";
import SystemMap from "@/components/SystemMap";
import {
  fetchEdgeLabel,
  fetchSource,
  repoRelative,
  type CallSite,
  type CodemapResponse,
} from "@/lib/api";
import { isStaticRuntime } from "@/lib/runtime";

// Cytoscape loads only when a graph is shown, so the wiki
// narrative paints first and the graph fills in a beat later.
const CodeGraph = lazy(() => import("@/components/CodeGraph"));

function GraphLoading() {
  return (
    <div className="codegraph">
      <div className="codegraph-canvas codegraph-loading">Loading graph…</div>
    </div>
  );
}

// What a source peek is showing: an edge's exact call site(s), or a node's
// definition. Both resolve to a (file, line) the /source endpoint can open.
type PeekSource = ({ kind: "edge" } & EdgeClickInfo) | { kind: "node"; node: GraphNodeInfo };

function githubFileUrl(
  repoFullName: string | undefined,
  sourceUrl: string | null | undefined,
  commit: string | undefined,
  file: string,
  line?: number | null,
): string | null {
  if ((!repoFullName && !sourceUrl) || !file) return null;
  const root = (sourceUrl || `https://github.com/${repoFullName}`).replace(/\/+$/, "");
  const anchor = line ? `#L${line}` : "";
  return `${root}/blob/${commit || "HEAD"}/${file}${anchor}`;
}

function compactSymbol(label: string): string {
  return label.split(":").pop() || label;
}

/**
 * Source peek grounded in the LSP/SCIP index — the payoff of the graph:
 *  - edge → the exact line(s) where the call happens (pager for multiple sites);
 *  - node → the symbol's definition, with an optional "Focus here" shortcut.
 * Either way the precise line is spotlighted; nothing here is a guess.
 */
function SourcePeek({
  repoId,
  source,
  onClose,
  onFocus,
  repoFullName,
  sourceUrl,
  commit,
}: {
  repoId: string;
  source: PeekSource;
  onClose: () => void;
  onFocus?: (label: string) => void;
  repoFullName?: string;
  sourceUrl?: string | null;
  commit?: string;
}) {
  const peekRef = useRef<HTMLDivElement>(null);
  const isNode = source.kind === "node";
  const nodeEnd = source.kind === "node" ? source.node.endLine : null;
  const sites: CallSite[] = isNode
    ? [{ file: source.node.file || "", line: source.node.line }]
    : source.anchors;
  const totalSites =
    source.kind === "edge" ? Math.max(source.anchorCount, sites.length) : sites.length;

  const [idx, setIdx] = useState(0);
  const [code, setCode] = useState("");
  const [start, setStart] = useState(1);
  const [state, setState] = useState<"loading" | "ok" | "err">("loading");
  // Short LLM phrase describing how the source uses the target (edge peeks only).
  // Fetched on-demand; the server returns "" when the feature is disabled.
  const [edgeLabel, setEdgeLabel] = useState<"idle" | "loading" | string>("idle");

  const site = sites[Math.min(idx, sites.length - 1)] || sites[0];
  const embedded = isNode ? source.node.source : site?.source;
  const rel = repoRelative(site.file);
  const line = site.line ?? 1;
  const isExternal = source.kind === "node" && !!source.node.external;
  // A directory node in the module map aggregates many files — there is no one
  // definition to peek at.
  const hasNoSingleFile = source.kind === "node" && !source.node.file;
  const fileName = rel.split("/").pop() || rel;
  const sourceTitle =
    source.kind === "edge"
      ? `${compactSymbol(source.srcLabel)} -> ${compactSymbol(source.tgtLabel)}`
      : compactSymbol(source.node.short);
  const sourceMeta = source.kind === "edge" ? "Exact reference" : source.node.kind;
  const fileUrl = isExternal
    ? null
    : githubFileUrl(repoFullName, sourceUrl, commit, rel, line);

  useEffect(() => {
    const id = requestAnimationFrame(() => {
      peekRef.current?.scrollIntoView({ block: "center" });
    });
    return () => cancelAnimationFrame(id);
  }, [source]);

  useEffect(() => {
    if (source.kind !== "edge") {
      setEdgeLabel("idle");
      return;
    }
    if (isStaticRuntime()) {
      setEdgeLabel("");
      return;
    }
    let cancelled = false;
    const ctrl = new AbortController();
    setEdgeLabel("loading");
    fetchEdgeLabel(
      repoId,
      {
        source: {
          file: source.srcFile || "",
          line: source.srcLine ?? null,
          end_line: source.srcEnd ?? null,
          label: source.srcLabel,
        },
        target: {
          file: source.tgtFile || "",
          line: source.tgtLine ?? null,
          end_line: source.tgtEnd ?? null,
          label: source.tgtLabel,
        },
        // Static page graphs may carry bounded source previews on each anchor.
        // The edge-label endpoint only needs provenance, not the embedded code.
        anchors: source.anchors.map(({ file, line }) => ({ file, line })),
        commit,
      },
      { signal: ctrl.signal }
    )
      .then((r) => !cancelled && setEdgeLabel(r.label || ""))
      .catch(() => !cancelled && setEdgeLabel(""));
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [source, repoId, commit]);

  useEffect(() => {
    if (isExternal || hasNoSingleFile) return; // nothing single to fetch
    let cancelled = false;
    setState("loading");
    if (embedded === null) {
      setCode("");
      setState("err");
      return;
    }
    if (embedded?.content && repoRelative(embedded.file) === rel) {
      setCode(embedded.content);
      setStart(embedded.start_line || 1);
      setState("ok");
      return;
    }
    // Node peeks use the indexed symbol span, plus a little context around it.
    // Edge peeks remain point locations with context above and below.
    const PAD = 3;
    const symEnd = nodeEnd && nodeEnd >= line ? nodeEnd : line;
    const before = isNode ? PAD : 6;
    const end = isNode ? symEnd + PAD : line + 6;
    fetchSource(repoId, rel, Math.max(1, line - before), end, commit)
      .then((s) => {
        if (cancelled) return;
        setCode(s.content || "");
        setStart(s.start_line || Math.max(1, line - before));
        setState("ok");
      })
      .catch(() => !cancelled && setState("err"));
    return () => {
      cancelled = true;
    };
  }, [
    repoId,
    rel,
    line,
    isNode,
    nodeEnd,
    isExternal,
    hasNoSingleFile,
    commit,
    embedded,
  ]);

  return (
    <div className="callsite-peek" ref={peekRef}>
      <div className="callsite-head">
        <div className="callsite-heading">
          <span className="callsite-eyebrow">Code Preview</span>
          <span className="callsite-title mono">
            <b>{fileName || "external"}</b>
            <span className="callsite-dot"> · </span>
            <span>{sourceTitle}</span>
          </span>
          <span className="callsite-loc mono">
            {isExternal
              ? "external dependency"
              : hasNoSingleFile
                ? "directory"
                : `${rel}:${line}`}
          </span>
          {source.kind === "edge" &&
            typeof edgeLabel === "string" &&
            edgeLabel !== "idle" &&
            (edgeLabel === "loading" ? (
              <span className="callsite-edgelabel muted">describing dependency…</span>
            ) : edgeLabel ? (
              <span className="callsite-edgelabel">“{edgeLabel}”</span>
            ) : null)}
        </div>
        <div className="callsite-actions">
          <span className="callsite-kindtag">{sourceMeta}</span>
          {source.kind === "edge" && sites.length > 1 && (
            <span className="callsite-pager">
              {totalSites} call sites{totalSites > sites.length ? ` (${sites.length} shown)` : ""}:
              {sites.map((a, i) => (
                <button
                  key={i}
                  type="button"
                  className={i === idx ? "on" : ""}
                  onClick={() => setIdx(i)}
                  title={`${repoRelative(a.file)}:${a.line}`}
                >
                  {a.line}
                </button>
              ))}
            </span>
          )}
          {source.kind === "node" && onFocus && (
            <button
              type="button"
              className="callsite-focus"
              onClick={() => onFocus(source.node.label)}
              title="Re-root the graph on this symbol"
            >
              Focus in graph
            </button>
          )}
          {fileUrl && (
            <a
              className="callsite-open"
              href={fileUrl}
              target="_blank"
              rel="noreferrer"
              title={`Open ${rel} on GitHub`}
            >
              Open file
            </a>
          )}
          <button type="button" className="callsite-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
      </div>
      {hasNoSingleFile && !isExternal ? (
        <p className="muted callsite-msg">
          A directory aggregates many files, so there is no single definition to
          show. Use “Focus in graph” to open its own dependency map.
        </p>
      ) : isExternal ? (
        <p className="muted callsite-msg">
          External symbol — its definition lives outside this repository, so there&apos;s no source to show.
          Use “Focus here” to see where this repo references it.
        </p>
      ) : state === "loading" ? (
        <p className="muted callsite-msg">Loading source…</p>
      ) : state === "err" ? (
        <p className="muted callsite-msg">Source not available.</p>
      ) : (
        <HighlightedCode
          code={code}
          file={rel}
          startLine={start}
          highlightLine={line}
          highlightEnd={isNode ? nodeEnd ?? line : undefined}
        />
      )}
    </div>
  );
}

/**
 * Reusable interactive graph + source peek. Used both by the codemap mode and
 * by wiki pages (rendered as a view over the graph). `onFocus` (optional) wires
 * the node peek's "Focus here" — omit it on surfaces that can't re-root.
 */
export default function GraphView({
  repoId,
  data,
  variant = "explore",
  onFocus,
  repoFullName,
  sourceUrl,
  commit,
}: {
  repoId: string;
  data: CodemapResponse;
  // "wiki" = focused top-down dependency map; "explore" = standalone Graph
  // view; "modules" = the file/directory dependency map.
  variant?: "wiki" | "explore" | "modules";
  onFocus?: (label: string) => void;
  repoFullName?: string;
  sourceUrl?: string | null;
  commit?: string;
}) {
  const [peek, setPeek] = useState<PeekSource | null>(null);
  // In the explore graph, "Focus in graph" centers + expands the node in place
  // (no re-root). The nonce makes repeat clicks on the same node re-fire.
  const [focusReq, setFocusReq] = useState<{ label: string; nonce: number } | null>(null);
  useEffect(() => {
    setPeek(null); // a fresh graph invalidates the open peek
    setFocusReq(null);
  }, [data]);

  // Wiki: focusing re-roots into the standalone explore graph (how you get in).
  // Explore: focus stays inside the current graph so it isn't thrown away.
  const hasHierarchy = (data.hierarchy?.nodes?.length ?? 0) > 0;

  const peekFocus =
    variant === "wiki" || variant === "modules"
      ? onFocus
      : (label: string) => setFocusReq((p) => ({ label, nonce: (p?.nonce ?? 0) + 1 }));

  const openNodeSource = (node: GraphNodeInfo) => {
    if (!node.external && node.source === null) return;
    setPeek({ kind: "node", node });
  };

  const openEdgeSource = (info: EdgeClickInfo) => {
    const anchors = info.anchors.filter((anchor) => anchor.source !== null);
    if (!anchors.length) return;
    setPeek({ kind: "edge", ...info, anchors });
  };

  return (
    <>
      {variant === "wiki" ? (
        // Drill down the containment tree when the payload carries one; the flat
        // component view is the fallback for payloads that do not.
        hasHierarchy ? (
          <HierarchyMap
            data={data}
            onNodeClick={openNodeSource}
            onSourceClick={openNodeSource}
            onEdgeClick={openEdgeSource}
          />
        ) : (
          <SystemMap
            data={data}
            onNodeClick={openNodeSource}
            onEdgeClick={openEdgeSource}
          />
        )
      ) : (
        <Suspense fallback={<GraphLoading />}>
          <CodeGraph
            data={data}
            variant={variant}
            focusRequest={focusReq}
            repoId={repoId}
            onNodeClick={openNodeSource}
            onEdgeClick={openEdgeSource}
          />
        </Suspense>
      )}
      {peek && (
        <SourcePeek
          key={peek.kind === "node" ? `node:${peek.node.label}` : `${peek.srcLabel}->${peek.tgtLabel}`}
          repoId={repoId}
          source={peek}
          onClose={() => setPeek(null)}
          onFocus={peekFocus}
          repoFullName={repoFullName}
          sourceUrl={sourceUrl}
          commit={commit}
        />
      )}
    </>
  );
}
