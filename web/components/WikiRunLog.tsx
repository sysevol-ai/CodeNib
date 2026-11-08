// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useState, type CSSProperties, type ReactNode } from "react";

import { mebibytes } from "@/components/WikiRepositoryRules";
import { withBasePath } from "@/lib/runtime";
import { wikiActivity } from "@/lib/wikiActivity";
import { NEUTRAL_COLOR } from "@/lib/wikiGraph";
import { clock, runLead, runStarted, runSteps, runTitle, type RunStep } from "@/lib/wikiRun";
import { wikiModelLabel, type SavedWiki, type WikiModelCatalog } from "@/lib/visitorWiki";
import "./WikiRun.css";

const timeOfDay = (at: number) =>
  new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

/** Setup steps finish with a tick; a finished chapter is a filled dot, as on the map. */
function StepMark({ state, chapter }: { state: RunStep["state"]; chapter: boolean }) {
  if (state === "done" && !chapter)
    return (
      <svg className="run-mark" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M4.5 8.4 7 10.8l4.6-5.3" />
      </svg>
    );
  return <span className="run-mark" aria-hidden="true" />;
}

/**
 * The run in the reader's column: what is happening now, every step so far
 * with the facts the server reported, and what comes next. Chapters carry
 * their colour on the map, so a line here and a node there read as one thing.
 */
export default function WikiRunLog({
  wiki,
  now,
  connected,
  colors,
  catalog,
  onPick,
  actions,
  children,
  heading = true,
}: {
  wiki: SavedWiki;
  now: number;
  connected: boolean;
  colors: Map<string, string>;
  catalog: WikiModelCatalog | null;
  onPick: (pageId: string) => void;
  /** Owner controls beside the headline, such as stopping the run. */
  actions?: ReactNode;
  /** Shown under the steps, such as the form that resumes a stopped run. */
  children?: ReactNode;
  heading?: boolean;
}) {
  const [copied, setCopied] = useState("");
  const activity = wikiActivity(wiki, now, connected);
  const steps = runSteps(wiki, now);
  const total = steps.filter((step) => step.pageId).length;
  const ready = steps.filter((step) => step.pageId && step.state === "done").length;
  const running = activity.running;
  const started = runStarted(wiki);
  const link = new URL(withBasePath(`/wiki/${wiki.id}`), window.location.origin).href;
  const streaming = running && wiki.request_active && connected && !activity.delayed;
  // Nothing turns while the server is silent or unreachable: motion means progress.
  const still = running && !activity.animate;

  return (
    <section className={`run-log${running ? " is-running" : ""}${still ? " is-still" : ""}`} aria-label="Wiki generation">
      {heading && (
        <header className="run-head">
          <div>
            <h1 aria-live="polite">{runTitle(wiki, ready)}</h1>
            <p className="run-lead">{runLead(wiki, ready)}</p>
          </div>
          {actions && <div className="run-actions">{actions}</div>}
        </header>
      )}
      {!heading && actions && <div className="run-actions is-inline">{actions}</div>}
      <dl className="run-facts">
        <div>
          <dt>Chapters ready</dt>
          <dd>{total ? `${ready} of ${total}` : "Not planned yet"}</dd>
        </div>
        <div>
          <dt>{running ? "Running for" : "Ran for"}</dt>
          <dd>{clock((running ? now : wiki.updated_at) - started)}</dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>{wikiModelLabel(wiki.model ?? "anthropic/claude-sonnet-4.6", catalog)}</dd>
        </div>
        <div>
          <dt>Reported spend</dt>
          <dd>
            ${wiki.reported_cost_usd.toFixed(4)} <span>of ${wiki.budget_usd.toFixed(2)}</span>
            <small>
              {wiki.calls.toLocaleString()} model {wiki.calls === 1 ? "request" : "requests"}
            </small>
          </dd>
        </div>
      </dl>

      {running && activity.tone === "waiting" && (
        <p className="run-notice" role="status">
          <strong>{activity.label}.</strong> {activity.explanation}
        </p>
      )}

      <ol className="run-steps">
        {steps.map((step) => {
          const color = step.pageId ? colors.get(step.pageId) ?? NEUTRAL_COLOR : undefined;
          const current = step.state === "current";
          return (
            <li
              key={step.id}
              className={`run-step is-${step.state}${step.pageId ? " is-chapter" : ""}`}
              style={color ? ({ "--step": color } as CSSProperties) : undefined}
              aria-current={current ? "step" : undefined}
            >
              <StepMark state={step.state} chapter={!!step.pageId} />
              <time dateTime={step.at ? new Date(step.at * 1000).toISOString() : undefined}>
                {step.at ? timeOfDay(step.at) : ""}
              </time>
              <div className="run-step-body">
                {step.pageId && step.state !== "next" ? (
                  <button type="button" className="run-step-title" onClick={() => onPick(step.pageId!)}>
                    {step.label}
                  </button>
                ) : (
                  <span className="run-step-title">{step.label}</span>
                )}
                {(step.detail || step.seconds !== undefined) && (
                  <span className="run-step-detail">
                    {step.detail}
                    {step.detail && step.seconds !== undefined ? " · " : ""}
                    {step.seconds !== undefined && (step.state === "done" ? `written in ${clock(step.seconds)}` : clock(step.seconds))}
                  </span>
                )}
                {current && streaming && (
                  <span className="run-stream">
                    {(wiki.response_chars || 0) > 0
                      ? `Receiving the model’s response · ${(wiki.response_chars || 0).toLocaleString()} characters`
                      : "Waiting for the model to respond"}
                    <i className="run-caret" aria-hidden="true" />
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ol>

      {!!wiki.skipped_files?.length && (
        <details className="run-more">
          <summary>
            {wiki.skipped_files.length} {wiki.skipped_files.length === 1 ? "file" : "files"} over 4 MiB left out
          </summary>
          <p>These files are excluded from this Wiki. The remaining source is used for generation.</p>
          <ul>
            {wiki.skipped_files.map((file) => (
              <li key={file.path}>
                <code>{file.path}</code> {mebibytes(file.size_bytes)}
              </li>
            ))}
          </ul>
        </details>
      )}
      {wiki.unreported_call_cost && !running && (
        <p className="run-note">
          The reported total excludes an in-flight or unreported request. Check OpenRouter usage before resuming.
        </p>
      )}

      {children}

      {running && (
        <footer className="run-foot">
          <p>
            Generation runs on the server, so you can close this tab. Chapters are saved as they pass, and this link
            brings you back:
          </p>
          <div className="run-link">
            <code>{link.replace(/^https?:\/\//, "")}</code>
            <button
              type="button"
              className="btn-ghost"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(link);
                  setCopied("Copied");
                } catch {
                  setCopied("Copy it from the address bar");
                }
              }}
            >
              {copied || "Copy link"}
            </button>
          </div>
        </footer>
      )}
    </section>
  );
}
