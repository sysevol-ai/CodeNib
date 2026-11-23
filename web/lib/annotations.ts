/**
 * Wiki annotations: questions, change requests and notes a person leaves on a
 * page, the tasks that send them to their coding agent, and the answers that
 * come back. Only a local Wiki started by `codenib wiki` has them; its
 * terminal link carries a session key (`?session=`) that every call sends.
 */

import { API_BASE } from "./api";

export type HumanKind = "question" | "change" | "note";
export type AnnotationKind = HumanKind | "answer" | "summary";
export type AnnotationStatus =
  | "draft"
  | "open"
  | "dispatched"
  | "answered"
  | "needs_review"
  | "applied"
  | "rejected"
  | "failed"
  | "resolved";

export interface AnnotationAnchor {
  page?: string;
  section?: string;
  quote?: string;
  prefix?: string;
  suffix?: string;
  block?: string;
  path?: string;
  start_line?: number;
  end_line?: number;
}

export interface CodeRef {
  path: string;
  start_line?: number;
  end_line?: number;
}

export interface Annotation {
  id: string;
  kind: AnnotationKind;
  text: string;
  author: "human" | "agent";
  status: AnnotationStatus;
  on: AnnotationAnchor;
  reply_to: string | null;
  task: string | null;
  files: CodeRef[];
  created: string;
  updated: string;
  /** Where the code it sits on stands now (code annotations only). */
  anchor_state?: "current" | "moved" | "changed" | "detached" | null;
}

export interface TaskUsage {
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  turns?: number;
  duration_ms?: number;
}

export interface AgentTask {
  id: string;
  inputs: string[];
  kind: "question" | "change";
  agent: string;
  status: "queued" | "running" | "needs_review" | "done" | "rejected" | "failed";
  created: string;
  started: string;
  finished: string;
  error: string;
  results: string[];
  attempts: number;
  diff: {
    branch?: string;
    base?: string;
    accepted?: string[];
    rejected?: string[];
    files?: { path: string; added: number; deleted: number }[];
    pages?: { id: string; title?: string; files?: string[] }[];
  };
  usage: TaskUsage;
  progress: string;
}

export interface AgentInfo {
  id: string;
  name: string;
  available: boolean;
  install: string;
}

export interface AnnotationState {
  annotations: Annotation[];
  tasks: AgentTask[];
  agents: AgentInfo[];
}

const SESSION_KEY = "codenib-local-session";
let session: string | null = null;

/** Take the session key from the address once, then keep it for this origin. */
export function localSession(): string | null {
  if (session !== null) return session || null;
  if (typeof window === "undefined") return null;
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("session");
  if (fromUrl) {
    session = fromUrl;
    try {
      window.localStorage.setItem(SESSION_KEY, fromUrl);
    } catch {
      // Private windows may refuse storage; this tab still has the key.
    }
    url.searchParams.delete("session");
    window.history.replaceState(window.history.state, "", url.toString());
    return session;
  }
  try {
    session = window.localStorage.getItem(SESSION_KEY) ?? "";
  } catch {
    session = "";
  }
  return session || null;
}

export class AnnotationApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const key = localSession();
  const headers = new Headers(init.headers);
  if (key) headers.set("X-CodeNib-Session", key);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = "The request was not valid";
    } catch {
      // Keep the status code as the message.
    }
    throw new AnnotationApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

const repoPath = (repoId: string) => `/api/repos/${encodeURIComponent(repoId)}`;

export function fetchAnnotations(repoId: string, page?: string): Promise<AnnotationState> {
  const query = page ? `?page=${encodeURIComponent(page)}` : "";
  return call(`${repoPath(repoId)}/annotations${query}`);
}

