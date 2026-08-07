"use client";

import { useMemo, useState } from "react";

import type { EdgeClickInfo, GraphNodeInfo } from "@/components/CodeGraph";
import type { CallSite, CodemapHierarchyNode, CodemapResponse } from "@/lib/api";

type CMNode = CodemapResponse["nodes"][number];

interface RolledEdge {
  key: string;
  source: string;
  target: string;
  weight: number;
  anchors: CallSite[];
  srcLabel: string;
  tgtLabel: string;
}

const CONTAINER_KINDS = new Set(["root", "directory", "file"]);

function nodeToInfo(node: CMNode): GraphNodeInfo {
  return {
    label: node.label,
    short: node.short,
    file: node.file ?? null,
    line: node.line ?? null,
    endLine: node.end_line ?? null,
    kind: node.kind,
    external: node.external === true,
  };
}

/** Drill-down over the served containment tree: root -> directory -> file -> symbol.
 *
 *  Edges arrive keyed to leaf symbols. Which container an edge should be drawn
 *  between depends on what the reader has expanded, so the roll-up happens here
 *  rather than on the server: each endpoint resolves to its nearest *visible*
 *  ancestor and edges are aggregated onto that pair. */
export default function HierarchyMap({
  data,
  onNodeClick,
  onEdgeClick,
}: {
  data: CodemapResponse;
  onNodeClick?: (node: GraphNodeInfo) => void;
  onEdgeClick?: (info: EdgeClickInfo) => void;
}) {
  const hierarchy = data.hierarchy;

  const { byId, childrenOf, root, defaultOpen } = useMemo(() => {
    const nodes: CodemapHierarchyNode[] = hierarchy?.nodes ?? [];
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const childrenOf = new Map<string, CodemapHierarchyNode[]>();
    for (const node of nodes) {
      if (!node.parent) continue;
      const siblings = childrenOf.get(node.parent) ?? [];
      siblings.push(node);
      childrenOf.set(node.parent, siblings);
    }
    // Highest interest first, so an expanded container leads with what matters.
    for (const siblings of childrenOf.values()) {
      siblings.sort(
        (a, b) => (b.doi ?? 0) - (a.doi ?? 0) || a.label.localeCompare(b.label)
      );
    }
    const rootId = hierarchy?.root ?? "";
    const defaultOpen = new Set<string>(
      nodes.filter((n) => n.open_by_default && CONTAINER_KINDS.has(n.kind)).map((n) => n.id)
    );
    if (rootId) defaultOpen.add(rootId);
    return { byId, childrenOf, root: rootId, defaultOpen };
  }, [hierarchy]);

  const [open, setOpen] = useState<Set<string>>(defaultOpen);

  const viewNodeById = useMemo(
    () => new Map(data.nodes.map((n) => [n.id, n])),
    [data.nodes]
  );
  // A hierarchy symbol carries the view id its edges use.
  const hierIdForViewId = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of hierarchy?.nodes ?? []) {
      if (node.kind === "symbol" && node.node_id) map.set(node.node_id, node.id);
    }
    return map;
  }, [hierarchy]);

  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  /** Deepest ancestor-or-self the reader can currently see. */
  const nearestVisible = (hid: string | undefined): string | null => {
    if (!hid || !byId.has(hid)) return null;
    const chain: string[] = [];
    let cur: string | null | undefined = hid;
    while (cur && byId.has(cur)) {
      chain.push(cur);
      cur = byId.get(cur)?.parent ?? null;
    }
    chain.reverse();
    let visible = chain[0] ?? null;
    for (const id of chain) {
      visible = id;
      // A closed container is the boundary: nothing beneath it is on screen.
      if (CONTAINER_KINDS.has(byId.get(id)?.kind ?? "") && !open.has(id)) break;
    }
    return visible;
  };

  const labelFor = (hid: string): string => byId.get(hid)?.label ?? hid;

  const rolled = useMemo(() => {
    const acc = new Map<string, RolledEdge>();
    for (const edge of data.edges) {
      const srcHid =
        edge.source_hierarchy ?? hierIdForViewId.get(edge.source) ?? undefined;
      const tgtHid =
        edge.target_hierarchy ?? hierIdForViewId.get(edge.target) ?? undefined;
      const source = nearestVisible(srcHid);
      const target = nearestVisible(tgtHid);
      // Both endpoints inside one collapsed container: there is no visible pair
      // to draw between. Expanding it is what reveals the relationship.
      if (!source || !target || source === target) continue;
      const key = `${source}\u0000${target}`;
      let link = acc.get(key);
      if (!link) {
        link = {
          key,
          source,
          target,
          weight: 0,
          anchors: [],
          srcLabel: labelFor(source),
          tgtLabel: labelFor(target),
        };
        acc.set(key, link);
      }
      link.weight += edge.weight ?? edge.anchors?.length ?? 1;
      // Keep every descendant's call site, or collapsing a container would
      // silently change what clicking its edge opens.
      if (edge.anchors) link.anchors.push(...edge.anchors);
    }
    return [...acc.values()].sort((a, b) => b.weight - a.weight).slice(0, 12);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.edges, open, byId, hierIdForViewId]);

  if (!hierarchy || !hierarchy.nodes.length) return null;

  const renderNode = (node: CodemapHierarchyNode, depth: number) => {
    if (node.kind === "symbol") {
      const viewNode = node.node_id ? viewNodeById.get(node.node_id) : undefined;
      return (
        <button
          key={node.id}
          type="button"
          className={`hier-symbol${viewNode?.declaration ? " is-derived" : ""}`}
          title={viewNode?.label ?? node.label}
          disabled={!viewNode || !onNodeClick}
          onClick={() => viewNode && onNodeClick?.(nodeToInfo(viewNode))}
        >
          <span className="hier-symbol-name">{node.label}</span>
          {node.line != null && (
            <span className="hier-symbol-line mono">:{node.line}</span>
          )}
        </button>
      );
    }

    const kids = childrenOf.get(node.id) ?? [];
    const isOpen = open.has(node.id);
    return (
      <div
        key={node.id}
        className={`hier-box hier-${node.kind}${isOpen ? " is-open" : ""}`}
        style={{ marginInlineStart: depth > 1 ? 10 : 0 }}
      >
        <button
          type="button"
          className="hier-box-head"
          aria-expanded={isOpen}
          onClick={() => toggle(node.id)}
        >
          <span className="hier-chevron" aria-hidden="true">
            {isOpen ? "-" : "+"}
          </span>
          <span className="hier-label">{node.label}</span>
          <span className="hier-meta">
            {node.kind === "file"
              ? `${node.symbol_count} symbol${node.symbol_count === 1 ? "" : "s"}`
              : `${node.child_count} item${node.child_count === 1 ? "" : "s"} · ${node.symbol_count} symbols`}
          </span>
        </button>
        {isOpen && kids.length > 0 && (
          <div className="hier-children">
            {kids.map((child) => renderNode(child, depth + 1))}
          </div>
        )}
      </div>
    );
  };

  const rootNode = byId.get(root);
  const topLevel = childrenOf.get(root) ?? [];

  return (
    <div className="hier-map">
      <div className="hier-head">
        <span className="hier-kicker">Structure</span>
        <span className="hier-stats">
          {rootNode?.symbol_count ?? data.nodes.length} symbols ·{" "}
          {rolled.length} visible relationship{rolled.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="hier-tree">{topLevel.map((node) => renderNode(node, 1))}</div>

      {rolled.length > 0 && (
        <div className="hier-links" aria-label="Relationships at the current depth">
          {rolled.map((link) => {
            const clickable = link.anchors.length > 0 && !!onEdgeClick;
            return (
              <button
                key={link.key}
                type="button"
                className="hier-link"
                title={`${link.srcLabel} -> ${link.tgtLabel}`}
                disabled={!clickable}
                onClick={() =>
                  clickable &&
                  onEdgeClick?.({
                    anchors: link.anchors,
                    anchorCount: link.weight,
                    srcLabel: link.srcLabel,
                    tgtLabel: link.tgtLabel,
                  })
                }
              >
                <span className="hier-link-main">
                  <span>{link.srcLabel}</span>
                  <b>-&gt;</b>
                  <span>{link.tgtLabel}</span>
                </span>
                <em>{link.weight} refs</em>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
