// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";
import type { Citation, WikiMediaSlot } from "@/lib/api";
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
}: {
  slot: WikiMediaSlot;
  trace?: ArchitectureJourney;
  onEvidence?: (ids: string[]) => void;
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

  return (
    <section className="arch" aria-label={slot.title}>
      <h2 className="arch-title">{slot.title}</h2>
      <ol className="arch-path">
        {primaryNodes.map((node, index) => {
          const handoff = handoffs[index];
          return (
            <li className="arch-step" key={node.id}>
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
    </section>
  );
}
