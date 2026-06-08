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
    // Prefer clean /source; the citation `content` is line-number-wrapped for the
    // LLM and would double the gutter. Fall back to it only if the fetch fails.
    if (rel && c.start_line != null) {
      fetchSource(repoId, rel, c.start_line, c.end_line ?? undefined)
        .then((s) => {
          if (cancelled) return;
          setCode(s.content || "");
          setStart(s.start_line || c.start_line || 1);
        })
        .catch(() => {
          if (cancelled) return;
          if (c.content) {
            setCode(c.content);
            setStart(c.start_line ?? 1);
          } else setErr(true);
        })
        .finally(() => !cancelled && setLoading(false));
    } else if (c.content) {
      setCode(c.content);
      setStart(c.start_line ?? 1);
      setLoading(false);
    } else {
      setErr(true);
      setLoading(false);
    }
    return () => {
      cancelled = true;
    };
  }, [repoId, rel, c.start_line, c.end_line, c.content]);

  const gh = ghUrl(repo, commit, c, rel);
  // node_name is the qualified "file:symbol()" form; show just the symbol.
  const symbol = (c.node_name || "").split(":").pop() || rel.split("/").pop() || rel;
  const loc = `${rel}${c.start_line != null ? `:${c.start_line}-${c.end_line}` : ""}`;
  return (
    <div className={`frag ${active ? "active" : ""}`}>
      <div className="frag-head mono">
        <span className="frag-name">{symbol}</span>
        {gh ? (
          <a href={gh} target="_blank" rel="noreferrer" className="frag-loc" title="Open on GitHub">
            {loc} ↗
          </a>
        ) : (
          <span className="frag-loc">{loc}</span>
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
  active: activeProp,
  onActiveChange,
}: {
  repoId: string;
  citations: Citation[];
  repo?: string;
  commit?: string;
  /** Controlled active fragment, so prose chips can drive the pane. */
  active?: number;
  onActiveChange?: (i: number) => void;
}) {
  const refs = citations.filter((c) => repoRelative(c.file)).slice(0, 12);
  const [internal, setInternal] = useState(0);
  const active = activeProp ?? internal;
  const setActive = onActiveChange ?? setInternal;
  const fragEls = useRef<(HTMLDivElement | null)[]>([]);
  const didMount = useRef(false);

  useEffect(() => {
    fragEls.current = [];
  }, [citations]);

  // Scroll the active fragment into view on change (skip the first render).
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true;
      return;
    }
    fragEls.current[active]?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [active]);

  function jump(i: number) {
    setActive(i);
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
