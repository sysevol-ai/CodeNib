"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import CodePanel from "@/components/CodePanel";
import { askQuestion, fetchRepos, type ChatResponse, type RepoInfo } from "@/lib/api";

function AskAnswer() {
  const params = useParams<{ repoId: string }>();
  const repoId = decodeURIComponent(params.repoId);
  const q = (useSearchParams().get("q") ?? "").trim();

  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [resp, setResp] = useState<ChatResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchRepos()
      .then((rs) => setRepo(rs.find((r) => r.id === repoId) ?? null))
      .catch(() => {});
  }, [repoId]);

  // Re-fetch whenever the question changes — every submit is its own answer.
  useEffect(() => {
    if (!q) {
      setResp(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setResp(null);
    setErr(null);
    askQuestion(repoId, q)
      .then((r) => !cancelled && setResp(r))
      .catch((e) => !cancelled && setErr(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, q]);

  const repoName = repo ? repo.repo : repoId;
  const citations = resp?.citations ?? [];

  // Long questions are clamped to a few lines with a "Show full text" toggle
  // (matches DeepWiki). Measure while clamped to decide whether to show it.
  const [qExpanded, setQExpanded] = useState(false);
  const [qOverflows, setQOverflows] = useState(false);
  const qRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    setQExpanded(false);
    const el = qRef.current;
    if (el) setQOverflows(el.scrollHeight > el.clientHeight + 2);
  }, [q]);

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
          <h1 ref={qRef} className={`ask-q ${qExpanded ? "" : "clamped"}`}>
            {q || "Ask a question"}
          </h1>
          {q && qOverflows && (
            <button className="ask-q-toggle" onClick={() => setQExpanded((e) => !e)}>
              {qExpanded ? "Show less" : "Show full text"}
            </button>
          )}

          {!q && <p className="muted">Type a question in the bar below.</p>}
          {loading && <p className="muted ask-thinking">Searching {repoName}…</p>}
          {err && (
            <p className="muted">
              Couldn&apos;t reach the retrieval backend. Is the API running?
            </p>
          )}

          {resp && (
            <>
              <article className="ask-a">
                <Markdown>{resp.answer || "(no answer)"}</Markdown>
              </article>
              <div className="ask-tools muted small">
                {resp.tool_calls.length} tool calls · {resp.total_turns} turns ·{" "}
                {Math.round(resp.total_duration_ms)} ms · {citations.length} references
              </div>
            </>
          )}
        </section>

        <aside className="ask-code">
          <CodePanel
            repoId={repoId}
            citations={citations}
            repo={repo?.repo}
            commit={repo?.base_commit}
          />
        </aside>
      </div>

      <AskBar repoId={repoId} repo={repoName} defaultValue={q} />
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
