"use client";

import { useEffect, useRef, useState } from "react";
import HighlightedCode from "@/components/HighlightedCode";
import { fetchSource, repoRelative, type Citation } from "@/lib/api";

function ghUrl(repo: string | undefined, commit: string | undefined, c: Citation, rel: string) {
  if (!repo) return null;
  const anchor = c.start_line != null ? `#L${c.start_line}-L${c.end_line ?? c.start_line}` : "";
  return `https://github.com/${repo}/blob/${commit || "HEAD"}/${rel}${anchor}`;
}

/** One relevant code fragment: GitHub-linked header + highlighted line range. */
function Fragment({
  repoId,
  c,
  repo,
  commit,
  active,
}: {
  repoId: string;
  c: Citation;
  repo?: string;
  commit?: string;
  active: boolean;
}) {
  const rel = repoRelative(c.file);
  const [code, setCode] = useState("");
  const [start, setStart] = useState(c.start_line ?? 1);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(false);
    if (c.content) {
      setCode(c.content);
      setStart(c.start_line ?? 1);
      setLoading(false);
      return;
    }
    fetchSource(repoId, rel, c.start_line ?? undefined, c.end_line ?? undefined)
      .then((s) => {
        if (cancelled) return;
        setCode(s.content || "");
        setStart(s.start_line || c.start_line || 1);
      })
      .catch(() => !cancelled && setErr(true))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, rel, c.start_line, c.end_line, c.content]);

  const gh = ghUrl(repo, commit, c, rel);
  return (
    <div className={`frag ${active ? "active" : ""}`}>
      <div className="frag-head mono">
        {gh ? (
          <a href={gh} target="_blank" rel="noreferrer" title="Open on GitHub">
            <span className="frag-name">{c.node_name || rel}</span>
            <span className="frag-loc">
              {rel}
              {c.start_line != null ? `:${c.start_line}-${c.end_line}` : ""} ↗
            </span>
          </a>
        ) : (
          <span className="frag-name">{c.node_name || rel}</span>
        )}
      </div>
      {loading ? (
        <p className="muted frag-msg">Loading…</p>
      ) : err || !code ? (
        <p className="muted frag-msg">Source not available.</p>
      ) : (
        <HighlightedCode code={code} file={rel} startLine={start} />
      )}
    </div>
  );
}

/**
 * DeepWiki-style code pane: the relevant code shown as a vertical stack of
 * fragments (the cited line ranges, syntax-highlighted with real line numbers),
 * with a sticky nav bar of references at the TOP. Clicking a reference scrolls
 * to and highlights its fragment.
 */
export default function CodePanel({
  repoId,
  citations,
  repo,
  commit,
}: {
  repoId: string;
  citations: Citation[];
  repo?: string;
  commit?: string;
}) {
  const refs = citations.filter((c) => repoRelative(c.file)).slice(0, 12);
  const [active, setActive] = useState(0);
  const fragEls = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    setActive(0);
    fragEls.current = [];
  }, [citations]);

  function jump(i: number) {
    setActive(i);
    fragEls.current[i]?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  if (refs.length === 0) {
    return (
      <div className="codepane codepane-empty">
        <p className="muted">No code references for this answer yet.</p>
      </div>
    );
  }

  return (
    <div className="codepane">
      <div className="codepane-nav" role="tablist">
        {refs.map((c, i) => (
          <button
            key={i}
            role="tab"
            aria-selected={i === active}
            className={`codepane-tab ${i === active ? "active" : ""}`}
            title={`${c.node_name || ""} ${repoRelative(c.file)}`}
            onClick={() => jump(i)}
          >
            {(c.node_name || repoRelative(c.file).split("/").pop() || "").split(":").pop()}
          </button>
        ))}
      </div>
      <div className="codepane-body">
        {refs.map((c, i) => (
          <div
            key={i}
            ref={(el) => {
              fragEls.current[i] = el;
            }}
            onMouseEnter={() => setActive(i)}
          >
            <Fragment repoId={repoId} c={c} repo={repo} commit={commit} active={i === active} />
          </div>
        ))}
      </div>
    </div>
  );
}
