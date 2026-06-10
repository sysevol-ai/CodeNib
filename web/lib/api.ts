export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

/** Strip an absolute index prefix (e.g. /home/.../repo/) to a repo-relative path. */
export function repoRelative(path: string | null | undefined): string {
  if (!path) return "";
  const p = path.replace(/\\/g, "/");
  const i = p.lastIndexOf("/repo/");
  if (i !== -1) return p.slice(i + "/repo/".length);
  return p.replace(/^\/+/, "");
}

export interface RepoInfo {
  id: string;
  name: string;
  repo: string;
  base_commit: string;
  commit_short: string;
  language: string;
  description: string;
  problem_statement: string;
  languages: string[];
  file_count: number;
  capabilities: Record<string, boolean>;
}

export interface Citation {
  file: string | null;
  /** 1-based inclusive source range, matching the /source endpoint. */
  start_line: number | null;
  end_line: number | null;
  node_name: string;
  type: string;
  score: number | null;
  content: string | null;
}

export interface ToolCallInfo {
  skill_id: string;
  arguments: Record<string, unknown>;
  result_count: number;
  duration_ms: number;
  error: string | null;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  tool_calls: ToolCallInfo[];
  total_turns: number;
  total_duration_ms: number;
}

export async function fetchRepos(): Promise<RepoInfo[]> {
  const res = await fetch(`${API_BASE}/api/repos`);
  if (!res.ok) throw new Error(`Failed to load repos (${res.status})`);
  return res.json();
}

export interface WikiPageRef {
  id: string;
  title: string;
  children: WikiPageRef[];
}

export interface WikiTree {
  repo: string;
  pages: WikiPageRef[];
}

export interface WikiPage {
  id: string;
  title: string;
  markdown: string;
  citations: Citation[];
  diagram: string;
}

export interface SourceSlice {
  file: string;
  start_line: number;
  end_line: number;
  content: string;
}

export async function fetchWikiTree(repoId: string): Promise<WikiTree> {
  const res = await fetch(`${API_BASE}/api/repos/${encodeURIComponent(repoId)}/wiki`);
  if (!res.ok) throw new Error(`Failed to load wiki (${res.status})`);
  return res.json();
}

export async function fetchWikiPage(repoId: string, pageId: string): Promise<WikiPage> {
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/wiki/${encodeURIComponent(pageId)}`
  );
  if (!res.ok) throw new Error(`Failed to load page (${res.status})`);
  return res.json();
}

export async function fetchSource(
  repoId: string,
  file: string,
  start?: number,
  end?: number
): Promise<SourceSlice> {
  const params = new URLSearchParams({ file });
  if (start != null) params.set("start", String(start));
  if (end != null) params.set("end", String(end));
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/source?${params}`
  );
  if (!res.ok) throw new Error(`Failed to load source (${res.status})`);
  return res.json();
}

/** One conversation message. The last one sent is the current question. */
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export async function askQuestion(
  repoId: string,
  messages: ChatMessage[]
): Promise<ChatResponse> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_id: repoId, messages }),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Request failed (${res.status}): ${detail}`);
  }
  return res.json();
}

export interface CodemapNode {
  id: string;
  name: string;
  label: string;
  short: string;
  file: string | null;
  line: number | null;
  end_line?: number | null; // 1-based end of the symbol's definition (for the code drawer)
  kind: string;
  depth: number;
  is_root: boolean;
  external?: boolean; // no openable in-repo source (external dep / file node)
  importance?: number; // PageRank rank-percentile in [0,1] — drives node size
  community?: number; // detected cluster id — drives node colour
  ref_count?: number; // in-degree: how many symbols reference this one
  entry_score?: number; // out/(in+out) in [0,1] — high for drivers / entry points
  hidden_callees?: number; // out-edges folded away when this hub exceeded the cap
}

export interface CallSite {
  file: string;
  line: number | null;
}

export interface CodemapEdge {
  source: string;
  target: string;
  // Exact LSP/SCIP reference (call) site(s) for this edge — the caller file +
  // line where the call happens. Lets the UI jump an edge to its source.
  anchors?: CallSite[];
  weight?: number; // count of distinct call sites (= anchors.length) — drives edge width
}

export interface CodemapResponse {
  available: boolean;
  root?: string;
  root_label?: string;
  direction?: string;
  depth?: number;
  truncated?: boolean;
  nodes: CodemapNode[];
  edges: CodemapEdge[];
  mermaid: string;
  note?: string;
}

export async function fetchCodemap(
  repoId: string,
  opts: { symbol?: string; direction?: string; depth?: number; maxNodes?: number } = {}
): Promise<CodemapResponse> {
  const params = new URLSearchParams();
  if (opts.symbol) params.set("symbol", opts.symbol);
  if (opts.direction) params.set("direction", opts.direction);
  if (opts.depth != null) params.set("depth", String(opts.depth));
  if (opts.maxNodes != null) params.set("max_nodes", String(opts.maxNodes));
  const qs = params.toString();
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/codemap${qs ? `?${qs}` : ""}`
  );
  if (!res.ok) throw new Error(`Failed to load codemap (${res.status})`);
  return res.json();
}

// Induced dependency subgraph over a wiki page's cited symbols — lets a wiki
// page render as a view over the graph.
export async function fetchWikiGraph(repoId: string, pageId: string): Promise<CodemapResponse> {
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/wiki/${encodeURIComponent(pageId)}/graph`
  );
  if (!res.ok) throw new Error(`Failed to load page graph (${res.status})`);
  return res.json();
}
