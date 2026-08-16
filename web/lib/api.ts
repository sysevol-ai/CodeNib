import { apiBase, isStaticRuntime, staticDataUrl } from "./runtime";

export const API_BASE = apiBase();

async function responseError(response: Response, label: string): Promise<Error> {
  let detail = "";
  try {
    const raw = (await response.text()).trim();
    if (raw) {
      try {
        const parsed = JSON.parse(raw) as { detail?: unknown };
        detail = typeof parsed.detail === "string" ? parsed.detail : raw;
      } catch {
        detail = raw;
      }
    }
  } catch {
    // The HTTP status remains useful even when the response body is unreadable.
  }
  const bounded = detail.replace(/\s+/g, " ").slice(0, 500);
  return new Error(`${label} (${response.status})${bounded ? `: ${bounded}` : ""}`);
}

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
  source_url?: string | null;
  base_commit: string;
  commit_short: string;
  language: string;
  description: string;
  problem_statement: string;
  languages: string[];
  file_count: number;
  capabilities: Record<string, boolean>;
  graph_coverage?: GraphCoverage | null;
  /** Commit-window cost figures; absent for repos without a prebuilt window. */
  incremental?: WindowStats | null;
}

export interface GraphCoverage {
  available_languages: string[];
  unavailable_languages: string[];
  partial: boolean;
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

export async function fetchRepos(opts: { signal?: AbortSignal } = {}): Promise<RepoInfo[]> {
  const url = isStaticRuntime() ? staticDataUrl("repos.json") : `${API_BASE}/api/repos`;
  const res = await fetch(url, { signal: opts.signal });
  if (!res.ok) throw new Error(`Failed to load repos (${res.status})`);
  return res.json();
}

export interface WikiPageRef {
  id: string;
  title: string;
  cache_state?: "ready" | "cold" | "retryable" | "degraded";
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
  media_slots?: WikiMediaSlot[];
  evidence?: {
    items: WikiEvidenceItem[];
    relations: WikiRelationItem[];
  };
  generation?: {
    mode: "generated" | "offline" | "degraded";
    model: string | null;
    repaired?: boolean;
    fallback?: "fact_plan" | null;
    renderer?: "fact_plan" | "narrative";
    reason?: string;
    plan_warnings?: string[];
    retry?: {
      state: "scheduled" | "exhausted" | "recovered";
      attempts: number;
      max_attempts: number;
      last_attempt_epoch: number;
      next_attempt_epoch: number | null;
    };
    metrics?: {
      total_ms: number;
      retrieval_ms: number;
      relation_ms: number;
      planning_ms: number;
      model_call_ms: number;
      model_calls: number;
      model_failures: number;
      initial_attempts: number;
      fresh_replans: number;
      repair_attempts: number;
    };
  };
  grounding?: {
    valid: boolean;
    citation_coverage: number;
    // Strict reading: everything on the page resolved. `grounded` is the
    // looser coverage floor; it still rejects unknown sources and identifiers.
    grounded?: boolean;
    cited_evidence?: number;
    evidence_count: number;
    relation_count: number;
    unknown_citations?: string[];
    unknown_files?: string[];
    unsupported_identifiers?: string[];
  };
  quality?: {
    valid: boolean;
    planned_sections: number;
    required_sections: number;
    rendered_sections: number;
    substantive_blocks: number;
    required_blocks: number;
    covered_claims: number;
    planned_claims: number;
    claim_coverage: number;
  };
}

export interface WikiMediaSlot {
  id: string;
  kind: "diagram" | "image" | "storyboard" | "video";
  placement: "lead" | "section" | "aside" | "appendix";
  title: string;
  purpose: string;
  source_citations: string[];
  prompt: string;
  asset?: WikiMediaAsset;
  human_prior: {
    editable: boolean;
    notes: string[];
    [key: string]: unknown;
  };
}

export interface WikiMediaAsset {
  slot_id: string;
  kind: "diagram" | "image" | "storyboard" | "video";
  uri: string;
  mime_type: string;
  model: string;
  provider: string;
  prompt: string;
  source_citations: string[];
  metadata?: Record<string, unknown>;
}

/** Keep diagnostic or structurally invalid prose out of the reader surface. */
export function shouldWithholdWikiPage(page: WikiPage | null | undefined): boolean {
  if (page?.generation?.mode !== "degraded") return false;
  return Boolean(
    page.generation.fallback === "fact_plan" ||
      page.quality?.valid === false ||
      page.grounding?.valid === false,
  );
}

export interface WikiEvidenceItem {
  id: string;
  file: string;
  start_line: number | null;
  end_line: number | null;
  symbol: string;
  kind: string;
  routes: string[];
}

export interface WikiRelationItem {
  id: string;
  source: string;
  target: string;
  anchors: string[];
}

export interface SourceSlice {
  file: string;
  start_line: number;
  end_line: number;
  content: string;
}

export async function fetchWikiTree(repoId: string): Promise<WikiTree> {
  const url = isStaticRuntime()
    ? staticDataUrl("repos", repoId, "wiki.json")
    : `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/wiki`;
  const res = await fetch(url);
  if (!res.ok) throw await responseError(res, "Failed to load wiki");
  return res.json();
}

const wikiPageRequests = new Map<string, Promise<WikiPage>>();

export async function fetchWikiPage(
  repoId: string,
  pageId: string,
  options: { refresh?: boolean } = {},
): Promise<WikiPage> {
  const cacheKey = `${repoId}\u0000${pageId}`;
  if (!options.refresh) {
    const cached = wikiPageRequests.get(cacheKey);
    if (cached) return cached;
  }
  const request = (async () => {
    const url = isStaticRuntime()
      ? staticDataUrl("repos", repoId, "pages", `${pageId}.json`)
      : `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/wiki/${encodeURIComponent(pageId)}`;
    const res = await fetch(url);
    if (!res.ok) throw await responseError(res, "Failed to load page");
    return res.json();
  })();
  wikiPageRequests.set(cacheKey, request);
  try {
    return await request;
  } catch (error) {
    if (wikiPageRequests.get(cacheKey) === request) {
      wikiPageRequests.delete(cacheKey);
    }
    throw error;
  }
}

export async function fetchSource(
  repoId: string,
  file: string,
  start?: number,
  end?: number,
  commit?: string
): Promise<SourceSlice> {
  if (isStaticRuntime()) {
    throw new Error("Source slices are embedded in static Wiki citations");
  }
  const params = new URLSearchParams({ file });
  if (start != null) params.set("start", String(start));
  if (end != null) params.set("end", String(end));
  if (commit) params.set("commit", commit);
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
  if (isStaticRuntime()) {
    throw new Error("Interactive Ask requires a CodeNib runtime");
  }
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
  // Symbol comes from a derived file: a type-declaration stub (.d.ts/.pyi), a
  // build artifact, or source with a "generated by" banner. Still real context,
  // but held out of the importance ranking so a wall of typedefs cannot pass
  // itself off as the repo's core.
  declaration?: boolean;
  /** Precomputed source preview used by static Wiki exports. Live runtimes
   *  normally omit this and resolve the same slice through /source. */
  source?: SourceSlice | null;
}

export interface CallSite {
  file: string;
  line: number | null;
  /** Bounded exact call-site context embedded by the static Wiki exporter. */
  source?: SourceSlice | null;
}

export interface CodemapEdge {
  source: string;
  target: string;
  // Bounded sample of exact LSP/SCIP reference sites. `weight` retains the
  // complete distinct-site count when the sample is truncated.
  anchors?: CallSite[];
  weight?: number; // complete count of distinct call sites — drives edge width
  hidden_anchors?: number;
  source_hierarchy?: string;
  target_hierarchy?: string;
  bundle_path?: string[]; // containment route used for hierarchical edge bundling
  bundle_lca?: string;
  bundle_lca_kind?: string;
  cross_file?: boolean;
}

export interface CodemapHierarchyNode {
  id: string;
  parent: string | null;
  kind: "root" | "directory" | "file" | "symbol";
  label: string;
  path?: string;
  file?: string;
  node_id?: string;
  line?: number;
  end_line?: number;
  depth: number;
  child_count: number;
  symbol_count: number;
  doi: number;
  importance?: number;
  open_by_default?: boolean;
  external?: boolean;
  /** Source for projected parent scopes that are not standalone view nodes. */
  source?: SourceSlice | null;
}

export interface CodemapHierarchy {
  root: string;
  nodes: CodemapHierarchyNode[];
  open_files?: string[];
  source_root?: string;
}

export interface GraphSetupLanguage {
  language: string;
  display_name: string;
  state: "ready" | "missing" | "unsupported";
  backend: string | null;
  command: string[];
  resolved_command: string | null;
  missing: string[];
  note: string;
}

export interface GraphSetupReport {
  ready: boolean;
  partial_ready: boolean;
  languages: GraphSetupLanguage[];
  buildable_languages: string[];
  unavailable_languages: string[];
  unsupported_languages: string[];
  install_hints: string[];
  commands: string[];
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
  hierarchy?: CodemapHierarchy;
  mermaid: string;
  note?: string;
  setup?: GraphSetupReport;
  // Which commit snapshot this payload was built from.
  commit?: string | null;
  // True when the selected snapshot could not be loaded and the API served
  // the repository's default graph instead.
  fell_back?: boolean;
}

// One selectable point in the repo's commit window. Snapshots are built by
// scripts/build_commit_window.py; `method` is "cold" for the anchor build and
// "patched" for commits reached incrementally.
export interface CommitRef {
  sha: string;
  short: string;
  subject: string;
  date: string;
  author: string;
  method: "cold" | "patched";
  build_seconds: number;
  node_count: number;
  edge_count: number;
  changed_files: number;
}

// Cold-build vs warm-patch cost for a repo's commit window. Derived server-side
// in commit_window.window_stats so the API, this UI and the build script cannot
// report different numbers. This excludes LSP startup and transition overhead;
// `speedup` is null when no defensible ratio exists.
export interface WindowStats {
  commit_count: number;
  patched_count: number;
  cold_seconds: number | null;
  mean_patch_seconds: number | null;
  speedup: number | null;
}

export interface CommitWindowResponse {
  available: boolean;
  stats?: WindowStats | null;
  ref?: string;
  language?: string;
  // Every language actually built into these snapshots.
  languages?: string[];
  lsp_startup_seconds?: number;
  commits: CommitRef[];
  selected: string | null;
}

// Repos without a prebuilt window return available=false; callers should then
// fall back to the repo's single indexed commit.
export async function fetchCommits(repoId: string): Promise<CommitWindowResponse> {
  const url = isStaticRuntime()
    ? staticDataUrl("repos", repoId, "commits.json")
    : `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/commits`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load commits (${res.status})`);
  return res.json();
}

export async function fetchCodemap(
  repoId: string,
  opts: {
    symbol?: string;
    direction?: string;
    depth?: number;
    maxNodes?: number;
    commit?: string;
  } = {}
): Promise<CodemapResponse> {
  if (isStaticRuntime()) {
    throw new Error("Interactive dependency exploration requires a CodeNib runtime");
  }
  const params = new URLSearchParams();
  if (opts.symbol) params.set("symbol", opts.symbol);
  if (opts.direction) params.set("direction", opts.direction);
  if (opts.depth != null) params.set("depth", String(opts.depth));
  if (opts.maxNodes != null) params.set("max_nodes", String(opts.maxNodes));
  if (opts.commit) params.set("commit", opts.commit);
  const qs = params.toString();
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/codemap${qs ? `?${qs}` : ""}`
  );
  if (!res.ok) throw new Error(`Failed to load codemap (${res.status})`);
  return res.json();
}

