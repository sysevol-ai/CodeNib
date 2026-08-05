"use client";

import { useState } from "react";
import HighlightedCode from "@/components/HighlightedCode";
import { fetchSource, repoRelative, type Citation } from "@/lib/api";

/** A code reference backing an answer / wiki page; expands to show the source. */
export default function CitationItem({ repoId, c }: { repoId: string; c: Citation }) {
  const [src, setSrc] = useState<string | null>(c.content ?? null);
  const [open, setOpen] = useState(false);
  const rel = repoRelative(c.file);
  async function toggle() {
    if (!open && src == null && rel) {
      try {
        const s = await fetchSource(
          repoId,
          rel,
          c.start_line ?? undefined,
          c.end_line ?? undefined
        );
        setSrc(s.content);
      } catch {
        setSrc("// source unavailable");
      }
    }
    setOpen((o) => !o);
  }
  return (
    <div className="citation">
      <button className="head" onClick={toggle} aria-expanded={open}>
        {c.node_name ? `${c.node_name} — ` : ""}
        {rel}{c.start_line != null ? `:${c.start_line}-${c.end_line}` : ""}
        <span className="cite-toggle">{open ? "▾ source" : "▸ view source"}</span>
      </button>
      {open && (
        <HighlightedCode
          code={src ?? c.content ?? ""}
          file={rel}
          startLine={c.start_line ?? 1}
        />
      )}
    </div>
  );
}
