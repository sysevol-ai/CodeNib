// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { SourceOutline } from "@/lib/visitorWiki";
import { layoutWikiGraph, type WikiGraph, type WikiGraphEdge, type WikiGraphNode } from "@/lib/wikiGraph";

export const SOURCE_HUB = "source:hub";
/** Folders stay grey: colour arrives with chapters, which give the map its meaning. */
const HUB_COLOR = "#c4c7c5";
const FOLDER_COLOR = "#6f757b";

export interface SourceNodeFacts {
  path: string;
  files: number;
  direct: boolean;
}

/** A folder's name on the map, relative to the folder the outline opened. */
export function folderLabel(path: string, root: string, direct = false): string {
  if (direct) return path ? `files in ${path.split("/").pop()}/` : "top-level files";
  const relative = root && path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
  return `${relative}/`;
}

/**
 * The snapshot as a map before any chapter exists: the opened folder in the
 * middle and its folders around it, sized by how many code files each holds.
 * Laid out by the same deterministic layout as the Wiki map.
 */
export function buildSourceGraph(
  repository: string,
  outline: SourceOutline | null | undefined,
): { graph: WikiGraph; facts: Map<string, SourceNodeFacts> } {
  const name = repository.split("/").pop() || repository;
  const facts = new Map<string, SourceNodeFacts>();
  const nodes: WikiGraphNode[] = [
    {
      id: SOURCE_HUB,
      title: outline?.root ? `${outline.root}/` : `${name}/`,
      kind: "area",
      areaId: SOURCE_HUB,
      color: HUB_COLOR,
      radius: 9,
      x: 0,
      y: 0,
    },
  ];
  const edges: WikiGraphEdge[] = [];
  facts.set(SOURCE_HUB, { path: outline?.root ?? "", files: outline?.files ?? 0, direct: false });
  const folders = outline?.folders ?? [];
  const most = Math.max(1, ...folders.map((folder) => folder.files));
  const add = (id: string, title: string, files: number, fact: SourceNodeFacts) => {
    facts.set(id, fact);
    nodes.push({
      id,
      title,
      kind: "page",
      areaId: SOURCE_HUB,
      color: FOLDER_COLOR,
      radius: 3.5 + 8.5 * Math.sqrt(files / most),
      x: 0,
      y: 0,
    });
    edges.push({ id: `${SOURCE_HUB}>${id}`, source: SOURCE_HUB, target: id, kind: "contains" });
  };
  for (const folder of folders) {
    add(
      `source:${folder.direct ? "files:" : ""}${folder.path}`,
      folderLabel(folder.path, outline?.root ?? "", folder.direct),
      folder.files,
      { path: folder.path, files: folder.files, direct: !!folder.direct },
    );
  }
  if (outline?.more.folders) {
    add(
      "source:more",
      `${outline.more.folders} more ${outline.more.folders === 1 ? "folder" : "folders"}`,
      outline.more.files,
      { path: "", files: outline.more.files, direct: false },
    );
  }
  layoutWikiGraph(nodes, edges);
  return { graph: { nodes, edges, colors: new Map() }, facts };
}

/** The status line for a folder under the pointer. */
export function describeSourceNode(
  node: WikiGraphNode,
  facts: Map<string, SourceNodeFacts>,
): { title: string; detail: string } | null {
  const fact = facts.get(node.id);
  if (!fact) return null;
  const files = `${fact.files.toLocaleString()} code ${fact.files === 1 ? "file" : "files"}`;
  if (node.id === SOURCE_HUB) return { title: node.title, detail: `${files} in this snapshot` };
  if (node.id === "source:more") return { title: node.title, detail: `${files} in smaller folders` };
  return { title: fact.direct ? node.title : fact.path || node.title, detail: files };
}
