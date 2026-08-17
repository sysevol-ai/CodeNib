"use client";

import { useEffect, useRef, useState } from "react";
import HighlightedCode from "@/components/HighlightedCode";
import { fetchSource, repoRelative, type Citation } from "@/lib/api";
import { codeRefs } from "@/lib/citations";
import { sourceIndexAtThreshold } from "@/lib/sourceNavigation";

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
  onSelect,
  onVisibleChange,
  scrollSignal,
}: {
  repoId: string;
  citations: Citation[];
  repo?: string;
  commit?: string;
  /** Controlled active fragment, so prose chips can drive the pane. */
  active?: number;
  onSelect?: (i: number) => void;
  /** Scroll-spy update; unlike a tab click, this must not initiate a scroll. */
  onVisibleChange?: (i: number) => void;
  /** Bumped by the parent on every selection; each bump re-scrolls to active. */
  scrollSignal?: number;
}) {
  const refs = codeRefs(citations);
  const [internal, setInternal] = useState(0);
  const requestedActive = activeProp ?? internal;
  const active = refs.length
    ? Math.max(0, Math.min(requestedActive, refs.length - 1))
    : 0;
  const fragEls = useRef<(HTMLDivElement | null)[]>([]);
  const bodyEl = useRef<HTMLDivElement | null>(null);
  const navEl = useRef<HTMLDivElement | null>(null);
  const tabEls = useRef<(HTMLButtonElement | null)[]>([]);
  const scrollSpyFrame = useRef<number | null>(null);
  const programmaticScroll = useRef(false);
  const scrollEndTimer = useRef<number | null>(null);
  const citationKey = refs
    .map((c) => `${repoRelative(c.file)}:${c.start_line ?? ""}-${c.end_line ?? ""}`)
    .join("|");

  useEffect(() => {
    programmaticScroll.current = false;
    bodyEl.current?.scrollTo({ top: 0, left: 0 });
  }, [citationKey]);

  useEffect(
    () => () => {
      if (scrollSpyFrame.current != null) {
        cancelAnimationFrame(scrollSpyFrame.current);
      }
      if (scrollEndTimer.current != null) {
        window.clearTimeout(scrollEndTimer.current);
      }
    },
    [],
  );

  const revealActiveTab = (index: number) => {
    const nav = navEl.current;
    const tab = tabEls.current[index];
    if (!nav || !tab) return;
    const navRect = nav.getBoundingClientRect();
    const tabRect = tab.getBoundingClientRect();
    let delta = 0;
    if (tabRect.left < navRect.left) delta = tabRect.left - navRect.left - 8;
    else if (tabRect.right > navRect.right) delta = tabRect.right - navRect.right + 8;
    if (delta) nav.scrollTo({ left: nav.scrollLeft + delta, behavior: "smooth" });
  };

  useEffect(() => {
    revealActiveTab(active);
  }, [active, citationKey]);

  const reportVisibleFragment = (index: number) => {
    if (index < 0 || index === active) return;
    if (onVisibleChange) onVisibleChange(index);
    else if (activeProp == null) setInternal(index);
  };

  const syncVisibleFragment = () => {
    const body = bodyEl.current;
    if (!body) return;
    const bodyRect = body.getBoundingClientRect();
    const maxScroll = Math.max(0, body.scrollHeight - body.clientHeight);
    const remainingScroll = Math.max(0, maxScroll - body.scrollTop);
    const atEnd = maxScroll > 2 && remainingScroll <= 2;
    const baseOffset = Math.min(72, bodyRect.height * 0.18);
    // Keep the reading marker near the top for most of the document. During
    // the final viewport, let it move down so short tail fragments can become
    // active without manufacturing visible padding below the source.
    const tailOffset = Math.min(
      bodyRect.height * 0.68,
      baseOffset + Math.max(0, bodyRect.height - remainingScroll),
    );
    const threshold = bodyRect.top + tailOffset;
    const index = sourceIndexAtThreshold(
      fragEls.current.slice(0, refs.length).map((element) =>
        element ? element.getBoundingClientRect().top : Number.POSITIVE_INFINITY,
      ),
      threshold,
      atEnd,
    );
    reportVisibleFragment(index);
  };

  const finishProgrammaticScroll = () => {
    if (scrollEndTimer.current != null) {
      window.clearTimeout(scrollEndTimer.current);
    }
    scrollEndTimer.current = window.setTimeout(() => {
      programmaticScroll.current = false;
    }, 140);
  };

  const handleBodyScroll = () => {
    if (programmaticScroll.current) {
      finishProgrammaticScroll();
      return;
    }
    if (scrollSpyFrame.current != null) return;
    scrollSpyFrame.current = requestAnimationFrame(() => {
      scrollSpyFrame.current = null;
      syncVisibleFragment();
    });
  };

  const takeOverScroll = () => {
    programmaticScroll.current = false;
    if (scrollEndTimer.current != null) {
      window.clearTimeout(scrollEndTimer.current);
      scrollEndTimer.current = null;
    }
  };

  const scrollToFrag = (i: number) => {
    const body = bodyEl.current;
    const target = fragEls.current[i];
    if (!body || !target) return;

    // Keep navigation inside the code pane. Element.scrollIntoView() also
    // scrolls outer ancestors, which made the sticky answer column and its
    // tabs slide across the source while selecting a reference.
    const bodyRect = body.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const top = Math.max(0, body.scrollTop + targetRect.top - bodyRect.top);
    if (Math.abs(body.scrollTop - top) < 1) return;
    programmaticScroll.current = true;
    body.scrollTo({
      top,
      behavior: "smooth",
    });
    finishProgrammaticScroll();
  };

  // Scroll on every selection, even re-selecting the same fragment after the
  // user scrolled away. Keyed on scrollSignal (not active) so it always fires.
  useEffect(() => {
    scrollToFrag(active);
  }, [scrollSignal]);

  function select(i: number) {
    if (onSelect) onSelect(i);
    else {
      setInternal(i);
      scrollToFrag(i);
    }
  }

  if (refs.length === 0) {
    return (
      <div className="codepane codepane-empty">
        <p className="muted">No verified source range is available for this answer.</p>
      </div>
    );
  }

  return (
    <div className="codepane">
      <div className="codepane-nav" role="tablist" ref={navEl}>
        {refs.map((c, i) => (
          <button
            key={i}
            ref={(element) => {
              tabEls.current[i] = element;
            }}
            role="tab"
            aria-selected={i === active}
            className={`codepane-tab ${i === active ? "active" : ""}`}
            title={`${c.node_name || ""} ${repoRelative(c.file)}`}
            onClick={() => select(i)}
          >
            {(c.node_name || repoRelative(c.file).split("/").pop() || "").split(":").pop()}
          </button>
        ))}
      </div>
      <div
        className="codepane-body"
        ref={bodyEl}
        onScroll={handleBodyScroll}
        onWheel={takeOverScroll}
        onTouchStart={takeOverScroll}
        onPointerDown={takeOverScroll}
      >
        {refs.map((c, i) => (
          <div
            key={i}
            ref={(el) => {
              fragEls.current[i] = el;
            }}
          >
            <Fragment repoId={repoId} c={c} repo={repo} commit={commit} active={i === active} />
          </div>
        ))}
      </div>
    </div>
  );
}
