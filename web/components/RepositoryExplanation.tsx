// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";
import { OpenRouterTrialSession } from "@/lib/openrouterTrial";
import {
  previewReadme,
  previewSources,
  readPreviewFile,
  type RepositoryPreview,
} from "@/lib/githubPreview";
import {
  overviewInput,
  type OverviewInput,
  type OverviewCitation,
  type RepositoryOverview,
} from "@/lib/repositoryOverview";

export default function RepositoryExplanation({
  snapshot,
}: {
  snapshot: RepositoryPreview;
}) {
  const session = useRef(new OpenRouterTrialSession());
  const input = useRef<HTMLInputElement>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const resultPanel = useRef<HTMLDivElement>(null);
  const [connected, setConnected] = useState(false);
  const [settings, setSettings] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<{
    overview: RepositoryOverview;
    input: OverviewInput;
  } | null>(null);
  const [usage, setUsage] = useState(session.current.usage());

  function disconnect() {
    ++generation.current;
    controller.current?.abort();
    session.current.disconnect();
    if (input.current) input.current.value = "";
    setConnected(false);
    setBusy("");
    setUsage(session.current.usage());
  }
  useEffect(() => {
    const expiry = setInterval(
      () => setConnected(session.current.connected()),
      1000,
    );
    return () => {
      ++generation.current;
      clearInterval(expiry);
      controller.current?.abort();
      session.current.disconnect();
    };
  }, []);

  useEffect(() => {
    if (result)
      resultPanel.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
  }, [result]);

  async function connect(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !input.current) return;
    const key = input.current.value;
    input.current.value = ""; // Never put a credential in React state or storage.
    const current = ++generation.current;
    setBusy("Connecting…");
    setError("");
    try {
      const account = await session.current.useExistingKey(key);
      if (generation.current !== current) return;
      setSettings(account.settingsUrl);
      setConnected(true);
      setUsage(session.current.usage());
    } catch (reason) {
      if (generation.current === current)
        setError(
          reason instanceof Error
            ? reason.message
            : "Could not connect OpenRouter.",
        );
    } finally {
      if (generation.current === current) setBusy("");
    }
  }

  async function explain() {
    if (busy) return;
    const current = generation.current;
    const abort = new AbortController();
    controller.current = abort;
    const timeout = setTimeout(() => abort.abort(), 30000);
    setBusy("Reading source…");
    setError("");
    try {
      const readme = previewReadme(snapshot.entries);
      const entries = [
        ...(readme ? [readme] : []),
        ...previewSources(snapshot.entries),
      ];
      const files = await Promise.all(
        entries.map((entry) => readPreviewFile(snapshot, entry, abort.signal)),
      );
      if (abort.signal.aborted || generation.current !== current) return;
      const context = overviewInput(snapshot, files);
      setBusy("Explaining the repository…");
      clearTimeout(timeout);
      const overview = await session.current.explainRepository(context);
      if (!abort.signal.aborted && generation.current === current)
        setResult({ overview, input: context });
    } catch (reason) {
      if (generation.current === current)
        setError(
          abort.signal.aborted
            ? "Explanation cancelled. Check reported usage before retrying."
            : reason instanceof Error
              ? reason.message
              : "Could not explain this repository.",
        );
    } finally {
      // If one parallel source read failed, stop the remaining reads too.
      abort.abort();
      clearTimeout(timeout);
      if (controller.current === abort) controller.current = null;
      if (generation.current === current) {
        setBusy("");
        setUsage(session.current.usage());
      }
    }
  }

  function citation(evidence: OverviewCitation) {
    const source = result!.input.sources[evidence.source];
    return (
      <a
        className="overview-citation"
        href={`${source.url}#L${evidence.start_line}-L${evidence.end_line}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        {source.path}:{evidence.start_line}–{evidence.end_line}
      </a>
    );
  }

  return (
    <section
      className="repository-explanation"
      aria-labelledby="explanation-title"
    >
      <h2 id="explanation-title">How does this repository work?</h2>
      <p>
        Get a short explanation and a component map, with links to the source.
      </p>
      <details open={connected || Boolean(busy) || Boolean(error) || undefined}>
        <summary>Use your OpenRouter key for an AI explanation</summary>
        <p className="small muted">
          Your browser sends up to five public source excerpts to OpenRouter and
          its model providers. Your key goes only to OpenRouter and stays in
          this tab’s memory for up to 10 minutes. Reloading, leaving this page
          or disconnecting clears it.
        </p>
        {!connected && (
          <form className="overview-connect" onSubmit={connect}>
            <label htmlFor="overview-key">OpenRouter inference key</label>
            <div className="repository-entry-row">
              <input
                id="overview-key"
                ref={input}
                type="password"
                autoComplete="off"
                spellCheck={false}
                maxLength={1024}
                required
                disabled={Boolean(busy)}
                placeholder="sk-or-…"
              />
              <button type="submit" disabled={Boolean(busy)}>
                Connect key
              </button>
            </div>
            <a
              className="small"
              href="https://openrouter.ai/settings/keys"
              target="_blank"
              rel="noopener noreferrer"
            >
              Create a key with a credit limit
            </a>
          </form>
        )}
        <div className="trial-actions">
          {connected && (
            <button
              type="button"
              className="trial-primary"
              disabled={Boolean(busy) || usage.unknownCost}
              onClick={explain}
            >
              {busy ||
                (result ? "Regenerate explanation" : "Explain repository")}
            </button>
          )}
          {(connected || busy) && (
            <button type="button" onClick={disconnect}>
              Disconnect
            </button>
          )}
          {busy && (
            <button
              type="button"
              onClick={() => {
                controller.current?.abort();
                session.current.cancelQuery();
              }}
            >
              Cancel
            </button>
          )}
        </div>
        {busy && <p role="status">{busy}</p>}
        <p className="small muted">
          One Claude Sonnet 4.6 call per explanation; no automatic retries.
          Reported-cost stop: $0.50 per connection. An in-flight call can exceed
          that; set your key’s credit limit on OpenRouter for a billing cap.
        </p>
        {settings && (
          <p className="small">
            <a href={settings} target="_blank" rel="noopener noreferrer">
              Manage key limits or revoke on OpenRouter
            </a>
            . Disconnecting here does not revoke the key.
          </p>
        )}
        {usage.calls.length > 0 && (
          <p className="small" aria-live="polite">
            ${usage.reportedCost.toFixed(6)} reported · {usage.calls.length}{" "}
            model calls in this connection
          </p>
        )}
        {usage.unknownCost && (
          <p className="trial-error">
            The last call may have a charge that was not reported. Check
            OpenRouter before reconnecting.
          </p>
        )}
        {error && (
          <p className="trial-error" role="alert">
            {error}
          </p>
        )}
      </details>
      {result && (
        <div className="overview-result" ref={resultPanel} aria-live="polite">
          <p>{result.overview.summary}</p>
          <p className="small muted">
            AI explanation from {result.input.sources.length} sampled files at{" "}
            {snapshot.commit.slice(0, 8)}. Relationships are inferred from these
            excerpts.
          </p>
          <div className="overview-components">
            {result.overview.components.map((component, i) => (
              <article key={i}>
                <h3>
                  <span className="component-number">{i + 1}</span>
                  {component.title}
                </h3>
                <p>{component.description}</p>
                {citation(component.evidence)}
              </article>
            ))}
          </div>
          {result.overview.connections.length > 0 && (
            <ul className="overview-connections">
              {result.overview.connections.map((link, i) => (
                <li key={i}>
                  <strong>
                    {result.overview.components[link.from].title} →{" "}
                    {result.overview.components[link.to].title}
                  </strong>
                  <p>{link.label}</p>
                  {citation(link.evidence)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
