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
  WikiRequestError,
} from "@/lib/visitorWiki";
import { recordExperience } from "@/lib/experience";
import WikiRepositoryRules, { mebibytes } from "./WikiRepositoryRules";

const usd = (value: number) => `$${value.toFixed(2).replace(/\.00$/, "")}`;
const BUDGETS = [1, 2, 5];

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
  onChecked,
  resume = false,
}: {
  attempt: WikiAttempt;
  onStarted: () => void;
  /** The source check's result, so the page can draw the snapshot. */
  onChecked?: (result: WikiRepositoryCheck | null) => void;
  resume?: boolean;
}) {
  const keyInput = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Only an unanswered request may have been accepted; a refusal was not.
  const [outcomeUnknown, setOutcomeUnknown] = useState(false);
  const [retryAt, setRetryAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [budget, setBudget] = useState(2);
  const [catalog, setCatalog] = useState<WikiModelCatalog>(FALLBACK_WIKI_MODELS);
  const [model, setModel] = useState(FALLBACK_WIKI_MODELS.default);
  const [eligibility, setEligibility] = useState<WikiRepositoryCheck | null>(null);
  const [checkError, setCheckError] = useState("");
  const [checkLimited, setCheckLimited] = useState(false);
  const [checkRetry, setCheckRetry] = useState(0);
  const admitted = resume || eligibility?.eligible === true;
  const checked = useRef(onChecked);
  checked.current = onChecked;
  useEffect(() => {
    if (resume) return;
    let active = true;
    setEligibility(null);
    setCheckError("");
    setCheckLimited(false);
    checked.current?.(null);
    checkWikiRepository(attempt.repository)
      .then((result) => {
        if (!active) return;
        setEligibility(result);
        checked.current?.(result);
      })
      .catch((reason) => {
        if (!active) return;
        setCheckError(reason instanceof Error ? reason.message : "Could not check this repository.");
        setCheckLimited(reason instanceof WikiRequestError && reason.status === 429);
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
  // Count down to the moment the server accepts another try.
  useEffect(() => {
    if (retryAt === null) return;
    const timer = setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= retryAt) {
        setRetryAt(null);
        setError("");
      }
    }, 250);
    return () => clearInterval(timer);
  }, [retryAt]);
  const waitSeconds = retryAt === null ? 0 : Math.max(1, Math.ceil((retryAt - now) / 1000));
  const chosen = catalog.models.find((choice) => choice.id === model);
  const rejected = !!checkError || eligibility?.eligible === false;
  return (
    <form
      className={`wiki-generation-form${resume ? " is-resume" : ""}`}
      onSubmit={async (event) => {
        event.preventDefault();
        if (busy || !admitted || retryAt !== null || !keyInput.current) return;
        const key = keyInput.current.value.trim();
        keyInput.current.value = "";
        if (!key) return;
        setBusy(true);
        setError("");
        setOutcomeUnknown(false);
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
          const refused = reason instanceof WikiRequestError;
          setOutcomeUnknown(!refused);
          if (refused && reason.retryAfter) {
            setNow(Date.now());
            setRetryAt(Date.now() + reason.retryAfter * 1000);
          }
        } finally {
          setBusy(false);
        }
      }}
    >
      {resume && (
        <div className="wgf-head">
          <h2>Continue your Wiki</h2>
          <p>Ready chapters are reused. Only unfinished chapters need generation.</p>
        </div>
      )}
      {!resume && (
        <div
          className={`wgf-check${rejected ? " is-rejected" : eligibility ? " is-admitted" : " is-checking"}`}
          role={rejected ? "alert" : "status"}
        >
          <span className="wgf-check-mark" aria-hidden="true" />
          <div>
            {!eligibility && !checkError ? (
              <p>Checking the repository size and source languages… No API key or model call is needed.</p>
            ) : (
              <>
                <p>{checkError || eligibility?.message}</p>
                {eligibility?.eligible && (
                  <p className="wgf-quiet">
                    Checked {eligibility.source_files?.toLocaleString()} files at{" "}
                    <code>{eligibility.commit?.slice(0, 8)}</code>
                    {eligibility.source_tree
                      ? `, ${eligibility.source_tree.files.toLocaleString()} of them in a supported language`
                      : ""}
                    . No model call was made.
                  </p>
                )}
                {!!eligibility?.skipped_files?.length && (
                  <details>
                    <summary>
                      {eligibility.skipped_files.length} oversized{" "}
                      {eligibility.skipped_files.length === 1 ? "file will" : "files will"} be skipped
                    </summary>
                    <ul>
                      {eligibility.skipped_files.map((file) => (
                        <li key={file.path}>
                          <code>{file.path}</code> ({mebibytes(file.size_bytes)})
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {rejected && (
                  <p className="wgf-check-actions">
                    {!checkLimited && (
                      <a href="https://docs.codenib.ai/web_demo/" target="_blank" rel="noopener noreferrer">
                        Run CodeNib locally ↗
                      </a>
                    )}
                    <button type="button" className="btn-ghost" onClick={() => setCheckRetry((value) => value + 1)}>
                      Check again
                    </button>
                  </p>
                )}
              </>
            )}
          </div>
        </div>
      )}
      {!resume && (
        <div className="wgf-field">
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
            <p className="wgf-note">
              {usd(chosen.input_usd)} per million input tokens and{" "}
              {usd(chosen.output_usd)} per million output tokens,{" "}
              {contextLabel(chosen.context)} context
              {catalog.prices === "live" ? ", current OpenRouter prices" : ""}.{" "}
              {chosen.tested
                ? "Complete Wikis have been generated with this model."
                : "Not yet tested with CodeNib. If a chapter fails, the run stops and finished chapters are kept."}
            </p>
          )}
        </div>
      )}
      <div className="wgf-field">
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
        <p className="wgf-note">
          Create a key with a credit limit in{" "}
          <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener noreferrer">
            OpenRouter settings ↗
          </a>
          .
        </p>
      </div>
      <fieldset className="wgf-field wgf-budget">
        <legend>Budget for this run</legend>
        <div className="wgf-segments">
          {BUDGETS.map((value) => (
            <label key={value} className={budget === value ? "is-chosen" : undefined}>
              <input
                type="radio"
                name="wiki-generation-budget"
                value={value}
                checked={budget === value}
                disabled={busy}
                onChange={() => setBudget(value)}
              />
              <span>${value}</span>
            </label>
          ))}
        </div>
        <p className="wgf-note">
          Stops new calls when reported spending reaches this amount. A call
          already in progress can exceed it. Set a credit limit on your OpenRouter
          key; include BYOK usage if you also use your own provider keys through
          OpenRouter.
        </p>
      </fieldset>
      <label className="wiki-consent">
        <input type="checkbox" required disabled={busy || !admitted} />
        <span>
          I authorize generation using my OpenRouter account. CodeNib keeps my
          key in server memory only while this run is active, sends public
          source to OpenRouter and its model providers, and saves the Wiki for
          anyone with its link. The key is never saved.
        </span>
      </label>
      <button type="submit" className="wgf-submit" disabled={busy || !admitted || retryAt !== null}>
        {busy
          ? "Starting generation…"
          : resume
            ? "Resume unfinished chapters"
            : "Generate Wiki"}
      </button>
      {busy && (
        <div className="wiki-starting" role="status">
          <span className="wgf-spinner" aria-hidden="true" />
          <div>
            <strong>Checking your account and repository…</strong>
            <p>Your Wiki opens when the server accepts this run.</p>
          </div>
        </div>
      )}
      {error && (
        <div role="alert" className="trial-error wgf-error">
          <p>{error}</p>
          {retryAt !== null && (
            <p className="wgf-wait" aria-live="off">
              You can try again in {waitSeconds} {waitSeconds === 1 ? "second" : "seconds"}.
            </p>
          )}
          {outcomeUnknown && (
            <p>
              The server may still have accepted this run.{" "}
              <button type="button" className="btn-ghost" onClick={onStarted}>
                Check saved attempt
              </button>
            </p>
          )}
        </div>
      )}
      {!resume && (
        <details className="wgf-rules">
          <summary>Hosted limits and which repositories qualify</summary>
          <WikiRepositoryRules limits={eligibility?.limits} />
        </details>
      )}
    </form>
  );
}
