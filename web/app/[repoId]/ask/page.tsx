"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import AskIndexFreshness, {
  askIndexAttention,
} from "@/components/AskIndexFreshness";
import CodePanel from "@/components/CodePanel";
import {
  askQuestion,
  fetchIndexStatus,
  fetchRepos,
  fetchWikiTree,
  type ChatMessage,
  type ChatResponse,
  type RepoInfo,
  type RepoIndexStatus,
  type WikiPageRef,
} from "@/lib/api";
import { codeRefs } from "@/lib/citations";
import { hasCanonicalIndexSurfaces } from "@/lib/indexStatus";
import { relatedWikiPages } from "@/lib/relatedWiki";
import { AppLink } from "@/lib/router";
import { isStaticRuntime } from "@/lib/runtime";

// DeepWiki clamps the question to ~200 chars before "Show full text".
const Q_TRUNCATE = 200;
const INDEX_STATUS_GATE_TIMEOUT_MS = 2_000;

// One question + its (eventual) answer in the conversation thread.
interface Turn {
  q: string;
  resp: ChatResponse | null;
  err: string | null;
}

interface QueuedQuestion {
  id: number;
  query: string;
}

function AskAnswer({ repoId, query }: { repoId: string; query: string }) {
  const q = query.trim();
  const staticRuntime = isStaticRuntime();

  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [wikiPages, setWikiPages] = useState<WikiPageRef[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingSeconds, setLoadingSeconds] = useState(0);
  const [indexStatus, setIndexStatus] = useState<RepoIndexStatus | null>(null);
  const [indexStatusResolved, setIndexStatusResolved] = useState(false);
  const [pendingQuestion, setPendingQuestion] =
    useState<QueuedQuestion | null>(null);
  const [showIndexUpdate, setShowIndexUpdate] = useState(false);
  // Bumped whenever the thread resets so in-flight answers from a previous
  // conversation can't land in the new one.
  const genRef = useRef(0);
  const indexStatusRequest = useRef<AbortController | null>(null);
  const indexStatusRequestId = useRef(0);
  const questionId = useRef(0);
  const conversationKey = `${repoId}\u0000${q}`;
  const conversationKeyRef = useRef(conversationKey);
  const dispatchedQuestionId = useRef(0);
  const consumedPendingId = useRef(0);
  const [queuedQuestion, setQueuedQuestion] =
    useState<QueuedQuestion | null>(() =>
      q ? { id: ++questionId.current, query: q } : null,
    );

  const loadIndexStatus = useCallback(async () => {
    indexStatusRequest.current?.abort();
    const controller = new AbortController();
    indexStatusRequest.current = controller;
    const requestId = ++indexStatusRequestId.current;
    setIndexStatusResolved(false);
    if (staticRuntime) {
      indexStatusRequest.current = null;
      setIndexStatus(null);
      setIndexStatusResolved(true);
      return;
    }

    const timeout = window.setTimeout(
      () => controller.abort(),
      INDEX_STATUS_GATE_TIMEOUT_MS,
    );
    try {
      const next = await fetchIndexStatus(repoId, {
        signal: controller.signal,
      });
      if (next.repo_id !== repoId || !hasCanonicalIndexSurfaces(next)) {
        throw new Error("Index status response is incomplete");
      }
      if (indexStatusRequestId.current === requestId) setIndexStatus(next);
    } catch {
      // Index awareness must not make an otherwise usable Ask route fail. A
      // bounded unavailable read falls back to the currently loaded runtime.
    } finally {
      window.clearTimeout(timeout);
      if (indexStatusRequestId.current === requestId) {
        indexStatusRequest.current = null;
        setIndexStatusResolved(true);
      }
    }
  }, [repoId, staticRuntime]);

  useEffect(() => {
    setIndexStatus(null);
    void loadIndexStatus();
    return () => {
      indexStatusRequestId.current += 1;
      indexStatusRequest.current?.abort();
      indexStatusRequest.current = null;
    };
  }, [loadIndexStatus]);

  useEffect(() => {
    let cancelled = false;
    setWikiPages([]);
    fetchRepos()
      .then((rs) => {
        if (!cancelled) setRepo(rs.find((r) => r.id === repoId) ?? null);
      })
      .catch(() => {});
    // Related links are optional here. Ask must never trigger an outline model
    // call merely because the page was opened.
    fetchWikiTree(repoId, { cachedOnly: true })
      .then((tree) => {
        if (!cancelled) setWikiPages(tree.pages ?? []);
      })
      .catch(() => {
        if (!cancelled) setWikiPages([]);
      });
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  useEffect(() => {
    if (!loading) {
      setLoadingSeconds(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(
      () => setLoadingSeconds(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [loading]);

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

  // A new URL question starts one fresh conversation. The key ref also avoids
  // issuing a duplicate initial request when React StrictMode replays effects.
  useEffect(() => {
    if (conversationKeyRef.current === conversationKey) return;
    conversationKeyRef.current = conversationKey;
    genRef.current += 1;
    setTurns([]);
    setLoading(false);
    setQExpanded(new Set());
    setPendingQuestion(null);
    setShowIndexUpdate(false);
    consumedPendingId.current = 0;
    setQueuedQuestion(
      q ? { id: ++questionId.current, query: q } : null,
    );
  }, [conversationKey, q]);

  const indexAttention = useMemo(
    () => (indexStatus ? askIndexAttention(indexStatus) : null),
    [indexStatus],
  );

  // A first question waits only for the bounded status read. Missing or failed
  // status awareness fails open to the already loaded retrieval snapshot.
  useEffect(() => {
    if (!queuedQuestion || !indexStatusResolved) return;
    if (dispatchedQuestionId.current === queuedQuestion.id) return;
    dispatchedQuestionId.current = queuedQuestion.id;
    setQueuedQuestion(null);
    if (indexStatus && indexAttention?.needsAttention) {
      setPendingQuestion(queuedQuestion);
      return;
    }
    runAsk(queuedQuestion.query, []);
  }, [
    indexAttention,
    indexStatus,
    indexStatusResolved,
    queuedQuestion,
    runAsk,
  ]);

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

  function submitQuestion(query: string) {
    if (loading) return;
    if (turns.length > 0) {
      followUp(query);
      return;
    }
    setQueuedQuestion({ id: ++questionId.current, query });
  }

  const askPendingQuestion = useCallback(() => {
    if (!pendingQuestion || consumedPendingId.current === pendingQuestion.id) {
      return;
    }
    consumedPendingId.current = pendingQuestion.id;
    const next = pendingQuestion.query;
    setPendingQuestion(null);
    setShowIndexUpdate(false);
    runAsk(next, []);
  }, [pendingQuestion, runAsk]);

  // Once the guarded runtime refresh reports a current retrieval snapshot,
  // continue the held question without asking the user to submit it again.
  useEffect(() => {
    if (
      !showIndexUpdate ||
      !pendingQuestion ||
      !indexStatusResolved ||
      !indexStatus ||
      indexAttention?.needsAttention
    ) {
      return;
    }
    askPendingQuestion();
  }, [
    askPendingQuestion,
    indexAttention,
    indexStatus,
    indexStatusResolved,
    pendingQuestion,
    showIndexUpdate,
  ]);

  const repoName = repo ? repo.repo : repoId;
  const waitingQuestion = pendingQuestion ?? queuedQuestion;
  // Per-turn citation lists so a chip's index lines up with the code pane.
  const turnRefs = useMemo(
    () => turns.map((t) => codeRefs(t.resp?.citations ?? [])),
    [turns]
  );
  const relatedByTurn = useMemo(
    () =>
      turns.map((turn, index) =>
        relatedWikiPages(
          wikiPages,
          turn.q,
          turn.resp?.answer ?? "",
          turnRefs[index] ?? [],
        ),
      ),
    [turns, turnRefs, wikiPages],
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
            <AppLink href={`/${encodeURIComponent(repoId)}`} className="mono">
              {repoName}
            </AppLink>
            <span className="crumb-sep">/</span>
            <span className="crumb-page">Ask</span>
          </nav>
        }
      />

      {/* DeepWiki-style codemap: hierarchical explanation (left) + code (right). */}
      <div className={`ask-codemap ${turns.length === 0 ? "empty" : ""}`}>
        <section className="ask-explain">
          <AppLink className="ask-back" href={`/${encodeURIComponent(repoId)}`}>
            ← Back to wiki
          </AppLink>

          {turns.length === 0 && (
            <>
              <h1 className="ask-q">
                {waitingQuestion?.query ?? `Ask about ${repoName}`}
              </h1>
              {queuedQuestion && !indexStatusResolved && (
                <div className="ask-index-checking" role="status">
                  Checking retrieval index freshness…
                </div>
              )}
              {indexStatus && indexAttention?.needsAttention && (
                <AskIndexFreshness
                  status={indexStatus}
                  hasPendingQuestion={Boolean(pendingQuestion)}
                  showUpdateControl={showIndexUpdate}
                  onShowUpdate={() => setShowIndexUpdate(true)}
                  onAskCurrent={askPendingQuestion}
                  onRefresh={loadIndexStatus}
                />
              )}
              {!waitingQuestion && (
                <AskBar
                  repoId={repoId}
                  repo={repoName}
                  onSubmit={submitQuestion}
                  disabled={loading}
                  inline
                />
              )}
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
                  <div className="muted ask-thinking" role="status" aria-live="polite">
                    <div>
                      {loadingSeconds < 5
                        ? `Searching ${repoName}…`
                        : "Reviewing retrieved code and refining the answer…"}
                    </div>
                    {loadingSeconds >= 5 && (
                      <div className="ask-thinking-detail">
                        Multi-step agent run
                        <span className="mono"> · {loadingSeconds}s</span>
                      </div>
                    )}
                  </div>
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
                    {relatedByTurn[i]?.length > 0 && (
                      <nav className="ask-related" aria-label="Related Wiki pages">
                        <span className="ask-related-label">Related wiki</span>
                        {relatedByTurn[i].map((related) => (
                          <AppLink
                            className="ask-related-link"
                            href={`/${encodeURIComponent(repoId)}?p=${encodeURIComponent(related.id)}`}
                            key={related.id}
                            title={related.breadcrumb}
                          >
                            {related.title}
                            <span aria-hidden> →</span>
                          </AppLink>
                        ))}
                      </nav>
                    )}
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

        {turns.length > 0 && (
          <aside className="ask-code">
            <CodePanel
              repoId={repoId}
              citations={turnRefs[activeTurn] ?? []}
              repo={repo?.repo}
              commit={repo?.base_commit}
              active={active}
              onSelect={(i) => selectCitation(activeTurn, i)}
              onVisibleChange={setActive}
              scrollSignal={scrollSignal}
            />
          </aside>
        )}
      </div>

      {turns.length > 0 && (
        <AskBar
          repoId={repoId}
          repo={repoName}
          onSubmit={submitQuestion}
          disabled={loading}
        />
      )}
    </div>
  );
}

export default function AskPage({
  repoId,
  query,
}: {
  repoId: string;
  query: string;
}) {
  return <AskAnswer repoId={repoId} query={query} />;
}
