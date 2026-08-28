// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

"use client";

import type { IndexType, RepoIndexStatus } from "@/lib/api";
import IndexJobControl from "./IndexJobControl";

const RETRIEVAL_INDEXES = new Set<IndexType>(["bm25", "vector"]);
const INDEX_LABELS: Record<IndexType, string> = {
  bm25: "BM25",
  vector: "Embeddings",
  symbol_graph: "Symbol graph",
};

export interface AskIndexAttention {
  needsAttention: boolean;
  state: "fresh" | "stale" | "updating" | "failed";
  indexes: IndexType[];
  writable: boolean;
}

export function askIndexAttention(status: RepoIndexStatus): AskIndexAttention {
  const relevant = status.indexes.filter(
    (surface) =>
      surface.index_type === "bm25" ||
      (surface.index_type === "vector" &&
        (surface.state !== "missing" || surface.updates_enabled)),
  );
  const unsettled = relevant.filter((surface) => surface.state !== "built");
  const needsAttention = status.stale || unsettled.length > 0;
  let state: AskIndexAttention["state"] = "fresh";
  if (needsAttention) {
    state = unsettled.some((surface) => surface.state === "failed")
      ? "failed"
      : unsettled.some((surface) => surface.state === "updating")
        ? "updating"
        : "stale";
  }
  return {
    needsAttention,
    state,
    indexes: (unsettled.length ? unsettled : relevant).map(
      (surface) => surface.index_type,
    ),
    writable: relevant.some((surface) => surface.updates_enabled),
  };
}

function retrievalOnlyStatus(status: RepoIndexStatus): RepoIndexStatus {
  return {
    ...status,
    indexes: status.indexes.map((surface) =>
      RETRIEVAL_INDEXES.has(surface.index_type)
        ? surface
        : {
            ...surface,
            update_mode: "unavailable",
            updates_enabled: false,
            update_reason: "Symbol graph updates do not change Ask retrieval.",
          },
    ),
  };
}

const TITLES: Record<AskIndexAttention["state"], string> = {
  fresh: "Retrieval indexes are current",
  stale: "Newer source changes are available",
  updating: "Retrieval indexes are updating",
  failed: "A retrieval index needs attention",
};

export default function AskIndexFreshness({
  status,
  hasPendingQuestion,
  showUpdateControl,
  onShowUpdate,
  onAskCurrent,
  onRefresh,
}: {
  status: RepoIndexStatus;
  hasPendingQuestion: boolean;
  showUpdateControl: boolean;
  onShowUpdate: () => void;
  onAskCurrent: () => void;
  onRefresh: () => Promise<void>;
}) {
  const attention = askIndexAttention(status);
  if (!attention.needsAttention) return null;
  const labels = attention.indexes.map((indexType) => INDEX_LABELS[indexType]);
  const subject = labels.length ? labels.join(" and ") : "retrieval indexes";
  const tracking = attention.state === "updating";

  return (
    <section
      className={`ask-index-freshness ${attention.state}`}
      aria-label="Ask index freshness"
    >
      <div className="ask-index-freshness-head">
        <span className={`index-state ${attention.state}`}>
          {attention.state}
        </span>
        <strong>{TITLES[attention.state]}</strong>
      </div>
      <p>
        {subject} may not match the repository HEAD. You can use the current
        indexed snapshot or update before asking.
      </p>
      {status.last_indexed_commit && status.current_head && (
        <div className="ask-index-commits mono">
          Indexed {status.last_indexed_commit.slice(0, 8)} · HEAD{" "}
          {status.current_head.slice(0, 8)}
        </div>
      )}
      <div className="ask-index-actions">
        {attention.writable && !showUpdateControl && (
          <button type="button" onClick={onShowUpdate}>
            {tracking ? "Track update first" : "Update indexes first"}
          </button>
        )}
        {hasPendingQuestion && (
          <button type="button" onClick={onAskCurrent}>
            Ask with current index
          </button>
        )}
      </div>
      {showUpdateControl && (
        <IndexJobControl
          status={retrievalOnlyStatus(status)}
          onRefresh={onRefresh}
        />
      )}
    </section>
  );
}
