// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";
import AgentSetup from "./AgentSetup";
import { fetchRepos } from "@/lib/api";
import { AppLink } from "@/lib/router";
import { assetUrl } from "@/lib/runtime";
import {
  loadTrialContext,
  OpenRouterAuthorizationError,
  OpenRouterTrialSession,
  type TrialCandidate,
  type TrialContext,
  type TrialUsage,
} from "@/lib/openrouterTrial";

function RevocationNotice({ urls }: { urls: string[] }) {
  if (!urls.length) return null;
  return (
    <div className="trial-error" role="alert">
      <p>
        An earlier authorization may have created a key that could not be
        verified for this session. Disconnecting here does not revoke it.
      </p>
      {urls.map((url) => (
        <p key={url}>
          <a href={url} target="_blank" rel="noopener noreferrer">
            Review or revoke the unused key on OpenRouter
          </a>
        </p>
      ))}
    </div>
  );
}

export default function OpenRouterTrial({
  base,
  repoId,
  query,
}: {
  base: string;
  repoId: string;
  query: string;
}) {
  const session = useRef(new OpenRouterTrialSession());
  const channel = useRef<BroadcastChannel | null>(null);
  const authTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const generation = useRef(0);
  const [context, setContext] = useState<TrialContext | null>(null);
  const [question, setQuestion] = useState(query.slice(0, 16000));
  const [connection, setConnection] = useState<
    "off" | "authorizing" | "connected"
  >("off");
  const [account, setAccount] = useState<{
    settingsUrl: string;
    remaining: number | null;
  } | null>(null);
  const [authorizationUrl, setAuthorizationUrl] = useState("");
  const [revocationUrls, setRevocationUrls] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState<TrialCandidate[] | null>(null);
  const [usage, setUsage] = useState<TrialUsage>({
    reportedCost: 0,
    unknownCost: false,
    calls: [],
  });
  const [local, setLocal] = useState(false);

  function closeAuthorization() {
    channel.current?.close();
    channel.current = null;
    if (authTimer.current !== null) clearTimeout(authTimer.current);
    authTimer.current = null;
  }

  function disconnect() {
    generation.current += 1;
    session.current.disconnect();
    closeAuthorization();
    setConnection("off");
    setAuthorizationUrl("");
    setBusy(false);
    setResults(null);
    setUsage(session.current.usage());
  }

  useEffect(() => {
    const controller = new AbortController();
    const current = ++generation.current;
    session.current.disconnect();
    closeAuthorization();
    setContext(null);
    setConnection("off");
    setAccount(null);
    setError("");
    setAuthorizationUrl("");
    setResults(null);
    setBusy(false);
    setQuestion(query.slice(0, 16000));
    fetchRepos({ signal: controller.signal })
      .then(async (repos) => {
        const repo = repos.find((item) => item.id === repoId);
        if (!repo)
          throw new Error("This repository is not in the public preview.");
        const published = await loadTrialContext(
          base,
          repoId,
          repo.repo,
          repo.base_commit,
          controller.signal,
        );
        if (generation.current === current) setContext(published);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted && generation.current === current)
          setError(
            reason instanceof Error
              ? reason.message
              : "The public source service is unavailable.",
          );
      });
    const expiry = setInterval(() => {
      if (session.current.connected()) return;
      setConnection((value) => (value === "connected" ? "off" : value));
    }, 1000);
    return () => {
      controller.abort();
      clearInterval(expiry);
      session.current.disconnect();
      closeAuthorization();
      generation.current += 1;
    };
  }, [base, repoId, query]);

  async function connect() {
    setError("");
    setAccount(null);
    setResults(null);
    const current = ++generation.current;
    closeAuthorization();
    setConnection("authorizing");
    try {
      if (typeof BroadcastChannel === "undefined" || !window.isSecureContext)
        throw new Error(
          "This browser cannot open a secure trial session. Use local agent setup.",
        );
      const attempt = await session.current.beginLogin(
        new URL(assetUrl("/openrouter-callback.html"), window.location.origin)
          .href,
      );
      if (generation.current !== current) return;
      const receiver = new BroadcastChannel(
        `codenib-openrouter:${attempt.nonce}`,
      );
      channel.current = receiver;
      receiver.onmessage = async (event: MessageEvent) => {
        const data = event.data;
        if (
          !data ||
          data.nonce !== attempt.nonce ||
          generation.current !== current
        )
          return;
        if (data.error !== true && typeof data.code !== "string") return;
        receiver.postMessage({ nonce: attempt.nonce, received: true });
        closeAuthorization();
        setAuthorizationUrl("");
        try {
          if (data.error === true)
            throw new Error("OpenRouter authorization was declined.");
          const connected = await session.current.acceptGrant(
            attempt.nonce,
            data.code,
          );
          if (generation.current !== current) return;
          setAccount(connected);
          setConnection("connected");
          setUsage(session.current.usage());
        } catch (reason) {
          // Keep revocation guidance even if the user cancelled while key
          // metadata was in flight. It contains only a safe provider URL.
          if (reason instanceof OpenRouterAuthorizationError)
            setRevocationUrls((urls) => [
              ...new Set([...urls, reason.settingsUrl]),
            ]);
          if (generation.current !== current) return;
          session.current.disconnect();
          setConnection("off");
          setError(
            reason instanceof Error
              ? reason.message
              : "Authorization failed. Connect again.",
          );
        }
      };
      authTimer.current = setTimeout(() => {
        if (generation.current !== current) return;
        disconnect();
        setError("Authorization expired. Connect again.");
      }, 300000);
      setAuthorizationUrl(attempt.url);
      // A nonce-scoped BroadcastChannel permits noopener even through provider
      // redirects. Popup blocking leaves the explicit authorization link below.
      window.open(attempt.url, "_blank", "noopener,noreferrer");
    } catch (reason) {
      if (generation.current !== current) return;
      disconnect();
      setError(
        reason instanceof Error ? reason.message : "Authorization failed.",
      );
    }
  }

  async function retrieve(event: React.FormEvent) {
    event.preventDefault();
    if (!context || busy) return;
    const current = generation.current;
    setBusy(true);
    setError("");
    setResults(null);
    try {
      const found = await session.current.retrieve(context, question.trim());
      if (generation.current === current) setResults(found);
    } catch (reason) {
      if (generation.current === current)
        setError(
          reason instanceof Error
            ? reason.message
            : "The query could not finish.",
        );
    } finally {
      if (generation.current === current) {
        setBusy(false);
        setUsage(session.current.usage());
      }
    }
  }

  if (local)
    return (
      <>
        <RevocationNotice urls={revocationUrls} />
        <AgentSetup repoId={repoId} query={question} />
      </>
    );
  return (
    <section
      className="agent-setup openrouter-trial"
      aria-labelledby="trial-title"
    >
      <AppLink href={`/${encodeURIComponent(repoId)}`}>
        ← Browse the Wiki
      </AppLink>
      <p className="preview-label">Public repository trial</p>
      <h1 id="trial-title">Find the source. Use your OpenRouter account.</h1>
      <p>
        Ask about this public example. CodeNib runs bounded grep; your browser
        asks OpenRouter to plan the search and Jev to rank the source.
      </p>
      <p className="small muted">
        Your question and selected public source snippets go to OpenRouter and
        its model providers. Your key stays in this tab’s memory for up to 10
        minutes and is sent only to OpenRouter. Reloading or disconnecting
        removes it from this tab.
      </p>
      <div className="trial-actions">
        {connection === "off" && (
          <button
            className="trial-primary"
            type="button"
            disabled={!context || busy}
            onClick={connect}
          >
            Connect OpenRouter
          </button>
        )}
        {connection !== "off" && (
          <button type="button" onClick={disconnect}>
            {connection === "authorizing"
              ? "Cancel authorization"
              : "Disconnect"}
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            disconnect();
            setLocal(true);
          }}
        >
          Use with your local agent
        </button>
      </div>
      {authorizationUrl && (
        <p role="status">
          Complete authorization in the OpenRouter tab.{" "}
          <a href={authorizationUrl} target="_blank" rel="noopener noreferrer">
            Open authorization page
          </a>
        </p>
      )}
      {connection === "connected" && (
        <p role="status">
          OpenRouter connected. A query starts only when you select “Find code”.
        </p>
      )}
      {context && (
        <p className="small muted">
          {context.repository} · {context.commit.slice(0, 8)} ·{" "}
          {context.protocol.planner_model} + {context.protocol.reranker_model}
        </p>
      )}
      {!context && !error && (
        <p role="status">Checking the published source…</p>
      )}
      <form onSubmit={retrieve} className="trial-form">
        <label htmlFor="trial-question">What do you want to find?</label>
        <textarea
          id="trial-question"
          rows={4}
          maxLength={16000}
          value={question}
          disabled={busy}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Where does Requests strip Authorization during redirects?"
        />
        <div className="trial-actions">
          <button
            className="trial-primary"
            type="submit"
            disabled={connection !== "connected" || busy || !question.trim()}
          >
            {busy ? "Finding source…" : "Find code"}
          </button>
          {busy && (
            <button type="button" onClick={() => session.current.cancelQuery()}>
              Cancel query
            </button>
          )}
        </div>
      </form>
      <p className="small muted">
        Reported-cost stop: $0.10 per query, $0.50 per connection. An in-flight
        call can exceed this; set an OpenRouter key credit limit for a billing
        cap. Calls are never retried automatically.
      </p>
      {account && (
        <p className="small">
          <a
            href={account.settingsUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            Key limits, usage and revocation on OpenRouter
          </a>
          {account.remaining !== null &&
            ` · $${account.remaining.toFixed(2)} remaining when connected`}
          . Disconnecting here does not revoke the provider key.
        </p>
      )}
      <RevocationNotice urls={revocationUrls} />
      {usage.calls.length > 0 && (
        <p className="small" aria-live="polite">
          ${usage.reportedCost.toFixed(6)} reported · {usage.calls.length} model
          calls in this connection
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
      {results && (
        <div className="trial-results" aria-live="polite">
          <h2>
            {results.length ? "Source to inspect" : "No matching source found"}
          </h2>
          <p className="small muted">
            {results.length
              ? "Ranked source context, ready to inspect or use with your coding agent."
              : "Try a concrete identifier or behavior. No broader search or model fallback was started."}
          </p>
          {results.map((result) => (
            <article className="agent-question" key={result.id}>
              <a href={result.url} target="_blank" rel="noopener noreferrer">
                {result.file}:{result.startLine}–{result.endLine}
              </a>
              <p className="small muted">
                {result.name} · relevance {result.score.toFixed(2)}
              </p>
              <pre tabIndex={0} aria-label={`Source from ${result.file}`}>
                <code>{result.source}</code>
              </pre>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
