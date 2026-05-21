"use client";

import { useState } from "react";
import { askQuestion, type Citation } from "@/lib/api";
import Markdown from "./Markdown";

const REPO_ID = process.env.NEXT_PUBLIC_REPO_ID ?? "codeminer";

interface Turn {
  q: string;
  answer?: string;
  citations?: Citation[];
  error?: string;
}

export default function AskPanel() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || loading) return;
    setInput("");
    setOpen(true);
    setLoading(true);
    setTurns((t) => [...t, { q }]);
    try {
      const resp = await askQuestion(REPO_ID, q);
      setTurns((t) =>
        t.map((turn, i) =>
          i === t.length - 1
            ? { ...turn, answer: resp.answer, citations: resp.citations }
            : turn
        )
      );
    } catch (err) {
      setTurns((t) =>
        t.map((turn, i) =>
          i === t.length - 1
            ? {
                ...turn,
                error:
                  "The retrieval backend isn't reachable in this environment. " +
                  "Run `codeminer-web` with a built index and an LLM key to enable answers.",
              }
            : turn
        )
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <form className="ask-form" onSubmit={submit} role="search">
        <span className="ask-icon" aria-hidden>
          ⌕
        </span>
        <input
          aria-label="Ask a question about this repository"
          placeholder="Ask a question about this repo…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </form>

      {open && (
        <div className="ask-drawer" role="dialog" aria-label="Answers">
          <div className="ask-drawer-head">
            <span>Ask the repo</span>
            <button onClick={() => setOpen(false)} aria-label="Close">
              ×
            </button>
          </div>
          <div className="ask-drawer-body">
            {turns.length === 0 && (
              <p className="muted">Ask about how a module or feature works.</p>
            )}
            {turns.map((t, i) => (
              <div className="ask-turn" key={i}>
                <div className="ask-q">{t.q}</div>
                {t.answer !== undefined && (
                  <div className="ask-a">
                    <Markdown>{t.answer || "(no answer)"}</Markdown>
                    {t.citations && t.citations.length > 0 && (
                      <details className="citations">
                        <summary>{t.citations.length} code references</summary>
                        {t.citations.map((c, j) => (
                          <div className="citation" key={j}>
                            <div className="head">
                              {c.node_name ? `${c.node_name} — ` : ""}
                              {c.file}
                              {c.start_line != null
                                ? `:${c.start_line}-${c.end_line}`
                                : ""}
                            </div>
                            {c.content && <pre>{c.content}</pre>}
                          </div>
                        ))}
                      </details>
                    )}
                  </div>
                )}
                {t.error && <div className="ask-error">{t.error}</div>}
              </div>
            ))}
            {loading && <div className="muted">Searching the codebase…</div>}
          </div>
        </div>
      )}
    </>
  );
}
