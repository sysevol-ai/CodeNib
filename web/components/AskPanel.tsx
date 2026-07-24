"use client";

import { useEffect, useRef, useState } from "react";
import Markdown from "@/components/Markdown";
import { askQuestion, type Citation } from "@/lib/api";

interface Message {
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  error?: boolean;
}

function CitationCard({ c }: { c: Citation }) {
  const loc = c.start_line != null ? `${c.file}:${c.start_line}-${c.end_line}` : c.file;
  return (
    <div className="citation">
      <div className="head">
        {c.node_name ? `${c.node_name} — ` : ""}
        {loc}
      </div>
      {c.content && <pre>{c.content}</pre>}
    </div>
  );
}

const SUGGESTIONS = [
  "What does this repository do?",
  "Where is the main entry point?",
  "How is the project structured?",
];

export default function AskPanel({ repoId, repo }: { repoId: string; repo: string }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function submit(q: string) {
    const query = q.trim();
    if (!query || loading) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: query }]);
    setLoading(true);
    try {
      const resp = await askQuestion(repoId, [{ role: "user", content: query }]);
      setMessages((m) => [
        ...m,
        { role: "assistant", text: resp.answer || "(no answer)", citations: resp.citations },
      ]);
    } catch {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          error: true,
          text:
            "Couldn't reach the retrieval backend (or no LLM key is set). " +
            "Start it with `codeminer-web` and configure a model.",
        },
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="ask-panel">
      <div className="ask-messages">
        {messages.length === 0 && (
          <div className="ask-suggestions">
            <p className="muted">Ask anything about <code>{repo}</code>:</p>
            {SUGGESTIONS.map((s) => (
              <button key={s} className="suggestion" onClick={() => submit(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m, i) => (
          <div className={`msg ${m.role}`} key={i}>
            <div className="role">{m.role === "user" ? "You" : "CodeNib"}</div>
            <div className={`bubble ${m.error ? "error" : ""}`}>
              {m.role === "assistant" && !m.error ? <Markdown>{m.text}</Markdown> : m.text}
              {m.citations && m.citations.length > 0 && (
                <details className="citations">
                  <summary>{m.citations.length} code references</summary>
                  {m.citations.map((c, j) => (
                    <CitationCard c={c} key={j} />
                  ))}
                </details>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div className="msg assistant">
            <div className="role">CodeNib</div>
            <div className="bubble thinking">Searching the codebase…</div>
          </div>
        )}
        <div ref={endRef} />
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit(input);
        }}
      >
        <input
          value={input}
          placeholder={`Ask about ${repo}…`}
          onChange={(e) => setInput(e.target.value)}
          disabled={loading}
          aria-label="Ask a question"
        />
        <button type="submit" disabled={loading || !input.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}
