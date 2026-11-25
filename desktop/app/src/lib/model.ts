// What CodeNib's local service returns, as far as the window reads it.

export interface PageRef {
  id: string;
  title: string;
  children?: PageRef[];
}

export interface DocRef {
  id: string;
  title: string;
  path: string;
}

export interface Tree {
  repo: string;
  pages: PageRef[];
  docs?: DocRef[];
}

export interface RepoInfo {
  id: string;
  name: string;
  repo: string;
  commit_short?: string;
  description?: string;
  file_count?: number;
}

export interface Citation {
  file: string;
  start_line: number;
  end_line: number;
  node_name?: string;
  type?: string;
}

/** What a claim on a page rests on: E# is code, R# a recorded call. */
export interface EvidenceItem {
  id: string;
  file: string;
  start_line: number;
  end_line: number;
  symbol?: string;
}

export interface EvidenceRelation {
  id: string;
  source: string;
  target: string;
  anchors: string[];
}

export interface Page {
  id: string;
  title: string;
  markdown: string;
  citations?: Citation[];
  evidence?: { items?: EvidenceItem[]; relations?: EvidenceRelation[] };
  generation?: { mode?: string; model?: string | null; renderer?: string | null };
}

export type AnnotationKind = "question" | "change" | "note" | "answer" | "summary";

export interface Anchor {
  page?: string;
  section?: string;
  quote?: string;
  block?: string;
  suffix?: string;
  path?: string;
  start_line?: number;
  end_line?: number;
}

export interface Annotation {
  id: string;
  kind: AnnotationKind;
  text: string;
  author: string;
  status: string;
  on: Anchor;
  reply_to: string;
  task: string;
  created: string;
  updated: string;
}

export interface Task {
  id: string;
  /** The agent that ran it: "claude-code" or "codex". */
  agent?: string;
  inputs: string[];
  kind: "question" | "change";
  status: "queued" | "running" | "needs_review" | "done" | "rejected" | "failed";
  created: string;
  finished: string;
  error: string;
  results: string[];
  progress: string;
  diff?: { files?: string[]; stat?: string };
}

const AGENT_NAMES: Record<string, string> = { "claude-code": "Claude Code", codex: "Codex" };

/** "Claude Code" or "Codex" for an agent id. */
export function agentName(id: string | undefined | null): string {
  return AGENT_NAMES[id ?? ""] ?? "Claude Code";
}

export interface Agent {
  id: string;
  name: string;
  available: boolean;
  install?: string;
}

export interface NotesState {
  annotations: Annotation[];
  tasks: Task[];
  agents: Agent[];
  mode: string;
}

export interface Source {
  file: string;
  start_line: number;
  end_line: number;
  content: string;
}

/** A task as the person sees it: what it is waiting for. */
export type Need = "needs" | "working" | "done" | "failed";

export function taskNeed(task: Task, annotations: Annotation[]): Need {
  if (task.status === "queued" || task.status === "running") return "working";
  if (task.status === "failed") return "failed";
  if (task.status === "needs_review") return "needs";
  if (task.status === "done" && task.kind === "question") {
    // A new answer waits for the person until they resolve the question.
    const asked = annotations.filter((a) => task.inputs.includes(a.id));
    if (asked.some((a) => a.status === "answered")) return "needs";
  }
  return "done";
}

/** The words a task is about: its first input's text. */
export function taskTitle(task: Task, annotations: Annotation[]): string {
  const first = annotations.find((a) => task.inputs.includes(a.id));
  const text = first?.text ?? (task.kind === "change" ? "A change" : "A question");
  return text.length > 90 ? `${text.slice(0, 88)}…` : text;
}

export function flattenPages(pages: PageRef[], depth = 0): (PageRef & { depth: number })[] {
  return pages.flatMap((p) => [{ ...p, depth }, ...flattenPages(p.children ?? [], depth + 1)]);
}

/** "3 min", "2 h", "Yesterday": how long ago, for task rows. */
export function ago(iso: string): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h`;
  const days = Math.round(hours / 24);
  return days === 1 ? "Yesterday" : `${days} days`;
}
