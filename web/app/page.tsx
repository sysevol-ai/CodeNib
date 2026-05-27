"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "@/components/Markdown";
import {
  askQuestion,
  fetchRepos,
  type Citation,
  type RepoInfo,
} from "@/lib/api";

interface Message {
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  error?: boolean;
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

function RepoCard({
  r,
  active,
  onClick,
}: {
  r: RepoInfo;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button className={`repo-card ${active ? "active" : ""}`} onClick={onClick}>
      <div className="repo-card-name">{r.repo}</div>
      <div className="repo-card-meta">
        <span className={`lang lang-${r.language}`}>{r.language || "code"}</span>
        <span className="mono">{r.commit_short}</span>
        {r.file_count > 0 && <span>{r.file_count} files</span>}
      </div>
    </button>
  );
}

const SUGGESTIONS = [
  "What does this repository do?",
  "Where is the main entry point?",
  "How is the project structured?",
];

export default function Home() {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [reposError, setReposError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [byRepo, setByRepo] = useState<Record<string, Message[]>>({});
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchRepos()
      .then((r) => {
        setRepos(r);
        if (r.length > 0) setActiveId(r[0].id);
      })
      .catch((e) => setReposError(String(e)));
  }, []);

  const active = useMemo(
    () => repos.find((r) => r.id === activeId) ?? null,
    [repos, activeId]
  );
  const messages = activeId ? byRepo[activeId] ?? [] : [];

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  function pick(id: string) {
    setActiveId(id);
    setSidebarOpen(false);
  }

  function pushMessage(repoId: string, msg: Message) {
    setByRepo((prev) => ({ ...prev, [repoId]: [...(prev[repoId] ?? []), msg] }));
  }

  async function submit(q: string) {
    const query = q.trim();
    if (!query || !activeId || loading) return;
    const repoId = activeId;
    setInput("");
    pushMessage(repoId, { role: "user", text: query });
    setLoading(true);
    try {
      const resp = await askQuestion(repoId, query);
      pushMessage(repoId, {
        role: "assistant",
        text: resp.answer || "(no answer)",
        citations: resp.citations,
      });
    } catch {
      pushMessage(repoId, {
        role: "assistant",
        error: true,
        text:
          "Couldn't reach the retrieval backend. Start it with `codeminer-web` " +
          "(after `python scripts/build_qa_index.py`) and set an LLM API key.",
      });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="qa">
      <header className="qa-top">
        <button
          className="hamburger"
          aria-label="Toggle repository list"
          onClick={() => setSidebarOpen((o) => !o)}
        >
          ☰
        </button>
        <span className="brand">
          <span className="brand-mark">◆</span> CodeMiner
          <span className="brand-tag">Code QA</span>
        </span>
        {active && (
          <span className="active-repo mono">
            {active.repo} @ {active.commit_short}
          </span>
        )}
      </header>

      <div className="qa-body">
        <aside className={`repo-list ${sidebarOpen ? "open" : ""}`}>
          <div className="repo-list-title">Repositories</div>
          {reposError && (
            <div className="repo-empty">
              Backend unavailable. Run <code>codeminer-web</code> after building
              an index.
            </div>
          )}
          {!reposError && repos.length === 0 && (
            <div className="repo-empty">Loading repositories…</div>
          )}
          {repos.map((r) => (
            <RepoCard
              key={r.id}
              r={r}
              active={r.id === activeId}
              onClick={() => pick(r.id)}
            />
          ))}
        </aside>
        {sidebarOpen && (
          <div className="scrim" onClick={() => setSidebarOpen(false)} aria-hidden />
        )}

        <main className="chat">
          {!active ? (
            <div className="chat-empty">
              <h1>Ask a codebase anything</h1>
              <p>
                Select a repository on the left, then ask a question. CodeMiner
                searches the code and answers with citations.
              </p>
            </div>
          ) : (
            <>
              <div className="chat-header">
                <h1>{active.repo}</h1>
                <div className="chat-sub mono">
                  @ {active.commit_short}
                  {active.language ? ` · ${active.language}` : ""}
                  {active.file_count ? ` · ${active.file_count} files` : ""}
                </div>
                {active.problem_statement && (
                  <details className="issue">
                    <summary>Example issue from this instance</summary>
                    <div className="issue-body">
                      <Markdown>
                        {active.problem_statement.slice(0, 1200)}
                      </Markdown>
                      <button
                        className="ghost"
                        onClick={() =>
                          submit(
                            "Where in the code would I fix this issue?\n\n" +
                              active.problem_statement.slice(0, 600)
                          )
                        }
                      >
                        Ask where to fix this
                      </button>
                    </div>
                  </details>
                )}
              </div>

              <div className="messages">
                {messages.length === 0 && (
                  <div className="suggestions">
                    {SUGGESTIONS.map((s) => (
                      <button key={s} onClick={() => submit(s)}>
                        {s}
                      </button>
                    ))}
                  </div>
                )}
                {messages.map((m, i) => (
                  <div className={`msg ${m.role}`} key={i}>
                    <div className="role">
                      {m.role === "user" ? "You" : "CodeMiner"}
                    </div>
                    <div className={`bubble ${m.error ? "error" : ""}`}>
                      {m.role === "assistant" && !m.error ? (
                        <Markdown>{m.text}</Markdown>
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
                  <div className="msg assistant">
                    <div className="role">CodeMiner</div>
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
                  placeholder={`Ask about ${active.repo}…`}
                  onChange={(e) => setInput(e.target.value)}
                  disabled={loading}
                  aria-label="Ask a question"
                />
                <button type="submit" disabled={loading || !input.trim()}>
                  Ask
                </button>
              </form>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
