"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  askQuestion,
  fetchRepos,
  type ChatResponse,
  type Citation,
  type RepoInfo,
} from "@/lib/api";

interface Message {
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
}

function CitationCard({ c }: { c: Citation }) {
  const loc =
    c.start_line != null ? `${c.file}:${c.start_line}-${c.end_line}` : c.file;
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

export default function Home() {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [activeRepo, setActiveRepo] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchRepos()
      .then((r) => {
        setRepos(r);
        if (r.length > 0) setActiveRepo(r[0].id);
      })
      .catch((e) => setError(String(e)));
  }, []);

  function selectRepo(id: string) {
    setActiveRepo(id);
    setMessages([]);
    setError(null);
  }

  async function send() {
    const query = input.trim();
    if (!query || !activeRepo || loading) return;
    setInput("");
    setError(null);
    setMessages((m) => [...m, { role: "user", text: query }]);
    setLoading(true);
    try {
      const resp: ChatResponse = await askQuestion(activeRepo, query);
      setMessages((m) => [
        ...m,
        { role: "assistant", text: resp.answer, citations: resp.citations },
      ]);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>CodeMiner Wiki</h1>
        <div className="tagline">Chat with a repository</div>
        {repos.map((r) => (
          <button
            key={r.id}
            className={`repo-item ${r.id === activeRepo ? "active" : ""}`}
            onClick={() => selectRepo(r.id)}
          >
            <div className="name">{r.name}</div>
            <div className="meta">
              {r.languages.join(", ")} · {r.file_count} files
            </div>
          </button>
        ))}
        {repos.length === 0 && !error && (
          <div className="spinner">Loading repos…</div>
        )}
      </aside>

      <main className="main">
        <div className="messages">
          {messages.length === 0 && (
            <div className="empty">
              {activeRepo
                ? "Ask a question about this repository."
                : "Select a repository to begin."}
            </div>
          )}
          {messages.map((m, i) => (
            <div className="msg" key={i}>
              <div className="role">{m.role === "user" ? "You" : "CodeMiner"}</div>
              <div className={`bubble ${m.role}`}>
                {m.role === "assistant" ? (
                  <ReactMarkdown>{m.text || "(no answer)"}</ReactMarkdown>
                ) : (
                  m.text
                )}
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
            <div className="msg">
              <div className="spinner">CodeMiner is searching the codebase…</div>
            </div>
          )}
          {error && (
            <div className="msg">
              <div className="error">{error}</div>
            </div>
          )}
        </div>

        <div className="composer">
          <input
            value={input}
            placeholder="How does X work?"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") send();
            }}
            disabled={!activeRepo || loading}
          />
          <button onClick={send} disabled={!activeRepo || loading || !input.trim()}>
            Ask
          </button>
        </div>
      </main>
    </div>
  );
}
