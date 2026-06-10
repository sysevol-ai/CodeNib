"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import CodePanel from "@/components/CodePanel";
import {
  askQuestion,
  fetchRepos,
  type ChatMessage,
  type ChatResponse,
  type RepoInfo,
} from "@/lib/api";
import { codeRefs } from "@/lib/citations";

// DeepWiki clamps the question to ~200 chars before "Show full text".
const Q_TRUNCATE = 200;

// One question + its (eventual) answer in the conversation thread.
interface Turn {
  q: string;
  resp: ChatResponse | null;
  err: string | null;
}

function AskAnswer() {
  const params = useParams<{ repoId: string }>();
  const repoId = decodeURIComponent(params.repoId);
  const q = (useSearchParams().get("q") ?? "").trim();

  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  // Bumped whenever the thread resets so in-flight answers from a previous
  // conversation can't land in the new one.
  const genRef = useRef(0);

  useEffect(() => {
    fetchRepos()
      .then((rs) => setRepo(rs.find((r) => r.id === repoId) ?? null))
      .catch(() => {});
  }, [repoId]);

  // Append a turn and fetch its answer. The request carries the prior turns
  // plus this question as the final user message (DeepWiki-style) so the agent
  // can resolve follow-ups ("what calls it?", "show me where").
  const runAsk = useCallback(
    (query: string, history: ChatMessage[]) => {
      const gen = genRef.current;
      setTurns((ts) => [...ts, { q: query, resp: null, err: null }]);
      setLoading(true);
      askQuestion(repoId, [...history, { role: "user", content: query }])
        .then((r) => {
          if (gen !== genRef.current) return;
          setTurns((ts) => {
            const next = [...ts];
            next[next.length - 1] = { ...next[next.length - 1], resp: r };
            return next;
          });
        })
        .catch((e) => {
          if (gen !== genRef.current) return;
          setTurns((ts) => {
            const next = [...ts];
            next[next.length - 1] = { ...next[next.length - 1], err: String(e) };
            return next;
          });
        })
        .finally(() => {
          if (gen === genRef.current) setLoading(false);
        });
    },
    [repoId]
  );

  // A new URL question (arriving from a wiki page's ask bar) starts a fresh
  // conversation; follow-ups submitted on this page append in place instead.
  useEffect(() => {
    genRef.current += 1;
    setTurns([]);
    setLoading(false);
    setQExpanded(new Set());
    if (q) runAsk(q, []);
  }, [repoId, q, runAsk]);

  function followUp(query: string) {
    if (loading) return;
    const history: ChatMessage[] = [];
    for (const t of turns) {
      if (!t.resp) continue; // skip errored / unanswered turns
      history.push({ role: "user", content: t.q });
      history.push({ role: "assistant", content: t.resp.answer });
    }
    runAsk(query, history);
  }

  const repoName = repo ? repo.repo : repoId;
  // Per-turn citation lists so a chip's index lines up with the code pane.
  const turnRefs = useMemo(
    () => turns.map((t) => codeRefs(t.resp?.citations ?? [])),
    [turns]
  );
  // Which turn's citations the code pane shows + the highlighted one.
  const [activeTurn, setActiveTurn] = useState(0);
  const [active, setActive] = useState(0);
  const [scrollSignal, setScrollSignal] = useState(0);
  // Focus the newest answered turn's citations whenever the thread changes.
  useEffect(() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].resp) {
        setActiveTurn(i);
        setActive(0);
        return;
      }
    }
    setActiveTurn(0);
    setActive(0);
  }, [turns]);
  // Select a citation: highlight it and (re-)scroll the code pane to it.
  const selectCitation = (turn: number, i: number) => {
    setActiveTurn(turn);
    setActive(i);
    setScrollSignal((s) => s + 1);
  };

  // Scroll the latest question into view as the thread grows.
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (turns.length > 1) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns.length]);

  // Truncate long questions with a per-turn "Show full text" toggle.
  const [qExpanded, setQExpanded] = useState<Set<number>>(new Set());
  const toggleQ = (i: number) =>
    setQExpanded((s) => {
      const next = new Set(s);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });

  return (
    <div className="wiki ask-page">
      <Header
        center={
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link href={`/${encodeURIComponent(repoId)}`} className="mono">
              {repoName}
            </Link>
            <span className="crumb-sep">/</span>
            <span className="crumb-page">Ask</span>
          </nav>
        }
      />

      {/* DeepWiki-style codemap: hierarchical explanation (left) + code (right). */}
      <div className="ask-codemap">
        <section className="ask-explain">
          <Link className="ask-back" href={`/${encodeURIComponent(repoId)}`}>
            ← Back to wiki
          </Link>

          {turns.length === 0 && (
            <>
              <h1 className="ask-q">Ask a question</h1>
              <p className="muted">Type a question in the bar below.</p>
            </>
          )}

          {turns.map((t, i) => {
            const long = t.q.length > Q_TRUNCATE;
            const shown =
              !long || qExpanded.has(i) ? t.q : t.q.slice(0, Q_TRUNCATE).trimEnd() + "…";
            return (
              <div className={`ask-turn ${i > 0 ? "followup" : ""}`} key={i}>
                {i === 0 ? (
                  <h1 className="ask-q">{shown}</h1>
                ) : (
                  <h2 className="ask-q">{shown}</h2>
                )}
                {long && (
                  <button className="ask-q-toggle" onClick={() => toggleQ(i)}>
                    {qExpanded.has(i) ? "Show less" : "Show full text"}
                  </button>
                )}

                {!t.resp && !t.err && (
                  <p className="muted ask-thinking">Searching {repoName}…</p>
                )}
                {t.err && (
                  <p className="muted">
                    Couldn&apos;t reach the retrieval backend. Is the API running?
                  </p>
                )}
                {t.resp && (
                  <>
                    <article className="ask-a">
                      <Markdown
                        citations={turnRefs[i]}
                        onCite={(j) => selectCitation(i, j)}
                      >
                        {t.resp.answer || "(no answer)"}
                      </Markdown>
                    </article>
                    <div className="ask-tools muted small">
                      {t.resp.tool_calls.length} tool calls · {t.resp.total_turns}{" "}
                      turns · {Math.round(t.resp.total_duration_ms)} ms ·{" "}
                      {turnRefs[i].length} references
                    </div>
                  </>
                )}
              </div>
            );
          })}
          <div ref={endRef} />
        </section>

        <aside className="ask-code">
          <CodePanel
            repoId={repoId}
            citations={turnRefs[activeTurn] ?? []}
            repo={repo?.repo}
            commit={repo?.base_commit}
            active={active}
            onSelect={(i) => selectCitation(activeTurn, i)}
            scrollSignal={scrollSignal}
          />
        </aside>
      </div>

      <AskBar repoId={repoId} repo={repoName} onSubmit={followUp} disabled={loading} />
    </div>
  );
}

export default function AskPage() {
  return (
    <Suspense
      fallback={
        <main className="ask-answer">
          <p className="muted">Loading…</p>
        </main>
      }
    >
      <AskAnswer />
    </Suspense>
  );
}
