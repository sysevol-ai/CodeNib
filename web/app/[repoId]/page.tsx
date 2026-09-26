"use client";

import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import Header from "@/components/Header";
import Markdown from "@/components/Markdown";
import PageBoundaryCard from "@/components/PageBoundary";
import AreaMap from "@/components/AreaMap";
import AskBar from "@/components/AskBar";
import { AppLink } from "@/lib/router";
import { isStaticRuntime, mediaAssetUrl } from "@/lib/runtime";
import {
  extractJourney,
  stageRoles,
  stageSentence,
  type Journey,
  partitionWikiMediaSlots,
  splitWikiMarkdown,
  wikiRetryNotice,
} from "@/lib/wikiPresentation";
import {
  fetchCommits,
  fetchRepos,
  fetchWikiAreaMap,
  fetchWikiBoundary,
  fetchWikiGraph,
  fetchWikiPage,
  fetchWikiTree,
  isSourceCheckedWikiPage,
  materializedWikiMediaSlots,
  repoRelative,
  shouldWithholdWikiPage,
  type CodemapResponse,
  type PageBoundary,
  type WikiAreaMap,
  type Citation,
  type CommitRef,
  type RepoInfo,
  type WikiMediaSlot,
  type WikiPage,
  type WikiPageRef,
} from "@/lib/api";

const CodePanel = lazy(() => import("@/components/CodePanel"));
const Codemap = lazy(() => import("@/components/Codemap"));
const GraphView = lazy(() => import("@/components/GraphView"));

interface Heading {
  id: string;
  text: string;
  level: number;
}

// The wiki page now leads with an interactive subsystem graph, so the narrator's
// generated mermaid diagrams (and any heading left empty once removed) are
// redundant — strip them before rendering.
/** Drop diagrams unless they came from the validated structured plan.
 *
 *  The WikiBuilder path emits a mechanical fan-out of every module it found,
 *  which Mermaid's auto-layout renders as noise. Free-form model Markdown is
 *  also not trusted to supply executable Mermaid directives. A generated
 *  fact-plan diagram is different: its directed edges were validated against
 *  the page's evidence before the backend rendered the fence. */
