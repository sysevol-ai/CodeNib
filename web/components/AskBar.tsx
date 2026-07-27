"use client";

import { useEffect, useRef, useState } from "react";
import { navigate } from "@/lib/router";

/**
 * DeepWiki-style ask bar pinned to the bottom of the page. By default,
 * submitting routes to the dedicated answer page (`/[repoId]/ask?q=...`).
 * When `onSubmit` is given (the ask page's conversation thread), the question
 * is handed to it in place instead and the input clears for the next one.
 */
export default function AskBar({
  repoId,
  repo,
  defaultValue = "",
  onSubmit,
  disabled = false,
  collapsible = false,
}: {
  repoId: string;
  repo: string;
  defaultValue?: string;
  onSubmit?: (query: string) => void;
  disabled?: boolean;
  collapsible?: boolean;
}) {
  const [q, setQ] = useState(defaultValue);
  const [expanded, setExpanded] = useState(!collapsible);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (expanded && collapsible) inputRef.current?.focus();
  }, [collapsible, expanded]);

  useEffect(() => {
    if (!collapsible || !expanded) return;
    const collapse = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    document.addEventListener("keydown", collapse);
    return () => document.removeEventListener("keydown", collapse);
  }, [collapsible, expanded]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const query = q.trim();
    if (!query || disabled) return;
    if (onSubmit) {
      onSubmit(query);
      setQ("");
      return;
    }
    navigate(`/${encodeURIComponent(repoId)}/ask?q=${encodeURIComponent(query)}`);
  }

  if (collapsible && !expanded) {
    return (
      <div className="askbar askbar-collapsed">
        <button
          className="askbar-trigger"
          type="button"
          onClick={() => setExpanded(true)}
          aria-label={`Ask a question about ${repo}`}
          title={`Ask about ${repo}`}
        >
          <span className="askbar-icon" aria-hidden>
            ✦
          </span>
          <span>Ask this repository</span>
        </button>
      </div>
    );
  }

  return (
    <form
      className={`askbar ${collapsible ? "askbar-expanded" : ""}`}
      onSubmit={submit}
      role="search"
    >
      <div className="askbar-inner">
        <span className="askbar-icon" aria-hidden>
          ✦
        </span>
        <input
          ref={inputRef}
          className="askbar-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={
            onSubmit ? `Ask a follow-up about ${repo}…` : `Ask anything about ${repo}…`
          }
          aria-label={`Ask a question about ${repo}`}
        />
        {collapsible && (
          <button
            className="askbar-close"
            type="button"
            onClick={() => setExpanded(false)}
            aria-label="Close question input"
            title="Close"
          >
            ×
          </button>
        )}
        <button className="askbar-send" type="submit" disabled={disabled || !q.trim()}>
          Ask <span className="askbar-kbd">↵</span>
        </button>
      </div>
    </form>
  );
}
