// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useState, type ReactNode } from "react";
import type { Citation, WikiMediaSlot } from "@/lib/api";
import {
  stageRoles,
  stageSentence,
  type Journey,
} from "@/lib/wikiPresentation";

function architectureLayerLabel(layer: string | undefined): string {
  switch (layer) {
    case "external":
      return "Outside the system";
    case "interface":
      return "Interface";
    case "coordination":
      return "Coordination";
    case "execution":
      return "Execution";
    case "data":
      return "Data & artifacts";
    default:
      return "System role";
  }
}

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
  /** False when the page already shows the path elsewhere (the poster). */
  showPath?: boolean;
}

export default function SystemArchitecture({
  slot,
  trace,
  onEvidence,
}: {
  slot: WikiMediaSlot;
  trace?: ArchitectureJourney;
  onEvidence?: (ids: string[]) => void;
}) {
  const [activeRole, setActiveRole] = useState<string | null>(null);
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
  const primaryConnections = contract.data.primary_path
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
  const stagesOf = (roleId: string) =>
    stages.filter((_stage, index) => roles[index] === roleId);
  const lit = (roleId: string | null) =>
    activeRole == null ? "" : roleId === activeRole ? "is-lit" : "is-dim";

  const supportingConnection = (nodeId: string) => {
    const edge = contract.data.edges.find(
      (candidate) => candidate.source === nodeId || candidate.target === nodeId,
    );
    if (!edge) return null;
    const peerId = edge.source === nodeId ? edge.target : edge.source;
    const peer = nodes.get(peerId);
    if (!peer) return null;
    return `${edge.label} ${edge.source === nodeId ? "→" : "←"} ${peer.label}`;
  };

  const roleStages = (roleId: string) => {
    const own = stagesOf(roleId);
    if (!own.length) return null;
    return (
      <div
        className="wiki-system-role-stages"
        aria-label="Traced functions in this role"
      >
        {own.map((stage) => (
          <span className="wiki-system-stage-chip" key={stage.index}>
            <span className="wiki-system-stage-number">{stage.index}</span>
            <code>{stage.symbol}</code>
          </span>
        ))}
      </div>
    );
  };

  return (
    <section
      className="wiki-system-architecture"
      aria-label={slot.title}
      onMouseLeave={() => setActiveRole(null)}
    >
      <header className="wiki-system-architecture-head">
        <div>
          <span className="wiki-system-architecture-eyebrow">
            System architecture
          </span>
          <h3>{slot.title}</h3>
        </div>
      </header>
      <div className="wiki-system-architecture-body">
        <div className="wiki-system-primary">
          <div className="wiki-system-section-label">
            <span>Main path</span>
          </div>
          <ol className="wiki-system-path">
            {primaryNodes.map((node, index) => {
              const connection = primaryConnections[index];
              return (
                <li className="wiki-system-path-step" key={node.id}>
                  <article
                    className={`wiki-system-role layer-${node.layer ?? "system"} ${lit(node.id)}`}
                    onMouseEnter={() => setActiveRole(node.id)}
                  >
                    <span className="wiki-system-role-index" aria-hidden="true">
                      {index + 1}
                    </span>
                    <div className="wiki-system-role-copy">
                      <span className="wiki-system-role-layer">
                        {architectureLayerLabel(node.layer)}
                      </span>
                      <h4>{node.label}</h4>
                      <p>{inlineCode(node.detail)}</p>
                      {roleStages(node.id)}
                      {onEvidence && (
                        <button
                          type="button"
                          className="wiki-system-evidence"
                          onClick={() => onEvidence(node.evidence)}
                        >
                          View source ↗
                        </button>
                      )}
                    </div>
                  </article>
                  {connection && (
                    <div className="wiki-system-connection">
                      <span
                        className="wiki-system-connection-line"
                        aria-hidden="true"
                      />
                      <span>{inlineCode(connection.label)}</span>
                      {onEvidence && (
                        <button
                          type="button"
                          className="wiki-system-evidence"
                          aria-label={`Source for ${connection.label}`}
                          onClick={() => onEvidence(connection.evidence)}
                        >
                          ↗
                        </button>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
        <aside className="wiki-system-context">
          {supportingNodes.length > 0 && (
            <div className="wiki-system-supporting">
              <div className="wiki-system-section-label">
                <span>Supporting roles</span>
              </div>
              {supportingNodes.map((node) => (
                <article
                  className={`wiki-system-support-card ${lit(node.id)}`}
                  key={node.id}
                  onMouseEnter={() => setActiveRole(node.id)}
                >
                  <span>{architectureLayerLabel(node.layer)}</span>
                  <h4>{node.label}</h4>
                  <p>{inlineCode(node.detail)}</p>
                  {roleStages(node.id)}
                  {onEvidence && (
                    <button
                      type="button"
                      className="wiki-system-evidence"
                      onClick={() => onEvidence(node.evidence)}
                    >
                      View source ↗
                    </button>
                  )}
                  {supportingConnection(node.id) && (
                    <small>{inlineCode(supportingConnection(node.id)!)}</small>
                  )}
                </article>
              ))}
            </div>
          )}
          {contract.data.boundaries.length > 0 && (
            <div className="wiki-system-boundaries">
              <div className="wiki-system-section-label">
                <span>Boundaries</span>
              </div>
              {contract.data.boundaries.map((boundary) => (
                <article className="wiki-system-boundary" key={boundary.id}>
                  <span
                    className="wiki-system-boundary-mark"
                    aria-hidden="true"
                  />
                  <div>
                    <h4>{boundary.label}</h4>
                    {boundary.detail && <p>{inlineCode(boundary.detail)}</p>}
                    <small>
                      {boundary.members
                        .map((member) => nodes.get(member)?.label)
                        .filter(Boolean)
                        .join(" · ")}
                    </small>
                  </div>
                </article>
              ))}
            </div>
          )}
        </aside>
      </div>
      {trace && trace.showPath !== false && stages.length > 0 && (
        <div className="wiki-system-trace">
          <div className="wiki-system-section-label">
            <span>Traced call path, recorded in the index</span>
          </div>
          <h4 className="wiki-system-trace-title">
            {inlineCode(trace.journey.title)}
          </h4>
          <ol className="wiki-system-trace-steps">
            {stages.map((stage, index) => {
              const role = roles[index] ? nodes.get(roles[index]!) : undefined;
              return (
                <li
                  key={stage.index}
                  className={`wiki-system-trace-step ${role ? lit(role.id) : activeRole ? "is-dim" : ""}`}
                  onMouseEnter={() => setActiveRole(role?.id ?? null)}
                >
                  <span className="wiki-system-stage-number">
                    {stage.index}
                  </span>
                  <div className="wiki-system-trace-copy">
                    <div className="wiki-system-trace-head">
                      <code className="wiki-system-trace-symbol">
                        {stage.symbol}
                      </code>
                      {role && (
                        <span
                          className={`wiki-system-trace-role layer-${role.layer ?? "system"}`}
                        >
                          {role.label}
                        </span>
                      )}
                      {stage.page && trace.onPick && (
                        <button
                          type="button"
                          className="wiki-system-trace-page"
                          onClick={() => trace.onPick?.(stage.page!.id)}
                        >
                          {stage.page.title} →
                        </button>
                      )}
                    </div>
                    <div className="wiki-system-trace-text">
                      {trace.renderText(stageSentence(stage))}
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </section>
  );
}
