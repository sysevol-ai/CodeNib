// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { CSSProperties, ReactNode } from "react";
import type { Citation, WikiMediaSlot } from "@/lib/api";
import MainPath from "@/components/MainPath";
import { displaySymbol } from "@/lib/symbols";
import { stageRoles, type Journey } from "@/lib/wikiPresentation";

/** Render a heading string with its `code` spans. */
function inlineCode(text: string): ReactNode[] {
  return text
    .split(/(`[^`]+`)/)
    .map((part, index) =>
      part.startsWith("`") && part.endsWith("`") ? (
        <code key={index}>{part.slice(1, -1)}</code>
      ) : (
        <span key={index}>{part}</span>
      ),
    );
}

/** What the architecture needs to fall as the main path does: the map's
 *  colours, each page's top-level area, and the page's sources. */
export interface ArchitectureCascade {
  colors: Map<string, string>;
  areaOf: (pageId: string) => { id: string; title: string } | undefined;
  citations?: Citation[];
  onPick: (pageId: string) => void;
  onSource?: (index: number) => void;
  renderText: (markdown: string) => ReactNode;
}

export interface ArchitectureJourney {
  journey: Journey;
  citations?: Citation[];
  renderText: (markdown: string) => ReactNode;
  onPick?: (pageId: string) => void;
}

/**
 * How the parts of a system fit together, read as a numbered path with the
 * other roles and boundaries listed after it. The plan names each role, what
 * it does and what it hands to the next; a role's name opens its source.
 *
 * It used to be a card of cards, each tagged with a layer ("Execution",
 * "Data & artifacts") and followed by its own "View source" link. Those tags
 * were the model's classification, not anything the reader asked about.
 */
export default function SystemArchitecture({
  slot,
  trace,
  onEvidence,
  cascade,
}: {
  slot: WikiMediaSlot;
  trace?: ArchitectureJourney;
  onEvidence?: (ids: string[]) => void;
  /** Draw the roles as the main path is drawn, in the map's colours. */
  cascade?: ArchitectureCascade;
}) {
  const contract = slot.render_contract;
  if (
    contract?.adapter !== "architecture" ||
    contract.provenance !== "architecture-plan"
  ) {
    return null;
  }
  const nodes = new Map(contract.data.nodes.map((node) => [node.id, node]));
  const primaryNodes = contract.data.primary_path
    .map((id) => nodes.get(id))
    .filter((node) => node !== undefined);
  const primaryIds = new Set(primaryNodes.map((node) => node.id));
  const supportingNodes = contract.data.nodes.filter(
    (node) => !primaryIds.has(node.id),
  );
  const handoffs = contract.data.primary_path
    .slice(0, -1)
    .map((source, index) =>
      contract.data.edges.find(
        (edge) =>
          edge.source === source &&
          edge.target === contract.data.primary_path[index + 1],
      ),
    );
  const stages = trace?.journey.stages ?? [];
  const roles = trace
    ? stageRoles(stages, contract.data.nodes, trace.citations)
    : [];

  const sourceButton = (content: ReactNode, evidence: string[], title: string) =>
    onEvidence && evidence.length > 0 ? (
      <button
        type="button"
        className="arch-source"
        title={title}
        onClick={() => onEvidence(evidence)}
      >
        {content}
      </button>
    ) : (
      content
    );

  /** The traced functions that run inside a role, when the path says so. */
  const roleStages = (roleId: string) => {
    const own = stages.filter((_stage, index) => roles[index] === roleId);
    if (!own.length) return null;
    return (
      <p className="arch-stages" aria-label="Traced functions in this role">
        {own.map((stage) => (
          <code key={stage.index} title={stage.symbol}>
            {displaySymbol(stage.symbol, { short: true })}
          </code>
        ))}
      </p>
    );
  };

  const peerOf = (nodeId: string) => {
    const edge = contract.data.edges.find(
      (candidate) => candidate.source === nodeId || candidate.target === nodeId,
    );
    const peer = edge && nodes.get(edge.source === nodeId ? edge.target : edge.source);
    if (!edge || !peer) return null;
    return { edge, peer, outgoing: edge.source === nodeId };
  };

  const context = (
    <>
        {(supportingNodes.length > 0 || contract.data.boundaries.length > 0) && (
          <div className="arch-context">
            {supportingNodes.length > 0 && (
              <div className="arch-group">
                <h3 className="arch-group-title">Also involved</h3>
                <dl>
                  {supportingNodes.map((node) => {
                    const link = peerOf(node.id);
                    return (
                      <div className="arch-item" key={node.id}>
                        <dt>{sourceButton(node.label, node.evidence, "Open the source for this role")}</dt>
                        <dd>
                          {inlineCode(node.detail)}
                          {link && (
                            <span className="arch-item-link">
                              {link.outgoing ? "Hands to " : "Takes from "}
                              {link.peer.label}: {inlineCode(link.edge.label)}
                            </span>
                          )}
                        </dd>
                      </div>
                    );
                  })}
                </dl>
              </div>
            )}
            {contract.data.boundaries.length > 0 && (
              <div className="arch-group">
                <h3 className="arch-group-title">Boundaries</h3>
                <dl>
                  {contract.data.boundaries.map((boundary) => (
                    <div className="arch-item" key={boundary.id}>
                      <dt>{boundary.label}</dt>
                      <dd>
                        {boundary.detail && inlineCode(boundary.detail)}
                        <span className="arch-item-link">
                          {boundary.members
                            .map((member) => nodes.get(member)?.label)
                            .filter(Boolean)
                            .join(" · ")}
                        </span>
                      </dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}
          </div>
        )}
    </>
  );

  /** What a role hands to the next, and the traced functions inside it. */
  const after = (index: number) => {
    const node = primaryNodes[index];
    const handoff = handoffs[index];
    return (
      <>
        {node && roleStages(node.id)}
        {handoff && (
          <p className="main-path-handoff">
            {sourceButton(inlineCode(handoff.label), handoff.evidence, "Open the source for this handoff")}
          </p>
        )}
      </>
    );
  };

  if (cascade) {
    // The roles fall as the main path does, each line in the colour of the
    // area its source is in, so a repository with no traced call path still
    // reads as one coloured stair down the system.
    const journey = {
      title: slot.title,
      stages: primaryNodes.map((node, index) => ({
        index: index + 1,
        symbol: node.label,
        text: node.detail,
        evidence: node.evidence,
        source: node.area?.evidence,
        page: node.area ? cascade.areaOf(node.area.id) ?? { id: node.area.id, title: node.area.title } : undefined,
      })),
    };
    return (
      <section className="arch is-cascade" aria-label={slot.title}>
        <MainPath
          journey={journey}
          roles
          onPick={cascade.onPick}
          renderText={cascade.renderText}
          colors={cascade.colors}
          citations={cascade.citations}
          onSource={cascade.onSource}
          renderAfter={after}
        />
        {context}
      </section>
    );
  }

  return (
    <section className="arch" aria-label={slot.title}>
      <h2 className="arch-title">{slot.title}</h2>
      {/* The roles fall as the Overview's main path does: each a step right
          of the one that hands to it, joined by an elbow. */}
      <ol className="arch-path" style={{ "--steps": Math.max(primaryNodes.length - 1, 1) } as CSSProperties}>
        {primaryNodes.map((node, index) => {
          const handoff = handoffs[index];
          return (
            <li className="arch-step" key={node.id} style={{ "--depth": index } as CSSProperties}>
              <span className="arch-index" aria-hidden="true">
                {index + 1}
              </span>
              <div className="arch-body">
                <h3 className="arch-role">
                  {sourceButton(node.label, node.evidence, "Open the source for this role")}
                </h3>
                <p>{inlineCode(node.detail)}</p>
                {roleStages(node.id)}
                {handoff && (
                  <p className="arch-handoff">
                    {sourceButton(
                      inlineCode(handoff.label),
                      handoff.evidence,
                      "Open the source for this handoff",
                    )}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {context}
    </section>
  );
}
