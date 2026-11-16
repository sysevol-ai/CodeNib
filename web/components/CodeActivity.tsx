// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import type { CodeActivityItem, CodeActivityOverlay } from "@/lib/api";
import {
  type ActivityGroup,
  type ActivityKind,
  activityDate,
  activityHref,
  activityReach,
  activitySources,
  activityTitle,
  codeSpans,
  pageActivity,
} from "@/lib/codeActivity";

/**
 * What is happening on the code this page explains: open issues that point at
 * it, open pull requests that change it, and the changes that last touched it.
 * Matches that reach a cited symbol lead; matches that only reach a cited file
 * are folded below them. The overlay is matched by code, not by a model.
 */
export default function CodeActivity({
  overlay,
  pageId,
}: {
  overlay: CodeActivityOverlay | null;
  pageId: string;
}) {
  const groups = pageActivity(overlay, pageId);
  if (!overlay || groups.length === 0) return null;
  const asOf = activityDate(overlay.generated_at);
  return (
    <section className="code-activity" aria-labelledby="code-activity-title">
      <h2 id="code-activity-title">Activity on this code</h2>
      <p className="code-activity-intro">
        {activitySources(groups)} that reach the code this page cites, matched by
        code{overlay.rev ? (
          <>
            {" "}
            at commit <span className="mono">{overlay.rev.slice(0, 7)}</span>
          </>
        ) : null}
        {asOf ? ` on ${asOf}` : ""}.
      </p>
      {groups.map((group) => (
        <Group key={group.kind} group={group} />
      ))}
    </section>
  );
}

function Group({ group }: { group: ActivityGroup }) {
  const nearbyLabel =
    group.kind === "issues"
      ? "point at files this page cites"
      : "edit files this page cites";
  return (
    <div className="code-activity-group">
      <h3>
        {group.label}
        <span className="code-activity-count">
          {group.direct.length + group.nearby.length}
        </span>
      </h3>
      {group.direct.length > 0 && (
        <ul>
          {group.direct.map((item) => (
            <Row key={`${item.number}:${item.commit ?? ""}`} item={item} kind={group.kind} />
          ))}
        </ul>
      )}
      {group.nearby.length > 0 &&
        (group.direct.length === 0 ? (
          <ul>
            {group.nearby.map((item) => (
              <Row key={`${item.number}:${item.commit ?? ""}`} item={item} kind={group.kind} />
            ))}
          </ul>
        ) : (
          <details className="code-activity-nearby">
            <summary>
              {group.nearby.length} more {nearbyLabel}
            </summary>
            <ul>
              {group.nearby.map((item) => (
                <Row key={`${item.number}:${item.commit ?? ""}`} item={item} kind={group.kind} />
              ))}
            </ul>
          </details>
        ))}
    </div>
  );
}

function Row({ item, kind }: { item: CodeActivityItem; kind: ActivityKind }) {
  const href = activityHref(item);
  const reach = activityReach(item, kind);
  const date = activityDate(item.at);
  const body = (
    <>
      <span className="code-activity-head">
        {item.number > 0 && <span className="code-activity-number mono">#{item.number}</span>}
        <span className="code-activity-title">
          {codeSpans(activityTitle(item)).map((part, index) =>
            part.code ? <code key={index}>{part.text}</code> : <span key={index}>{part.text}</span>,
          )}
        </span>
        {item.draft && <span className="code-activity-tag">Draft</span>}
      </span>
      <span className="code-activity-meta">
        {reach && (
          <span className={item.strength === "symbol" ? "code-activity-reach direct" : "code-activity-reach"}>
            {reach.verb} <code>{reach.name}</code>
            {reach.more ? ` ${reach.more}` : ""}
          </span>
        )}
        {date && <time dateTime={item.at}>{date}</time>}
      </span>
    </>
  );
  return (
    <li>
      {href ? (
        <a className="code-activity-row" href={href} target="_blank" rel="noreferrer">
          {body}
        </a>
      ) : (
        <div className="code-activity-row">{body}</div>
      )}
    </li>
  );
}
