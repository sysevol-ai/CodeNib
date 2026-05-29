"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

/**
 * DeepWiki-style ask bar pinned to the bottom of the page. Submitting routes
 * to the dedicated answer page (`/[repoId]/ask?q=...`) — every question opens
 * its own page rather than appending to an in-place chat.
 */
export default function AskBar({
  repoId,
  repo,
  defaultValue = "",
}: {
  repoId: string;
  repo: string;
  defaultValue?: string;
}) {
  const router = useRouter();
  const [q, setQ] = useState(defaultValue);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const query = q.trim();
    if (!query) return;
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
          placeholder={`Ask anything about ${repo}…`}
          aria-label={`Ask a question about ${repo}`}
        />
        <button className="askbar-send" type="submit" disabled={!q.trim()}>
          Ask <span className="askbar-kbd">↵</span>
        </button>
      </div>
    </form>
  );
}
