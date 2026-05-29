"use client";

import { Suspense, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import AskBar from "@/components/AskBar";
import CitationItem from "@/components/CitationItem";
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

      <main className="ask-answer">
        <Link className="ask-back" href={`/${encodeURIComponent(repoId)}`}>
          ← Back to wiki
        </Link>
        <h1 className="ask-q">{q || "Ask a question"}</h1>

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
            {resp.citations.length > 0 && (
              <details className="citations" open>
                <summary>{resp.citations.length} code references</summary>
                {resp.citations.map((c, i) => (
                  <CitationItem repoId={repoId} c={c} key={i} />
                ))}
              </details>
            )}
            <div className="ask-tools muted small">
              {resp.tool_calls.length} tool calls · {resp.total_turns} turns ·{" "}
              {Math.round(resp.total_duration_ms)} ms
            </div>
          </>
        )}
      </main>

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
