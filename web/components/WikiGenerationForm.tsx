// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from "react";
import { startWiki, type WikiAttempt } from "@/lib/visitorWiki";

export default function WikiGenerationForm({
  attempt,
  onStarted,
  resume = false,
}: {
  attempt: WikiAttempt;
  onStarted: () => void;
  resume?: boolean;
}) {
  const keyInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [budget, setBudget] = useState(2);
  const [model, setModel] = useState("deepseek/deepseek-v4.1-flash");
  return (
    <form
      className="wiki-generation-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || !keyInput.current) return;
        const key = keyInput.current.value.trim();
        keyInput.current.value = "";
        if (!key) return;
        setBusy(true);
        setError("");
        try {
          await startWiki(attempt, key, budget, model);
          onStarted();
        } catch (reason) {
          setError(
            reason instanceof Error
              ? reason.message
              : "Could not start generation.",
          );
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2>{resume ? "Continue your Wiki" : "Generate your Repo Wiki"}</h2>
      <p>
        {resume
          ? "Ready chapters are reused. Only unfinished chapters need generation."
          : "Start with an Overview and a few core chapters, with source citations. Read each chapter as soon as it is ready."}
      </p>
      {!resume && <>
        <label htmlFor="wiki-generation-model">Writing model</label>
        <select id="wiki-generation-model" value={model} disabled={busy} onChange={(event) => setModel(event.target.value)}>
          <option value="deepseek/deepseek-v4.1-flash">DeepSeek V4.1 Flash · try the faster option</option>
          <option value="anthropic/claude-sonnet-4.6">Claude Sonnet 4.6</option>
        </select>
        <p className="small muted">Flash offers lower-cost generation. Speed varies by OpenRouter provider.</p>
      </>}
      <label htmlFor="wiki-generation-key">OpenRouter inference key</label>
      <input
        id="wiki-generation-key"
        ref={keyInput}
        type="password"
        autoComplete="off"
        spellCheck={false}
        required
        maxLength={1024}
        disabled={busy}
        placeholder="sk-or-…"
      />
      <p className="small muted">
        Create a key with a credit limit in{" "}
        <a
          href="https://openrouter.ai/settings/keys"
          target="_blank"
          rel="noopener noreferrer"
        >
          OpenRouter settings ↗
        </a>
        .
      </p>
      <label htmlFor="wiki-generation-budget">Budget for this run</label>
      <select
        id="wiki-generation-budget"
        value={budget}
        disabled={busy}
        onChange={(event) => setBudget(Number(event.target.value))}
      >
        <option value={1}>$1</option>
        <option value={2}>$2</option>
        <option value={5}>$5</option>
      </select>
      <p className="small muted">
        Stops new calls when reported spending reaches this amount. A call
        already in progress can exceed it. Set a credit limit on your
        OpenRouter key; include BYOK usage if you also use your own provider
        keys through OpenRouter.
      </p>
      <label className="wiki-consent">
        <input type="checkbox" required disabled={busy} />
        <span>
          I authorize generation using my OpenRouter account. CodeNib keeps my
          key in server memory only while this run is active, sends public
          source to OpenRouter and its model providers, and saves the Wiki for
          anyone with its link. The key is never saved.
        </span>
      </label>
      <button type="submit" className="btn-primary" disabled={busy}>
        {busy
          ? "Starting generation…"
          : resume
            ? "Resume unfinished chapters"
            : "Generate Wiki"}
      </button>
      {busy && <div className="wiki-starting" role="status">
        <span className="wiki-run-pulse is-animated" aria-hidden="true"><i /><i /><i /></span>
        <div><strong>Checking your account and repository…</strong><p>Your Wiki opens when the server accepts this run.</p></div>
      </div>}
      {error && (
        <p role="alert" className="trial-error">
          {error}{" "}
          <button type="button" className="btn-ghost" onClick={onStarted}>
            Check saved attempt
          </button>
        </p>
      )}
    </form>
  );
}