/**
 * One file or directory in the module map. Extends CodemapNode so a module
 * payload is assignable to CodemapResponse and the existing graph renderers
 * accept it unchanged; `line`/`end_line` carry no meaning for a directory.
 */
export interface ModuleNode extends CodemapNode {
  path: string;
  kind: "file" | "directory";
  symbol_count: number;
  /**
   * What the build manifest calls this module ("preact/compat"), or null when
   * it declares none. Distinct from `entry_score`, which is a graph shape —
   * this one is the repo stating its own public surface.
   */
  entry_point?: string | null;
}

export interface ModuleEdge extends CodemapEdge {
  source: string;
  target: string;
  /** Distinct symbol pairs behind this dependency — how coupled the modules are. */
  weight: number;
  /** Total reference sites rolled into this edge (>= weight). */
  call_sites: number;
  anchors: CallSite[]; // capped sample; see hidden_anchors for the remainder
  hidden_anchors: number;
}

export interface ModulemapResponse extends Omit<CodemapResponse, "nodes" | "edges"> {
  available: boolean;
  /** Resolved granularity — "auto" becomes "file" or "directory" server-side. */
  granularity: "file" | "directory";
  focus?: string;
  focus_label?: string;
  depth?: number;
  truncated?: boolean;
  total_modules?: number;
  /** What the filters dropped, so the UI never implies full coverage. */
  excluded?: { test_files: number; derived_files: number };
  nodes: ModuleNode[];
  edges: ModuleEdge[];
  hierarchy?: CodemapHierarchy;
  mermaid: string;
  note?: string;
  setup?: GraphSetupReport;
  commit?: string | null;
  fell_back?: boolean;
}

