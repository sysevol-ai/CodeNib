"use client";

import type { EdgeClickInfo, GraphNodeInfo } from "@/components/CodeGraph";
import { repoRelative, type CallSite, type CodemapResponse } from "@/lib/api";

type CMNode = CodemapResponse["nodes"][number];
type CMEdge = CodemapResponse["edges"][number];

interface ComponentGroup {
  id: string;
  title: string;
  subtitle: string;
  files: string[];
  nodes: CMNode[];
  roots: number;
  inbound: number;
  outbound: number;
  stage: StageKey;
}

interface ComponentLink {
  key: string;
  source: string;
  target: string;
  weight: number;
  anchors: CallSite[];
  srcLabel: string;
  tgtLabel: string;
  /** Both endpoints sit in one component, so this is a symbol-to-symbol
   *  relationship rather than an aggregate between two components. A topical
   *  wiki page usually cites one module, so these are most of its edges. */
  internal: boolean;
}

type StageKey = "entry" | "core" | "capability" | "evidence";

interface StageColumn {
  key: StageKey;
  label: string;
  groups: ComponentGroup[];
}

const STAGE_ORDER: StageKey[] = ["entry", "core", "capability", "evidence"];
const STAGE_LABELS: Record<StageKey, string> = {
  entry: "Entry",
  core: "Core",
  capability: "Capabilities",
  evidence: "Evidence",
};

const GENERIC_ROOTS = new Set(["src", "lib", "packages", "package", "modules", "crates", "app"]);

function fileKey(node: CMNode): string {
  return node.external ? "external" : repoRelative(node.file || "") || "external";
}

function fileParts(file: string): string[] {
  return file.split("/").filter(Boolean);
}

function titleCase(text: string): string {
  return text
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
    .trim();
}

function basename(file: string): string {
  const parts = fileParts(file);
  return parts[parts.length - 1] || file;
}

function compactSymbol(label: string): string {
  const sym = label.split(":").pop() || label;
  return sym.length > 46 ? `${sym.slice(0, 43)}...` : sym;
}

function componentForFile(file: string): { id: string; title: string; subtitle: string } {
  if (!file || file === "external") {
    return { id: "external", title: "External APIs", subtitle: "outside repository" };
  }
  const parts = fileParts(file);
  if (parts[0] === "crates" && parts[1]) {
    const section = parts[2] === "src" && parts[3] ? parts[3].replace(/\.[^.]+$/, "") : "";
    const crateTitle = titleCase(parts[1]);
    return {
      id: section ? `crate:${parts[1]}:${section}` : `crate:${parts[1]}`,
      title: section ? `${crateTitle} / ${titleCase(section)}` : crateTitle,
      subtitle: section ? "crate area" : "crate",
    };
  }
  if (parts[0] === "astropy" && parts[1]) {
    return {
      id: `astropy:${parts[1]}`,
      title: titleCase(parts[1]),
      subtitle: "package",
    };
  }
  if (parts[0] === "modules" && parts[1]) {
    return {
      id: `module:${parts[1]}`,
      title: titleCase(parts[1]),
      subtitle: "module",
    };
  }
  if (parts[0] === "lib" && parts[1]) {
    return {
      id: `lib:${parts[1]}`,
      title: titleCase(parts[1]),
      subtitle: "library area",
    };
  }
  const last = parts[parts.length - 1] || "";
  const fallback = last === "__init__.py" && parts.length > 1 ? parts[parts.length - 2] : last;
  const firstMeaningful = parts.find((part) => !GENERIC_ROOTS.has(part)) || fallback;
  const id = firstMeaningful || parts[0] || "root";
  return {
    id: `path:${id}`,
    title: titleCase(id === parts[parts.length - 1] ? id.replace(/\.[^.]+$/, "") : id),
    subtitle: parts.length > 1 ? "component" : "root",
  };
}

function compareNodes(a: CMNode, b: CMNode): number {
  if (a.is_root !== b.is_root) return a.is_root ? -1 : 1;
  const imp = (b.importance ?? 0) - (a.importance ?? 0);
  if (Math.abs(imp) > 0.001) return imp;
  const refs = (b.ref_count ?? 0) - (a.ref_count ?? 0);
  if (refs !== 0) return refs;
  return (a.line ?? Number.MAX_SAFE_INTEGER) - (b.line ?? Number.MAX_SAFE_INTEGER);
}

function stageForGroup(group: ComponentGroup, rootGroupCount: number): StageKey {
  const active = group.inbound + group.outbound;
  if (active === 0) return group.roots > 0 ? "core" : "evidence";

  const strongEntry = group.outbound >= 2 && group.outbound > group.inbound * 1.35;
  const strongTarget = group.inbound >= 2 && group.inbound > group.outbound * 1.35;

  if (group.roots > 0) {
    if (rootGroupCount > 1 && strongEntry) return "entry";
    if (rootGroupCount > 1 && strongTarget) return "capability";
    return "core";
  }
  if (group.outbound > 0 && group.inbound === 0) return "entry";
  if (group.inbound > 0 && group.outbound === 0) return "capability";
  if (strongEntry) return "entry";
  if (strongTarget) return "capability";
  return "core";
}

