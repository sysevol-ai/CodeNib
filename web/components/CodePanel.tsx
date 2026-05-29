"use client";

import { useEffect, useState } from "react";
import hljs from "highlight.js";
import { fetchSource, type Citation } from "@/lib/api";

const EXT_LANG: Record<string, string> = {
  py: "python",
  go: "go",
  rs: "rust",
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  c: "c",
  h: "cpp",
  cc: "cpp",
  cpp: "cpp",
  hpp: "cpp",
  java: "java",
  rb: "ruby",
  json: "json",
};

/**
 * DeepWiki-style code pane: shows the source behind the selected reference,
 * syntax-highlighted with a line-number gutter (1-based, repo-true lines).
 */
export default function CodePanel({
  repoId,
  citation,
}: {
  repoId: string;
  citation: Citation | null;
}) {
  const [code, setCode] = useState("");
  const [start, setStart] = useState(1);
  const [loading, setLoading] = useState(false);

  const file = citation?.file ?? null;
  const s = citation?.start_line ?? null;
  const e = citation?.end_line ?? null;

  useEffect(() => {
    if (!file) {
      setCode("");
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetchSource(repoId, file, s ?? undefined, e ?? undefined)
      .then((res) => {
        if (cancelled) return;
        setCode(res.content || "");
        setStart(res.start_line || s || 1);
      })
      .catch(() => !cancelled && setCode("// source unavailable"))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, file, s, e]);

  if (!citation) {
    return (
      <div className="codepanel codepanel-empty">
        <p className="muted">Select a reference on the left to view its code.</p>
      </div>
    );
  }

  const ext = (file || "").split(".").pop()?.toLowerCase() || "";
  const lang = EXT_LANG[ext];
  let highlighted = "";
  try {
    highlighted = lang
      ? hljs.highlight(code, { language: lang }).value
      : hljs.highlightAuto(code).value;
  } catch {
    highlighted = code.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);
  }
  const lineCount = code ? code.split("\n").length : 0;

  return (
    <div className="codepanel">
      <div className="codepanel-head mono">
        <span className="codepanel-name">{citation.node_name || file}</span>
        <span className="codepanel-loc">
          {file}:{s}-{e}
        </span>
      </div>
      <div className="codepanel-body">
        {loading ? (
          <p className="muted codepanel-loading">Loading source…</p>
        ) : (
          <div className="codepanel-scroll">
            <div className="codepanel-gutter" aria-hidden>
              {Array.from({ length: lineCount }, (_, i) => (
                <div key={i}>{start + i}</div>
              ))}
            </div>
            <pre className="codepanel-pre">
              <code className="hljs" dangerouslySetInnerHTML={{ __html: highlighted }} />
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