function stripGeneratedDiagrams(
  md: string,
  keepValidatedPlan: boolean,
): string {
  if (keepValidatedPlan) return md;
  return md
    // a heading, its diagram, and the caption paragraph under it
    .replace(/\n#{1,6}[^\n]*\n+```mermaid[\s\S]*?```(?:\n+(?![#\n])[^\n]*)?/g, "")
    .replace(/```mermaid[\s\S]*?```(?:\n+(?![#\n])[^\n]*)?/g, "") // any stray diagram
    .trimEnd();
}

// Link a repo-relative source path to the exact blob on GitHub at the indexed commit.
function ghFileUrl(
  repo: string | undefined,
  sourceUrl: string | null | undefined,
  commit: string | undefined,
  file: string,
  start?: number | null,
  end?: number | null
): string | null {
  if (!repo) return null;
  const lines = start ? `#L${start}${end && end !== start ? `-L${end}` : ""}` : "";
  const root = (sourceUrl || `https://github.com/${repo}`).replace(/\/+$/, "");
  return `${root}/blob/${commit || "HEAD"}/${file}${lines}`;
}

function TocTree({
  pages,
  activeId,
  onPick,
}: {
  pages: WikiPageRef[];
  activeId: string;
  onPick: (id: string) => void;
}) {
  const cacheLabels = {
    ready: "Cached page",
    cold: "First load will generate this page",
    retryable: "Quality retry queued",
    degraded: "Page needs review",
  } as const;
  return (
    <ul className="toc-tree">
      {pages.map((p) => (
        <li key={p.id}>
          <button
            className={`toc-link ${p.id === activeId ? "active" : ""}`}
            aria-current={p.id === activeId ? "page" : undefined}
            title={p.title}
            onClick={() => onPick(p.id)}
          >
            <span className="toc-label">{p.title}</span>
            {p.cache_state && (
              <span
                className={`toc-cache-state ${p.cache_state}`}
                aria-label={cacheLabels[p.cache_state]}
                title={cacheLabels[p.cache_state]}
              />
            )}
          </button>
          {p.children.length > 0 && (
            <TocTree pages={p.children} activeId={activeId} onPick={onPick} />
          )}
        </li>
      ))}
    </ul>
  );
}

function flattenPages(pages: WikiPageRef[]): WikiPageRef[] {
  return pages.flatMap((page) => [page, ...flattenPages(page.children)]);
}

function markPageCacheState(
  pages: WikiPageRef[],
  pageId: string,
  cacheState: WikiPageRef["cache_state"],
): WikiPageRef[] {
  return pages.map((item) => ({
    ...item,
    cache_state: item.id === pageId ? cacheState : item.cache_state,
    children: markPageCacheState(item.children, pageId, cacheState),
  }));
}

// Timed cold graph-build or warm patch section for the selected snapshot. This
// excludes LSP startup and transition overhead and does not imply equality with
// a fresh rebuild.
function commitEvidence(commits: CommitRef[], selected?: string): string | null {
  if (!commits.length) return null;
  const i = commits.findIndex((c) => c.sha === selected);
  const c = i >= 0 ? commits[i] : commits[0];
  if (!c) return null;

  if (c.method === "cold") {
    return c.build_seconds != null ? `cold build · ${c.build_seconds.toFixed(1)}s` : null;
  }

  const parts: string[] = [];
  if (c.build_seconds != null) parts.push(`warm patch · ${c.build_seconds.toFixed(2)}s`);
  if (c.changed_files != null) {
    parts.push(`${c.changed_files} file${c.changed_files === 1 ? "" : "s"} changed`);
  }
  // commits[] is newest-first, so this commit's predecessor is the next entry.
  const prev = i >= 0 ? commits[i + 1] : commits[1];
  if (prev && c.node_count != null && prev.node_count != null) {
    const d = c.node_count - prev.node_count;
    parts.push(d === 0 ? "±0 nodes" : `${d > 0 ? "+" : "−"}${Math.abs(d)} nodes`);
  }
  return parts.length ? parts.join(" · ") : null;
}

function mediaKindLabel(kind: WikiMediaSlot["kind"]): string {
  switch (kind) {
    case "diagram":
      return "Diagram";
    case "image":
      return "Illustration";
    case "storyboard":
      return "Storyboard";
    case "chart":
      return "Chart";
    case "video":
      return "Video";
    default:
      return "Media";
  }
}

function MediaPreview({
  slot,
  trace,
}: {
  slot: WikiMediaSlot;
  trace?: ArchitectureJourney;
}) {
  const asset = slot.asset;
  const src = asset?.uri ? mediaAssetUrl(asset.uri) : null;
  if (!src || !asset) return null;
  const adapter = slot.render_contract?.adapter;
  if (
    adapter === "architecture" &&
    slot.render_contract?.provenance === "architecture-plan"
  ) {
    return <SystemArchitecture slot={slot} trace={trace} />;
  }
  if (adapter === "storyboard") {
    return <StoryPath slot={slot} />;
  }
  if (asset.mime_type.startsWith("video/")) {
    return (
      <video className="wiki-media-asset" controls src={src}>
        Video preview is unavailable in this browser.
      </video>
    );
  }
  return (
    <img
      className={`wiki-media-asset ${
        asset.provider === "repository"
          ? "repository-owned"
          : slot.render_contract
            ? "contract-visual"
            : ""
      }`}
      src={src}
      alt={slot.title}
      referrerPolicy="no-referrer"
    />
  );
}

function architectureLayerLabel(layer: string | undefined): string {
  switch (layer) {
    case "external":
      return "Outside the system";
    case "interface":
      return "Interface";
    case "coordination":
      return "Coordination";
    case "execution":
      return "Execution";
    case "data":
      return "Data & artifacts";
    default:
      return "System role";
  }
}

/** Render a heading string with its `code` spans. */
function inlineCode(text: string): ReactNode[] {
  return text.split(/(`[^`]+`)/).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") ? (
      <code key={index}>{part.slice(1, -1)}</code>
    ) : (
      <span key={index}>{part}</span>
    ),
  );
}

interface ArchitectureJourney {
  journey: Journey;
  citations?: Citation[];
  renderText: (markdown: string) => ReactNode;
  onPick?: (pageId: string) => void;
}

function SystemArchitecture({
  slot,
  trace,
}: {
  slot: WikiMediaSlot;
  trace?: ArchitectureJourney;
}) {
  const [activeRole, setActiveRole] = useState<string | null>(null);
  const contract = slot.render_contract;
  if (
    contract?.adapter !== "architecture" ||
    contract.provenance !== "architecture-plan"
  ) {
    return null;
  }
  const nodes = new Map(contract.data.nodes.map((node) => [node.id, node]));
  const primaryNodes = contract.data.primary_path
    .map((id) => nodes.get(id))
    .filter((node) => node !== undefined);
  const primaryIds = new Set(primaryNodes.map((node) => node.id));
  const supportingNodes = contract.data.nodes.filter(
    (node) => !primaryIds.has(node.id),
  );
  const primaryConnections = contract.data.primary_path.slice(0, -1).map(
    (source, index) =>
      contract.data.edges.find(
        (edge) =>
          edge.source === source &&
          edge.target === contract.data.primary_path[index + 1],
      ),
  );
  const stages = trace?.journey.stages ?? [];
  const roles = trace ? stageRoles(stages, contract.data.nodes, trace.citations) : [];
  const stagesOf = (roleId: string) =>
    stages.filter((_stage, index) => roles[index] === roleId);
  const lit = (roleId: string | null) =>
    activeRole == null ? "" : roleId === activeRole ? "is-lit" : "is-dim";

  const supportingConnection = (nodeId: string) => {
    const edge = contract.data.edges.find(
      (candidate) =>
        candidate.source === nodeId || candidate.target === nodeId,
    );
    if (!edge) return null;
    const peerId = edge.source === nodeId ? edge.target : edge.source;
    const peer = nodes.get(peerId);
    if (!peer) return null;
    return `${edge.label} ${edge.source === nodeId ? "→" : "←"} ${peer.label}`;
  };

  const roleStages = (roleId: string) => {
    const own = stagesOf(roleId);
    if (!own.length) return null;
    return (
      <div className="wiki-system-role-stages" aria-label="Traced functions in this role">
        {own.map((stage) => (
          <span className="wiki-system-stage-chip" key={stage.index}>
            <span className="wiki-system-stage-number">{stage.index}</span>
            <code>{stage.symbol}</code>
          </span>
        ))}
      </div>
    );
  };

  return (
    <section
      className="wiki-system-architecture"
      aria-label={slot.title}
      onMouseLeave={() => setActiveRole(null)}
    >
      <header className="wiki-system-architecture-head">
        <div>
          <span className="wiki-system-architecture-eyebrow">System architecture</span>
          <h3>{slot.title}</h3>
        </div>
      </header>
      <div className="wiki-system-architecture-body">
        <div className="wiki-system-primary">
          <div className="wiki-system-section-label">
            <span>Main path</span>
          </div>
          <ol className="wiki-system-path">
            {primaryNodes.map((node, index) => {
              const connection = primaryConnections[index];
              return (
                <li className="wiki-system-path-step" key={node.id}>
                  <article
                    className={`wiki-system-role layer-${node.layer ?? "system"} ${lit(node.id)}`}
                    onMouseEnter={() => setActiveRole(node.id)}
                  >
                    <span className="wiki-system-role-index" aria-hidden="true">
                      {index + 1}
                    </span>
                    <div className="wiki-system-role-copy">
                      <span className="wiki-system-role-layer">
                        {architectureLayerLabel(node.layer)}
                      </span>
                      <h4>{node.label}</h4>
                      <p>{inlineCode(node.detail)}</p>
                      {roleStages(node.id)}
                    </div>
                  </article>
                  {connection && (
                    <div className="wiki-system-connection">
                      <span className="wiki-system-connection-line" aria-hidden="true" />
                      <span>{connection.label}</span>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
        <aside className="wiki-system-context">
          {supportingNodes.length > 0 && (
            <div className="wiki-system-supporting">
              <div className="wiki-system-section-label">
                <span>Supporting roles</span>
              </div>
              {supportingNodes.map((node) => (
                <article
                  className={`wiki-system-support-card ${lit(node.id)}`}
                  key={node.id}
                  onMouseEnter={() => setActiveRole(node.id)}
                >
                  <span>{architectureLayerLabel(node.layer)}</span>
                  <h4>{node.label}</h4>
                  <p>{inlineCode(node.detail)}</p>
                  {roleStages(node.id)}
                  {supportingConnection(node.id) && (
                    <small>{supportingConnection(node.id)}</small>
                  )}
                </article>
              ))}
            </div>
          )}
          {contract.data.boundaries.length > 0 && (
            <div className="wiki-system-boundaries">
              <div className="wiki-system-section-label">
                <span>Boundaries</span>
              </div>
              {contract.data.boundaries.map((boundary) => (
                <article className="wiki-system-boundary" key={boundary.id}>
                  <span className="wiki-system-boundary-mark" aria-hidden="true" />
                  <div>
                    <h4>{boundary.label}</h4>
                    {boundary.detail && <p>{inlineCode(boundary.detail)}</p>}
                    <small>
                      {boundary.members
                        .map((member) => nodes.get(member)?.label)
                        .filter(Boolean)
                        .join(" · ")}
                    </small>
                  </div>
                </article>
              ))}
            </div>
          )}
        </aside>
      </div>
      {trace && stages.length > 0 && (
        <div className="wiki-system-trace">
          <div className="wiki-system-section-label">
            <span>Traced call path, recorded in the index</span>
          </div>
          <h4 className="wiki-system-trace-title">{inlineCode(trace.journey.title)}</h4>
          <ol className="wiki-system-trace-steps">
            {stages.map((stage, index) => {
              const role = roles[index] ? nodes.get(roles[index]!) : undefined;
              return (
                <li
                  key={stage.index}
                  className={`wiki-system-trace-step ${role ? lit(role.id) : activeRole ? "is-dim" : ""}`}
                  onMouseEnter={() => setActiveRole(role?.id ?? null)}
                >
                  <span className="wiki-system-stage-number">{stage.index}</span>
                  <div className="wiki-system-trace-copy">
                    <div className="wiki-system-trace-head">
                      <code className="wiki-system-trace-symbol">{stage.symbol}</code>
                      {role && (
                        <span className={`wiki-system-trace-role layer-${role.layer ?? "system"}`}>
                          {role.label}
                        </span>
                      )}
                      {stage.page && trace.onPick && (
                        <button
                          type="button"
                          className="wiki-system-trace-page"
                          onClick={() => trace.onPick?.(stage.page!.id)}
                        >
                          {stage.page.title} →
                        </button>
                      )}
                    </div>
                    <div className="wiki-system-trace-text">
                      {trace.renderText(stageSentence(stage))}
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </section>
  );
}

function StoryPath({ slot }: { slot: WikiMediaSlot }) {
  const contract = slot.render_contract;
  if (contract?.adapter !== "storyboard") return null;

  return (
    <ol className="wiki-story-path" aria-label={slot.title}>
      {contract.data.panels.map((panel, index) => (
        <li className="wiki-story-path-step" key={panel.id}>
          <span className="wiki-story-path-index" aria-hidden="true">
            {String(index + 1).padStart(2, "0")}
          </span>
          <div className="wiki-story-path-copy">
            {panel.role && <span className="wiki-story-path-role">{panel.role}</span>}
            <strong>{panel.title}</strong>
            {panel.detail && <p>{panel.detail}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}

function MediaStatus({ slot }: { slot: WikiMediaSlot }) {
  const repositoryOwned = slot.asset?.provider === "repository";
  const adapter = slot.render_contract?.adapter.replace("-", " ");
  return (
    <div className="wiki-media-status">
      <span className="ready">
        {repositoryOwned
          ? "Provided by repository"
          : adapter
            ? "Sources"
            : "Generated from cited source"}
      </span>
    </div>
  );
}

function mediaOpenLabel(slot: WikiMediaSlot): string {
  switch (slot.render_contract?.adapter) {
    case "architecture":
      return "Open as image ↗";
    case "flow":
      return "Open as image ↗";
    case "storyboard":
      return "Open as image ↗";
    default:
      return "Open generated asset ↗";
  }
}

function MediaCitations({
  slot,
  repo,
}: {
  slot: WikiMediaSlot;
  repo: RepoInfo | null;
}) {
  if (!(slot.source_citations ?? []).length) return null;
  return (
    <div className="wiki-media-citations">
      {(slot.source_citations ?? []).slice(0, 4).map((file) => {
        const url = ghFileUrl(
          repo?.repo,
          repo?.source_url,
          repo?.base_commit,
          file,
        );
        return url ? (
          <a
            className="wiki-media-citation mono"
            href={url}
            target="_blank"
            rel="noreferrer"
            key={file}
            title={`Open ${file} on GitHub`}
          >
            {repoRelative(file)}
          </a>
        ) : (
          <span className="wiki-media-citation mono" key={file}>
            {repoRelative(file)}
          </span>
        );
      })}
    </div>
  );
}

function MultimodalMedia({
  slots,
  repo,
  variant = "section",
  trace,
}: {
  slots: WikiMediaSlot[];
  repo: RepoInfo | null;
  variant?: "lead" | "bridge" | "section";
  /** The Overview's entry path, drawn inside the architecture card. */
  trace?: ArchitectureJourney;
}) {
  const visibleSlots = materializedWikiMediaSlots(slots).filter((slot) =>
    slot.asset?.uri ? Boolean(mediaAssetUrl(slot.asset.uri)) : false,
  );
  if (!visibleSlots.length) return null;

  if (variant === "bridge") {
    return (
      <section className="wiki-media wiki-media-bridge" aria-label="Page reading map">
        {visibleSlots.map((slot) => {
          const assetUrl = slot.asset?.uri ? mediaAssetUrl(slot.asset.uri) : null;
          return (
            <article className="wiki-story-map" key={slot.id}>
              <div className="wiki-story-map-frame">
                <MediaPreview slot={slot} trace={trace} />
              </div>
              <div className="wiki-story-map-footer">
                <MediaStatus slot={slot} />
                <MediaCitations slot={slot} repo={repo} />
                {assetUrl && (
                  <a
                    className="wiki-media-open"
                    href={assetUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {mediaOpenLabel(slot)}
                  </a>
                )}
              </div>
            </article>
          );
        })}
      </section>
    );
  }

  const [primary, ...secondary] = visibleSlots;
  const primaryAssetUrl = primary?.asset?.uri
    ? mediaAssetUrl(primary.asset.uri)
    : null;

  return (
    <section
      className={`wiki-media ${variant === "lead" ? "wiki-media-lead" : ""}`}
      aria-label={variant === "lead" ? "Overview visual" : "Source-linked visuals"}
    >
      {variant !== "lead" && (
        <div className="wiki-media-head">
          <div>
            <h2>Source-linked visuals</h2>
            <p>
              Generated visual explanations grounded in the source citations on this page.
            </p>
          </div>
          <span className="wiki-media-count">
            {visibleSlots.length} visual{visibleSlots.length === 1 ? "" : "s"}
          </span>
        </div>
      )}

      {primary && (
        <article className="wiki-media-feature">
          <div className="wiki-media-feature-preview">
            <MediaPreview slot={primary} trace={trace} />
          </div>
          <div className="wiki-media-feature-body">
            <div className="wiki-media-eyebrow">
              {primary.asset?.provider === "repository"
                ? "Repository visual"
                : `${mediaKindLabel(primary.kind)} · ${primary.placement}`}
            </div>
            <h3>{primary.title}</h3>
            <p>{primary.purpose}</p>
            <MediaStatus slot={primary} />
            <MediaCitations slot={primary} repo={repo} />
            {primaryAssetUrl && (
              <a
                className="wiki-media-open"
                href={primaryAssetUrl}
                target="_blank"
                rel="noreferrer"
              >
                {primary.asset?.provider === "repository"
                  ? "Open repository asset ↗"
                  : "Open generated asset ↗"}
              </a>
            )}
          </div>
        </article>
      )}

      {secondary.length > 0 && (
        <div className="wiki-media-grid">
          {secondary.map((slot) => (
            <article className="wiki-media-slot" key={slot.id}>
              <MediaPreview slot={slot} trace={trace} />
              <div className="wiki-media-body">
                <div className="wiki-media-meta">
                  <span>{mediaKindLabel(slot.kind)}</span>
                  <span>{slot.placement}</span>
                </div>
                <h3>{slot.title}</h3>
                <p>{slot.purpose}</p>
                <MediaStatus slot={slot} />
                <MediaCitations slot={slot} repo={repo} />
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export default function WikiPageView({
  repoId,
  initialPageId = "overview",
}: {
  repoId: string;
  initialPageId?: string;
}) {

  const staticRuntime = isStaticRuntime();

  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [pages, setPages] = useState<WikiPageRef[]>([]);
  const [activeId, setActiveId] = useState<string>(initialPageId);
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageLoadSeconds, setPageLoadSeconds] = useState(0);
  const [pageGraph, setPageGraph] = useState<CodemapResponse | null>(null);
  const [boundary, setBoundary] = useState<PageBoundary | null>(null);
  const [areaMap, setAreaMap] = useState<WikiAreaMap | null>(null);
  const [pageGraphOpen, setPageGraphOpen] = useState(false);
  const [pageGraphLoading, setPageGraphLoading] = useState(false);
  const [pageGraphError, setPageGraphError] = useState(false);
  // The graph explorer opens as a full-screen modal, optionally seeded on a
  // symbol when launched via "Focus here" from a wiki subsystem map.
  const [graphSeed, setGraphSeed] = useState<string | undefined>(undefined);
  const [graphOpen, setGraphOpen] = useState(false);
  const [sourceCitation, setSourceCitation] = useState<Citation | null>(null);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [activeHeading, setActiveHeading] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [tocLoading, setTocLoading] = useState(true);
  const [tocOpen, setTocOpen] = useState(false);
  // Commit window: empty when this repo has no prebuilt snapshots, in which
  // case the rail keeps its static "Last indexed" label.
  const [commits, setCommits] = useState<CommitRef[]>([]);
  const [selectedCommit, setSelectedCommit] = useState<string | undefined>(undefined);
  const commitCost = commitEvidence(commits, selectedCommit);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setCommits([]);
    setSelectedCommit(undefined);

    fetchRepos()
      .then((rs) => {
        if (!cancelled) setRepo(rs.find((x) => x.id === repoId) ?? null);
      })
      .catch(() => {});
    // Optional feature: repos without a prebuilt window just keep the static
    // commit label, so a failure here is not surfaced as a page error.
    fetchCommits(repoId)
      .then((w) => {
        if (cancelled || !w.available) return;
        setCommits(w.commits);
        setSelectedCommit(w.selected ?? undefined);
      })
      .catch(() => {});
    setTocLoading(true);
    setError(null);
    fetchWikiTree(repoId)
      .then((t) => {
        if (!cancelled) setPages(t.pages);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setTocLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  useEffect(() => {
    let cancelled = false;
    const startedAt = Date.now();
    const elapsedTimer = window.setInterval(() => {
      if (!cancelled) {
        setPageLoadSeconds(Math.floor((Date.now() - startedAt) / 1000));
      }
    }, 1000);
    setPageLoadSeconds(0);
    setPage(null);
    setPageError(null);
    setPageGraph(null);
    setPageGraphOpen(false);
    setPageGraphLoading(false);
    setPageGraphError(false);
    setSourceCitation(null);
    fetchWikiPage(repoId, activeId)
      .then((p) => !cancelled && setPage(p))
      .catch(
        (e) =>
          !cancelled && setPageError(e instanceof Error ? e.message : String(e)),
      )
      .finally(() => window.clearInterval(elapsedTimer));
    return () => {
      cancelled = true;
      window.clearInterval(elapsedTimer);
    };
  }, [repoId, activeId]);

  useEffect(() => {
    if (!page) return;
    setPages((current) =>
      markPageCacheState(
        current,
        page.id,
        shouldWithholdWikiPage(page) ? "degraded" : "ready",
      ),
    );
  }, [page]);

  // Preload at most two adjacent pages that the server already reports as
  // cached. This makes sidebar navigation instant without turning browsing
  // into hidden model spend for cold pages.
  useEffect(() => {
    if (!page || pages.length === 0) return;
    const flattened = flattenPages(pages);
    const activeIndex = flattened.findIndex((item) => item.id === activeId);
    if (activeIndex < 0) return;
    [flattened[activeIndex + 1], flattened[activeIndex - 1]]
      .filter(
        (item): item is WikiPageRef =>
          Boolean(item && item.cache_state === "ready"),
      )
      .slice(0, 2)
      .forEach((item) => {
        void fetchWikiPage(repoId, item.id, { materializeMedia: false }).catch(
          () => {},
        );
      });
  }, [repoId, activeId, page, pages]);

  useEffect(() => {
    if (!pageGraphOpen || pageGraph) return;
    let cancelled = false;
    setPageGraphLoading(true);
    setPageGraphError(false);
    fetchWikiGraph(repoId, activeId)
      .then((graph) => {
        if (!cancelled) setPageGraph(graph);
      })
      .catch(() => {
        if (!cancelled) setPageGraphError(true);
      })
      .finally(() => {
        if (!cancelled) setPageGraphLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repoId, activeId, pageGraphOpen, pageGraph]);

  // The system map belongs to the repository, not a page: load it once.
  useEffect(() => {
    setAreaMap(null);
    let cancelled = false;
    fetchWikiAreaMap(repoId)
      .then((m) => {
        if (!cancelled) setAreaMap(m);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  // The graph card is deterministic and cheap; load it with the page rather
  // than behind a toggle so the reader meets the code's shape first.
  useEffect(() => {
    setBoundary(null);
    if (activeId === "overview") return;
    let cancelled = false;
    fetchWikiBoundary(repoId, activeId)
      .then((b) => {
        if (!cancelled) setBoundary(b);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [repoId, activeId]);

  // Build "On this page" from the actually-rendered heading ids (matches rehype-slug).
  const rescanHeadings = useCallback(() => {
    const root = contentRef.current;
    if (!root) return setHeadings([]);
    const hs = Array.from(root.querySelectorAll<HTMLElement>("h1, h2, h3"))
      .filter((h) => h.id)
      .map((h) => ({ id: h.id, text: h.textContent || "", level: Number(h.tagName[1]) }));
    setHeadings(hs);
  }, []);

  useEffect(() => {
    const t = setTimeout(rescanHeadings, 80);
    return () => clearTimeout(t);
  }, [page, rescanHeadings]);

  // Scroll-spy: highlight the heading currently in view in the right rail.
  useEffect(() => {
    if (headings.length === 0) return;
    const els = headings
      .map((h) => document.getElementById(h.id))
      .filter((e): e is HTMLElement => !!e);
    if (els.length === 0) return;
    const obs = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveHeading(visible[0].target.id);
      },
      { rootMargin: "-64px 0px -70% 0px", threshold: 0 }
    );
    els.forEach((e) => obs.observe(e));
    setActiveHeading((cur) => cur || headings[0].id);
    return () => obs.disconnect();
  }, [headings]);

  function pick(id: string) {
    setActiveId(id);
    setTocOpen(false);
    const url = `${window.location.pathname}?p=${encodeURIComponent(id)}`;
    window.history.replaceState(null, "", url);
    window.scrollTo({ top: 0 });
  }

  // The document carries a `<base href>`, so a bare `#id` link resolves
  // against the site root and would leave the wiki for the landing page.
  // Scroll in place and record the fragment ourselves.
  function jumpToHeading(event: MouseEvent<HTMLAnchorElement>, id: string) {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    const el = document.getElementById(id);
    if (!el) return;
    event.preventDefault();
    el.scrollIntoView({ block: "start" });
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}#${id}`,
    );
    setActiveHeading(id);
  }

  // Esc closes the graph explorer modal.
  useEffect(() => {
    if (!graphOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setGraphOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [graphOpen]);

  useEffect(() => {
    if (!sourceCitation) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSourceCitation(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sourceCitation]);

  const hasGraph = !!repo?.capabilities?.codemap;
  const hasPageGraph = hasGraph || !!repo?.capabilities?.wiki_graph;
  const generationMode = page?.generation?.mode ?? "offline";
  // "Source checked" is the strict claim: source identifiers resolve and the
  // complete page passes its planned coverage checks. Generation mode is an
  // operator diagnostic, so it must not override those reader-facing checks.
  const sourceChecked = isSourceCheckedWikiPage(page);
  const withheldByQualityGuard = shouldWithholdWikiPage(page);
  const retryNotice = wikiRetryNotice(page);
  const evidenceRoutes = [
    ...new Set(page?.evidence?.items.flatMap((item) => item.routes) ?? []),
  ];
  // A fact-plan diagram was validated edge-by-edge against the page's
  // evidence before the backend rendered the fence, so it stays even when a
  // prose check later marked the page degraded; losing it left an orphan
  // caption under the heading.
  const wikiMedia = partitionWikiMediaSlots(page?.media_slots);
  // A typed visual (storyboard or architecture) draws the same validated
  // path as the fact-plan mermaid fence; showing both is two competing
  // pictures of one flow, so the fence yields to the richer rendering.
  const hasTypedVisual = [...wikiMedia.lead, ...wikiMedia.bridge, ...wikiMedia.body].some(
    (slot) => !!slot.render_contract?.adapter,
  );
  const renderedMarkdown = page
    ? stripGeneratedDiagrams(
        page.markdown,
        page.generation?.renderer === "fact_plan" && !hasTypedVisual,
      )
    : "";
  const wikiMarkdown = splitWikiMarkdown(renderedMarkdown);
  // On the Overview the recorded entry path is drawn inside the architecture
  // card, so the page tells one path instead of two stacked ones.
  const hasArchitecture = [...wikiMedia.lead, ...wikiMedia.bridge, ...wikiMedia.body].some(
    (slot) =>
      slot.render_contract?.adapter === "architecture" &&
      slot.render_contract?.provenance === "architecture-plan",
  );
  const lifted =
    page && activeId === "overview" && hasArchitecture
      ? extractJourney(wikiMarkdown.body)
      : { journey: null, rest: wikiMarkdown.body };
  const trace = lifted.journey
    ? {
        journey: lifted.journey,
        citations: page?.citations,
        onPick: pick,
        renderText: (markdown: string) => (
          <Markdown
            citations={page?.citations}
            relations={page?.evidence?.relations}
            onCite={(index) => setSourceCitation(page?.citations[index] ?? null)}
            repoId={repoId}
            onPageLink={pick}
          >
            {markdown}
          </Markdown>
        ),
      }
    : undefined;
  const openGraph = (seed?: string) => {
    setGraphSeed(seed);
    setGraphOpen(true);
  };

  return (
    <div className="wiki wiki-reading">
      <Header
        actions={
          repo?.capabilities?.chat ? (
            <AppLink
              className="icon-btn header-ask"
              href={`/${encodeURIComponent(repoId)}/ask`}
              aria-label={`Ask a question about ${repo.repo}`}
              title="Ask this repository"
            >
              <span aria-hidden>✦</span>
            </AppLink>
          ) : null
        }
        center={
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <button
              className="toc-toggle"
              aria-label="Toggle section list"
              onClick={() => setTocOpen((o) => !o)}
            >
              ☰
            </button>
            <span className="crumb-sep">/</span>
            <span className="crumb-repo mono">{repo ? repo.repo : repoId}</span>
            {(!staticRuntime || hasGraph) && (
              <button
                className={`codegraph-launch ${hasGraph ? "" : "unavailable"}`}
                onClick={() => openGraph()}
                title={
                  hasGraph
                    ? "Open the interactive code dependency graph"
                    : "Dependency graph is not indexed for this repository"
                }
                aria-label={
                  hasGraph
                    ? "Open dependency map"
                    : "Set up dependency map"
                }
              >
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  aria-hidden
                >
                  <circle cx="5" cy="6" r="2" />
                  <circle cx="19" cy="7" r="2" />
                  <circle cx="12" cy="19" r="2" />
                  <path d="m7 6.2 10 .6M6.5 8l4.4 9.2m6.5-8.3-4.3 8.4" />
                </svg>
                <span>Dependency Map</span>
              </button>
            )}
            {page && <span className="crumb-sep">/</span>}
            {page && <span className="crumb-page">{page.title}</span>}
          </nav>
        }
      />

      <div className="wiki-grid">
        {tocOpen && <div className="toc-scrim" onClick={() => setTocOpen(false)} aria-hidden />}
        <aside className={`wiki-toc ${tocOpen ? "open" : ""}`} data-rail="left">
          <div className="rail-title">{repo ? repo.repo : repoId}</div>
          {commits.length > 0 ? (
            <div className="rail-sub commit-picker">
              <label className="commit-picker-label" htmlFor="commit-select">
                Graph snapshot
              </label>
              <select
                id="commit-select"
                className="commit-select mono"
                value={selectedCommit ?? ""}
                onChange={(e) => setSelectedCommit(e.target.value || undefined)}
                title="Show the symbol graph as of this commit"
              >
                {commits.map((c) => (
                  <option key={c.sha} value={c.sha}>
                    {c.short} · {c.date} · {c.subject.slice(0, 40)}
                  </option>
                ))}
              </select>
              {commitCost && <div className="commit-evidence">{commitCost}</div>}
            </div>
          ) : (
            repo?.commit_short && (
              <div className="rail-sub mono">Last indexed {repo.commit_short}</div>
            )
          )}
          {error ? (
            <div className="page-load-error" role="alert">
              <div>Failed to load this wiki.</div>
              <div className="page-load-error-detail mono">{error}</div>
            </div>
          ) : tocLoading ? (
            <div className="toc-skeleton" aria-hidden>
              {Array.from({ length: 7 }).map((_, i) => (
                <div key={i} className="toc-skeleton-row" />
              ))}
            </div>
          ) : pages.length === 0 ? (
            <div className="muted small">No pages in this wiki yet.</div>
          ) : (
            <TocTree pages={pages} activeId={activeId} onPick={pick} />
          )}
        </aside>

        <main className="wiki-main">
          <div className="wiki-content" ref={contentRef}>
              {page && !withheldByQualityGuard && (
                <div className="wiki-story">
                  {wikiMarkdown.lead && (
                    <Markdown
                      citations={page.citations}
                      relations={page.evidence?.relations}
                      onCite={(index) =>
                        setSourceCitation(page.citations[index] ?? null)
                      }
                      repoId={repoId}
                      onPageLink={pick}
                    >
                      {wikiMarkdown.lead}
                    </Markdown>
                  )}
                  {activeId === "overview" && areaMap?.available && (
                    <AreaMap map={areaMap} commit={repo?.commit_short} onPick={pick} />
                  )}
                  {boundary?.available && (
                    <PageBoundaryCard
                      boundary={boundary}
                      commit={repo?.commit_short}
                      onFocus={hasGraph ? (symbol) => openGraph(symbol) : undefined}
                      onOpenMap={hasGraph ? () => openGraph() : undefined}
                    />
                  )}
                  {wikiMedia.lead.length > 0 && (
                    <MultimodalMedia
                      slots={wikiMedia.lead}
                      repo={repo}
                      variant="lead"
                      trace={trace}
                    />
                  )}
                  {wikiMedia.bridge.length > 0 && (
                    <MultimodalMedia
                      slots={wikiMedia.bridge}
                      repo={repo}
                      variant="bridge"
                      trace={trace}
                    />
                  )}
                  {lifted.rest && (
                    <Markdown
                      citations={page.citations}
                      relations={page.evidence?.relations}
                      onCite={(index) =>
                        setSourceCitation(page.citations[index] ?? null)
                      }
                      repoId={repoId}
                      onPageLink={pick}
                    >
                      {lifted.rest}
                    </Markdown>
                  )}
                  {wikiMedia.body.length > 0 && (
                    <MultimodalMedia slots={wikiMedia.body} repo={repo} trace={trace} />
                  )}
                </div>
              )}
              {page && (
                <div
                  className={`page-provenance ${
                    sourceChecked ? "source-checked" : "needs-review"
                  }`}
                  role="status"
                >
                  <span className="provenance-state">
                    {generationMode === "offline"
                      ? "Index-derived page"
                      : withheldByQualityGuard
                        ? "Explanation withheld by quality guard"
                        : sourceChecked
                          ? "Evidence-linked generation"
                          : page.generation?.fallback
                            ? "Index-derived fallback"
                            : "Generated, evidence review needed"}
                  </span>
                  {page.grounding && (
                    // The reader gets counts they can check; the pipeline's
                    // routes, review score, and visual audit stay in the
                    // tooltip for whoever maintains the page.
                    <span
                      className="provenance-detail"
                      title={[
                        evidenceRoutes.length > 0 && `Evidence routes: ${evidenceRoutes.join(" + ")}`,
                        page.quality?.story_review &&
                          `Reader review ${page.quality.story_review.score}/${page.quality.story_review.max_score}${
                            page.quality.story_review.notes ? `: ${page.quality.story_review.notes}` : ""
                          }`,
                        (page.visual_quality?.grounded_visuals ?? 0) > 0 &&
                          `${page.visual_quality?.grounded_visuals} source-grounded visual(s)`,
                      ]
                        .filter(Boolean)
                        .join("\n")}
                    >
                      {page.grounding.evidence_count} cited symbols
                      {page.grounding.relation_count > 0 &&
                        ` · ${page.grounding.relation_count} recorded calls`}
                      {page.grounding.citation_coverage < 1 &&
                        ` · ${Math.round(page.grounding.citation_coverage * 100)}% of blocks sourced`}
                    </span>
                  )}
                  {page.generation?.model && (
                    <span className="provenance-model mono">{page.generation.model}</span>
                  )}
                </div>
              )}
              {hasPageGraph && (
                <details
                  className="subsystem-map"
                  open={pageGraphOpen}
                  onToggle={(event) =>
                    setPageGraphOpen(event.currentTarget.open)
                  }
                >
                  <summary className="subsystem-summary">
                    <span className="subsystem-title">Subsystem map</span>
                    <span className="subsystem-count">
                      {pageGraph?.available && pageGraph.nodes.length > 0
                        ? `${pageGraph.nodes.length} symbols`
                        : pageGraph
                          ? "Unavailable"
                          : "Indexed graph"}
                    </span>
                    {repo?.commit_short && (
                      <span className="subsystem-index mono">Last indexed {repo.commit_short}</span>
                    )}
                  </summary>
                  {pageGraphOpen &&
                    (pageGraphLoading ? (
                      <div className="subsystem-loading">Loading subsystem map…</div>
                    ) : pageGraphError ||
                      !pageGraph?.available ||
                      pageGraph.nodes.length === 0 ? (
                      <div className="subsystem-loading">
                        {pageGraph?.note ||
                          "No source-linked graph is available for this page."}
                      </div>
                    ) : (
                      <Suspense
                        fallback={
                          <div className="subsystem-loading">Loading graph view…</div>
                        }
                      >
                        <GraphView
                          repoId={repoId}
                          data={pageGraph}
                          variant="wiki"
                          onFocus={hasGraph ? (label) => openGraph(label) : undefined}
                          repoFullName={repo?.repo}
                          sourceUrl={repo?.source_url}
                          commit={repo?.base_commit}
                        />
                      </Suspense>
                    ))}
                </details>
              )}
              {page?.evidence && page.evidence.items.length > 0 ? (
                <details className="evidence-ledger">
                  <summary>
                    Source evidence ({page.evidence.items.length})
                  </summary>
                  <div className="evidence-list">
                    {page.evidence.items.map((item) => {
                      const url = ghFileUrl(
                        repo?.repo,
                        repo?.source_url,
                        repo?.base_commit,
                        item.file,
                        item.start_line,
                        item.end_line
                      );
                      return (
                        <a
                          id={`evidence-${item.id}`}
                          key={item.id}
                          className="evidence-row"
                          href={url ?? undefined}
                          target={url ? "_blank" : undefined}
                          rel={url ? "noreferrer" : undefined}
                        >
                          <span className="evidence-id mono">{item.id}</span>
                          <span className="evidence-symbol mono">{item.symbol}</span>
                          <span className="evidence-location mono">
                            {item.file}
                            {item.start_line ? `:${item.start_line}` : ""}
                          </span>
                          {item.routes.length > 0 && (
                            <span className="evidence-route">{item.routes.join(" + ")}</span>
                          )}
                        </a>
                      );
                    })}
                    {page.evidence.relations.map((item) => (
                      <div
                        id={`evidence-${item.id}`}
                        key={item.id}
                        className="evidence-row relation"
                      >
                        <span className="evidence-id mono">{item.id}</span>
                        <span className="evidence-symbol mono">
                          {item.source} → {item.target}
                        </span>
                        <span className="evidence-location mono">
                          {item.anchors[0] ?? "static reference"}
                        </span>
                      </div>
                    ))}
                  </div>
                </details>
              ) : page && page.citations.length > 0 ? (
                <details className="relevant-files-wiki">
                  {(() => {
                    const wikiFiles = [
                      ...new Set(
                        page.citations.map((c) => repoRelative(c.file)).filter(Boolean)
                      ),
                    ];
                    return (
                      <>
                        <summary>Relevant source files ({wikiFiles.length})</summary>
                        <div className="relevant-files-list">
                          {wikiFiles.map((f) => {
                            const url = ghFileUrl(
                              repo?.repo,
                              repo?.source_url,
                              repo?.base_commit,
                              f,
                            );
                            return url ? (
                              <a
                                key={f}
                                className="relevant-file mono"
                                href={url}
                                target="_blank"
                                rel="noreferrer"
                                title={`Open ${f} on GitHub`}
                              >
                                {f} ↗
                              </a>
                            ) : (
                              <span key={f} className="relevant-file mono">
                                {f}
                              </span>
                            );
                          })}
                        </div>
                      </>
                    );
                  })()}
                </details>
              ) : null}
              {page ? (
                withheldByQualityGuard && (
                  <div className="page-load-error" role="alert">
                    <p>{retryNotice.headline}</p>
                    <p className="page-load-error-detail">
                      {retryNotice.detail}
                      {retryNotice.nextAttemptEpoch !== null && (
                        <>
                          {" "}
                          Cooldown ends{" "}
                          <time
                            dateTime={new Date(
                              retryNotice.nextAttemptEpoch * 1000,
                            ).toISOString()}
                          >
                            {new Date(retryNotice.nextAttemptEpoch * 1000).toLocaleString()}
                          </time>
                          .
                        </>
                      )}
                    </p>
                  </div>
                )
              ) : pageError ? (
                <div className="page-load-error" role="alert">
                  <p>
                    Couldn't load this page. Pick another section or retry after
                    fixing the backend.
                  </p>
                  <p className="page-load-error-detail mono">{pageError}</p>
                </div>
              ) : (
                <div className="page-loading-state" role="status" aria-live="polite">
                  <div className="page-loading-title">
                    {pageLoadSeconds < 2
                      ? "Loading the indexed page…"
                      : "Preparing a source-linked page…"}
                  </div>
                  {pageLoadSeconds >= 2 && (
                    <div className="page-loading-detail">
                      A first load retrieves evidence and may generate prose.
                      <span className="mono"> {pageLoadSeconds}s</span>
                    </div>
                  )}
                </div>
              )}
          </div>
        </main>

        <aside className="wiki-onthispage" data-rail="right">
          <div className="rail-title">On this page</div>
          {headings.length > 0 ? (
            <ul className="onthispage-list">
              {headings.map((h) => (
                <li key={h.id} className={`lvl-${h.level}`}>
                  <a
                    href={`#${h.id}`}
                    className={h.id === activeHeading ? "active" : ""}
                    onClick={(event) => jumpToHeading(event, h.id)}
                  >
                    {h.text}
                  </a>
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted small">—</div>
          )}
          {repo && (
            <button
              className="refresh-wiki"
              title="Re-fetch this wiki page"
              onClick={() => {
                fetchWikiTree(repoId).then((t) => setPages(t.pages)).catch(() => {});
                fetchWikiPage(repoId, activeId, { refresh: true })
                  .then(setPage)
                  .catch(() => {});
              }}
            >
              Refresh this wiki
            </button>
          )}
        </aside>
      </div>

      {graphOpen && (
        <div
          className="graph-modal-scrim"
          onClick={() => setGraphOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label="Repository dependency map"
        >
          <div className="graph-modal" onClick={(e) => e.stopPropagation()}>
            <div className="graph-modal-head">
              <b>Dependency Map</b>
              <span aria-hidden>·</span>
              <span className="graph-modal-repo mono">
                {repo ? repo.repo : repoId}
              </span>
              <span className="graph-modal-hint muted small">
                Symbol references at the indexed commit · Esc to close
              </span>
              <button
                className="graph-modal-close"
                onClick={() => setGraphOpen(false)}
                aria-label="Close graph"
              >
                ×
              </button>
            </div>
            <div className="graph-modal-body">
              <Suspense
                fallback={
                  <div className="codemap-loading">Loading dependency map…</div>
                }
              >
                <Codemap
                  repoId={repoId}
                  initialSymbol={graphSeed}
                  commit={selectedCommit}
                  coverage={repo?.graph_coverage}
                />
              </Suspense>
            </div>
          </div>
        </div>
      )}

      {sourceCitation && (
        <div
          className="source-modal-scrim"
          onClick={() => setSourceCitation(null)}
          role="dialog"
          aria-modal="true"
          aria-label="Source definition"
        >
          <div
            className="source-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="source-modal-head">
              <div>
                <span className="source-modal-kicker">Indexed definition</span>
                <div className="source-modal-title mono">
                  {sourceCitation.node_name ||
                    repoRelative(sourceCitation.file)}
                </div>
                <div className="source-modal-location mono">
                  {repoRelative(sourceCitation.file)}
                  {sourceCitation.start_line != null
                    ? `:${sourceCitation.start_line}-${
                        sourceCitation.end_line ??
                        sourceCitation.start_line
                      }`
                    : ""}
                </div>
              </div>
              <button
                className="source-modal-close"
                onClick={() => setSourceCitation(null)}
                aria-label="Close source"
              >
                ×
              </button>
            </div>
            <div className="source-modal-body">
              <Suspense
                fallback={
                  <div className="codemap-loading">Loading source…</div>
                }
              >
                <CodePanel
                  repoId={repoId}
                  citations={[sourceCitation]}
                  repo={repo?.repo}
                  commit={repo?.base_commit}
                />
              </Suspense>
            </div>
          </div>
        </div>
      )}

      {repo?.capabilities?.chat && (
        <AskBar repoId={repoId} repo={repo.repo} collapsible />
      )}
    </div>
  );
}
