"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
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
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

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
  // Distinct relevant files, ranked by first appearance (retrieval order).
  const files = useMemo(
    () => [...new Set(citations.map((c) => c.file).filter(Boolean) as string[])],
    [citations]
  );
  useEffect(() => {
    setSelectedFile((cur) => (cur && files.includes(cur) ? cur : files[0] ?? null));
  }, [files]);

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
          <h1 className="ask-q">{q || "Ask a question"}</h1>

          {/* Relevant source files — at the top, below the title (DeepWiki). */}
          {files.length > 0 && (
            <div className="relevant-files">
              <div className="relevant-files-title">Relevant source files</div>
              <div className="relevant-files-list">
                {files.slice(0, 14).map((f) => (
                  <button
                    key={f}
                    className={`relevant-file mono ${f === selectedFile ? "active" : ""}`}
                    title={f}
                    onClick={() => setSelectedFile(f)}
                  >
                    {f}
                  </button>
                ))}
                {files.length > 14 && (
                  <span className="muted small">+{files.length - 14} more</span>
                )}
              </div>
            </div>
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
                {Math.round(resp.total_duration_ms)} ms · {files.length} files
              </div>
            </>
          )}
        </section>

        <aside className="ask-code">
          <CodePanel
            repoId={repoId}
            files={files}
            activeFile={selectedFile}
            onPick={setSelectedFile}
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