function stageRank(stage: StageKey): number {
  const idx = STAGE_ORDER.indexOf(stage);
  return idx === -1 ? STAGE_ORDER.length : idx;
}

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

function buildMainFlow(groups: ComponentGroup[], links: ComponentLink[]): ComponentGroup[] {
  const groupById = new Map(groups.map((group) => [group.id, group]));
  if (!links.length) {
    return groups
      .filter((group) => group.roots > 0)
      .slice(0, 4);
  }

  const orderedLinks = [...links].sort((a, b) => b.weight - a.weight || a.key.localeCompare(b.key));
  const path: string[] = [orderedLinks[0].source, orderedLinks[0].target];
  const seen = new Set(path);

  while (path.length < 5) {
    const tail = path[path.length - 1];
    const next = orderedLinks.find((link) => link.source === tail && !seen.has(link.target));
    if (!next) break;
    path.push(next.target);
    seen.add(next.target);
  }
  while (path.length < 5) {
    const head = path[0];
    const prev = orderedLinks.find((link) => link.target === head && !seen.has(link.source));
    if (!prev) break;
    path.unshift(prev.source);
    seen.add(prev.source);
  }

  return path
    .map((id) => groupById.get(id))
    .filter((group): group is ComponentGroup => Boolean(group));
}

function buildSystem(data: CodemapResponse): {
  groups: ComponentGroup[];
  links: ComponentLink[];
  stages: StageColumn[];
  mainFlow: ComponentGroup[];
} {
  const groups = new Map<string, ComponentGroup>();
  const groupByNode = new Map<string, string>();
  const nodeById = new Map(data.nodes.map((node) => [node.id, node]));

  for (const node of data.nodes) {
    const file = fileKey(node);
    const component = componentForFile(file);
    let group = groups.get(component.id);
    if (!group) {
      group = {
        id: component.id,
        title: component.title,
        subtitle: component.subtitle,
        files: [],
        nodes: [],
        roots: 0,
        inbound: 0,
        outbound: 0,
        stage: "evidence",
      };
      groups.set(component.id, group);
    }
    group.nodes.push(node);
    if (node.is_root) group.roots += 1;
    if (!group.files.includes(file)) group.files.push(file);
    groupByNode.set(node.id, group.id);
  }

  const links = new Map<string, ComponentLink>();
  for (const edge of data.edges as CMEdge[]) {
    const sourceGroup = groupByNode.get(edge.source);
    const targetGroup = groupByNode.get(edge.target);
    if (!sourceGroup || !targetGroup) continue;
    // Same-component edges used to be dropped here. A wiki page is topical and
    // a topic usually lives in one module, so that discarded nearly every edge
    // the page had -- including the call-site anchors that make an edge
    // clickable. Keep them, keyed per symbol pair rather than per component.
    const internal = sourceGroup === targetGroup;
    const key = internal
      ? `sym\u0000${edge.source}\u0000${edge.target}`
      : `grp\u0000${sourceGroup}\u0000${targetGroup}`;
    const sourceNode = nodeById.get(edge.source);
    const targetNode = nodeById.get(edge.target);
    const weight = edge.weight ?? edge.anchors?.length ?? 1;
    let link = links.get(key);
    if (!link) {
      link = {
        key,
        source: sourceGroup,
        target: targetGroup,
        weight: 0,
        anchors: [],
        srcLabel: sourceNode?.short || groups.get(sourceGroup)?.title || sourceGroup,
        tgtLabel: targetNode?.short || groups.get(targetGroup)?.title || targetGroup,
        internal,
      };
      links.set(key, link);
    }
    link.weight += weight;
    if (edge.anchors) link.anchors.push(...edge.anchors);
    if (internal) continue;
    // Stage ordering reads flow *between* components; counting a component's
    // internal traffic as both its own inbound and outbound would flatten it.
    const source = groups.get(sourceGroup);
    const target = groups.get(targetGroup);
    if (source) source.outbound += weight;
    if (target) target.inbound += weight;
  }

  const rootGroupCount = [...groups.values()].filter((group) => group.roots > 0).length;
  for (const group of groups.values()) {
    group.stage = stageForGroup(group, rootGroupCount);
  }

  const orderedGroups = [...groups.values()].sort((a, b) => {
    const stage = stageRank(a.stage) - stageRank(b.stage);
    if (stage !== 0) return stage;
    if (b.roots !== a.roots) return b.roots - a.roots;
    const flow = b.outbound - b.inbound - (a.outbound - a.inbound);
    if (flow !== 0) return flow;
    return a.title.localeCompare(b.title);
  });
  for (const group of orderedGroups) {
    group.nodes.sort(compareNodes);
    group.files.sort();
  }

  const orderedLinks = [...links.values()]
    // Cross-component relationships describe the system, so they lead; the
    // symbol-level ones follow, ordered by how much traffic they carry.
    .sort(
      (a, b) =>
        Number(a.internal) - Number(b.internal) ||
        b.weight - a.weight ||
        a.key.localeCompare(b.key)
    )
    .slice(0, 10);

  const stages = STAGE_ORDER.map((stage) => ({
    key: stage,
    label: STAGE_LABELS[stage],
    groups: orderedGroups.filter((group) => group.stage === stage),
  })).filter((stage) => stage.groups.length > 0);

  return { groups: orderedGroups, links: orderedLinks, stages, mainFlow: buildMainFlow(
      orderedGroups,
      orderedLinks.filter((link) => !link.internal)
    ) };
}

