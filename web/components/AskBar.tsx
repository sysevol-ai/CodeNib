"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

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
}: {
  repoId: string;
  repo: string;
  defaultValue?: string;
  onSubmit?: (query: string) => void;
  disabled?: boolean;
}) {
  const router = useRouter();
  const [q, setQ] = useState(defaultValue);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const query = q.trim();
    if (!query || disabled) return;
    if (onSubmit) {
      onSubmit(query);
      setQ("");
      return;
    }
    router.push(`/${encodeURIComponent(repoId)}/ask?q=${encodeURIComponent(query)}`);
  }

  return (
    <form className="askbar" onSubmit={submit} role="search">
      <div className="askbar-inner">
        <span className="askbar-icon" aria-hidden>
          ✦
        </span>
        <input
          className="askbar-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={
            onSubmit ? `Ask a follow-up about ${repo}…` : `Ask anything about ${repo}…`
          }
          aria-label={`Ask a question about ${repo}`}
        />
        <button className="askbar-send" type="submit" disabled={disabled || !q.trim()}>
          Ask <span className="askbar-kbd">↵</span>
        </button>
      </div>
    </form>
  );
}