/**
 * Module-level dependency map. Projected from symbol references through each
 * symbol's file, so it works on existing graphs — CodeNib emits no `import`
 * edges today.
 */
export async function fetchModulemap(
  repoId: string,
  opts: {
    focus?: string;
    granularity?: "auto" | "file" | "directory";
    depth?: number;
    maxNodes?: number;
    includeTests?: boolean;
    commit?: string;
  } = {}
): Promise<ModulemapResponse> {
  if (isStaticRuntime()) {
    throw new Error("Interactive module exploration requires a CodeNib runtime");
  }
  const params = new URLSearchParams();
  if (opts.focus) params.set("focus", opts.focus);
  if (opts.granularity) params.set("granularity", opts.granularity);
  if (opts.depth != null) params.set("depth", String(opts.depth));
  if (opts.maxNodes != null) params.set("max_nodes", String(opts.maxNodes));
  if (opts.includeTests) params.set("include_tests", "true");
  if (opts.commit) params.set("commit", opts.commit);
  const qs = params.toString();
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/modulemap${qs ? `?${qs}` : ""}`
  );
  if (!res.ok) throw new Error(`Failed to load module map (${res.status})`);
  return res.json();
}

// Induced dependency subgraph over a wiki page's cited symbols — lets a wiki
// page render as a view over the graph.
export async function fetchWikiGraph(repoId: string, pageId: string): Promise<CodemapResponse> {
  const url = isStaticRuntime()
    ? staticDataUrl("repos", repoId, "page-graphs", `${pageId}.json`)
    : `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/wiki/${encodeURIComponent(pageId)}/graph`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load page graph (${res.status})`);
  return res.json();
}

