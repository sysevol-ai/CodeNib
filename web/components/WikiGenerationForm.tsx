// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";
import {
  FALLBACK_WIKI_MODELS,
  checkWikiRepository,
  loadWikiModels,
  startWiki,
  type WikiAttempt,
  type WikiModelCatalog,
  type WikiModelChoice,
  type WikiRepositoryCheck,
} from "@/lib/visitorWiki";
import { recordExperience } from "@/lib/experience";
import WikiRepositoryRules, { mebibytes } from "./WikiRepositoryRules";

const usd = (value: number) => `$${value.toFixed(2).replace(/\.00$/, "")}`;

function contextLabel(tokens: number) {
  return tokens >= 1_000_000
    ? `${Math.round(tokens / 100_000) / 10}M`
    : `${Math.round(tokens / 1000)}K`;
}

/** Choices grouped by the company that makes the model, in listed order. */
function byProvider(models: WikiModelChoice[]) {
  const groups = new Map<string, WikiModelChoice[]>();
  for (const model of models) {
    groups.set(model.provider, [...(groups.get(model.provider) ?? []), model]);
  }
  return [...groups.entries()];
}

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
  const [catalog, setCatalog] = useState<WikiModelCatalog>(FALLBACK_WIKI_MODELS);
  const [model, setModel] = useState(FALLBACK_WIKI_MODELS.default);
  const [eligibility, setEligibility] = useState<WikiRepositoryCheck | null>(null);
  const [checkError, setCheckError] = useState("");
  const [checkRetry, setCheckRetry] = useState(0);
  const admitted = resume || eligibility?.eligible === true;
  useEffect(() => {
    if (resume) return;
    let active = true;
    setEligibility(null);
    setCheckError("");
    checkWikiRepository(attempt.repository)
      .then((result) => { if (active) setEligibility(result); })
      .catch((reason) => {
        if (active) setCheckError(reason instanceof Error ? reason.message : "Could not check this repository.");
      });
    return () => { active = false; };
  }, [attempt.repository, resume, checkRetry]);
  useEffect(() => { recordExperience("generation_form_view"); }, []);
  useEffect(() => {
    let cancelled = false;
    loadWikiModels()
      .then((value) => {
        if (cancelled || value.models.length === 0) return;
        setCatalog(value);
        setModel((current) =>
          value.models.some((choice) => choice.id === current) ? current : value.default,
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  const chosen = catalog.models.find((choice) => choice.id === model);
  return (
    <form
      className="wiki-generation-form"
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || !admitted || !keyInput.current) return;
        const key = keyInput.current.value.trim();
        keyInput.current.value = "";
        if (!key) return;
        setBusy(true);
        setError("");
        try {
          await startWiki(attempt, key, budget, model);
          recordExperience("generation_start");
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
      {!resume && (
        <p className="small muted">
          Your Wiki will appear in My Wikis. When it is ready, you can choose
          Publish to Community to share it with other readers.
        </p>
      )}
      <WikiRepositoryRules limits={eligibility?.limits} />
      {!resume && (
        <div className="wiki-repository-check" role={eligibility?.eligible === false || checkError ? "alert" : "status"}>
          {!eligibility && !checkError ? (
            <p>Checking the repository size and source languages… No API key or model call is needed.</p>
          ) : (
            <>
              <p>{checkError || eligibility?.message}</p>
              {eligibility?.eligible && <p className="small muted">Checked {eligibility.source_files?.toLocaleString()} source files at <code>{eligibility.commit?.slice(0, 8)}</code>. No model call was made.</p>}
              {!!eligibility?.skipped_files?.length && (
                <details>
                  <summary>{eligibility.skipped_files.length} oversized {eligibility.skipped_files.length === 1 ? "file will" : "files will"} be skipped</summary>
                  <ul>{eligibility.skipped_files.map((file) => <li key={file.path}><code>{file.path}</code> ({mebibytes(file.size_bytes)})</li>)}</ul>
                </details>
              )}
              {(checkError || eligibility?.eligible === false) && (
                <p><a href="https://docs.codenib.ai/web_demo/" target="_blank" rel="noopener noreferrer">Run CodeNib locally ↗</a>{" "}<button type="button" className="btn-ghost" onClick={() => setCheckRetry((value) => value + 1)}>Check again</button></p>
              )}
            </>
          )}
        </div>
      )}
      {!resume && (
        <>
          <label htmlFor="wiki-generation-model">Writing model</label>
          <select
            id="wiki-generation-model"
            value={model}
            disabled={busy}
            onChange={(event) => setModel(event.target.value)}
          >
            {byProvider(catalog.models).map(([provider, models]) => (
              <optgroup key={provider} label={provider}>
                {models.map((choice) => (
                  <option key={choice.id} value={choice.id}>
                    {choice.label} · {usd(choice.input_usd)} in / {usd(choice.output_usd)} out
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          {chosen && (
            <p className="small muted wiki-model-note">
              {usd(chosen.input_usd)} per million input tokens and{" "}
              {usd(chosen.output_usd)} per million output tokens,{" "}
              {contextLabel(chosen.context)} context
              {catalog.prices === "live" ? ", current OpenRouter prices" : ""}.{" "}
              {chosen.tested
                ? "Complete Wikis have been generated with this model."
                : "Not yet tested with CodeNib. If a chapter fails, the run stops and finished chapters are kept."}
            </p>
          )}
        </>
      )}
      <label htmlFor="wiki-generation-key">OpenRouter inference key</label>
      <input
        id="wiki-generation-key"
        ref={keyInput}
        type="password"
        autoComplete="off"
        spellCheck={false}
        required
        maxLength={1024}
        disabled={busy || !admitted}
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
        already in progress can exceed it. Set a credit limit on your OpenRouter
        key; include BYOK usage if you also use your own provider keys through
        OpenRouter.
      </p>
      <label className="wiki-consent">
        <input type="checkbox" required disabled={busy || !admitted} />
        <span>
          I authorize generation using my OpenRouter account. CodeNib keeps my
          key in server memory only while this run is active, sends public
          source to OpenRouter and its model providers, and saves the Wiki for
          anyone with its link. The key is never saved.
        </span>
      </label>
      <button type="submit" className="btn-primary" disabled={busy || !admitted}>
        {busy
          ? "Starting generation…"
          : resume
            ? "Resume unfinished chapters"
            : "Generate Wiki"}
      </button>
      {busy && (
        <div className="wiki-starting" role="status">
          <span className="wiki-run-pulse is-animated" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <div>
            <strong>Checking your account and repository…</strong>
            <p>Your Wiki opens when the server accepts this run.</p>
          </div>
        </div>
      )}
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
