// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

"use client";

import { useState } from "react";
import { AppLink } from "@/lib/router";

export const AGENT_SETUP_URL = "https://docs.codenib.ai/codegraph/";

/** A static Wiki never submits a visitor's question to a hosted model. */
export default function AgentSetup({ repoId, query = "" }: { repoId: string; query?: string }) {
  const [copied, setCopied] = useState(false);
  const question = query.trim().slice(0, 8000);
  return (
    <section className="agent-setup" aria-labelledby="agent-setup-title">
      <AppLink href={`/${encodeURIComponent(repoId)}`}>← Browse the Wiki</AppLink>
      <p className="preview-label">Precomputed Wiki</p>
      <h1 id="agent-setup-title">Ask on your own repository</h1>
      <p>
        These pages and source references are ready to browse. For a new question,
        connect CodeNib to Claude Code or Codex on your machine.
      </p>
      {question && (
        <div className="agent-question">
          <p>{question}</p>
          <button type="button" className="codegraph-fit" onClick={async () => {
            try {
              await navigator.clipboard.writeText(question);
              setCopied(true);
            } catch { setCopied(false); }
          }}>{copied ? "Question copied" : "Copy question"}</button>
        </div>
      )}
      <pre><code>{'pip install "codenib[graph,mcp]"\ncodenib codegraph init /path/to/your/repo'}</code></pre>
      <p className="small muted">
        This sets up local graph tools. Your agent provides the model and its billing.
        Setup requirements depend on your repository’s languages.
      </p>
      <a className="btn-primary" href={AGENT_SETUP_URL} target="_blank" rel="noreferrer">
        Set up your agent →
      </a>
      <p className="small muted">No question or API key is submitted by this page.</p>
    </section>
  );
}