// One end of a dependency edge, addressed by (file, line span) — the frontend
// has this from the codemap payload but not the graph's internal symbol id.
export interface EdgeEndpointInput {
  file: string;
  line: number | null; // 1-based start of the symbol definition
  end_line: number | null; // 1-based end
  label: string; // display name, for the prompt only (not identity)
}

export interface EdgeLabelResult {
  label: string; // short LLM phrase, e.g. "validates user input" ("" if none)
  cached: boolean;
  disabled: boolean; // feature is off in server config
}

// Short LLM phrase describing how the source symbol uses the target. On-demand +
// cached server-side; returns an empty label when the feature is disabled or
// nothing could be generated (the UI then shows nothing extra).
export async function fetchEdgeLabel(
  repoId: string,
  body: {
    source: EdgeEndpointInput;
    target: EdgeEndpointInput;
    anchors: CallSite[];
    commit?: string;
  },
  opts: { signal?: AbortSignal } = {}
): Promise<EdgeLabelResult> {
  if (isStaticRuntime()) {
    throw new Error("Generated edge labels require a CodeNib runtime");
  }
  const res = await fetch(
    `${API_BASE}/api/repos/${encodeURIComponent(repoId)}/edge-label`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // The server reads at most three call-site snippets when constructing the
      // label prompt. Do not send the full aggregate edge history over HTTP.
      body: JSON.stringify({ ...body, anchors: body.anchors.slice(0, 3) }),
      signal: opts.signal,
    }
  );
  if (!res.ok) throw new Error(`Failed to load edge label (${res.status})`);
  return res.json();
}
