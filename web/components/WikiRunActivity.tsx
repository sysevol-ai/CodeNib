// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { SavedWiki } from "@/lib/visitorWiki";
import { formatWait, wikiActivity } from "@/lib/wikiActivity";

export default function WikiRunActivity({ wiki, now, checkedAt, connected, compact = false }: {
  wiki: SavedWiki; now: number; checkedAt: number; connected: boolean;
  compact?: boolean;
}) {
  const activity = wikiActivity(wiki, now, connected);
  if (!activity.running) return null;
  const receiving = wiki.request_active && (wiki.response_chars || 0) > 0;
  return (
    <div className={`wiki-run-activity is-${activity.tone} ${compact ? "is-compact" : ""}`} aria-label="Current agent activity">
      <div className="wiki-run-status" role="status">
        <span className={`wiki-run-pulse ${activity.animate ? "is-animated" : ""}`} aria-hidden="true"><i /><i /><i /></span>
        <strong>{activity.label}</strong>
        <span className="wiki-run-elapsed">{activity.stageAge === null ? "" : formatWait(activity.stageAge)}</span>
      </div>
      <div className="wiki-run-description" aria-live="polite">
        {activity.chapter && <strong className="wiki-run-chapter">Working on: {activity.chapter}</strong>}
        <p className="wiki-work-explanation">{activity.description}</p>
        {wiki.request_active && connected && !activity.delayed && <p className="wiki-stream-state">
          {receiving ? "Receiving the model’s response…" : "Waiting for the model to respond…"}
        </p>}
      </div>
      <div className="wiki-run-facts">
        {wiki.source_files != null && <span>✓ {wiki.source_files} repository files prepared</span>}
        {(wiki.scope === "concise" || wiki.scope === "focused") && <span>Overview + core chapters</span>}
      </div>
      {activity.tone === "waiting" && <p className="wiki-run-explanation">{activity.explanation}</p>}
      <details className="wiki-run-details">
        <summary>Live activity &amp; timing</summary>
        {compact && <p>{activity.description}</p>}
        <p className="wiki-run-next"><span>Next</span> {activity.next}</p>
        <dl className="wiki-run-timing">
          <div><dt>Time in this step</dt><dd>{activity.stageAge === null ? "Not reported" : formatWait(activity.stageAge)}</dd></div>
          <div><dt>Last progress</dt><dd>{formatWait(activity.progressAge)} ago</dd></div>
          <div><dt>Server connection</dt><dd>{connected ? `Checked ${formatWait(now - checkedAt)} ago` : "Reconnecting…"}</dd></div>
        </dl>
        {!!wiki.request_active && <p className="small muted">{(wiki.response_chars || 0).toLocaleString()} response characters received. Draft content appears only after source checks pass.</p>}
        {activity.tone !== "waiting" && <p className="wiki-run-explanation">{activity.explanation}</p>}
        {activity.recent.length > 0 && <div className="wiki-run-recent">
          <h3>Recent agent activity</h3>
          <ol>{activity.recent.map((event, index) => <li key={`${event.at}-${index}`}>
            <time dateTime={new Date(event.at * 1000).toISOString()}>{new Date(event.at * 1000).toLocaleTimeString()}</time>
            <span>{event.label}{event.chapter ? ` · ${event.chapter}` : ""}</span>
          </li>)}</ol>
        </div>}
      </details>
    </div>
  );
}
