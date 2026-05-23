export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

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

export async function askQuestion(
  repoId: string,
  query: string
): Promise<ChatResponse> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_id: repoId, query }),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Request failed (${res.status}): ${detail}`);
  }
  return res.json();
}
