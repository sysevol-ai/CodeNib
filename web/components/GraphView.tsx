"use client";

import { useEffect, useState } from "react";
import CodeGraph, { type EdgeClickInfo, type GraphNodeInfo } from "@/components/CodeGraph";
import HighlightedCode from "@/components/HighlightedCode";
import { fetchSource, repoRelative, type CallSite, type CodemapResponse } from "@/lib/api";

// What a source peek is showing: an edge's exact call site(s), or a node's
// definition. Both resolve to a (file, line) the /source endpoint can open.
type PeekSource = ({ kind: "edge" } & EdgeClickInfo) | { kind: "node"; node: GraphNodeInfo };

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
}: {
  repoId: string;
  source: PeekSource;
  onClose: () => void;
  onFocus?: (label: string) => void;
}) {
  const isNode = source.kind === "node";
  const sites: CallSite[] = isNode
    ? [{ file: source.node.file || "", line: source.node.line }]
    : source.anchors;

  const [idx, setIdx] = useState(0);
  const [code, setCode] = useState("");
  const [start, setStart] = useState(1);
  const [state, setState] = useState<"loading" | "ok" | "err">("loading");

  const site = sites[Math.min(idx, sites.length - 1)] || sites[0];
  const rel = repoRelative(site.file);
  const line = site.line ?? 1;
  const isExternal = source.kind === "node" && !!source.node.external;

  useEffect(() => {
    if (isExternal) return; // external dep — no in-repo source to fetch
    let cancelled = false;
    setState("loading");
    // Nodes show their definition from its first line; call sites get context above.
    const before = isNode ? 0 : 6;
    const after = isNode ? 16 : 6;
    fetchSource(repoId, rel, Math.max(1, line - before), line + after)
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
  }, [repoId, rel, line, isNode, isExternal]);

  return (
    <div className="callsite-peek">
      <div className="callsite-head mono">
        {source.kind === "edge" ? (
          <span className="callsite-title">
            <b>{source.srcLabel}</b> <span className="callsite-arrow">→</span> <b>{source.tgtLabel}</b>
          </span>
        ) : (
          <span className="callsite-title">
            <span className="callsite-kindtag">{source.node.kind}</span> <b>{source.node.short}</b>
          </span>
        )}
        <span className="callsite-loc">{isExternal ? "external dependency" : `${rel}:${line}`}</span>
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
            ⟳ Focus here
          </button>
        )}
        <button type="button" className="callsite-close" onClick={onClose} aria-label="Close">
          ×
        </button>
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
        <HighlightedCode code={code} file={rel} startLine={start} highlightLine={line} />
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
  onFocus,
}: {
  repoId: string;
  data: CodemapResponse;
  onFocus?: (label: string) => void;
}) {
  const [peek, setPeek] = useState<PeekSource | null>(null);
  useEffect(() => setPeek(null), [data]); // a fresh graph invalidates the open peek

  return (
    <>
      <CodeGraph
        data={data}
        onNodeClick={(node) => setPeek({ kind: "node", node })}
        onEdgeClick={(info) => setPeek({ kind: "edge", ...info })}
      />
      {peek && (
        <SourcePeek
          key={peek.kind === "node" ? `node:${peek.node.label}` : `${peek.srcLabel}->${peek.tgtLabel}`}
          repoId={repoId}
          source={peek}
          onClose={() => setPeek(null)}
          onFocus={onFocus}
        />
      )}
    </>
  );
}