export default function SystemMap({
  data,
  onNodeClick,
  onEdgeClick,
}: {
  data: CodemapResponse;
  onNodeClick?: (node: GraphNodeInfo) => void;
  onEdgeClick?: (info: EdgeClickInfo) => void;
}) {
  const { groups, links, stages, mainFlow } = buildSystem(data);
  const groupById = new Map(groups.map((group) => [group.id, group]));
  const totalFiles = new Set(data.nodes.map(fileKey)).size;
  const citedEntityCount = data.nodes.filter((node) => node.is_root).length;
  const hasRelationships = links.length > 0;

  return (
    <div className="system-map">
      <div className="system-map-head">
        <div>
          <div className="system-map-kicker">Core Logic Flow</div>
          <div className="system-map-title">
            {hasRelationships && mainFlow.length > 1
              ? mainFlow.map((group) => group.title).join(" -> ")
              : `${groups.length} cited component${groups.length === 1 ? "" : "s"}`}
          </div>
        </div>
        <div className="system-map-stats">
          <span>{citedEntityCount} cited entities</span>
          <span>{totalFiles} files</span>
          <span>{links.length} relationships</span>
        </div>
      </div>

      {hasRelationships && mainFlow.length > 1 && (
        <div className="system-flow-strip" aria-label="Primary component flow">
          {mainFlow.map((group, index) => (
            <div className="system-flow-step" key={group.id}>
              <span>{STAGE_LABELS[group.stage]}</span>
              <b>{group.title}</b>
              {index < mainFlow.length - 1 && <i aria-hidden="true">-&gt;</i>}
            </div>
          ))}
        </div>
      )}

      <div className="system-stage-board">
        {stages.map((stage) => (
          <section className="system-stage" key={stage.key}>
            <div className="system-stage-head">
              <span>{stage.label}</span>
              <b>{stage.groups.length}</b>
            </div>
            <div className="system-stage-body">
              {stage.groups.map((group) => (
                <article className={`system-node-card ${group.roots > 0 ? "root" : ""}`} key={group.id}>
                  <div className="system-node-head">
                    <div>
                      <h3>{group.title}</h3>
                      <p>{group.subtitle}</p>
                    </div>
                  </div>

                  <div className="system-source-row">
                    {group.files.slice(0, 2).map((file) => (
                      <span key={file} className="mono" title={file}>
                        {basename(file)}
                      </span>
                    ))}
                    {group.files.length > 2 && <span>+{group.files.length - 2} files</span>}
                  </div>

                  <div className="system-symbols">
                    {group.nodes.slice(0, 4).map((node) => (
                      <button
                        key={node.id}
                        type="button"
                        className={`system-symbol ${node.is_root ? "root" : ""}`}
                        onClick={() => onNodeClick?.(nodeToInfo(node))}
                        title={`${node.label}${node.file ? ` (${repoRelative(node.file)}:${node.line ?? "?"})` : ""}`}
                      >
                        <span>{node.kind}</span>
                        <b>{compactSymbol(node.short)}</b>
                        {node.file && <em className="mono">{basename(repoRelative(node.file))}:{node.line ?? "?"}</em>}
                      </button>
                    ))}
                    {group.nodes.length > 4 && <div className="system-more">+{group.nodes.length - 4} more cited symbols</div>}
                  </div>
                </article>
              ))}
            </div>
          </section>
        ))}
      </div>

      {links.length > 0 && (
        <div className="system-links" aria-label="Exact code relationships">
          {links.map((link) => {
            const source = groupById.get(link.source);
            const target = groupById.get(link.target);
            const clickable = link.anchors.length > 0 && !!onEdgeClick;
            return (
              <button
                key={link.key}
                type="button"
                className="system-link"
                // Labels are compacted to fit the chip, and two distinct
                // symbols can compact to the same string; keep the full pair
                // reachable so they stay tellable apart.
                title={`${link.srcLabel} -> ${link.tgtLabel}`}
                disabled={!clickable}
                onClick={() =>
                  clickable &&
                  onEdgeClick?.({
                    anchors: link.anchors,
                    srcLabel: link.srcLabel,
                    tgtLabel: link.tgtLabel,
                  })
                }
              >
                <span className="system-link-main">
                  {/* A cross-component link aggregates many symbol pairs, so it
                      is named by component; an internal one is a single pair and
                      is named by the symbols themselves. */}
                  <span>
                    {link.internal
                      ? compactSymbol(link.srcLabel)
                      : source?.title || link.source}
                  </span>
                  <b>-&gt;</b>
                  <span>
                    {link.internal
                      ? compactSymbol(link.tgtLabel)
                      : target?.title || link.target}
                  </span>
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