export function createAnnotation(
  repoId: string,
  body: { kind: HumanKind; text: string; on: AnnotationAnchor; reply_to?: string },
): Promise<Annotation> {
  return call(`${repoPath(repoId)}/annotations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateAnnotation(
  repoId: string,
  id: string,
  body: { text?: string; kind?: HumanKind; status?: "draft" | "open" | "resolved" },
): Promise<Annotation> {
  return call(`${repoPath(repoId)}/annotations/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteAnnotation(repoId: string, id: string): Promise<{ deleted: string }> {
  return call(`${repoPath(repoId)}/annotations/${id}`, { method: "DELETE" });
}

export function dispatchTask(repoId: string, annotations: string[]): Promise<AgentTask> {
  return call(`${repoPath(repoId)}/tasks`, {
    method: "POST",
    body: JSON.stringify({ annotations, agent: "claude-code" }),
  });
}

export interface DiffHunk {
  id: string;
  path: string;
  header: string;
  added: number;
  deleted: number;
  state: "open" | "accepted" | "rejected";
  lines: string[];
}

export function fetchTaskDiff(
  repoId: string,
  taskId: string,
): Promise<{ patch: string; hunks: DiffHunk[] }> {
  return call(`${repoPath(repoId)}/tasks/${taskId}/diff`);
}

/** Accept or discard the hunks named, or every undecided hunk. */
export function decideTask(
  repoId: string,
  taskId: string,
  decision: "accept" | "reject",
  hunks?: string[],
): Promise<AgentTask> {
  return call(`${repoPath(repoId)}/tasks/${taskId}/${decision}`, {
    method: "POST",
    body: JSON.stringify({ hunks: hunks ?? [] }),
  });
}

// --- Anchoring -------------------------------------------------------------
//
// An annotation remembers the words a person selected, a little text on
// either side, the section heading above them and the passage they sit in.
// Finding it again walks the page's text with whitespace collapsed, so a
// re-rendered page or a reflowed paragraph still matches.

const BLOCK_SELECTOR = "p, li, h1, h2, h3, h4, h5, h6, blockquote, pre, td, th, figcaption, dd, dt";
const HEADING_SELECTOR = "h1, h2, h3, h4";
const CONTEXT = 40;

export const squash = (text: string) => text.replace(/\s+/g, " ").trim();

interface FlatText {
  text: string;
  nodes: { node: Text; offsets: number[] }[];
  /** For each character of `text`, which node and offset it came from. */
  map: [number, number][];
}

function readable(node: Text, root: Element): boolean {
  let element = node.parentElement;
  while (element && element !== root) {
    if (element.closest("[data-annotation-ui]")) return false;
    const tag = element.tagName;
    if (tag === "SCRIPT" || tag === "STYLE" || tag === "svg" || tag === "SVG") return false;
    element = element.parentElement;
  }
  return true;
}

function flatten(root: Element): FlatText {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const flat: FlatText = { text: "", nodes: [], map: [] };
  let lastSpace = true;
  for (let current = walker.nextNode(); current; current = walker.nextNode()) {
    const node = current as Text;
    if (!readable(node, root)) continue;
    const index = flat.nodes.length;
    flat.nodes.push({ node, offsets: [] });
    const data = node.data;
    for (let offset = 0; offset < data.length; offset += 1) {
      const char = data[offset];
      if (/\s/.test(char)) {
        if (lastSpace) continue;
        flat.text += " ";
        lastSpace = true;
      } else {
        flat.text += char;
        lastSpace = false;
      }
      flat.map.push([index, offset]);
    }
  }
  return flat;
}

function blockOf(node: Node, root: Element): Element {
  const element = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
  const block = element?.closest(BLOCK_SELECTOR);
  return block && root.contains(block) ? block : root;
}

function sectionOf(block: Element, root: Element): string {
  let heading = "";
  for (const candidate of Array.from(root.querySelectorAll(HEADING_SELECTOR))) {
    if (candidate === block || candidate.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING) {
      heading = squash(candidate.textContent ?? "");
    } else {
      break;
    }
  }
  return heading;
}

/** Describe a selection inside the page, or null when it is not one. */
export function anchorFromSelection(
  root: Element,
  range: Range,
  page: string,
): AnnotationAnchor | null {
  if (range.collapsed || !root.contains(range.commonAncestorContainer)) return null;
  const quote = squash(range.toString());
  if (quote.length < 2) return null;
  const block = blockOf(range.startContainer, root);
  const before = document.createRange();
  before.selectNodeContents(block);
  before.setEnd(range.startContainer, range.startOffset);
  const after = document.createRange();
  after.selectNodeContents(block);
  after.setStart(range.endContainer, range.endOffset);
  const prefix = before.toString().replace(/\s+/g, " ").slice(-CONTEXT).trimStart();
  const suffix = after.toString().replace(/\s+/g, " ").slice(0, CONTEXT).trimEnd();
  return {
    page,
    section: sectionOf(block, root),
    quote: quote.slice(0, 2000),
    prefix,
    suffix,
    block: squash(block.textContent ?? "").slice(0, 1500),
  };
}

/** The words an anchor names, as a DOM range in the page, or null. */
export function findAnchor(root: Element, anchor: AnnotationAnchor): Range | null {
  const quote = squash(anchor.quote ?? "");
  if (!quote) return null;
  const flat = flatten(root);
  let best = -1;
  let bestScore = -1;
  const prefix = squash(anchor.prefix ?? "");
  const suffix = squash(anchor.suffix ?? "");
  for (let at = flat.text.indexOf(quote); at !== -1; at = flat.text.indexOf(quote, at + 1)) {
    let score = 0;
    if (prefix && flat.text.slice(Math.max(0, at - prefix.length - 1), at).includes(prefix.slice(-12))) {
      score += 2;
    }
    if (suffix && flat.text.slice(at + quote.length, at + quote.length + suffix.length + 1).includes(suffix.slice(0, 12))) {
      score += 2;
    }
    if (score > bestScore) {
      best = at;
      bestScore = score;
    }
  }
  if (best === -1) return null;
  const start = flat.map[best];
  const end = flat.map[best + quote.length - 1];
  if (!start || !end) return null;
  const range = document.createRange();
  range.setStart(flat.nodes[start[0]].node, start[1]);
  range.setEnd(flat.nodes[end[0]].node, end[1] + 1);
  return range;
}

/**
 * Describe a selection inside a source fragment: its file, the lines it
 * covers (read from the fragment's gutter) and the selected code.
 */
export function anchorFromCodeSelection(range: Range, page: string): AnnotationAnchor | null {
  if (range.collapsed) return null;
  const start = range.startContainer.parentElement?.closest(".hl-code");
  const end = range.endContainer.parentElement?.closest(".hl-code");
  if (!start || start !== end) return null;
  const code = start.querySelector("pre.hl-pre code");
  const path = start.closest<HTMLElement>("[data-path]")?.dataset.path;
  if (!code || !path || !code.contains(range.startContainer)) return null;
  const quote = range.toString().replace(/\s+$/, "");
  if (!quote.trim()) return null;
  const rowOf = (node: Node, offset: number) => {
    const before = document.createRange();
    before.selectNodeContents(code);
    before.setEnd(node, offset);
    return before.toString().split("\n").length - 1;
  };
  const numbers = Array.from(start.querySelectorAll(".hl-gutter > div")).map((row) => {
    const value = Number.parseInt(row.textContent ?? "", 10);
    return Number.isFinite(value) ? value : null;
  });
  let first = rowOf(range.startContainer, range.startOffset);
  let last = rowOf(range.endContainer, range.endOffset);
  // A selection that ends at the start of a line does not cover that line.
  if (last > first && range.toString().endsWith("\n")) last -= 1;
  while (first < numbers.length && numbers[first] == null) first += 1;
  while (last >= 0 && numbers[last] == null) last -= 1;
  const startLine = numbers[first];
  const endLine = numbers[last];
  if (startLine == null || endLine == null || endLine < startLine) return null;
  return {
    page,
    path,
    start_line: startLine,
    end_line: endLine,
    quote: quote.slice(0, 2000),
  };
}

// Words too common to say two passages are about the same thing.
const STOPWORDS = new Set(
  "a an and are as at be by for from has in into is it its of on or that the then this to was were which with".split(" "),
);

const words = (text: string) =>
  new Set(
    (text.toLowerCase().match(/[\p{L}\p{N}_]+/gu) ?? []).filter(
      (word) => word.length > 1 && !STOPWORDS.has(word),
    ),
  );

/** How alike two passages are: the Dice overlap of their words (0 to 1). */
export function passageSimilarity(a: string, b: string): number {
  const left = words(a);
  const right = words(b);
  if (!left.size || !right.size) return 0;
  let shared = 0;
  for (const word of left) if (right.has(word)) shared += 1;
  return (2 * shared) / (left.size + right.size);
}

/**
 * Find an anchor's words; when the page was rewritten and they are gone,
 * fall back to the passage most like the one the person annotated.
 */
export function placeAnchor(
  root: Element,
  anchor: AnnotationAnchor,
): { range: Range; moved: boolean } | null {
  const exact = findAnchor(root, anchor);
  if (exact) return { range: exact, moved: false };
  const before = squash(anchor.block || anchor.quote || "");
  if (!before) return null;
  let best: Element | null = null;
  let score = 0;
  for (const block of Array.from(root.querySelectorAll(BLOCK_SELECTOR))) {
    if (block.querySelector(BLOCK_SELECTOR)) continue; // the innermost passages only
    const similarity = passageSimilarity(before, block.textContent ?? "");
    if (similarity > score) {
      best = block;
      score = similarity;
    }
  }
  if (!best || score < 0.5) return null;
  const range = document.createRange();
  range.selectNodeContents(best);
  return { range, moved: true };
}

/** The block element a found range sits in, to scroll to or align beside. */
export function anchorBlock(root: Element, range: Range): Element {
  return blockOf(range.startContainer, root);
}

// --- Threads ---------------------------------------------------------------

export interface Thread {
  root: Annotation;
  replies: Annotation[];
}

/** Group a page's annotations into threads: each root with its replies. */
export function threadsOf(annotations: Annotation[]): Thread[] {
  const byId = new Map(annotations.map((a) => [a.id, a]));
  const rootOf = (annotation: Annotation): Annotation => {
    let current = annotation;
    const seen = new Set<string>();
    while (current.reply_to && byId.has(current.reply_to) && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.reply_to)!;
    }
    return current;
  };
  const threads = new Map<string, Thread>();
  for (const annotation of annotations) {
    const root = rootOf(annotation);
    const thread = threads.get(root.id) ?? { root, replies: [] };
    if (annotation.id !== root.id) thread.replies.push(annotation);
    threads.set(root.id, thread);
  }
  for (const thread of threads.values()) {
    thread.replies.sort((a, b) => a.created.localeCompare(b.created) || a.id.localeCompare(b.id));
  }
  return [...threads.values()];
}

const CODE_REF = /`?((?:[\w.-]+\/)*[\w-][\w.-]*\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?`?/g;

/**
 * Turn `path:start-end` in an agent's answer into the Wiki's own evidence
 * links, so each opens the source panel like a citation on the page.
 */
export function linkCodeRefs(text: string): { markdown: string; refs: CodeRef[] } {
  const refs: CodeRef[] = [];
  const seen = new Map<string, number>();
  const markdown = text
    .split(/(```[\s\S]*?```)/g)
    .map((part) =>
      part.startsWith("```")
        ? part
        : part.replace(CODE_REF, (_whole, path: string, start: string, end?: string) => {
            const key = `${path}:${start}-${end ?? start}`;
            let index = seen.get(key);
            if (index === undefined) {
              index = refs.length;
              refs.push({ path, start_line: Number(start), end_line: Number(end ?? start) });
              seen.set(key, index);
            }
            return `[E${index + 1}](#evidence-E${index + 1})`;
          }),
    )
    .join("");
  return { markdown, refs };
}

export function isLive(task: AgentTask | undefined): boolean {
  return !!task && (task.status === "queued" || task.status === "running");
}

export function usageLine(usage: TaskUsage): string {
  const parts: string[] = [];
  if (usage.turns) parts.push(`${usage.turns} turns`);
  const tokens = (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
  if (tokens) parts.push(`${tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : tokens} tokens`);
  if (usage.cost_usd) parts.push(`$${usage.cost_usd.toFixed(usage.cost_usd < 0.1 ? 3 : 2)}`);
  if (usage.duration_ms) parts.push(`${Math.round(usage.duration_ms / 1000)} s`);
  return parts.join(" · ");
}

// --- People write the Wiki ------------------------------------------------

export interface PageSource {
  markdown: string;
  /** The version the editor opened; saving refuses a page changed since. */
  base: string;
  /** "document": the repository file itself; "page": a Wiki page edit. */
  kind: "document" | "page";
  path: string;
  edited: boolean;
}

const pagePath = (repoId: string, pageId: string) =>
  `${repoPath(repoId)}/pages/${encodeURIComponent(pageId)}`;

export function fetchPageSource(repoId: string, pageId: string): Promise<PageSource> {
  return call(`${pagePath(repoId, pageId)}/markdown`);
}

export function savePageSource(
  repoId: string,
  pageId: string,
  markdown: string,
  base: string,
): Promise<PageSource> {
  return call(`${pagePath(repoId, pageId)}/markdown`, {
    method: "PUT",
    body: JSON.stringify({ markdown, base }),
  });
}

export function revertPage(repoId: string, pageId: string): Promise<{ reverted: boolean }> {
  return call(`${pagePath(repoId, pageId)}/markdown`, { method: "DELETE" });
}

export function foldAnswer(repoId: string, pageId: string, annotation: string): Promise<PageSource> {
  return call(`${pagePath(repoId, pageId)}/fold`, {
    method: "POST",
    body: JSON.stringify({ annotation }),
  });
}
