// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { SavedWiki } from "@/lib/visitorWiki";
import { formatWait, wikiActivity } from "@/lib/wikiActivity";

export default function WikiRunActivity({
  wiki,
  now,
  checkedAt,
  connected,
}: {
  wiki: SavedWiki;
  now: number;
  checkedAt: number;
  connected: boolean;
}) {
  const activity = wikiActivity(wiki, now, connected);
  if (!activity.running) return null;
  return (
    <div className={`wiki-run-activity is-${activity.tone}`} aria-label="Current agent activity">
      <div className="wiki-run-status" role="status">
        <span className={`wiki-run-pulse ${activity.animate ? "is-animated" : ""}`} aria-hidden="true">
          <i /><i /><i />
        </span>
        <strong>{activity.label}</strong>
      </div>
      <div className="wiki-run-description" aria-live="polite">
        {activity.chapter && <span className="wiki-run-chapter">Working on: {activity.chapter}</span>}
        <p>{activity.description}</p>
        <p className="wiki-run-next"><span>Next</span> {activity.next}</p>
      </div>
      <dl className="wiki-run-timing">
        <div><dt>Time in this step</dt><dd>{activity.stageAge === null ? "Not reported" : formatWait(activity.stageAge)}</dd></div>
        <div><dt>Last progress</dt><dd>{formatWait(activity.progressAge)} ago</dd></div>
        <div><dt>Server connection</dt><dd>{connected ? `Checked ${formatWait(now - checkedAt)} ago` : "Reconnecting…"}</dd></div>
      </dl>
      <p className="wiki-run-explanation">{activity.explanation}</p>
      {activity.recent.length > 0 && (
        <div className="wiki-run-recent">
          <h3>Recent agent activity</h3>
          <ol>
            {activity.recent.map((event, index) => (
              <li key={`${event.at}-${index}`}>
                <time dateTime={new Date(event.at * 1000).toISOString()}>{new Date(event.at * 1000).toLocaleTimeString()}</time>
                <span>{event.label}{event.chapter ? ` · ${event.chapter}` : ""}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
