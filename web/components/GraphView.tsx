"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import type { EdgeClickInfo, GraphNodeInfo } from "@/components/CodeGraph";
import HighlightedCode from "@/components/HighlightedCode";
import SystemMap from "@/components/SystemMap";
import {
  fetchEdgeLabel,
  fetchSource,
  repoRelative,
  type CallSite,
  type CodemapResponse,
} from "@/lib/api";

// Cytoscape loads only when a graph is shown, so the wiki
// narrative paints first and the graph fills in a beat later.
const CodeGraph = dynamic(() => import("@/components/CodeGraph"), {
  ssr: false,
  loading: () => (
    <div className="codegraph">
      <div className="codegraph-canvas codegraph-loading">Loading graph…</div>
    </div>
  ),
});

// What a source peek is showing: an edge's exact call site(s), or a node's
// definition. Both resolve to a (file, line) the /source endpoint can open.
type PeekSource = ({ kind: "edge" } & EdgeClickInfo) | { kind: "node"; node: GraphNodeInfo };

function githubFileUrl(repoFullName: string | undefined, commit: string | undefined, file: string): string | null {
  if (!repoFullName || !file) return null;
  return `https://github.com/${repoFullName}/blob/${commit || "HEAD"}/${file}`;
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
  commit,
}: {
  repoId: string;
  source: PeekSource;
  onClose: () => void;
  onFocus?: (label: string) => void;
  repoFullName?: string;
  commit?: string;
}) {
  const peekRef = useRef<HTMLDivElement>(null);
  const isNode = source.kind === "node";
  const nodeEnd = source.kind === "node" ? source.node.endLine : null;
  const sites: CallSite[] = isNode
    ? [{ file: source.node.file || "", line: source.node.line }]
    : source.anchors;

  const [idx, setIdx] = useState(0);
  const [code, setCode] = useState("");
  const [start, setStart] = useState(1);
  const [state, setState] = useState<"loading" | "ok" | "err">("loading");
  // Short LLM phrase describing how the source uses the target (edge peeks only).
  // Fetched on-demand; the server returns "" when the feature is disabled.
  const [edgeLabel, setEdgeLabel] = useState<"idle" | "loading" | string>("idle");

  const site = sites[Math.min(idx, sites.length - 1)] || sites[0];
  const rel = repoRelative(site.file);
  const line = site.line ?? 1;
  const isExternal = source.kind === "node" && !!source.node.external;
  const fileName = rel.split("/").pop() || rel;
  const sourceTitle =
    source.kind === "edge"
      ? `${compactSymbol(source.srcLabel)} -> ${compactSymbol(source.tgtLabel)}`
      : compactSymbol(source.node.short);
  const sourceMeta = source.kind === "edge" ? "Exact reference" : source.node.kind;
  const fileUrl = isExternal ? null : githubFileUrl(repoFullName, commit, rel);

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
        anchors: source.anchors,
      },
      { signal: ctrl.signal }
    )
      .then((r) => !cancelled && setEdgeLabel(r.label || ""))
      .catch(() => !cancelled && setEdgeLabel(""));
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [source, repoId]);

  useEffect(() => {
    if (isExternal) return; // external dep — no in-repo source to fetch
    let cancelled = false;
    setState("loading");
    // Node peeks use the indexed symbol span, plus a little context around it.
    // Edge peeks remain point locations with context above and below.
    const PAD = 3;
    const symEnd = nodeEnd && nodeEnd >= line ? nodeEnd : line;
    const before = isNode ? PAD : 6;
    const end = isNode ? symEnd + PAD : line + 6;
    fetchSource(repoId, rel, Math.max(1, line - before), end)
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
  }, [repoId, rel, line, isNode, nodeEnd, isExternal]);

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
            {isExternal ? "external dependency" : `${rel}:${line}`}
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
              {sites.length} call sites:
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
      {isExternal ? (
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
  commit,
}: {
  repoId: string;
  data: CodemapResponse;
  // "wiki" = focused top-down dependency map; "explore" = standalone Graph view.
  variant?: "wiki" | "explore";
  onFocus?: (label: string) => void;
  repoFullName?: string;
  commit?: string;
}) {
  const [peek, setPeek] = useState<PeekSource | null>(null);
  useEffect(() => setPeek(null), [data]); // a fresh graph invalidates the open peek

  return (
    <>
      {variant === "wiki" ? (
        <SystemMap
          data={data}
          onNodeClick={(node) => setPeek({ kind: "node", node })}
          onEdgeClick={(info) => setPeek({ kind: "edge", ...info })}
        />
      ) : (
        <CodeGraph
          data={data}
          variant={variant}
          onNodeClick={(node) => setPeek({ kind: "node", node })}
          onEdgeClick={(info) => setPeek({ kind: "edge", ...info })}
        />
      )}
      {peek && (
        <SourcePeek
          key={peek.kind === "node" ? `node:${peek.node.label}` : `${peek.srcLabel}->${peek.tgtLabel}`}
          repoId={repoId}
          source={peek}
          onClose={() => setPeek(null)}
          onFocus={onFocus}
          repoFullName={repoFullName}
          commit={commit}
        />
      )}
    </>
  );
}
