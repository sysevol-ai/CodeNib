"use client";

import { useState } from "react";
import { fetchSource, type Citation } from "@/lib/api";

/** A code reference backing an answer / wiki page; expands to show the source. */
export default function CitationItem({ repoId, c }: { repoId: string; c: Citation }) {
  const [src, setSrc] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  async function toggle() {
    if (!open && src == null && c.file && c.start_line != null) {
      try {
        const s = await fetchSource(repoId, c.file, c.start_line, c.end_line ?? undefined);
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
        {c.file}:{c.start_line}-{c.end_line}
        <span className="cite-toggle">{open ? "▾ source" : "▸ view source"}</span>
      </button>
      {open && <pre>{src ?? c.content}</pre>}
    </div>
  );
}
