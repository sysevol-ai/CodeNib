"use client";

import { useEffect, useState } from "react";
import hljs from "highlight.js";
import { fetchSource } from "@/lib/api";

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
  yml: "yaml",
  yaml: "yaml",
};
const MAX_LINES = 1500;

/**
 * DeepWiki-style code window: a tabbed, scrollable source viewer. Each
 * relevant file is a tab along the bottom; the active file is shown in full
 * (scrollable) with syntax highlighting and a line-number gutter.
 */
export default function CodePanel({
  repoId,
  files,
  activeFile,
  onPick,
}: {
  repoId: string;
  files: string[];
  activeFile: string | null;
  onPick: (file: string) => void;
}) {
  const [code, setCode] = useState("");
  const [start, setStart] = useState(1);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!activeFile) {
      setCode("");
      return;
    }
    let cancelled = false;
    setLoading(true);
    setUnavailable(false);
    fetchSource(repoId, activeFile)
      .then((res) => {
        if (cancelled) return;
        setCode(res.content || "");
        setStart(res.start_line || 1);
      })
      .catch(() => {
        if (!cancelled) setUnavailable(true);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repoId, activeFile]);

  if (!activeFile) {
    return (
      <div className="codepanel codepanel-empty">
        <p className="muted">Select a reference on the left to view its code.</p>
      </div>
    );
  }

  const ext = activeFile.split(".").pop()?.toLowerCase() || "";
  const lang = EXT_LANG[ext];
  const allLines = code.split("\n");
  const clipped = allLines.length > MAX_LINES;
  const shown = clipped ? allLines.slice(0, MAX_LINES).join("\n") : code;
  let highlighted = "";
  try {
    highlighted = lang
      ? hljs.highlight(shown, { language: lang }).value
      : hljs.highlightAuto(shown).value;
  } catch {
    highlighted = shown.replace(
      /[&<>]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!
    );
  }
  const lineCount = clipped ? MAX_LINES : allLines.length;

  return (
    <div className="codepanel">
      <div className="codepanel-head mono">
        <span className="codepanel-name">{activeFile}</span>
        <span className="codepanel-loc">
          {unavailable ? "not found" : `${allLines.length} lines`}
        </span>
      </div>

      <div className="codepanel-body">
        {loading ? (
          <p className="muted codepanel-loading">Loading source…</p>
        ) : unavailable ? (
          <p className="muted codepanel-loading">Source not available for this file.</p>
        ) : (
          <div className="codepanel-scroll">
            <div className="codepanel-gutter" aria-hidden>
              {Array.from({ length: lineCount }, (_, i) => (
                <div key={i}>{start + i}</div>
              ))}
              {clipped && <div>…</div>}
            </div>
            <pre className="codepanel-pre">
              <code className="hljs" dangerouslySetInnerHTML={{ __html: highlighted }} />
              {clipped && <div className="codepanel-clip">… {allLines.length - MAX_LINES} more lines</div>}
            </pre>
          </div>
        )}
      </div>

      {files.length > 1 && (
        <div className="codepanel-tabs" role="tablist">
          {files.map((f) => (
            <button
              key={f}
              role="tab"
              aria-selected={f === activeFile}
              className={`codepanel-tab ${f === activeFile ? "active" : ""}`}
              title={f}
              onClick={() => onPick(f)}
            >
              {f.split("/").pop()}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
