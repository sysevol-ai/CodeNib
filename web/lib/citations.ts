import { repoRelative, type Citation } from "./api";

/** Filtered, index-stable citation list both panes share.
 *  Backend API citations are already 1-based for display + /source lookup. */
export function codeRefs(citations: Citation[]): Citation[] {
  return citations.filter((c) => repoRelative(c.file)).slice(0, 12);
}

function norm(s: string): string {
  return s.trim().toLowerCase();
}

function basename(p: string): string {
  return p.split("/").pop() || p;
}

/** A match: its index in {@link codeRefs}, and whether the span named a symbol
 *  (has a real line range) or a file (ambiguous — no range shown). */
export interface CiteMatch {
  index: number;
  kind: "symbol" | "file";
}

/**
 * Map an inline `code` span to the citation it names, or null. Conservative so
 * ordinary inline code (`Wald`, `[0, 1]`) stays plain; only a clear symbol or
 * file matches. Symbol matches carry a line range; file matches don't.
 */
export function matchCitation(raw: string, refs: Citation[]): CiteMatch | null {
  const text = raw.trim().replace(/\(\)$/, "");
  if (text.length < 2) return null;
  const t = norm(text);

  const rels = refs.map((c) => repoRelative(c.file));

  // exact symbol
  for (let i = 0; i < refs.length; i++) {
    const n = refs[i].node_name;
    if (n && norm(n.replace(/\(\)$/, "")) === t) return { index: i, kind: "symbol" };
  }
  // exact repo-relative path
  for (let i = 0; i < refs.length; i++) {
    if (rels[i] && norm(rels[i]) === t) return { index: i, kind: "file" };
  }
  // file basename
  if (/\.[a-z0-9]+$/i.test(text)) {
    for (let i = 0; i < refs.length; i++) {
      if (rels[i] && norm(basename(rels[i])) === t) return { index: i, kind: "file" };
    }
  }
  // last segment of a qualified symbol ("file.py:sym()"); >=3 chars avoids "py"
  if (t.length >= 3) {
    for (let i = 0; i < refs.length; i++) {
      const n = refs[i].node_name;
      if (n) {
        const seg = n.replace(/\(\)$/, "").split(/[/.:#]/).pop();
        if (seg && norm(seg) === t) return { index: i, kind: "symbol" };
      }
    }
  }
  // partial path suffix (needs a slash)
  if (text.includes("/")) {
    for (let i = 0; i < refs.length; i++) {
      const r = rels[i] ? norm(rels[i]) : "";
      if (r && (r.endsWith(t) || t.endsWith(r))) return { index: i, kind: "file" };
    }
  }
  // path:line form
  const m = text.match(/^(.+?):(\d+)/);
  if (m) {
    const file = norm(m[1]);
    for (let i = 0; i < refs.length; i++) {
      const r = rels[i] ? norm(rels[i]) : "";
      if (r && (r === file || norm(basename(r)) === norm(basename(m[1]))))
        return { index: i, kind: "file" };
    }
  }
  return null;
}

/** "843-851" / "843" / "" — the line-range badge shown on a chip. */
export function lineLabel(c: Citation): string {
  if (c.start_line == null) return "";
  if (c.end_line == null || c.end_line === c.start_line) return String(c.start_line);
  return `${c.start_line}-${c.end_line}`;
}
